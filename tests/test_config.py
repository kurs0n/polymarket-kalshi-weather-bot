import unittest

from pydantic import ValidationError

from backend.config import Settings


class SettingsValidationTests(unittest.TestCase):
    def test_valid_settings_normalize_weather_cities(self):
        settings = Settings(WEATHER_CITIES=" nyc, chicago ")

        self.assertEqual(settings.WEATHER_CITIES, "nyc,chicago")
        self.assertEqual(settings.weather_city_list, ["nyc", "chicago"])

    def test_rejects_invalid_risk_and_price_values(self):
        with self.assertRaises(ValidationError):
            Settings(KELLY_FRACTION=1.1)

        with self.assertRaises(ValidationError):
            Settings(WEATHER_MAX_ENTRY_PRICE=-0.1)

        with self.assertRaises(ValidationError):
            Settings(INITIAL_BANKROLL=0)

    def test_rejects_empty_and_normalizes_duplicate_weather_cities(self):
        with self.assertRaises(ValidationError):
            Settings(WEATHER_CITIES="  ")

        settings = Settings(WEATHER_CITIES="nyc,nyc,chicago")
        self.assertEqual(settings.weather_city_list, ["nyc", "chicago"])


if __name__ == "__main__":
    unittest.main()