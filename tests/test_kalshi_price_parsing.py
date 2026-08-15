"""
Unit tests for Kalshi bid-ask price extraction and liquidity filtering.

Tests _extract_prices() (summary endpoint) and _extract_prices_from_orderbook()
(live orderbook endpoint) directly — no network calls needed.

Run with:
    cd polymarket-kalshi-weather-bot
    python -m pytest tests/test_kalshi_price_parsing.py -v
"""
import pytest
from backend.data.kalshi_markets import (
    _extract_prices,
    _extract_prices_from_orderbook,
    _PriceRejectReason,
    MAX_BID_ASK_SPREAD_CENTS,
    _parse_kalshi_ticker,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _market(yes_ask=None, yes_bid=None, no_ask=None, no_bid=None, last_price=None):
    """Build a minimal Kalshi market dict with the given fields."""
    return {
        "yes_ask":    yes_ask,
        "yes_bid":    yes_bid,
        "no_ask":     no_ask,
        "no_bid":     no_bid,
        "last_price": last_price,
    }


# ---------------------------------------------------------------------------
# Test group 1: valid liquid markets
# ---------------------------------------------------------------------------

class TestValidMarkets:

    def test_normal_liquid_market(self):
        """Standard mid-market: yes_ask=52, yes_bid=48 → 4¢ spread."""
        prices, reason = _extract_prices(_market(yes_ask=52, yes_bid=48))
        assert reason is None
        assert prices is not None
        assert prices["yes_ask"]  == pytest.approx(0.52)
        assert prices["yes_bid"]  == pytest.approx(0.48)
        assert prices["bid_ask_spread"] == pytest.approx(0.04)

    def test_spread_exactly_at_limit_is_accepted(self):
        """Spread = MAX_BID_ASK_SPREAD_CENTS exactly should pass."""
        ask = 55
        bid = ask - MAX_BID_ASK_SPREAD_CENTS
        prices, reason = _extract_prices(_market(yes_ask=ask, yes_bid=bid))
        assert reason is None
        assert prices is not None
        assert prices["bid_ask_spread"] == pytest.approx(MAX_BID_ASK_SPREAD_CENTS / 100.0)

    def test_no_prices_derived_from_yes_book(self):
        """When no_ask/no_bid are absent, they're derived from yes_bid/yes_ask."""
        prices, reason = _extract_prices(_market(yes_ask=60, yes_bid=55))
        assert reason is None
        # no_ask = 100 - yes_bid = 45; no_bid = 100 - yes_ask = 40
        assert prices["no_ask"] == pytest.approx(0.45)
        assert prices["no_bid"] == pytest.approx(0.40)

    def test_explicit_no_prices_are_preserved(self):
        """When no_ask/no_bid are in the response, use them directly."""
        prices, reason = _extract_prices(
            _market(yes_ask=52, yes_bid=48, no_ask=46, no_bid=44)
        )
        assert reason is None
        assert prices["no_ask"] == pytest.approx(0.46)
        assert prices["no_bid"] == pytest.approx(0.44)


# ---------------------------------------------------------------------------
# Test group 2: illiquid/missing ask → rejected
# ---------------------------------------------------------------------------

class TestMissingAsk:

    def test_yes_ask_none_is_rejected(self):
        prices, reason = _extract_prices(_market(yes_ask=None, yes_bid=48))
        assert prices is None
        assert reason == _PriceRejectReason.NO_ASK

    def test_yes_ask_zero_is_rejected(self):
        """Zero is the sentinel for 'field missing'; must not be traded."""
        prices, reason = _extract_prices(_market(yes_ask=0, yes_bid=48))
        assert prices is None
        assert reason == _PriceRejectReason.NO_ASK

    def test_no_last_price_fallback(self):
        """
        Critical regression: the old code fell back to last_price=50 when
        yes_ask was 0. That produced a phantom 50¢ price on illiquid markets.
        Confirm the fix: a missing ask with a valid last_price is still rejected.
        """
        prices, reason = _extract_prices(_market(yes_ask=0, yes_bid=48, last_price=50))
        assert prices is None, (
            "Should NOT fall back to last_price when yes_ask is 0 — "
            "last_price is stale and produces phantom quotes"
        )
        assert reason == _PriceRejectReason.NO_ASK

    def test_no_default_50_cent_fallback(self):
        """
        Regression: old code used `(m.get('last_price') or 50) / 100`.
        With yes_ask=0 and last_price=None, it produced exactly 0.50.
        Confirm that path is gone.
        """
        prices, reason = _extract_prices(_market(yes_ask=None, yes_bid=None))
        assert prices is None
        # Must be one of the rejection reasons, never a 0.50 price
        assert reason in (
            _PriceRejectReason.NO_ASK,
            _PriceRejectReason.NO_BID,
        )


# ---------------------------------------------------------------------------
# Test group 3: missing bid → rejected
# ---------------------------------------------------------------------------

class TestMissingBid:

    def test_yes_bid_none_is_rejected(self):
        prices, reason = _extract_prices(_market(yes_ask=52, yes_bid=None))
        assert prices is None
        assert reason == _PriceRejectReason.NO_BID

    def test_yes_bid_zero_is_rejected(self):
        """Zero yes_bid = one-sided book (no buyers). Can't verify spread."""
        prices, reason = _extract_prices(_market(yes_ask=52, yes_bid=0))
        assert prices is None
        assert reason == _PriceRejectReason.NO_BID


# ---------------------------------------------------------------------------
# Test group 4: spread guard
# ---------------------------------------------------------------------------

class TestSpreadGuard:

    def test_spread_one_cent_over_limit_is_rejected(self):
        ask = 60
        bid = ask - (MAX_BID_ASK_SPREAD_CENTS + 1)
        prices, reason = _extract_prices(_market(yes_ask=ask, yes_bid=bid))
        assert prices is None
        assert reason == _PriceRejectReason.SPREAD_TOO_WIDE

    def test_wide_spread_20_cents_rejected(self):
        prices, reason = _extract_prices(_market(yes_ask=60, yes_bid=40))
        assert prices is None
        assert reason == _PriceRejectReason.SPREAD_TOO_WIDE

    def test_crossed_book_rejected(self):
        """ask < bid is a data error — should not produce a negative spread."""
        prices, reason = _extract_prices(_market(yes_ask=48, yes_bid=52))
        assert prices is None
        assert reason == _PriceRejectReason.SPREAD_TOO_WIDE

    def test_spread_reported_correctly(self):
        prices, _ = _extract_prices(_market(yes_ask=55, yes_bid=48))
        assert prices["bid_ask_spread"] == pytest.approx(0.07)


# ---------------------------------------------------------------------------
# Test group 5: ticker parsing
# ---------------------------------------------------------------------------

class TestTickerParsing:

    def test_bottom_bracket_above(self):
        result = _parse_kalshi_ticker("KXHIGHNY-26MAR01-B45.5", "nyc")
        assert result is not None
        assert result["direction"] == "above"
        assert result["threshold_f"] == pytest.approx(45.5)

    def test_top_bracket_below(self):
        result = _parse_kalshi_ticker("KXHIGHNY-26MAR01-T45.5", "nyc")
        assert result is not None
        assert result["direction"] == "below"

    def test_boston_series_with_t_prefix(self):
        """KXHIGHTBOS series ticker — the 'T' in KXHIGHT is part of the series
        name, not the bracket type. The bracket type comes after the date."""
        result = _parse_kalshi_ticker("KXHIGHTBOS-26AUG10-B85.0", "boston")
        assert result is not None
        assert result["direction"] == "above"
        assert result["threshold_f"] == pytest.approx(85.0)

    def test_invalid_ticker_returns_none(self):
        assert _parse_kalshi_ticker("KXHIGHNY-26MAR01", "nyc") is None
        assert _parse_kalshi_ticker("KXHIGHNY-26MAR01-X45.5", "nyc") is None
        assert _parse_kalshi_ticker("", "nyc") is None


# ---------------------------------------------------------------------------
# Test group 6: orderbook_fp extraction (_extract_prices_from_orderbook)
# ---------------------------------------------------------------------------

def _ob(yes_dollars=None, no_dollars=None):
    """Build a minimal orderbook_fp response dict."""
    ob = {}
    if yes_dollars is not None:
        ob["yes_dollars"] = yes_dollars
    if no_dollars is not None:
        ob["no_dollars"] = no_dollars
    return {"orderbook_fp": ob}


class TestOrderbookExtraction:
    """
    Tests for _extract_prices_from_orderbook.

    Binary market identity used throughout:
        yes_ask = 1.0 - max_no_bid
        no_ask  = 1.0 - max_yes_bid
        spread  = yes_ask - yes_bid = (1 - max_no_bid) - max_yes_bid
    """

    def test_normal_liquid_market(self):
        """
        YES bids: best = 0.48; NO bids: best = 0.49
        → yes_ask = 1 - 0.49 = 0.51, spread = 0.51 - 0.48 = 0.03
        """
        data = _ob(
            yes_dollars=[[0.48, 100], [0.45, 200]],
            no_dollars= [[0.49,  75], [0.46, 150]],
        )
        prices, reason = _extract_prices_from_orderbook(data)
        assert reason is None
        assert prices is not None
        assert prices["yes_bid"] == pytest.approx(0.48)
        assert prices["no_bid"]  == pytest.approx(0.49)
        assert prices["yes_ask"] == pytest.approx(0.51)
        assert prices["no_ask"]  == pytest.approx(0.52)
        assert prices["bid_ask_spread"] == pytest.approx(0.03)

    def test_best_bid_is_taken_from_multiple_levels(self):
        """The highest price across all levels is used, not the first entry."""
        data = _ob(
            yes_dollars=[[0.40, 50], [0.47, 100], [0.44, 80]],
            no_dollars= [[0.45, 60], [0.50, 30],  [0.48, 90]],
        )
        prices, reason = _extract_prices_from_orderbook(data)
        assert reason is None
        assert prices["yes_bid"] == pytest.approx(0.47)
        assert prices["no_bid"]  == pytest.approx(0.50)
        assert prices["yes_ask"] == pytest.approx(0.50)  # 1 - 0.50
        assert prices["no_ask"]  == pytest.approx(0.53)  # 1 - 0.47

    def test_spread_exactly_at_limit_is_accepted(self):
        """Spread = MAX_BID_ASK_SPREAD_CENTS / 100 exactly should pass."""
        max_spread = MAX_BID_ASK_SPREAD_CENTS / 100.0
        # yes_bid=0.47, no_bid=0.43 → yes_ask=0.57, spread=0.57-0.47=0.10
        data = _ob(
            yes_dollars=[[0.47, 100]],
            no_dollars= [[0.43, 100]],
        )
        prices, reason = _extract_prices_from_orderbook(data)
        assert reason is None
        assert prices["bid_ask_spread"] == pytest.approx(max_spread)

    def test_spread_one_cent_over_limit_is_rejected(self):
        # yes_bid=0.47, no_bid=0.42 → yes_ask=0.58, spread=0.11 > 0.10
        data = _ob(
            yes_dollars=[[0.47, 100]],
            no_dollars= [[0.42, 100]],
        )
        prices, reason = _extract_prices_from_orderbook(data)
        assert prices is None
        assert reason == _PriceRejectReason.SPREAD_TOO_WIDE

    def test_empty_yes_dollars_rejected_no_bid(self):
        """No YES bids → can't compute no_ask = 1 - yes_bid."""
        data = _ob(yes_dollars=[], no_dollars=[[0.48, 100]])
        prices, reason = _extract_prices_from_orderbook(data)
        assert prices is None
        assert reason == _PriceRejectReason.NO_BID

    def test_missing_yes_dollars_key_rejected(self):
        """orderbook_fp with no yes_dollars key at all."""
        data = _ob(no_dollars=[[0.48, 100]])
        prices, reason = _extract_prices_from_orderbook(data)
        assert prices is None
        assert reason == _PriceRejectReason.NO_BID

    def test_empty_no_dollars_rejected_no_ask(self):
        """No NO bids → can't compute yes_ask = 1 - no_bid."""
        data = _ob(yes_dollars=[[0.48, 100]], no_dollars=[])
        prices, reason = _extract_prices_from_orderbook(data)
        assert prices is None
        assert reason == _PriceRejectReason.NO_ASK

    def test_missing_no_dollars_key_rejected(self):
        data = _ob(yes_dollars=[[0.48, 100]])
        prices, reason = _extract_prices_from_orderbook(data)
        assert prices is None
        assert reason == _PriceRejectReason.NO_ASK

    def test_all_zero_bids_rejected(self):
        """Entries with price=0 must be ignored; if all are zero, reject."""
        data = _ob(
            yes_dollars=[[0.0, 100], [0.0, 50]],
            no_dollars= [[0.48, 75]],
        )
        prices, reason = _extract_prices_from_orderbook(data)
        assert prices is None
        assert reason == _PriceRejectReason.NO_BID

    def test_crossed_book_rejected(self):
        """
        yes_bid=0.70, no_bid=0.40 → yes_ask=1-0.40=0.60
        spread = 0.60 - 0.70 = -0.10 → crossed book.
        """
        data = _ob(
            yes_dollars=[[0.70, 100]],
            no_dollars= [[0.40, 100]],
        )
        prices, reason = _extract_prices_from_orderbook(data)
        assert prices is None
        assert reason == _PriceRejectReason.SPREAD_TOO_WIDE

    def test_missing_orderbook_fp_key(self):
        """Response with no orderbook_fp key at all."""
        prices, reason = _extract_prices_from_orderbook({})
        assert prices is None

    def test_none_entry_values_are_skipped(self):
        """None values inside entries must not cause crashes."""
        data = _ob(
            yes_dollars=[[None, 100], [0.48, 50]],
            no_dollars= [[0.49,  75]],
        )
        prices, reason = _extract_prices_from_orderbook(data)
        assert reason is None
        assert prices["yes_bid"] == pytest.approx(0.48)

    def test_single_level_each_side(self):
        """Minimal valid book: one YES bid, one NO bid."""
        data = _ob(
            yes_dollars=[[0.52, 200]],
            no_dollars= [[0.44, 200]],
        )
        prices, reason = _extract_prices_from_orderbook(data)
        assert reason is None
        assert prices["yes_bid"] == pytest.approx(0.52)
        assert prices["no_bid"]  == pytest.approx(0.44)
        assert prices["yes_ask"] == pytest.approx(0.56)   # 1 - 0.44
        assert prices["no_ask"]  == pytest.approx(0.48)   # 1 - 0.52
        assert prices["bid_ask_spread"] == pytest.approx(0.04)  # 0.56 - 0.52
