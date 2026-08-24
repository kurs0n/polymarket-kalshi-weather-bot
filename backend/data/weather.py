"""Weather data fetcher using Open-Meteo Ensemble API and NWS observations."""
import asyncio
import httpx
import logging
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional
import statistics
import time
from zoneinfo import ZoneInfo

from scipy.stats import t as _t

logger = logging.getLogger("trading_bot")

# Minimum temperature uncertainty floor in °F.
# Even a perfect 31-member ensemble has ~2-4°F short-range forecast error.
# This prevents probability collapse when std → 0 (degenerate or single-member fetch).
TEMP_UNCERTAINTY_FLOOR_F = 3.0

# Tail distribution degrees-of-freedom — added 2026-08-17 alongside the
# model-disagreement std widening below, both aimed at the same root cause:
# calibration.py found the bot's own 95-100% model-win-probability bucket
# (n=963 settled trades) only actually won 77.7% of the time. That's the
# signature of a tail distribution that's too thin, and it has two
# independent causes: (a) the CDF's std was blind to cross-model
# disagreement (see _model_disagreement_std_high), and (b) a plain Gaussian
# tail decays faster than real forecast-error tails do — meteorological
# forecast busts cluster more than a normal distribution predicts. This
# swaps the Gaussian CDF for a Student-t with a fixed, deliberately
# conservative df=6 (fat enough to matter at the z>=2 tails this bot
# actually trades, without needing a fitted-from-history df yet). scale is
# rescaled (see _t_scale) so the distribution's VARIANCE still equals
# eff_std**2 — i.e. this changes tail shape, not the central spread
# TEMP_UNCERTAINTY_FLOOR_F and the model-disagreement widening already
# calibrate. Once enough settled history accumulates in ModelForecastLog,
# a natural follow-up is fitting df empirically from standardized residuals
# instead of holding it fixed — same "thin sample keeps the default"
# posture as get_model_weights()'s MIN_MODEL_SAMPLES gate.
TAIL_DIST_DF = 6.0


def _t_scale(std: float) -> float:
    """
    Student-t `scale` parameter that reproduces a target standard deviation.

    A Student-t(df, loc, scale) has variance = scale**2 * df/(df-2) (df>2),
    not scale**2 like a Gaussian — so scale must be shrunk by
    sqrt((df-2)/df) for the resulting distribution's actual std to equal
    `std`. Without this, swapping in a Student-t with scale=std would
    silently widen the central spread too, not just the tails.
    """
    return std * ((TAIL_DIST_DF - 2.0) / TAIL_DIST_DF) ** 0.5


# Laplace/additive smoothing applied to the empirical ensemble-count
# fraction before it's blended with the parametric CDF (see
# probability_high_above / probability_low_above below).
#
# Root-caused 2026-08-20: the count-blend weight w = min((n/31)**0.5, 1.0)
# is exactly 1.0 whenever the full 31-member ensemble is available — the
# normal case — which discards the parametric estimate ENTIRELY regardless
# of how sparse the specific count is. A market where only 1 of 31 members
# landed above some threshold got reported as a precise 3.2% probability
# (96.8% confidence on the other side), when 1-out-of-31 is genuinely
# high-uncertainty evidence — its own 95% CI is roughly [0.4%, 14%]. Traced
# a whole bucket of live "NO"-favorite trades (all built on raw counts of
# 1-6 out of 31, all at w=1.0) claiming ~91% average confidence while
# actually winning 62.5% of the time (n=8 — not conclusive on its own, but
# consistent with this mechanism, not with normal variance).
#
# Laplace smoothing pulls sparse/extreme counts toward the parametric
# estimate proportionally more than moderate ones (adding a fixed amount to
# numerator and denominator changes a count near 0 or n far more, in
# relative terms, than one near the middle) — exactly the correction this
# needs, without touching the n>=5 gate or the w formula above it. 2
# pseudo-samples (1 "prior" hit, 1 "prior" miss) is a deliberately modest
# choice — not tuned to force-match the observed 62.5%, which would be
# curve-fitting a correction to n=8 of noise. Revisit the constant once
# this bucket has enough settled trades to actually calibrate against.
COUNT_PSEUDO_SAMPLES = 2


# HRRR blending parameters
HRRR_WEIGHT          = 0.60   # weight given to HRRR during solar window
GFS_WEIGHT           = 0.40   # weight given to GFS mean during solar window
SOLAR_WINDOW_START   = 12     # 12:00 local time
SOLAR_WINDOW_END     = 17     # 17:59 local time (inclusive)
HRRR_LOOKAHEAD_HOURS = 18     # fetch HRRR only when target is within this many hours

# Minimum independent model families (GFS/HRRR blend, ECMWF, NWS) required
# before cross-model disagreement is trusted as a real uncertainty signal —
# see _model_disagreement_std_high. Below this (e.g. before ECMWF/NWS have
# been fetched yet today), disagreement is reported as 0.0 — a no-op, not a
# guess — same cold-start-safe posture as get_model_weights().
MODEL_DISAGREEMENT_MIN_COMPONENTS = 2

_hrrr_cache: Dict[str, tuple] = {}
_HRRR_CACHE_TTL = 300  # 5 minutes

