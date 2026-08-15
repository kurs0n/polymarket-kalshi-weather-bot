"""Simulated and live order execution for weather temperature markets."""
import logging
from datetime import datetime, date as _date
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

from backend.models.database import Trade

logger = logging.getLogger("trading_bot")

# ──────────────────────────────────────────────────────────────────────────────
# Order-book quality threshold for paper fills
# ──────────────────────────────────────────────────────────────────────────────
MAX_PAPER_SPREAD = 0.10   # 10¢ — wider than this and the paper fill is rejected

# ──────────────────────────────────────────────────────────────────────────────
# Physical execution guardrail constants
#
# These thresholds encode physical truths that the GFS/HRRR ensemble cannot
# capture once live METAR observations have already partially constrained the
# day's temperature trajectory.
# ──────────────────────────────────────────────────────────────────────────────

# Kill switch: reject if live temp ≥ bracket_ceiling − this buffer (°F).
#
# Root-caused 2026-08-14: Kalshi switched settlement source from raw NWS to
# "The Weather Company" on this date. Checked their actual rules text — TWC's
# number is their "Daily Climate Report," which for a station like KMIA is
# the standard NWS climate summary product republished by TWC, not an
# independently-measured reading. So this isn't a different thermometer, but
# there is a real (smaller) gap: we watch a live, rolling METAR feed, while
# Kalshi grades on the final, QC'd end-of-day report, which can differ
# slightly from a simple max-of-hourly-readings. 1.5°F covered instrument
# error alone; bumped to 2.0°F to also cover that live-vs-final reporting gap
# now that the graded source is one step removed from what we're polling.
_KILL_SWITCH_BUFFER_F = 2.0

# Velocity kill switch: projected peak = current_temp + hours_to_peak × trend.
# If projected_peak > bracket_ceiling, reject even when current temp is still safe.
# Assumed local diurnal peak hour (24h clock) — 3 PM is a conservative CONUS estimate.
_DIURNAL_PEAK_HOUR = 15

# Tail-risk guard: refuse below-ceiling bets trading < this market probability
# when a warming trend is active. A contract at 10¢ looks cheap, but a warming
# METAR means the model probability is stale and the "edge" is an artefact.
_LOW_PROB_TAIL_THRESHOLD = 0.20

# Execution time window: only fire automated orders for same-day contracts
# within this local-hour range. Before 11h the morning heating curve hasn't
# stabilised; after 14h the daily peak is likely in the past or imminent.
_TRADE_WINDOW_START_H = 11
_TRADE_WINDOW_END_H   = 14


# ──────────────────────────────────────────────────────────────────────────────
# Guardrail helpers
# ──────────────────────────────────────────────────────────────────────────────

def _hours_until_diurnal_peak(city_key: str) -> float:
    """
    Hours from now until the assumed 3 PM local temperature peak.
    Returns 0.0 when the peak has already passed today.
    """
    from backend.data.weather import CITY_CONFIG
    tz_str = CITY_CONFIG.get(city_key, {}).get("timezone", "UTC")
    local_now = datetime.now(ZoneInfo(tz_str))
    peak = local_now.replace(hour=_DIURNAL_PEAK_HOUR, minute=0, second=0, microsecond=0)
    return max(0.0, (peak - local_now).total_seconds() / 3600.0)


