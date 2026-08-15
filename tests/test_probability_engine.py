"""
Unit tests for the ensemble probability engine.

Verifies that EnsembleForecast produces physically sensible probabilities
under all conditions: full 31-member ensemble, thin ensemble, and the
degenerate single-member case that caused the original Brier-0.90 bug.

Run with:
    cd polymarket-kalshi-weather-bot
    python -m pytest tests/test_probability_engine.py -v
"""
import pytest
from datetime import date
from backend.data.weather import EnsembleForecast, TEMP_UNCERTAINTY_FLOOR_F


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_forecast(mean: float, std: float, n: int = 31) -> EnsembleForecast:
    """
    Build a synthetic EnsembleForecast with `n` members drawn from a
    normal distribution with the given mean and std.
    For n=1, std collapses to 0 (the degenerate case).
    """
    import random
    random.seed(42)
    if n == 1:
        members = [mean]
    else:
        members = [random.gauss(mean, std) for _ in range(n)]

    return EnsembleForecast(
        city_key="test",
        city_name="Test City",
        target_date=date.today(),
        member_highs=members,
        member_lows=[m - 15.0 for m in members],
    )


# ---------------------------------------------------------------------------
# Test group 1: full ensemble (31 members, mean=90°F, std=3°F)
# ---------------------------------------------------------------------------

class TestFullEnsemble:
    """31-member ensemble with mean=90°F ± 3°F."""

    @pytest.fixture(autouse=True)
    def forecast(self):
        self.fc = _make_forecast(mean=90.0, std=3.0, n=31)

    def test_probability_at_mean_is_near_fifty_percent(self):
        p = self.fc.probability_high_above(90.0)
        assert 0.35 <= p <= 0.65, f"P(high>mean) should be ~50%, got {p:.1%}"

    def test_probability_one_std_above_mean_is_below_half(self):
        # threshold = mean + 1*std = 93°F; should be roughly 16–30%
        p = self.fc.probability_high_above(93.0)
        assert p < 0.50, f"P(high>mean+1σ) should be <50%, got {p:.1%}"
        assert p > 0.05, f"P(high>mean+1σ) should be >5%, got {p:.1%}"

    def test_probability_two_std_above_mean_is_small(self):
        # threshold = 96°F; Gaussian tail ~2.3%
        p = self.fc.probability_high_above(96.0)
        assert p < 0.15, f"P(high>mean+2σ) should be <15%, got {p:.1%}"

    def test_far_tail_97F_against_94F_mean(self):
        """Reproduces the reported bug: 94°F mean, 97°F threshold."""
        fc = _make_forecast(mean=94.0, std=3.0, n=31)
        p = fc.probability_high_above(97.0)
        # Should be roughly 16% (1 std above), definitely NOT 95%
        assert p < 0.35, f"P(NYC high>97F | mean=94F) should be <35%, got {p:.1%}"
        assert p > 0.02, f"P(NYC high>97F | mean=94F) should be >2%, got {p:.1%}"

    def test_far_tail_boston_85F(self):
        """Boston 85.5°F threshold when mean is lower."""
        fc = _make_forecast(mean=80.0, std=3.0, n=31)
        p = fc.probability_high_above(85.5)
        # 5.5°F above mean, ~1.8 std → Gaussian ~3.6%
        assert p < 0.20, f"P(Boston high>85.5F | mean=80F) should be <20%, got {p:.1%}"

    def test_probability_high_below_complements_above(self):
        for threshold in [85.0, 90.0, 95.0]:
            p_above = self.fc.probability_high_above(threshold)
            p_below = self.fc.probability_high_below(threshold)
            assert abs(p_above + p_below - 1.0) < 1e-9, \
                f"above+below must sum to 1 at {threshold}F"

    def test_probability_monotone_decreasing_with_threshold(self):
        thresholds = [80.0, 85.0, 90.0, 95.0, 100.0]
        probs = [self.fc.probability_high_above(t) for t in thresholds]
        for i in range(len(probs) - 1):
            assert probs[i] >= probs[i + 1], \
                f"P should decrease as threshold rises: {probs}"

    def test_no_clipping_to_extreme_values(self):
        # Even at the mean, should never be exactly 0.05 or 0.95
        # (those were hallmarks of the broken single-member path)
        p = self.fc.probability_high_above(90.0)
        assert p not in (0.05, 0.95), "Probability looks clipped — check member count"


# ---------------------------------------------------------------------------
# Test group 2: degenerate single-member ensemble (the original bug)
# ---------------------------------------------------------------------------

