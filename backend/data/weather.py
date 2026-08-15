"""Weather data fetcher using Open-Meteo Ensemble API and NWS observations."""
import httpx
import logging
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional
import statistics
import time
from zoneinfo import ZoneInfo

from scipy.stats import norm as _norm

logger = logging.getLogger("trading_bot")

# Minimum temperature uncertainty floor in °F.
# Even a perfect 31-member ensemble has ~2-4°F short-range forecast error.
# This prevents probability collapse when std → 0 (degenerate or single-member fetch).
TEMP_UNCERTAINTY_FLOOR_F = 3.0

# HRRR blending parameters
HRRR_WEIGHT          = 0.60   # weight given to HRRR during solar window
GFS_WEIGHT           = 0.40   # weight given to GFS mean during solar window
SOLAR_WINDOW_START   = 12     # 12:00 local time
SOLAR_WINDOW_END     = 17     # 17:59 local time (inclusive)
HRRR_LOOKAHEAD_HOURS = 18     # fetch HRRR only when target is within this many hours

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
    # std = 0. Without the floor, the Gaussian CDF degenerates to a step
    # function, reproducing the original 0%/100% collapse.
    # ------------------------------------------------------------------

    def effective_mean_high(self) -> float:
        """GFS mean, blended toward HRRR at 60% during afternoon solar window."""
        if self.hrrr_high is None:
            return self.mean_high
        city = CITY_CONFIG.get(self.city_key, {})
        tz_str = city.get("timezone", "UTC")
        local_hour = datetime.now(ZoneInfo(tz_str)).hour
        if SOLAR_WINDOW_START <= local_hour <= SOLAR_WINDOW_END:
            return HRRR_WEIGHT * self.hrrr_high + GFS_WEIGHT * self.mean_high
        return self.mean_high

    def _eff_std_high(self) -> float:
        return max(self.std_high, TEMP_UNCERTAINTY_FLOOR_F)

    def _eff_std_low(self) -> float:
        return max(self.std_low, TEMP_UNCERTAINTY_FLOOR_F)

    # ------------------------------------------------------------------
    # Probability methods — Gaussian CDF primary, count blend secondary.
    #
    # Strategy:
    #   1. Always compute a Gaussian probability using the ensemble mean
    #      and the *effective* std (with floor). This gives smooth tail
    #      decay even with a single-member fetch.
    #   2. If we have ≥ 5 members, blend in the empirical count fraction
    #      weighted by sqrt(n/31). The Gaussian dominates in the tails
    #      where the count undersamples; the count dominates near the mean.
    # ------------------------------------------------------------------

    def probability_high_above(self, threshold_f: float) -> float:
        """P(daily_high > threshold_f) via Gaussian CDF + empirical blend."""
        if not self.member_highs:
            return 0.5
        p_gauss = float(1.0 - _norm.cdf(threshold_f, loc=self.effective_mean_high(), scale=self._eff_std_high()))
        n = len(self.member_highs)
        if n >= 5:
            p_count = sum(1 for h in self.member_highs if h > threshold_f) / n
            w = min((n / 31.0) ** 0.5, 1.0)
            return w * p_count + (1.0 - w) * p_gauss
        return p_gauss

    def probability_high_below(self, threshold_f: float) -> float:
        """P(daily_high < threshold_f)."""
        return 1.0 - self.probability_high_above(threshold_f)

    def probability_low_above(self, threshold_f: float) -> float:
        """P(daily_low > threshold_f) via Gaussian CDF + empirical blend."""
        if not self.member_lows:
            return 0.5
        p_gauss = float(1.0 - _norm.cdf(threshold_f, loc=self.mean_low, scale=self._eff_std_low()))  # low uses GFS mean directly
        n = len(self.member_lows)
        if n >= 5:
            p_count = sum(1 for lo in self.member_lows if lo > threshold_f) / n
            w = min((n / 31.0) ** 0.5, 1.0)
            return w * p_count + (1.0 - w) * p_gauss
        return p_gauss

    def probability_low_below(self, threshold_f: float) -> float:
        """P(daily_low < threshold_f)."""
        return 1.0 - self.probability_low_above(threshold_f)

    def probability_high_between(self, floor_f: float, cap_f: float) -> float:
        """P(floor_f <= daily_high <= cap_f) — narrow Kalshi bracket contracts."""
        return max(0.0, self.probability_high_above(floor_f) - self.probability_high_above(cap_f))

    def probability_low_between(self, floor_f: float, cap_f: float) -> float:
        """P(floor_f <= daily_low <= cap_f) — narrow Kalshi bracket contracts."""
        return max(0.0, self.probability_low_above(floor_f) - self.probability_low_above(cap_f))

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


async def fetch_station_high_water_mark(
    city_key: str,
    target_date: date,
) -> Optional[float]:
    """
    Return the highest observed temperature so far for city_key on target_date.

    Wraps fetch_nws_observed_temperature with a 300-second TTL cache so that
    the exit-evaluation job (also running every 300s) pays at most one NWS
    request per city per 5-minute window.  None is never cached — a station
    outage allows an immediate retry on the next scheduler tick.
    """
    cache_key = f"nws_hwm_{city_key}_{target_date.isoformat()}"
    now = time.time()
    if cache_key in _nws_obs_cache:
        cached_val, cached_ts = _nws_obs_cache[cache_key]
        if now - cached_ts < _NWS_OBS_CACHE_TTL:
            return cached_val

    obs = await fetch_nws_observed_temperature(city_key, target_date)
    if obs is None:
        return None

    high_f = obs.get("high")
    if high_f is not None:
        _nws_obs_cache[cache_key] = (high_f, now)
    return high_f