async def _check_physical_guardrails(signal) -> Optional[str]:
    """
    Run three live-data guardrails before any order is placed.

    Returns a rejection reason string, or None when the trade is safe.

    Checks (in order):
      1. Time window gate — same-day contracts only execute 11h–14h local time,
         after the morning diurnal heating slope has stabilised.
      2. Absolute floor kill switch — live METAR has already pushed the station
         temperature within 1.5°F of the bracket ceiling.  The daily high
         cannot decrease once observed, so the bet is physically lost.
      3. Warming velocity kill switch — even if the current reading is safe,
         the current °F/hr trend projected to the 3 PM peak will breach the
         bracket ceiling.
      4. Tail-risk probability guard — a below-ceiling bet priced below 20¢
         combined with a positive warming trend is a mispricing trap: the
         model probability is stale; the cheap price reflects the market's
         live METAR access, not a genuine inefficiency.

    Checks 2–4 require a live METAR reading.  If the NWS station is
    unreachable, only the time-window gate runs — the trade is allowed through
    rather than blocked on an infrastructure failure.
    """
    from backend.data.weather import CITY_CONFIG, fetch_metar_current

    market = signal.market
    city_key = market.city_key
    tz_str = CITY_CONFIG.get(city_key, {}).get("timezone", "UTC")
    local_now = datetime.now(ZoneInfo(tz_str))
    local_hour = local_now.hour

    # ── 1. Execution time window (same-day contracts only) ────────────────────
    is_same_day = (market.target_date == _date.today())
    if is_same_day and not (_TRADE_WINDOW_START_H <= local_hour <= _TRADE_WINDOW_END_H):
        return (
            f"TIME_GATE: local hour {local_hour:02d}h is outside the "
            f"{_TRADE_WINDOW_START_H:02d}h–{_TRADE_WINDOW_END_H:02d}h execution window "
            f"for same-day contract {market.market_id}"
        )

    # ── Fetch live METAR (cached) ─────────────────────────────────────────────
    metar = await fetch_metar_current(city_key)
    if metar is None:
        logger.debug(
            f"METAR unavailable for {city_key} — skipping physical guardrails"
        )
        return None

    current_temp = metar.observed_temp_f
    trend        = metar.trend_f_per_hour   # °F/hr; +ve = warming
    hours_to_peak = _hours_until_diurnal_peak(city_key)

    # Determine whether this trade WINS only if the daily high stays BELOW
    # a ceiling.  Both legs of the below-ceiling bet share the same kill-switch
    # logic regardless of whether we're long YES or long NO.
    #
    #  direction="yes" + market.direction="below"  → YES wins if high < ceiling
    #  direction="no"  + market.direction="above"  → NO  wins if high < ceiling
    betting_high_stays_below = (
        (signal.direction == "yes" and market.direction == "below") or
        (signal.direction == "no"  and market.direction == "above")
    )

    if betting_high_stays_below:
        ceiling = market.threshold_f

        # ── 2. Absolute floor kill switch ─────────────────────────────────────
        if current_temp >= ceiling - _KILL_SWITCH_BUFFER_F:
            return (
                f"KILL_SWITCH: {metar.station} live temp {current_temp:.1f}°F "
                f">= bracket ceiling {ceiling:.1f}°F − {_KILL_SWITCH_BUFFER_F}°F buffer. "
                f"Order rejected: Live ground temperature ({current_temp:.1f}°F) "
                f"has breached bracket threshold."
            )

        # ── 3. Warming velocity kill switch ───────────────────────────────────
        if trend > 0 and hours_to_peak > 0:
            projected_peak = current_temp + hours_to_peak * trend
            if projected_peak > ceiling:
                return (
                    f"VELOCITY_KILL: projected peak {projected_peak:.1f}°F "
                    f"({current_temp:.1f}°F + {hours_to_peak:.1f}h × {trend:+.2f}°F/hr) "
                    f"will exceed bracket ceiling {ceiling:.1f}°F"
                )

    # ── 4. Low-probability tail guard ────────────────────────────────────────
    entry_price = market.yes_price if signal.direction == "yes" else market.no_price
    if (
        entry_price < _LOW_PROB_TAIL_THRESHOLD
        and betting_high_stays_below
        and trend > 0
    ):
        return (
            f"TAIL_GUARD: {entry_price:.0%} market price + warming trend "
            f"({trend:+.2f}°F/hr) on below-ceiling bet {market.market_id}. "
            f"Refusing low-probability tail contract against adverse temperature trend."
        )

    return None


# ──────────────────────────────────────────────────────────────────────────────
# Order-book live-ask fetch (unchanged from prior session)
# ──────────────────────────────────────────────────────────────────────────────

async def _fetch_live_ask(
    market_id: str, direction: str
) -> Tuple[Optional[float], Optional[str]]:
    """
    Fetch the live best ask for a Kalshi market from the real-time order book.

    Returns (ask_price, None) on success, (None, reason) when the book has no
    ask liquidity, the spread is too wide, or credentials are unavailable.
    """
    try:
        from backend.data.kalshi_client import KalshiClient, kalshi_credentials_present
        from backend.data.kalshi_markets import _extract_prices_from_orderbook

        if not kalshi_credentials_present():
            return None, "no_credentials"

        client = KalshiClient()
        ob_data = await client.get_orderbook(market_id)
        prices, reject_reason = _extract_prices_from_orderbook(ob_data)

        if prices is None:
            return None, reject_reason or "no_liquidity"

        spread = prices["bid_ask_spread"]
        if spread > MAX_PAPER_SPREAD:
            return None, f"spread_too_wide({spread:.2f})"

        ask = prices["yes_ask"] if direction == "yes" else prices["no_ask"]
        return ask, None

    except Exception as e:
        logger.warning(f"Order book fetch failed for {market_id}: {e}")
        return None, f"exception: {e}"


# ──────────────────────────────────────────────────────────────────────────────
# Paper trade execution
# ──────────────────────────────────────────────────────────────────────────────

