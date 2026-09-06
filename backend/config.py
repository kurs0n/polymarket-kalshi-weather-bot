"""Configuration settings for the weather trading bot."""
from typing import Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @field_validator("INITIAL_BANKROLL", "MAX_TRADE_SIZE", "DAILY_LOSS_LIMIT", "WEATHER_MAX_TRADE_SIZE")
    @classmethod
    def validate_positive_amount(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("must be greater than zero")
        return value

    @field_validator("KELLY_FRACTION", "WEATHER_MIN_EDGE_THRESHOLD", "WEATHER_MAX_ENTRY_PRICE")
    @classmethod
    def validate_probability(cls, value: float) -> float:
        if not 0 <= value <= 1:
            raise ValueError("must be between 0 and 1")
        return value

    @field_validator("MAX_TOTAL_PENDING_TRADES", "SETTLEMENT_INTERVAL_SECONDS", "WEATHER_SCAN_INTERVAL_SECONDS")
    @classmethod
    def validate_positive_integer(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("must be greater than zero")
        return value

    @field_validator("DATABASE_URL")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value

    @field_validator("WEATHER_CITIES")
    @classmethod
    def validate_weather_cities(cls, value: str) -> str:
        cities = [city.strip() for city in value.split(",") if city.strip()]
        if not cities:
            raise ValueError("must contain at least one city key")
        return ",".join(dict.fromkeys(cities))

    @property
    def weather_city_list(self) -> list[str]:
        """Return configured weather cities in normalized form."""
        return self.WEATHER_CITIES.split(",")


settings = Settings()