# City configurations with lat/lon and NWS station identifiers.
#
# All coordinates and station IDs are verified against Kalshi contract
# rules_primary (fetched from /markets?series_ticker=KXHIGH*) and the
# NWS stations API (api.weather.gov/stations/{id}).
#
# Kalshi settlement sources (from rules_primary, verified 2026-08-10):
#   NYC  → "Central Park, New York"        → KNYC  (40.7833, -73.9667)
#   CHI  → "Chicago Midway, IL"            → KMDW  (41.7842, -87.7553)
#   MIA  → "Miami International Airport"   → KMIA  (25.7906, -80.3164)
#   LAX  → "Los Angeles Airport, CA"       → KLAX  (33.9381, -118.3889)
#   DEN  → "Denver, CO"                    → KDEN  (39.8466, -104.6562)
#   BOS  → "Boston (Logan Airport), MA"    → KBOS  (42.3606, -71.0106)
CITY_CONFIG: Dict[str, dict] = {
    "nyc": {
        "name": "New York City",
        "lat": 40.7833,   # KNYC Central Park — was 40.7128 (lower Manhattan, 8.5 km off)
        "lon": -73.9667,  # was -74.0060 (Hudson River basin, GFS water-cell cold bias)
        "nws_station": "KNYC",
        "nws_office": "OKX",
        "nws_gridpoint": "OKX/34,45",  # was OKX/33,37
        "station_bias_f": 0.0,
        "timezone": "America/New_York",
    },
    "chicago": {
        "name": "Chicago",
        "lat": 41.7842,   # KMDW Midway — was 41.8781 (O'Hare area, 14.7 km off)
        "lon": -87.7553,  # was -87.6298
        "nws_station": "KMDW",  # was KORD (O'Hare); Kalshi settles at Midway
        "nws_office": "LOT",
        "nws_gridpoint": "LOT/72,69",  # was LOT/75,72
        "station_bias_f": 0.0,
        "timezone": "America/Chicago",
    },
    "miami": {
        "name": "Miami",
        "lat": 25.7906,   # KMIA airport — was 25.7617 (downtown Brickell, 12.9 km off)
        "lon": -80.3164,  # was -80.1918
        "nws_station": "KMIA",
        "nws_office": "MFL",
        "nws_gridpoint": "MFL/105,51",  # was MFL/75,53
        "station_bias_f": 0.0,
        "timezone": "America/New_York",
    },
    "los_angeles": {
        "name": "Los Angeles",
        "lat": 33.9381,    # KLAX airport — was 34.0522 (downtown LA, 18.4 km off)
        "lon": -118.3889,  # was -118.2437
        "nws_station": "KLAX",
        "nws_office": "LOX",
        "nws_gridpoint": "LOX/149,41",  # was LOX/154,44
        "station_bias_f": 0.0,
        "timezone": "America/Los_Angeles",
    },
    "denver": {
        "name": "Denver",
        "lat": 39.8466,    # KDEN Denver Intl — was 39.7392 (downtown Denver, 30.9 km off)
        "lon": -104.6562,  # was -104.9903
        "nws_station": "KDEN",
        "nws_office": "BOU",
        "nws_gridpoint": "BOU/75,66",  # was BOU/62,60
        "station_bias_f": 0.0,
        "timezone": "America/Denver",
    },
    "boston": {
        "name": "Boston",
        "lat": 42.3606,    # KBOS Logan — was 42.3656 (0.6 km, effectively correct)
        "lon": -71.0106,   # was -71.0096
        "nws_station": "KBOS",
        "nws_office": "BOX",
        "nws_gridpoint": "BOX/73,101",  # was BOX/71,101
        "station_bias_f": 0.0,
        "timezone": "America/New_York",
    },
}


