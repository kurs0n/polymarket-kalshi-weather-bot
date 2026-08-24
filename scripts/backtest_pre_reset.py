#!/usr/bin/env python3
"""
Backtest: replay the pre-reset (Aug 9-14) historical weather markets
through TODAY's probability code — 2026-08-22.

Why this exists: trades_rows.csv holds 30 unique pre-reset markets, but
their STORED model_probability values were computed by code that predates
every fix made this week (most notably the sparse-ensemble-count
overconfidence bug — 28 of those 30 stored probabilities sit at an
extreme ≥95%/≤5%, the exact fingerprint of that bug). Blending those
stored numbers into anything would just reimport the old bug under a new
name. This script instead:

  1. Re-fetches the REAL historical 31-member GFS ensemble for each
     market's city/date from Open-Meteo (confirmed to serve archived
     ensemble data for past dates, not just forecasts).
  2. Re-fetches each market's AUTHORITATIVE strike_type/floor/cap and
     real settlement result directly from Kalshi (resolved markets stay
     queryable indefinitely) — not the CSV's possibly-stale fields.
  3. Runs that real historical weather data through the CURRENT
     EnsembleForecast probability code (Student-t tail + the
     COUNT_PSEUDO_SAMPLES smoothing fix) to get a FRESH probability
     estimate — what today's code would have said, given only what was
     knowable at the time.
  4. Compares that fresh probability to the REAL outcome (from Kalshi,
     not the CSV) — both of which are code-independent facts, so this
     comparison isn't contaminated by any bug on either end.

Scope limitation, stated plainly: this only reconstructs the GFS
ensemble component. HRRR/ECMWF/NWS cross-model blending and the rolling
bias correction (effective_mean_high()'s other inputs) need same-day
data this script has no way to backfill for arbitrary past dates, so
they're left at their documented cold-start defaults (no correction) —
the exact same behavior the live bot has for any city before it's
accumulated a few days of its own settled history. This is not a
regression in the comparison; it's what "no cross-model history yet"
already means in the real code.

Usage:
    python scripts/backtest_pre_reset.py
"""
import asyncio
import csv
import re
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from backend.data.kalshi_client import KalshiClient, kalshi_credentials_present
from backend.data.kalshi_markets import _parse_kalshi_ticker, CITY_SERIES
from backend.data.weather import CITY_CONFIG, EnsembleForecast


