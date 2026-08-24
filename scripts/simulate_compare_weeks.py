#!/usr/bin/env python3
"""
Monte Carlo: Week 1 (pre-fix) trade profile vs Week 2-so-far (post-fix)
trade profile, projected forward under identical assumptions — 2026-08-23.

Answers "if the future kept looking like week 1's outcomes, vs week 2's,
how different would the trajectory actually be" — using the same
Beta-posterior, event-driven engine as simulate.py and simulate_compare_
main.py, just with the historical pool split by settlement week instead
of by branch.

IMPORTANT, read before trusting the numbers: Week 2 so far is only 7
settled trades (2 wins). That is a genuinely tiny sample to draw a "true
win rate" posterior from — the Beta credible interval for week 2 will be
much wider than week 1's, and this script prints both intervals explicitly
so that width is visible rather than hidden. This is NOT a claim that
week 2 is proven better or worse; it is what the numbers say IF you
extrapolate each week's small sample forward, with the uncertainty that
implies made explicit.

Unlike simulate_compare_main.py, this does NOT use paired trials — the
two pools are genuinely different populations (different trades), so
there's no meaningful way to force them through identical random draws.
Each week is simulated independently, the standard way.

Usage:
    python scripts/simulate_compare_weeks.py
    python scripts/simulate_compare_weeks.py --days 30 --trials 2000
"""
import argparse
import statistics
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scipy.stats import beta as beta_dist

from backend.config import settings
from backend.models.database import SessionLocal, Trade, BotState

from simulate import generate_trial_events, run_trial, estimate_daily_trade_rate


def load_pool_for_week(start: date, end: date):
    """Same dedup/shape as simulate.py's load_historical_pool(), filtered
    to trades that SETTLED within [start, end] inclusive — settlement week
    is what determines which week's outcome a trade belongs to, matching
    the day-by-day breakdown already used in this project's reviews."""
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
        ts = t.settlement_time or t.timestamp
        if ts is None:
            continue
        d = ts.date()
        if not (start <= d <= end):
            continue
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


def simulate_pool(pool, label, days, trials, starting_bankroll, daily_rate, seed_base):
    wins = sum(1 for p in pool if p["won"])
    losses = len(pool) - wins
    n = len(pool)

    lo90 = beta_dist.ppf(0.05, wins + 1, losses + 1)
    hi90 = beta_dist.ppf(0.95, wins + 1, losses + 1)

    print(f"--- {label} ---")
    print(f"n={n} ({wins}W/{losses}L), observed win rate={wins/n:.1%}" if n else f"n=0")
    print(f"True win rate — 90% credible interval: [{lo90:.1%}, {hi90:.1%}]")
    if n < 30:
        print(f"⚠️  n={n} is well below the n≥30 bar this project holds to — treat the")
        print(f"   interval above as the honest (wide) uncertainty, not a bug.")

    if n < 5:
        print("Too few trades to simulate anything meaningful. Skipping.")
        print()
        return None

    results = []
    for i in range(trials):
        seed_i = seed_base + i
        true_rate = beta_dist.rvs(wins + 1, losses + 1, random_state=seed_i)
        results.append(run_trial(pool, days, daily_rate, starting_bankroll, true_rate))

    endings = sorted(r["ending_bankroll"] for r in results)
    drawdowns = sorted(r["max_drawdown_pct"] for r in results)

    def pct(data, p):
        idx = min(len(data) - 1, max(0, int(len(data) * p)))
        return data[idx]

    n_profitable = sum(1 for e in endings if e > starting_bankroll)
    n_ruined = sum(1 for e in endings if e < starting_bankroll * 0.5)

    print(f"P5:  ${pct(endings, 0.05):>12,.2f}")
    print(f"P50: ${pct(endings, 0.50):>12,.2f}  (median)")
    print(f"P95: ${pct(endings, 0.95):>12,.2f}")
    print(f"P(profitable after {days}d): {n_profitable/trials:.1%}")
    print(f"P(lose >50% of bankroll):    {n_ruined/trials:.1%}")
    print(f"Median max drawdown:         {pct(drawdowns, 0.50):.1%}")
    print()
    return {"p50": pct(endings, 0.50), "p_profitable": n_profitable / trials}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--trials", type=int, default=2000)
    parser.add_argument("--starting-bankroll", type=float, default=None)
    args = parser.parse_args()

    week1_pool = load_pool_for_week(date(2026, 8, 15), date(2026, 8, 21))
    week2_pool = load_pool_for_week(date(2026, 8, 22), date(2026, 8, 23))

    db = SessionLocal()
    try:
        state = db.query(BotState).first()
        live_bankroll = state.bankroll if state else settings.INITIAL_BANKROLL
    finally:
        db.close()
    starting_bankroll = args.starting_bankroll if args.starting_bankroll is not None else live_bankroll

    # Same observed pace for both — isolating the win/loss PROFILE difference,
    # not also injecting a made-up difference in how often signals fire.
    days_live = max(1.0, (datetime.utcnow() - datetime(2026, 8, 15)).total_seconds() / 86400)
    all_pool_size = len(week1_pool) + len(week2_pool)
    daily_rate = estimate_daily_trade_rate(all_pool_size, days_live)

    print("=" * 78)
    print("MONTE CARLO: Week 1 (pre-fix) vs Week 2-so-far (post-fix) trade profile")
    print("=" * 78)
    print(f"Starting bankroll: ${starting_bankroll:,.2f} | pace: ~{daily_rate:.1f} trades/day (shared) | {args.trials} trials x {args.days}d")
    print()

    r1 = simulate_pool(week1_pool, "Week 1 (Aug 15-21)", args.days, args.trials, starting_bankroll, daily_rate, seed_base=1)
    r2 = simulate_pool(week2_pool, "Week 2 so far (Aug 22-23)", args.days, args.trials, starting_bankroll, daily_rate, seed_base=9001)

    print("=" * 78)
    if r1 and r2:
        print(f"Week 1 median: ${r1['p50']:,.2f}  |  Week 2 median: ${r2['p50']:,.2f}")
        print("Given week 2's n=7 (2 wins), do not read this gap as proof either way —")
        print("it is what a very small, noisy sample projects forward, nothing more.")
    print("=" * 78)


if __name__ == "__main__":
    main()