@dataclass
class EnsembleForecast:
    """Ensemble weather forecast with per-member data."""
    city_key: str
    city_name: str
    target_date: date
    member_highs: List[float]  # Daily max temps (°F) per ensemble member
    member_lows: List[float]   # Daily min temps (°F) per ensemble member
    mean_high: float = 0.0
    std_high: float = 0.0
    mean_low: float = 0.0
    std_low: float = 0.0
    num_members: int = 0
    fetched_at: datetime = field(default_factory=datetime.utcnow)
    hrrr_high: Optional[float] = None  # HRRR deterministic daily-max, if fetched
    ecmwf_high: Optional[float] = None  # ECMWF ensemble mean daily-max, if fetched
    nws_high: Optional[float] = None    # NWS human forecast daily-max, if fetched
    bias_correction_f: float = 0.0      # recent (actual - predicted) rolling average
    model_weights: Optional[Dict[str, float]] = None  # rolling-accuracy weights, see get_model_weights()

    def __post_init__(self):
        if self.member_highs:
            self.mean_high = statistics.mean(self.member_highs)
            self.std_high = statistics.stdev(self.member_highs) if len(self.member_highs) > 1 else 0.0
            self.num_members = len(self.member_highs)
        if self.member_lows:
            self.mean_low = statistics.mean(self.member_lows)
            self.std_low = statistics.stdev(self.member_lows) if len(self.member_lows) > 1 else 0.0

    # ------------------------------------------------------------------
    # Effective standard deviations with uncertainty floor applied.
    # When the ensemble collapses to 1 member (or members are identical),
    # std = 0. Without the floor, the tail CDF degenerates to a step
    # function, reproducing the original 0%/100% collapse.
    # ------------------------------------------------------------------

    def _model_components_high(self) -> Dict[str, float]:
        """
        Per-model-family daily-high point estimates: GFS (optionally
        HRRR-blended during the solar window), plus ECMWF/NWS when fetched.

        Shared by effective_mean_high() (the blended point estimate) and
        _model_disagreement_std_high() (how much the families disagree)
        so the two never drift out of sync — factored out 2026-08-17
        alongside the disagreement-std addition below.
        """
        base = self.mean_high
        if self.hrrr_high is not None:
            city = CITY_CONFIG.get(self.city_key, {})
            tz_str = city.get("timezone", "UTC")
            local_hour = datetime.now(ZoneInfo(tz_str)).hour
            if SOLAR_WINDOW_START <= local_hour <= SOLAR_WINDOW_END:
                base = HRRR_WEIGHT * self.hrrr_high + GFS_WEIGHT * self.mean_high

        # "gfs" bucket = the (possibly HRRR-blended) GFS-family number above;
        # HRRR doesn't get its own rolling-accuracy weight — it's only ever
        # fetched within an 18h lookahead window, too short a life to build
        # its own 14-day track record independent of the GFS blend it feeds.
        components: Dict[str, float] = {"gfs": base}
        if self.ecmwf_high is not None:
            components["ecmwf"] = self.ecmwf_high
        if self.nws_high is not None:
            components["nws"] = self.nws_high
        return components

    def effective_mean_high(self) -> float:
        """
        GFS mean, blended toward HRRR at 60% during the afternoon solar
        window, then combined against ECMWF/NWS when available (independent
        second opinions — added 2026-08-17 after finding GFS alone had run
        3-4°F warm for NY/Boston two days running with nothing to catch it),
        then shifted by the recent per-city bias correction.

        Combining step: weighted by self.model_weights when a rolling
        accuracy weighting is available (see get_model_weights() — a
        14-day-lookback inverse-MAE weight per model family, computed
        against this city's own settled outcomes), falling back to a plain
        equal-weight average otherwise. model_weights is None for any city
        without enough settled history yet, which reproduces the exact
        equal-weight formula this replaced — cold start is a no-op, not a
        regression.
        """
        components = self._model_components_high()

        if self.model_weights:
            weighted_sum = sum(self.model_weights.get(name, 0.0) * val for name, val in components.items())
            weight_total = sum(self.model_weights.get(name, 0.0) for name in components)
            base = weighted_sum / weight_total if weight_total > 0 else statistics.mean(components.values())
        else:
            base = statistics.mean(components.values())

        return base + self.bias_correction_f

    def _model_disagreement_std_high(self) -> float:
        """
        How much the independent model families (GFS/HRRR blend, ECMWF,
        NWS) disagree with each other on today's daily-high, as a
        model_weights-weighted standard deviation around their blended
        mean (same weights effective_mean_high() uses for the mean itself).

        Root-caused 2026-08-17: calibration.py found the bot's own
        95-100% model-win-probability bucket (n=963 settled trades) only
        actually won 77.7% of the time. effective_mean_high() already
        blends GFS/HRRR/ECMWF/NWS into the point estimate, but the CDF's
        std (_eff_std_high) was still built purely from self.std_high —
        the GFS ensemble's OWN internal spread — which is blind to
        genuine disagreement BETWEEN model families. GFS running 3-4°F
        warm while ECMWF/NWS don't agree is real uncertainty about the
        true high, not GFS internal noise, and it never widened the tail
        probability at all: the mean moved, the std didn't. This folds
        that disagreement into _eff_std_high() so the tail distribution
        itself reflects cross-model disagreement instead of relying
        entirely on calibration.py's post-hoc empirical shrinkage to
        paper over it.

        Returns 0.0 (no-op) with fewer than MODEL_DISAGREEMENT_MIN_
        COMPONENTS model families available — e.g. before ECMWF/NWS have
        been fetched yet today. Same cold-start-safe posture as
        get_model_weights()/get_recent_bias: no data, no correction.
        """
        components = self._model_components_high()
        if len(components) < MODEL_DISAGREEMENT_MIN_COMPONENTS:
            return 0.0

        weights = self.model_weights or {}
        w = {name: weights.get(name, 1.0) for name in components}
        weight_total = sum(w.values())
        if weight_total <= 0:
            return statistics.pstdev(components.values())

        weighted_mean = sum(w[name] * val for name, val in components.items()) / weight_total
        weighted_var = sum(
            w[name] * (val - weighted_mean) ** 2 for name, val in components.items()
        ) / weight_total
        return weighted_var ** 0.5

    def _eff_std_high(self) -> float:
        """
        Combined uncertainty: GFS ensemble's own internal spread and
        cross-model disagreement are independent sources of error, so they
        combine in quadrature (sqrt of sum of squares) rather than adding
        linearly — standard treatment for two independent variance
        contributions. Floored at TEMP_UNCERTAINTY_FLOOR_F as before.
        """
        combined = (self.std_high ** 2 + self._model_disagreement_std_high() ** 2) ** 0.5
        return max(combined, TEMP_UNCERTAINTY_FLOOR_F)

    def _eff_std_low(self) -> float:
        return max(self.std_low, TEMP_UNCERTAINTY_FLOOR_F)

    # ------------------------------------------------------------------
    # Probability methods — Student-t CDF primary, count blend secondary.
    #
    # Strategy:
    #   1. Always compute a tail probability using the ensemble mean and
    #      the *effective* std (with floor and model-disagreement
    #      widening), via a Student-t(df=TAIL_DIST_DF) rather than a plain
    #      Gaussian — see TAIL_DIST_DF's docstring: real forecast-error
    #      tails decay slower than Gaussian, and this bot trades almost
    #      exclusively the deep tails (z>=2), which is exactly where that
    #      difference matters most. scale is rescaled (_t_scale) so the
    #      distribution's variance still equals eff_std**2 — this changes
    #      tail shape, not the central spread the floor/widening calibrate.
    #   2. If we have ≥ 5 members, blend in the empirical count fraction
    #      weighted by sqrt(n/31). The parametric tail dominates where the
    #      count undersamples; the count dominates near the mean.
    # ------------------------------------------------------------------

    def _p_dist_high_above(self, threshold_f: float) -> float:
        """Parametric-only (Student-t CDF) tail probability, no count blend —
        shared by probability_high_above() and probability_high_between()."""
        return float(1.0 - _t.cdf(
            threshold_f, df=TAIL_DIST_DF,
            loc=self.effective_mean_high(), scale=_t_scale(self._eff_std_high()),
        ))

    def probability_high_above(self, threshold_f: float) -> float:
        """P(daily_high > threshold_f) via Student-t CDF + empirical blend."""
        if not self.member_highs:
            return 0.5
        p_dist = self._p_dist_high_above(threshold_f)
        n = len(self.member_highs)
        if n >= 5:
            k = sum(1 for h in self.member_highs if h > threshold_f)
            # Laplace-smoothed count — see COUNT_PSEUDO_SAMPLES above.
            p_count = (k + COUNT_PSEUDO_SAMPLES / 2) / (n + COUNT_PSEUDO_SAMPLES)
            w = min((n / 31.0) ** 0.5, 1.0)
            return w * p_count + (1.0 - w) * p_dist
        return p_dist

    def probability_high_below(self, threshold_f: float) -> float:
        """P(daily_high < threshold_f)."""
        return 1.0 - self.probability_high_above(threshold_f)

    def _p_dist_low_above(self, threshold_f: float) -> float:
        """Parametric-only (Student-t CDF) tail probability, no count blend —
        shared by probability_low_above() and probability_low_between()."""
        # low uses GFS mean directly — no ECMWF/NWS cross-check exists for
        # lows, so no model-disagreement term to fold into _eff_std_low.
        return float(1.0 - _t.cdf(
            threshold_f, df=TAIL_DIST_DF,
            loc=self.mean_low, scale=_t_scale(self._eff_std_low()),
        ))

    def probability_low_above(self, threshold_f: float) -> float:
        """P(daily_low > threshold_f) via Student-t CDF + empirical blend."""
        if not self.member_lows:
            return 0.5
        p_dist = self._p_dist_low_above(threshold_f)
        n = len(self.member_lows)
        if n >= 5:
            k = sum(1 for lo in self.member_lows if lo > threshold_f)
            # Laplace-smoothed count — see COUNT_PSEUDO_SAMPLES above.
            p_count = (k + COUNT_PSEUDO_SAMPLES / 2) / (n + COUNT_PSEUDO_SAMPLES)
            w = min((n / 31.0) ** 0.5, 1.0)
            return w * p_count + (1.0 - w) * p_dist
        return p_dist

    def probability_low_below(self, threshold_f: float) -> float:
        """P(daily_low < threshold_f)."""
        return 1.0 - self.probability_low_above(threshold_f)

    def probability_high_between(self, floor_f: float, cap_f: float) -> float:
        """
        P(floor_f <= daily_high <= cap_f) — narrow Kalshi bracket contracts.

        Root-caused 2026-08-22: unlike probability_high_above/below, this
        deliberately does NOT blend in the empirical member count. A
        31-member ensemble is a coarse ruler for "did the high land in this
        specific ~1F window" — it's easy for 0 or 1 of 31 discrete samples
        to miss a narrow band even when the true continuous probability is
        a perfectly ordinary 25-40%, and at a full ensemble (n=31) the count
        blend gets FULL weight (w=1.0 in probability_high_above), so that
        small-sample noise was replacing the much more stable parametric
        estimate entirely rather than supplementing it. Real data: trades
        priced off the count-blended version claimed ~97% average
        confidence on this exact bracket type, settling only 33.3% of the
        time (n=6, 95% CI [9.7%, 70.0%] — doesn't contain the stated
        figure). The count blend is still correct and untouched for simple
        above/below thresholds, where a large, stable fraction of the
        ensemble is expected on each side — that's a fundamentally
        different, count-appropriate question.
        """
        return max(0.0, self._p_dist_high_above(floor_f) - self._p_dist_high_above(cap_f))

    def probability_low_between(self, floor_f: float, cap_f: float) -> float:
        """P(floor_f <= daily_low <= cap_f) — narrow Kalshi bracket contracts.
        See probability_high_between()'s docstring: same reasoning, parametric-only."""
        return max(0.0, self._p_dist_low_above(floor_f) - self._p_dist_low_above(cap_f))

    @property
    def ensemble_agreement(self) -> float:
        """How one-sided the ensemble is (0.5 = split, 1.0 = unanimous)."""
        if not self.member_highs:
            return 0.5
        median = statistics.median(self.member_highs)
        above = sum(1 for h in self.member_highs if h > median)
        frac = above / len(self.member_highs)
        return max(frac, 1 - frac)


