"""FastAPI backend for weather temperature trading bot dashboard."""
from fastapi import FastAPI, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from datetime import datetime
from typing import List, Optional
import asyncio
import os

from backend.config import settings
from backend.models.database import (
    get_db, init_db, SessionLocal,
    Signal, Trade, BotState, AILog
)
from backend.models.outcomes import Outcome
from backend.api.schemas import (
    ActionResponse,
    BotStats,
    CalibrationBucket,
    CalibrationResponse,
    CalibrationSummary,
    DashboardData,
    EquityPoint,
    EventResponse,
    HealthResponse,
    KalshiStatusResponse,
    ResetResponse,
    RootResponse,
    ScanResponse,
    SettlementResponse,
    SimulatedTradeResponse,
    TradeResponse,
    WeatherForecastResponse,
    WeatherMarketResponse,
    WeatherSignalResponse,
)

app = FastAPI(
    title="Weather Trading Bot",
    description=(
        "API for a simulation-only weather temperature trading bot. "
        "It combines Polymarket and optional Kalshi market data with "
        "Open-Meteo ensemble forecasts to generate signals, track paper "
        "trades, and measure calibration. No live orders are submitted."
    ),
    version="4.0.0",
    openapi_tags=[
        {"name": "System", "description": "Service health and API information."},
        {"name": "Dashboard", "description": "Aggregated data used by the dashboard."},
        {"name": "Trading", "description": "Simulation, scanning, and settlement operations."},
        {"name": "Weather", "description": "Forecasts, markets, and model-generated signals."},
        {"name": "Bot", "description": "Start, stop, and reset the scheduler state."},
        {"name": "Calibration", "description": "Signal outcome and probability calibration data."},
        {"name": "Integrations", "description": "External market integration status."},
    ],
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
            db.commit()
            print(f"Loaded bot state: Bankroll ${state.bankroll:,.2f}, P&L ${state.total_pnl:+,.2f}, {state.total_trades} trades")
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


@app.get("/", response_model=RootResponse, tags=["System"], summary="Get API status")
async def root():
    return {
        "status": "ok",
        "message": "Weather Trading Bot API v4.0",
        "simulation_mode": settings.SIMULATION_MODE
    }


@app.get("/api/health", response_model=HealthResponse, tags=["System"], summary="Check service health")
async def health():
    return {"status": "healthy"}


@app.get(
    "/api/stats",
    response_model=BotStats,
    tags=["Dashboard"],
    summary="Get bot statistics",
    description="Return the current simulated bankroll, trade totals, P&L, and scheduler state.",
    responses={404: {"description": "Bot state has not been initialized."}},
)
async def get_stats(db: Session = Depends(get_db)):
    state = db.query(BotState).first()
    if not state:
        raise HTTPException(status_code=404, detail="Bot state not initialized")

    win_rate = state.winning_trades / state.total_trades if state.total_trades > 0 else 0

    return BotStats(
        bankroll=state.bankroll,
        total_trades=state.total_trades,
        winning_trades=state.winning_trades,
        win_rate=win_rate,
        total_pnl=state.total_pnl,
        is_running=state.is_running,
        last_run=state.last_run
    )


@app.get(
    "/api/trades",
    response_model=List[TradeResponse],
    tags=["Dashboard"],
    summary="List simulated trades",
    description="Return recent simulated trades, optionally filtered by settlement result.",
)
async def get_trades(
    limit: int = Query(50, ge=1, le=500, description="Maximum number of trades to return."),
    status: Optional[str] = Query(None, description="Optional result filter, such as pending, win, or loss."),
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
            pnl=t.pnl
        )
        for t in trades
    ]


@app.get(
    "/api/equity-curve",
    response_model=List[EquityPoint],
    tags=["Dashboard"],
    summary="Get the simulated equity curve",
    description="Return cumulative realized P&L and simulated bankroll after each settled trade.",
)
async def get_equity_curve(db: Session = Depends(get_db)):
    trades = db.query(Trade).filter(Trade.settled == True).order_by(Trade.timestamp).all()

    curve = []
    cumulative_pnl = 0
    bankroll = settings.INITIAL_BANKROLL

    for trade in trades:
        if trade.pnl is not None:
            cumulative_pnl += trade.pnl
            curve.append({
                "timestamp": trade.timestamp.isoformat(),
                "pnl": cumulative_pnl,
                "bankroll": bankroll + cumulative_pnl,
                "trade_id": trade.id
            })

    return curve


