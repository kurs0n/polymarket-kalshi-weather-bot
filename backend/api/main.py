"""FastAPI backend for weather temperature trading bot dashboard."""
from fastapi import FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from datetime import datetime
from typing import Dict, List, Optional
import asyncio
import os

from backend.config import settings
from backend.models.database import (
    get_db, init_db, SessionLocal,
    Signal, Trade, BotState, AILog
)

from pydantic import BaseModel

app = FastAPI(
    title="Weather Trading Bot",
    description="Polymarket + Kalshi weather temperature market trading bot",
    version="4.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                pass


ws_manager = ConnectionManager()


class TradeResponse(BaseModel):
    id: int
    market_ticker: str
    platform: str
    event_slug: Optional[str] = None
    direction: str
    entry_price: float
    size: float
    timestamp: datetime
    settled: bool
    result: str
    pnl: Optional[float]
    model_probability: Optional[float] = None
    edge_at_entry: Optional[float] = None
    confidence: Optional[float] = None
    # Added 2026-08-22 so the dashboard can show early exits (trailing-stop /
    # price-stop-loss / METAR liquidations) distinctly from trades held to a
    # real settlement, and how much of a position's peak gain was actually
    # captured before it sold — see position_liquidator_job in scheduler.py.
    execution_type: Optional[str] = None       # "liquidated" | "simulated" | "maker_limit" | "timed_out" | ...
    peak_gain_pct: Optional[float] = None       # highest unrealised gain ever seen, tracked every cycle
    settlement_value: Optional[float] = None    # exit price per contract (liquidation) or final settlement value
    settlement_source: Optional[str] = None     # "official" | "nws_early" | "trend_stop" | None (not yet settled)


class BotStats(BaseModel):
    bankroll: float
    total_trades: int
    winning_trades: int
    win_rate: float
    total_pnl: float
    is_running: bool
    last_run: Optional[datetime]


class CalibrationBucket(BaseModel):
    bucket: str
    predicted_avg: float
    actual_rate: float
    count: int


class CalibrationSummary(BaseModel):
    total_signals: int
    total_with_outcome: int
    accuracy: float
    avg_predicted_edge: float
    avg_actual_edge: float
    brier_score: float


class WeatherForecastResponse(BaseModel):
    model_config = {"protected_namespaces": ()}  # allow model_weights/model_probability field names

    city_key: str
    city_name: str
    target_date: str
    mean_high: float          # raw GFS ensemble mean — NOT what the bot trades on, see effective_mean_high
    std_high: float
    mean_low: float
    std_low: float
    num_members: int
    ensemble_agreement: float

    # The actual number generate_weather_signal() trades on: GFS blended
    # with HRRR (solar window), ECMWF/NWS cross-checks, the rolling
    # per-city bias correction, and (when enough settled history exists)
    # the rolling per-model accuracy weighting — see
    # EnsembleForecast.effective_mean_high() in backend/data/weather.py.
    # Exposed separately from mean_high (not as a replacement) so the
    # dashboard can show both the raw ensemble and the blend that actually
    # drove the trade, instead of silently only ever showing the former.
    effective_mean_high: float
    hrrr_high: Optional[float] = None
    ecmwf_high: Optional[float] = None
    nws_high: Optional[float] = None
    bias_correction_f: float = 0.0
    model_weights: Optional[Dict[str, float]] = None


class WeatherMarketResponse(BaseModel):
    slug: str
    market_id: str
    platform: str = "polymarket"
    title: str
    city_key: str
    city_name: str
    target_date: str
    threshold_f: float
    metric: str
    direction: str
    yes_price: float
    no_price: float
    volume: float


class WeatherSignalResponse(BaseModel):
    model_config = {"protected_namespaces": ()}  # allow model_probability field name

    market_id: str
    city_key: str
    city_name: str
    target_date: str
    threshold_f: float
    metric: str
    direction: str
    model_probability: float
    market_probability: float
    edge: float
    confidence: float
    kelly_fraction: float = 0.0
    suggested_size: float
    sources: List[str] = []
    reasoning: str
    ensemble_mean: float
    ensemble_std: float
    ensemble_members: int
    actionable: bool = False
    platform: str = "polymarket"


class TailCalibrationBucket(BaseModel):
    """One row of the rolling empirical tail-calibration table — see
    backend/core/calibration.py. Only buckets with enough settled trade
    history to be trusted (>= MIN_BUCKET_SAMPLES) ever appear here."""
    bucket: str                # e.g. "95%-100%"
    n: int
    empirical_win_rate: float


class DashboardData(BaseModel):
    stats: BotStats
    recent_trades: List[TradeResponse]
    equity_curve: List[dict]
    calibration: Optional[CalibrationSummary] = None
    tail_calibration: List[TailCalibrationBucket] = []
    weather_signals: List[WeatherSignalResponse] = []
    weather_forecasts: List[WeatherForecastResponse] = []


class EventResponse(BaseModel):
    timestamp: str
    type: str
    message: str
    data: dict = {}


async def _fetch_kalshi_live_balance() -> Optional[float]:
    """
    Return the live Kalshi portfolio balance in dollars, or None on failure.
    Uses balance_dollars from the /portfolio/balance endpoint (full precision).
    """
    try:
        from backend.data.kalshi_client import KalshiClient, kalshi_credentials_present
        if not kalshi_credentials_present():
            return None
        client = KalshiClient()
        resp = await client.get_balance()
        bal_str = resp.get("balance_dollars")
        if bal_str:
            return float(bal_str)
        cents = resp.get("balance")
        if cents is not None:
            return int(cents) / 100.0
    except Exception as e:
        print(f"  Warning: could not fetch Kalshi balance: {e}")
    return None


@app.on_event("startup")
async def startup():
    print("=" * 60)
    print("WEATHER TRADING BOT v4.0")
    print("=" * 60)
    print("Initializing database...")

    init_db()

    db = SessionLocal()
    try:
        state = db.query(BotState).first()
        if not state:
            # First run — seed from config
            state = BotState(
                bankroll=settings.INITIAL_BANKROLL,
                total_trades=0,
                winning_trades=0,
                total_pnl=0.0,
                is_running=True
            )
            db.add(state)
            db.commit()
            print(f"Created new bot state with ${settings.INITIAL_BANKROLL:,.2f} bankroll")
        else:
            state.is_running = True

            if not settings.SIMULATION_MODE:
                # Live mode: cap the operational bankroll at INITIAL_BANKROLL so
                # the .env setting acts as a hard budget ceiling regardless of the
                # total Kalshi account balance.
                live_balance = await _fetch_kalshi_live_balance()
                cap = float(settings.INITIAL_BANKROLL)
                if live_balance is not None:
                    capped = min(live_balance, cap)
                    print(
                        f"LIVE MODE: Kalshi balance ${live_balance:,.2f}, "
                        f"INITIAL_BANKROLL cap ${cap:,.2f} → bankroll set to ${capped:,.2f}"
                    )
                    state.bankroll = capped
                else:
                    # Kalshi API unavailable — fall back to configured initial bankroll
                    print(
                        f"LIVE MODE: Kalshi balance unavailable, "
                        f"resetting bankroll to ${cap:,.2f}"
                    )
                    state.bankroll = cap

                # Record the live session start time and baseline balance once
                # (preserved across restarts so the dashboard sees the full live history).
                if state.live_session_start is None:
                    state.live_session_start = datetime.utcnow()
                    state.live_start_balance = state.bankroll
                    print(
                        f"LIVE MODE: new live session started at {state.live_session_start.isoformat()} "
                        f"with ${state.live_start_balance:,.2f}"
                    )
                else:
                    print(
                        f"LIVE MODE: resuming live session started {state.live_session_start.isoformat()}"
                    )

            db.commit()
            print(
                f"Bot state: Bankroll ${state.bankroll:,.2f} | "
                f"P&L ${state.total_pnl:+,.2f} | {state.total_trades} trades"
            )
    finally:
        db.close()

    print("")
    print("Configuration:")
    print(f"  - Simulation mode: {settings.SIMULATION_MODE}")
    print(f"  - Min edge threshold: {settings.WEATHER_MIN_EDGE_THRESHOLD:.0%}")
    print(f"  - Kelly fraction: {settings.KELLY_FRACTION:.0%}")
    print(f"  - Scan interval: {settings.WEATHER_SCAN_INTERVAL_SECONDS}s")
    print(f"  - Settlement interval: {settings.SETTLEMENT_INTERVAL_SECONDS}s")
    print(f"  - Cities: {settings.WEATHER_CITIES}")
    print(f"  - Kalshi enabled: {settings.KALSHI_ENABLED}")
    print("")

    from backend.core.scheduler import start_scheduler, log_event
    start_scheduler()
    log_event("success", "Weather trading bot initialized")

    print("Bot is now running!")
    print(f"  - Weather scan: every {settings.WEATHER_SCAN_INTERVAL_SECONDS}s (edge >= {settings.WEATHER_MIN_EDGE_THRESHOLD:.0%})")
    print(f"  - Settlement check: every {settings.SETTLEMENT_INTERVAL_SECONDS}s")
    print("=" * 60)


@app.on_event("shutdown")
async def shutdown():
    from backend.core.scheduler import stop_scheduler
    stop_scheduler()


@app.get("/")
async def root():
    return {
        "status": "ok",
        "message": "Weather Trading Bot API v4.0",
        "simulation_mode": settings.SIMULATION_MODE
    }


@app.get("/api/health")
async def health():
    return {"status": "healthy"}


@app.get("/api/stats", response_model=BotStats)
async def get_stats(db: Session = Depends(get_db)):
    state = db.query(BotState).first()
    if not state:
        raise HTTPException(status_code=404, detail="Bot state not initialized")

    live_mode = not settings.SIMULATION_MODE

    if live_mode and state.live_session_start:
        # Derive trade stats from DB so only live-session trades count — the
        # BotState counters include all historical paper trades too.
        live_trades = (
            db.query(Trade)
            .filter(Trade.timestamp >= state.live_session_start)
            .all()
        )
        settled_live = [
            t for t in live_trades
            if t.settled and t.result not in ("timed_out", "pending")
        ]
        total_trades = len(live_trades)
        winning_trades = sum(1 for t in settled_live if t.result == "win")
        total_pnl = sum(t.pnl for t in settled_live if t.pnl is not None)
        win_rate = winning_trades / total_trades if total_trades > 0 else 0.0
    else:
        total_trades = state.total_trades
        winning_trades = state.winning_trades
        win_rate = winning_trades / total_trades if total_trades > 0 else 0.0
        total_pnl = state.total_pnl

    return BotStats(
        bankroll=state.bankroll,
        total_trades=total_trades,
        winning_trades=winning_trades,
        win_rate=win_rate,
        total_pnl=total_pnl,
        is_running=state.is_running,
        last_run=state.last_run
    )


@app.get("/api/trades", response_model=List[TradeResponse])
async def get_trades(
    limit: int = 50,
    status: Optional[str] = None,
    db: Session = Depends(get_db)
):
    query = db.query(Trade)
    if status:
        query = query.filter(Trade.result == status)
    trades = query.order_by(Trade.timestamp.desc()).limit(limit).all()

    return [
        TradeResponse(
            id=t.id,
            market_ticker=t.market_ticker,
            platform=t.platform,
            event_slug=t.event_slug,
            direction=t.direction,
            entry_price=t.entry_price,
            size=t.size,
            timestamp=t.timestamp,
            settled=t.settled,
            result=t.result,
            pnl=t.pnl,
            model_probability=t.model_probability,
            edge_at_entry=t.edge_at_entry,
            confidence=t.confidence,
            execution_type=t.execution_type,
            peak_gain_pct=t.peak_gain_pct,
            settlement_value=t.settlement_value,
            settlement_source=t.settlement_source,
        )
        for t in trades
    ]


@app.get("/api/equity-curve")
async def get_equity_curve(db: Session = Depends(get_db)):
    live_mode = not settings.SIMULATION_MODE

    if live_mode:
        state = db.query(BotState).first()
        if not state or not state.live_session_start:
            return []
        cutoff = state.live_session_start
        start_balance = state.live_start_balance or float(settings.INITIAL_BANKROLL)
        trades = (
            db.query(Trade)
            .filter(Trade.settled == True, Trade.timestamp >= cutoff)
            .order_by(Trade.timestamp)
            .all()
        )
    else:
        start_balance = float(settings.INITIAL_BANKROLL)
        trades = db.query(Trade).filter(Trade.settled == True).order_by(Trade.timestamp).all()

    curve = []
    cumulative_pnl = 0.0
    for trade in trades:
        if trade.pnl is not None:
            cumulative_pnl += trade.pnl
            curve.append({
                "timestamp": trade.timestamp.isoformat(),
                "pnl": cumulative_pnl,
                "bankroll": start_balance + cumulative_pnl,
                "trade_id": trade.id
            })

    return curve


@app.post("/api/simulate-trade")
async def simulate_trade(signal_ticker: str, db: Session = Depends(get_db)):
    from backend.core.scheduler import log_event
    from backend.core.weather_signals import scan_for_weather_signals

    signals = await scan_for_weather_signals()
    signal = next((s for s in signals if s.market.market_id == signal_ticker), None)

    if not signal:
        raise HTTPException(status_code=404, detail="Signal not found")

    state = db.query(BotState).first()
    if not state:
        raise HTTPException(status_code=500, detail="Bot state not initialized")

    entry_price = signal.market.yes_price if signal.direction == "yes" else signal.market.no_price

    trade = Trade(
        market_ticker=signal.market.market_id,
        platform=signal.market.platform,
        event_slug=signal.market.slug,
        market_type="weather",
        direction=signal.direction,
        entry_price=entry_price,
        size=min(signal.suggested_size, state.bankroll * 0.05),
        model_probability=signal.model_probability,
        market_price_at_entry=signal.market_probability,
        edge_at_entry=signal.edge
    )

    db.add(trade)
    state.total_trades += 1
    db.commit()

    log_event("trade", f"Manual weather trade: {signal.direction.upper()} {signal.market.city_name}")
    return {"status": "ok", "trade_id": trade.id, "size": trade.size}


@app.post("/api/run-scan")
async def run_scan(db: Session = Depends(get_db)):
    from backend.core.scheduler import run_manual_scan, log_event

    state = db.query(BotState).first()
    if state:
        state.last_run = datetime.utcnow()
        db.commit()

    log_event("info", "Manual weather scan triggered")
    await run_manual_scan()

    from backend.core.weather_signals import scan_for_weather_signals
    wx_signals = await scan_for_weather_signals()
    wx_actionable = [s for s in wx_signals if s.passes_threshold]

    return {
        "status": "ok",
        "weather_signals": len(wx_signals),
        "weather_actionable": len(wx_actionable),
        "total_signals": len(wx_signals),
        "actionable_signals": len(wx_actionable),
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.post("/api/settle-trades")
async def settle_trades_endpoint(db: Session = Depends(get_db)):
    from backend.core.settlement import settle_pending_trades, update_bot_state_with_settlements
    from backend.core.scheduler import log_event

    log_event("info", "Manual settlement triggered")

    settled = await settle_pending_trades(db)
    await update_bot_state_with_settlements(db, settled)

    return {
        "status": "ok",
        "settled_count": len(settled),
        "trades": [{"id": t.id, "result": t.result, "pnl": t.pnl} for t in settled]
    }


def _compute_calibration_summary(db: Session) -> Optional[CalibrationSummary]:
    """Compute calibration summary from settled signals."""
    total_signals = db.query(Signal).count()
    settled_signals = db.query(Signal).filter(Signal.outcome_correct.isnot(None)).all()

    if not settled_signals:
        if total_signals == 0:
            return None
        return CalibrationSummary(
            total_signals=total_signals,
            total_with_outcome=0,
            accuracy=0.0,
            avg_predicted_edge=0.0,
            avg_actual_edge=0.0,
            brier_score=0.0,
        )

    total_with_outcome = len(settled_signals)
    correct = sum(1 for s in settled_signals if s.outcome_correct)
    accuracy = correct / total_with_outcome if total_with_outcome > 0 else 0.0

    avg_predicted_edge = sum(abs(s.edge) for s in settled_signals) / total_with_outcome
    avg_actual_edge = sum(
        abs(s.edge) if s.outcome_correct else -abs(s.edge)
        for s in settled_signals
    ) / total_with_outcome

    brier_sum = 0.0
    for s in settled_signals:
        actual = s.settlement_value if s.settlement_value is not None else 0.5
        brier_sum += (s.model_probability - actual) ** 2
    brier_score = brier_sum / total_with_outcome

    return CalibrationSummary(
        total_signals=total_signals,
        total_with_outcome=total_with_outcome,
        accuracy=accuracy,
        avg_predicted_edge=avg_predicted_edge,
        avg_actual_edge=avg_actual_edge,
        brier_score=brier_score,
    )


@app.get("/api/calibration")
async def get_calibration(db: Session = Depends(get_db)):
    """
    Return calibration data: predicted probability vs actual win rate.

    Root-caused 2026-08-17: this used to bucket by raw model_probability
    (P(YES)) but paired each bucket with outcome_correct, which is already
    DIRECTION-aware (direction == actual_outcome). A high-confidence NO
    signal at model_probability=0.05 (i.e. 95% confident in NO) landed in
    the "0-5%" bucket labeled ~5% predicted, while its actual correctness
    rate would show ~95% — looking like severe miscalibration when it was
    really just an axis mismatch. Buckets are now keyed by the model's own
    implied WIN probability for the side it actually predicted (same
    correction used by backend/core/calibration.py's tail-calibration
    table), which is the number outcome_correct is actually measuring
    against.
    """
    signals = db.query(Signal).filter(Signal.outcome_correct.isnot(None)).all()

    if not signals:
        return {"buckets": [], "summary": None}

    from collections import defaultdict
    buckets_data = defaultdict(lambda: {"predicted_sum": 0.0, "correct": 0, "total": 0})

    for s in signals:
        win_prob = s.model_probability if s.direction in ("yes", "up") else (1.0 - s.model_probability)
        bin_start = int(win_prob * 100 // 5) * 5
        bin_end = bin_start + 5
        bucket_key = f"{bin_start}-{bin_end}%"

        buckets_data[bucket_key]["predicted_sum"] += win_prob
        buckets_data[bucket_key]["total"] += 1
        if s.outcome_correct:
            buckets_data[bucket_key]["correct"] += 1

    buckets = []
    for bucket_key in sorted(buckets_data.keys()):
        d = buckets_data[bucket_key]
        buckets.append(CalibrationBucket(
            bucket=bucket_key,
            predicted_avg=d["predicted_sum"] / d["total"],
            actual_rate=d["correct"] / d["total"],
            count=d["total"],
        ))

    summary = _compute_calibration_summary(db)

    return {"buckets": buckets, "summary": summary}


@app.get("/api/kalshi/status")
async def get_kalshi_status():
    """Test Kalshi API authentication and return connection status."""
    from backend.data.kalshi_client import KalshiClient, kalshi_credentials_present

    if not kalshi_credentials_present():
        return {
            "connected": False,
            "error": "Kalshi credentials not configured (KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH)",
        }

    try:
        client = KalshiClient()
        balance_data = await client.get_balance()
        return {
            "connected": True,
            "balance": balance_data,
        }
    except Exception as e:
        return {
            "connected": False,
            "error": str(e),
        }


def _forecast_to_response(forecast) -> WeatherForecastResponse:
    """
    Build the API response for one EnsembleForecast, including the blended
    effective_mean_high() the bot actually trades on (see the field's
    docstring on WeatherForecastResponse) alongside the raw ensemble mean.
    """
    return WeatherForecastResponse(
        city_key=forecast.city_key,
        city_name=forecast.city_name,
        target_date=forecast.target_date.isoformat(),
        mean_high=forecast.mean_high,
        std_high=forecast.std_high,
        mean_low=forecast.mean_low,
        std_low=forecast.std_low,
        num_members=forecast.num_members,
        ensemble_agreement=forecast.ensemble_agreement,
        effective_mean_high=forecast.effective_mean_high(),
        hrrr_high=forecast.hrrr_high,
        ecmwf_high=forecast.ecmwf_high,
        nws_high=forecast.nws_high,
        bias_correction_f=forecast.bias_correction_f,
        model_weights=forecast.model_weights,
    )


async def _get_weather_forecasts_impl() -> List[WeatherForecastResponse]:
    """
    Fetch one forecast per (city, target_date) actually in play — i.e. every
    date the cached signals cover, not just today. Falls back to "today only,
    every configured city" when there's no cached-signal history yet (cold
    start / WEATHER_ENABLED just turned on), which reproduces the previous
    behaviour rather than returning nothing.

    Root-caused 2026-08-17: the old version always called
    fetch_ensemble_forecast(city_key) with no target_date, defaulting to
    date.today() — a city whose only actionable signal was for TOMORROW
    still showed today's (different-day) forecast stats paired with it in
    the UI, keyed only by city_key.
    """
    from backend.data.weather import fetch_ensemble_forecast, CITY_CONFIG
    from backend.core.weather_signals import get_cached_weather_signals

    city_keys = [c.strip() for c in settings.WEATHER_CITIES.split(",") if c.strip()]

    pairs = sorted({
        (s.market.city_key, s.market.target_date)
        for s in get_cached_weather_signals()
        if s.market.city_key in CITY_CONFIG
    })
    if not pairs:
        import datetime as _dt
        today = _dt.date.today()
        pairs = [(city_key, today) for city_key in city_keys if city_key in CITY_CONFIG]

    forecasts = []
    for city_key, target_date in pairs:
        forecast = await fetch_ensemble_forecast(city_key, target_date)
        if forecast:
            forecasts.append(_forecast_to_response(forecast))
    return forecasts


@app.get("/api/weather/forecasts", response_model=List[WeatherForecastResponse])
async def get_weather_forecasts():
    """Get ensemble forecasts for every (city, date) the current signals cover."""
    if not settings.WEATHER_ENABLED:
        return []
    try:
        return await _get_weather_forecasts_impl()
    except Exception:
        return []


@app.get("/api/weather/markets", response_model=List[WeatherMarketResponse])
async def get_weather_markets():
    """Get active weather temperature markets."""
    if not settings.WEATHER_ENABLED:
        return []

    try:
        from backend.data.weather_markets import fetch_polymarket_weather_markets

        city_keys = [c.strip() for c in settings.WEATHER_CITIES.split(",") if c.strip()]
        markets = await fetch_polymarket_weather_markets(city_keys)

        if settings.KALSHI_ENABLED:
            try:
                from backend.data.kalshi_client import kalshi_credentials_present
                from backend.data.kalshi_markets import fetch_kalshi_weather_markets
                if kalshi_credentials_present():
                    kalshi_markets = await fetch_kalshi_weather_markets(city_keys)
                    markets.extend(kalshi_markets)
            except Exception:
                pass

        return [
            WeatherMarketResponse(
                slug=m.slug,
                market_id=m.market_id,
                platform=m.platform,
                title=m.title,
                city_key=m.city_key,
                city_name=m.city_name,
                target_date=m.target_date.isoformat(),
                threshold_f=m.threshold_f,
                metric=m.metric,
                direction=m.direction,
                yes_price=m.yes_price,
                no_price=m.no_price,
                volume=m.volume,
            )
            for m in markets
        ]
    except Exception:
        return []


@app.get("/api/weather/signals", response_model=List[WeatherSignalResponse])
async def get_weather_signals():
    """Get current weather trading signals."""
    if not settings.WEATHER_ENABLED:
        return []

    try:
        from backend.core.weather_signals import scan_for_weather_signals

        signals = await scan_for_weather_signals()
        return [_weather_signal_to_response(s) for s in signals]
    except Exception:
        return []


def _weather_signal_to_response(s) -> WeatherSignalResponse:
    return WeatherSignalResponse(
        market_id=s.market.market_id,
        city_key=s.market.city_key,
        city_name=s.market.city_name,
        target_date=s.market.target_date.isoformat(),
        threshold_f=s.market.threshold_f,
        metric=s.market.metric,
        direction=s.direction,
        model_probability=s.model_probability,
        market_probability=s.market_probability,
        edge=s.edge,
        confidence=s.confidence,
        kelly_fraction=s.kelly_fraction,
        suggested_size=s.suggested_size,
        sources=s.sources,
        reasoning=s.reasoning,
        ensemble_mean=s.ensemble_mean,
        ensemble_std=s.ensemble_std,
        ensemble_members=s.ensemble_members,
        actionable=s.passes_threshold,
        platform=s.market.platform,
    )


@app.get("/api/events", response_model=List[EventResponse])
async def get_events(limit: int = 50, db: Session = Depends(get_db)):
    from backend.core.scheduler import get_recent_events
    events = get_recent_events(200)  # fetch generously, then filter+slice

    live_mode = not settings.SIMULATION_MODE
    if live_mode:
        state = db.query(BotState).first()
        if state and state.live_session_start:
            cutoff_str = state.live_session_start.isoformat()
            events = [e for e in events if e.get("timestamp", "") >= cutoff_str]

    events = events[-limit:]  # most-recent `limit` after filtering
    return [
        EventResponse(
            timestamp=e["timestamp"],
            type=e["type"],
            message=e["message"],
            data=e.get("data", {})
        )
        for e in events
    ]


@app.post("/api/bot/start")
async def start_bot(db: Session = Depends(get_db)):
    from backend.core.scheduler import start_scheduler, log_event, is_scheduler_running

    state = db.query(BotState).first()
    if state:
        state.is_running = True
        db.commit()

    if not is_scheduler_running():
        start_scheduler()

    log_event("success", "Weather trading bot started")
    return {"status": "started", "is_running": True}


@app.post("/api/bot/stop")
async def stop_bot(db: Session = Depends(get_db)):
    from backend.core.scheduler import log_event

    state = db.query(BotState).first()
    if state:
        state.is_running = False
        db.commit()

    log_event("info", "Trading bot paused")
    return {"status": "stopped", "is_running": False}


@app.post("/api/bot/reset")
async def reset_bot(db: Session = Depends(get_db)):
    from backend.core.scheduler import log_event

    try:
        trades_deleted = db.query(Trade).delete()
        state = db.query(BotState).first()
        if state:
            state.bankroll = settings.INITIAL_BANKROLL
            state.total_trades = 0
            state.winning_trades = 0
            state.total_pnl = 0.0
            state.is_running = True
            state.live_session_start = None
            state.live_start_balance = None

        ai_logs_deleted = db.query(AILog).delete()
        db.commit()

        log_event("success", f"Bot reset: {trades_deleted} trades deleted. Fresh start with ${settings.INITIAL_BANKROLL:,.2f}")

        return {
            "status": "reset",
            "trades_deleted": trades_deleted,
            "ai_logs_deleted": ai_logs_deleted,
            "new_bankroll": settings.INITIAL_BANKROLL
        }

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Reset failed: {e}")


@app.get("/api/dashboard", response_model=DashboardData)
async def get_dashboard(db: Session = Depends(get_db)):
    """Get all dashboard data in one call."""
    stats = await get_stats(db)

    live_mode = not settings.SIMULATION_MODE
    state = db.query(BotState).first()
    live_cutoff = state.live_session_start if (live_mode and state) else None
    live_start_balance = (
        (state.live_start_balance or float(settings.INITIAL_BANKROLL))
        if (live_mode and state)
        else float(settings.INITIAL_BANKROLL)
    )

    trades_q = db.query(Trade)
    if live_cutoff:
        trades_q = trades_q.filter(Trade.timestamp >= live_cutoff)
    trades = trades_q.order_by(Trade.timestamp.desc()).limit(50).all()
    recent_trades = [
        TradeResponse(
            id=t.id,
            market_ticker=t.market_ticker,
            platform=t.platform,
            event_slug=t.event_slug,
            direction=t.direction,
            entry_price=t.entry_price,
            size=t.size,
            timestamp=t.timestamp,
            settled=t.settled,
            result=t.result,
            pnl=t.pnl,
            model_probability=t.model_probability,
            edge_at_entry=t.edge_at_entry,
            confidence=t.confidence,
            execution_type=t.execution_type,
            peak_gain_pct=t.peak_gain_pct,
            settlement_value=t.settlement_value,
            settlement_source=t.settlement_source,
        )
        for t in trades
    ]

    eq_q = db.query(Trade).filter(Trade.settled == True)
    if live_cutoff:
        eq_q = eq_q.filter(Trade.timestamp >= live_cutoff)
    equity_trades = eq_q.order_by(Trade.timestamp).all()
    equity_curve = []
    cumulative_pnl = 0.0
    for trade in equity_trades:
        if trade.pnl is not None:
            cumulative_pnl += trade.pnl
            equity_curve.append({
                "timestamp": trade.timestamp.isoformat(),
                "pnl": cumulative_pnl,
                "bankroll": live_start_balance + cumulative_pnl
            })

    calibration = _compute_calibration_summary(db)

    tail_calibration_data = []
    try:
        from backend.core.calibration import get_calibration_table
        table = await get_calibration_table()
        tail_calibration_data = [
            TailCalibrationBucket(
                bucket=f"{lo:.0%}-{hi:.0%}",
                n=entry["n"],
                empirical_win_rate=entry["empirical_win_rate"],
            )
            for (lo, hi), entry in sorted(table.items())
        ]
    except Exception:
        pass

    weather_signals_data = []
    weather_forecasts_data = []
    if settings.WEATHER_ENABLED:
        try:
            from backend.core.weather_signals import get_cached_weather_signals

            # Read the scheduled scan's cached result rather than running a
            # fresh live market scan on every dashboard poll (this endpoint
            # is hit every 10s by the frontend) — see the cache's comment in
            # weather_signals.py for why that mattered.
            wx_signals = get_cached_weather_signals()
            weather_signals_data = [_weather_signal_to_response(s) for s in wx_signals]
            weather_forecasts_data = await _get_weather_forecasts_impl()
        except Exception:
            pass

    return DashboardData(
        stats=stats,
        recent_trades=recent_trades,
        equity_curve=equity_curve,
        calibration=calibration,
        tail_calibration=tail_calibration_data,
        weather_signals=weather_signals_data,
        weather_forecasts=weather_forecasts_data,
    )


@app.websocket("/ws/events")
async def websocket_events(websocket: WebSocket):
    await ws_manager.connect(websocket)

    try:
        await websocket.send_json({
            "timestamp": datetime.utcnow().isoformat(),
            "type": "success",
            "message": "Connected to weather trading bot"
        })

        from backend.core.scheduler import get_recent_events
        for event in get_recent_events(20):
            await websocket.send_json(event)

        last_event_count = len(get_recent_events(200))
        while True:
            await asyncio.sleep(2)

            current_events = get_recent_events(200)
            if len(current_events) > last_event_count:
                new_events = current_events[last_event_count - len(current_events):]
                for event in new_events:
                    await websocket.send_json(event)
                last_event_count = len(current_events)

            await websocket.send_json({
                "type": "heartbeat",
                "timestamp": datetime.utcnow().isoformat()
            })

    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception:
        ws_manager.disconnect(websocket)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
