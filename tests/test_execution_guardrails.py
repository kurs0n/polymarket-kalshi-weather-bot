"""
Unit tests for the three physical execution guardrails.

Tests mock:
  - `fetch_metar_current` — controls live temperature + trend
  - `_fetch_live_ask`     — isolates guardrail logic from order-book IO
  - local time (via ZoneInfo-aware datetime.now mocking)

Each test names the guardrail it exercises so failures are self-documenting.
"""
import asyncio
from datetime import datetime, date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.data.weather import METARSnapshot
from backend.core.execution import (
    _check_physical_guardrails,
    _KILL_SWITCH_BUFFER_F,
    _LOW_PROB_TAIL_THRESHOLD,
    _TRADE_WINDOW_START_H,
    _TRADE_WINDOW_END_H,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_market(
    threshold_f: float,
    direction: str = "above",
    metric: str = "high",
    yes_price: float = 0.40,
    target_date: date = None,
    city_key: str = "nyc",
):
    m = MagicMock()
    m.market_id   = f"KXHIGHNY-TEST-B{threshold_f}"
    m.city_key    = city_key
    m.threshold_f = threshold_f
    m.direction   = direction
    m.metric      = metric
    m.yes_price   = yes_price
    m.no_price    = round(1.0 - yes_price, 2)
    m.target_date = target_date or date.today()
    return m


def _make_signal(market, trade_direction: str = "yes"):
    s = MagicMock()
    s.market    = market
    s.direction = trade_direction
    return s


def _make_metar(temp_f: float, trend: float = 0.0, city_key: str = "nyc") -> METARSnapshot:
    return METARSnapshot(
        city_key=city_key,
        station="KNYC",
        observed_temp_f=temp_f,
        trend_f_per_hour=trend,
        observed_at=datetime.utcnow(),
    )


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Guardrail 1 — Execution time window
# ---------------------------------------------------------------------------

class TestTimeWindowGuardrail:
    """Same-day contracts may only execute during _TRADE_WINDOW_START_H – _TRADE_WINDOW_END_H."""

    def _run_with_hour(self, local_hour: int, target_date=None):
        """Patch datetime.now to return a fixed local hour for NYC."""
        market  = _make_market(90.0, target_date=target_date or date.today())
        signal  = _make_signal(market)

        mock_dt = MagicMock()
        mock_dt.hour = local_hour

        with patch(
            "backend.core.execution.datetime"
        ) as mock_datetime, patch(
            "backend.data.weather.fetch_metar_current",
            new=AsyncMock(return_value=None),  # METAR unavailable; only time gate fires
        ):
            mock_datetime.now.return_value = mock_dt
            mock_datetime.utcnow.return_value = datetime.utcnow()
            result = run(_check_physical_guardrails(signal))

        return result

    def test_before_window_is_rejected(self):
        """Hour 09 < TRADE_WINDOW_START_H → TIME_GATE fires."""
        reject = self._run_with_hour(9)
        assert reject is not None
        assert "TIME_GATE" in reject

    def test_after_window_is_rejected(self):
        """Hour 16 > TRADE_WINDOW_END_H → TIME_GATE fires."""
        reject = self._run_with_hour(16)
        assert reject is not None
        assert "TIME_GATE" in reject

    def test_at_window_start_passes(self):
        """Hour == TRADE_WINDOW_START_H → time gate passes."""
        reject = self._run_with_hour(_TRADE_WINDOW_START_H)
        # Should be None (METAR unavailable means other checks also skip)
        assert reject is None

    def test_at_window_end_passes(self):
        """Hour == TRADE_WINDOW_END_H → time gate passes."""
        reject = self._run_with_hour(_TRADE_WINDOW_END_H)
        assert reject is None

    def test_next_day_contract_skips_time_gate(self):
        """Tomorrow's contract is not same-day → time gate does not fire."""
        tomorrow = date.today() + timedelta(days=1)
        reject = self._run_with_hour(9, target_date=tomorrow)
        # Gate must not fire — only METAR checks (skipped because METAR=None)
        assert reject is None


# ---------------------------------------------------------------------------
# Guardrail 2 — Absolute floor kill switch
# ---------------------------------------------------------------------------

class TestKillSwitch:
    """
    When betting high stays below a ceiling:
      current_temp >= ceiling − KILL_SWITCH_BUFFER_F  → KILL_SWITCH rejection.
    """

    def _check(self, current_temp_f, ceiling_f, trade_dir="no", market_dir="above"):
        """
        Default: NO on an ABOVE market = betting high stays below ceiling.
        Patches the hour to be inside the execution window so only the kill
        switch is tested.  Also mocks _hours_until_diurnal_peak=0.0 so that
        the velocity guard (which shares the datetime mock) does not interfere.
        """
        market = _make_market(ceiling_f, direction=market_dir)
        signal = _make_signal(market, trade_direction=trade_dir)
        metar  = _make_metar(current_temp_f, trend=0.0)

        mock_dt = MagicMock()
        mock_dt.hour = _TRADE_WINDOW_START_H  # inside window

        with patch("backend.core.execution.datetime") as mock_datetime, \
             patch("backend.data.weather.fetch_metar_current",
                   new=AsyncMock(return_value=metar)), \
             patch("backend.core.execution._hours_until_diurnal_peak",
                   return_value=0.0):  # isolate kill switch from velocity calc
            mock_datetime.now.return_value = mock_dt
            mock_datetime.utcnow.return_value = datetime.utcnow()
            return run(_check_physical_guardrails(signal))

    def test_temp_exactly_at_threshold_fires(self):
        """current_temp == ceiling − buffer → KILL_SWITCH fires (≥ boundary)."""
        ceiling = 87.5
        current = ceiling - _KILL_SWITCH_BUFFER_F  # exactly at boundary
        reject = self._check(current, ceiling)
        assert reject is not None
        assert "KILL_SWITCH" in reject

    def test_temp_above_threshold_fires(self):
        """current_temp clearly above threshold → KILL_SWITCH fires."""
        reject = self._check(current_temp_f=87.0, ceiling_f=85.0)
        assert reject is not None
        assert "KILL_SWITCH" in reject

    def test_temp_safely_below_threshold_passes(self):
        """current_temp well below ceiling → no kill switch."""
        ceiling = 90.0
        current = ceiling - _KILL_SWITCH_BUFFER_F - 5.0  # 5°F headroom
        reject = self._check(current, ceiling)
        assert reject is None

    def test_kill_switch_logs_station_and_temp(self):
        """Rejection message must include station name and the live temperature."""
        reject = self._check(current_temp_f=88.0, ceiling_f=86.0)
        assert "KNYC" in reject
        assert "88.0" in reject

    def test_kill_switch_does_not_fire_for_above_bets(self):
        """
        Buying YES on ABOVE market: we WIN if temp goes UP. Kill switch must
        not fire even when temp is near the threshold.
        """
        # YES on ABOVE = betting high exceeds ceiling; kill switch only guards
        # the opposite (betting below stays safe)
        market = _make_market(85.0, direction="above")
        signal = _make_signal(market, trade_direction="yes")
        # Current temp 84.9°F — 0.1°F below ceiling of 85°F
        metar = _make_metar(84.9, trend=0.0)

        mock_dt = MagicMock()
        mock_dt.hour = _TRADE_WINDOW_START_H

        with patch("backend.core.execution.datetime") as mock_datetime, \
             patch("backend.data.weather.fetch_metar_current",
                   new=AsyncMock(return_value=metar)), \
             patch("backend.core.execution._hours_until_diurnal_peak",
                   return_value=0.0):
            mock_datetime.now.return_value = mock_dt
            mock_datetime.utcnow.return_value = datetime.utcnow()
            reject = run(_check_physical_guardrails(signal))

        assert reject is None


# ---------------------------------------------------------------------------
# Guardrail 3 — Warming velocity kill switch
# ---------------------------------------------------------------------------

class TestVelocityKillSwitch:
    """
    current_temp + hours_to_peak × trend > ceiling → VELOCITY_KILL.
    We mock _hours_until_diurnal_peak to return a fixed value.
    """

    def _check(self, current_temp_f, trend_f_hr, hours_to_peak, ceiling_f):
        market = _make_market(ceiling_f, direction="above")
        signal = _make_signal(market, trade_direction="no")  # NO on above = below bet
        metar  = _make_metar(current_temp_f, trend=trend_f_hr)

        mock_dt = MagicMock()
        mock_dt.hour = _TRADE_WINDOW_START_H

        with patch("backend.core.execution.datetime") as mock_datetime, \
             patch("backend.data.weather.fetch_metar_current",
                   new=AsyncMock(return_value=metar)), \
             patch("backend.core.execution._hours_until_diurnal_peak",
                   return_value=hours_to_peak):
            mock_datetime.now.return_value = mock_dt
            mock_datetime.utcnow.return_value = datetime.utcnow()
            return run(_check_physical_guardrails(signal))

    def test_projected_peak_exceeds_ceiling_is_rejected(self):
        """82°F + 3h × 2.5°F/hr = 89.5°F > ceiling 88°F → VELOCITY_KILL."""
        reject = self._check(
            current_temp_f=82.0, trend_f_hr=2.5, hours_to_peak=3.0, ceiling_f=88.0
        )
        assert reject is not None
        assert "VELOCITY_KILL" in reject

    def test_projected_peak_below_ceiling_passes(self):
        """82°F + 3h × 1.0°F/hr = 85°F < ceiling 88°F → no velocity kill."""
        reject = self._check(
            current_temp_f=82.0, trend_f_hr=1.0, hours_to_peak=3.0, ceiling_f=88.0
        )
        assert reject is None

    def test_cooling_trend_does_not_fire(self):
        """Negative trend (cooling) → velocity guard must not trigger."""
        reject = self._check(
            current_temp_f=86.0, trend_f_hr=-1.5, hours_to_peak=3.0, ceiling_f=88.0
        )
        assert reject is None

    def test_past_peak_does_not_fire(self):
        """hours_to_peak = 0 (peak already past) → velocity guard must not fire."""
        reject = self._check(
            current_temp_f=84.0, trend_f_hr=3.0, hours_to_peak=0.0, ceiling_f=88.0
        )
        assert reject is None

    def test_velocity_message_includes_projection(self):
        """Rejection string must include the projected peak temperature."""
        reject = self._check(
            current_temp_f=82.0, trend_f_hr=2.5, hours_to_peak=3.0, ceiling_f=88.0
        )
        # Projected = 82 + 3 × 2.5 = 89.5
        assert "89.5" in reject


# ---------------------------------------------------------------------------
# Guardrail 4 — Low-probability tail guard
# ---------------------------------------------------------------------------

class TestTailRiskGuard:
    """
    entry_price < 0.20 AND below-ceiling bet AND warming trend → TAIL_GUARD.
    """

    def _check(
        self,
        yes_price: float,
        trade_dir: str,
        market_dir: str,
        trend: float,
    ):
        market = _make_market(90.0, direction=market_dir, yes_price=yes_price)
        signal = _make_signal(market, trade_direction=trade_dir)
        metar  = _make_metar(78.0, trend=trend)  # temp well below ceiling

        mock_dt = MagicMock()
        mock_dt.hour = _TRADE_WINDOW_START_H

        with patch("backend.core.execution.datetime") as mock_datetime, \
             patch("backend.data.weather.fetch_metar_current",
                   new=AsyncMock(return_value=metar)), \
             patch("backend.core.execution._hours_until_diurnal_peak",
                   return_value=2.0):
            mock_datetime.now.return_value = mock_dt
            mock_datetime.utcnow.return_value = datetime.utcnow()
            return run(_check_physical_guardrails(signal))

    def test_cheap_no_on_above_market_plus_warming_rejected(self):
        """
        Buying NO (below-ceiling bet) at <20¢ + warming trend → TAIL_GUARD.
        NO price = 1 − yes_price; use yes_price = 0.88 so no_price = 0.12.
        """
        reject = self._check(
            yes_price=0.88, trade_dir="no", market_dir="above", trend=1.5
        )
        assert reject is not None
        assert "TAIL_GUARD" in reject

    def test_cheap_yes_on_below_market_plus_warming_rejected(self):
        """Buying YES on BELOW market at 10¢ + warming → TAIL_GUARD."""
        reject = self._check(
            yes_price=0.10, trade_dir="yes", market_dir="below", trend=1.2
        )
        assert reject is not None
        assert "TAIL_GUARD" in reject

    def test_cooling_trend_exempts_tail_guard(self):
        """Same cheap contract but temperature is FALLING → tail guard must not fire."""
        reject = self._check(
            yes_price=0.88, trade_dir="no", market_dir="above", trend=-1.0
        )
        assert reject is None

    def test_price_above_threshold_exempts_tail_guard(self):
        """Entry price >= 0.20 → tail guard does not fire even with warming."""
        reject = self._check(
            yes_price=0.75, trade_dir="no", market_dir="above", trend=2.0
        )
        # no_price = 0.25 ≥ 0.20 — guard should not fire
        assert reject is None

    def test_above_bet_warming_does_not_trigger_tail_guard(self):
        """
        Buying YES on ABOVE market (betting temp goes UP) + warming trend →
        tail guard must not fire — warming HELPS this position.
        """
        # yes_price=0.10 (cheap), YES on ABOVE = we WIN if temp exceeds ceiling
        reject = self._check(
            yes_price=0.10, trade_dir="yes", market_dir="above", trend=2.0
        )
        assert reject is None


# ---------------------------------------------------------------------------
# Integration — execute_paper_trade and execute_live_trade both enforce guardrails
# ---------------------------------------------------------------------------

class TestGuardrailIntegration:
    """
    Confirm that both entry-point functions return None when any guardrail fires.
    These tests mock _check_physical_guardrails directly to isolate the plumbing.
    """

    def test_paper_trade_returns_none_on_guardrail(self):
        from backend.core.execution import execute_paper_trade

        market = _make_market(90.0)
        signal = _make_signal(market)

        with patch(
            "backend.core.execution._check_physical_guardrails",
            new=AsyncMock(return_value="KILL_SWITCH: test"),
        ):
            result = asyncio.run(execute_paper_trade(signal, 50.0))

        assert result is None

    def test_live_trade_returns_none_on_guardrail(self):
        from backend.core.execution import execute_live_trade

        market = _make_market(90.0)
        signal = _make_signal(market)
        signal.limit_price = 0.45

        with patch(
            "backend.core.execution._check_physical_guardrails",
            new=AsyncMock(return_value="TIME_GATE: test"),
        ):
            result = asyncio.run(execute_live_trade(signal, 50.0))

        assert result is None

    def test_paper_trade_proceeds_when_guardrails_clear(self):
        from backend.core.execution import execute_paper_trade

        market = _make_market(90.0, yes_price=0.55)
        signal = _make_signal(market)
        signal.model_probability = 0.70
        signal.market_probability = 0.55
        signal.edge = 0.15

        with patch(
            "backend.core.execution._check_physical_guardrails",
            new=AsyncMock(return_value=None),
        ), patch(
            "backend.core.execution._fetch_live_ask",
            new=AsyncMock(return_value=(0.57, None)),
        ):
            result = asyncio.run(execute_paper_trade(signal, 50.0))

        assert result is not None
        assert result.entry_price == pytest.approx(0.57)
