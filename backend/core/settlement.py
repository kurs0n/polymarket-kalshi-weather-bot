"""Trade settlement logic for weather markets on Polymarket and Kalshi."""
import httpx
import json
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Tuple
from sqlalchemy.orm import Session

from backend.models.database import Trade, BotState, Signal

logger = logging.getLogger("trading_bot")


async def fetch_polymarket_resolution(market_id: str, event_slug: Optional[str] = None) -> Tuple[bool, Optional[float]]:
    """
    Fetch actual market resolution from Polymarket API.

    Returns: (is_resolved, settlement_value)
        - settlement_value: 1.0 if Yes won, 0.0 if No won
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            if event_slug:
                response = await client.get(
                    "https://gamma-api.polymarket.com/events",
                    params={"slug": event_slug}
                )
                response.raise_for_status()
                events = response.json()

                if events:
                    event = events[0] if isinstance(events, list) else events
                    markets = event.get("markets", [])
                    if markets:
                        return _parse_market_resolution(markets[0])

            url = f"https://gamma-api.polymarket.com/markets/{market_id}"
            response = await client.get(url)

            if response.status_code == 404:
                return await _search_market_in_events(market_id)

            response.raise_for_status()
            market = response.json()
            return _parse_market_resolution(market)

    except Exception as e:
        logger.warning(f"Failed to fetch resolution for {event_slug or market_id}: {e}")
        return False, None


async def _search_market_in_events(market_id: str) -> Tuple[bool, Optional[float]]:
    """Search for market in events (both active and closed)."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            for closed in [True, False]:
                params = {
                    "closed": str(closed).lower(),
                    "limit": 200
                }
                response = await client.get(
                    "https://gamma-api.polymarket.com/events",
                    params=params
                )
                response.raise_for_status()
                events = response.json()

                for event in events:
                    for market in event.get("markets", []):
                        if str(market.get("id")) == str(market_id):
                            return _parse_market_resolution(market)

        return False, None

    except Exception as e:
        logger.warning(f"Failed to search for market {market_id}: {e}")
        return False, None


def _parse_market_resolution(market: dict) -> Tuple[bool, Optional[float]]:
    """
    Parse market data to determine if resolved and outcome.

    - outcomePrices[0] > 0.99 -> Yes won
    - outcomePrices[0] < 0.01 -> No won
    """
    is_closed = market.get("closed", False)

    if not is_closed:
        return False, None

    outcome_prices = market.get("outcomePrices", [])
    if not outcome_prices:
        return False, None

    try:
        if isinstance(outcome_prices, str):
            outcome_prices = json.loads(outcome_prices)

        first_price = float(outcome_prices[0]) if outcome_prices else 0.5

        if first_price > 0.99:
            logger.info(f"Market {market.get('id')} resolved: YES won")
            return True, 1.0
        elif first_price < 0.01:
            logger.info(f"Market {market.get('id')} resolved: NO won")
            return True, 0.0
        else:
            return False, None

    except (ValueError, IndexError, TypeError) as e:
        logger.warning(f"Failed to parse outcome prices: {e}")
        return False, None


def calculate_pnl(trade: Trade, settlement_value: float) -> float:
    """
    Calculate net P&L for a trade given the settlement value.

    settlement_value: 1.0 if Yes outcome, 0.0 if No outcome

    Root-caused 2026-08-17: trade.size is the DOLLAR amount committed at
    entry (see execute_paper_trade/execute_live_trade docstrings — "Dollar
    amount to commit" — and the live-fill path, which stores actual
    maker/taker_fill_cost_dollars into it). The scheduler debits that full
    dollar amount from bankroll immediately at entry
    (`state.bankroll -= trade_size`).
    This function previously treated trade.size as a CONTRACT COUNT instead
    (cost = size * entry_price), which silently threw away most of every
    stake: a losing trade was double-charged (full stake gone at entry,
    then an extra size*entry_price debited again here) and — worse — a
    WINNING trade still came out net negative, because the entry debit
    removed the whole stake but this formula only ever credited back a
    small fraction of it. Confirmed live: $553 of a $10,000 sim bankroll
    had already leaked out of just 7 trades before this fix.
    Correct formula, given size = dollars staked buying (size / entry_price)
    contracts at $1 payout each: win → net profit = size*(1-price)/price;
    loss → net loss = -size (the whole stake, already reflected by the
    entry-time debit — see update_bot_state_with_settlements for how the
    stake is added back before this net pnl is applied, avoiding a double
    subtraction).
    """
    direction = trade.direction
    if direction == "up":
        direction = "yes"
    elif direction == "down":
        direction = "no"

    won = (direction == "yes" and settlement_value == 1.0) or \
          (direction == "no" and settlement_value == 0.0)

    if won:
        pnl = trade.size * (1.0 - trade.entry_price) / trade.entry_price
    else:
        pnl = -trade.size

    return round(pnl, 2)


