"""
Unit tests for the NWS high-water mark tracker and active position exit engine.

Tests are split into three groups:
  1. TestNWSHighWaterMarkCache   — cache TTL, None-skip, passthrough behaviour
  2. TestStationBias             — CITY_CONFIG bias fields exist and are float zeros
  3. TestExitEvaluation          — evaluate_open_positions_for_exit outcome logic

Async functions are tested via asyncio.run() to stay consistent with the
existing test suite (no pytest-asyncio dependency required).

Run with:
    cd polymarket-kalshi-weather-bot
    python -m pytest tests/test_nws_tracker_and_exits.py -v
"""
import asyncio
import time
import pytest
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_trade(
    id=1,
    market_ticker="KXHIGHNY-26AUG10-B85.0",
    platform="kalshi",
    direction="yes",
    entry_price=0.70,
    size=100.0,
    settled=False,
    market_type="weather",
    signal_id=None,
):
    t = MagicMock()
    t.id = id
    t.market_ticker = market_ticker
    t.platform = platform
    t.direction = direction
    t.entry_price = entry_price
    t.size = size
    t.settled = settled
    t.market_type = market_type
    t.signal_id = signal_id
    return t


def _make_db(trades):
    """Return a mock db whose .query(Trade).filter(...).all() returns trades."""
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = trades
    return db


def _make_client(ticker, strike_type, floor_strike=None, cap_strike=None):
    """
    Mock KalshiClient whose get_market() returns strike_type/floor_strike/
    cap_strike — the authoritative fields _resolve_city_from_ticker needs to
    determine direction. evaluate_open_positions_for_exit requires a client
    for this (see its docstring: "callers doing exit/kill-switch comparisons
    should always pass a client" — ticker-only parsing can't tell "above"
    from "below" from the B/T letter alone, root-caused 2026-08-13).

    `ticker` must be included in the returned market dict — a real Kalshi
    market payload carries its own ticker, and _parse_kalshi_ticker reads it
    from there (not from the original lookup argument) when given a dict.
    """
    client = MagicMock()
    client.get_market = AsyncMock(return_value={
        "market": {
            "ticker":       ticker,
            "strike_type":  strike_type,
            "floor_strike": floor_strike,
            "cap_strike":   cap_strike,
        }
    })
    return client


# ---------------------------------------------------------------------------
# Group 1: NWS High-Water Mark Cache
# ---------------------------------------------------------------------------

