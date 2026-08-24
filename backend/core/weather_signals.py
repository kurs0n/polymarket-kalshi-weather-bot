"""Signal generator for weather temperature markets using ensemble forecasts."""
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from backend.config import settings
from backend.core.sizing import calculate_edge, calculate_kelly_size
from backend.data.weather import (
    fetch_ensemble_forecast, EnsembleForecast, CITY_CONFIG,
    TEMP_UNCERTAINTY_FLOOR_F, fetch_station_high_water_mark,
)
from backend.data.kalshi_markets import CITY_SERIES, LOW_SERIES, _parse_kalshi_ticker
from backend.data.weather_markets import WeatherMarket, fetch_polymarket_weather_markets
from backend.models.database import SessionLocal, Signal, Trade, BotState

logger = logging.getLogger("trading_bot")

# Signals where the ensemble has fewer than this many members get a confidence
# penalty and a warning. Below 3 we only have the Gaussian approximation.
MIN_RELIABLE_MEMBERS = 5

EXIT_BUFFER_F = 2.0  # observed must exceed threshold by this margin to trigger early exit

# Trend-stop confluence — added 2026-08-17 on user request ("take into
# account both the metar and the market sentiment, if they align we go for
# that"). A live METAR trend reading is a single, sometimes-noisy source —
# it can wobble between observations without real new information. Before
# firing an early trend-based exit, the LIVE MARKET PRICE for our side must
# also have dropped at least this fraction below our entry price — i.e. the
# crowd's own pricing has to independently agree the position has weakened,
# not just our own trend projection. If only one source signals trouble,
# hold rather than act on noise.
TREND_STOP_MARKET_CONFIRM_PCT = 0.15
_SERIES_TO_CITY: Dict[str, str] = {}

# Minimum confidence (z-score-derived, see generate_weather_signal) required to
# trade at all. Added 2026-08-14 after reviewing that day's live trades: every
# one of the 29 historically-settled trades that actually built this bot's
# track record was a high-z, high-confidence tail bet (e.g. "won't hit 99F").
# There is zero settled history for the near-mean, low-confidence pattern —
# that day's NY/LA/Miami/Boston trades (confidence 54%/25%/10%/4%) — so we
# stop trading that pattern until it earns a track record of its own.
MIN_CONFIDENCE_THRESHOLD = 0.30

# Market-implied consensus check — added 2026-08-16. market_implied_mean is
# the price-weighted midpoint across every "between" bracket Kalshi lists for
# a city/date (see scan_for_weather_signals), i.e. what the whole order book
# collectively implies the high/low will be, independent of our own model.
# Below SOFT_F the two are considered in agreement (no penalty). Between SOFT
# and HARD, confidence is scaled down linearly — the market may just be slow
# to reprice. At or beyond HARD_F the signal is blocked outright: a forecast
# pipeline (GFS/HRRR/ECMWF/NWS blend + bias correction) disagreeing with the
# whole market's aggregate view by that much is more often a sign our own
# pipeline is wrong than evidence of a real edge (same rationale as the
# consensus-bracket guardrail, applied continuously instead of per-bracket).
MARKET_DIVERGENCE_SOFT_F = 4.0
MARKET_DIVERGENCE_HARD_F = 7.0

# Cache of the most recent scan_for_weather_signals() result. Written only by
# scan_for_weather_signals() itself (i.e. the scheduled 300s trading job, or a
# manual scan). Read-only callers — notably GET /api/dashboard, which the
# frontend polls every 10s — should read this cache via
# get_cached_weather_signals() instead of triggering their own live market
# scan (Root-caused 2026-08-14: the dashboard endpoint was calling
# scan_for_weather_signals() directly on every poll, turning a 300s-interval
# job into a live Kalshi/Polymarket scan every ~10s per open tab).
_signals_cache: Dict[str, object] = {"signals": [], "timestamp": None}


def get_cached_weather_signals() -> List["WeatherTradingSignal"]:
    """Return the most recently computed weather signals without hitting any live APIs."""
    return _signals_cache["signals"]


@dataclass
class WeatherTradingSignal:
    """A trading signal for a weather temperature market."""
    market: WeatherMarket

    # Core signal data
    model_probability: float = 0.5   # Ensemble probability of YES outcome
    market_probability: float = 0.5  # Market's implied YES probability
    edge: float = 0.0
    direction: str = "yes"           # "yes" or "no"

    # Confidence and sizing
    confidence: float = 0.5
    kelly_fraction: float = 0.0
    suggested_size: float = 0.0

    # Metadata
    sources: List[str] = field(default_factory=list)
    reasoning: str = ""
    timestamp: datetime = field(default_factory=datetime.utcnow)

    # Forecast context
    ensemble_mean: float = 0.0
    ensemble_std: float = 0.0
    ensemble_members: int = 0
    limit_price: float = 0.0   # computed resting maker bid (0–1)

    @property
    def entry_price(self) -> float:
        """Price actually paid per contract for the side being traded — market_probability
        is always P(YES), so a "no" trade's real entry price is its complement."""
        return self.market_probability if self.direction == "yes" else (1 - self.market_probability)

    @property
    def passes_threshold(self) -> bool:
        """
        Check if signal passes minimum edge threshold.

        Root-caused 2026-08-21: expensive "favorite" entries (>=WEATHER_HIGH_PRICE_THRESHOLD)
        need a bigger edge cushion — see WEATHER_HIGH_PRICE_MIN_EDGE's full
        rationale in config.py. This does not touch model_probability itself.
        """
        threshold = settings.WEATHER_MIN_EDGE_THRESHOLD
        if self.entry_price >= settings.WEATHER_HIGH_PRICE_THRESHOLD:
            threshold = settings.WEATHER_HIGH_PRICE_MIN_EDGE
        return abs(self.edge) >= threshold


