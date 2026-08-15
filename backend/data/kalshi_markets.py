"""Kalshi weather temperature market fetcher."""
import asyncio
import logging
import re
from datetime import date
from typing import Dict, List, Optional, Tuple

from backend.data.kalshi_client import KalshiClient, kalshi_credentials_present
from backend.data.weather_markets import WeatherMarket

logger = logging.getLogger("trading_bot")

# ---------------------------------------------------------------------------
# Series / city mappings
# ---------------------------------------------------------------------------

CITY_SERIES: Dict[str, str] = {
    "nyc":        "KXHIGHNY",
    "chicago":    "KXHIGHCHI",
    "miami":      "KXHIGHMIA",
    "los_angeles":"KXHIGHLAX",
    "denver":     "KXHIGHDEN",
    "boston":     "KXHIGHTBOS",
}

CITY_NAMES: Dict[str, str] = {
    "nyc":        "New York",
    "chicago":    "Chicago",
    "miami":      "Miami",
    "los_angeles":"Los Angeles",
    "denver":     "Denver",
    "boston":     "Boston",
}

MONTH_ABBR = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Reject markets where yes_ask - yes_bid > this threshold.
MAX_BID_ASK_SPREAD_CENTS: int = 10   # 10¢ = $0.10

# Maximum concurrent orderbook fetches. Keeps total latency under ~2 s
# for 100-200 candidate contracts while respecting Kalshi rate limits.
ORDERBOOK_CONCURRENCY: int = 12

# Optional: reject markets with lifetime volume below this floor.
MIN_VOLUME: float = 0.0  # set > 0 to enforce a minimum trade history


# ---------------------------------------------------------------------------
# Rejection reason constants (used in logs and unit tests)
# ---------------------------------------------------------------------------

class _PriceRejectReason:
    NO_ASK          = "no_ask"
    NO_BID          = "no_bid"
    SPREAD_TOO_WIDE = "spread_too_wide"
    RESOLVED        = "resolved"
    LOW_VOLUME      = "low_volume"
    ORDERBOOK_ERROR = "orderbook_error"


# ---------------------------------------------------------------------------
# Ticker parser
# ---------------------------------------------------------------------------

def _parse_kalshi_ticker(m, city_key: str) -> Optional[dict]:
    """
    Parse a Kalshi bracket market into structured parameters.

    IMPORTANT: direction and threshold(s) are derived from Kalshi's
    authoritative `strike_type` field on the market payload — NOT guessed
    from the ticker's B/T prefix. The B/T letter does not reliably indicate
    direction: e.g. KXHIGHDEN-26AUG14-T81 has strike_type="less" (YES if
    high stays BELOW 81°F) while KXHIGHDEN-26AUG14-T88 has strike_type=
    "greater" (YES if high goes ABOVE 88°F) — same letter, opposite meaning.
    (Root-caused 2026-08-13 after the bot bought YES on T81 believing it
    meant "above," when Kalshi's own rules_primary said "less than 81°.")

    "B"-prefixed tickers are narrow "between floor and cap" brackets (e.g.
    B87.5 = "87–88°F, and nothing else"), not cumulative above/below bets —
    also read from strike_type ("between") plus floor_strike/cap_strike.

    Accepts either:
      - the raw market dict from the Kalshi API (has strike_type/
        floor_strike/cap_strike) — the normal, authoritative path, or
      - a bare ticker string — ticker-only fallback for callers that don't
        have the live market payload handy. Direction cannot be determined
        reliably in this mode; the caller should prefer passing the dict.

    Ticker format: KXHIGHNY-26MAR01-B45.5 → 26MAR01 = target date 2026-03-01.
    """
    if isinstance(m, str):
        ticker = m
        market_meta = None
    else:
        ticker = m.get("ticker", "")
        market_meta = m

    match = re.match(
        r'^[A-Z]+-(\d{2})([A-Z]{3})(\d{2})-([BT])([\d.]+)$',
        ticker,
    )
    if not match:
        return None

    yy      = int(match.group(1))
    mon_str = match.group(2)
    dd      = int(match.group(3))

    month = MONTH_ABBR.get(mon_str)
    if not month:
        return None

    try:
        target_date = date(2000 + yy, month, dd)
    except ValueError:
        return None

    if market_meta is None:
        # No live market payload available — direction is unknown.  Return
        # the date/ticker-derived info only; callers must not rely on
        # "direction"/"threshold_f" from this branch for trading decisions.
        logger.warning(
            f"_parse_kalshi_ticker({ticker}) called without market metadata — "
            f"direction cannot be determined reliably from the ticker alone."
        )
        return {
            "target_date": target_date,
            "metric":      "high",
            "direction":   None,
            "threshold_f": float(match.group(5)),
            "floor_f":     None,
            "cap_f":       None,
        }

    strike_type  = market_meta.get("strike_type")
    floor_strike = market_meta.get("floor_strike")
    cap_strike   = market_meta.get("cap_strike")

    if strike_type == "greater":
        if floor_strike is None:
            return None
        return {
            "target_date": target_date,
            "metric":      "high",
            "direction":   "above",
            "threshold_f": float(floor_strike),
            "floor_f":     None,
            "cap_f":       None,
        }
    elif strike_type == "less":
        if cap_strike is None:
            return None
        return {
            "target_date": target_date,
            "metric":      "high",
            "direction":   "below",
            "threshold_f": float(cap_strike),
            "floor_f":     None,
            "cap_f":       None,
        }
    elif strike_type == "between":
        if floor_strike is None or cap_strike is None:
            return None
        return {
            "target_date": target_date,
            "metric":      "high",
            "direction":   "between",
            "threshold_f": (float(floor_strike) + float(cap_strike)) / 2.0,
            "floor_f":     float(floor_strike),
            "cap_f":       float(cap_strike),
        }
    else:
        logger.warning(f"Unknown strike_type {strike_type!r} for {ticker} — skipping")
        return None