@app.post(
    "/api/simulate-trade",
    response_model=SimulatedTradeResponse,
    tags=["Trading"],
    summary="Create a simulated trade",
    description="Find a current weather signal and create a paper trade. This endpoint never submits a live order.",
    responses={404: {"description": "No current signal matches the requested market ticker."}, 500: {"description": "Bot state has not been initialized."}},
)
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

    entry_price = signal.market.yes_price if signal.direction == Outcome.YES else signal.market.no_price

    trade = Trade(
        market_ticker=signal.market.market_id,
        platform=signal.market.platform,
        event_slug=signal.market.slug,
        market_type="weather",
        direction=signal.direction.value,
        entry_price=entry_price,
        size=min(signal.suggested_size, state.bankroll * 0.05, settings.WEATHER_MAX_TRADE_SIZE),
        model_probability=signal.model_probability,
        market_price_at_entry=signal.market_probability,
        edge_at_entry=signal.edge
    )

    db.add(trade)
    state.total_trades += 1
    db.commit()

    log_event("trade", f"Manual weather trade: {signal.direction.upper()} {signal.market.city_name}")
    return {"status": "ok", "trade_id": trade.id, "size": trade.size}


@app.post(
    "/api/run-scan",
    response_model=ScanResponse,
    tags=["Trading"],
    summary="Run a weather market scan",
    description="Trigger an immediate scan and return the number of generated and actionable signals.",
)
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


@app.post(
    "/api/settle-trades",
    response_model=SettlementResponse,
    tags=["Trading"],
    summary="Settle pending simulated trades",
    description="Check resolved markets and update pending paper trades and bot P&L.",
)
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


@app.get(
    "/api/calibration",
    response_model=CalibrationResponse,
    tags=["Calibration"],
    summary="Get signal calibration",
    description="Compare predicted probabilities with observed outcomes in probability buckets.",
)
async def get_calibration(db: Session = Depends(get_db)):
    """Return calibration data: predicted probability vs actual win rate."""
    signals = db.query(Signal).filter(Signal.outcome_correct.isnot(None)).all()

    if not signals:
        return {"buckets": [], "summary": None}

    from collections import defaultdict
    buckets_data = defaultdict(lambda: {"predicted_sum": 0.0, "correct": 0, "total": 0})

    for s in signals:
        bin_start = int(s.model_probability * 100 // 5) * 5
        bin_end = bin_start + 5
        bucket_key = f"{bin_start}-{bin_end}%"

        buckets_data[bucket_key]["predicted_sum"] += s.model_probability
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


@app.get(
    "/api/kalshi/status",
    response_model=KalshiStatusResponse,
    tags=["Integrations"],
    summary="Check Kalshi connectivity",
    description="Report whether Kalshi credentials are configured and whether an authenticated balance request succeeds.",
)
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


@app.get(
    "/api/weather/forecasts",
    response_model=List[WeatherForecastResponse],
    tags=["Weather"],
    summary="Get configured city forecasts",
    description="Return Open-Meteo GFS ensemble forecasts for configured cities. Temperatures are in degrees Fahrenheit.",
)
async def get_weather_forecasts():
    """Get ensemble forecasts for configured cities."""
    if not settings.WEATHER_ENABLED:
        return []

    try:
        from backend.data.weather import fetch_ensemble_forecast, CITY_CONFIG

        city_keys = settings.weather_city_list
        forecasts = []

        for city_key in city_keys:
            if city_key not in CITY_CONFIG:
                continue
            forecast = await fetch_ensemble_forecast(city_key)
            if forecast:
                forecasts.append(WeatherForecastResponse(
                    city_key=forecast.city_key,
                    city_name=forecast.city_name,
                    target_date=forecast.target_date.isoformat(),
                    mean_high=forecast.mean_high,
                    std_high=forecast.std_high,
                    mean_low=forecast.mean_low,
                    std_low=forecast.std_low,
                    num_members=forecast.num_members,
                    ensemble_agreement=forecast.ensemble_agreement,
                ))

        return forecasts
    except Exception:
        return []


@app.get(
    "/api/weather/markets",
    response_model=List[WeatherMarketResponse],
    tags=["Weather"],
    summary="Get active weather markets",
    description="Return active Polymarket markets and, when enabled and authenticated, Kalshi KXHIGH markets.",
)
async def get_weather_markets():
    """Get active weather temperature markets."""
    if not settings.WEATHER_ENABLED:
        return []

    try:
        from backend.data.weather_markets import fetch_polymarket_weather_markets

        city_keys = settings.weather_city_list
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


@app.get(
    "/api/weather/signals",
    response_model=List[WeatherSignalResponse],
    tags=["Weather"],
    summary="Get current weather signals",
    description="Return model probabilities, market probabilities, edge, confidence, and suggested paper-trade size.",
)
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
        direction=s.direction.value,
        model_probability=s.model_probability,
        market_probability=s.market_probability,
        edge=s.edge,
        confidence=s.confidence,
        suggested_size=s.suggested_size,
        reasoning=s.reasoning,
        ensemble_mean=s.ensemble_mean,
        ensemble_std=s.ensemble_std,
        ensemble_members=s.ensemble_members,
        actionable=s.passes_threshold,
        platform=s.market.platform,
    )