def _calculate_limit_price(market: WeatherMarket, direction: str) -> float:
    """
    Compute resting maker limit price: best_bid + 1¢ if still inside the spread,
    otherwise fall back to best_bid (avoids crossing and becoming a taker).
    """
    if direction == "yes":
        candidate = round(market.yes_bid + 0.01, 2)
        return candidate if candidate < market.yes_price else market.yes_bid
    else:
        candidate = round(market.no_bid + 0.01, 2)
        return candidate if candidate < market.no_price else market.no_bid


def _read_live_bankroll() -> float:
    """Read current bankroll from BotState, falling back to config default."""
    try:
        from backend.models.database import BotState
        db = SessionLocal()
        try:
            state = db.query(BotState).first()
            if state and state.bankroll > 0:
                return float(state.bankroll)
        finally:
            db.close()
    except Exception:
        pass
    return float(settings.INITIAL_BANKROLL)


async def generate_weather_signal(
    market: WeatherMarket, is_consensus_bracket: bool = False,
    market_implied_mean: Optional[float] = None,
) -> Optional[WeatherTradingSignal]:
    """
    Generate a trading signal for a weather temperature market.

    Probability pipeline:
      1. Reject markets without a confirmed live order book (has_live_book=False)
         or with a bid-ask spread wider than the threshold.
      2. Fetch 31-member GFS ensemble (hourly → daily max/min per member).
      3. Compute P(YES) via Student-t CDF (fat tails; see TAIL_DIST_DF) using
         ensemble mean ± max(std, 3°F floor), the std additionally widened
         by cross-model disagreement (see _model_disagreement_std_high).
         When ≥5 members are available, blend count fraction in by sqrt(n/31).
      4. Soft-clip to [0.02, 0.98] — wide enough to let tail signals through
         without the old [0.05, 0.95] compression that fabricated ±45% edges.
      5. Compare to market price → edge → fractional Kelly size.
    """
    # ------------------------------------------------------------------
    # Order book guard — must run before any probability computation.
    #
    # Kalshi markets without a confirmed live book (has_live_book=False)
    # have already been rejected at fetch time, so this catches Polymarket
    # markets (which don't set has_live_book) and any future data sources
    # that might slip through without a spread check.
    # ------------------------------------------------------------------
    from backend.data.kalshi_markets import MAX_BID_ASK_SPREAD_CENTS
    MAX_SPREAD = MAX_BID_ASK_SPREAD_CENTS / 100.0

    if market.platform == "kalshi":
        if not market.has_live_book:
            logger.debug(f"Skipping {market.market_id}: no live order book confirmed")
            return None
        if market.bid_ask_spread > MAX_SPREAD:
            logger.debug(
                f"Skipping {market.market_id}: spread {market.bid_ask_spread:.2f} "
                f"> {MAX_SPREAD:.2f} limit"
            )
            return None

    forecast = await fetch_ensemble_forecast(market.city_key, market.target_date)
    if not forecast or not forecast.member_highs:
        return None

    # ------------------------------------------------------------------
    # Degenerate ensemble guard
    # ------------------------------------------------------------------
    if forecast.num_members < MIN_RELIABLE_MEMBERS:
        logger.warning(
            f"Skipping {market.title}: only {forecast.num_members} ensemble member(s) "
            f"— probability estimate unreliable, signal suppressed."
        )
        # Still return a signal with edge=0 so the UI can show the forecast;
        # it won't pass passes_threshold so no trade will be placed.
        return WeatherTradingSignal(
            market=market,
            model_probability=0.5,
            market_probability=market.yes_price,
            edge=0.0,
            direction="yes",
            confidence=0.0,
            sources=[f"open_meteo_ensemble_{forecast.num_members}m_DEGENERATE"],
            reasoning=f"[SUPPRESSED] Only {forecast.num_members} ensemble member(s); "
                      f"need ≥{MIN_RELIABLE_MEMBERS} for reliable probability estimate.",
            ensemble_mean=forecast.mean_high,
            ensemble_std=forecast.std_high,
            ensemble_members=forecast.num_members,
        )

    # ------------------------------------------------------------------
    # Dynamic confidence gate — two tracks, added 2026-08-14.
    #
    # Track 1: above/below contracts (unchanged). Reject contracts where
    # the strike is within 2.0σ of the ensemble mean. Contracts this close
    # to the mean sit inside normal forecast variance — the tail CDF
    # produces noise-level output near its centre and any apparent edge
    # is spurious. This reasoning fits a simple above/below bet, where
    # "near the mean" means "near 50/50" for BOTH the model and the market.
    #
    # Track 2: "between" (narrow 1°F bracket) contracts. The above
    # reasoning does not transfer: a bracket sitting AT the mean is where
    # our estimate is most stable (small forecast error → small probability
    # error), while a bracket far in the tail is where a small error in the
    # mean swings the probability the most. Gating "between" contracts on
    # distance-from-mean would reject exactly the cases where the model
    # has its most trustworthy opinion — including cases where the market
    # is plausibly mispriced on the CONSENSUS bracket. So no z-gate here;
    # instead we require the raw (pre-clip) probability to show genuine
    # differentiation (not pinned at the clip floor/ceiling, which means
    # the model has no real signal left to offer, just a saturated output).
    # ------------------------------------------------------------------
    # Root-caused 2026-08-17: this used raw forecast.mean_high (GFS ensemble
    # only) for confidence/z-score/display, while the actual probability
    # calc below (probability_high_between etc.) already called
    # effective_mean_high() — the HRRR/ECMWF/NWS-blended, bias-corrected
    # value. That meant confidence and the "Ensemble: XX.XF" reasoning text
    # didn't reflect the number actually driving the trade.
    mean_val = forecast.effective_mean_high() if market.metric == "high" else forecast.mean_low
    raw_std  = forecast.std_high  if market.metric == "high" else forecast.std_low
    eff_std  = max(raw_std, TEMP_UNCERTAINTY_FLOOR_F)
    z_score  = abs(mean_val - market.threshold_f) / eff_std

    # ------------------------------------------------------------------
    # Market-implied consensus hard block — see MARKET_DIVERGENCE_* above.
    # Checked early, before any probability computation, since a large
    # divergence invalidates the whole signal regardless of direction.
    # ------------------------------------------------------------------
    market_divergence_f = None
    if market_implied_mean is not None:
        market_divergence_f = abs(mean_val - market_implied_mean)
        if market_divergence_f >= MARKET_DIVERGENCE_HARD_F:
            logger.info(
                f"[FILTERED] {market.market_id}: model mean ({mean_val:.1f}F) diverges "
                f"{market_divergence_f:.1f}F from the market-implied consensus "
                f"({market_implied_mean:.1f}F) — beyond the {MARKET_DIVERGENCE_HARD_F:.1f}F "
                f"hard-block threshold, our forecast pipeline is more likely missing "
                f"something than the whole market is wrong."
            )
            return None

    if market.direction != "between":
        if z_score < 2.0:
            logger.debug(
                f"[FILTERED] {market.market_id}: z_score ({z_score:.2f}) < 2.0 threshold "
                f"(mean={mean_val:.1f}°F, strike={market.threshold_f}°F, eff_std={eff_std:.1f}°F)"
            )
            return None

    # ------------------------------------------------------------------
    # Probability estimation (Student-t CDF + count blend in EnsembleForecast)
    # ------------------------------------------------------------------
    if market.metric == "high":
        if market.direction == "above":
            model_yes_prob = forecast.probability_high_above(market.threshold_f)
        elif market.direction == "below":
            model_yes_prob = forecast.probability_high_below(market.threshold_f)
        else:  # "between" — narrow Kalshi bracket, not a cumulative threshold
            model_yes_prob = forecast.probability_high_between(market.floor_f, market.cap_f)
    else:  # "low"
        if market.direction == "above":
            model_yes_prob = forecast.probability_low_above(market.threshold_f)
        elif market.direction == "below":
            model_yes_prob = forecast.probability_low_below(market.threshold_f)
        else:  # "between"
            model_yes_prob = forecast.probability_low_between(market.floor_f, market.cap_f)

    if market.direction == "between" and (model_yes_prob < 0.03 or model_yes_prob > 0.97):
        logger.debug(
            f"[FILTERED] {market.market_id}: raw model_p ({model_yes_prob:.1%}) is saturated "
            f"at the clip boundary — no genuine differentiation, not trustworthy for a "
            f"deep-tail bracket bet."
        )
        return None

    # ------------------------------------------------------------------
    # Soft clip [0.02, 0.98].
    #
    # The original [0.05, 0.95] was too tight: when gfs_seamless returned
    # only 1 member, every probability was 0.0 or 1.0, clipped to 0.05/0.95,
    # producing a fake ±45% edge against 50¢ markets and a Brier score of 0.90.
    #
    # With the Student-t CDF and 3°F floor in EnsembleForecast, extreme
    # outputs (>0.98 or <0.02) now only occur when the forecast genuinely
    # leaves the threshold in the far tail (>2.5 std away). Allowing those
    # through is correct; we just cap at 0.02/0.98 to preserve a minimum
    # uncertainty margin.
    # ------------------------------------------------------------------
    model_yes_prob = max(0.02, min(0.98, model_yes_prob))

    # Warn when the Gaussian is still producing near-extreme outputs even
    # after the fix (could indicate a data problem upstream).
    if model_yes_prob >= 0.90 or model_yes_prob <= 0.10:
        logger.warning(
            f"[EXTREME PROB] {market.city_name} {market.metric} "
            f"{market.direction} {market.threshold_f:.1f}F: "
            f"model={model_yes_prob:.1%}, z={z_score:.1f}σ, "
            f"mean={mean_val:.1f}F, eff_std={eff_std:.1f}F, "
            f"n={forecast.num_members}"
        )

    market_yes_prob = market.yes_price

    # ------------------------------------------------------------------
    # Edge and direction
    # ------------------------------------------------------------------
    edge, direction_raw = calculate_edge(model_yes_prob, market_yes_prob)
    direction = "yes" if direction_raw == "up" else "no"

    # ------------------------------------------------------------------
    # Empirical tail calibration — see backend/core/calibration.py.
    #
    # Shrinks model_yes_prob toward the bot's own observed hit rate in
    # well-populated extreme win-probability buckets (built from settled
    # weather trades) before edge/confidence/Kelly sizing are finalized, so
    # sizing doesn't run off a raw Gaussian tail the live trade history
    # says is overconfident (root-caused 2026-08-17: the 95-100%
    # model-win-probability bucket, n=963 settled trades, has an actual
    # 77.7% hit rate). No-op (returns model_yes_prob unchanged) until a
    # bucket has at least MIN_BUCKET_SAMPLES settled trades, so this has no
    # effect until it has earned the right to.
    # ------------------------------------------------------------------
    from backend.core.calibration import calibrate_probability
    calibrated_prob = await calibrate_probability(model_yes_prob, direction)
    if calibrated_prob != model_yes_prob:
        model_yes_prob = calibrated_prob
        edge, direction_raw = calculate_edge(model_yes_prob, market_yes_prob)
        direction = "yes" if direction_raw == "up" else "no"

    # ------------------------------------------------------------------
    # Consensus-bracket guardrail (between-contracts only).
    #
    # is_consensus_bracket means this specific bracket is the city's own
    # highest-24h-volume "between" contract — i.e. the crowd's favorite,
    # most heavily traded pick for that day. Betting NO against that
    # specific bracket means claiming a liquid, well-traded market is
    # badly wrong on its single most-agreed-upon view. Root-caused
    # 2026-08-14 (Miami B93.5: 74% market conviction, 8.5k contracts
    # volume, model said 2%) — that kind of gap is more often evidence
    # our model is missing something than evidence of a real edge.
    # Betting YES on the consensus bracket (agreeing with the crowd) is
    # unaffected; only the "fight the crowd's favorite" direction is blocked.
    # ------------------------------------------------------------------
    if market.direction == "between" and is_consensus_bracket and direction == "no":
        logger.info(
            f"[FILTERED] {market.market_id}: would bet NO against this city's own "
            f"highest-volume consensus bracket (model={model_yes_prob:.1%}) — "
            f"blocked by consensus-bracket guardrail, not enough independent "
            f"evidence to fight a liquid market's top pick."
        )
        return None

    # Entry price filter
    entry_price = market.yes_price if direction == "yes" else market.no_price
    if entry_price > settings.WEATHER_MAX_ENTRY_PRICE:
        edge = 0.0  # Zero out but still return for UI visibility

    # ------------------------------------------------------------------
    # Confidence: based on z-score distance of threshold from forecast mean.
    #
    # A threshold very close to the forecast mean (z ≈ 0) is genuinely
    # uncertain — the market might be well-priced. A threshold far from
    # the mean (z > 2) carries real informational edge.
    #
    # We scale confidence by min(z/2, 1.0) so it rises from 0 at the mean
    # to 1.0 at 2+ standard deviations, then cap at 0.90.
    #
    # If the ensemble is thin (<MIN_RELIABLE_MEMBERS), penalty applied.
    # ------------------------------------------------------------------
    confidence_base = min(z_score / 2.0, 1.0)

    # Thin-ensemble penalty: linearly reduce confidence below MIN_RELIABLE_MEMBERS
    member_factor = min(forecast.num_members / MIN_RELIABLE_MEMBERS, 1.0)
    confidence = min(0.90, confidence_base * member_factor)

    # Market-implied consensus soft penalty: linearly derate confidence as
    # divergence grows from SOFT_F to HARD_F (already hard-blocked above
    # HARD_F). At SOFT_F or below, no penalty — model and market agree.
    divergence_factor = 1.0
    if market_divergence_f is not None and market_divergence_f > MARKET_DIVERGENCE_SOFT_F:
        span = MARKET_DIVERGENCE_HARD_F - MARKET_DIVERGENCE_SOFT_F
        divergence_factor = max(
            0.0, 1.0 - (market_divergence_f - MARKET_DIVERGENCE_SOFT_F) / span
        )
        confidence *= divergence_factor

    if confidence < MIN_CONFIDENCE_THRESHOLD:
        logger.info(
            f"[FILTERED] {market.market_id}: confidence {confidence:.0%} < "
            f"{MIN_CONFIDENCE_THRESHOLD:.0%} minimum — bracket sits too close to the "
            f"forecast mean (z={z_score:.1f}σ) to trust the edge; this is the "
            f"untested near-mean pattern, not the proven tail-bet one."
        )
        return None

    # ------------------------------------------------------------------
    # Kelly sizing using live bankroll from BotState
    # ------------------------------------------------------------------
    bankroll = _read_live_bankroll()
    suggested_size = calculate_kelly_size(
        edge=abs(edge),
        probability=model_yes_prob,
        market_price=market_yes_prob,
        direction=direction_raw,
        bankroll=bankroll,
    )
    # No WEATHER_MAX_TRADE_SIZE clamp here — calculate_kelly_size's own
    # KELLY_MAX_TRADE_FRACTION ceiling already bounds this as a % of
    # bankroll. A flat-dollar clamp here was silently overriding it (every
    # signal was hitting the same $100 ceiling regardless of edge, and
    # kelly_fraction below — computed from this post-clamp number — was
    # reporting that flat cap divided by bankroll instead of Kelly's actual
    # output). See sizing.py's calculate_kelly_size for the fuller note.

    limit_price = _calculate_limit_price(market, direction)

    # raw_std (unflored) used for display; eff_std (floored) used for probability
    std_val = raw_std

    # ------------------------------------------------------------------
    # Reasoning string
    # ------------------------------------------------------------------
    filter_status = "ACTIONABLE" if abs(edge) >= settings.WEATHER_MIN_EDGE_THRESHOLD else "FILTERED"
    filter_notes = []
    if entry_price > settings.WEATHER_MAX_ENTRY_PRICE:
        filter_notes.append(f"entry {entry_price:.0%} > {settings.WEATHER_MAX_ENTRY_PRICE:.0%}")
    if forecast.num_members < MIN_RELIABLE_MEMBERS:
        filter_notes.append(f"thin_ensemble({forecast.num_members}m)")
    if market_divergence_f is not None and divergence_factor < 1.0:
        filter_notes.append(
            f"market_divergence({market_divergence_f:.1f}F, x{divergence_factor:.2f})"
        )
    filter_note = f" [{', '.join(filter_notes)}]" if filter_notes else ""

    market_implied_str = (
        f" | Market-implied: {market_implied_mean:.1f}F (Δ{market_divergence_f:.1f}F)"
        if market_implied_mean is not None else ""
    )

    reasoning = (
        f"[{filter_status}]{filter_note} "
        f"{market.city_name} {market.metric} {market.direction} {market.threshold_f:.0f}F "
        f"on {market.target_date} | "
        f"Ensemble [{forecast.num_members}m]: {mean_val:.1f}F ±{std_val:.1f}F "
        f"(floor={TEMP_UNCERTAINTY_FLOOR_F:.1f}F, z={z_score:.1f}σ){market_implied_str} | "
        f"Model YES: {model_yes_prob:.0%} vs Market: {market_yes_prob:.0%} | "
        f"Edge: {edge:+.1%} → {direction.upper()} @ {entry_price:.0%} | "
        f"Confidence: {confidence:.0%}"
    )

    return WeatherTradingSignal(
        market=market,
        model_probability=model_yes_prob,
        market_probability=market_yes_prob,
        edge=edge,
        direction=direction,
        confidence=confidence,
        kelly_fraction=suggested_size / bankroll if bankroll > 0 else 0,
        suggested_size=suggested_size,
        sources=[f"open_meteo_gfs025_{forecast.num_members}m"],
        reasoning=reasoning,
        ensemble_mean=mean_val,
        ensemble_std=std_val,
        ensemble_members=forecast.num_members,
        limit_price=limit_price,
    )