# ---------------------------------------------------------------------------
# Price extraction — summary endpoint (retained for unit tests and fallback)
# ---------------------------------------------------------------------------

def _extract_prices(m: dict) -> Tuple[Optional[dict], Optional[str]]:
    """
    Extract and validate bid/ask prices from a Kalshi /markets list entry.

    All raw values from the list endpoint are integers in cents (0-100).
    This function is retained for unit tests and as a reference implementation.
    The main fetch flow uses _extract_prices_from_orderbook instead.

    Returns:
        (prices_dict, None)   — valid, tradeable market
        (None, reason_str)    — rejected

    prices_dict keys (all floats 0-1): yes_ask, yes_bid, no_ask, no_bid, bid_ask_spread
    """
    yes_ask_c = int(m.get("yes_ask") or 0)
    yes_bid_c = int(m.get("yes_bid") or 0)
    no_ask_c  = int(m.get("no_ask")  or 0)
    no_bid_c  = int(m.get("no_bid")  or 0)

    if yes_ask_c <= 0:
        return None, _PriceRejectReason.NO_ASK
    if yes_bid_c <= 0:
        return None, _PriceRejectReason.NO_BID

    spread_c = yes_ask_c - yes_bid_c
    if spread_c < 0:
        return None, _PriceRejectReason.SPREAD_TOO_WIDE
    if spread_c > MAX_BID_ASK_SPREAD_CENTS:
        return None, _PriceRejectReason.SPREAD_TOO_WIDE

    if no_bid_c <= 0:
        no_bid_c = 100 - yes_ask_c
    if no_ask_c <= 0:
        no_ask_c = 100 - yes_bid_c

    return {
        "yes_ask":        yes_ask_c / 100.0,
        "yes_bid":        yes_bid_c / 100.0,
        "no_ask":         no_ask_c  / 100.0,
        "no_bid":         no_bid_c  / 100.0,
        "bid_ask_spread": spread_c  / 100.0,
    }, None


# ---------------------------------------------------------------------------
# Price extraction — live orderbook (primary path)
# ---------------------------------------------------------------------------

