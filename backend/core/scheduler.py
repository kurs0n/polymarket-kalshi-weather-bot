"""Background scheduler for weather temperature trading."""
import asyncio
from datetime import datetime, timedelta
from typing import List, Optional
from zoneinfo import ZoneInfo
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import func
import logging

from sqlalchemy import text

from backend.config import settings
from backend.models.database import SessionLocal, Trade, BotState, Signal, engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("trading_bot")

# Global scheduler instance
scheduler: Optional[AsyncIOScheduler] = None

# Event log for terminal display (in-memory, last 200 events)
event_log: List[dict] = []
MAX_LOG_SIZE = 200

# Position liquidator thresholds (WEATHER_PROFIT_TARGET_PCT,
# WEATHER_PRICE_STOP_LOSS_PCT) moved to backend/config.py / .env 2026-08-16 —
# same reasoning as the sizing settings above: a risk parameter that needs
# tuning shouldn't require a code edit to change.

# Trailing-stop / overnight de-risk tuning — see position_liquidator_job's
# "Exit condition 1" for the full rationale and the live trade
# (KXHIGHMIA-26AUG18-B93.5) that motivated replacing the flat profit target.
OVERNIGHT_DERISK_LOCAL_HOUR     = 20   # 8 PM local — diurnal peak long past, less edge left to protect
OVERNIGHT_ACTIVATION_MULTIPLIER = 0.5  # trail activates at half the normal gain threshold late in the day
OVERNIGHT_TRAIL_MULTIPLIER      = 0.5  # ...and gives back half as much room before exiting


def _trailing_stop_giveback(peak_gain_pct: float) -> float:
    """
    How many percentage points of gain the trailing stop lets a position
    retrace from its peak before selling, once activated.

    Root-caused 2026-08-21: a flat giveback (the old WEATHER_TRAILING_STOP_PCT
    behavior) is a reasonable trail for a position that peaked at, say, 60%
    gain, but on a cheap longshot that peaked at 700%+ it fires almost
    immediately after any pullback and locks in a tiny fraction of what the
    position was actually worth (see WEATHER_TRAILING_STOP_RATIO's comment
    in config.py for the real trade that motivated this). Scaling the
    giveback with how far the position ran fixes that without giving up
    protection on modest gains — WEATHER_TRAILING_STOP_PCT is still the
    floor, so a peak just past activation gets the same firm minimum trail
    as before.
    """
    return max(
        settings.WEATHER_TRAILING_STOP_PCT,
        peak_gain_pct * settings.WEATHER_TRAILING_STOP_RATIO,
    )

# Re-entrancy guard: weather_scan_and_trade_job places live orders, so two
# concurrent invocations (e.g. the scheduled interval firing while a manual
# scan or a startup task is still mid-flight) could both pass the "no open
# position exists" dedup check before either commits its Trade row, and
# double up a real Kalshi order for the same city/date bracket. This lock
# makes concurrent execution structurally impossible instead of relying on
# every caller to coordinate timing.
#
# This is an asyncio.Lock, so it only protects against overlap WITHIN one
# process/event loop. Root-caused 2026-08-17: several weather trades were
# bought twice within minutes at bit-for-bit identical edge — impossible
# for this lock to have missed if it were a same-process race. The actual
# cause is cross-process: stop_scheduler() calls shutdown(wait=False) (see
# below), which doesn't wait for an in-flight job to finish, and
# start_scheduler()'s IntervalTrigger fires an immediate first run on
# start(). Under `uvicorn --reload`, every file-change restart creates a
# window where the OLD process's in-flight scan/trade loop is still
# running while the NEW process's scheduler immediately starts its own —
# two separate Python processes, each with its own independent
# _scan_lock, both reading the same not-yet-committed DB state and both
# deciding to buy the same signal. See _try_acquire_cross_process_scan_lock
# below for the fix: a Postgres advisory lock, which (unlike this
# asyncio.Lock) is visible to every process connected to the database.
_scan_lock = asyncio.Lock()

# Arbitrary but constant bigint key for the cross-process weather-scan
# advisory lock — pg_advisory_lock just needs a stable int64, not anything
# meaningful. Every process/connection using this same key contends for
# the same named lock.
_WEATHER_SCAN_LOCK_KEY = 823401773


def _try_acquire_cross_process_scan_lock():
    """
    Postgres session-level advisory lock, held for the duration of one
    weather_scan_and_trade_job run — the cross-process counterpart to
    _scan_lock above (see its comment for the restart-race this closes).

    Returns (conn, True) when the lock was acquired: caller MUST pass
    `conn` to _release_cross_process_scan_lock() when done, in a finally
    block, even on error — an unlocked advisory lock on a connection
    returned to the pool would wedge every future scan behind a lock
    nobody's still using.

    Returns (None, False) when another process already holds it — the
    caller should skip this run, same as the in-process lock's behavior.

    Returns (None, True) — i.e. "treat as acquired, proceed" — when the DB
    isn't Postgres (advisory locks are a Postgres-specific feature; local
    sqlite dev/test setups are inherently single-process, so there's no
    cross-process race to guard against there) or when the lock RPC itself
    errors (a real infra problem, unrelated to the restart race this
    exists for — same fail-open-on-infrastructure-failure posture as the
    METAR/order-book guards in execution.py, rather than halting all
    trading over a transient DB hiccup).
    """
    if engine.dialect.name != "postgresql":
        return None, True

    try:
        conn = engine.connect()
    except Exception as e:
        logger.warning(f"Cross-process scan lock: connect failed, proceeding without it: {e}")
        return None, True

    try:
        acquired = conn.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": _WEATHER_SCAN_LOCK_KEY}
        ).scalar()
    except Exception as e:
        logger.warning(f"Cross-process scan lock: acquire failed, proceeding without it: {e}")
        conn.close()
        return None, True

    if not acquired:
        conn.close()
        return None, False

    return conn, True


def _release_cross_process_scan_lock(conn) -> None:
    """Release a lock acquired by _try_acquire_cross_process_scan_lock, if any."""
    if conn is None:
        return
    try:
        conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _WEATHER_SCAN_LOCK_KEY})
    except Exception as e:
        logger.warning(f"Cross-process scan lock: release failed (will clear when connection recycles): {e}")
    finally:
        conn.close()


def log_event(event_type: str, message: str, data: dict = None):
    """Log an event for terminal display."""
    event = {
        "timestamp": datetime.utcnow().isoformat(),
        "type": event_type,
        "message": message,
        "data": data or {}
    }
    event_log.append(event)

    while len(event_log) > MAX_LOG_SIZE:
        event_log.pop(0)

    log_func = {
        "error": logger.error,
        "warning": logger.warning,
        "success": logger.info,
        "info": logger.info,
        "data": logger.debug,
        "trade": logger.info
    }.get(event_type, logger.info)

    log_func(f"[{event_type.upper()}] {message}")


def get_recent_events(limit: int = 50) -> List[dict]:
    """Get recent events for terminal display."""
    return event_log[-limit:]


def _log_suppressed_signals(reason: str, actionable_signals) -> None:
    """
    Surface which actionable signals a circuit breaker is currently blocking.

    Root-caused 2026-08-17: a real tail bet (KXHIGHNY-26AUG18-T92, a genuine
    2.9-sigma mispricing — exactly the pattern that backtests at ~83%
    accuracy) sat [ACTIONABLE] for 2+ hours while the daily-loss circuit
    breaker silently blocked every trade. Nobody knew until the contract
    had already expired and the miss was found by accident while digging
    through old signal rows. A blocked scan used to just log "N actionable"
    as a count — this logs WHAT they were, so a miss like that shows up in
    the event log the moment it happens instead of hours (or never) later.
    """
    if not actionable_signals:
        return
    lines = [
        f"{s.market.market_id} ({s.direction.upper()}, edge={s.edge:+.1%}, conf={s.confidence:.0%})"
        for s in actionable_signals[:10]
    ]
    log_event(
        "warning",
        f"{reason} — {len(actionable_signals)} actionable signal(s) NOT traded "
        f"this cycle: {'; '.join(lines)}",
        {
            "reason": reason,
            "suppressed_count": len(actionable_signals),
            "suppressed": [
                {"ticker": s.market.market_id, "direction": s.direction,
                 "edge": s.edge, "confidence": s.confidence}
                for s in actionable_signals
            ],
        },
    )