def _get_series_to_city() -> Dict[str, str]:
    """
    Series ticker -> city_key, for matching a bare ticker's prefix back to
    a city. Root-caused 2026-08-23: this only ever included CITY_SERIES
    (high-temp) — a low-temp ticker wouldn't match any prefix here at all,
    so _resolve_city_from_ticker() would silently return None for every
    low-temp position, meaning none of the METAR-based exit/kill-switch
    checks that depend on it could ever find a low-temp trade to evaluate.
    Now includes LOW_SERIES too (only relevant once WEATHER_LOW_TEMP_ENABLED
    is on, but resolving correctly doesn't depend on the flag — it should
    just always be correct).
    """
    global _SERIES_TO_CITY
    if not _SERIES_TO_CITY:
        _SERIES_TO_CITY = {v: k for k, v in CITY_SERIES.items()}
        _SERIES_TO_CITY.update({v: k for k, v in LOW_SERIES.items()})
    return _SERIES_TO_CITY


async def _resolve_city_from_ticker(ticker: str, client=None) -> Optional[tuple]:
    """
    Return (city_key, parsed_dict) by matching the series prefix.

    Direction/threshold come from Kalshi's live market metadata (strike_type)
    when `client` is provided — required for correctness, since the ticker's
    B/T prefix alone does not reliably indicate direction (see
    _parse_kalshi_ticker docstring). Without a client, falls back to a
    ticker-only parse whose direction is unknown (None) — callers doing
    exit/kill-switch comparisons should always pass a client.
    """
    for series_prefix, city_key in _get_series_to_city().items():
        if ticker.startswith(series_prefix):
            market_meta = None
            if client is not None:
                try:
                    resp = await client.get_market(ticker)
                    market_meta = resp.get("market")
                except Exception as e:
                    logger.warning(f"_resolve_city_from_ticker: get_market({ticker}) failed: {e}")
            parsed = _parse_kalshi_ticker(market_meta if market_meta else ticker, city_key)
            if parsed is not None:
                return city_key, parsed
    return None