async def check_weather_settlement(trade: Trade) -> Tuple[bool, Optional[float], Optional[float]]:
    """
    Check if a weather trade's market has settled.
    Routes to the correct platform's resolution method.
    """
    platform = getattr(trade, 'platform', 'polymarket') or 'polymarket'

    if platform == "kalshi":
        is_resolved, settlement_value = await _fetch_kalshi_resolution(trade.market_ticker)
    else:
        is_resolved, settlement_value = await fetch_polymarket_resolution(
            trade.market_ticker,
            event_slug=trade.event_slug,
        )

    if is_resolved and settlement_value is not None:
        pnl = calculate_pnl(trade, settlement_value)
        return True, settlement_value, pnl

    return False, None, None


async def _fetch_kalshi_resolution(ticker: str) -> Tuple[bool, Optional[float]]:
    """Fetch resolution status for a Kalshi market."""
    try:
        from backend.data.kalshi_client import KalshiClient, kalshi_credentials_present

        if not kalshi_credentials_present():
            return False, None

        client = KalshiClient()
        data = await client.get_market(ticker)
        market = data.get("market", data)

        status = market.get("status", "")
        result = market.get("result", "")

        if status in ("finalized", "determined") and result:
            if result == "yes":
                return True, 1.0
            elif result == "no":
                return True, 0.0

        return False, None

    except Exception as e:
        logger.warning(f"Failed to fetch Kalshi resolution for {ticker}: {e}")
        return False, None


async def settle_pending_trades(db: Session) -> List[Trade]:
    """Process all pending weather trades for settlement."""
    try:
        pending = db.query(Trade).filter(Trade.settled == False).all()
    except Exception as e:
        logger.error(f"Failed to query pending trades: {e}")
        return []

    if not pending:
        logger.info("No pending trades to settle")
        return []

    logger.info(f"Checking {len(pending)} pending trades for settlement...")
    settled_trades = []

    for trade in pending:
        try:
            is_settled, settlement_value, pnl = await check_weather_settlement(trade)

            if is_settled and settlement_value is not None:
                trade.settled = True
                trade.settlement_value = settlement_value
                trade.pnl = pnl
                trade.settlement_time = datetime.utcnow()
                # This IS the official Kalshi/Polymarket resolution — ground
                # truth, never needs reconciling against itself.
                trade.settlement_source = "official"

                if pnl is not None and pnl > 0:
                    trade.result = "win"
                elif pnl is not None and pnl < 0:
                    trade.result = "loss"
                else:
                    trade.result = "push"

                settled_trades.append(trade)

                if trade.signal_id:
                    linked_signal = db.query(Signal).filter(Signal.id == trade.signal_id).first()
                    if linked_signal:
                        actual_outcome = "yes" if settlement_value == 1.0 else "no"
                        linked_signal.actual_outcome = actual_outcome
                        linked_signal.outcome_correct = (linked_signal.direction == actual_outcome)
                        linked_signal.settlement_value = settlement_value
                        linked_signal.settled_at = datetime.utcnow()
        except Exception as e:
            logger.error(f"Failed to settle trade {trade.id}: {e}")
            continue

    if settled_trades:
        try:
            db.commit()
            logger.info(f"Settled {len(settled_trades)} trades")
        except Exception as e:
            logger.error(f"Failed to commit settlements: {e}")
            db.rollback()
            return []
    else:
        logger.info("No trades ready for settlement (markets still open)")

    return settled_trades


# How long to wait after an nws_early settlement before checking it against
# the official result — Kalshi weather markets settle off the next-morning
# NWS climate report, so anything younger than this would almost always
# just come back "not resolved yet" and waste an API call.
RECONCILE_MIN_AGE_HOURS = 12.0


