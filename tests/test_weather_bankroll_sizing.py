import asyncio
import unittest
from datetime import date
from unittest.mock import MagicMock, patch

from backend.core import weather_signals
from backend.data.weather import EnsembleForecast
from backend.data.weather_markets import WeatherMarket


class WeatherBankrollSizingTests(unittest.TestCase):
    def test_current_bankroll_is_read_from_bot_state(self):
        state = MagicMock(bankroll=4321.50)
        db = MagicMock()
        db.query.return_value.first.return_value = state

        with patch.object(weather_signals, "SessionLocal", return_value=db):
            bankroll = weather_signals._get_current_bankroll()

        self.assertEqual(bankroll, 4321.50)
        db.close.assert_called_once_with()

    def test_initial_bankroll_is_used_when_state_is_unavailable(self):
        db = MagicMock()
        db.query.side_effect = RuntimeError("database unavailable")

        with patch.object(weather_signals, "SessionLocal", return_value=db), patch.object(
            weather_signals.settings, "INITIAL_BANKROLL", 10000.0
        ):
            bankroll = weather_signals._get_current_bankroll()

        self.assertEqual(bankroll, 10000.0)
        db.close.assert_called_once_with()

    def test_suggested_size_scales_with_current_bankroll(self):
        market = WeatherMarket(
            slug="weather-test",
            market_id="weather-test",
            platform="polymarket",
            title="High temperature above 70F",
            city_key="nyc",
            city_name="New York City",
            target_date=date(2026, 9, 7),
            threshold_f=70.0,
            metric="high",
            direction="above",
            yes_price=0.50,
            no_price=0.50,
        )
        forecast = EnsembleForecast(
            city_key="nyc",
            city_name="New York City",
            target_date=market.target_date,
            member_highs=[75.0] * 20 + [65.0] * 11,
            member_lows=[60.0] * 31,
        )

        async def run_signal(bankroll):
            with patch.object(
                weather_signals, "fetch_ensemble_forecast", return_value=forecast
            ), patch.object(
                weather_signals, "_get_current_bankroll", return_value=bankroll
            ), patch.object(
                weather_signals.settings, "WEATHER_MAX_ENTRY_PRICE", 0.70
            ), patch.object(
                weather_signals.settings, "WEATHER_MAX_TRADE_SIZE", 1000.0
            ), patch.object(
                weather_signals.settings, "MAX_TRADE_SIZE", 1000.0
            ):
                return await weather_signals.generate_weather_signal(market)

        full_bankroll_signal = asyncio.run(run_signal(10000.0))
        half_bankroll_signal = asyncio.run(run_signal(5000.0))

        self.assertIsNotNone(full_bankroll_signal)
        self.assertIsNotNone(half_bankroll_signal)
        self.assertGreater(full_bankroll_signal.suggested_size, 0)
        self.assertAlmostEqual(
            half_bankroll_signal.suggested_size,
            full_bankroll_signal.suggested_size / 2,
        )


if __name__ == "__main__":
    unittest.main()