def _evaluate_between_bracket_outcome(
    city_key: str, floor_f: float, cap_f: float, corrected_high: float
) -> Optional[str]:
    """
    Determine whether a "between floor and cap" bracket's outcome is already
    decided from the day's observed high-water-mark, or still genuinely open.

    Root-caused 2026-08-16: two other METAR-based checks in this codebase
    (check_pending_orders_job's Phase B sweep, position_liquidator_job's
    exit condition 2) only ever handled simple above/below contracts and
    silently skipped "between" brackets — which is most of what this bot
    actually trades. This is the shared logic all three now use.

    Ceiling side is safe to check at any time of day: once the high-water-
    mark has already cleared the cap (+ buffer), the day's final high can
    only be >= that reading, so the band is permanently unreachable.

    Floor side needs the diurnal peak to have already passed before it's
    safe to conclude anything — a high-water-mark below the floor mid-day
    doesn't mean the band is out of reach, since the day's peak may still
    be ahead. Only once the assumed peak hour has passed does "hasn't
    reached the floor yet" become "won't reach the floor at all today."

    Returns "yes_wins" (final high lands in the band), "no_wins" (it won't),
    or None if still genuinely undetermined.
    """
    if corrected_high >= cap_f + EXIT_BUFFER_F:
        return "no_wins"  # already blown past the ceiling — band unreachable

    from backend.core.execution import _hours_until_diurnal_peak
    past_peak = _hours_until_diurnal_peak(city_key) <= 0.0
    if not past_peak:
        return None  # floor side is still genuinely open until peak passes

    if corrected_high <= floor_f - EXIT_BUFFER_F:
        return "no_wins"  # peak already happened, never reached the floor
    if floor_f + EXIT_BUFFER_F <= corrected_high <= cap_f - EXIT_BUFFER_F:
        return "yes_wins"  # peak already happened, landed inside the band

    return None  # inside the buffer zone at either edge — stay undetermined


