from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.config import settings
from backend.api.schemas import ScanResponse, SettlementResponse, SimulatedTradeResponse
from backend.models.database import BotState, Trade, get_db
from backend.models.outcomes import Outcome

router = APIRouter()


@router.post(
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
        edge_at_entry=signal.edge,
    )

    db.add(trade)
    state.total_trades += 1
    db.commit()

    log_event("trade", f"Manual weather trade: {signal.direction.upper()} {signal.market.city_name}")
    return {"status": "ok", "trade_id": trade.id, "size": trade.size}


@router.post(
    "/api/run-scan",
    response_model=ScanResponse,
    tags=["Trading"],
    summary="Run a weather market scan",
    description="Trigger an immediate scan and return the number of generated and actionable signals.",
)
async def run_scan(db: Session = Depends(get_db)):
    from backend.core.scheduler import log_event, run_manual_scan

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


@router.post(
    "/api/settle-trades",
    response_model=SettlementResponse,
    tags=["Trading"],
    summary="Settle pending simulated trades",
    description="Check resolved markets and update pending paper trades and bot P&L.",
)
async def settle_trades_endpoint(db: Session = Depends(get_db)):
    from backend.core.scheduler import log_event
    from backend.core.settlement import settle_pending_trades, update_bot_state_with_settlements

    log_event("info", "Manual settlement triggered")

    settled = await settle_pending_trades(db)
    await update_bot_state_with_settlements(db, settled)

    return {
        "status": "ok",
        "settled_count": len(settled),
        "trades": [{"id": t.id, "result": t.result, "pnl": t.pnl} for t in settled],
    }
