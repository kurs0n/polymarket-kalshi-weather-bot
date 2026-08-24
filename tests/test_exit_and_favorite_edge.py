"""
Unit tests for the 2026-08-21 fixes:
  1. Proportional trailing stop (_trailing_stop_giveback in scheduler.py) —
     replaces a flat percentage-point giveback with one that scales with how
     far the position ran, so a huge longshot winner isn't sold for a
     sliver of its value the moment it pulls back slightly, while a modest
     winner still gets a firm minimum trail.
  2. Price-tiered edge threshold (WeatherTradingSignal.passes_threshold) —
     expensive "favorite" entries need a bigger edge cushion than cheap
     longshots, since breakeven win rate at price p is p itself.

Run with:
    cd polymarket-kalshi-weather-bot
    python -m pytest tests/test_exit_and_favorite_edge.py -v
"""
import pytest
from unittest.mock import MagicMock

from backend.config import settings
from backend.core.scheduler import _trailing_stop_giveback


# ---------------------------------------------------------------------------
# _trailing_stop_giveback
# ---------------------------------------------------------------------------

class TestTrailingStopGiveback:

    def test_modest_peak_uses_the_flat_floor(self):
        """A peak just past activation (e.g. 55%) should get the flat floor,
        not a smaller ratio-based amount — ratio*peak < floor here."""
        peak = 0.55
        giveback = _trailing_stop_giveback(peak)
        assert giveback == pytest.approx(settings.WEATHER_TRAILING_STOP_PCT)

    def test_large_peak_scales_with_ratio_not_floor(self):
        """A peak that ran up a lot should give back a multiple of the flat
        floor — proportional to the peak, not clamped to it."""
        peak = 7.25  # the real KXHIGHTBOS-26AUG20-B87.5 peak
        giveback = _trailing_stop_giveback(peak)
        assert giveback == pytest.approx(peak * settings.WEATHER_TRAILING_STOP_RATIO)
        assert giveback > settings.WEATHER_TRAILING_STOP_PCT

    def test_giveback_never_falls_below_the_flat_floor(self):
        """Regression guard: even a peak of exactly 0 must not produce a
        giveback smaller than the floor (division/multiplication edge case)."""
        assert _trailing_stop_giveback(0.0) == pytest.approx(settings.WEATHER_TRAILING_STOP_PCT)

    def test_scaled_giveback_leaves_more_of_a_huge_win_banked_than_a_full_reversal(self):
        """
        Direct regression for the Boston trade: with the safety net on and
        this giveback, a position that peaked at +725% should still sell
        with a large positive realised gain, not ride to zero.
        """
        peak = 7.25
        giveback = _trailing_stop_giveback(peak)
        exit_gain_pct = peak - giveback
        assert exit_gain_pct > 3.0, "should still bank well over 300% gain, not give it all back"

    def test_old_flat_trail_would_have_exited_earlier_on_a_modest_longshot_runup(self):
        """
        Direct regression for KXHIGHCHI-26AUG18-B84.5 (7c entry, peaked +71%,
        sold at +51% under the OLD flat 20pp trail): the new giveback must
        require a deeper retracement before exiting, giving the position more
        real room to run.
        """
        peak = 0.7143
        old_flat_giveback = 0.20
        new_giveback = _trailing_stop_giveback(peak)
        assert new_giveback > old_flat_giveback
        old_exit_gain = peak - old_flat_giveback
        new_exit_gain = peak - new_giveback
        assert new_exit_gain < old_exit_gain, "new trail should require a bigger pullback than the old flat one"


# ---------------------------------------------------------------------------
# Price-tiered edge threshold
# ---------------------------------------------------------------------------

def _make_signal(direction: str, market_probability: float, edge: float):
    """Minimal stand-in for WeatherTradingSignal's threshold-relevant fields,
    avoiding the need to construct a full WeatherMarket."""
    from backend.core.weather_signals import WeatherTradingSignal
    return WeatherTradingSignal(
        market=MagicMock(),
        model_probability=0.5,
        market_probability=market_probability,
        edge=edge,
        direction=direction,
    )


class TestPriceTieredEdgeThreshold:

    def test_cheap_yes_longshot_uses_standard_threshold(self):
        # 5c "yes" entry, edge just above the standard 8% bar
        sig = _make_signal("yes", market_probability=0.05, edge=0.09)
        assert sig.entry_price == pytest.approx(0.05)
        assert sig.passes_threshold

    def test_expensive_no_favorite_needs_bigger_edge(self):
        # 70c "no" entry (market_probability=0.30 => no price = 0.70):
        # edge of 10% clears the standard bar but not the high-price bar.
        sig = _make_signal("no", market_probability=0.30, edge=0.10)
        assert sig.entry_price == pytest.approx(0.70)
        assert not sig.passes_threshold

    def test_expensive_no_favorite_passes_with_enough_edge(self):
        sig = _make_signal("no", market_probability=0.30, edge=0.16)
        assert sig.entry_price == pytest.approx(0.70)
        assert sig.passes_threshold

    def test_threshold_boundary_is_inclusive_of_high_price_cutoff(self):
        # Exactly at WEATHER_HIGH_PRICE_THRESHOLD should use the high-price bar.
        sig = _make_signal("yes", market_probability=settings.WEATHER_HIGH_PRICE_THRESHOLD, edge=0.10)
        assert not sig.passes_threshold  # 10% clears standard 8% but not the 15% high-price bar

    def test_just_below_high_price_cutoff_uses_standard_threshold(self):
        sig = _make_signal("yes", market_probability=settings.WEATHER_HIGH_PRICE_THRESHOLD - 0.01, edge=0.10)
        assert sig.passes_threshold