async def evaluate_open_positions_for_exit(db, client=None) -> List[dict]:
    """
    Scan unsettled Kalshi weather trades and identify those to close early.

    Returns a list of exit-recommendation dicts, each tagged "exit_type":
      - "settlement": outcome is already DECIDED by current NWS observations
        (a 2°F buffer past the threshold — EXIT_BUFFER_F — required before
        triggering, to avoid acting on noisy near-threshold readings). The
        caller settles the trade for real (settlement_value 1.0/0.0).
      - "trend_stop": outcome is NOT yet decided, but the live METAR warming
        trend, projected to the day's diurnal peak, now points at a breach.
        This is a PROJECTION, not a certainty — the caller should sell at
        the current market price (a stop-loss/take-profit), not force-settle
        the trade as a win/loss. Added 2026-08-17: this used to be an ENTRY
        guardrail (see execution.py's 2026-08-17 note) that blocked opening
        a position at all outside 11h-14h local. Moved here on user
        feedback — entering early on a real edge is the point; the trend
        projection is far more useful as a live "dump it, the trajectory
        just turned against us" monitor on an OPEN position.

    `client` (a KalshiClient) is passed through to _resolve_city_from_ticker
    so direction comes from Kalshi's authoritative strike_type field rather
    than an unreliable ticker-only guess. "between"-bracket positions use
    _evaluate_between_bracket_outcome (added 2026-08-16) for the settlement
    check; trend_stop only applies to simple above/below contracts (a
    "between" position's risk is already handled by the high-water-mark
    logic in _evaluate_between_bracket_outcome).
    """
    from backend.data.weather import fetch_metar_current, fetch_station_low_water_mark
    from backend.core.execution import _hours_until_diurnal_peak
    from backend.data.kalshi_markets import _extract_prices_from_orderbook

    open_trades = (
        db.query(Trade)
        .filter(
            Trade.settled == False,
            Trade.market_type == "weather",
            Trade.platform == "kalshi",
        )
        .all()
    )

    results = []
    for trade in open_trades:
        resolved = await _resolve_city_from_ticker(trade.market_ticker, client)
        if resolved is None:
            continue

        city_key, parsed = resolved
        metric = parsed.get("metric", "high")
        # Root-caused 2026-08-23: this used to always read the day's HIGH
        # water mark regardless of the market's metric — for a low-temp
        # contract that's comparing the wrong physical quantity entirely
        # (a day's high tells you nothing about whether its low breached a
        # threshold). Note station_bias_f (below) was only ever measured
        # against high-temp readings; reused here for lows too as the best
        # available estimate, not a verified low-specific correction.
        if metric == "low":
            observed = await fetch_station_low_water_mark(city_key, parsed["target_date"])
        else:
            observed = await fetch_station_high_water_mark(city_key, parsed["target_date"])
        if observed is None:
            continue

        bias = CITY_CONFIG.get(city_key, {}).get("station_bias_f", 0.0)
        corrected = observed + bias
        threshold = parsed["threshold_f"]
        direction = parsed["direction"]

        if direction == "above":
            if corrected > threshold + EXIT_BUFFER_F:
                outcome = "yes_wins"
            elif corrected < threshold - EXIT_BUFFER_F:
                outcome = "no_wins"
            else:
                outcome = None
        elif direction == "below":
            if corrected < threshold - EXIT_BUFFER_F:
                outcome = "yes_wins"
            elif corrected > threshold + EXIT_BUFFER_F:
                outcome = "no_wins"
            else:
                outcome = None
        elif direction == "between":
            floor_f = parsed.get("floor_f")
            cap_f = parsed.get("cap_f")
            if floor_f is None or cap_f is None:
                continue
            outcome = _evaluate_between_bracket_outcome(city_key, floor_f, cap_f, corrected)
        else:
            continue

        if outcome is not None:
            trade_wins = (
                (outcome == "yes_wins" and trade.direction == "yes") or
                (outcome == "no_wins"  and trade.direction == "no")
            )
            results.append({
                "trade_id":       trade.id,
                "exit_type":      "settlement",
                "outcome":        outcome,
                "trade_wins":     trade_wins,
                # Key names kept as "*_high" for backward compatibility with
                # any existing consumer — holds the LOW water mark instead
                # when metric == "low" (see `metric` above).
                "observed_high":  observed,
                "corrected_high": corrected,
                "threshold_f":    threshold,
                "direction":      direction,
                "reason": (
                    f"NWS corrected {corrected:.1f}°F vs threshold {threshold:.1f}°F "
                    f"({direction}) — buffer {EXIT_BUFFER_F}°F exceeded"
                ),
            })
            continue

        # ── Not yet decided — check the live trend projection ────────────
        # (above/below only; "between" already handled by the high-water-
        # mark outcome check above, which returned None = "not yet decided,
        # nothing more to do this cycle" for that contract type.)
        #
        # Gated behind WEATHER_EARLY_EXITS_ENABLED — see its docstring in
        # config.py. This is a PROJECTION-based exit (sells at current
        # market price on a guess about where the day ends up), unlike the
        # settlement branch above (which only fires once the real outcome
        # is already locked in) — exactly the kind of early exit that was
        # clipping winners down to ~10% of their real payout. Disabled for
        # testing means every above/below position rides to a real decided
        # outcome instead.
        if not settings.WEATHER_EARLY_EXITS_ENABLED:
            continue
        if direction not in ("above", "below") or client is None:
            continue
        # 2026-08-23, low-temp Part 2: deliberately NOT extended to low-temp
        # contracts. This whole projection assumes a WARMING trend heading
        # toward the day's diurnal PEAK — right physics for a high-temp
        # bracket, wrong for a low-temp one (a low is typically set near
        # dawn/overnight, and the relevant risk is a COOLING trend toward
        # the day's trough, an entirely different projection this code
        # doesn't compute). Building that properly is its own piece of
        # work, not a find-and-replace of "peak" with "trough" — low-temp
        # positions still get the unconditional settlement check above
        # (physically-decided outcomes), just not this live projection.
        if metric == "low":
            continue

        betting_high_stays_below = (
            (trade.direction == "yes" and direction == "below") or
            (trade.direction == "no"  and direction == "above")
        )
        if not betting_high_stays_below:
            continue

        metar = await fetch_metar_current(city_key)
        if metar is None:
            continue

        hours_to_peak = _hours_until_diurnal_peak(city_key)
        if metar.trend_f_per_hour <= 0 or hours_to_peak <= 0:
            continue

        projected_peak = metar.observed_temp_f + hours_to_peak * metar.trend_f_per_hour
        if projected_peak <= threshold:
            continue

        # ── Confluence check: METAR alone doesn't fire an exit — the live
        # market price for our side must ALSO have moved against us. See
        # TREND_STOP_MARKET_CONFIRM_PCT above for the rationale.
        try:
            ob_data = await client.get_orderbook(trade.market_ticker)
            prices, _ = _extract_prices_from_orderbook(ob_data)
        except Exception:
            prices = None

        current_price = None
        price_drop_pct = 0.0
        if prices is not None:
            current_price = prices["yes_bid"] if trade.direction == "yes" else prices["no_bid"]
            if trade.entry_price and trade.entry_price > 0:
                price_drop_pct = (trade.entry_price - current_price) / trade.entry_price

        if price_drop_pct < TREND_STOP_MARKET_CONFIRM_PCT:
            logger.debug(
                f"TREND_WATCH (not acted, single-source): {trade.market_ticker} METAR "
                f"projects a breach ({projected_peak:.1f}F vs {threshold:.1f}F ceiling) "
                f"but market price hasn't confirmed (current={current_price}, "
                f"entry={trade.entry_price}, drop={price_drop_pct:.0%} < "
                f"{TREND_STOP_MARKET_CONFIRM_PCT:.0%} needed) — holding."
            )
            continue

        results.append({
            "trade_id": trade.id,
            "exit_type": "trend_stop",
            "direction": direction,
            "threshold_f": threshold,
            "projected_peak": projected_peak,
            "reason": (
                f"TREND_STOP: {metar.station} projected peak {projected_peak:.1f}°F "
                f"({metar.observed_temp_f:.1f}°F + {hours_to_peak:.1f}h × "
                f"{metar.trend_f_per_hour:+.2f}°F/hr) will exceed bracket ceiling "
                f"{threshold:.1f}°F, AND market price confirms (current {current_price:.2f} "
                f"vs entry {trade.entry_price:.2f}, -{price_drop_pct:.0%}) — METAR and "
                f"market sentiment agree, selling now while there's still a bid rather "
                f"than waiting for certainty."
            ),
        })

    return results


