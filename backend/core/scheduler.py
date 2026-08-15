"""Background scheduler for weather temperature trading."""
import asyncio
from datetime import datetime, timedelta
from typing import List, Optional
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import func
import logging

from backend.config import settings
from backend.models.database import SessionLocal, Trade, BotState, Signal

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("trading_bot")

# Global scheduler instance
scheduler: Optional[AsyncIOScheduler] = None

# Event log for terminal display (in-memory, last 200 events)
event_log: List[dict] = []
MAX_LOG_SIZE = 200

# ──────────────────────────────────────────────────────────────────────────────
# Position liquidator thresholds (configurable here, not in settings, because
# these are trading-strategy parameters rather than environment config).
# ──────────────────────────────────────────────────────────────────────────────
PROFIT_TARGET_PCT   = 0.50   # sell when unrealised gain ≥ 50% of entry cost
PRICE_STOP_LOSS_PCT = 0.80   # sell when position has lost ≥ 80% of entry value

# Re-entrancy guard: weather_scan_and_trade_job places live orders, so two
# concurrent invocations (e.g. the scheduled interval firing while a manual
# scan or a startup task is still mid-flight) could both pass the "no open
# position exists" dedup check before either commits its Trade row, and
# double up a real Kalshi order for the same city/date bracket. This lock
# makes concurrent execution structurally impossible instead of relying on
# every caller to coordinate timing.
_scan_lock = asyncio.Lock()


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