async def weather_scan_and_trade_job():
    """
    Background job: Scan weather temperature markets, generate signals, execute trades.
    Runs every WEATHER_SCAN_INTERVAL_SECONDS when WEATHER_ENABLED.

    Thin wrapper around _run_weather_scan_and_trade that serialises execution
    two ways — see each lock's own comment for what it covers:
      1. _scan_lock (asyncio.Lock): same-process overlap, e.g. a manual scan
         firing while the scheduled interval is still mid-flight.
      2. The cross-process advisory lock: a --reload restart (or any future
         multi-worker deployment) where a DIFFERENT process is mid-scan —
         invisible to #1 since each process has its own asyncio.Lock.
    """
    if _scan_lock.locked():
        log_event(
            "warning",
            "Weather scan already in progress — skipping this invocation "
            "to avoid placing duplicate live orders for the same signal.",
        )
        return

    async with _scan_lock:
        conn, acquired = _try_acquire_cross_process_scan_lock()
        if not acquired:
            log_event(
                "warning",
                "Another process already holds the weather-scan lock (likely an "
                "overlapping --reload restart) — skipping this invocation to avoid "
                "placing duplicate live orders for the same signal.",
            )
            return
        try:
            await _run_weather_scan_and_trade()
        finally:
            _release_cross_process_scan_lock(conn)


async def _run_weather_scan_and_trade():
    log_event("info", "Scanning weather temperature markets...")

    try:
        from backend.core.weather_signals import scan_for_weather_signals

        signals = await scan_for_weather_signals()
        actionable = [s for s in signals if s.passes_threshold]

        log_event("data", f"Weather: {len(signals)} signals, {len(actionable)} actionable", {
            "total_signals": len(signals),
            "actionable": len(actionable),
        })

        if not actionable:
            log_event("info", "No actionable weather signals")
            return

        db = SessionLocal()
        try:
            state = db.query(BotState).first()
            if not state:
                log_event("error", "Bot state not initialized")
                return

            if not state.is_running:
                log_event("info", "Bot is paused, skipping weather trades")
                _log_suppressed_signals("Bot paused", actionable)
                return

            # Root-caused 2026-08-15: these three were hardcoded locals, so
            # every bankroll change (INITIAL_BANKROLL) also needed a code
            # edit here to keep MIN_TRADE_SIZE below Kelly's max possible
            # output (bankroll * KELLY_MAX_TRADE_FRACTION) — at $30 bankroll
            # a hardcoded $10 floor sat ABOVE Kelly's entire $1.50 ceiling,
            # so every trade silently came out identical regardless of
            # confidence. Now both live in backend/config.py / .env, so
            # resizing the bankroll is a config change, not a code change.
            MAX_TRADES_PER_SCAN = settings.WEATHER_MAX_TRADES_PER_SCAN
            MIN_TRADE_SIZE = settings.WEATHER_MIN_TRADE_SIZE

            # Bounded scale-in — added 2026-08-17 on user request. A flat
            # one-trade-per-ticket dedup (added 2026-08-14) fixed the
            # unbounded-retry bug that produced the -$1,133 NY loss cluster,
            # but it also blocks legitimately adding to a position when NEW
            # evidence shows up (price moved further our way, model
            # agreement strengthened) — "still actionable 5 minutes later"
            # isn't new evidence (the forecast barely changes scan to scan),
            # so a re-entry only counts as genuine if the edge has actually
            # GROWN past the last entry into this exact ticket. Bounded to
            # MAX_SCALE_INS additional adds and a total-exposure cap so a
            # wrong call can't compound the way the unbounded version did.
            MAX_SCALE_INS = 2                      # up to 2 adds -> 3 entries total per ticket
            SCALE_IN_EXPOSURE_CAP_MULTIPLE = 2.0   # total size per ticket <= 2x the first entry
            MIN_SCALE_IN_EDGE_GROWTH = 0.02        # edge must grow by >=2pp, not just != last entry —
                                                    # observed 2026-08-18: back-to-back scans with an
                                                    # unmoved market/forecast reproduce the exact same
                                                    # edge float, which a bare `>` comparison should
                                                    # reject but a same-scan-interval duplicate got
                                                    # through anyway; a real margin is a sturdier bar
                                                    # than exact inequality regardless of the cause.

            # --- Daily loss circuit breaker ---
            today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            daily_pnl = db.query(func.coalesce(func.sum(Trade.pnl), 0.0)).filter(
                Trade.settled == True,
                Trade.settlement_time >= today_start
            ).scalar()

            if daily_pnl <= -settings.DAILY_LOSS_LIMIT:
                log_event("warning", f"Daily loss limit hit: ${daily_pnl:.2f} (limit: -${settings.DAILY_LOSS_LIMIT:.0f}). Stopping trades.")
                _log_suppressed_signals("Daily loss limit", actionable)
                return

            total_pending = db.query(Trade).filter(Trade.settled == False).count()
            if total_pending >= settings.MAX_TOTAL_PENDING_TRADES:
                log_event("info", f"Max pending trades reached ({total_pending}/{settings.MAX_TOTAL_PENDING_TRADES})")
                _log_suppressed_signals("Max pending trades reached", actionable)
                return

            weather_pending = db.query(func.coalesce(func.sum(Trade.size), 0.0)).filter(
                Trade.settled == False,
                Trade.market_type == "weather",
            ).scalar()

            # Root-caused 2026-08-16: this used to be a flat dollar cap
            # (WEATHER_MAX_ALLOCATION=$500) that made sense at the old $30
            # live bankroll but silently became a 5% ceiling once bankroll
            # moved to $10,000 — the bot hit "allocation limit reached" on
            # every scan for over an hour, blocking real edges for a reason
            # that had nothing to do with edge quality. Now a percentage of
            # total capital (free bankroll + already-committed positions),
            # so it scales automatically with whatever INITIAL_BANKROLL is.
            total_capital = state.bankroll + weather_pending
            MAX_WEATHER_ALLOCATION = total_capital * settings.WEATHER_MAX_ALLOCATION_PCT

            if weather_pending >= MAX_WEATHER_ALLOCATION:
                log_event("info", f"Weather allocation limit reached: ${weather_pending:.0f}/${MAX_WEATHER_ALLOCATION:.0f} ({settings.WEATHER_MAX_ALLOCATION_PCT:.0%} of ${total_capital:.0f} total capital)")
                _log_suppressed_signals("Weather allocation limit", actionable)
                return

            trades_executed = 0
            for signal in actionable[:MAX_TRADES_PER_SCAN]:
                # Guard 1 (exact ticker): may SCALE IN under the bounded
                #   rules below, instead of an automatic block.
                # Guard 2 (city/date prefix, different ticker): still a hard
                #   block, unchanged — prevents capital being split across
                #   correlated brackets (e.g. B83.5 and B84.0 for NYC same
                #   day). Scale-in only ever adds to the SAME bracket.
                # Root-caused 2026-08-23: always used the HIGH series here
                # regardless of the signal's actual metric — for a low-temp
                # signal this built a prefix that could never match its own
                # ticker, silently disabling this guard for every low-temp
                # trade (it would still pass Guard 1's exact-ticker check,
                # but never correctly detect a DIFFERENT low-temp bracket on
                # the same city/day as correlated).
                from backend.data.kalshi_markets import CITY_SERIES, LOW_SERIES
                city_key    = signal.market.city_key
                target_date = signal.market.target_date
                series_map  = LOW_SERIES if signal.market.metric == "low" else CITY_SERIES
                series      = series_map.get(city_key, "")
                date_str    = target_date.strftime("%y%b%d").upper()
                ticker_prefix = f"{series}-{date_str}-"
                this_ticker = signal.market.market_id

                today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

                # Block 2: a DIFFERENT bracket for this city/date, either still
                # open or already filled today. Unchanged from before.
                other_open = db.query(Trade).filter(
                    Trade.settled == False,
                    Trade.platform == "kalshi",
                    Trade.market_ticker.like(f"{ticker_prefix}%"),
                    Trade.market_ticker != this_ticker,
                ).first()
                other_filled = db.query(Trade).filter(
                    Trade.platform == "kalshi",
                    Trade.market_ticker.like(f"{ticker_prefix}%"),
                    Trade.market_ticker != this_ticker,
                    Trade.timestamp >= today_start,
                    Trade.execution_type == "maker_limit",
                    Trade.order_status != "timed_out",
                    Trade.order_status != "cancelled",
                ).first()
                if other_open or other_filled:
                    blocking = other_open or other_filled
                    log_event(
                        "info",
                        f"Skipping {signal.market.market_id}: a different bracket already "
                        f"held for {city_key}/{target_date} ({blocking.market_ticker})",
                    )
                    continue

                # Guard 1: existing entries into THIS exact ticket — bounded scale-in.
                same_ticket_trades = (
                    db.query(Trade)
                    .filter(Trade.platform == "kalshi", Trade.market_ticker == this_ticker)
                    .order_by(Trade.timestamp.asc())
                    .all()
                )

                remaining_budget = None
                if same_ticket_trades:
                    if len(same_ticket_trades) > MAX_SCALE_INS:
                        log_event(
                            "info",
                            f"Skipping {this_ticker}: already at max {MAX_SCALE_INS} scale-ins",
                        )
                        continue

                    last_edge = same_ticket_trades[-1].edge_at_entry or 0.0
                    if abs(signal.edge) < abs(last_edge) + MIN_SCALE_IN_EDGE_GROWTH:
                        log_event(
                            "info",
                            f"Skipping {this_ticker}: edge {abs(signal.edge):.1%} hasn't grown "
                            f"at least {MIN_SCALE_IN_EDGE_GROWTH:.0%} past last entry's "
                            f"{abs(last_edge):.1%} — a re-scan agreeing with itself (even one "
                            f"that recomputes a bit-identical edge) isn't new evidence",
                        )
                        continue

                    original_size = same_ticket_trades[0].size
                    committed_size = sum(t.size for t in same_ticket_trades)
                    exposure_cap = original_size * SCALE_IN_EXPOSURE_CAP_MULTIPLE
                    remaining_budget = exposure_cap - committed_size
                    if remaining_budget < MIN_TRADE_SIZE:
                        log_event(
                            "info",
                            f"Skipping {this_ticker}: at {SCALE_IN_EXPOSURE_CAP_MULTIPLE:.0f}x "
                            f"exposure cap (${committed_size:.0f}/${exposure_cap:.0f})",
                        )
                        continue

                # signal.suggested_size is already Kelly-sized and capped at
                # KELLY_MAX_TRADE_FRACTION of bankroll — no separate flat
                # WEATHER_MAX_TRADE_SIZE clamp here (see weather_signals.py).
                trade_size = signal.suggested_size
                if remaining_budget is not None:
                    trade_size = min(trade_size, remaining_budget)
                trade_size = max(trade_size, MIN_TRADE_SIZE)
                is_scale_in = bool(same_ticket_trades)

                if state.bankroll < MIN_TRADE_SIZE:
                    log_event("warning", f"Bankroll too low: ${state.bankroll:.2f}")
                    break

                if trades_executed >= MAX_TRADES_PER_SCAN:
                    break

                sim_mode = settings.SIMULATION_MODE
                from backend.core.execution import execute_paper_trade, execute_live_trade

                if sim_mode:
                    trade = await execute_paper_trade(signal, trade_size)
                    if trade is None:
                        log_event(
                            "info",
                            f"Paper trade rejected: {signal.market.market_id} "
                            f"(guardrail, no ask liquidity, or spread > 10¢)",
                        )
                        continue
                else:
                    trade = await execute_live_trade(signal, trade_size)
                    if trade is None:
                        # None now covers two distinct cases — a physical
                        # guardrail rejection (see execute_live_trade's own
                        # [GUARDRAIL LIVE] log line for that) or a genuine
                        # Kalshi order-placement failure (its own [ERROR]
                        # line, 2026-08-20 fix). Either way nothing was
                        # committed here — no bankroll debit, no phantom
                        # position — so this is just a skip, not a specific
                        # diagnosis; check the two log lines above for which.
                        log_event(
                            "warning",
                            f"Live trade not placed: {signal.market.market_id} "
                            f"(guardrail rejection or order placement failure — see prior log line)",
                        )
                        continue

                db.add(trade)
                db.flush()

                # Live trades need the order_id recorded after DB flush gives us an ID
                if not sim_mode and signal.market.platform == "kalshi" and not trade.order_id:
                    logger.debug(
                        f"Live order for {trade.market_ticker} placed via execute_live_trade"
                    )

                # Reduce available bankroll immediately so the next iteration
                # in this same scan doesn't double-count capital.
                state.bankroll -= trade_size

                matching_signal = db.query(Signal).filter(
                    Signal.market_ticker == signal.market.market_id,
                    Signal.market_type == "weather",
                    Signal.executed == False,
                ).order_by(Signal.timestamp.desc()).first()
                if matching_signal:
                    matching_signal.executed = True
                    trade.signal_id = matching_signal.id

                state.total_trades += 1
                trades_executed += 1

                log_event("trade",
                    f"WX {'[SCALE-IN] ' if is_scale_in else ''}{signal.market.city_name}: "
                    f"{signal.direction.upper()} ${trade_size:.0f} @ {trade.entry_price:.0%} "
                    f"({'sim/ask' if sim_mode else 'live/limit'}) | "
                    f"{signal.market.metric} {signal.market.direction} {signal.market.threshold_f:.0f}F",
                    {
                        "slug": signal.market.slug,
                        "direction": signal.direction,
                        "size": trade_size,
                        "edge": signal.edge,
                        "entry_price": trade.entry_price,
                        "execution_type": trade.execution_type,
                        "city": signal.market.city_name,
                        "platform": signal.market.platform,
                        "scale_in": is_scale_in,
                    }
                )

            state.last_run = datetime.utcnow()
            db.commit()

            if trades_executed > 0:
                log_event("success", f"Executed {trades_executed} weather trade(s)")
            else:
                log_event("info", "No new weather trades executed")

        finally:
            db.close()

    except Exception as e:
        log_event("error", f"Weather scan error: {str(e)}")
        logger.exception("Error in weather_scan_and_trade_job")


