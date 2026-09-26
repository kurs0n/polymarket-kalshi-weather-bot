"""Pydantic schemas for the weather trading bot API."""
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class TradeResponse(BaseModel):
    id: int = Field(description="Database identifier for the simulated trade.")
    market_ticker: str = Field(description="Platform-specific market identifier.")
    platform: str = Field(description="Market platform, such as polymarket or kalshi.")
    event_slug: Optional[str] = Field(default=None, description="Optional platform event slug.")
    direction: str = Field(description="Selected market outcome: yes or no.")
    entry_price: float = Field(description="YES or NO contract price at entry, from 0 to 1.")
    size: float = Field(description="Simulated position size in USD.")
    timestamp: datetime = Field(description="UTC time the simulated trade was created.")
    settled: bool = Field(description="Whether settlement has completed.")
    result: str = Field(description="Settlement state: pending, win, or loss.")
    pnl: Optional[float] = Field(default=None, description="Realized profit or loss in USD, if settled.")


class BotStats(BaseModel):
    bankroll: float = Field(description="Current simulated bankroll in USD.")
    total_trades: int = Field(description="Number of simulated trades created.")
    winning_trades: int = Field(description="Number of settled trades with a positive result.")
    win_rate: float = Field(description="Winning trades divided by total trades, from 0 to 1.")
    total_pnl: float = Field(description="Cumulative realized profit or loss in USD.")
    is_running: bool = Field(description="Whether scheduled scanning is enabled.")
    last_run: Optional[datetime] = Field(default=None, description="UTC time of the most recent scan.")


class CalibrationBucket(BaseModel):
    bucket: str = Field(description="Model probability bucket, for example 70-75%.")
    predicted_avg: float = Field(description="Average predicted probability in the bucket.")
    actual_rate: float = Field(description="Observed success rate in the bucket.")
    count: int = Field(description="Number of settled signals in the bucket.")


class CalibrationSummary(BaseModel):
    total_signals: int = Field(description="Total signals recorded.")
    total_with_outcome: int = Field(description="Signals with a known settlement outcome.")
    accuracy: float = Field(description="Fraction of settled signals with the correct outcome.")
    avg_predicted_edge: float = Field(description="Mean absolute model edge at signal creation.")
    avg_actual_edge: float = Field(description="Mean realized signed edge after settlement.")
    brier_score: float = Field(description="Mean squared probability error; lower is better.")


class WeatherForecastResponse(BaseModel):
    city_key: str = Field(description="Configured city key, for example nyc.")
    city_name: str = Field(description="Human-readable city name.")
    target_date: str = Field(description="Forecast target date in ISO-8601 date format.")
    mean_high: float = Field(description="Ensemble mean daily high in degrees Fahrenheit.")
    std_high: float = Field(description="Ensemble standard deviation for the high in degrees Fahrenheit.")
    mean_low: float = Field(description="Ensemble mean daily low in degrees Fahrenheit.")
    std_low: float = Field(description="Ensemble standard deviation for the low in degrees Fahrenheit.")
    num_members: int = Field(description="Number of forecast ensemble members.")
    ensemble_agreement: float = Field(description="Agreement score of the ensemble, from 0 to 1.")


class WeatherMarketResponse(BaseModel):
    slug: str = Field(description="Platform event slug.")
    market_id: str = Field(description="Platform-specific market ticker or condition ID.")
    platform: str = Field(default="polymarket", description="Market platform.")
    title: str = Field(description="Market title shown to users.")
    city_key: str = Field(description="Configured city key.")
    city_name: str = Field(description="Human-readable city name.")
    target_date: str = Field(description="Market settlement date in ISO-8601 date format.")
    threshold_f: float = Field(description="Temperature threshold in degrees Fahrenheit.")
    metric: str = Field(description="Temperature metric, typically high or low.")
    direction: str = Field(description="YES/NO interpretation of the threshold market.")
    yes_price: float = Field(description="Current YES contract price, from 0 to 1.")
    no_price: float = Field(description="Current NO contract price, from 0 to 1.")
    volume: float = Field(description="Market volume in platform-native units.")


class WeatherSignalResponse(BaseModel):
    market_id: str = Field(description="Market identifier used by simulate-trade.")
    city_key: str = Field(description="Configured city key.")
    city_name: str = Field(description="Human-readable city name.")
    target_date: str = Field(description="Market target date in ISO-8601 date format.")
    threshold_f: float = Field(description="Temperature threshold in degrees Fahrenheit.")
    metric: str = Field(description="Temperature metric, typically high or low.")
    direction: str = Field(description="Recommended outcome: yes or no.")
    model_probability: float = Field(description="Forecast-derived probability of the recommended outcome.")
    market_probability: float = Field(description="Current market probability for the recommended outcome.")
    edge: float = Field(description="Model probability minus market probability.")
    confidence: float = Field(description="Ensemble confidence score, from 0 to 1.")
    suggested_size: float = Field(description="Suggested simulated position size in USD.")
    reasoning: str = Field(description="Human-readable explanation of the signal.")
    ensemble_mean: float = Field(description="Ensemble mean temperature in degrees Fahrenheit.")
    ensemble_std: float = Field(description="Ensemble temperature standard deviation in degrees Fahrenheit.")
    ensemble_members: int = Field(description="Number of forecast ensemble members used.")
    actionable: bool = Field(default=False, description="Whether the signal passes the configured edge threshold.")
    platform: str = Field(default="polymarket", description="Market platform.")


class EquityPoint(BaseModel):
    timestamp: str = Field(description="UTC timestamp of the settled trade.")
    pnl: float = Field(description="Cumulative realized P&L in USD at this point.")
    bankroll: float = Field(description="Simulated bankroll in USD at this point.")
    trade_id: Optional[int] = Field(default=None, description="Settled trade identifier, when returned by the equity-curve endpoint.")


class DashboardData(BaseModel):
    stats: BotStats
    recent_trades: List[TradeResponse]
    equity_curve: List[EquityPoint]
    calibration: Optional[CalibrationSummary] = None
    weather_signals: List[WeatherSignalResponse] = Field(default_factory=list)
    weather_forecasts: List[WeatherForecastResponse] = Field(default_factory=list)


class EventResponse(BaseModel):
    timestamp: str = Field(description="UTC event timestamp in ISO-8601 format.")
    type: str = Field(description="Event category used by the dashboard.")
    message: str = Field(description="Human-readable event message.")
    data: Dict[str, Any] = Field(default_factory=dict, description="Optional event-specific data.")


class CalibrationResponse(BaseModel):
    buckets: List[CalibrationBucket]
    summary: Optional[CalibrationSummary] = None


class RootResponse(BaseModel):
    status: str
    message: str
    simulation_mode: bool = Field(description="Whether trades are simulated rather than submitted live.")


class HealthResponse(BaseModel):
    status: str


class ActionResponse(BaseModel):
    status: str
    is_running: bool


class ScanResponse(BaseModel):
    status: str
    weather_signals: int
    weather_actionable: int
    total_signals: int
    actionable_signals: int
    timestamp: str


class SimulatedTradeResponse(BaseModel):
    status: str
    trade_id: int
    size: float


class SettlementResponse(BaseModel):
    status: str
    settled_count: int
    trades: List[Dict[str, Any]]


class ResetResponse(BaseModel):
    status: str
    trades_deleted: int
    ai_logs_deleted: int
    new_bankroll: float


class KalshiStatusResponse(BaseModel):
    connected: bool
    balance: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