# ---------------------------------------------------------------------------
# Sanity assertions — called after each fetch so degenerate data surfaces
# immediately in logs rather than silently producing garbage probabilities.
# ---------------------------------------------------------------------------

def _assert_forecast_sanity(forecast: EnsembleForecast) -> None:
    """
    Log warnings when the forecast fails basic physical sanity checks.

    Not raised as exceptions so a bad forecast for one city doesn't
    kill the whole scan; the warnings surface in the scheduler log.

    Example expectations for a mean_high of 90 °F ± 3 °F:
      - P(high > mean+4*floor) should be < 8%   (4-sigma tail)
      - P(high > mean)         should be 45–55%  (near 50% by symmetry)
    """
    mean = forecast.mean_high
    floor = TEMP_UNCERTAINTY_FLOOR_F

    p_at_mean = forecast.probability_high_above(mean)
    if not (0.35 <= p_at_mean <= 0.65):
        logger.warning(
            f"[SANITY] {forecast.city_name}: P(high>{mean:.1f}F) = {p_at_mean:.1%}, "
            f"expected ~50%%. Ensemble may be degenerate (n={forecast.num_members}, "
            f"std={forecast.std_high:.2f}F)."
        )

    far_threshold = mean + 4 * max(forecast.std_high, floor)
    p_far_tail = forecast.probability_high_above(far_threshold)
    if p_far_tail > 0.10:
        logger.warning(
            f"[SANITY] {forecast.city_name}: P(high>{far_threshold:.1f}F) = {p_far_tail:.1%}, "
            f"expected <10%% (4-sigma tail). Gaussian spread may be too wide."
        )

    if forecast.num_members < 5:
        logger.warning(
            f"[SANITY] {forecast.city_name}: only {forecast.num_members} ensemble member(s) "
            f"fetched. Probabilities rely entirely on Gaussian approximation with "
            f"{floor}°F uncertainty floor."
        )


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_forecast_cache: Dict[str, tuple] = {}
_CACHE_TTL = 900  # 15 minutes

_nws_obs_cache: Dict[str, tuple] = {}
_NWS_OBS_CACHE_TTL = 300  # 5 minutes

# Live METAR snapshot cache — short TTL so guardrails see recent readings.
_metar_cache: Dict[str, tuple] = {}
METAR_CACHE_TTL = 180  # 3 minutes


def _celsius_to_fahrenheit(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


# ---------------------------------------------------------------------------
# Live METAR snapshot (used by execution guardrails)
# ---------------------------------------------------------------------------

@dataclass
class METARSnapshot:
    """Most-recent METAR observation + 1-hour warming/cooling trend."""
    city_key: str
    station: str
    observed_temp_f: float      # current surface temperature in °F
    trend_f_per_hour: float     # °F/hr; +ve = warming, −ve = cooling, 0.0 = single obs
    observed_at: datetime       # UTC of the most recent reading


async def fetch_metar_current(city_key: str) -> Optional[METARSnapshot]:
    """
    Fetch the live METAR temperature for the Kalshi settlement station.

    Calls `GET /stations/{station}/observations?limit=12` (the last ~1-2 hours
    of ASOS reports) and computes a 1-hour trend by comparing the newest
    reading to the observation closest to 60 minutes prior.

    Cache TTL: 3 minutes. None is never cached (station outage → immediate retry).
    """
    if city_key not in CITY_CONFIG:
        return None

    cache_key = f"metar_{city_key}"
    now = time.time()
    if cache_key in _metar_cache:
        cached_val, cached_ts = _metar_cache[cache_key]
        if now - cached_ts < METAR_CACHE_TTL:
            return cached_val

    city = CITY_CONFIG[city_key]
    station = city["nws_station"]

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"https://api.weather.gov/stations/{station}/observations",
                params={"limit": 12},
                headers={"User-Agent": "(weather-trading-bot, contact@example.com)"},
            )
            resp.raise_for_status()
            data = resp.json()

        features = data.get("features", [])
        if not features:
            return None

        # Parse all valid (timestamp, temp_f) pairs; NWS returns newest-first.
        obs_list: List[tuple] = []
        for feat in features:
            props = feat.get("properties", {})
            ts_str = props.get("timestamp")
            temp_c = props.get("temperature", {}).get("value")
            if ts_str is None or temp_c is None:
                continue
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).replace(tzinfo=None)
                obs_list.append((ts, _celsius_to_fahrenheit(float(temp_c))))
            except Exception:
                continue

        if not obs_list:
            return None

        obs_list.sort(key=lambda x: x[0], reverse=True)
        current_ts, current_temp_f = obs_list[0]

        # 1-hour trend: compare to the observation closest to 60 min ago.
        trend_f_per_hour = 0.0
        if len(obs_list) > 1:
            target_prior = current_ts - timedelta(hours=1)
            prior_ts, prior_temp_f = min(
                obs_list[1:],
                key=lambda x: abs((x[0] - target_prior).total_seconds()),
            )
            delta_h = (current_ts - prior_ts).total_seconds() / 3600.0
            if delta_h >= 0.05:  # require at least 3-minute gap
                trend_f_per_hour = (current_temp_f - prior_temp_f) / delta_h

        snapshot = METARSnapshot(
            city_key=city_key,
            station=station,
            observed_temp_f=current_temp_f,
            trend_f_per_hour=trend_f_per_hour,
            observed_at=current_ts,
        )
        _metar_cache[cache_key] = (snapshot, now)
        logger.info(
            f"METAR {station}: {current_temp_f:.1f}°F "
            f"(trend {trend_f_per_hour:+.2f}°F/hr)"
        )
        return snapshot

    except Exception as e:
        logger.warning(f"METAR fetch failed for {city_key} ({station}): {e}")
        return None


