from fastapi import APIRouter

from backend.config import settings
from backend.api.schemas import WeatherForecastResponse, WeatherMarketResponse, WeatherSignalResponse

router = APIRouter()


@router.get(
    "/api/weather/forecasts",
    response_model=list[WeatherForecastResponse],
    tags=["Weather"],
    summary="Get configured city forecasts",
    description="Return Open-Meteo GFS ensemble forecasts for configured cities. Temperatures are in degrees Fahrenheit.",
)
async def get_weather_forecasts():
    """Get ensemble forecasts for configured cities."""
    if not settings.WEATHER_ENABLED:
        return []

    try:
        from backend.data.weather import CITY_CONFIG, fetch_ensemble_forecast

        city_keys = settings.weather_city_list
        forecasts = []

        for city_key in city_keys:
            if city_key not in CITY_CONFIG:
                continue
            forecast = await fetch_ensemble_forecast(city_key)
            if forecast:
                forecasts.append(
                    WeatherForecastResponse(
                        city_key=forecast.city_key,
                        city_name=forecast.city_name,
                        target_date=forecast.target_date.isoformat(),
                        mean_high=forecast.mean_high,
                        std_high=forecast.std_high,
                        mean_low=forecast.mean_low,
                        std_low=forecast.std_low,
                        num_members=forecast.num_members,
                        ensemble_agreement=forecast.ensemble_agreement,
                    )
                )

        return forecasts
    except Exception:
        return []


@router.get(
    "/api/weather/markets",
    response_model=list[WeatherMarketResponse],
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


@router.get(
    "/api/weather/signals",
    response_model=list[WeatherSignalResponse],
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