def _extract_prices_from_orderbook(data: dict) -> Tuple[Optional[dict], Optional[str]]:
    """
    Extract and validate prices from a Kalshi orderbook_fp response.

    The orderbook endpoint returns dollar-denominated bids (buyers' prices):
        orderbook_fp.yes_dollars: [[price_float, size], ...]  — YES bids, best first
        orderbook_fp.no_dollars:  [[price_float, size], ...]  — NO bids, best first

    Binary market identity (YES + NO = $1.00):
        Selling YES at P is identical to buying NO at (1-P), so the lowest
        YES offer in the market is the counterparty to the best NO bid.

    Derived prices:
        yes_bid = max(yes_dollars prices)      — best YES buyer
        no_bid  = max(no_dollars prices)       — best NO buyer
        yes_ask = 1.00 - no_bid               — cheapest YES offer (= best NO bid counterparty)
        no_ask  = 1.00 - yes_bid              — cheapest NO offer (= best YES bid counterparty)

    Rejects when:
        - yes_dollars is empty or all-zero → NO_BID (no active YES buyers)
        - no_dollars is empty or all-zero  → NO_ASK (can't derive yes_ask)
        - spread > MAX_BID_ASK_SPREAD_CENTS → SPREAD_TOO_WIDE
        - crossed book (ask < bid)          → SPREAD_TOO_WIDE
    """
    ob = data.get("orderbook_fp") or {}

    yes_entries = ob.get("yes_dollars") or []
    no_entries  = ob.get("no_dollars")  or []

    def _best_bid(entries: list) -> Optional[float]:
        """Return the highest valid bid price from an orderbook side."""
        try:
            values = [
                float(e[0])
                for e in entries
                if e and len(e) >= 1 and e[0] is not None and float(e[0]) > 0
            ]
            return max(values) if values else None
        except (TypeError, ValueError):
            return None

    max_yes_bid = _best_bid(yes_entries)
    max_no_bid  = _best_bid(no_entries)

    # Both sides must have active bids to derive all four prices safely.
    if max_yes_bid is None:
        return None, _PriceRejectReason.NO_BID
    if max_no_bid is None:
        # No NO bids means we cannot compute yes_ask = 1 - no_bid.
        return None, _PriceRejectReason.NO_ASK

    yes_bid = max_yes_bid
    no_bid  = max_no_bid
    yes_ask = round(1.0 - no_bid,  6)
    no_ask  = round(1.0 - yes_bid, 6)

    # Sanity: all prices must be in the open interval (0, 1).
    if not (0 < yes_bid < 1 and 0 < no_bid < 1):
        return None, _PriceRejectReason.SPREAD_TOO_WIDE

    spread = round(yes_ask - yes_bid, 6)
    if spread < 0:
        # Crossed book: best NO bid is so high that yes_ask < yes_bid.
        return None, _PriceRejectReason.SPREAD_TOO_WIDE
    if spread > MAX_BID_ASK_SPREAD_CENTS / 100.0:
        return None, _PriceRejectReason.SPREAD_TOO_WIDE

    return {
        "yes_ask":        yes_ask,
        "yes_bid":        yes_bid,
        "no_ask":         no_ask,
        "no_bid":         no_bid,
        "bid_ask_spread": spread,
    }, None


# ---------------------------------------------------------------------------
# Two-phase fetch helpers
# ---------------------------------------------------------------------------

async def _collect_candidates(
    client: KalshiClient,
    city_key: str,
    series: str,
    today: date,
    rejected: Dict[str, int],
) -> List[dict]:
    """
    Phase 1: page through /markets for a series and return parsed candidate dicts.
    No prices are fetched here — only ticker metadata and volume.
    """
    candidates: List[dict] = []
    city_name = CITY_NAMES.get(city_key, city_key)
    cursor = None

    while True:
        params: dict = {"series_ticker": series, "status": "open", "limit": 200}
        if cursor:
            params["cursor"] = cursor

        data = await client.get_markets(params)
        raw = data.get("markets", [])

        for m in raw:
            ticker = m.get("ticker", "")

            parsed = _parse_kalshi_ticker(m, city_key)
            if not parsed or parsed.get("direction") is None:
                rejected["parse_failed"] += 1
                continue

            if parsed["target_date"] < today:
                rejected["expired"] += 1
                continue

            candidates.append({
                "ticker":    ticker,
                "city_key":  city_key,
                "city_name": city_name,
                "parsed":    parsed,
                # The v2 /markets list endpoint has no plain "volume" key —
                # only "volume_fp"/"volume_24h_fp". Reading "volume" silently
                # returned 0 for every market (root-caused 2026-08-14; harmless
                # so far since MIN_VOLUME=0, but would have broken the first
                # feature that actually relies on real volume data).
                "volume":    float(m.get("volume_24h_fp", 0) or 0),
                "title":     m.get("title", ticker),
            })

        cursor = data.get("cursor")
        if not cursor or not raw:
            break

    return candidates