# ---------------------------------------------------------------------------
# Ensemble fetch — hourly temperature_2m with gfs025 (explicit 31-member GEFS)
# ---------------------------------------------------------------------------

async def fetch_ensemble_forecast(
    city_key: str,
    target_date: Optional[date] = None,
) -> Optional[EnsembleForecast]:
    """
    Fetch 31-member GFS ensemble forecast from Open-Meteo.

    Uses hourly temperature_2m data (not daily aggregations) because
    the daily endpoint for gfs_seamless returns only the control member,
    giving member_highs=[single_value] and causing probability collapse.

    gfs025 = GEFS 0.25° ensemble, 31 members, keys:
        temperature_2m (control), temperature_2m_member01 … member30
    Daily high/low are derived by taking max/min over the 24 hourly values
    for each member.
    """
    if city_key not in CITY_CONFIG:
        logger.warning(f"Unknown city key: {city_key}")
        return None

    if target_date is None:
        target_date = date.today()

    cache_key = f"{city_key}_{target_date.isoformat()}"
    now = time.time()
    if cache_key in _forecast_cache:
        cached_time, cached_forecast = _forecast_cache[cache_key]
        if now - cached_time < _CACHE_TTL:
            return cached_forecast

    city = CITY_CONFIG[city_key]

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            params = {
                "latitude": city["lat"],
                "longitude": city["lon"],
                "hourly": "temperature_2m",
                "temperature_unit": "fahrenheit",
                "start_date": target_date.isoformat(),
                "end_date": target_date.isoformat(),
                # gfs025 = explicit 31-member GEFS; avoids the gfs_seamless
                # deterministic-only response that caused the probability collapse.
                "models": "gfs025",
            }

            response = await client.get(
                "https://ensemble-api.open-meteo.com/v1/ensemble",
                params=params,
            )
            response.raise_for_status()
            data = response.json()

        hourly = data.get("hourly", {})
        if not hourly:
            logger.warning(f"Empty hourly block for {city_key} on {target_date}")
            return None

        member_highs: List[float] = []
        member_lows: List[float] = []

        # Every key whose name starts with "temperature_2m" is a member array.
        # The control run uses the bare key; perturbed members use _member01 … _member30.
        for key, values in hourly.items():
            if not key.startswith("temperature_2m"):
                continue
            if not isinstance(values, list):
                continue
            valid = [v for v in values if v is not None]
            if not valid:
                continue
            member_highs.append(max(valid))
            member_lows.append(min(valid))

        if not member_highs:
            logger.warning(f"No temperature members found for {city_key} on {target_date}. "
                           f"API keys returned: {list(hourly.keys())[:10]}")
            return None

        forecast = EnsembleForecast(
            city_key=city_key,
            city_name=city["name"],
            target_date=target_date,
            member_highs=member_highs,
            member_lows=member_lows,
        )

        _assert_forecast_sanity(forecast)

        # Blend HRRR when target is within HRRR_LOOKAHEAD_HOURS
        days_ahead = (target_date - date.today()).days
        hours_ahead = days_ahead * 24 + datetime.utcnow().hour
        if days_ahead == 0 or (days_ahead == 1 and hours_ahead < HRRR_LOOKAHEAD_HOURS):
            hrrr_high = await fetch_hrrr_forecast_high(city_key, target_date)
            if hrrr_high is not None:
                forecast.hrrr_high = hrrr_high
                logger.info(
                    f"HRRR blend ready for {city['name']}: "
                    f"GFS {forecast.mean_high:.1f}°F, HRRR {hrrr_high:.1f}°F "
                    f"(active during {SOLAR_WINDOW_START}h–{SOLAR_WINDOW_END}h local)"
                )

        # Independent-model cross-checks + recent bias correction — added
        # 2026-08-17. Run concurrently since they're four unrelated
        # network/DB calls, not a dependency chain.
        ecmwf_high, nws_high, bias, model_weights = await asyncio.gather(
            fetch_ecmwf_forecast_high(city_key, target_date),
            fetch_nws_forecast_high(city_key, target_date),
            get_recent_bias(city_key),
            get_model_weights(city_key),
            return_exceptions=True,
        )
        if isinstance(ecmwf_high, float):
            forecast.ecmwf_high = ecmwf_high
        if isinstance(nws_high, float):
            forecast.nws_high = nws_high
        if isinstance(bias, float):
            forecast.bias_correction_f = bias
        if isinstance(model_weights, dict):
            forecast.model_weights = model_weights

        # Log this fetch's per-model raw predictions so get_model_weights()
        # has data to score once target_date settles. Fire-and-forget-safe:
        # internally rate-limited and swallows its own errors.
        await _log_model_forecasts(city_key, target_date, forecast)

        divergence_bits = []
        if forecast.ecmwf_high is not None:
            divergence_bits.append(f"ECMWF {forecast.ecmwf_high:.1f}F")
        if forecast.nws_high is not None:
            divergence_bits.append(f"NWS {forecast.nws_high:.1f}F")
        if divergence_bits:
            logger.info(
                f"Cross-check {city['name']}: GFS {forecast.mean_high:.1f}F vs "
                f"{', '.join(divergence_bits)} | bias correction {forecast.bias_correction_f:+.1f}F "
                f"| effective mean {forecast.effective_mean_high():.1f}F"
            )

        _forecast_cache[cache_key] = (now, forecast)
        logger.info(
            f"Ensemble [{forecast.num_members}m] {city['name']} {target_date}: "
            f"High {forecast.mean_high:.1f}±{forecast.std_high:.1f}F  "
            f"Low {forecast.mean_low:.1f}±{forecast.std_low:.1f}F"
        )
        return forecast

    except Exception as e:
        logger.warning(f"Failed to fetch ensemble forecast for {city_key}: {e}")
        return None


# ---------------------------------------------------------------------------
# NWS observed temperature (used for independent settlement verification)
# ---------------------------------------------------------------------------

