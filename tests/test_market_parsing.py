import unittest
from datetime import date

from backend.data.weather_markets import (
    _parse_polymarket_weather,
    _parse_weather_market_title,
)


class WeatherMarketParsingTests(unittest.TestCase):
    def test_parse_title_extracts_city_temperature_metric_direction_and_date(self):
        parsed = _parse_weather_market_title(
            "Will Miami's low be below 65°F on September 7, 2026?"
        )

        self.assertEqual(parsed["city_key"], "miami")
        self.assertEqual(parsed["metric"], "low")
        self.assertEqual(parsed["direction"], "below")
        self.assertEqual(parsed["threshold_f"], 65.0)
        self.assertEqual(parsed["target_date"], date(2026, 9, 7))

    def test_parse_title_rejects_non_weather_or_unknown_city(self):
        self.assertIsNone(_parse_weather_market_title("Will the election happen on September 7, 2026?"))
        self.assertIsNone(_parse_weather_market_title("Seattle temperature above 70°F on September 7, 2026?"))

    def test_parse_market_accepts_json_prices_and_filters_closed_markets(self):
        market_data = {
            "id": 123,
            "question": "NYC high temperature above 70°F on September 7, 2026",
            "outcomePrices": '["0.60", "0.40"]',
            "volume": "250.5",
        }

        market = _parse_polymarket_weather(market_data, "weather-nyc")

        self.assertIsNotNone(market)
        self.assertEqual(market.market_id, "123")
        self.assertEqual(market.yes_price, 0.60)
        self.assertEqual(market.volume, 250.5)

        market_data["closed"] = True
        self.assertIsNone(_parse_polymarket_weather(market_data, "weather-nyc"))


if __name__ == "__main__":
    unittest.main()