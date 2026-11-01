from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.api.deps import require_api_key
from backend.api.schemas import ActionResponse, ResetResponse
from backend.config import settings
from backend.models.database import BotState, Trade, get_db

router = APIRouter()


@router.post(
    "/api/bot/start",
    dependencies=[Depends(require_api_key)],
    response_model=ActionResponse,
    tags=["Bot"],
    summary="Start the bot scheduler",
    description="Enable scheduled scanning and mark the bot as running. Trading remains simulation-only.",
)
async def start_bot(db: Session = Depends(get_db)):
    from backend.core.scheduler import is_scheduler_running, log_event, start_scheduler

    state = db.query(BotState).first()
    if state:
        state.is_running = True
        db.commit()

    if not is_scheduler_running():
        start_scheduler()

    log_event("success", "Weather trading bot started")
    return {"status": "started", "is_running": True}


@router.post(
    "/api/bot/stop",
    dependencies=[Depends(require_api_key)],
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


@router.post(
    "/api/bot/reset",
    dependencies=[Depends(require_api_key)],
    response_model=ResetResponse,
    tags=["Bot"],
    summary="Reset simulated bot state",
    description="Delete simulated trades, then restore the configured initial bankroll.",
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

        db.commit()

        log_event(
            "success", f"Bot reset: {trades_deleted} trades deleted. Fresh start with ${settings.INITIAL_BANKROLL:,.2f}"
        )

        return {
            "status": "reset",
            "trades_deleted": trades_deleted,
            "new_bankroll": settings.INITIAL_BANKROLL,
        }

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Reset failed: {e}") from e