async def fetch_nws_observed_temperature(
    city_key: str,
    target_date: Optional[date] = None,
) -> Optional[Dict[str, float]]:
    """
    Fetch observed temperature from NWS API for settlement verification.
    Returns {'high': float, 'low': float} in Fahrenheit, or None.
    """
    if city_key not in CITY_CONFIG:
        return None

    city = CITY_CONFIG[city_key]
    if target_date is None:
        target_date = date.today()

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            station = city["nws_station"]
            url = f"https://api.weather.gov/stations/{station}/observations"
            headers = {"User-Agent": "(trading-bot, contact@example.com)"}

            # target_date is the station's LOCAL calendar date (matches the Kalshi
            # ticker's date and "daily high" definition). Build the window from
            # local midnight, not naive UTC midnight — for a west-of-UTC station
            # (e.g. Denver, UTC-6), naive UTC midnight is still the PREVIOUS local
            # evening, so the old code could report last night's cooldown as
            # "today's high so far" hours before the local day even started.
            # (Root-caused 2026-08-13: caused a bogus early-settlement.)
            tz = ZoneInfo(city.get("timezone", "UTC"))
            local_start = datetime.combine(target_date, datetime.min.time(), tzinfo=tz)
            local_end = datetime.combine(target_date + timedelta(days=1), datetime.min.time(), tzinfo=tz)
            start = local_start.astimezone(ZoneInfo("UTC")).isoformat()
            end = local_end.astimezone(ZoneInfo("UTC")).isoformat()

            response = await client.get(url, params={"start": start, "end": end}, headers=headers)
            response.raise_for_status()
            data = response.json()

            features = data.get("features", [])
            if not features:
                return None

            temps = []
            for obs in features:
                props = obs.get("properties", {})
                temp_c = props.get("temperature", {}).get("value")
                if temp_c is not None:
                    temps.append(_celsius_to_fahrenheit(temp_c))

            if not temps:
                return None

            return {"high": max(temps), "low": min(temps)}

    except Exception as e:
        logger.warning(f"Failed to fetch NWS observations for {city_key}: {e}")
        return None


async def fetch_hrrr_forecast_high(city_key: str, target_date: date) -> Optional[float]:
    """
    Fetch NOAA HRRR deterministic daily-high from Open-Meteo (api.open-meteo.com, not ensemble-api).
    Returns the max of 24 hourly temperature_2m values for target_date in °F, or None.
    5-minute TTL cache (HRRR updates hourly from NWS).
    """
    cache_key = f"hrrr_{city_key}_{target_date.isoformat()}"
    now = time.time()
    if cache_key in _hrrr_cache:
        val, ts = _hrrr_cache[cache_key]
        if now - ts < _HRRR_CACHE_TTL:
            return val

    city = CITY_CONFIG.get(city_key)
    if not city:
        return None

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            params = {
                "latitude":         city["lat"],
                "longitude":        city["lon"],
                "hourly":           "temperature_2m",
                "temperature_unit": "fahrenheit",
                # "hrrr" is not a valid Open-Meteo model identifier — this was
                # silently 400ing on every single request (root-caused
                # 2026-08-14), meaning the HRRR/GFS solar-window blend in
                # effective_mean_high() never actually activated; the model
                # has been running on GFS ensemble alone this whole time.
                "models":           "ncep_hrrr_conus",
                "start_date":       target_date.isoformat(),
                "end_date":         target_date.isoformat(),
            }
            response = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params=params,
            )
            response.raise_for_status()
            data = response.json()

        values = data.get("hourly", {}).get("temperature_2m", [])
        valid = [v for v in values if v is not None]
        if not valid:
            return None

        high_f = max(valid)
        _hrrr_cache[cache_key] = (high_f, now)
        logger.info(f"HRRR {city['name']} {target_date}: daily high {high_f:.1f}°F")
        return high_f

    except Exception as e:
        logger.warning(f"HRRR fetch failed for {city_key}: {e}")
        return None


_ecmwf_cache: Dict[str, tuple] = {}
_ECMWF_CACHE_TTL = 900  # 15 minutes — ECMWF ensemble updates less often than HRRR


async def fetch_ecmwf_forecast_high(city_key: str, target_date: date) -> Optional[float]:
    """
    Fetch ECMWF's own 51-member ensemble mean daily-high (independent model
    family from GFS — different physics, different biases). Added 2026-08-17:
    GFS alone had run 3-4°F warm for NY/Boston two consecutive days with
    nothing to catch it; this gives effective_mean_high() a second opinion
    from a genuinely different model, not just another GFS perturbation.
    """
    cache_key = f"ecmwf_{city_key}_{target_date.isoformat()}"
    now = time.time()
    if cache_key in _ecmwf_cache:
        val, ts = _ecmwf_cache[cache_key]
        if now - ts < _ECMWF_CACHE_TTL:
            return val

    city = CITY_CONFIG.get(city_key)
    if not city:
        return None

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            params = {
                "latitude":         city["lat"],
                "longitude":        city["lon"],
                "hourly":           "temperature_2m",
                "temperature_unit": "fahrenheit",
                "start_date":       target_date.isoformat(),
                "end_date":         target_date.isoformat(),
                "models":           "ecmwf_ifs025",
            }
            response = await client.get(
                "https://ensemble-api.open-meteo.com/v1/ensemble",
                params=params,
            )
            response.raise_for_status()
            data = response.json()

        hourly = data.get("hourly", {})
        member_highs = []
        for key, values in hourly.items():
            if not key.startswith("temperature_2m"):
                continue
            valid = [v for v in values if v is not None]
            if valid:
                member_highs.append(max(valid))

        if not member_highs:
            return None

        mean_f = statistics.mean(member_highs)
        _ecmwf_cache[cache_key] = (mean_f, now)
        logger.info(f"ECMWF [{len(member_highs)}m] {city['name']} {target_date}: mean high {mean_f:.1f}°F")
        return mean_f

    except Exception as e:
        logger.warning(f"ECMWF fetch failed for {city_key}: {e}")
        return None


_nws_forecast_cache: Dict[str, tuple] = {}
_NWS_FORECAST_CACHE_TTL = 1800  # 30 minutes — NWS forecasts update a few times a day