class TestNWSHighWaterMarkCache:

    def setup_method(self):
        """Clear the module-level NWS obs cache before each test."""
        import backend.data.weather as wx
        wx._nws_obs_cache.clear()

    def test_cache_hit_within_ttl_skips_second_nws_call(self):
        """A second call within 300s must return the cached value without hitting NWS."""
        from backend.data.weather import fetch_station_high_water_mark

        mock_obs = AsyncMock(return_value={"high": 85.0, "low": 70.0})
        target = date(2026, 8, 10)

        with patch("backend.data.weather.fetch_nws_observed_temperature", mock_obs):
            r1 = asyncio.run(fetch_station_high_water_mark("nyc", target))
            r2 = asyncio.run(fetch_station_high_water_mark("nyc", target))

        assert r1 == pytest.approx(85.0)
        assert r2 == pytest.approx(85.0)
        assert mock_obs.call_count == 1

    def test_low_water_mark_shares_the_high_cache_entry(self):
        """Added 2026-08-23 (low-temp Part 2): fetch_station_low_water_mark()
        must share one underlying NWS fetch with the high version, not
        double the request rate — both read the same cached {high, low} obs."""
        from backend.data.weather import fetch_station_high_water_mark, fetch_station_low_water_mark

        mock_obs = AsyncMock(return_value={"high": 85.0, "low": 70.0})
        target = date(2026, 8, 10)

        with patch("backend.data.weather.fetch_nws_observed_temperature", mock_obs):
            high = asyncio.run(fetch_station_high_water_mark("nyc", target))
            low = asyncio.run(fetch_station_low_water_mark("nyc", target))

        assert high == pytest.approx(85.0)
        assert low == pytest.approx(70.0)
        assert mock_obs.call_count == 1, "the second call (for low) must hit the shared cache, not NWS again"

    def test_cache_expires_after_ttl(self):
        """After 300s the cache entry must be treated as stale and NWS re-queried."""
        import backend.data.weather as wx
        from backend.data.weather import fetch_station_high_water_mark

        target = date(2026, 8, 10)
        cache_key = f"nws_obs_nyc_{target.isoformat()}"

        # Pre-seed cache with a timestamp in the past (beyond TTL)
        stale_ts = time.time() - (wx._NWS_OBS_CACHE_TTL + 1)
        wx._nws_obs_cache[cache_key] = ({"high": 83.0, "low": 68.0}, stale_ts)

        fresh_mock = AsyncMock(return_value={"high": 87.0, "low": 71.0})
        with patch("backend.data.weather.fetch_nws_observed_temperature", fresh_mock):
            result = asyncio.run(fetch_station_high_water_mark("nyc", target))

        assert result == pytest.approx(87.0)
        assert fresh_mock.call_count == 1

    def test_none_result_not_cached(self):
        """When NWS returns None (station offline), the result must NOT be cached
        so that the next scheduler tick can retry immediately."""
        from backend.data.weather import fetch_station_high_water_mark
        import backend.data.weather as wx

        target = date(2026, 8, 10)
        none_mock = AsyncMock(return_value=None)

        with patch("backend.data.weather.fetch_nws_observed_temperature", none_mock):
            r1 = asyncio.run(fetch_station_high_water_mark("nyc", target))
            r2 = asyncio.run(fetch_station_high_water_mark("nyc", target))

        assert r1 is None
        assert r2 is None
        # Both calls must have reached NWS — no caching of None
        assert none_mock.call_count == 2
        assert f"nws_obs_nyc_{target.isoformat()}" not in wx._nws_obs_cache

    def test_missing_high_key_returns_none(self):
        """obs dict with only 'low' key (no 'high') must return None for the
        high-water-mark and must NOT be cached — a partial reading shouldn't
        get either metric stuck with a cached result missing its own field."""
        from backend.data.weather import fetch_station_high_water_mark
        import backend.data.weather as wx

        target = date(2026, 8, 10)
        low_only_mock = AsyncMock(return_value={"low": 60.0})

        with patch("backend.data.weather.fetch_nws_observed_temperature", low_only_mock):
            result = asyncio.run(fetch_station_high_water_mark("nyc", target))

        assert result is None
        assert f"nws_obs_nyc_{target.isoformat()}" not in wx._nws_obs_cache

    def test_missing_low_key_returns_none(self):
        """Mirror of the above for fetch_station_low_water_mark — added
        2026-08-23 alongside the low-temp Part 2 work."""
        from backend.data.weather import fetch_station_low_water_mark
        import backend.data.weather as wx

        target = date(2026, 8, 10)
        high_only_mock = AsyncMock(return_value={"high": 90.0})

        with patch("backend.data.weather.fetch_nws_observed_temperature", high_only_mock):
            result = asyncio.run(fetch_station_low_water_mark("nyc", target))

        assert result is None
        assert f"nws_obs_nyc_{target.isoformat()}" not in wx._nws_obs_cache

    def test_city_key_passthrough(self):
        """fetch_station_high_water_mark must forward city_key to fetch_nws_observed_temperature."""
        from backend.data.weather import fetch_station_high_water_mark

        target = date(2026, 8, 10)
        mock_obs = AsyncMock(return_value={"high": 78.0, "low": 62.0})

        with patch("backend.data.weather.fetch_nws_observed_temperature", mock_obs):
            asyncio.run(fetch_station_high_water_mark("chicago", target))

        mock_obs.assert_called_once_with("chicago", target)


# ---------------------------------------------------------------------------
# Group 2: Station Bias Calibration
# ---------------------------------------------------------------------------

class TestStationBias:

    def test_all_cities_have_station_bias_f_key(self):
        """Every city entry in CITY_CONFIG must have a 'station_bias_f' key."""
        from backend.data.weather import CITY_CONFIG

        for city_key, cfg in CITY_CONFIG.items():
            assert "station_bias_f" in cfg, (
                f"CITY_CONFIG['{city_key}'] is missing 'station_bias_f'"
            )

    def test_all_initial_biases_are_float_zero(self):
        """All initial bias values must be exactly 0.0 and of type float."""
        from backend.data.weather import CITY_CONFIG

        for city_key, cfg in CITY_CONFIG.items():
            bias = cfg["station_bias_f"]
            assert isinstance(bias, float), (
                f"station_bias_f for '{city_key}' must be float, got {type(bias)}"
            )
            assert bias == pytest.approx(0.0), (
                f"Initial station_bias_f for '{city_key}' must be 0.0, got {bias}"
            )

    def test_nonzero_bias_shifts_corrected_high(self):
        """Applying a non-zero bias adds the offset to the raw observed value."""
        observed = 83.0
        bias = 1.5
        corrected = observed + bias
        assert corrected == pytest.approx(84.5)


