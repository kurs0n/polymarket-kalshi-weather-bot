#!/usr/bin/env python3
"""
Monte Carlo comparison: this branch's sizing logic vs. main branch's,
run side by side against IDENTICAL simulated conditions — 2026-08-21.

Answers "would main's code have actually done better or worse," not by
approximating what main did, but by pulling main's real
backend/core/sizing.py verbatim via `git show main:...` and executing it
unmodified — no hand-copied/paraphrased logic.

Scope, deliberately narrow: this compares SIZING only (main's flat $100
MAX_TRADE_SIZE cap + hardcoded 5% Kelly ceiling vs. this branch's $200
WEATHER_LIQUIDITY_CAP + configurable KELLY_MAX_TRADE_FRACTION). It does
NOT model main's missing scale-in/exposure-cap guardrails (main has no
per-signal re-entry limit and no WEATHER_MAX_ALLOCATION_PCT at all — see
optimization_report.md history for the real $24k/week duplicate-trade bug
those guardrails were added to fix). Reproducing that faithfully would
mean simulating main's actual per-scan-cycle re-entry behavior against
synthetic bracket selection, which is a materially bigger, more
speculative undertaking than a sizing-formula swap — out of scope here
unless asked for specifically. Both variants below run under THIS
branch's entry-rate and exposure-guardrail assumptions; only the bet-size
formula differs.

Fairness: both variants see the EXACT SAME sequence of candidate trades
(same historical-pool draws, same win/loss outcomes, same arrival timing)
per trial — pre-generated once via generate_trial_events() and replayed
against each sizing function, so the only thing that can differ between
the two result sets is the sizing decision itself, not random noise. See
simulate.py's generate_trial_events() docstring for why this matters.

Usage:
    python scripts/simulate_compare_main.py
    python scripts/simulate_compare_main.py --days 30 --trials 2000 --seed 42
"""
import argparse
import random
import statistics
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scipy.stats import beta as beta_dist

from backend.config import settings
from backend.core.sizing import calculate_kelly_size as current_kelly_size
from backend.models.database import SessionLocal, BotState

from simulate import load_historical_pool, estimate_daily_trade_rate, generate_trial_events, run_trial


def load_main_kelly_size():
    """
    Pull backend/core/sizing.py from the main branch verbatim (git show,
    no manual retyping) and exec it in an isolated namespace bound to
    THIS process's `settings` object — main's function only ever touches
    settings.KELLY_FRACTION and settings.MAX_TRADE_SIZE, both of which
    still exist on this branch's Settings class (kept for .env
    compatibility even though this branch's own calculate_kelly_size no
    longer reads MAX_TRADE_SIZE — see its comment in config.py), so no
    patching of the settings object itself is needed.
    """
    repo_root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        ["git", "show", "main:backend/core/sizing.py"],
        cwd=repo_root, capture_output=True, text=True, check=True,
    )
    source = result.stdout
    namespace = {"settings": settings, "__name__": "main_branch_sizing"}
    exec(compile(source, "main:backend/core/sizing.py", "exec"), namespace)
    return namespace["calculate_kelly_size"]


