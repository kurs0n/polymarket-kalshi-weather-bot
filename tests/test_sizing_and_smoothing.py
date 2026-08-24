"""
Unit tests for the 2026-08-20 evening fixes:
  1. WEATHER_LIQUIDITY_CAP — an absolute dollar ceiling on Kelly position
     size, added alongside the existing bankroll-percentage ceiling after a
     Monte Carlo simulation showed a pure-% cap doesn't bound size once
     bankroll compounds, while real order-book depth does.
  2. COUNT_PSEUDO_SAMPLES — Laplace smoothing on the empirical ensemble
     count fraction, so a sparse count (e.g. 1 member out of 31) isn't
     trusted as a precise probability the way a moderate count legitimately
     can be. Root-caused from a real "NO"-favorite trade bucket claiming
     ~91% average confidence off raw counts as low as 1/31 while actually
     winning 62.5% of the time.

Run with:
    cd polymarket-kalshi-weather-bot
    python -m pytest tests/test_sizing_and_smoothing.py -v
"""
from datetime import date

import pytest

from backend.config import settings
from backend.core.sizing import calculate_kelly_size
from backend.data.weather import EnsembleForecast, COUNT_PSEUDO_SAMPLES


# ---------------------------------------------------------------------------
# WEATHER_LIQUIDITY_CAP
# ---------------------------------------------------------------------------

class TestLiquidityCap:

    def test_strong_edge_large_bankroll_is_capped_at_liquidity_limit(self):
        """
        A large bankroll + strong edge would push the % cap (5% of bankroll)
        to $2,000+ — the whole point of this fix is that WEATHER_LIQUIDITY_CAP
        (default $200) should win regardless.
        """
        size = calculate_kelly_size(
            edge=0.30, probability=0.55, market_price=0.28,
            direction="up", bankroll=40000.0,
        )
        assert size == pytest.approx(settings.WEATHER_LIQUIDITY_CAP)

    def test_weak_edge_small_size_is_unaffected(self):
        """A weak-edge trade sized well under the liquidity cap shouldn't be touched by it."""
        size = calculate_kelly_size(
            edge=0.03, probability=0.30, market_price=0.27,
            direction="up", bankroll=8000.0,
        )
        assert 0 < size < settings.WEATHER_LIQUIDITY_CAP

    def test_sizing_still_differentiates_by_edge_below_the_cap(self):
        """Regression guard for yesterday's fix: sizes must still scale with edge, not flatline."""
        weak = calculate_kelly_size(edge=0.03, probability=0.30, market_price=0.27, direction="up", bankroll=8000.0)
        strong = calculate_kelly_size(edge=0.15, probability=0.40, market_price=0.25, direction="up", bankroll=8000.0)
        assert strong > weak


# ---------------------------------------------------------------------------
# COUNT_PSEUDO_SAMPLES — sparse-count smoothing
# ---------------------------------------------------------------------------

def _forecast_with_exact_count(k: int, n: int = 31, threshold: float = 90.0) -> EnsembleForecast:
    """
    Build a forecast where EXACTLY k of n members exceed `threshold` —
    full manual control, no randomness, so the resulting probability can be
    checked against the exact expected smoothed-count formula.
    """
    above = [threshold + 1.0] * k
    below = [threshold - 1.0] * (n - k)
    members = above + below
    return EnsembleForecast(
        city_key="test", city_name="Test City", target_date=date.today(),
        member_highs=members, member_lows=[m - 15.0 for m in members],
    )


class TestSparseCountSmoothing:

    def test_sparse_count_is_pulled_away_from_raw_fraction(self):
        """
        1 of 31 members above threshold -> w=1.0 (full ensemble), so the
        result equals the smoothed count exactly regardless of the
        parametric estimate: (1 + PSEUDO/2) / (31 + PSEUDO), not the raw
        1/31 = 3.2% an unsmoothed count would give.
        """
        fc = _forecast_with_exact_count(k=1, n=31, threshold=90.0)
        p = fc.probability_high_above(90.0)
        raw_fraction = 1 / 31
        expected_smoothed = (1 + COUNT_PSEUDO_SAMPLES / 2) / (31 + COUNT_PSEUDO_SAMPLES)
        assert p == pytest.approx(expected_smoothed, abs=1e-9)
        assert p > raw_fraction, "smoothed sparse count must sit above the raw unsmoothed fraction"

    def test_moderate_count_is_barely_affected(self):
        """15 of 31 (near the middle) should barely move — smoothing's whole point
        is proportionally larger correction at the extremes, not a flat shift."""
        fc = _forecast_with_exact_count(k=15, n=31, threshold=90.0)
        p = fc.probability_high_above(90.0)
        raw_fraction = 15 / 31
        assert abs(p - raw_fraction) < abs((1 + COUNT_PSEUDO_SAMPLES / 2) / (31 + COUNT_PSEUDO_SAMPLES) - 1 / 31), \
            "moderate-count correction should be smaller than the sparse-count correction"

    def test_extreme_high_count_is_pulled_below_certainty(self):
        """31 of 31 (every member above) should NOT round to exactly 100% —
        Laplace smoothing keeps even a unanimous count short of certainty."""
        fc = _forecast_with_exact_count(k=31, n=31, threshold=90.0)
        p = fc.probability_high_above(90.0)
        assert p < 1.0
        expected = (31 + COUNT_PSEUDO_SAMPLES / 2) / (31 + COUNT_PSEUDO_SAMPLES)
        assert p == pytest.approx(expected, abs=1e-9)

    def test_complement_still_sums_to_one(self):
        """Regression guard: high_above + high_below must still sum to 1 after smoothing."""
        fc = _forecast_with_exact_count(k=1, n=31, threshold=90.0)
        p_above = fc.probability_high_above(90.0)
        p_below = fc.probability_high_below(90.0)
        assert abs(p_above + p_below - 1.0) < 1e-9

    def test_thin_ensemble_below_five_members_unaffected(self):
        """n<5 skips the count blend entirely (existing behavior) — smoothing must not touch this path."""
        fc = EnsembleForecast(
            city_key="test", city_name="Test City", target_date=date.today(),
            member_highs=[88.0, 89.0, 90.0], member_lows=[73.0, 74.0, 75.0],
        )
        # Should not raise, and should return the pure parametric estimate
        # (no count blend at n=3 < 5).
        p = fc.probability_high_above(90.0)
        assert 0.0 <= p <= 1.0
