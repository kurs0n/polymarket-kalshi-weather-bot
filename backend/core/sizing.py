"""Shared edge and Kelly position sizing helpers."""
from backend.config import settings


def calculate_edge(
    model_prob: float,
    market_price: float
) -> tuple[float, str]:
    """
    Calculate edge and determine direction.

    Treats the first outcome as "up"/"yes" (market_price) and the second as "down"/"no".

    Returns:
        (edge, direction) where direction is "up" or "down"
    """
    up_edge = model_prob - market_price
    down_edge = (1 - model_prob) - (1 - market_price)

    if up_edge >= down_edge:
        return up_edge, "up"
    return down_edge, "down"


def calculate_kelly_size(
    edge: float,
    probability: float,
    market_price: float,
    direction: str,
    bankroll: float
) -> float:
    """
    Calculate position size using fractional Kelly criterion.

    Kelly formula: f = (p * b - q) / b
    """
    if direction == "up":
        win_prob = probability
        price = market_price
    else:
        win_prob = 1 - probability
        price = 1 - market_price

    if price <= 0 or price >= 1:
        return 0

    odds = (1 - price) / price
    lose_prob = 1 - win_prob
    kelly = (win_prob * odds - lose_prob) / odds

    kelly *= settings.KELLY_FRACTION
    kelly = min(kelly, settings.KELLY_MAX_TRADE_FRACTION)  # max % of bankroll per trade
    kelly = max(kelly, 0)

    size = kelly * bankroll

    # Liquidity cap — added 2026-08-20, deliberately different from the
    # MAX_TRADE_SIZE flat-dollar clamp removed from here the same day. That
    # one was stale bankroll-era arithmetic sitting UNDER
    # KELLY_MAX_TRADE_FRACTION and collapsing every trade to the same size
    # regardless of edge. This one is a real, separate constraint that
    # KELLY_MAX_TRADE_FRACTION alone can't express: a % of bankroll grows
    # without bound as bankroll compounds, but actual Kalshi weather-market
    # order-book depth does not — a Monte Carlo simulation that day showed
    # pure-% Kelly sizing compounding to sizes no real thin weather contract
    # could fill. See WEATHER_LIQUIDITY_CAP's own comment in config.py for
    # the depth data behind the $200 figure.
    return min(size, settings.WEATHER_LIQUIDITY_CAP)