@app.get(
    "/api/events",
    response_model=List[EventResponse],
    tags=["Dashboard"],
    summary="Get recent bot events",
)
async def get_events(limit: int = Query(50, ge=1, le=500, description="Maximum number of events to return.")):
    from backend.core.scheduler import get_recent_events
    events = get_recent_events(limit)
    return [
        EventResponse(
            timestamp=e["timestamp"],
            type=e["type"],
            message=e["message"],
            data=e.get("data", {})
        )
        for e in events
    ]


@app.post(
    "/api/bot/start",
    response_model=ActionResponse,
    tags=["Bot"],
    summary="Start the bot scheduler",
    description="Enable scheduled scanning and mark the bot as running. Trading remains simulation-only.",
)
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


@app.post(
    "/api/bot/stop",
    response_model=ActionResponse,
    tags=["Bot"],
    summary="Stop the bot scheduler",
    description="Pause scheduled scanning without deleting simulated trades or resetting P&L.",
)
async def stop_bot(db: Session = Depends(get_db)):
    from backend.core.scheduler import log_event

    state = db.query(BotState).first()
    if state:
        state.is_running = False
        db.commit()

    log_event("info", "Trading bot paused")
    return {"status": "stopped", "is_running": False}


@app.post(
    "/api/bot/reset",
    response_model=ResetResponse,
    tags=["Bot"],
    summary="Reset simulated bot state",
    description="Delete simulated trades and AI logs, then restore the configured initial bankroll.",
    responses={500: {"description": "The database transaction could not be completed."}},
)
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


@app.get(
    "/api/dashboard",
    response_model=DashboardData,
    tags=["Dashboard"],
    summary="Get the complete dashboard payload",
    description="Return statistics, recent simulated trades, equity, calibration, weather signals, and forecasts in one call.",
    responses={404: {"description": "Bot state has not been initialized."}},
)
async def get_dashboard(db: Session = Depends(get_db)):
    """Get all dashboard data in one call."""
    stats = await get_stats(db)

    trades = db.query(Trade).order_by(Trade.timestamp.desc()).limit(50).all()
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
            pnl=t.pnl
        )
        for t in trades
    ]

    equity_trades = db.query(Trade).filter(Trade.settled == True).order_by(Trade.timestamp).all()
    equity_curve = []
    cumulative_pnl = 0
    for trade in equity_trades:
        if trade.pnl is not None:
            cumulative_pnl += trade.pnl
            equity_curve.append({
                "timestamp": trade.timestamp.isoformat(),
                "pnl": cumulative_pnl,
                "bankroll": settings.INITIAL_BANKROLL + cumulative_pnl
            })

    calibration = _compute_calibration_summary(db)

    weather_signals_data = []
    weather_forecasts_data = []
    if settings.WEATHER_ENABLED:
        try:
            from backend.core.weather_signals import scan_for_weather_signals
            from backend.data.weather import fetch_ensemble_forecast, CITY_CONFIG

            wx_signals = await scan_for_weather_signals()
            weather_signals_data = [_weather_signal_to_response(s) for s in wx_signals]

            city_keys = settings.weather_city_list
            for city_key in city_keys:
                if city_key not in CITY_CONFIG:
                    continue
                forecast = await fetch_ensemble_forecast(city_key)
                if forecast:
                    weather_forecasts_data.append(WeatherForecastResponse(
                        city_key=forecast.city_key,
                        city_name=forecast.city_name,
                        target_date=forecast.target_date.isoformat(),
                        mean_high=forecast.mean_high,
                        std_high=forecast.std_high,
                        mean_low=forecast.mean_low,
                        std_low=forecast.std_low,
                        num_members=forecast.num_members,
                        ensemble_agreement=forecast.ensemble_agreement,
                    ))
        except Exception:
            pass

    return DashboardData(
        stats=stats,
        recent_trades=recent_trades,
        equity_curve=equity_curve,
        calibration=calibration,
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