async def settlement_job():
    """Background job: Check and settle pending weather trades."""
    log_event("info", "Checking weather trade settlements...")

    try:
        from backend.core.settlement import settle_pending_trades, update_bot_state_with_settlements

        db = SessionLocal()
        try:
            pending_count = db.query(Trade).filter(Trade.settled == False).count()

            if pending_count == 0:
                log_event("data", "No pending trades to settle")
                return

            log_event("data", f"Processing {pending_count} pending trades")

            settled = await settle_pending_trades(db)

            if settled:
                await update_bot_state_with_settlements(db, settled)

                wins = sum(1 for t in settled if t.result == "win")
                losses = sum(1 for t in settled if t.result == "loss")
                total_pnl = sum(t.pnl for t in settled if t.pnl is not None)

                log_event("success", f"Settled {len(settled)} trades: {wins}W/{losses}L, P&L: ${total_pnl:.2f}", {
                    "settled_count": len(settled),
                    "wins": wins,
                    "losses": losses,
                    "pnl": total_pnl
                })

                for trade in settled:
                    result_prefix = "+" if trade.pnl and trade.pnl > 0 else ""
                    log_event("data", f"  {trade.event_slug}: {trade.result.upper()} {result_prefix}${trade.pnl:.2f}")
            else:
                log_event("info", "No trades ready for settlement")

        finally:
            db.close()

    except Exception as e:
        log_event("error", f"Settlement error: {str(e)}")
        logger.exception("Error in settlement_job")


async def reconciliation_job():
    """
    Background job: verify nws_early-settled trades against Kalshi's
    official result once it's likely available, correcting the ledger on
    any mismatch. See settlement.py's reconcile_early_settlements for the
    full rationale (2026-08-20 audit finding).
    """
    try:
        from backend.core.settlement import reconcile_early_settlements

        db = SessionLocal()
        try:
            mismatched = await reconcile_early_settlements(db)
            if mismatched:
                log_event(
                    "error",
                    f"Reconciliation found {len(mismatched)} early-settlement mismatch(es) — corrected",
                    {"trade_ids": [t.id for t in mismatched]},
                )
        finally:
            db.close()

    except Exception as e:
        log_event("error", f"Reconciliation error: {str(e)}")
        logger.exception("Error in reconciliation_job")


