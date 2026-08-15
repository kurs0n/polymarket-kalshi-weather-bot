"""Configuration settings for the weather trading bot."""
import os
from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Database (SQLite for Phase 1, PostgreSQL for production)
    DATABASE_URL: str = "sqlite:///./tradingbot.db"

    # API Keys (optional)
    POLYMARKET_API_KEY: Optional[str] = None

    # Kalshi API
    KALSHI_API_KEY_ID: Optional[str] = None
    KALSHI_PRIVATE_KEY_PATH: Optional[str] = None
    KALSHI_ENABLED: bool = True

    # Bot settings
    SIMULATION_MODE: bool = True
    INITIAL_BANKROLL: float = 10000.0
    KELLY_FRACTION: float = 0.15  # Fractional Kelly
    MAX_TRADE_SIZE: float = 100.0
    DAILY_LOSS_LIMIT: float = 300.0
    MAX_TOTAL_PENDING_TRADES: int = 20

    # Settlement
    SETTLEMENT_INTERVAL_SECONDS: int = 1800  # 30 min

    # Weather trading settings
    WEATHER_ENABLED: bool = True
    WEATHER_SCAN_INTERVAL_SECONDS: int = 300  # 5 min
    WEATHER_MIN_EDGE_THRESHOLD: float = 0.08  # 8%
    WEATHER_MAX_ENTRY_PRICE: float = 0.70
    WEATHER_MAX_TRADE_SIZE: float = 100.0
    WEATHER_CITIES: str = "nyc,chicago,miami,los_angeles,denver,boston"

    # Per-scan trading limits — 2026-08-15: pulled out of scheduler.py so
    # bankroll changes (INITIAL_BANKROLL) don't also require code edits here.
    # WEATHER_MIN_TRADE_SIZE in particular only makes sense relative to the
    # bankroll: it must stay well below Kelly's max-per-trade output
    # (bankroll * KELLY_MAX_TRADE_FRACTION) or it silently overrides Kelly's
    # sizing and every trade comes out identical regardless of confidence
    # (this happened at $30 bankroll with a $10 floor — the floor was above
    # Kelly's entire possible output range).
    WEATHER_MIN_TRADE_SIZE: float = 1.0
    WEATHER_MAX_TRADES_PER_SCAN: int = 3
    WEATHER_MAX_ALLOCATION: float = 500.0

    # Kelly sizing cap — max fraction of bankroll any single trade can use,
    # applied after KELLY_FRACTION. This is the ceiling counterpart to
    # WEATHER_MIN_TRADE_SIZE above: together they define the dollar range
    # confidence can actually size within at the current bankroll.
    KELLY_MAX_TRADE_FRACTION: float = 0.05  # 5%

    class Config:
        env_file = ".env"


settings = Settings()
