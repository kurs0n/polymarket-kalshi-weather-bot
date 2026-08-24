"""
Unit tests for the physical execution guardrails still on the entry path:
absolute floor kill switch, and the low-probability tail guard.

Two guardrails formerly covered here — a same-day TIME_GATE (11h-14h local)
and an entry-side VELOCITY_KILL projection — were deliberately removed from
_check_physical_guardrails on 2026-08-17 per user feedback: entering early
on a real edge is the point, and blocking entry to protect one
unreliable-before-11h check threw away that edge for no real safety
benefit. The velocity projection lives on the exit side now instead (see
weather_signals.py's evaluate_open_positions_for_exit / its "trend_stop"
exit_type, which watches an OPEN position's trajectory rather than refusing
to open one) — see execution.py's 2026-08-17 note above _DIURNAL_PEAK_HOUR
for the full rationale.

Tests mock:
  - `fetch_metar_current` — controls live temperature + trend
  - `_fetch_live_ask`     — isolates guardrail logic from order-book IO

Each test names the guardrail it exercises so failures are self-documenting.
"""
import asyncio
from datetime import datetime, date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.data.weather import METARSnapshot
from backend.core.execution import (
    _check_physical_guardrails,
    _KILL_SWITCH_BUFFER_F,
    _LOW_PROB_TAIL_THRESHOLD,
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
# Guardrail 1 — Absolute floor kill switch
# ---------------------------------------------------------------------------

class TestKillSwitch:
    """
    When betting high stays below a ceiling:
      current_temp >= ceiling − KILL_SWITCH_BUFFER_F  → KILL_SWITCH rejection.
    """

    def _check(self, current_temp_f, ceiling_f, trade_dir="no", market_dir="above"):
        """
        Default: NO on an ABOVE market = betting high stays below ceiling.
        Mocks _hours_until_diurnal_peak=0.0 so the (exit-side) velocity
        calculation some helpers share doesn't interfere with this check.
        """
        market = _make_market(ceiling_f, direction=market_dir)
        signal = _make_signal(market, trade_direction=trade_dir)
        metar  = _make_metar(current_temp_f, trend=0.0)

        with patch("backend.data.weather.fetch_metar_current",
                   new=AsyncMock(return_value=metar)), \
             patch("backend.core.execution._hours_until_diurnal_peak",
                   return_value=0.0):  # isolate kill switch from velocity calc
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

        with patch("backend.data.weather.fetch_metar_current",
                   new=AsyncMock(return_value=metar)), \
             patch("backend.core.execution._hours_until_diurnal_peak",
                   return_value=0.0):
            reject = run(_check_physical_guardrails(signal))

        assert reject is None


# NOTE: a "Warming velocity kill switch" guardrail (VELOCITY_KILL) used to be
# tested here as an entry-side check. It was removed from _check_physical_
# guardrails on 2026-08-17 (see the module docstring above and execution.py's
# note above _DIURNAL_PEAK_HOUR) — the same projection now runs on the exit
# side instead, as evaluate_open_positions_for_exit's "trend_stop" exit_type
# in weather_signals.py, covered by tests/test_nws_tracker_and_exits.py.


# ---------------------------------------------------------------------------
# Guardrail 2 — Low-probability tail guard
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

        with patch("backend.data.weather.fetch_metar_current",
                   new=AsyncMock(return_value=metar)), \
             patch("backend.core.execution._hours_until_diurnal_peak",
                   return_value=2.0):
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