class TestSingleMemberEnsemble:
    """
    Simulates what happened when gfs_seamless returned only 1 member.
    The Gaussian floor should prevent the 0%/100% collapse.
    """

    @pytest.fixture(autouse=True)
    def forecast(self):
        # Single member = 94°F. Old code: P(>97)=0→clip 5%, P(<91)=100→clip 95%.
        self.fc = _make_forecast(mean=94.0, std=0.0, n=1)
        # Confirm std collapsed as expected
        assert self.fc.std_high == 0.0
        assert self.fc.num_members == 1

    def test_effective_std_uses_floor(self):
        eff_std = self.fc._eff_std_high()
        assert eff_std == TEMP_UNCERTAINTY_FLOOR_F, \
            f"Single-member std should use floor {TEMP_UNCERTAINTY_FLOOR_F}°F"

    def test_probability_at_mean_is_near_fifty(self):
        # mean=94°F, threshold=94°F → should be ~50% via Gaussian
        p = self.fc.probability_high_above(94.0)
        assert 0.35 <= p <= 0.65, f"P(high>94F | single member 94F) = {p:.1%}, expected ~50%"

    def test_probability_above_threshold_not_clipped_to_5pct(self):
        # The old bug: threshold=97°F, single member=94°F → P=0/1=0 → clip 5%
        p = self.fc.probability_high_above(97.0)
        assert p != 0.05, "Probability is the old hard-clip 5% — fix not applied"
        # With floor=3°F, Gaussian gives 1-norm.cdf(97,94,3) ≈ 15.9%
        assert 0.05 < p < 0.40, f"Expected ~16%, got {p:.1%}"

    def test_probability_does_not_collapse_to_zero_or_one(self):
        for threshold in [88.0, 91.0, 94.0, 97.0, 100.0]:
            p = self.fc.probability_high_above(threshold)
            assert 0.01 < p < 0.99, \
                f"P(high>{threshold}F) = {p:.3f} — collapsed to 0/1 for single member"

    def test_no_fake_45pct_edge_against_50ct_market(self):
        """
        Reproduces the symptom: model=95%, market=50% → edge=+45%.
        After the fix, model should be nowhere near 95% for a 1-sigma miss.
        """
        p = self.fc.probability_high_above(97.0)  # 3°F above single member
        fake_edge = abs(p - 0.50)
        assert fake_edge < 0.40, \
            f"Edge of {fake_edge:.1%} still looks fabricated (model={p:.1%})"


# ---------------------------------------------------------------------------
# Test group 3: thin ensemble (3 members)
# ---------------------------------------------------------------------------

class TestThinEnsemble:
    """3-member ensemble — should rely mostly on Gaussian."""

    @pytest.fixture(autouse=True)
    def forecast(self):
        self.fc = _make_forecast(mean=75.0, std=4.0, n=3)

    def test_probabilities_are_sensible(self):
        p = self.fc.probability_high_above(75.0)
        assert 0.30 <= p <= 0.70

    def test_far_tail_is_small(self):
        # 83°F = mean + 2*std → should be < 10%
        p = self.fc.probability_high_above(83.0)
        assert p < 0.25, f"2-sigma tail with thin ensemble: {p:.1%}"


# ---------------------------------------------------------------------------
# Test group 4: low temperature markets
# ---------------------------------------------------------------------------

class TestLowTemperatureMarkets:

    @pytest.fixture(autouse=True)
    def forecast(self):
        # mean_low = 65°F, std = 2.5°F
        self.fc = EnsembleForecast(
            city_key="miami",
            city_name="Miami",
            target_date=date.today(),
            member_highs=[85.0 + i * 0.2 for i in range(31)],
            member_lows=[65.0 + (i - 15) * 0.3 for i in range(31)],
        )

    def test_low_probability_above_at_mean_is_near_fifty(self):
        p = self.fc.probability_low_above(self.fc.mean_low)
        assert 0.35 <= p <= 0.65

    def test_low_probability_complements(self):
        for t in [60.0, 65.0, 70.0]:
            assert abs(
                self.fc.probability_low_above(t) + self.fc.probability_low_below(t) - 1.0
            ) < 1e-9


# ---------------------------------------------------------------------------
# Test group 5: sanity regression for the reported examples
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mean,std,threshold,expected_max,label", [
    (94.0, 3.0, 97.0, 0.35, "NYC 97°F vs 94°F mean"),
    (80.0, 3.0, 85.5, 0.20, "Boston 85.5°F vs 80°F mean"),
    (90.0, 3.0, 97.0, 0.05, "Generic 3-sigma tail"),
    (90.0, 3.0, 84.0, 0.05, "Generic 2-sigma below (low prob above)"),
])
def test_reported_extreme_cases(mean, std, threshold, expected_max, label):
    fc = _make_forecast(mean=mean, std=std, n=31)
    p = fc.probability_high_above(threshold)
    # For below-mean thresholds, probability_high_above should be HIGH.
    # We only check the high-threshold cases for "expected_max":
    if threshold > mean:
        assert p <= expected_max, (
            f"[{label}] P(high>{threshold:.1f}F | mean={mean}F) = {p:.1%}, "
            f"expected <{expected_max:.0%}"
        )
