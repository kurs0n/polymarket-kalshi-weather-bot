from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from backend.config import settings
from backend.api.schemas import (
    BotStats,
    DashboardData,
    EquityPoint,
    EventResponse,
    TradeResponse,
)
from backend.models.database import BotState, Trade, get_db

router = APIRouter()


@router.get(
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
        last_run=state.last_run,
    )


@router.get(
    "/api/trades",
    response_model=List[TradeResponse],
    tags=["Dashboard"],
    summary="List simulated trades",
    description="Return recent simulated trades, optionally filtered by settlement result.",
)
async def get_trades(
    limit: int = Query(50, ge=1, le=500, description="Maximum number of trades to return."),
    status: Optional[str] = Query(None, description="Optional result filter, such as pending, win, or loss."),
    db: Session = Depends(get_db),
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
        )
        for t in trades
    ]


@router.get(
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
                "trade_id": trade.id,
            })

    return curve


@router.get(
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
            data=e.get("data", {}),
        )
        for e in events
    ]


@router.get(
    "/api/dashboard",
    response_model=DashboardData,
    tags=["Dashboard"],
    summary="Get the complete dashboard payload",
    description="Return statistics, recent simulated trades, equity, calibration, weather signals, and forecasts in one call.",
    responses={404: {"description": "Bot state has not been initialized."}},
)
async def get_dashboard(db: Session = Depends(get_db)):
    """Get all dashboard data in one call."""
    from backend.api.routes.calibration import _compute_calibration_summary
    from backend.api.routes.weather import _weather_signal_to_response

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
            pnl=t.pnl,
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
                "bankroll": settings.INITIAL_BANKROLL + cumulative_pnl,
            })

    calibration = _compute_calibration_summary(db)

    weather_signals_data = []
    weather_forecasts_data = []
    if settings.WEATHER_ENABLED:
        try:
            from backend.core.weather_signals import scan_for_weather_signals
            from backend.data.weather import CITY_CONFIG, fetch_ensemble_forecast

            wx_signals = await scan_for_weather_signals()
            weather_signals_data = [_weather_signal_to_response(s) for s in wx_signals]

            city_keys = settings.weather_city_list
            for city_key in city_keys:
                if city_key not in CITY_CONFIG:
                    continue
                forecast = await fetch_ensemble_forecast(city_key)
                if forecast:
                    weather_forecasts_data.append({
                        "city_key": forecast.city_key,
                        "city_name": forecast.city_name,
                        "target_date": forecast.target_date.isoformat(),
                        "mean_high": forecast.mean_high,
                        "std_high": forecast.std_high,
                        "mean_low": forecast.mean_low,
                        "std_low": forecast.std_low,
                        "num_members": forecast.num_members,
                        "ensemble_agreement": forecast.ensemble_agreement,
                    })
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
