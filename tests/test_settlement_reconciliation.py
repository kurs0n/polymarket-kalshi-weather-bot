"""
Unit tests for the 2026-08-20 audit fixes:
  1. execute_live_trade no longer fabricates a "filled" trade when Kalshi
     order placement genuinely fails (used to silently create a phantom
     position — see execution.py's fix for the full rationale).
  2. reconcile_early_settlements corrects the ledger when an NWS early
     settlement (a live-observation projection, not Kalshi's own official
     result) disagrees with the real outcome once it's available.

Run with:
    cd polymarket-kalshi-weather-bot
    python -m pytest tests/test_settlement_reconciliation.py -v
"""
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.settlement import calculate_pnl, reconcile_early_settlements


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_trade(
    id=1,
    market_ticker="KXHIGHNY-26AUG18-B85.0",
    platform="kalshi",
    direction="yes",
    entry_price=0.20,
    size=100.0,
    settlement_value=1.0,
    pnl=400.0,
    result="win",
    settlement_source="nws_early",
    settlement_time=None,
    reconciled_at=None,
    signal_id=None,
):
    t = MagicMock()
    t.id = id
    t.market_ticker = market_ticker
    t.platform = platform
    t.direction = direction
    t.entry_price = entry_price
    t.size = size
    t.settled = True
    t.settlement_value = settlement_value
    t.pnl = pnl
    t.result = result
    t.settlement_source = settlement_source
    t.settlement_time = settlement_time or (datetime.utcnow() - timedelta(hours=24))
    t.reconciled_at = reconciled_at
    t.reconciliation_mismatch = None
    t.signal_id = signal_id
    return t


def _make_db(trades, signal=None, bot_state=None):
    """Mock db whose .query(Trade).filter(...).all() returns `trades`, and
    whose .query(Signal)/.query(BotState).filter(...).first() return the
    given single row (or a fresh MagicMock if none supplied)."""
    db = MagicMock()

    def query_side_effect(model):
        q = MagicMock()
        name = getattr(model, "__name__", str(model))
        if name == "Trade":
            q.filter.return_value.all.return_value = trades
        elif name == "Signal":
            q.filter.return_value.first.return_value = signal
        elif name == "BotState":
            q.filter.return_value.first.return_value = bot_state
            q.first.return_value = bot_state
        return q

    db.query.side_effect = query_side_effect
    return db


def _make_bot_state(bankroll=10000.0, total_pnl=0.0, winning_trades=5):
    s = MagicMock()
    s.bankroll = bankroll
    s.total_pnl = total_pnl
    s.winning_trades = winning_trades
    return s


# ---------------------------------------------------------------------------
# calculate_pnl — sanity check the already-fixed formula
# ---------------------------------------------------------------------------

class TestCalculatePnl:

    def test_win_pays_full_contract_payout_minus_stake(self):
        """$100 stake @ 20c entry -> 500 contracts -> $500 payout -> +$400 pnl."""
        trade = _make_trade(direction="yes", entry_price=0.20, size=100.0)
        pnl = calculate_pnl(trade, settlement_value=1.0)
        assert pnl == pytest.approx(400.0)

    def test_loss_forfeits_exactly_the_stake(self):
        trade = _make_trade(direction="yes", entry_price=0.20, size=100.0)
        pnl = calculate_pnl(trade, settlement_value=0.0)
        assert pnl == pytest.approx(-100.0)

    def test_no_direction_wins_when_settlement_is_zero(self):
        trade = _make_trade(direction="no", entry_price=0.30, size=100.0)
        pnl = calculate_pnl(trade, settlement_value=0.0)
        assert pnl == pytest.approx(round(100.0 * (1 - 0.30) / 0.30, 2))

    def test_no_direction_loses_when_settlement_is_one(self):
        trade = _make_trade(direction="no", entry_price=0.30, size=100.0)
        pnl = calculate_pnl(trade, settlement_value=1.0)
        assert pnl == pytest.approx(-100.0)


# ---------------------------------------------------------------------------
# execute_live_trade — phantom-fill regression test
# ---------------------------------------------------------------------------

