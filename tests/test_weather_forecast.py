import unittest
from datetime import date

from backend.data.weather import EnsembleForecast


class EnsembleForecastTests(unittest.TestCase):
    def setUp(self):
        self.forecast = EnsembleForecast(
            city_key="nyc",
            city_name="New York City",
            target_date=date(2026, 9, 7),
            member_highs=[70.0, 72.0, 75.0, 80.0],
            member_lows=[55.0, 60.0, 62.0, 65.0],
        )

    def test_summary_statistics_are_calculated_from_members(self):
        self.assertEqual(self.forecast.num_members, 4)
        self.assertAlmostEqual(self.forecast.mean_high, 74.25)
        self.assertAlmostEqual(self.forecast.mean_low, 60.5)

    def test_probability_helpers_count_strictly_above_threshold(self):
        self.assertAlmostEqual(self.forecast.probability_high_above(72.0), 0.5)
        self.assertAlmostEqual(self.forecast.probability_high_below(72.0), 0.5)
        self.assertAlmostEqual(self.forecast.probability_low_above(60.0), 0.5)
        self.assertAlmostEqual(self.forecast.probability_low_below(60.0), 0.5)

    def test_empty_forecast_uses_neutral_probabilities(self):
        forecast = EnsembleForecast(
            city_key="nyc",
            city_name="New York City",
            target_date=date(2026, 9, 7),
            member_highs=[],
            member_lows=[],
        )

        self.assertEqual(forecast.probability_high_above(70), 0.5)
        self.assertEqual(forecast.probability_low_below(50), 0.5)
        self.assertEqual(forecast.ensemble_agreement, 0.5)


if __name__ == "__main__":
    unittest.main()