async def fetch_nws_forecast_high(city_key: str, target_date: date) -> Optional[float]:
    """
    Fetch the National Weather Service's own human-reviewed forecast high
    for target_date, via the gridpoint config already sitting unused in
    CITY_CONFIG. Added 2026-08-17 alongside the ECMWF cross-check — this
    one is free (no new API dependency) and was flagged as available but
    never wired up.
    """
    cache_key = f"nwsfc_{city_key}_{target_date.isoformat()}"
    now = time.time()
    if cache_key in _nws_forecast_cache:
        val, ts = _nws_forecast_cache[cache_key]
        if now - ts < _NWS_FORECAST_CACHE_TTL:
            return val

    city = CITY_CONFIG.get(city_key)
    gridpoint = city.get("nws_gridpoint") if city else None
    if not gridpoint:
        return None

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                f"https://api.weather.gov/gridpoints/{gridpoint}/forecast",
                headers={"User-Agent": "(trading-bot, contact@example.com)"},
            )
            response.raise_for_status()
            data = response.json()

        tz_str = city.get("timezone", "UTC")
        periods = data.get("properties", {}).get("periods", [])
        for p in periods:
            start = p.get("startTime")
            if not start:
                continue
            local_date = datetime.fromisoformat(start).astimezone(ZoneInfo(tz_str)).date()
            # NWS periods alternate day/night ("isDaytime") — the daytime
            # period for target_date carries that day's forecast high.
            if local_date == target_date and p.get("isDaytime"):
                high_f = float(p["temperature"])
                _nws_forecast_cache[cache_key] = (high_f, now)
                return high_f

        return None

    except Exception as e:
        logger.warning(f"NWS forecast fetch failed for {city_key}: {e}")
        return None


_bias_cache: Dict[str, tuple] = {}
_BIAS_CACHE_TTL = 3600  # 1 hour — recompute periodically, not on every scan


async def get_recent_bias(city_key: str, lookback_days: int = 3) -> float:
    """
    Rolling per-city forecast bias: mean (actual − predicted) high over the
    last `lookback_days` *completed* days, from this city's own settled
    signal history. Added 2026-08-17 after finding NY and Boston had run
    3-4°F warm two days running — a repeating, city-specific error, which is
    exactly what this corrects. This is a different kind of fix from the
    ECMWF/NWS cross-checks above: those catch "our model disagrees with
    other models"; this catches "our model has been consistently wrong in
    the same direction for this specific city recently," which an
    independent model wouldn't necessarily flag if it's *also* off that way.

    Returns 0.0 (no correction) when there isn't enough recent history —
    never invents a correction from a single noisy data point.
    """
    now = time.time()
    if city_key in _bias_cache:
        val, ts = _bias_cache[city_key]
        if now - ts < _BIAS_CACHE_TTL:
            return val

    bias = 0.0
    try:
        import re as _re
        from backend.models.database import SessionLocal, Signal
        from backend.data.kalshi_markets import CITY_SERIES, MONTH_ABBR

        series = CITY_SERIES.get(city_key)
        if not series:
            return 0.0

        db = SessionLocal()
        try:
            rows = (
                db.query(Signal.market_ticker, Signal.reasoning, Signal.timestamp)
                .filter(
                    Signal.market_ticker.like(f"{series}-%"),
                    Signal.reasoning.like("%Ensemble%"),
                )
                .order_by(Signal.timestamp.asc())
                .limit(2000)
                .all()
            )
        finally:
            db.close()

        # Last (most recent, closest-to-settlement) prediction per target
        # date — later rows overwrite earlier ones since we iterate in
        # ascending timestamp order.
        predicted_by_date: Dict[date, float] = {}
        for ticker, reasoning, _ts in rows:
            m_date = _re.match(r'^[A-Z]+-(\d{2})([A-Z]{3})(\d{2})-', ticker)
            m_mean = _re.search(r'Ensemble \[\d+m\]: ([\d.]+)F', reasoning or "")
            if not m_date or not m_mean:
                continue
            month = MONTH_ABBR.get(m_date.group(2))
            if not month:
                continue
            try:
                d = date(2000 + int(m_date.group(1)), month, int(m_date.group(3)))
            except ValueError:
                continue
            predicted_by_date[d] = float(m_mean.group(1))

        today = datetime.utcnow().date()
        past_dates = sorted((d for d in predicted_by_date if d < today), reverse=True)[:lookback_days]

        errors = []
        for d in past_dates:
            actual = await fetch_station_high_water_mark(city_key, d)
            if actual is not None:
                errors.append(actual - predicted_by_date[d])

        if errors:
            # Root-caused 2026-08-17: backtested against a week of real
            # signals/outcomes (trades_rows.csv / signals_rows.csv, 38
            # city-days). A plain mean let one unusually large single-day
            # miss drag the correction for the following ~3 days even after
            # the forecast had already self-corrected — MAE was WORSE than
            # no correction at all (4.47F vs 4.19F raw). Median over the
            # same window is immune to that single-outlier drag and beat
            # raw in every slice tested (4.14F full sample, 2.94F excluding
            # one known bad-pipeline day) — never worse, unlike the mean.
            bias = statistics.median(errors)
            bias = max(-6.0, min(6.0, bias))  # clamp — never let one bad parse swing this wildly
            logger.info(
                f"Bias correction {CITY_CONFIG.get(city_key, {}).get('name', city_key)}: "
                f"{bias:+.1f}F (from {len(errors)} of last {lookback_days} day(s))"
            )
    except Exception as e:
        logger.warning(f"Bias correction lookup failed for {city_key}: {e}")
        bias = 0.0

    _bias_cache[city_key] = (bias, now)
    return bias


# ---------------------------------------------------------------------------
# Rolling per-model accuracy weighting — added 2026-08-17. Extends the same
# "our model has been consistently wrong recently" idea behind get_recent_bias
# above, but per model family instead of on the already-blended output:
# get_recent_bias corrects "the combined forecast is off by X"; this corrects
# "GFS specifically is running warmer than ECMWF/NWS this week for this city,
# so trust it less in the blend" — a different failure mode an independent
# model wouldn't necessarily catch if it's drifting the same direction too.
# ---------------------------------------------------------------------------

MODEL_WEIGHT_LOOKBACK_DAYS = 14
MIN_MODEL_SAMPLES = 3            # below this, keep the model at its default weight
DEFAULT_MODEL_WEIGHTS: Dict[str, float] = {"gfs": 1.0, "ecmwf": 1.0, "nws": 1.0}  # equal weight = current (pre-2026-08-17) behaviour

_model_weight_cache: Dict[str, tuple] = {}
_MODEL_WEIGHT_CACHE_TTL = 3600  # 1 hour — recompute periodically, not on every scan

_model_forecast_log_written: Dict[tuple, float] = {}
_MODEL_FORECAST_LOG_MIN_INTERVAL = 1800  # avoid a write on every 900s-cache-expiry re-fetch