async def fetch_historical_ensemble(city_key: str, target_date: date):
    """
    Plain async fetch — no threading, no sync client, no artificial
    pacing. Earlier debugging in this session chased what looked like
    server-side flakiness, but the actual cause was an unrelated NameError
    in this script's own call site (fixed), silently swallowed by a broad
    except clause. Once that was fixed, a real, separate, external issue
    showed up and is the actual current blocker: Open-Meteo's archived-
    ensemble endpoint returns the correct 31-member key STRUCTURE for any
    past date, but every value in every hourly array is null — confirmed
    with a plain `curl`, zero Python/our code involved, across every date
    Aug 9-18 tried. Today's date (a live forecast, not archived) returns
    real data fine. This is on Open-Meteo's side, not fixable here — this
    function is otherwise correct and should work whenever/if their
    archive starts serving real historical values again.
    """
    if city_key not in CITY_CONFIG:
        return None
    city = CITY_CONFIG[city_key]
    params = {
        "latitude": city["lat"], "longitude": city["lon"],
        "hourly": "temperature_2m", "temperature_unit": "fahrenheit",
        "start_date": target_date.isoformat(), "end_date": target_date.isoformat(),
        "models": "gfs025",
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get("https://ensemble-api.open-meteo.com/v1/ensemble", params=params)
        response.raise_for_status()
        data = response.json()

    hourly = data.get("hourly", {})
    member_highs, member_lows = [], []
    for key, values in hourly.items():
        if not key.startswith("temperature_2m") or not isinstance(values, list):
            continue
        valid = [v for v in values if v is not None]
        if not valid:
            continue
        member_highs.append(max(valid))
        member_lows.append(min(valid))
    if not member_highs:
        return None
    return EnsembleForecast(
        city_key=city_key, city_name=city["name"], target_date=target_date,
        member_highs=member_highs, member_lows=member_lows,
    )

TICKER_TO_CITY = {series: city for city, series in CITY_SERIES.items()}

CSV_PATH = Path(__file__).resolve().parent.parent / "trades_rows.csv"


def load_pre_reset_pool():
    """One row per unique pre-reset market (earliest entry), win/loss only."""
    with open(CSV_PATH) as f:
        rows = list(csv.DictReader(f))
    settled = [r for r in rows if r["result"] in ("win", "loss")]
    seen = {}
    for r in sorted(settled, key=lambda x: x["timestamp"]):
        seen.setdefault(r["market_ticker"], r)
    return list(seen.values())


def city_key_from_ticker(ticker: str):
    m = re.match(r"^([A-Z]+)-", ticker)
    if not m:
        return None
    return TICKER_TO_CITY.get(m.group(1))


async def backtest_one(client, ensemble_cache, row):
    ticker = row["market_ticker"]
    city_key = city_key_from_ticker(ticker)
    if city_key is None:
        return {"ticker": ticker, "error": f"unrecognized city prefix"}

    try:
        market_resp = await client.get_market(ticker)
    except Exception as e:
        return {"ticker": ticker, "error": f"get_market failed: {e}"}

    market = market_resp.get("market", {})
    parsed = _parse_kalshi_ticker(market, city_key)
    if parsed is None or parsed["direction"] is None:
        return {"ticker": ticker, "error": "could not parse strike_type/floor/cap"}

    target_date = parsed["target_date"]
    kalshi_result = market.get("result")  # "yes" or "no" — ground truth
    if kalshi_result not in ("yes", "no"):
        return {"ticker": ticker, "error": f"market not resolved (result={kalshi_result!r})"}

    cache_key = (city_key, target_date)
    if cache_key not in ensemble_cache:
        last_error = None
        forecast = None
        for attempt in range(3):
            try:
                forecast = await fetch_historical_ensemble(city_key, target_date)
                break
            except Exception as e:
                last_error = e
                await asyncio.sleep(2.0)
        if forecast is None and last_error is not None:
            print(f"  (ensemble fetch for {city_key}/{target_date} failed: {last_error})")
        ensemble_cache[cache_key] = forecast
    forecast = ensemble_cache[cache_key]
    if forecast is None:
        return {"ticker": ticker, "error": "no historical ensemble data available"}

    direction = parsed["direction"]
    if direction == "above":
        fresh_p_yes = forecast.probability_high_above(parsed["threshold_f"])
    elif direction == "below":
        fresh_p_yes = forecast.probability_high_below(parsed["threshold_f"])
    else:  # between
        fresh_p_yes = forecast.probability_high_between(parsed["floor_f"], parsed["cap_f"])

    traded_direction = row["direction"]  # "yes" or "no" — what we actually bought
    win_prob_stated_fresh = fresh_p_yes if traded_direction == "yes" else (1 - fresh_p_yes)
    actually_won = (kalshi_result == traded_direction)

    market_price = float(row["market_price_at_entry"]) if row["market_price_at_entry"] else None
    entry_price = float(row["entry_price"])

    return {
        "ticker": ticker,
        "city_key": city_key,
        "target_date": str(target_date),
        "traded_direction": traded_direction,
        "entry_price": entry_price,
        "fresh_win_prob": win_prob_stated_fresh,
        "old_stated_model_probability": float(row["model_probability"]) if row["model_probability"] else None,
        "actually_won": actually_won,
        "kalshi_result": kalshi_result,
        "num_members": forecast.num_members,
    }


async def main():
    if not kalshi_credentials_present():
        print("Kalshi credentials not present — cannot fetch resolved market metadata. Stopping.")
        return

    pool = load_pre_reset_pool()
    print(f"Pre-reset pool: {len(pool)} unique settled markets (Aug 9-14)")
    print()

    client = KalshiClient()
    ensemble_cache = {}
    results = []
    errors = []

    for row in pool:
        r = await backtest_one(client, ensemble_cache, row)
        if "error" in r:
            errors.append(r)
        else:
            results.append(r)

    print(f"Successfully backtested: {len(results)} / {len(pool)}")
    if errors:
        print(f"Skipped ({len(errors)}):")
        for e in errors:
            print(f"  {e['ticker']}: {e['error']}")
    print()

    if not results:
        print("Nothing to compare — stopping.")
        return

    n = len(results)
    wins = sum(1 for r in results if r["actually_won"])
    actual_rate = wins / n
    stated_fresh_avg = sum(r["fresh_win_prob"] for r in results) / n
    old_stated = [r["old_stated_model_probability"] for r in results if r["old_stated_model_probability"] is not None]
    old_avg = sum(old_stated) / len(old_stated) if old_stated else None

    print("=" * 78)
    print("BACKTEST RESULT — pre-reset markets, re-scored with TODAY's probability code")
    print("=" * 78)
    print(f"n = {n}")
    print(f"Actual win rate (Kalshi ground truth):              {actual_rate:.1%}")
    print(f"Fresh stated win prob (today's code, avg):          {stated_fresh_avg:.1%}")
    if old_avg is not None:
        print(f"OLD stated model_probability (buggy code, avg):     {old_avg:.1%}   <- for comparison only, not trustworthy")
    print()
    gap_fresh = abs(stated_fresh_avg - actual_rate)
    gap_old = abs(old_avg - actual_rate) if old_avg is not None else None
    print(f"|fresh stated - actual| = {gap_fresh:.1%}")
    if gap_old is not None:
        print(f"|old stated - actual|   = {gap_old:.1%}")
    print()

    print(f"{'ticker':<28} {'dir':<4} {'fresh_p':<9} {'old_p':<9} {'result':<6}")
    for r in sorted(results, key=lambda x: x["target_date"]):
        old_p = f"{r['old_stated_model_probability']:.1%}" if r["old_stated_model_probability"] is not None else "n/a"
        print(f"{r['ticker']:<28} {r['traded_direction']:<4} {r['fresh_win_prob']:<9.1%} {old_p:<9} {'WIN' if r['actually_won'] else 'loss'}")
    print("=" * 78)


if __name__ == "__main__":
    asyncio.run(main())
