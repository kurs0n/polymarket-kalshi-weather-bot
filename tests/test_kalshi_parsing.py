"""Kalshi weather ticker parsing tests."""

import unittest
from datetime import date

from backend.data.kalshi_markets import _parse_kalshi_ticker


class KalshiTickerParsingTests(unittest.TestCase):
    def test_bottom_boundary_is_above(self):
        parsed = _parse_kalshi_ticker("KXHIGHNY-26MAR01-B45.5", "nyc")

        self.assertEqual(parsed["target_date"], date(2026, 3, 1))
        self.assertEqual(parsed["threshold_f"], 45.5)
        self.assertEqual(parsed["metric"], "high")
        self.assertEqual(parsed["direction"], "above")

    def test_top_boundary_is_below(self):
        parsed = _parse_kalshi_ticker("KXHIGHTBOS-26DEC31-T94", "boston")

        self.assertEqual(parsed["target_date"], date(2026, 12, 31))
        self.assertEqual(parsed["threshold_f"], 94.0)
        self.assertEqual(parsed["direction"], "below")

    def test_invalid_tickers_are_rejected(self):
        for ticker in (
            "not-a-ticker",
            "KXHIGHNY-26XXX01-B45.5",
            "KXHIGHNY-26FEB30-B45.5",
            "KXHIGHNY-26MAR01-X45.5",
        ):
            with self.subTest(ticker=ticker):
                self.assertIsNone(_parse_kalshi_ticker(ticker, "nyc"))


if __name__ == "__main__":
    unittest.main()