class TestNoPhantomFillOnOrderFailure:

    def test_order_placement_exception_returns_none_not_filled(self):
        from backend.core.execution import execute_live_trade

        market = MagicMock()
        market.market_id = "KXHIGHNY-TEST-B90.0"
        market.city_key = "nyc"
        market.threshold_f = 90.0
        market.direction = "above"
        market.metric = "high"
        market.yes_price = 0.30
        market.no_price = 0.70
        market.target_date = None
        market.slug = "test-slug"
        market.platform = "kalshi"

        signal = MagicMock()
        signal.market = market
        signal.direction = "yes"
        signal.limit_price = 0.30

        with patch(
            "backend.core.execution._check_physical_guardrails",
            new=AsyncMock(return_value=None),  # guardrails clear
        ), patch(
            "backend.data.kalshi_client.KalshiClient"
        ) as MockClient:
            instance = MockClient.return_value
            instance.place_order = AsyncMock(side_effect=ConnectionError("Kalshi API unreachable"))

            result = asyncio.run(execute_live_trade(signal, 100.0))

        # Root-caused 2026-08-20: this used to return a Trade with
        # order_status="filled" here, fabricating a position that was never
        # actually placed. It must now return None, same as a guardrail
        # rejection, so the caller's `if trade is None: continue` catches it
        # and neither commits the trade nor debits bankroll for it.
        assert result is None


# ---------------------------------------------------------------------------
# reconcile_early_settlements
# ---------------------------------------------------------------------------

class TestReconcileEarlySettlements:

    def test_matching_official_result_marks_reconciled_no_correction(self):
        trade = _make_trade(settlement_value=1.0, pnl=400.0, result="win")
        db = _make_db([trade])
        state = _make_bot_state(bankroll=10000.0, total_pnl=400.0, winning_trades=5)
        db.query.side_effect = lambda m: (
            MagicMock(filter=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[trade]))))
            if getattr(m, "__name__", "") == "Trade"
            else MagicMock(first=MagicMock(return_value=state))
        )

        with patch(
            "backend.core.settlement.check_weather_settlement_source",
            new=AsyncMock(return_value=(True, 1.0)),  # official agrees: YES
        ):
            mismatched = asyncio.run(reconcile_early_settlements(db))

        assert mismatched == []
        assert trade.reconciled_at is not None
        assert trade.reconciliation_mismatch is False
        assert trade.pnl == pytest.approx(400.0)  # unchanged
        assert state.bankroll == pytest.approx(10000.0)  # unchanged

    def test_mismatch_corrects_pnl_bankroll_and_win_count(self):
        """Early call said WIN (settlement_value=1.0, pnl=+400); official says NO (0.0) -> should be a loss of the full $100 stake."""
        trade = _make_trade(
            direction="yes", entry_price=0.20, size=100.0,
            settlement_value=1.0, pnl=400.0, result="win",
        )
        state = _make_bot_state(bankroll=10400.0, total_pnl=400.0, winning_trades=5)

        def query_side_effect(model):
            name = getattr(model, "__name__", "")
            q = MagicMock()
            if name == "Trade":
                q.filter.return_value.all.return_value = [trade]
            elif name == "BotState":
                q.first.return_value = state
            elif name == "Signal":
                q.filter.return_value.first.return_value = None
            return q

        db = MagicMock()
        db.query.side_effect = query_side_effect

        with patch(
            "backend.core.settlement.check_weather_settlement_source",
            new=AsyncMock(return_value=(True, 0.0)),  # official disagrees: NO
        ):
            mismatched = asyncio.run(reconcile_early_settlements(db))

        assert len(mismatched) == 1
        assert trade.reconciliation_mismatch is True
        assert trade.result == "loss"
        assert trade.pnl == pytest.approx(-100.0)  # full stake lost, not +400
        # delta = corrected(-100) - original(+400) = -500
        assert state.total_pnl == pytest.approx(400.0 - 500.0)
        assert state.bankroll == pytest.approx(10400.0 - 500.0)
        assert state.winning_trades == 4  # decremented — was counted as a win, no longer is

    def test_not_yet_resolved_leaves_trade_untouched(self):
        trade = _make_trade(settlement_value=1.0, pnl=400.0, result="win")
        db = _make_db([trade])
        db.query.side_effect = lambda m: (
            MagicMock(filter=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[trade]))))
            if getattr(m, "__name__", "") == "Trade"
            else MagicMock(first=MagicMock(return_value=_make_bot_state()))
        )

        with patch(
            "backend.core.settlement.check_weather_settlement_source",
            new=AsyncMock(return_value=(False, None)),  # not resolved yet
        ):
            mismatched = asyncio.run(reconcile_early_settlements(db))

        assert mismatched == []
        assert trade.reconciled_at is None  # left pending, will retry later
        assert trade.pnl == pytest.approx(400.0)  # untouched
