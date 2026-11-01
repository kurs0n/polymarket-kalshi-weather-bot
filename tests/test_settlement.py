import asyncio
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.core.settlement import (
    _parse_market_resolution,
    calculate_pnl,
    settle_pending_trades,
)
from backend.models.database import Base, Signal, Trade


class SettlementTests(unittest.TestCase):
    def test_parse_market_resolution_handles_yes_no_and_unresolved_markets(self):
        self.assertEqual(
            _parse_market_resolution({"closed": True, "outcomePrices": ["1", "0"]}),
            (True, 1.0),
        )
        self.assertEqual(
            _parse_market_resolution({"closed": True, "outcomePrices": '["0", "1"]'}),
            (True, 0.0),
        )
        self.assertEqual(
            _parse_market_resolution({"closed": False, "outcomePrices": ["1", "0"]}),
            (False, None),
        )

    def test_parse_market_resolution_rejects_invalid_prices(self):
        self.assertEqual(
            _parse_market_resolution({"closed": True, "outcomePrices": ["not-a-price"]}),
            (False, None),
        )

    def test_calculate_pnl_supports_canonical_yes_no_directions(self):
        winning_yes = Trade(direction="yes", entry_price=0.40, size=25)
        losing_no = Trade(direction="no", entry_price=0.40, size=25)

        self.assertEqual(calculate_pnl(winning_yes, 1.0), 15.0)
        self.assertEqual(calculate_pnl(losing_no, 1.0), -10.0)

    def test_calculate_pnl_rejects_legacy_direction_values(self):
        legacy_trade = Trade(direction="up", entry_price=0.40, size=25)

        with self.assertRaises(ValueError):
            calculate_pnl(legacy_trade, 1.0)


class SignalOutcomeConsistencyTests(unittest.TestCase):
    """`outcome_correct` must always agree with `direction` and the settlement value.

    Settlement stores the linked signal's verdict as
    ``direction == actual_outcome``, where ``actual_outcome`` is derived from
    ``settlement_value`` (1.0 -> yes, 0.0 -> no). These tests drive the real
    ``settle_pending_trades`` path with only the platform resolution mocked, so
    the invariant is checked against production code rather than a hand-built
    fixture.
    """

    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=self.engine)
        self.db = sessionmaker(bind=self.engine)()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _settle_one(self, direction: str, settlement_value: float):
        signal = Signal(
            market_ticker=f"M-{direction}-{settlement_value}",
            platform="polymarket",
            direction=direction,
            model_probability=0.6,
            market_price=0.4,
            edge=0.2,
        )
        self.db.add(signal)
        self.db.commit()

        trade = Trade(
            signal_id=signal.id,
            market_ticker=signal.market_ticker,
            platform="polymarket",
            direction=direction,
            entry_price=0.4,
            size=25,
            settled=False,
        )
        self.db.add(trade)
        self.db.commit()

        async def fake_resolution(pending_trade):
            return True, settlement_value, calculate_pnl(pending_trade, settlement_value)

        with patch(
            "backend.core.settlement.check_weather_settlement",
            side_effect=fake_resolution,
        ):
            settled = asyncio.run(settle_pending_trades(self.db))

        self.assertEqual([t.id for t in settled], [trade.id])
        return self.db.get(Signal, signal.id), self.db.get(Trade, trade.id)

    def test_outcome_correct_matches_direction_and_settlement_value(self):
        cases = [
            ("yes", 1.0, True),
            ("yes", 0.0, False),
            ("no", 1.0, False),
            ("no", 0.0, True),
        ]

        for direction, settlement_value, expected_correct in cases:
            with self.subTest(direction=direction, settlement_value=settlement_value):
                signal, trade = self._settle_one(direction, settlement_value)
                expected_outcome = "yes" if settlement_value == 1.0 else "no"

                self.assertEqual(signal.actual_outcome, expected_outcome)
                self.assertEqual(signal.settlement_value, settlement_value)
                self.assertIsNotNone(signal.settled_at)
                self.assertEqual(signal.outcome_correct, expected_correct)

                # The invariant itself, expressed independently of the fixture.
                self.assertEqual(
                    signal.outcome_correct,
                    signal.direction == signal.actual_outcome,
                )

                self.assertTrue(trade.settled)
                self.assertEqual(trade.settlement_value, settlement_value)
                self.assertIsNotNone(trade.settlement_time)
                self.assertEqual(trade.result, "win" if expected_correct else "loss")


if __name__ == "__main__":
    unittest.main()