async def heartbeat_job():
    """Periodic heartbeat. Runs every minute."""
    db = None
    try:
        db = SessionLocal()
        state = db.query(BotState).first()
        pending = db.query(Trade).filter(Trade.settled == False).count()

        if state is None:
            log_event("warning", "Heartbeat: Bot state not initialized")
            return

        log_event("data", f"Heartbeat: {pending} pending trades, bankroll: ${state.bankroll:.2f}", {
            "pending_trades": pending,
            "bankroll": state.bankroll,
            "is_running": state.is_running
        })
    except Exception as e:
        log_event("warning", f"Heartbeat failed: {str(e)}")
    finally:
        if db:
            db.close()


async def check_pending_orders_job():
    """
    Two-phase job that maintains open Kalshi limit orders:

    Phase A — Timeout sweep (unchanged behaviour):
      Cancel orders resting > 180 s unfilled, return the bankroll stake, and
      re-queue with a fresh limit if the edge is still ≥ 10%.

    Phase B — METAR physical-impossibility sweep (new):
      For every pending_fill order whose trade WINS only if the daily high
      stays BELOW a bracket ceiling, check the live METAR temperature.  If
      the station reading has breached the kill-switch threshold (ceiling −
      1.5°F), cancel the Kalshi order immediately — the position is already
      physically lost and every minute it rests on the book wastes margin.
    """
    from backend.data.kalshi_client import KalshiClient, kalshi_credentials_present
    from backend.data.kalshi_markets import _extract_prices_from_orderbook

    timeout_cutoff = datetime.utcnow() - timedelta(seconds=180)

    db = SessionLocal()
    try:
        stale = (
            db.query(Trade)
            .filter(
                Trade.settled == False,
                Trade.order_status == "pending_fill",
                Trade.order_placed_at <= timeout_cutoff,
                Trade.platform == "kalshi",
            )
            .all()
        )

        if not stale:
            return

        client = KalshiClient() if kalshi_credentials_present() else None

        for trade in stale:
            # Cancel the resting order on Kalshi, then check whether it had
            # ALREADY partially (or fully) filled before the cancel landed —
            # Kalshi cancels only the unfilled remainder, so a "cancel" here
            # is not proof of zero fill. (Root-caused 2026-08-13: a $10
            # order filled 233 shares / $4.67 on Kalshi, then this job
            # blindly marked it "timed_out"/pnl=0 and refunded the full $10,
            # silently losing track of $4.67 of real, live position.)
            if client and trade.order_id:
                try:
                    await client.cancel_order(trade.order_id)
                except Exception as e:
                    logger.warning(f"Cancel failed for order {trade.order_id}: {e}")

            fill_count = 0.0
            fill_cost = 0.0
            if client and trade.order_id:
                try:
                    order_resp = await client.get_order(trade.order_id)
                    o = order_resp.get("order", {})
                    fill_count = float(o.get("fill_count_fp", 0) or 0)
                    fill_cost = float(o.get("maker_fill_cost_dollars", 0) or 0) + \
                                float(o.get("taker_fill_cost_dollars", 0) or 0)
                except Exception as e:
                    logger.warning(f"get_order failed for {trade.order_id}: {e}")

            if fill_count > 0:
                # Partially (or fully) filled before the cancel took effect —
                # this is a real, live position. Correct the trade record to
                # reflect the ACTUAL contracts/dollars acquired and only
                # refund the unfilled remainder of the stake, then treat it
                # exactly like a fill for re-queue purposes below.
                unfilled_refund = max(0.0, trade.size - fill_cost)
                trade.order_status = "filled"
                trade.execution_type = "maker_limit"
                trade.settled = False
                trade.result = "pending"
                trade.pnl = None
                trade.size = fill_cost

                state = db.query(BotState).first()
                if state and unfilled_refund > 0:
                    state.bankroll += unfilled_refund

                log_event(
                    "trade",
                    f"Order partially filled before cancel: {trade.market_ticker} | "
                    f"{fill_count:.2f} contracts / ${fill_cost:.2f} kept as a live position, "
                    f"${unfilled_refund:.2f} unfilled remainder refunded",
                )
                db.flush()
                continue

            # Genuinely zero fill — mark timed out and return the full stake.
            trade.order_status = "timed_out"
            trade.execution_type = "timed_out"
            trade.settled = True
            trade.result = "timed_out"
            trade.pnl = 0.0

            state = db.query(BotState).first()
            if state:
                state.bankroll += trade.size

            log_event(
                "trade",
                f"Order timeout: {trade.market_ticker} | bankroll restored ${trade.size:.2f}",
            )
            # Push the "timed_out" status to the DB now — without this flush,
            # the filled_today query below (autoflush=False on this session)
            # would still see this trade's PRE-update status and could count
            # itself as a "fill" (the self-counting bug root-caused
            # 2026-08-13, which made this skip-logic report a false fill on
            # every single timeout regardless of whether one ever occurred).
            db.flush()

            # Re-evaluate: fetch fresh orderbook and check if edge is still actionable.
            # Only re-queue if this ticker has NO fills today — a timeout means no one
            # took our price yet (re-queue is valid market-making). A prior fill means
            # we already accumulated a position; re-queuing would pile into the same
            # risk bucket regardless of the bankroll cap.
            today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            filled_today = db.query(Trade).filter(
                Trade.market_ticker == trade.market_ticker,
                Trade.execution_type == "maker_limit",
                Trade.timestamp >= today_start,
                Trade.order_status == "filled",
            ).count()

            if filled_today > 0:
                log_event(
                    "info",
                    f"Skip re-queue {trade.market_ticker}: "
                    f"{filled_today} fill(s) already accumulated today — "
                    f"position acquired, not chasing more size",
                )
                continue

            # Same confidence floor as the entry gate in generate_weather_signal
            # (weather_signals.py). This retry path recomputes its own edge from
            # a fresh orderbook rather than going back through the full signal
            # pipeline, so it was bypassing the confidence gate entirely — a
            # low-confidence trade would just keep re-queuing itself forever on
            # every timeout.
            #
            # Root-caused 2026-08-15: originally let confidence=None through
            # ("don't block on missing data for trades older than the column").
            # That's wrong for THIS path specifically — each requeue copies
            # confidence forward from the row before it, so a chain that
            # started before the column existed stayed None forever and kept
            # bypassing the gate indefinitely (caught KXHIGHMIA-26AUG15-B88.5
            # mid-loop: 32 requeues, confidence NULL on every one). Unknown
            # confidence now stops the chase instead of waving it through.
            from backend.core.weather_signals import MIN_CONFIDENCE_THRESHOLD
            if trade.confidence is None or trade.confidence < MIN_CONFIDENCE_THRESHOLD:
                shown_conf = f"{trade.confidence:.0%}" if trade.confidence is not None else "unknown"
                log_event(
                    "info",
                    f"Skip re-queue {trade.market_ticker}: confidence "
                    f"{shown_conf} < {MIN_CONFIDENCE_THRESHOLD:.0%} minimum "
                    f"— not chasing an untested low-confidence signal.",
                )
                continue

            try:
                if client:
                    ob = await client.get_orderbook(trade.market_ticker)
                    prices, _ = _extract_prices_from_orderbook(ob)
                    if prices:
                        fresh_ask = prices["yes_ask"] if trade.direction == "yes" else prices["no_ask"]
                        fresh_edge = abs(trade.model_probability - fresh_ask)

                        if fresh_edge >= 0.10:
                            yes_bid = prices["yes_bid"]
                            no_bid  = prices["no_bid"]
                            if trade.direction == "yes":
                                candidate = round(yes_bid + 0.01, 2)
                                new_lp = candidate if candidate < fresh_ask else yes_bid
                            else:
                                no_ask = prices["no_ask"]
                                candidate = round(no_bid + 0.01, 2)
                                new_lp = candidate if candidate < no_ask else no_bid

                            new_lp_cents = round(new_lp * 100)
                            count = max(1, round(trade.size / new_lp))

                            resp = await client.place_order(
                                trade.market_ticker, trade.direction, count, new_lp_cents
                            )
                            new_order = resp.get("order", {})

                            new_trade = Trade(
                                market_ticker=trade.market_ticker,
                                platform=trade.platform,
                                event_slug=trade.event_slug,
                                market_type=trade.market_type,
                                direction=trade.direction,
                                entry_price=new_lp,
                                size=trade.size,
                                model_probability=trade.model_probability,
                                market_price_at_entry=fresh_ask,
                                edge_at_entry=fresh_edge,
                                confidence=trade.confidence,
                                execution_type="maker_limit",
                                limit_price=new_lp,
                                order_id=new_order.get("order_id"),
                                order_placed_at=datetime.utcnow(),
                                order_status="pending_fill",
                            )
                            db.add(new_trade)
                            if state:
                                state.bankroll -= trade.size
                            log_event(
                                "trade",
                                f"Re-queued {trade.market_ticker} @ {new_lp:.0%} "
                                f"(edge {fresh_edge:+.1%})",
                            )
            except Exception as e:
                logger.warning(f"Re-evaluation failed for {trade.market_ticker}: {e}")

        db.commit()

        # ── Phase B: METAR physical-impossibility sweep ───────────────────────
        # Fetch all pending_fill orders (including non-timed-out ones) and
        # cancel any whose bracket has been breached by live surface temperature.
        # This is the automated open-order sweep requested in Guardrail 3.
        from backend.core.weather_signals import _resolve_city_from_ticker, _evaluate_between_bracket_outcome
        from backend.data.weather import fetch_metar_current, fetch_station_high_water_mark, fetch_station_low_water_mark, CITY_CONFIG
        from datetime import date as _date

        all_pending = (
            db.query(Trade)
            .filter(
                Trade.settled == False,
                Trade.order_status == "pending_fill",
                Trade.platform == "kalshi",
            )
            .all()
        )

        metar_state = db.query(BotState).first()

        for trade in all_pending:
            resolved = await _resolve_city_from_ticker(trade.market_ticker, client)
            if resolved is None:
                continue
            city_key, parsed = resolved
            if parsed.get("direction") is None:
                continue

            # Only apply kill switch to same-day contracts
            if parsed.get("target_date") != _date.today():
                continue

            threshold = parsed["threshold_f"]
            parsed_direction = parsed["direction"]
            metric = parsed.get("metric", "high")
            reason = None

            # Root-caused 2026-08-16: the "between" branch below used to not
            # exist at all — this sweep only ever handled simple above/below
            # contracts, silently leaving "between"-bracket orders (most of
            # what actually gets traded) resting regardless of live readings.
            if parsed_direction == "between":
                floor_f = parsed.get("floor_f")
                cap_f = parsed.get("cap_f")
                if floor_f is None or cap_f is None:
                    continue
                # 2026-08-23: metric-aware — see fetch_station_low_water_mark's
                # docstring; a between-bracket LOW contract needs the day's
                # LOW so far, not its high.
                if metric == "low":
                    observed = await fetch_station_low_water_mark(city_key, parsed["target_date"])
                else:
                    observed = await fetch_station_high_water_mark(city_key, parsed["target_date"])
                if observed is None:
                    continue
                bias = CITY_CONFIG.get(city_key, {}).get("station_bias_f", 0.0)
                corrected = observed + bias
                outcome = _evaluate_between_bracket_outcome(city_key, floor_f, cap_f, corrected)
                if outcome is None:
                    continue
                our_side_loses = (
                    (outcome == "yes_wins" and trade.direction == "no") or
                    (outcome == "no_wins"  and trade.direction == "yes")
                )
                if not our_side_loses:
                    continue
                reason = (
                    f"METAR_SWEEP: {city_key} {metric}-water-mark {corrected:.1f}°F "
                    f"vs band [{floor_f:.1f}, {cap_f:.1f}]°F ({outcome}). "
                    f"Order rejected: bracket already decided against our {trade.direction} side."
                )
            else:
                metar = await fetch_metar_current(city_key)
                if metar is None:
                    continue

                from backend.core.execution import _KILL_SWITCH_BUFFER_F
                current_temp = metar.observed_temp_f

                if metric == "low":
                    # 2026-08-23: mirror of the high-side kill switch below.
                    # A single live reading is a valid, time-of-day-independent
                    # bound on the day's still-to-come extreme either way: the
                    # day's HIGH can only be >= any reading taken that day, and
                    # the day's LOW can only be <= any reading taken that day.
                    # So "already this cold" is just as certain a physical fact
                    # as "already this hot" — no diurnal-timing model needed
                    # here (unlike the trend_stop projection in weather_signals.py,
                    # which genuinely does need one and was deliberately NOT
                    # extended to lows).
                    betting_low_stays_above = (
                        (trade.direction == "yes" and parsed_direction == "above") or
                        (trade.direction == "no"  and parsed_direction == "below")
                    )
                    if not betting_low_stays_above:
                        continue

                    floor = threshold
                    if current_temp > floor + _KILL_SWITCH_BUFFER_F:
                        continue  # bracket not yet breached — leave order resting

                    reason = (
                        f"METAR_SWEEP: {metar.station} {current_temp:.1f}°F "
                        f"<= floor {floor:.1f}°F + {_KILL_SWITCH_BUFFER_F}°F. "
                        f"Order rejected: Live ground temperature ({current_temp:.1f}°F) "
                        f"has already breached bracket floor."
                    )
                else:
                    betting_high_stays_below = (
                        (trade.direction == "yes" and parsed_direction == "below") or
                        (trade.direction == "no"  and parsed_direction == "above")
                    )
                    if not betting_high_stays_below:
                        continue

                    ceiling = threshold

                    if current_temp < ceiling - _KILL_SWITCH_BUFFER_F:
                        continue  # bracket not yet breached — leave order resting

                    reason = (
                        f"METAR_SWEEP: {metar.station} {current_temp:.1f}°F "
                        f">= ceiling {ceiling:.1f}°F − {_KILL_SWITCH_BUFFER_F}°F. "
                        f"Order rejected: Live ground temperature ({current_temp:.1f}°F) "
                        f"has breached bracket threshold."
                    )

            if client and trade.order_id:
                try:
                    await client.cancel_order(trade.order_id)
                except Exception as ce:
                    logger.warning(
                        f"METAR sweep cancel failed for order {trade.order_id}: {ce}"
                    )

            trade.order_status = "cancelled"
            trade.execution_type = "timed_out"
            trade.settled = True
            trade.result = "timed_out"
            trade.pnl = 0.0

            if metar_state:
                metar_state.bankroll += trade.size

            log_event(
                "warning",
                f"METAR sweep cancelled {trade.market_ticker}: {reason}",
                {
                    "trade_id": trade.id,
                    "station": metar.station,
                    "current_temp_f": current_temp,
                    "bracket_ceiling_f": ceiling,
                    "bankroll_returned": trade.size,
                },
            )

        db.commit()

    except Exception as e:
        logger.error(f"check_pending_orders_job error: {e}")
        db.rollback()
    finally:
        db.close()