async def execute_paper_trade(signal, trade_size: float) -> Optional[Trade]:
    """
    Simulate a paper trade using the live Kalshi order book best ask price.

    Physical guardrails run first.  The trade is rejected (returns None) when:
      - Any physical guardrail fires (time window, kill switch, tail guard), OR
      - The live order book has no ask liquidity AND the snapshot spread > 10¢.

    Entry price is set to the live ask when available so paper P&L reflects
    realistic taker cost.  Falls back to snapshot ask when the live book is
    transiently unavailable but the snapshot spread is acceptable.

    Args:
        signal:     WeatherTradingSignal with market, direction, and edge data.
        trade_size: Dollar amount to commit.

    Returns:
        Trade instance (not yet in DB session) on success, None when rejected.
    """
    market = signal.market

    # ── Physical guardrails (run before any order-book IO) ───────────────────
    reject = await _check_physical_guardrails(signal)
    if reject is not None:
        logger.info(f"[GUARDRAIL PAPER] {market.market_id}: {reject}")
        return None

    # ── Live order-book ask ───────────────────────────────────────────────────
    live_ask, reject_reason = await _fetch_live_ask(market.market_id, signal.direction)

    if live_ask is None:
        snapshot_spread = getattr(market, "bid_ask_spread", MAX_PAPER_SPREAD + 1.0)
        if snapshot_spread > MAX_PAPER_SPREAD:
            logger.debug(
                f"[PAPER REJECTED] {market.market_id}: {reject_reason}, "
                f"snapshot spread {snapshot_spread:.2f} > {MAX_PAPER_SPREAD:.2f}"
            )
            return None
        entry_price = market.yes_price if signal.direction == "yes" else market.no_price
        logger.debug(
            f"[PAPER FALLBACK] {market.market_id}: live book unavailable ({reject_reason}), "
            f"snapshot ask {entry_price:.2%}"
        )
    else:
        entry_price = live_ask
        logger.debug(
            f"[PAPER FILL] {market.market_id}: entry_price={entry_price:.2%} (live ask)"
        )

    return Trade(
        market_ticker=market.market_id,
        platform=market.platform,
        event_slug=market.slug,
        market_type="weather",
        direction=signal.direction,
        entry_price=entry_price,
        size=trade_size,
        model_probability=signal.model_probability,
        market_price_at_entry=signal.market_probability,
        edge_at_entry=signal.edge,
        confidence=signal.confidence,
        execution_type="simulated",
        limit_price=entry_price,
        order_placed_at=datetime.utcnow(),
        order_status="filled",
    )


# ──────────────────────────────────────────────────────────────────────────────
# Live trade execution
# ──────────────────────────────────────────────────────────────────────────────

async def execute_live_trade(signal, trade_size: float) -> Optional[Trade]:
    """
    Place a resting maker limit order on Kalshi and return the corresponding Trade.

    Physical guardrails run first — returns None when any guardrail fires.
    (The caller must handle None; see scheduler.py.)

    On successful order placement the trade has order_status="pending_fill".
    If the Kalshi API call fails, the trade falls back to order_status="filled"
    so the position is tracked rather than silently dropped.

    Args:
        signal:     WeatherTradingSignal with limit_price, direction, and market.
        trade_size: Dollar amount to commit.

    Returns:
        Trade instance (not yet in DB session) on success, None when a physical
        guardrail rejects the order.
    """
    from backend.data.kalshi_client import KalshiClient

    market = signal.market

    # ── Physical guardrails ───────────────────────────────────────────────────
    reject = await _check_physical_guardrails(signal)
    if reject is not None:
        logger.info(f"[GUARDRAIL LIVE] {market.market_id}: {reject}")
        return None

    entry_price = signal.limit_price if signal.limit_price > 0 else (
        market.yes_price if signal.direction == "yes" else market.no_price
    )

    trade = Trade(
        market_ticker=market.market_id,
        platform=market.platform,
        event_slug=market.slug,
        market_type="weather",
        direction=signal.direction,
        entry_price=entry_price,
        size=trade_size,
        model_probability=signal.model_probability,
        market_price_at_entry=signal.market_probability,
        edge_at_entry=signal.edge,
        confidence=signal.confidence,
        execution_type="maker_limit",
        limit_price=entry_price,
        order_placed_at=datetime.utcnow(),
        order_status="pending_fill",
    )

    try:
        client = KalshiClient()
        limit_cents = round(entry_price * 100)
        count = max(1, round(trade_size / entry_price))
        resp = await client.place_order(
            market.market_id, signal.direction, count, limit_cents
        )
        order = resp.get("order", {})
        trade.order_id = order.get("order_id")
    except Exception as e:
        logger.warning(f"Kalshi order placement failed for {market.market_id}: {e}")
        trade.order_status = "filled"

    return trade
