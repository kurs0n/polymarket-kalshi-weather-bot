"""FastAPI backend for weather temperature trading bot dashboard."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import List

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from backend.api.routes import (
    bot_router,
    calibration_router,
    dashboard_router,
    integrations_router,
    system_router,
    trading_router,
    weather_router,
)
from backend.config import settings
from backend.models.database import BotState, SessionLocal, init_db


def _initialize_database() -> None:
    """Create tables and make sure a bot state row exists."""
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
                is_running=True,
            )
            db.add(state)
            db.commit()
            print(f"Created new bot state with ${settings.INITIAL_BANKROLL:,.2f} bankroll")
        else:
            state.is_running = True
            db.commit()
            print(
                f"Loaded bot state: Bankroll ${state.bankroll:,.2f}, "
                f"P&L ${state.total_pnl:+,.2f}, {state.total_trades} trades"
            )
    finally:
        db.close()


def _print_startup_banner() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    print("=" * 60)
    print("WEATHER TRADING BOT v4.0")
    print("=" * 60)
    print("Configuration:")
    print(f"  - Simulation mode: {settings.SIMULATION_MODE}")
    print(f"  - Min edge threshold: {settings.WEATHER_MIN_EDGE_THRESHOLD:.0%}")
    print(f"  - Kelly fraction: {settings.KELLY_FRACTION:.0%}")
    print(f"  - Scan interval: {settings.WEATHER_SCAN_INTERVAL_SECONDS}s")
    print(f"  - Settlement interval: {settings.SETTLEMENT_INTERVAL_SECONDS}s")
    print(f"  - Cities: {settings.WEATHER_CITIES}")
    print(f"  - Kalshi enabled: {settings.KALSHI_ENABLED}")
    print(f"  - API key required: {bool(settings.API_KEY)}")
    print("")

    if not settings.API_KEY:
        print("WARNING: API_KEY is not set; mutating endpoints are unauthenticated.")
        print("")
    print("=" * 60)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Initialize the database and scheduler on startup, stop them on shutdown."""
    print("Initializing database...")
    _initialize_database()
    _print_startup_banner()

    from backend.core.scheduler import log_event, start_scheduler, stop_scheduler

    start_scheduler()
    log_event("success", "Weather trading bot initialized")

    print("Bot is now running!")
    print(
        f"  - Weather scan: every {settings.WEATHER_SCAN_INTERVAL_SECONDS}s "
        f"(edge >= {settings.WEATHER_MIN_EDGE_THRESHOLD:.0%})"
    )
    print(f"  - Settlement check: every {settings.SETTLEMENT_INTERVAL_SECONDS}s")
    print("=" * 60)

    try:
        yield
    finally:
        stop_scheduler()


app = FastAPI(
    title="Weather Trading Bot",
    description=(
        "API for a simulation-only weather temperature trading bot. "
        "It combines Polymarket and optional Kalshi market data with "
        "Open-Meteo ensemble forecasts to generate signals, track paper "
        "trades, and measure calibration. No live orders are submitted."
    ),
    version="4.0.0",
    lifespan=lifespan,
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
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(system_router)
app.include_router(dashboard_router)
app.include_router(trading_router)
app.include_router(weather_router)
app.include_router(bot_router)
app.include_router(calibration_router)
app.include_router(integrations_router)


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


@app.websocket("/ws/events")
async def websocket_events(websocket: WebSocket):
    await ws_manager.connect(websocket)

    try:
        from datetime import datetime

        await websocket.send_json(
            {
                "timestamp": datetime.utcnow().isoformat(),
                "type": "success",
                "message": "Connected to weather trading bot",
            }
        )

        from backend.core.scheduler import get_recent_events

        for event in get_recent_events(20):
            await websocket.send_json(event)

        last_event_count = len(get_recent_events(200))
        while True:
            await asyncio.sleep(2)

            current_events = get_recent_events(200)
            if len(current_events) > last_event_count:
                new_events = current_events[last_event_count - len(current_events) :]
                for event in new_events:
                    await websocket.send_json(event)
                last_event_count = len(current_events)

            await websocket.send_json(
                {
                    "type": "heartbeat",
                    "timestamp": datetime.utcnow().isoformat(),
                }
            )

    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception:
        ws_manager.disconnect(websocket)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