def _city_key_from_ticker(ticker: str) -> Optional[str]:
    """
    Cheap ticker-prefix -> city_key lookup with no API call, for callers
    that only need the city (e.g. a timezone lookup) and not the
    authoritative direction/threshold that _resolve_city_from_ticker fetches
    from Kalshi's market metadata.

    Root-caused 2026-08-23: only checked CITY_SERIES (high-temp) — a
    low-temp ticker would resolve to None here, silently skipping e.g. the
    overnight de-risk multiplier for low-temp positions (position_
    liquidator_job's trailing stop would run at the non-derisked width for
    them all night, since _is_overnight_derisk(None) is treated as False).
    """
    from backend.data.kalshi_markets import CITY_SERIES, LOW_SERIES
    for city_key, series in {**CITY_SERIES, **LOW_SERIES}.items():
        if ticker.startswith(f"{series}-"):
            return city_key
    return None


def _is_overnight_derisk(city_key: str) -> bool:
    """
    True once it's late evening in city_key's local time — well past the
    diurnal peak, with an open position now carrying overnight settlement-
    reporting-gap risk (see execution.py's _KILL_SWITCH_BUFFER_F docstring)
    for no further edge. Tightens the trailing-stop thresholds in
    position_liquidator_job during this window rather than forcing an
    unconditional exit — a position that's still actionable should still be
    allowed to run, just with less rope.
    """
    from backend.data.weather import CITY_CONFIG
    tz_str = CITY_CONFIG.get(city_key, {}).get("timezone", "UTC")
    local_hour = datetime.now(ZoneInfo(tz_str)).hour
    return local_hour >= OVERNIGHT_DERISK_LOCAL_HOUR


