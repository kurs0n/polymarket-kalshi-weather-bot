import unittest

from backend.core.settlement import _parse_market_resolution, calculate_pnl
from backend.models.database import Trade


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

    def test_calculate_pnl_supports_yes_no_and_up_down_directions(self):
        winning_yes = Trade(direction="yes", entry_price=0.40, size=25)
        losing_no = Trade(direction="no", entry_price=0.40, size=25)
        winning_up = Trade(direction="up", entry_price=0.40, size=25)

        self.assertEqual(calculate_pnl(winning_yes, 1.0), 15.0)
        self.assertEqual(calculate_pnl(losing_no, 1.0), -10.0)
        self.assertEqual(calculate_pnl(winning_up, 1.0), 15.0)


if __name__ == "__main__":
    unittest.main()