def summarize(results, starting_bankroll):
    endings = sorted(r["ending_bankroll"] for r in results)
    drawdowns = sorted(r["max_drawdown_pct"] for r in results)
    trade_counts = [r["n_trades"] for r in results]

    def pct(data, p):
        idx = min(len(data) - 1, max(0, int(len(data) * p)))
        return data[idx]

    n = len(results)
    return {
        "p5": pct(endings, 0.05), "p25": pct(endings, 0.25), "p50": pct(endings, 0.50),
        "p75": pct(endings, 0.75), "p95": pct(endings, 0.95),
        "p_profitable": sum(1 for e in endings if e > starting_bankroll) / n,
        "p_ruined": sum(1 for e in endings if e < starting_bankroll * 0.5) / n,
        "median_drawdown": pct(drawdowns, 0.50),
        "p95_drawdown": pct(drawdowns, 0.95),
        "median_trades": statistics.median(trade_counts),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--trials", type=int, default=2000)
    parser.add_argument("--starting-bankroll", type=float, default=None)
    parser.add_argument("--seed", type=int, default=42, help="Base seed — each trial i uses seed+i, shared by both variants (default: 42)")
    args = parser.parse_args()

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

    main_kelly_size = load_main_kelly_size()

    print("=" * 78)
    print("MONTE CARLO: THIS BRANCH vs. MAIN — sizing logic only, identical conditions")
    print("=" * 78)
    print(f"Historical pool: {len(pool)} unique settled markets ({wins}W/{losses}L, deduped, real)")
    print(f"Observed pace: ~{daily_rate:.1f} new trades/day, starting bankroll ${starting_bankroll:,.2f}")
    print(f"Simulating: {args.trials} trials x {args.days} days, paired (same random draws per trial)")
    print()
    print("This branch: $200 WEATHER_LIQUIDITY_CAP, configurable KELLY_MAX_TRADE_FRACTION")
    print(f"             (currently {settings.KELLY_MAX_TRADE_FRACTION:.0%})")
    print("Main branch: flat $100 MAX_TRADE_SIZE cap, hardcoded 5% Kelly ceiling")
    print("Both:        same entry-rate model, same exposure guardrails (main's own")
    print("             missing exposure/dedup guardrails are NOT modeled — see module")
    print("             docstring)")
    print()

    if len(pool) < 30:
        print(f"⚠️  n={len(pool)} is below the n≥30 bar this project's own reviews hold to.")
        print(f"   Both variants below share the exact same win/loss draws per trial, so this")
        print(f"   comparison is NOT affected by that uncertainty — but the absolute numbers")
        print(f"   (P50, etc.) still are. Trust the RELATIVE difference between the two columns")
        print(f"   more than either column's absolute value.")
        print()

    current_results, main_results = [], []
    for i in range(args.trials):
        seed_i = args.seed + i
        true_rate = beta_dist.rvs(wins + 1, losses + 1, random_state=seed_i)
        rng = random.Random(seed_i)
        events = generate_trial_events(pool, args.days, daily_rate, true_rate, rng)

        current_results.append(run_trial(
            pool, args.days, daily_rate, starting_bankroll, true_rate,
            kelly_size_fn=current_kelly_size, events=events,
        ))
        main_results.append(run_trial(
            pool, args.days, daily_rate, starting_bankroll, true_rate,
            kelly_size_fn=main_kelly_size, events=events,
        ))

    cur = summarize(current_results, starting_bankroll)
    mn = summarize(main_results, starting_bankroll)

    print("RESULTS")
    print("-" * 78)
    print(f"{'Metric':<30} {'This branch':<20} {'Main':<20}")
    for label, key, fmt in [
        ("P5 (bad case)", "p5", "$"), ("P25", "p25", "$"), ("P50 (median)", "p50", "$"),
        ("P75", "p75", "$"), ("P95 (good case)", "p95", "$"),
    ]:
        print(f"{label:<30} {fmt}{cur[key]:>17,.2f}  {fmt}{mn[key]:>17,.2f}")
    print()
    print(f"{'P(profitable)':<30} {cur['p_profitable']:>18.1%}  {mn['p_profitable']:>18.1%}")
    print(f"{'P(lose >50% of bankroll)':<30} {cur['p_ruined']:>18.1%}  {mn['p_ruined']:>18.1%}")
    print(f"{'Median max drawdown':<30} {cur['median_drawdown']:>18.1%}  {mn['median_drawdown']:>18.1%}")
    print(f"{'P95 max drawdown':<30} {cur['p95_drawdown']:>18.1%}  {mn['p95_drawdown']:>18.1%}")
    print(f"{'Median trades executed':<30} {cur['median_trades']:>18.0f}  {mn['median_trades']:>18.0f}")
    print("=" * 78)


if __name__ == "__main__":
    main()