async def position_liquidator_job():
    """
    Evaluate every open position and automatically sell when any exit
    condition is satisfied — this IS the "market sentiment" monitor
    (live price vs entry price), independent of the METAR-driven checks in
    evaluate_open_positions_for_exit.

    Exit conditions (checked in order):
      1. Profit target  — unrealised gain ≥ PROFIT_TARGET_PCT (default 50%).
      2. METAR stop-loss — live surface temperature has physically breached the
         bracket boundary so the position cannot win; cut losses immediately.
      3. Price stop-loss — market price has collapsed ≥ PRICE_STOP_LOSS_PCT
         (default 35%) below entry, regardless of METAR availability.

    Root-caused 2026-08-17: this used to hard-return under SIMULATION_MODE,
    meaning conditions 1 and 3 (the only ones that don't need live METAR)
    never ran at all in sim mode — the exact "market sentiment" signal the
    user asked for was built and then never switched on. Now runs in both
    modes: live mode sources positions from the real Kalshi account and
    places a real sell order; sim mode sources open positions from our own
    DB trades (each trade row — including scale-ins, which each have their
    own entry price — evaluated independently) and simulates the fill at
    the live order-book bid instead of calling client.sell_position().
    """
    from backend.config import settings
    sim_mode = settings.SIMULATION_MODE

    from backend.data.kalshi_client import KalshiClient, kalshi_credentials_present
    from backend.data.kalshi_markets import _extract_prices_from_orderbook
    from backend.core.weather_signals import _resolve_city_from_ticker, _evaluate_between_bracket_outcome
    from backend.data.weather import fetch_metar_current, fetch_station_high_water_mark, fetch_station_low_water_mark, CITY_CONFIG
    from backend.core.execution import _KILL_SWITCH_BUFFER_F
    from datetime import date as _date

    if not kalshi_credentials_present():
        return

    client = KalshiClient()
    db = SessionLocal()
    try:
        state = db.query(BotState).first()
        liquidated_count = 0

        # ── 1. Build the work list: (ticker, trade, live_net_position|None) ────
        work_items = []
        if sim_mode:
            open_trades = (
                db.query(Trade)
                .filter(
                    Trade.settled == False,
                    Trade.platform == "kalshi",
                    Trade.market_type == "weather",
                )
                .all()
            )
            for t in open_trades:
                work_items.append((t.market_ticker, t, None))
        else:
            try:
                positions_resp = await client.get_positions(settlement_status="unsettled")
            except Exception as e:
                logger.warning(f"position_liquidator: get_positions failed: {e}")
                return
            # Tolerate both {"positions": [...]} and {"market_positions": [...]}
            positions = (
                positions_resp.get("positions")
                or positions_resp.get("market_positions")
                or []
            )
            if not positions:
                return

            for pos in positions:
                ticker = (
                    pos.get("ticker")
                    or pos.get("market_ticker")
                    or pos.get("market_id")
                )
                if not ticker:
                    continue

                # Net signed contract count (positive = long YES, negative = long NO).
                #
                # Root-caused 2026-08-14: Kalshi's actual response field is
                # "position_fp" (a fixed-point decimal string, e.g. "-21.00"),
                # not "position" — that key doesn't exist in the real payload, so
                # this always read the dict .get() default of 0 and every
                # position looked flat. This job has likely never sold anything
                # via profit-target or stop-loss since it was written.
                try:
                    raw_pos = pos.get("position_fp", pos.get("position", 0))
                    net_position = float(raw_pos) if raw_pos not in (None, "") else 0.0
                except (ValueError, TypeError):
                    continue
                if net_position == 0:
                    continue

                trade = (
                    db.query(Trade)
                    .filter(
                        Trade.market_ticker == ticker,
                        Trade.settled == False,
                        Trade.platform == "kalshi",
                    )
                    .order_by(Trade.timestamp.desc())
                    .first()
                )
                if trade is None:
                    logger.debug(
                        f"position_liquidator: no unsettled DB trade for {ticker} "
                        f"(may be manually placed) — skipping"
                    )
                    continue
                work_items.append((ticker, trade, net_position))

        if not work_items:
            return

        for ticker, trade, net_position in work_items:
            # Derive holding side from DB trade (more reliable than API sign
            # when the API representation is ambiguous or pre-netted).
            # Fallback to API sign when no DB record exists (live mode only).
            holding_side = trade.direction if trade else ("yes" if (net_position or 0) > 0 else "no")

            # ── Get live order book ──────────────────────────────────────────
            try:
                ob_data = await client.get_orderbook(ticker)
                prices, _ = _extract_prices_from_orderbook(ob_data)
            except Exception as e:
                logger.warning(
                    f"position_liquidator: orderbook fetch failed for {ticker}: {e}"
                )
                continue

            if prices is None:
                continue

            # Current liquidation value = best bid price for the side we hold.
            # "sell_price" is in the contract denomination of holding_side.
            if holding_side == "yes":
                sell_price = prices["yes_bid"]
            else:
                sell_price = prices["no_bid"]   # = 1 − yes_ask

            # ── P&L calculation ──────────────────────────────────────────────
            entry_price_per_contract = trade.entry_price   # e.g., 0.35
            if sim_mode:
                # trade.size is DOLLARS committed (see settlement.py's
                # calculate_pnl fix, 2026-08-17), not a contract count —
                # contracts = size / entry_price.
                abs_count = trade.size / entry_price_per_contract if entry_price_per_contract else 0.0
                entry_cost = trade.size
            else:
                abs_count = int(round(abs(net_position)))
                entry_cost = entry_price_per_contract * abs_count
            sell_proceeds = sell_price * abs_count
            unrealised_pnl = sell_proceeds - entry_cost
            gain_pct = unrealised_pnl / entry_cost if entry_cost > 0 else 0.0

            # Trailing-stop peak tracking — persisted every cycle regardless
            # of whether an exit fires this pass (see the always-commit note
            # at the end of this function), so a retracement from peak can
            # still be detected on a LATER cycle even when this one is a
            # no-op otherwise.
            trade.peak_gain_pct = max(trade.peak_gain_pct or gain_pct, gain_pct)

            exit_reason: Optional[str] = None
            price_stop_loss_pct = settings.WEATHER_PRICE_STOP_LOSS_PCT

            # ── Exit condition 1: trailing profit stop ────────────────────────
            #
            # Root-caused 2026-08-17: a flat profit target sold the instant
            # gain_pct crossed WEATHER_PROFIT_TARGET_PCT (50%), regardless of
            # entry price. On a 3c tail-bet entry, +50% is still just 4.5c —
            # nowhere near "this is basically decided" — yet a real position
            # (KXHIGHMIA-26AUG18-B93.5, two $100 entries at 3c) was sold the
            # moment it hit 5c for a ~$67 gain, when holding to a winning
            # settlement would have paid ~$3,233. WEATHER_PROFIT_TARGET_PCT
            # is now the ACTIVATION threshold for a trailing stop instead of
            # an immediate sell: once gain has ever reached that level, exit
            # only once it has since retraced off its peak by the amount
            # _trailing_stop_giveback() computes (2026-08-21: now scales with
            # peak size instead of a flat percentage — see its own docstring
            # and WEATHER_TRAILING_STOP_RATIO in config.py) — letting a
            # genuine tail-bet winner keep running while still protecting
            # the gain already banked if it reverses.
            #
            # Both thresholds tighten in the overnight de-risk window (see
            # _is_overnight_derisk): less room to run once the diurnal peak
            # is long past and the position would otherwise carry overnight
            # settlement-reporting-gap risk for a smaller marginal edge.
            city_key_hint = _city_key_from_ticker(ticker)
            derisk = _is_overnight_derisk(city_key_hint) if city_key_hint else False
            activation_pct = settings.WEATHER_PROFIT_TARGET_PCT * (OVERNIGHT_ACTIVATION_MULTIPLIER if derisk else 1.0)
            trail_pct      = _trailing_stop_giveback(trade.peak_gain_pct or 0.0) * (OVERNIGHT_TRAIL_MULTIPLIER if derisk else 1.0)

            # Gated behind WEATHER_EARLY_EXITS_ENABLED (see its docstring in
            # config.py) — peak tracking above still runs unconditionally so
            # the trail stays accurate for whenever this is switched back on.
            if (
                settings.WEATHER_EARLY_EXITS_ENABLED
                and trade.peak_gain_pct >= activation_pct
                and gain_pct <= trade.peak_gain_pct - trail_pct
            ):
                exit_reason = (
                    f"TRAILING_STOP{' [EVENING]' if derisk else ''}: {gain_pct:+.0%} gain, "
                    f"down from peak {trade.peak_gain_pct:+.0%} "
                    f"(entry {entry_price_per_contract:.2f}, current {sell_price:.2f}, "
                    f"activation ≥{activation_pct:.0%}, trail {trail_pct:.0%})"
                )

            # ── Exit condition 2: METAR physical invalidation ────────────────
            if exit_reason is None:
                resolved = await _resolve_city_from_ticker(ticker, client)
                if resolved is not None and resolved[1].get("direction") is not None:
                    city_key, parsed = resolved
                    target_date = parsed.get("target_date")

                    if target_date == _date.today():
                        parsed_dir = parsed["direction"]
                        metric = parsed.get("metric", "high")

                        # Root-caused 2026-08-16: this only ever handled
                        # simple above/below contracts and silently skipped
                        # "between" brackets — most of what actually gets
                        # traded — leaving them with no physical stop-loss
                        # at all. Shares _evaluate_between_bracket_outcome
                        # with the pending-order sweep and the NWS exit job.
                        if parsed_dir == "between":
                            floor_f = parsed.get("floor_f")
                            cap_f = parsed.get("cap_f")
                            if floor_f is not None and cap_f is not None:
                                # 2026-08-23: metric-aware, mirroring the
                                # pending-order METAR sweep's same fix.
                                if metric == "low":
                                    observed = await fetch_station_low_water_mark(city_key, target_date)
                                else:
                                    observed = await fetch_station_high_water_mark(city_key, target_date)
                                if observed is not None:
                                    bias = CITY_CONFIG.get(city_key, {}).get("station_bias_f", 0.0)
                                    corrected = observed + bias
                                    outcome = _evaluate_between_bracket_outcome(city_key, floor_f, cap_f, corrected)
                                    our_side_loses = outcome is not None and (
                                        (outcome == "yes_wins" and trade.direction == "no") or
                                        (outcome == "no_wins"  and trade.direction == "yes")
                                    )
                                    if our_side_loses:
                                        exit_reason = (
                                            f"METAR_STOP_LOSS: {city_key} {metric}-water-mark "
                                            f"{corrected:.1f}°F vs band [{floor_f:.1f}, {cap_f:.1f}]°F "
                                            f"({outcome}). Position already decided against our "
                                            f"{trade.direction} side."
                                        )
                        else:
                            try:
                                metar = await fetch_metar_current(city_key)
                            except Exception:
                                metar = None

                            if metar is not None:
                                current_temp = metar.observed_temp_f
                                threshold    = parsed["threshold_f"]

                                if metric == "low":
                                    # 2026-08-23: mirror of the high-side check
                                    # below — see the pending-order METAR
                                    # sweep's identical fix for the physical
                                    # reasoning (a single live reading is a
                                    # valid bound on the day's still-to-come
                                    # low regardless of time of day).
                                    betting_stays_above = (
                                        (trade.direction == "yes" and parsed_dir == "above") or
                                        (trade.direction == "no"  and parsed_dir == "below")
                                    )
                                    if betting_stays_above:
                                        floor = threshold
                                        if current_temp <= floor + _KILL_SWITCH_BUFFER_F:
                                            exit_reason = (
                                                f"METAR_STOP_LOSS: {metar.station} "
                                                f"{current_temp:.1f}°F <= floor "
                                                f"{floor:.1f}°F + {_KILL_SWITCH_BUFFER_F}°F. "
                                                f"Live ground temperature ({current_temp:.1f}°F) "
                                                f"has already breached bracket floor."
                                            )
                                else:
                                    # The position loses if the daily high crosses the
                                    # bracket ceiling (same logic as the entry kill switch).
                                    betting_stays_below = (
                                        (trade.direction == "yes" and parsed_dir == "below") or
                                        (trade.direction == "no"  and parsed_dir == "above")
                                    )
                                    if betting_stays_below:
                                        ceiling = threshold
                                        if current_temp >= ceiling - _KILL_SWITCH_BUFFER_F:
                                            exit_reason = (
                                                f"METAR_STOP_LOSS: {metar.station} "
                                                f"{current_temp:.1f}°F >= ceiling "
                                                f"{ceiling:.1f}°F − {_KILL_SWITCH_BUFFER_F}°F. "
                                                f"Order rejected: Live ground temperature "
                                                f"({current_temp:.1f}°F) has breached bracket threshold."
                                            )

            # ── Exit condition 3: price stop-loss ────────────────────────────
            # Also gated behind WEATHER_EARLY_EXITS_ENABLED — see condition 1's
            # comment above. Condition 2 (METAR physical invalidation, above)
            # stays unconditional regardless of this flag: it only fires once
            # the outcome is already physically locked in, not a guess.
            if settings.WEATHER_EARLY_EXITS_ENABLED and exit_reason is None and gain_pct <= -price_stop_loss_pct:
                exit_reason = (
                    f"PRICE_STOP_LOSS: {gain_pct:+.0%} loss "
                    f"(entry {entry_price_per_contract:.2f}, "
                    f"current {sell_price:.2f}, floor −{price_stop_loss_pct:.0%})"
                )

            if exit_reason is None:
                continue

            # ── Execute the liquidation (real sell in live mode, simulated
            #    fill at the live bid in sim mode) ────────────────────────────
            try:
                sell_order_id = None
                if not sim_mode:
                    sell_resp = await client.sell_position(
                        ticker, abs_count, holding_side, sell_price
                    )
                    sell_order = sell_resp.get("order", {})
                    sell_order_id = sell_order.get("order_id")

                # Mark the DB trade as settled via liquidation
                trade.settled = True
                trade.settlement_time = datetime.utcnow()
                trade.settlement_value = sell_price  # exit price per contract
                trade.pnl = round(unrealised_pnl, 2)
                trade.result = "win" if unrealised_pnl > 0 else "loss"
                trade.order_status = "filled"
                trade.execution_type = "liquidated"

                # Update BotState: return proceeds and track P&L
                if state:
                    state.bankroll += sell_proceeds
                    state.total_pnl = (state.total_pnl or 0.0) + unrealised_pnl
                    if unrealised_pnl > 0:
                        state.winning_trades = (state.winning_trades or 0) + 1

                liquidated_count += 1
                log_event(
                    "trade" if unrealised_pnl >= 0 else "warning",
                    f"AUTO-LIQUIDATE{' [SIM]' if sim_mode else ''} {ticker}: "
                    f"{abs_count:.2f} {holding_side.upper()} @ {sell_price:.2f} | "
                    f"pnl {unrealised_pnl:+.2f} | {exit_reason}",
                    {
                        "ticker":        ticker,
                        "holding_side":  holding_side,
                        "count":         round(abs_count, 2),
                        "sell_price":    round(sell_price, 4),
                        "entry_price":   round(entry_price_per_contract, 4),
                        "unrealised_pnl": round(unrealised_pnl, 4),
                        "gain_pct":      round(gain_pct, 4),
                        "exit_reason":   exit_reason,
                        "sell_order_id": sell_order_id,
                        "simulated":     sim_mode,
                    },
                )

            except Exception as e:
                logger.error(
                    f"position_liquidator: sell order FAILED for {ticker}: {e}"
                )
                log_event(
                    "error",
                    f"Liquidation FAILED for {ticker}: {e}",
                    {"ticker": ticker, "error": str(e)},
                )

        # Always commit — even when nothing was liquidated this cycle, the
        # trailing-stop peak_gain_pct tracking above still needs to persist,
        # otherwise "peak" would reset to the current price every cycle and
        # a retracement from an earlier peak could never be detected.
        db.commit()
        if liquidated_count > 0:
            log_event(
                "success",
                f"Position liquidator: closed {liquidated_count} position(s)",
            )

    except Exception as e:
        logger.error(f"position_liquidator_job error: {e}")
        db.rollback()
    finally:
        db.close()