async def _log_model_forecasts(city_key: str, target_date: date, forecast: "EnsembleForecast") -> None:
    """
    Persist each independently-fetched model's raw daily-high prediction so
    get_model_weights() has something to score later, once the day settles.

    Rate-limited to one write per (city, date) per _MODEL_FORECAST_LOG_MIN_
    INTERVAL — forecasts get re-fetched every ~15 min throughout the day as
    the cache expires, and only the freshest-before-settlement prediction
    per model per day matters for scoring (same "latest wins" read pattern
    as get_recent_bias), so there's no value in writing every re-fetch.
    """
    key = (city_key, target_date)
    now = time.time()
    last_write = _model_forecast_log_written.get(key, 0.0)
    if now - last_write < _MODEL_FORECAST_LOG_MIN_INTERVAL:
        return

    rows = [("gfs", forecast.mean_high)]
    if forecast.ecmwf_high is not None:
        rows.append(("ecmwf", forecast.ecmwf_high))
    if forecast.nws_high is not None:
        rows.append(("nws", forecast.nws_high))

    try:
        from backend.models.database import SessionLocal, ModelForecastLog
        db = SessionLocal()
        try:
            for model_name, predicted in rows:
                db.add(ModelForecastLog(
                    city_key=city_key,
                    target_date=target_date,
                    model_name=model_name,
                    predicted_high_f=predicted,
                ))
            db.commit()
        finally:
            db.close()
        _model_forecast_log_written[key] = now
    except Exception as e:
        logger.debug(f"Model forecast logging failed for {city_key}/{target_date}: {e}")


async def get_model_weights(city_key: str, lookback_days: int = MODEL_WEIGHT_LOOKBACK_DAYS) -> Dict[str, float]:
    """
    Rolling inverse-MAE weight per model family (gfs/ecmwf/nws) for this
    city, from the last `lookback_days` *completed* days of logged
    predictions vs. this city's own settled highs.

    Any model with fewer than MIN_MODEL_SAMPLES scoreable days keeps its
    default weight of 1.0 (i.e. contributes at full, equal weight to the
    blend) rather than being penalized off a thin sample — same
    overfitting guard as get_recent_bias's minimum-sample gate. A city
    with no history at all returns DEFAULT_MODEL_WEIGHTS unchanged, which
    reproduces plain equal-weight averaging — cold start is a no-op.
    """
    now = time.time()
    if city_key in _model_weight_cache:
        val, ts = _model_weight_cache[city_key]
        if now - ts < _MODEL_WEIGHT_CACHE_TTL:
            return val

    weights = dict(DEFAULT_MODEL_WEIGHTS)
    try:
        from backend.models.database import SessionLocal, ModelForecastLog

        cutoff = datetime.utcnow().date() - timedelta(days=lookback_days)
        today = datetime.utcnow().date()

        db = SessionLocal()
        try:
            rows = (
                db.query(
                    ModelForecastLog.model_name,
                    ModelForecastLog.target_date,
                    ModelForecastLog.predicted_high_f,
                )
                .filter(
                    ModelForecastLog.city_key == city_key,
                    ModelForecastLog.target_date >= cutoff,
                    ModelForecastLog.target_date < today,
                )
                .order_by(ModelForecastLog.logged_at.asc())
                .all()
            )
        finally:
            db.close()

        # Last-logged prediction per (model, date) — later rows overwrite
        # earlier ones since we iterate in ascending logged_at order, same
        # "latest wins" pattern as get_recent_bias's predicted_by_date.
        last_pred: Dict[tuple, float] = {}
        for model_name, tgt_date, predicted in rows:
            last_pred[(model_name, tgt_date)] = predicted

        errors_by_model: Dict[str, List[float]] = {}
        for (model_name, tgt_date), predicted in last_pred.items():
            actual = await fetch_station_high_water_mark(city_key, tgt_date)
            if actual is not None:
                errors_by_model.setdefault(model_name, []).append(abs(actual - predicted))

        for model_name, errs in errors_by_model.items():
            if len(errs) < MIN_MODEL_SAMPLES:
                continue  # thin sample — leave this model at its default weight
            mae = statistics.mean(errs)
            weights[model_name] = 1.0 / max(mae, 0.5)  # floored so a near-zero MAE can't blow the weight up unboundedly

        if any(m in errors_by_model and len(errors_by_model[m]) >= MIN_MODEL_SAMPLES for m in weights):
            logger.info(
                f"Model weights {CITY_CONFIG.get(city_key, {}).get('name', city_key)}: "
                + ", ".join(f"{m}={w:.2f}" for m, w in weights.items())
            )
    except Exception as e:
        logger.warning(f"Model weight computation failed for {city_key}: {e}")
        weights = dict(DEFAULT_MODEL_WEIGHTS)

    _model_weight_cache[city_key] = (weights, now)
    return weights


async def _fetch_and_cache_nws_obs(city_key: str, target_date: date) -> Optional[Dict[str, float]]:
    """
    Shared underlying fetch for both fetch_station_high_water_mark() and
    fetch_station_low_water_mark() — added 2026-08-23 alongside the low-temp
    "Part 2" work. fetch_nws_observed_temperature() already returns BOTH
    {'high', 'low'} in a single call; caching them together here means a
    city/date with open positions on both metrics still pays only one NWS
    request per 5-minute window, not two. None is never cached — a station
    outage allows an immediate retry on the next scheduler tick.
    """
    cache_key = f"nws_obs_{city_key}_{target_date.isoformat()}"
    now = time.time()
    if cache_key in _nws_obs_cache:
        cached_val, cached_ts = _nws_obs_cache[cache_key]
        if now - cached_ts < _NWS_OBS_CACHE_TTL:
            return cached_val

    obs = await fetch_nws_observed_temperature(city_key, target_date)
    if obs is None:
        return None

    # Only cache once BOTH metrics are present. A partial reading (one
    # field missing) is treated like a station glitch: still returned for
    # this call, but not cached — otherwise a caller for the OTHER metric
    # could get stuck with a cached result that's missing exactly the
    # field it needs until the TTL expires, instead of retrying next tick.
    if obs.get("high") is not None and obs.get("low") is not None:
        _nws_obs_cache[cache_key] = (obs, now)
    return obs


async def fetch_station_high_water_mark(
    city_key: str,
    target_date: date,
) -> Optional[float]:
    """Return the highest observed temperature so far for city_key on target_date."""
    obs = await _fetch_and_cache_nws_obs(city_key, target_date)
    return obs.get("high") if obs else None


async def fetch_station_low_water_mark(
    city_key: str,
    target_date: date,
) -> Optional[float]:
    """
    Return the lowest observed temperature so far for city_key on
    target_date — the low-temp counterpart to fetch_station_high_water_mark,
    added 2026-08-23. Shares the same underlying fetch/cache, so calling
    both for the same city/date costs one NWS request, not two.
    """
    obs = await _fetch_and_cache_nws_obs(city_key, target_date)
    return obs.get("low") if obs else None
