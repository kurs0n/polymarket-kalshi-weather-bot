"""Shared edge and Kelly position sizing helpers."""
from backend.config import settings
from backend.models.outcomes import Outcome


def calculate_edge(
    model_prob: float,
    market_price: float
) -> tuple[float, Outcome]:
    """
    Calculate edge and determine direction.

    Treats the first outcome as YES and the second as NO.

    Returns:
        (edge, direction) where direction is an Outcome enum
    """
    up_edge = model_prob - market_price
    down_edge = (1 - model_prob) - (1 - market_price)

    if up_edge >= down_edge:
        return up_edge, Outcome.YES
    return down_edge, Outcome.NO


def calculate_kelly_size(
    edge: float,
    probability: float,
    market_price: float,
    direction: Outcome | str,
    bankroll: float
) -> float:
    """
    Calculate position size using fractional Kelly criterion.

    Kelly formula: f = (p * b - q) / b
    """
    if direction == Outcome.YES:
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
    kelly = min(kelly, 0.05)  # 5% max per trade
    kelly = max(kelly, 0)

    size = kelly * bankroll
    size = min(size, settings.MAX_TRADE_SIZE)
    return size