async def nws_observation_and_exit_job():
    """
    Early-close Kalshi weather positions via two independent triggers:
      - "settlement": NWS outcome is already DECIDED — force-settle at 1.0/0.0.
      - "trend_stop" (added 2026-08-17): outcome NOT yet decided, but the live
        METAR warming trend now projects a breach by the diurnal peak. This is
        a projection, not a certainty, so it sells at the live market price
        (a real stop-loss/take-profit) instead of force-settling as a win/loss
        — see evaluate_open_positions_for_exit's docstring for the rationale
        (this replaces what used to be a same-day entry time-gate).
    """
    from backend.core.weather_signals import evaluate_open_positions_for_exit
    from backend.core.settlement import calculate_pnl, update_bot_state_with_settlements
    from backend.data.kalshi_client import KalshiClient, kalshi_credentials_present
    from backend.data.kalshi_markets import _extract_prices_from_orderbook

    db = SessionLocal()
    try:
        client = KalshiClient() if kalshi_credentials_present() else None
        exits = await evaluate_open_positions_for_exit(db, client)
        if not exits:
            return

        newly_settled = []
        for rec in exits:
            trade = db.query(Trade).get(rec["trade_id"])
            if not trade or trade.settled:
                continue

            if rec["exit_type"] == "trend_stop":
                # Sell at the live market price — this is a stop-loss on a
                # projection, not a real settlement, so no settlement_value.
                try:
                    ob_data = await client.get_orderbook(trade.market_ticker)
                    prices, _ = _extract_prices_from_orderbook(ob_data)
                except Exception as e:
                    logger.warning(f"trend_stop: orderbook fetch failed for {trade.market_ticker}: {e}")
                    continue
                if prices is None:
                    continue

                sell_price = prices["yes_bid"] if trade.direction == "yes" else prices["no_bid"]
                contracts = trade.size / trade.entry_price if trade.entry_price else 0.0
                sell_proceeds = contracts * sell_price
                pnl = sell_proceeds - trade.size

                trade.settled = True
                trade.settlement_time = datetime.utcnow()
                trade.settlement_value = sell_price  # exit price, not a 1.0/0.0 outcome
                trade.pnl = round(pnl, 2)
                trade.result = "win" if pnl > 0 else ("loss" if pnl < 0 else "push")
                trade.execution_type = "liquidated"
                # A real sale at the live bid, not a projection — nothing to
                # reconcile later, unlike the nws_early branch below.
                trade.settlement_source = "trend_stop"

                newly_settled.append(trade)
                log_event(
                    "trade",
                    f"Trend-stop exit: {trade.market_ticker} → sold @ {sell_price:.2f} "
                    f"| {rec['reason']} | pnl={pnl:+.2f}",
                )
                continue

            settlement_value = 1.0 if rec["outcome"] == "yes_wins" else 0.0
            pnl = calculate_pnl(trade, settlement_value)

            trade.settled = True
            trade.settlement_time = datetime.utcnow()
            trade.settlement_value = settlement_value
            trade.pnl = pnl
            trade.result = "win" if pnl > 0 else ("loss" if pnl < 0 else "push")
            # A projection off a live NWS reading crossing EXIT_BUFFER_F, not
            # Kalshi's own official result — reconcile_early_settlements()
            # (settlement.py) verifies this against the real outcome once
            # it's available and corrects the ledger if they disagree.
            trade.settlement_source = "nws_early"

            if trade.signal_id:
                sig = db.query(Signal).filter(Signal.id == trade.signal_id).first()
                if sig:
                    actual = "yes" if settlement_value == 1.0 else "no"
                    sig.actual_outcome = actual
                    sig.outcome_correct = (sig.direction == actual)
                    sig.settlement_value = settlement_value
                    sig.settled_at = datetime.utcnow()

            newly_settled.append(trade)
            log_event(
                "trade",
                f"NWS early-settle: {trade.market_ticker} → "
                f"{'WIN' if pnl > 0 else 'LOSS'} | {rec['reason']} | pnl={pnl:+.2f}",
            )

        await update_bot_state_with_settlements(db, newly_settled)
        log_event("info", f"NWS exit check: {len(newly_settled)} early exit(s)")

    except Exception as e:
        logger.error(f"nws_observation_and_exit_job error: {e}")
        db.rollback()
    finally:
        db.close()