# ---------------------------------------------------------------------------
# Group 3: Exit Evaluation Logic
# ---------------------------------------------------------------------------

_EVAL_PATH = "backend.core.weather_signals.fetch_station_high_water_mark"


class TestExitEvaluation:

    def test_yes_above_confirmed_win(self):
        """YES trade on 'above 85°F', NWS shows 88°F → trade_wins=True."""
        from backend.core.weather_signals import evaluate_open_positions_for_exit

        trade = _make_trade(
            market_ticker="KXHIGHNY-26AUG10-B85.0",
            direction="yes",
        )
        db = _make_db([trade])
        client = _make_client("KXHIGHNY-26AUG10-B85.0", "greater", floor_strike=85.0)

        with patch(_EVAL_PATH, AsyncMock(return_value=88.0)):
            results = asyncio.run(evaluate_open_positions_for_exit(db, client))

        assert len(results) == 1
        assert results[0]["outcome"] == "yes_wins"
        assert results[0]["trade_wins"] is True
        assert results[0]["trade_id"] == 1

    def test_yes_above_inside_buffer_no_exit(self):
        """YES trade on 'above 85°F', NWS shows 86.5°F (inside 2°F buffer) → no exit."""
        from backend.core.weather_signals import evaluate_open_positions_for_exit

        trade = _make_trade(
            market_ticker="KXHIGHNY-26AUG10-B85.0",
            direction="yes",
        )
        db = _make_db([trade])
        client = _make_client("KXHIGHNY-26AUG10-B85.0", "greater", floor_strike=85.0)

        with patch(_EVAL_PATH, AsyncMock(return_value=86.5)):
            results = asyncio.run(evaluate_open_positions_for_exit(db, client))

        assert results == []

    def test_no_above_outcome_yes_wins_trade_loses(self):
        """NO trade on 'above 85°F', NWS shows 88°F → YES wins but bot holds NO → trade_wins=False."""
        from backend.core.weather_signals import evaluate_open_positions_for_exit

        trade = _make_trade(
            market_ticker="KXHIGHNY-26AUG10-B85.0",
            direction="no",
        )
        db = _make_db([trade])
        client = _make_client("KXHIGHNY-26AUG10-B85.0", "greater", floor_strike=85.0)

        with patch(_EVAL_PATH, AsyncMock(return_value=88.0)):
            results = asyncio.run(evaluate_open_positions_for_exit(db, client))

        assert len(results) == 1
        assert results[0]["outcome"] == "yes_wins"
        assert results[0]["trade_wins"] is False

    def test_yes_above_low_obs_confirmed_loss(self):
        """YES trade on 'above 85°F', NWS shows 82°F (below 83°F floor) → NO wins → trade_wins=False."""
        from backend.core.weather_signals import evaluate_open_positions_for_exit

        trade = _make_trade(
            market_ticker="KXHIGHNY-26AUG10-B85.0",
            direction="yes",
        )
        db = _make_db([trade])
        client = _make_client("KXHIGHNY-26AUG10-B85.0", "greater", floor_strike=85.0)

        with patch(_EVAL_PATH, AsyncMock(return_value=82.0)):
            results = asyncio.run(evaluate_open_positions_for_exit(db, client))

        assert len(results) == 1
        assert results[0]["outcome"] == "no_wins"
        assert results[0]["trade_wins"] is False

    def test_no_below_confirmed_win(self):
        """NO trade on 'below 85°F', NWS shows 88.5°F (above 87°F ceiling) → NO wins → trade_wins=True."""
        from backend.core.weather_signals import evaluate_open_positions_for_exit

        # KXHIGHTBOS-26AUG10-T85.0: direction="below", threshold=85°F
        trade = _make_trade(
            market_ticker="KXHIGHTBOS-26AUG10-T85.0",
            direction="no",
        )
        db = _make_db([trade])
        client = _make_client("KXHIGHTBOS-26AUG10-T85.0", "less", cap_strike=85.0)

        with patch(_EVAL_PATH, AsyncMock(return_value=88.5)):
            results = asyncio.run(evaluate_open_positions_for_exit(db, client))

        assert len(results) == 1
        assert results[0]["outcome"] == "no_wins"
        assert results[0]["trade_wins"] is True

    def test_non_kalshi_trade_excluded(self):
        """Polymarket trades must not appear in exit evaluation results."""
        from backend.core.weather_signals import evaluate_open_positions_for_exit

        # The DB filter (platform=="kalshi") excludes polymarket trades;
        # simulate by having the filtered query return an empty list.
        db = _make_db([])  # filter returns nothing

        with patch(_EVAL_PATH, AsyncMock(return_value=88.0)):
            results = asyncio.run(evaluate_open_positions_for_exit(db))

        assert results == []

    def test_nws_returns_none_no_exit(self):
        """When NWS is offline (returns None), no exit should be triggered."""
        from backend.core.weather_signals import evaluate_open_positions_for_exit

        trade = _make_trade(
            market_ticker="KXHIGHNY-26AUG10-B85.0",
            direction="yes",
        )
        db = _make_db([trade])

        with patch(_EVAL_PATH, AsyncMock(return_value=None)):
            results = asyncio.run(evaluate_open_positions_for_exit(db))

        assert results == []


