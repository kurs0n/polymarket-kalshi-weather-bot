"""FastAPI backend for weather temperature trading bot dashboard."""
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from typing import List
import asyncio
import os

from backend.config import settings
from backend.models.database import BotState, SessionLocal, init_db
from backend.api.routes import (
    bot_router,
    calibration_router,
    dashboard_router,
    integrations_router,
    system_router,
    trading_router,
    weather_router,
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
                is_running=True,
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


@app.websocket("/ws/events")
async def websocket_events(websocket: WebSocket):
    await ws_manager.connect(websocket)

    try:
        from datetime import datetime

        await websocket.send_json({
            "timestamp": datetime.utcnow().isoformat(),
            "type": "success",
            "message": "Connected to weather trading bot",
        })

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

            await websocket.send_json({
                "type": "heartbeat",
                "timestamp": datetime.utcnow().isoformat(),
            })

    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception:
        ws_manager.disconnect(websocket)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
