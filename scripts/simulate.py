#!/usr/bin/env python3
"""
Monte Carlo simulation for the weather trading bot — 2026-08-20.

Answers "will it perform as expected" honestly, given how little real
history exists (21 unique settled markets as of this writing).

Two earlier designs of this script were both wrong in instructive ways,
left here in the history rather than pretending the first attempt was
right:

  1. First attempt resampled a real historical trade's CHARACTERISTICS
     *and* its realized win/loss outcome together, deterministically. Any
     trade that happened to win once in this tiny real history then won
     every single time it got resampled across ~100+ simulated trades per
     trial — not Monte Carlo variance, just the same lucky coin flip
     replayed on repeat. Produced a six-figure median 30-day return.

  2. Second attempt fixed that by drawing a fresh independent Bernoulli
     outcome per simulated trade from the MODEL's own stated probability
     for that trade. Still wrong, for a subtler reason: Kelly sizing
     compounded against a probability the model asserts about itself is
     circular — trusting your own confidence as ground truth and then
     compounding on it will ALWAYS look like exponential growth, by
     construction, regardless of whether the edge is real. Produced an
     even more absurd result (five-figure PERCENT median return).

This version separates the two things that were getting conflated: HOW
THE BOT SIZES A BET (its own stated model probability / edge — exactly
what the real bot does, via calculate_kelly_size) from WHAT ACTUALLY
HAPPENS (governed by a "true win rate" that is itself uncertain given
only 21 observations). Each trial draws its own true win rate from
Beta(wins+1, losses+1) — the standard Bayesian posterior over a binomial
rate with a uniform prior — and every simulated trade in that trial
resolves against THAT rate, not the model's self-reported confidence.
Trials where the drawn true rate is poor show the strategy losing money
even though it kept sizing bets as if it had a real edge; trials where
it's good show compounding growth. The SPREAD across trials is the
honest answer to "how sure are we this works," given n=21.

This still cannot invent information that doesn't exist — the width of
that Beta posterior at n=21 is the actual uncertainty in your data, and
no amount of statistical technique shrinks it. Read the warning this
script prints before trusting the percentiles below.

Usage:
    python scripts/simulate.py                       # 30 days, 2000 trials
    python scripts/simulate.py --days 60 --trials 5000
    python scripts/simulate.py --starting-bankroll 5000
"""
import argparse
import random
import statistics
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scipy.stats import beta as beta_dist

from backend.config import settings
from backend.core.sizing import calculate_kelly_size
from backend.models.database import SessionLocal, Trade, BotState


def load_historical_pool():
    """
    One row per unique underlying market (first entry only — a scale-in
    pair is one real trial, not two), from all settled win/loss trades.
    """
    db = SessionLocal()
    try:
        rows = (
            db.query(Trade)
            .filter(Trade.settled == True, Trade.result.in_(["win", "loss"]))
            .order_by(Trade.timestamp.asc())
            .all()
        )
    finally:
        db.close()

    by_ticker = {}
    for t in rows:
        by_ticker.setdefault(t.market_ticker, t)  # first (earliest) entry wins

    pool = []
    for t in by_ticker.values():
        if t.model_probability is None or t.market_price_at_entry is None:
            continue
        direction = "up" if t.direction in ("yes", "up") else "down"
        pool.append({
            "model_probability": t.model_probability,
            "market_price": t.market_price_at_entry,
            "direction": direction,
            "won": t.result == "win",
        })
    return pool


def estimate_daily_trade_rate(pool_size: int, days_live: float) -> float:
    """Observed pace of new distinct-market trades/day since going live."""
    if days_live <= 0:
        return 4.0
    return max(0.5, pool_size / days_live)


def _poisson(lam: float, rng: random.Random) -> int:
    """Small-lambda Poisson sampler (no numpy dependency)."""
    import math
    L = math.exp(-lam)
    k = 0
    p = 1.0
    while True:
        k += 1
        p *= rng.random()
        if p <= L:
            return k - 1


SETTLEMENT_DELAY_HOURS = (6.0, 36.0)  # uniform range — matches observed same-day-to-next-morning settlement


def generate_trial_events(pool, days: int, daily_rate: float, true_win_rate: float, rng: random.Random) -> list:
    """
    Pre-generate one trial's entire candidate-event stream up front —
    added 2026-08-21 to support fair paired comparisons between two sizing
    strategies (e.g. this branch's calculate_kelly_size vs. main's). If the
    randomness were drawn inline inside run_trial as trades are accepted or
    skipped, two runs with different sizing logic would consume `rng` calls
    in different amounts/orders (a skipped trade under a stricter cap still
    "uses up" a slot in one run but not the other) and desync immediately —
    any difference in the results would then be partly just noise from
    drifted random state, not the sizing difference being compared. Every
    candidate event (whether or not any given strategy ends up taking it)
    is fixed here, once, before either strategy sees it.

    Returns a list of (hour, pool_sample, won, settlement_delay_hours).
    """
    total_hours = days * 24
    hourly_rate = daily_rate / 24.0
    events = []
    for hour in range(1, total_hours + 1):
        k = _poisson(hourly_rate, rng)
        for _ in range(k):
            sample = rng.choice(pool)
            won = rng.random() < true_win_rate
            delay = rng.uniform(*SETTLEMENT_DELAY_HOURS)
            events.append((float(hour), sample, won, delay))
    return events


