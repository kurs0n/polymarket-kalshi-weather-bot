import unittest
from unittest.mock import patch

from backend.core.sizing import calculate_edge, calculate_kelly_size


class SizingTests(unittest.TestCase):
    def test_calculate_edge_prefers_yes_when_model_probability_is_higher(self):
        edge, direction = calculate_edge(0.70, 0.50)

        self.assertAlmostEqual(edge, 0.20)
        self.assertEqual(direction, "up")

    def test_calculate_edge_prefers_no_when_model_probability_is_lower(self):
        edge, direction = calculate_edge(0.30, 0.50)

        self.assertAlmostEqual(edge, 0.20)
        self.assertEqual(direction, "down")

    def test_kelly_size_is_zero_for_invalid_or_unprofitable_trade(self):
        with patch("backend.core.sizing.settings.KELLY_FRACTION", 0.15):
            self.assertEqual(calculate_kelly_size(0.0, 0.50, 0.50, "up", 1000), 0)
            self.assertEqual(calculate_kelly_size(0.1, 0.50, 0.0, "up", 1000), 0)
            self.assertEqual(calculate_kelly_size(0.1, 0.50, 1.0, "up", 1000), 0)

    def test_kelly_size_respects_fractional_kelly_and_trade_cap(self):
        with patch("backend.core.sizing.settings.KELLY_FRACTION", 1.0), patch(
            "backend.core.sizing.settings.MAX_TRADE_SIZE", 100.0
        ):
            size = calculate_kelly_size(0.8, 0.95, 0.50, "up", 10000)

        self.assertEqual(size, 100.0)


if __name__ == "__main__":
    unittest.main()