async def weather_scan_and_trade_job():
    """
    Background job: Scan weather temperature markets, generate signals, execute trades.
    Runs every WEATHER_SCAN_INTERVAL_SECONDS when WEATHER_ENABLED.

    Thin wrapper around _run_weather_scan_and_trade that serialises execution
    via _scan_lock — see the lock's comment for why that matters here.
    """
    if _scan_lock.locked():
        log_event(
            "warning",
            "Weather scan already in progress — skipping this invocation "
            "to avoid placing duplicate live orders for the same signal.",
        )
        return

    async with _scan_lock:
        await _run_weather_scan_and_trade()


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
            MAX_WEATHER_ALLOCATION = settings.WEATHER_MAX_ALLOCATION

            # --- Daily loss circuit breaker ---
            today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            daily_pnl = db.query(func.coalesce(func.sum(Trade.pnl), 0.0)).filter(
                Trade.settled == True,
                Trade.settlement_time >= today_start
            ).scalar()

            if daily_pnl <= -settings.DAILY_LOSS_LIMIT:
                log_event("warning", f"Daily loss limit hit: ${daily_pnl:.2f} (limit: -${settings.DAILY_LOSS_LIMIT:.0f}). Stopping trades.")
                return

            total_pending = db.query(Trade).filter(Trade.settled == False).count()
            if total_pending >= settings.MAX_TOTAL_PENDING_TRADES:
                log_event("info", f"Max pending trades reached ({total_pending}/{settings.MAX_TOTAL_PENDING_TRADES})")
                return

            weather_pending = db.query(func.coalesce(func.sum(Trade.size), 0.0)).filter(
                Trade.settled == False,
                Trade.market_type == "weather",
            ).scalar()

            if weather_pending >= MAX_WEATHER_ALLOCATION:
                log_event("info", f"Weather allocation limit reached: ${weather_pending:.0f}/${MAX_WEATHER_ALLOCATION:.0f}")
                return

            trades_executed = 0
            for signal in actionable[:MAX_TRADES_PER_SCAN]:
                # Guard 1: exact ticker — already have this specific contract.
                # Guard 2: city/date prefix — already have ANY bracket for this
                #   city on this date, regardless of strike or direction.
                #   Prevents capital from being split across correlated brackets
                #   (e.g. B83.5 and B84.0 for NYC on the same day).
                from backend.data.kalshi_markets import CITY_SERIES
                city_key    = signal.market.city_key
                target_date = signal.market.target_date
                series      = CITY_SERIES.get(city_key, "")
                date_str    = target_date.strftime("%y%b%d").upper()
                ticker_prefix = f"{series}-{date_str}-"

                today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

                # Block 1: unsettled position — order is currently live on Kalshi.
                open_position = db.query(Trade).filter(
                    Trade.settled == False,
                    Trade.platform == "kalshi",
                    Trade.market_ticker.like(f"{ticker_prefix}%"),
                ).first()

                # Block 2: filled position today — a prior limit order actually executed.
                # settled=True doesn't mean we should re-enter; it means we already
                # accumulated real size. Timeouts are excluded (order_status != timed_out/cancelled).
                filled_position = db.query(Trade).filter(
                    Trade.platform == "kalshi",
                    Trade.market_ticker.like(f"{ticker_prefix}%"),
                    Trade.timestamp >= today_start,
                    Trade.execution_type == "maker_limit",
                    Trade.order_status != "timed_out",
                    Trade.order_status != "cancelled",
                ).first()

                if open_position or filled_position:
                    blocking = open_position or filled_position
                    log_event(
                        "info",
                        f"Skipping {signal.market.market_id}: "
                        f"{'open' if open_position else 'filled'} position already "
                        f"exists for {city_key}/{target_date} ({blocking.market_ticker})",
                    )
                    continue

                trade_size = min(signal.suggested_size, settings.WEATHER_MAX_TRADE_SIZE)
                trade_size = max(trade_size, MIN_TRADE_SIZE)

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
                        log_event(
                            "warning",
                            f"Live trade blocked by physical guardrail: "
                            f"{signal.market.market_id}",
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
                    f"WX {signal.market.city_name}: {signal.direction.upper()} "
                    f"${trade_size:.0f} @ {trade.entry_price:.0%} "
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
        from backend.core.weather_signals import _resolve_city_from_ticker
        from backend.data.weather import fetch_metar_current
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

            metar = await fetch_metar_current(city_key)
            if metar is None:
                continue

            threshold = parsed["threshold_f"]
            parsed_direction = parsed["direction"]

            betting_high_stays_below = (
                (trade.direction == "yes" and parsed_direction == "below") or
                (trade.direction == "no"  and parsed_direction == "above")
            )
            if not betting_high_stays_below:
                continue

            from backend.core.execution import _KILL_SWITCH_BUFFER_F
            ceiling = threshold
            current_temp = metar.observed_temp_f

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


async def position_liquidator_job():
    """
    Query the live Kalshi portfolio, evaluate every open position, and
    automatically sell when any exit condition is satisfied.

    Exit conditions (checked in order):
      1. Profit target  — unrealised gain ≥ PROFIT_TARGET_PCT (default 50%).
      2. METAR stop-loss — live surface temperature has physically breached the
         bracket boundary so the position cannot win; cut losses immediately.
      3. Price stop-loss — market price has collapsed ≥ PRICE_STOP_LOSS_PCT
         (default 80%) below entry, regardless of METAR availability.

    The sell order uses post_only=False so it crosses the spread as a taker
    and is filled immediately at the current best bid price.

    Only runs when SIMULATION_MODE=False — the live Kalshi API is the source
    of truth for positions.  In simulation mode the DB trade records and the
    NWS exit job handle position management instead.
    """
    from backend.config import settings
    if settings.SIMULATION_MODE:
        return

    from backend.data.kalshi_client import KalshiClient, kalshi_credentials_present
    from backend.data.kalshi_markets import _extract_prices_from_orderbook
    from backend.core.weather_signals import _resolve_city_from_ticker
    from backend.data.weather import fetch_metar_current
    from backend.core.execution import _KILL_SWITCH_BUFFER_F
    from datetime import date as _date

    if not kalshi_credentials_present():
        return

    client = KalshiClient()

    # ── 1. Fetch live portfolio positions ─────────────────────────────────────
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

    db = SessionLocal()
    try:
        state = db.query(BotState).first()
        liquidated_count = 0

        for pos in positions:
            # ── Resolve ticker ───────────────────────────────────────────────
            ticker = (
                pos.get("ticker")
                or pos.get("market_ticker")
                or pos.get("market_id")
            )
            if not ticker:
                continue

            # Net signed contract count (positive = long YES, negative = long NO).
            # Handle string fields from the API gracefully.
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

            abs_count = int(round(abs(net_position)))
            # Derive holding side from DB trade (more reliable than API sign
            # when the API representation is ambiguous or pre-netted).
            # Fallback to API sign when no DB record exists.
            api_holding_side = "yes" if net_position > 0 else "no"

            # ── Match to DB trade ────────────────────────────────────────────
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

            holding_side = trade.direction if trade else api_holding_side
            if trade is None:
                logger.debug(
                    f"position_liquidator: no unsettled DB trade for {ticker} "
                    f"(may be manually placed) — skipping"
                )
                continue

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
            entry_cost    = entry_price_per_contract * abs_count
            sell_proceeds = sell_price * abs_count
            unrealised_pnl = sell_proceeds - entry_cost
            gain_pct = unrealised_pnl / entry_cost if entry_cost > 0 else 0.0

            exit_reason: Optional[str] = None

            # ── Exit condition 1: profit target ──────────────────────────────
            if gain_pct >= PROFIT_TARGET_PCT:
                exit_reason = (
                    f"PROFIT_TARGET: {gain_pct:+.0%} gain "
                    f"(entry {entry_price_per_contract:.2f}, "
                    f"current {sell_price:.2f}, target ≥ {PROFIT_TARGET_PCT:.0%})"
                )

            # ── Exit condition 2: METAR physical invalidation ────────────────
            if exit_reason is None:
                resolved = await _resolve_city_from_ticker(ticker, client)
                if resolved is not None and resolved[1].get("direction") is not None:
                    city_key, parsed = resolved
                    target_date = parsed.get("target_date")

                    if target_date == _date.today():
                        try:
                            metar = await fetch_metar_current(city_key)
                        except Exception:
                            metar = None

                        if metar is not None:
                            current_temp   = metar.observed_temp_f
                            threshold      = parsed["threshold_f"]
                            parsed_dir     = parsed["direction"]

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
            if exit_reason is None and gain_pct <= -PRICE_STOP_LOSS_PCT:
                exit_reason = (
                    f"PRICE_STOP_LOSS: {gain_pct:+.0%} loss "
                    f"(entry {entry_price_per_contract:.2f}, "
                    f"current {sell_price:.2f}, floor −{PRICE_STOP_LOSS_PCT:.0%})"
                )

            if exit_reason is None:
                continue

            # ── Execute the liquidation sell order ───────────────────────────
            try:
                sell_resp = await client.sell_position(
                    ticker, abs_count, holding_side, sell_price
                )
                sell_order = sell_resp.get("order", {})
                sell_order_id = sell_order.get("order_id")

                # Mark the DB trade as settled via liquidation
                trade.settled = True
                trade.settlement_time = datetime.utcnow()
                trade.settlement_value = sell_price  # exit price per contract
                trade.pnl = unrealised_pnl
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
                    f"AUTO-LIQUIDATE {ticker}: "
                    f"{abs_count} {holding_side.upper()} @ {sell_price:.2f} | "
                    f"pnl {unrealised_pnl:+.2f} | {exit_reason}",
                    {
                        "ticker":        ticker,
                        "holding_side":  holding_side,
                        "count":         abs_count,
                        "sell_price":    round(sell_price, 4),
                        "entry_price":   round(entry_price_per_contract, 4),
                        "unrealised_pnl": round(unrealised_pnl, 4),
                        "gain_pct":      round(gain_pct, 4),
                        "exit_reason":   exit_reason,
                        "sell_order_id": sell_order_id,
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

        if liquidated_count > 0:
            db.commit()
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
    """Early-settle Kalshi weather positions whose NWS outcome is already confirmed."""
    from backend.core.weather_signals import evaluate_open_positions_for_exit
    from backend.core.settlement import calculate_pnl, update_bot_state_with_settlements
    from backend.data.kalshi_client import KalshiClient, kalshi_credentials_present

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

            settlement_value = 1.0 if rec["outcome"] == "yes_wins" else 0.0
            pnl = calculate_pnl(trade, settlement_value)

            trade.settled = True
            trade.settlement_time = datetime.utcnow()
            trade.settlement_value = settlement_value
            trade.pnl = pnl
            trade.result = "win" if pnl > 0 else ("loss" if pnl < 0 else "push")

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
        log_event("info", f"NWS exit check: {len(newly_settled)} early settlement(s)")

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