def run_trial(
    pool, days: int, daily_rate: float, starting_bankroll: float, true_win_rate: float,
    kelly_size_fn=None, events: list = None, rng: random.Random = None,
) -> dict:
    """
    One simulated path, event-driven over `days * 24` hours rather than
    resolving each trade instantly. That distinction matters: the live bot
    holds positions for real hours before they settle, during which the
    SAME two exposure guardrails scheduler.py actually enforces
    (WEATHER_MAX_ALLOCATION_PCT of total capital, MAX_TOTAL_PENDING_TRADES
    concurrent positions) throttle how much new capital can deploy. An
    earlier version of this script resolved every trade instantly before
    placing the next, which let it redeploy 100% of a compounding bankroll
    every single trade — never once hitting either cap — and produced
    billion-dollar 30-day outcomes as a direct consequence. Modeling the
    caps faithfully is not optional for this to mean anything.

    Bet SIZING uses each resampled trade's own model-stated edge/
    probability (exactly what the live bot does) via `kelly_size_fn`
    (defaults to this branch's calculate_kelly_size — pass a different
    function, e.g. main branch's, to compare sizing strategies). Bet
    OUTCOME uses this trial's drawn true_win_rate, not the model's
    self-reported confidence — see module docstring for why that
    distinction is the entire point of this redesign.

    `events`: optionally pass a pre-generated event stream from
    generate_trial_events() so multiple sizing strategies can be compared
    against the identical candidate-trade sequence (see that function's
    docstring). If omitted, generates its own from `rng` (or the module's
    shared `random` instance).
    """
    if kelly_size_fn is None:
        kelly_size_fn = calculate_kelly_size
    if events is None:
        events = generate_trial_events(pool, days, daily_rate, true_win_rate, rng or random)

    bankroll = starting_bankroll
    peak = starting_bankroll
    max_drawdown_pct = 0.0
    n_trades = 0
    open_positions = []  # list of (resolve_at_hour, stake, pnl)

    total_hours = days * 24
    event_idx = 0
    hour = 0.0

    while hour < total_hours:
        hour += 1.0

        # Settle anything whose delay has elapsed.
        still_open = []
        for resolve_at, stake, pnl in open_positions:
            if resolve_at <= hour:
                bankroll += stake + pnl  # return stake + net pnl, same accounting as the real bot
                peak = max(peak, bankroll)
                if peak > 0:
                    max_drawdown_pct = max(max_drawdown_pct, (peak - bankroll) / peak)
            else:
                still_open.append((resolve_at, stake, pnl))
        open_positions = still_open

        # Consume every pre-generated candidate event scheduled for this hour.
        while event_idx < len(events) and events[event_idx][0] == hour:
            _, sample, won, delay = events[event_idx]
            event_idx += 1

            weather_pending = sum(stake for _, stake, _ in open_positions)
            free_bankroll = bankroll - weather_pending
            if free_bankroll < settings.WEATHER_MIN_TRADE_SIZE:
                continue

            # Same two guardrails as scheduler.py's weather_scan_and_trade_job.
            if len(open_positions) >= settings.MAX_TOTAL_PENDING_TRADES:
                continue
            total_capital = free_bankroll + weather_pending
            max_allocation = total_capital * settings.WEATHER_MAX_ALLOCATION_PCT
            if weather_pending >= max_allocation:
                continue

            edge = abs(sample["model_probability"] - sample["market_price"])

            size = kelly_size_fn(
                edge=edge,
                probability=sample["model_probability"],
                market_price=sample["market_price"],
                direction=sample["direction"],
                bankroll=bankroll,  # sized off total bankroll, same as the live signal generator
            )
            size = max(size, settings.WEATHER_MIN_TRADE_SIZE)
            size = min(size, free_bankroll, max_allocation - weather_pending)
            if size <= 0:
                continue

            price = sample["market_price"] if sample["direction"] == "up" else (1 - sample["market_price"])
            if price <= 0 or price >= 1:
                continue

            pnl = size * (1 - price) / price if won else -size

            bankroll -= size  # stake debited immediately, same as scheduler.py
            resolve_at = hour + delay
            open_positions.append((resolve_at, size, pnl))
            n_trades += 1

    # Settle whatever's still open at the end of the horizon at its
    # already-determined (but not yet realized) outcome, so ending
    # bankroll reflects everything actually placed during the window.
    for _resolve_at, stake, pnl in open_positions:
        bankroll += stake + pnl
    peak = max(peak, bankroll)
    if peak > 0:
        max_drawdown_pct = max(max_drawdown_pct, (peak - bankroll) / peak)

    return {"ending_bankroll": bankroll, "n_trades": n_trades, "max_drawdown_pct": max_drawdown_pct}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=30, help="Simulated trading days per trial (default: 30)")
    parser.add_argument("--trials", type=int, default=2000, help="Number of Monte Carlo trials (default: 2000)")
    parser.add_argument("--starting-bankroll", type=float, default=None,
                         help="Override starting bankroll (default: live bankroll from BotState)")
    parser.add_argument("--seed", type=int, default=None, help="Random seed for reproducibility")
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    pool = load_historical_pool()
    if len(pool) < 5:
        print(f"Only {len(pool)} unique settled markets in history — too few to simulate anything meaningful. Stopping.")
        return

    wins = sum(1 for p in pool if p["won"])
    losses = len(pool) - wins

    db = SessionLocal()
    try:
        state = db.query(BotState).first()
        live_bankroll = state.bankroll if state else settings.INITIAL_BANKROLL
    finally:
        db.close()

    starting_bankroll = args.starting_bankroll if args.starting_bankroll is not None else live_bankroll
    days_live = max(1.0, (datetime.utcnow() - datetime(2026, 8, 15)).total_seconds() / 86400)
    daily_rate = estimate_daily_trade_rate(len(pool), days_live)

    print("=" * 72)
    print("MONTE CARLO SIMULATION (Bayesian true-rate uncertainty)")
    print("=" * 72)
    print(f"Historical pool: {len(pool)} unique settled markets ({wins}W/{losses}L, deduped, real)")
    print(f"Observed pace: ~{daily_rate:.1f} new trades/day over {days_live:.1f} days live")
    print(f"Starting bankroll: ${starting_bankroll:,.2f}")
    print(f"Simulating: {args.trials} trials x {args.days} days each")
    print()

    # The actual uncertainty in the data: 90% credible interval on the true
    # win rate given only `wins`/`losses` observed, uniform prior.
    lo90 = beta_dist.ppf(0.05, wins + 1, losses + 1)
    hi90 = beta_dist.ppf(0.95, wins + 1, losses + 1)
    print(f"True win rate — 90% credible interval given {len(pool)} observations: [{lo90:.1%}, {hi90:.1%}]")
    if len(pool) < 30:
        print(f"⚠️  WARNING: n={len(pool)} is well below the n≥30 bar used throughout this bot's own")
        print(f"   quant reviews. That credible interval above is genuinely this wide — not a")
        print(f"   simulation artifact. Every trial below draws its own \"true\" win rate from that")
        print(f"   uncertainty and simulates forward as if it were real; the spread in the results is")
        print(f"   the honest price of not yet having enough data, not a bug to fix by simulating more.")
    print()

    results = []
    for _ in range(args.trials):
        true_rate = beta_dist.rvs(wins + 1, losses + 1)
        results.append(run_trial(pool, args.days, daily_rate, starting_bankroll, true_rate))

    endings = sorted(r["ending_bankroll"] for r in results)
    drawdowns = sorted(r["max_drawdown_pct"] for r in results)
    trade_counts = [r["n_trades"] for r in results]

    def pct(data, p):
        idx = min(len(data) - 1, max(0, int(len(data) * p)))
        return data[idx]

    n_profitable = sum(1 for e in endings if e > starting_bankroll)
    n_ruined = sum(1 for e in endings if e < starting_bankroll * 0.5)

    print("RESULTS")
    print("-" * 72)
    print(f"{'Percentile':<15} {'Ending bankroll':<20} {'vs start'}")
    for p, label in [(0.05, "P5 (bad case)"), (0.25, "P25"), (0.50, "P50 (median)"), (0.75, "P75"), (0.95, "P95 (good case)")]:
        val = pct(endings, p)
        pct_change = (val / starting_bankroll - 1) * 100
        print(f"{label:<15} ${val:>15,.2f}   {pct_change:+.1f}%")

    print()
    print(f"P(profitable after {args.days} days):     {n_profitable / args.trials:.1%}")
    print(f"P(lose >50% of bankroll):          {n_ruined / args.trials:.1%}")
    print(f"Median max drawdown along the way: {pct(drawdowns, 0.50):.1%}")
    print(f"P95 max drawdown:                  {pct(drawdowns, 0.95):.1%}")
    print(f"Median trades executed:            {int(statistics.median(trade_counts))}")
    print("=" * 72)


if __name__ == "__main__":
    main()