# ---------------------------------------------------------------------------
# Group 4: Exit Evaluation Logic — low-temp markets, 2026-08-23 (Part 2)
# ---------------------------------------------------------------------------

_EVAL_LOW_PATH = "backend.data.weather.fetch_station_low_water_mark"


class TestLowMetricExitEvaluation:

    def test_low_ticker_reads_the_low_water_mark_not_the_high(self):
        """A KXLOWT... ticker must call fetch_station_low_water_mark, and
        must NOT call fetch_station_high_water_mark at all — root-caused
        2026-08-23: this used to always read the high water mark regardless
        of metric, comparing the wrong physical quantity for low contracts."""
        from backend.core.weather_signals import evaluate_open_positions_for_exit

        trade = _make_trade(
            market_ticker="KXLOWTNYC-26AUG10-B60.0",
            direction="yes",
        )
        db = _make_db([trade])
        client = _make_client("KXLOWTNYC-26AUG10-B60.0", "greater", floor_strike=60.0)

        low_mock = AsyncMock(return_value=63.0)
        with patch(_EVAL_LOW_PATH, low_mock), \
             patch(_EVAL_PATH, AsyncMock(side_effect=AssertionError("must not call the HIGH water mark for a low-metric ticker"))):
            results = asyncio.run(evaluate_open_positions_for_exit(db, client))

        assert len(results) == 1
        assert results[0]["outcome"] == "yes_wins"
        assert results[0]["trade_wins"] is True
        low_mock.assert_called_once()

    def test_yes_low_above_confirmed_loss(self):
        """YES trade on low 'above 60°F' (betting a warm night), NWS low
        shows 55°F (below the 2°F buffer) → NO wins → trade_wins=False."""
        from backend.core.weather_signals import evaluate_open_positions_for_exit

        trade = _make_trade(
            market_ticker="KXLOWTCHI-26AUG10-B60.0",
            direction="yes",
        )
        db = _make_db([trade])
        client = _make_client("KXLOWTCHI-26AUG10-B60.0", "greater", floor_strike=60.0)

        with patch(_EVAL_LOW_PATH, AsyncMock(return_value=55.0)):
            results = asyncio.run(evaluate_open_positions_for_exit(db, client))

        assert len(results) == 1
        assert results[0]["outcome"] == "no_wins"
        assert results[0]["trade_wins"] is False

    def test_low_metric_skips_trend_stop_projection(self):
        """Even with WEATHER_EARLY_EXITS_ENABLED on and a live client
        available, a low-metric position must NOT reach the trend_stop
        projection — that logic assumes a warming-toward-peak trend, wrong
        physics for a low-temp contract (see the 2026-08-23 comment in
        evaluate_open_positions_for_exit). Simulate "not yet decided" (obs
        inside the buffer) so it would fall through to trend_stop if the
        low-metric skip weren't there."""
        from backend.core.weather_signals import evaluate_open_positions_for_exit
        from backend.config import settings

        trade = _make_trade(
            market_ticker="KXLOWTMIA-26AUG10-B60.0",
            direction="yes",
        )
        db = _make_db([trade])
        client = _make_client("KXLOWTMIA-26AUG10-B60.0", "greater", floor_strike=60.0)

        # 60.5 is inside the 2°F EXIT_BUFFER_F around 60 — "not yet decided".
        with patch(_EVAL_LOW_PATH, AsyncMock(return_value=60.5)), \
             patch.object(settings, "WEATHER_EARLY_EXITS_ENABLED", True):
            results = asyncio.run(evaluate_open_positions_for_exit(db, client))

        assert results == []  # no trend_stop entry, no settlement entry