async def _enrich_with_orderbook(
    client: KalshiClient,
    candidate: dict,
    semaphore: asyncio.Semaphore,
    rejected: Dict[str, int],
) -> Optional[WeatherMarket]:
    """
    Phase 2: fetch the live orderbook for one candidate ticker, extract prices,
    apply liquidity and resolution filters, and return a WeatherMarket or None.

    The semaphore caps concurrent in-flight requests to ORDERBOOK_CONCURRENCY.
    """
    ticker = candidate["ticker"]

    try:
        async with semaphore:
            ob_data = await client.get_orderbook(ticker)

        prices, reason = _extract_prices_from_orderbook(ob_data)
        if prices is None:
            rejected[reason] = rejected.get(reason, 0) + 1
            logger.debug(f"Rejected {ticker} via orderbook: {reason}")
            return None

        # A yes_ask near 0 or 1 means the market has effectively resolved.
        if prices["yes_ask"] > 0.98 or prices["yes_ask"] < 0.02:
            rejected[_PriceRejectReason.RESOLVED] += 1
            return None

        if MIN_VOLUME > 0 and candidate["volume"] < MIN_VOLUME:
            rejected[_PriceRejectReason.LOW_VOLUME] += 1
            return None

        parsed = candidate["parsed"]
        return WeatherMarket(
            slug=ticker,
            market_id=ticker,
            platform="kalshi",
            title=candidate["title"],
            city_key=candidate["city_key"],
            city_name=candidate["city_name"],
            target_date=parsed["target_date"],
            threshold_f=parsed["threshold_f"],
            metric=parsed["metric"],
            direction=parsed["direction"],
            yes_price=prices["yes_ask"],   # entry price when buying YES
            no_price=prices["no_ask"],     # entry price when buying NO
            yes_bid=prices["yes_bid"],
            no_bid=prices["no_bid"],
            bid_ask_spread=prices["bid_ask_spread"],
            floor_f=parsed.get("floor_f"),
            cap_f=parsed.get("cap_f"),
            has_live_book=True,
            volume=candidate["volume"],
        )

    except Exception as e:
        logger.debug(f"Orderbook enrichment failed for {ticker}: {e}")
        rejected[_PriceRejectReason.ORDERBOOK_ERROR] += 1
        return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def fetch_kalshi_weather_markets(
    city_keys: Optional[List[str]] = None,
) -> List[WeatherMarket]:
    """
    Fetch open, liquid weather temperature markets from Kalshi.

    Two-phase design:
      Phase 1 — List: query /markets for each city series (paginated).
                Collect candidate tickers with parsed metadata. No prices yet.
      Phase 2 — Orderbook: fetch /markets/{ticker}/orderbook for every candidate
                concurrently (up to ORDERBOOK_CONCURRENCY in-flight at once).
                Extract live bid/ask from orderbook_fp.yes_dollars / no_dollars.
                Apply spread guard and resolved-market filter.

    A market reaches WeatherMarket only if:
      - Ticker parses to a valid future-dated bracket contract
      - Both YES and NO sides have active bids in the live orderbook
      - Derived spread (yes_ask - yes_bid) ≤ MAX_BID_ASK_SPREAD_CENTS / 100
      - yes_ask is between 0.02 and 0.98 (not effectively resolved)
    """
    if not kalshi_credentials_present():
        return []

    client = KalshiClient()
    today  = date.today()
    cities = city_keys or list(CITY_SERIES.keys())

    rejected: Dict[str, int] = {
        _PriceRejectReason.NO_BID:          0,
        _PriceRejectReason.NO_ASK:          0,
        _PriceRejectReason.SPREAD_TOO_WIDE: 0,
        _PriceRejectReason.RESOLVED:        0,
        _PriceRejectReason.LOW_VOLUME:      0,
        _PriceRejectReason.ORDERBOOK_ERROR: 0,
        "parse_failed":                     0,
        "expired":                          0,
    }

    # ---- Phase 1: collect candidate tickers --------------------------------
    all_candidates: List[dict] = []
    for city_key in cities:
        series = CITY_SERIES.get(city_key)
        if not series:
            continue
        try:
            candidates = await _collect_candidates(
                client, city_key, series, today, rejected
            )
            all_candidates.extend(candidates)
        except Exception as e:
            logger.warning(f"Failed to list Kalshi markets for {city_key} ({series}): {e}")

    logger.info(
        f"Kalshi: {len(all_candidates)} candidate tickers "
        f"across {len(cities)} cities — fetching orderbooks..."
    )

    # ---- Phase 2: concurrent orderbook fetches -----------------------------
    semaphore = asyncio.Semaphore(ORDERBOOK_CONCURRENCY)
    tasks = [
        _enrich_with_orderbook(client, c, semaphore, rejected)
        for c in all_candidates
    ]
    results = await asyncio.gather(*tasks)
    markets = [r for r in results if r is not None]

    total_rejected = sum(rejected.values())
    logger.info(
        f"Kalshi weather markets: {len(markets)} accepted, {total_rejected} rejected "
        f"(no_bid={rejected[_PriceRejectReason.NO_BID]}, "
        f"no_ask={rejected[_PriceRejectReason.NO_ASK]}, "
        f"wide_spread={rejected[_PriceRejectReason.SPREAD_TOO_WIDE]}, "
        f"resolved={rejected[_PriceRejectReason.RESOLVED]}, "
        f"ob_error={rejected[_PriceRejectReason.ORDERBOOK_ERROR]}, "
        f"expired={rejected['expired']})"
    )
    return markets
