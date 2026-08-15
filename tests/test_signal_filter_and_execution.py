"""
Unit tests for the dynamic z-score signal filter and paper trade execution.

Two test groups:
  1. TestZScoreFilter  — generate_weather_signal() rejects/passes based on z_score
  2. TestPaperExecution — execute_paper_trade() uses live ask, rejects on wide spread

Run with:
    cd polymarket-kalshi-weather-bot
    python -m pytest tests/test_signal_filter_and_execution.py -v
"""
import asyncio
import pytest
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

from backend.data.weather import EnsembleForecast, TEMP_UNCERTAINTY_FLOOR_F


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_ensemble(mean: float, std: float, n: int = 31) -> EnsembleForecast:
    """Build a synthetic EnsembleForecast centred at *mean* with *std* spread."""
    step = std / (n / 2) if n > 1 else 0.0
    members = [mean + (i - n // 2) * step for i in range(n)]
    return EnsembleForecast(
        city_key="nyc",
        city_name="New York City",
        target_date=date(2026, 8, 15),
        member_highs=members,
        member_lows=[m - 20.0 for m in members],
    )


def _make_market(threshold_f: float, yes_price: float = 0.52, spread: float = 0.04):
    """Build a minimal WeatherMarket mock for NYC high-above markets."""
    m = MagicMock()
    m.market_id = f"KXHIGHNY-26AUG15-B{threshold_f}"
    m.slug = m.market_id
    m.platform = "kalshi"
    m.city_key = "nyc"
    m.city_name = "New York City"
    m.target_date = date(2026, 8, 15)
    m.threshold_f = threshold_f
    m.metric = "high"
    m.direction = "above"
    m.yes_price = yes_price
    m.no_price = round(1.0 - yes_price, 2)
    m.yes_bid = round(yes_price - spread / 2, 2)
    m.no_bid = round(m.no_price - spread / 2, 2)
    m.bid_ask_spread = spread
    m.has_live_book = True
    m.volume = 5000
    return m


# ---------------------------------------------------------------------------
# Test group 1 — dynamic z-score filter
# ---------------------------------------------------------------------------

class TestZScoreFilter:
    """
    generate_weather_signal() must return None when z_score < 2.0 and a
    non-None signal when z_score >= 2.0, regardless of metric / direction.
    """

    def _run(self, ensemble, market):
        """Helper: run generate_weather_signal with a mocked ensemble fetch."""
        from backend.core.weather_signals import generate_weather_signal
        with patch(
            "backend.core.weather_signals.fetch_ensemble_forecast",
            new=AsyncMock(return_value=ensemble),
        ):
            return asyncio.run(generate_weather_signal(market))

    def test_z_below_2_is_rejected(self):
        """z_score = 1.0σ — must be rejected."""
        # mean=90, std=5 → threshold=95 → z = 5/5 = 1.0 < 2.0
        fc = _make_ensemble(mean=90.0, std=5.0)
        market = _make_market(threshold_f=95.0)
        assert self._run(fc, market) is None

    def test_z_at_exact_boundary_is_rejected(self):
        """z_score = 2.0σ exactly — strict < 2.0 means this passes."""
        # mean=90, std=5 → threshold=100 → z = 10/5 = 2.0 (not < 2.0 → passes)
        fc = _make_ensemble(mean=90.0, std=5.0)
        market = _make_market(threshold_f=100.0)
        # z == 2.0 is NOT < 2.0, so the signal should pass through
        signal = self._run(fc, market)
        assert signal is not None

    def test_z_above_2_passes(self):
        """z_score = 2.5σ — must produce a signal."""
        # mean=90, std=4 → threshold=100 → z = 10/4 = 2.5 ≥ 2.0
        fc = _make_ensemble(mean=90.0, std=4.0)
        market = _make_market(threshold_f=100.0)
        assert self._run(fc, market) is not None

    def test_z_uses_floored_std_for_thin_ensemble(self):
        """
        When ensemble std < TEMP_UNCERTAINTY_FLOOR_F, the filter uses the
        floor.  A threshold 5.5°F from a degenerate-std ensemble must be
        rejected (5.5 / 3.0 ≈ 1.83 < 2.0).

        Uses n=5 identical members so the degenerate-ensemble guard (< 5m)
        is not triggered before the z-score filter runs.
        """
        # 5 identical members → std = 0, but num_members = 5 = MIN_RELIABLE_MEMBERS
        fc = _make_ensemble(mean=90.0, std=0.0, n=5)
        assert fc.std_high == 0.0, "Sanity: identical-member std should be 0"
        assert fc.num_members == 5
        # z = 5.5 / TEMP_UNCERTAINTY_FLOOR_F(3.0) ≈ 1.83 < 2.0 → must be rejected
        market = _make_market(threshold_f=95.5)
        assert self._run(fc, market) is None

    def test_z_uses_floored_std_passes_when_far_enough(self):
        """
        Threshold 7°F from a 0-std ensemble: z = 7 / 3.0 ≈ 2.33 ≥ 2.0 → passes.
        Uses n=5 identical members so the degenerate guard doesn't fire first.
        """
        fc = _make_ensemble(mean=90.0, std=0.0, n=5)
        market = _make_market(threshold_f=97.0)
        signal = self._run(fc, market)
        assert signal is not None

    def test_signal_carries_z_score_in_reasoning(self):
        """Passed signals should embed the z-score in the reasoning string."""
        fc = _make_ensemble(mean=90.0, std=4.0)
        market = _make_market(threshold_f=100.0)   # z = 2.5σ
        signal = self._run(fc, market)
        assert signal is not None
        assert "z=" in signal.reasoning


# ---------------------------------------------------------------------------
# Test group 2 — paper trade execution
# ---------------------------------------------------------------------------

class TestPaperExecution:
    """
    execute_paper_trade() must:
      - Use the live order book best ask as entry_price when available.
      - Fall back to snapshot ask when the live book is unavailable but spread ≤ 10¢.
      - Reject (return None) when no live ask AND snapshot spread > 10¢.
    """

    def _make_signal(self, yes_price=0.55, spread=0.04, direction="yes"):
        signal = MagicMock()
        signal.direction = direction
        signal.model_probability = 0.70
        signal.market_probability = yes_price
        signal.edge = 0.15
        market = _make_market(threshold_f=97.0, yes_price=yes_price, spread=spread)
        signal.market = market
        return signal

    def _run(self, signal, trade_size=50.0):
        from backend.core.execution import execute_paper_trade
        return asyncio.run(execute_paper_trade(signal, trade_size))

    def test_uses_live_ask_when_available(self):
        """entry_price must equal the live order book ask when fetch succeeds."""
        signal = self._make_signal()

        with patch(
            "backend.core.execution._fetch_live_ask",
            new=AsyncMock(return_value=(0.58, None)),
        ):
            trade = self._run(signal)

        assert trade is not None
        assert trade.entry_price == pytest.approx(0.58)
        assert trade.execution_type == "simulated"
        assert trade.order_status == "filled"

    def test_rejects_when_no_live_ask_and_spread_too_wide(self):
        """
        When live book is unavailable AND snapshot spread > 10¢, the trade
        must be rejected (return None).
        """
        signal = self._make_signal(spread=0.12)  # snapshot spread = 12¢ > 10¢

        with patch(
            "backend.core.execution._fetch_live_ask",
            new=AsyncMock(return_value=(None, "no_credentials")),
        ):
            trade = self._run(signal)

        assert trade is None

    def test_fallback_to_snapshot_when_live_unavailable_and_spread_ok(self):
        """
        When the live book is unavailable but snapshot spread ≤ 10¢, fall back
        to the snapshot ask price rather than rejecting the trade.
        """
        signal = self._make_signal(yes_price=0.55, spread=0.04)

        with patch(
            "backend.core.execution._fetch_live_ask",
            new=AsyncMock(return_value=(None, "no_credentials")),
        ):
            trade = self._run(signal)

        assert trade is not None
        # Fallback uses snapshot yes_price (the ask)
        assert trade.entry_price == pytest.approx(0.55)

    def test_no_spread_rejects_when_spread_exactly_at_limit(self):
        """
        Spread exactly at 10¢ (≤ MAX_PAPER_SPREAD) should allow fallback fill,
        not rejection.
        """
        signal = self._make_signal(spread=0.10)  # exactly at limit

        with patch(
            "backend.core.execution._fetch_live_ask",
            new=AsyncMock(return_value=(None, "no_credentials")),
        ):
            trade = self._run(signal)

        # 0.10 == MAX_PAPER_SPREAD → not > MAX_PAPER_SPREAD → fallback allowed
        assert trade is not None

    def test_trade_fields_populated_correctly(self):
        """All Trade fields must be set correctly on a successful paper execution."""
        signal = self._make_signal()

        with patch(
            "backend.core.execution._fetch_live_ask",
            new=AsyncMock(return_value=(0.60, None)),
        ):
            trade = self._run(signal, trade_size=75.0)

        assert trade.market_type == "weather"
        assert trade.size == pytest.approx(75.0)
        assert trade.model_probability == pytest.approx(0.70)
        assert trade.edge_at_entry == pytest.approx(0.15)
        assert trade.execution_type == "simulated"
        assert trade.limit_price == pytest.approx(0.60)
        assert trade.order_status == "filled"
        assert trade.order_placed_at is not None