async def scan_for_weather_signals() -> List[WeatherTradingSignal]:
    """Scan weather markets and generate ensemble-based signals."""
    signals = []

    # dict.fromkeys (not set()) preserves the configured order while still
    # deduping — a repeated entry in WEATHER_CITIES (e.g. a copy-paste typo)
    # would otherwise scan and trade that city twice per cycle.
    city_keys = list(dict.fromkeys(c.strip() for c in settings.WEATHER_CITIES.split(",") if c.strip()))

    logger.info("=" * 50)
    logger.info("WEATHER SCAN: Fetching temperature markets...")

    markets = []

    # Polymarket
    try:
        poly_markets = await fetch_polymarket_weather_markets(city_keys)
        markets.extend(poly_markets)
        logger.info(f"Polymarket: {len(poly_markets)} weather markets")
    except Exception as e:
        logger.error(f"Failed to fetch Polymarket weather markets: {e}")

    # Kalshi
    if settings.KALSHI_ENABLED:
        try:
            from backend.data.kalshi_client import kalshi_credentials_present
            from backend.data.kalshi_markets import fetch_kalshi_weather_markets
            if kalshi_credentials_present():
                kalshi_markets = await fetch_kalshi_weather_markets(city_keys)
                markets.extend(kalshi_markets)
                logger.info(f"Kalshi: {len(kalshi_markets)} weather markets")
        except Exception as e:
            logger.error(f"Failed to fetch Kalshi weather markets: {e}")

    logger.info(f"Found {len(markets)} total weather temperature markets")

    # Identify each city/date's consensus "between" bracket — the crowd's
    # favorite pick — so generate_weather_signal can refuse to bet NO
    # against it (see consensus-bracket guardrail). "Consensus" is either
    # the highest-24h-volume bracket OR the highest-priced (highest implied
    # probability) bracket; these aren't always the same ticker (e.g. Boston
    # 2026-08-14: B82.5 had the most volume, but B84.5 had the highest
    # price/implied probability) — protect against fighting either signal.
    consensus_tickers_by_city_date: Dict[tuple, set] = {}
    best_volume_by_city_date: Dict[tuple, float] = {}
    best_price_by_city_date: Dict[tuple, float] = {}
    for m in markets:
        if m.direction != "between":
            continue
        key = (m.city_key, m.target_date)
        consensus_tickers_by_city_date.setdefault(key, set())
        if m.volume > best_volume_by_city_date.get(key, -1.0):
            best_volume_by_city_date[key] = m.volume
        if m.yes_price > best_price_by_city_date.get(key, -1.0):
            best_price_by_city_date[key] = m.yes_price
    for m in markets:
        if m.direction != "between":
            continue
        key = (m.city_key, m.target_date)
        if m.volume == best_volume_by_city_date.get(key) or m.yes_price == best_price_by_city_date.get(key):
            consensus_tickers_by_city_date[key].add(m.market_id)

    # Market-implied consensus temperature — added 2026-08-17. Every
    # "between" bracket's price is the crowd's probability estimate for that
    # 1°F slice; weighting each bracket's midpoint by its price and summing
    # gives a single "what temperature is Kalshi, as a whole, pricing in"
    # number — built entirely from money already on the book, no forecast
    # model involved. generate_weather_signal uses this as a sanity check:
    # if our model disagrees with the *entire* market by a wide margin, that
    # was the exact shape of the Miami mispricing (89°F model vs a market
    # clearly pricing low-90s) that motivated adding this.
    market_implied_mean_by_city_date: Dict[tuple, float] = {}
    _weighted_sum: Dict[tuple, float] = {}
    _weight_total: Dict[tuple, float] = {}
    for m in markets:
        if m.direction != "between" or m.floor_f is None or m.cap_f is None:
            continue
        key = (m.city_key, m.target_date)
        midpoint = (m.floor_f + m.cap_f) / 2.0
        weight = max(m.yes_price, 0.001)  # keep every bracket some voice, avoid pure zero-weight
        _weighted_sum[key] = _weighted_sum.get(key, 0.0) + midpoint * weight
        _weight_total[key] = _weight_total.get(key, 0.0) + weight
    for key, total_w in _weight_total.items():
        if total_w > 0:
            market_implied_mean_by_city_date[key] = _weighted_sum[key] / total_w

    for market in markets:
        try:
            key = (market.city_key, market.target_date)
            is_consensus = market.market_id in consensus_tickers_by_city_date.get(key, set())
            signal = await generate_weather_signal(
                market,
                is_consensus_bracket=is_consensus,
                market_implied_mean=market_implied_mean_by_city_date.get(key),
            )
            if signal:
                signals.append(signal)
        except Exception as e:
            logger.debug(f"Weather signal generation failed for {market.title}: {e}")

    signals.sort(key=lambda s: abs(s.edge), reverse=True)

    # One signal per (city_key, target_date) — keep only the highest-edge bracket.
    # Adjacent B/T-series contracts for the same city and date are correlated bets
    # on the same underlying temperature outcome. Taking more than one compounds
    # exposure without adding independent edge and competes against itself.
    seen_city_date: set = set()
    deduplicated: List[WeatherTradingSignal] = []
    for s in signals:
        key = (s.market.city_key, s.market.target_date)
        if key not in seen_city_date:
            seen_city_date.add(key)
            deduplicated.append(s)
        else:
            logger.debug(
                f"[DEDUP] Dropped {s.market.market_id} — already have a higher-edge "
                f"signal for {s.market.city_key}/{s.market.target_date}"
            )

    actionable = [s for s in deduplicated if s.passes_threshold]
    logger.info(
        f"WEATHER SCAN COMPLETE: {len(signals)} signals, "
        f"{len(deduplicated)} after city/date dedup, {len(actionable)} actionable"
    )

    for signal in actionable[:5]:
        logger.info(
            f"  {signal.market.city_name}: {signal.market.metric} {signal.market.direction} "
            f"{signal.market.threshold_f:.0f}F | "
            f"Edge: {signal.edge:+.1%} | z={abs(signal.market.threshold_f - signal.ensemble_mean) / max(signal.ensemble_std, TEMP_UNCERTAINTY_FLOOR_F):.1f}σ"
        )

    _signals_cache["signals"] = signals
    _signals_cache["timestamp"] = datetime.utcnow()

    _persist_weather_signals(signals)
    return signals


def _persist_weather_signals(signals: list):
    """Save weather signals to DB for calibration tracking."""
    to_save = [s for s in signals if abs(s.edge) > 0]
    if not to_save:
        return

    db = SessionLocal()
    try:
        for signal in to_save:
            existing = db.query(Signal).filter(
                Signal.market_ticker == signal.market.market_id,
                Signal.timestamp >= signal.timestamp.replace(second=0, microsecond=0),
            ).first()
            if existing:
                continue

            db_signal = Signal(
                market_ticker=signal.market.market_id,
                platform=signal.market.platform,
                market_type="weather",
                timestamp=signal.timestamp,
                direction=signal.direction,
                model_probability=signal.model_probability,
                market_price=signal.market_probability,
                edge=signal.edge,
                confidence=signal.confidence,
                kelly_fraction=signal.kelly_fraction,
                suggested_size=signal.suggested_size,
                sources=signal.sources,
                reasoning=signal.reasoning,
                executed=False,
            )
            db.add(db_signal)

        db.commit()
    except Exception as e:
        logger.warning(f"Failed to persist weather signals: {e}")
        db.rollback()
    finally:
        db.close()