def start_scheduler():
    """Start the background scheduler for weather trading."""
    global scheduler

    if scheduler is not None and scheduler.running:
        log_event("warning", "Scheduler already running")
        return

    if not settings.WEATHER_ENABLED:
        log_event("warning", "Weather trading is disabled (WEATHER_ENABLED=false)")
        return

    scheduler = AsyncIOScheduler()

    scan_seconds = settings.WEATHER_SCAN_INTERVAL_SECONDS
    settle_seconds = settings.SETTLEMENT_INTERVAL_SECONDS

    scheduler.add_job(
        weather_scan_and_trade_job,
        IntervalTrigger(seconds=scan_seconds),
        id="weather_scan",
        replace_existing=True,
        max_instances=1,
    )

    scheduler.add_job(
        settlement_job,
        IntervalTrigger(seconds=settle_seconds),
        id="settlement_check",
        replace_existing=True,
        max_instances=1
    )

    scheduler.add_job(
        heartbeat_job,
        IntervalTrigger(minutes=1),
        id="heartbeat",
        replace_existing=True,
        max_instances=1
    )

    scheduler.add_job(
        reconciliation_job,
        IntervalTrigger(hours=1),
        id="reconciliation_check",
        replace_existing=True,
        max_instances=1,
    )

    scheduler.add_job(
        nws_observation_and_exit_job,
        IntervalTrigger(seconds=scan_seconds),
        id="nws_exit_check",
        replace_existing=True,
        max_instances=1,
    )

    scheduler.add_job(
        check_pending_orders_job,
        IntervalTrigger(seconds=60),
        id="pending_order_check",
        replace_existing=True,
        max_instances=1,
    )

    scheduler.add_job(
        position_liquidator_job,
        IntervalTrigger(seconds=60),
        id="position_liquidator",
        replace_existing=True,
        max_instances=1,
    )

    scheduler.start()
    log_event("success", "Weather trading scheduler started", {
        "scan_interval": f"{scan_seconds}s",
        "settlement_interval": f"{settle_seconds}s",
        "min_edge": f"{settings.WEATHER_MIN_EDGE_THRESHOLD:.0%}",
        "cities": settings.WEATHER_CITIES,
    })

    # No manual kick-off here: APScheduler's IntervalTrigger fires an initial
    # run immediately on start() (start_date defaults to now), so this used
    # to launch a second, untracked concurrent scan on every scheduler start
    # (including every --reload restart) — see _scan_lock above for why that
    # was risky with live orders on the line.


def stop_scheduler():
    """Stop the background scheduler."""
    global scheduler

    if scheduler is None or not scheduler.running:
        log_event("info", "Scheduler not running")
        return

    scheduler.shutdown(wait=False)
    scheduler = None
    log_event("info", "Scheduler stopped")


def is_scheduler_running() -> bool:
    """Check if scheduler is currently running."""
    return scheduler is not None and scheduler.running


async def run_manual_scan():
    """Trigger a manual weather market scan."""
    log_event("info", "Manual weather scan triggered")
    await weather_scan_and_trade_job()


async def run_manual_settlement():
    """Trigger a manual settlement check."""
    log_event("info", "Manual settlement triggered")
    await settlement_job()