async def reconcile_early_settlements(db: Session) -> List[Trade]:
    """
    Verify every nws_early-settled trade old enough to likely have an
    official Kalshi result by now, and correct the ledger if the early
    projection disagreed with it.

    Added 2026-08-20 (see execution.py's phantom-fill fix from the same
    audit): the NWS early-exit path (weather_signals.py's "settlement"
    exit_type, EXIT_BUFFER_F=2.0°F) force-settles a trade off a live
    observation before Kalshi has officially resolved the market, and
    nothing previously ever checked that call against the real outcome —
    once settled=True, settle_pending_trades()'s official-resolution path
    (filtered to settled==False) would never look at it again. If the 2°F
    buffer was ever insufficient, the recorded P&L would be silently wrong
    forever. This closes that gap: mismatches get their pnl/result/bankroll
    corrected to match the true outcome, not just flagged.

    Returns the list of trades that were found to be MISMATCHED and
    corrected (empty list = everything checked either matched or isn't
    resolved yet).
    """
    cutoff = datetime.utcnow() - timedelta(hours=RECONCILE_MIN_AGE_HOURS)
    try:
        candidates = (
            db.query(Trade)
            .filter(
                Trade.settlement_source == "nws_early",
                Trade.reconciled_at.is_(None),
                Trade.settlement_time.isnot(None),
                Trade.settlement_time <= cutoff,
            )
            .all()
        )
    except Exception as e:
        logger.error(f"Failed to query trades pending reconciliation: {e}")
        return []

    if not candidates:
        return []

    logger.info(f"Reconciling {len(candidates)} early-settled trade(s) against official results...")
    mismatched: List[Trade] = []

    for trade in candidates:
        try:
            is_resolved, official_value = await check_weather_settlement_source(trade)
        except Exception as e:
            logger.error(f"Reconciliation check failed for trade {trade.id}: {e}")
            continue

        if not is_resolved or official_value is None:
            # Not officially resolved yet — leave reconciled_at unset and
            # try again on the next pass.
            continue

        if official_value == trade.settlement_value:
            trade.reconciled_at = datetime.utcnow()
            trade.reconciliation_mismatch = False
            continue

        # Mismatch — the early NWS call got it wrong. Correct pnl/result and
        # apply the delta to bankroll/total_pnl (the entry stake was already
        # debited once at entry time; only the payout difference matters —
        # same principle as update_bot_state_with_settlements).
        old_pnl = trade.pnl or 0.0
        old_result = trade.result
        correct_pnl = calculate_pnl(trade, official_value)
        delta = correct_pnl - old_pnl

        trade.settlement_value = official_value
        trade.pnl = correct_pnl
        trade.result = "win" if correct_pnl > 0 else ("loss" if correct_pnl < 0 else "push")
        trade.reconciled_at = datetime.utcnow()
        trade.reconciliation_mismatch = True

        if trade.signal_id:
            linked_signal = db.query(Signal).filter(Signal.id == trade.signal_id).first()
            if linked_signal:
                actual_outcome = "yes" if official_value == 1.0 else "no"
                linked_signal.actual_outcome = actual_outcome
                linked_signal.outcome_correct = (linked_signal.direction == actual_outcome)
                linked_signal.settlement_value = official_value

        state = db.query(BotState).first()
        if state:
            state.total_pnl += delta
            state.bankroll += delta
            if old_result == "win" and trade.result != "win":
                state.winning_trades -= 1
            elif old_result != "win" and trade.result == "win":
                state.winning_trades += 1

        logger.error(
            f"[RECONCILIATION MISMATCH] Trade {trade.id} ({trade.market_ticker}): "
            f"nws_early called it {old_result} (pnl={old_pnl:+.2f}) but the official "
            f"result was {'YES' if official_value == 1.0 else 'NO'} — corrected to "
            f"{trade.result} (pnl={correct_pnl:+.2f}, delta={delta:+.2f})"
        )
        mismatched.append(trade)

    try:
        db.commit()
    except Exception as e:
        logger.error(f"Failed to commit reconciliation results: {e}")
        db.rollback()
        return []

    if mismatched:
        logger.error(f"Reconciliation found {len(mismatched)} mismatch(es) — see above for details")
    else:
        logger.info(f"Reconciliation: {len(candidates)} checked, all matched or still pending")

    return mismatched


async def check_weather_settlement_source(trade: Trade) -> Tuple[bool, Optional[float]]:
    """
    Fetch the OFFICIAL resolution only (no pnl computed) — used by
    reconcile_early_settlements to check a trade that's already settled
    against the true outcome, as opposed to check_weather_settlement's
    settle-a-still-pending-trade use.
    """
    platform = getattr(trade, 'platform', 'polymarket') or 'polymarket'
    if platform == "kalshi":
        return await _fetch_kalshi_resolution(trade.market_ticker)
    return await fetch_polymarket_resolution(trade.market_ticker, event_slug=trade.event_slug)


async def update_bot_state_with_settlements(db: Session, settled_trades: List[Trade]) -> None:
    """Update bot state with P&L from settled trades."""
    if not settled_trades:
        return

    try:
        state = db.query(BotState).first()
        if not state:
            logger.warning("Bot state not found")
            return

        for trade in settled_trades:
            if trade.pnl is not None:
                state.total_pnl += trade.pnl
                # The stake (trade.size) was already fully debited from
                # bankroll at entry (scheduler.py: state.bankroll -=
                # trade_size). trade.pnl is the NET change (see
                # calculate_pnl), so crediting it back alone would still be
                # missing the returned stake on a win and would double-
                # subtract it on a loss. Adding size + pnl back returns
                # exactly the settlement payout: size/entry_price on a win,
                # 0 on a loss. Root-caused alongside calculate_pnl, 2026-08-17.
                state.bankroll += trade.size + trade.pnl
                if trade.result == "win":
                    state.winning_trades += 1

        db.commit()
        logger.info(f"Updated bot state: Bankroll ${state.bankroll:.2f}, P&L ${state.total_pnl:+.2f}")
    except Exception as e:
        logger.error(f"Failed to update bot state: {e}")
        db.rollback()
