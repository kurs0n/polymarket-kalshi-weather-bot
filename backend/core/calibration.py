"""
Empirical tail-probability calibration for weather signals.

Root-caused 2026-08-17 from a bucket analysis of trades_rows.csv (the bot's
own settled trade history, 965 settled win/loss trades): bucketing by the
model's own implied win probability FOR THE SIDE ACTUALLY TRADED (i.e.
model_probability if direction=="yes", else 1 - model_probability — bucketing
on raw model_probability without this correction silently mixes up long-YES
and long-NO trades and produces a meaningless table), the bot's trades are
almost entirely concentrated in its own 95-100% confidence bucket:

    95-100% bucket: n=963, empirical win rate = 77.7%

That's a large, well-populated sample, not noise. The strategy still has
real edge (77.7% actual hit rate vs. entry prices typically well below
that), but Kelly sizing computed directly off a 95-100% model probability
overstates confidence by roughly 15-20 points relative to what actually
happens. This module shrinks model_yes_prob toward the bot's own rolling
empirical hit rate for well-populated tail buckets, before edge/Kelly sizing
are finalized — leaving thin-sample buckets untouched.

Caveat worth keeping in mind: this table is built from trades the bot
CHOSE to take (already filtered by MIN_CONFIDENCE_THRESHOLD, entry-price
cap, the consensus-bracket guardrail, etc.), not a random sample of raw
model outputs. That's exactly what we want calibrated — "how good is the
bot's execution of its own strategy" — but it isn't evidence about Gaussian
tail miscalibration in general, so this intentionally only touches the
specific buckets the live trade history actually covers.
"""
import logging
import statistics
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("trading_bot")

# (lower, upper) win-probability bucket edges, inclusive-exclusive. Coarse
# and tail-only by design — the bot's own trade history is almost entirely
# concentrated at the extremes (that IS the strategy: see weather_signals.py's
# MIN_CONFIDENCE_THRESHOLD note on tail bets being the only pattern with a
# proven track record), so mid-range buckets never accumulate enough samples
# to calibrate and are intentionally left alone rather than guessed at.
CALIBRATION_BUCKETS: List[Tuple[float, float]] = [
    (0.00, 0.05),
    (0.95, 1.01),  # 1.01 so a win-prob of exactly 1.0 still lands in the last bucket
]

MIN_BUCKET_SAMPLES = 30          # below this, trust the raw model, not the bucket
CALIBRATION_LOOKBACK_DAYS = 90   # rolling window so the table adapts rather than fossilizing
SHRINK_TOWARD_EMPIRICAL = 0.70   # blend weight toward the empirical rate once a bucket is trusted

_calibration_cache: Dict[str, tuple] = {}
_CALIBRATION_CACHE_TTL = 3600  # 1 hour


def _bucket_for(p: float) -> Optional[Tuple[float, float]]:
    for lo, hi in CALIBRATION_BUCKETS:
        if lo <= p < hi:
            return (lo, hi)
    return None


def _win_probability(direction: str, model_probability: float) -> float:
    """The model's own implied win probability for the side actually held."""
    direction = "yes" if direction in ("yes", "up") else "no"
    return model_probability if direction == "yes" else (1.0 - model_probability)


async def get_calibration_table() -> Dict[Tuple[float, float], dict]:
    """
    Rolling empirical win-rate per tail bucket, from the bot's own settled
    weather trades over the last CALIBRATION_LOOKBACK_DAYS days. Cached for
    1 hour. Returns {} (i.e. calibrate_probability becomes a no-op) on any
    DB error or when there simply isn't a DB to read yet — same
    fail-open-to-raw-model posture as get_recent_bias.
    """
    now = time.time()
    cache_key = "global"
    if cache_key in _calibration_cache:
        val, ts = _calibration_cache[cache_key]
        if now - ts < _CALIBRATION_CACHE_TTL:
            return val

    table: Dict[Tuple[float, float], dict] = {}
    try:
        from backend.models.database import SessionLocal, Trade

        cutoff = datetime.utcnow() - timedelta(days=CALIBRATION_LOOKBACK_DAYS)
        db = SessionLocal()
        try:
            rows = (
                db.query(Trade.direction, Trade.model_probability, Trade.result)
                .filter(
                    Trade.settled == True,
                    Trade.market_type == "weather",
                    Trade.result.in_(["win", "loss"]),
                    Trade.model_probability.isnot(None),
                    Trade.timestamp >= cutoff,
                )
                .all()
            )
        finally:
            db.close()

        bucketed: Dict[Tuple[float, float], List[int]] = {b: [] for b in CALIBRATION_BUCKETS}
        for direction, model_prob, result in rows:
            if model_prob is None:
                continue
            wp = _win_probability(direction, model_prob)
            b = _bucket_for(wp)
            if b is None:
                continue
            bucketed[b].append(1 if result == "win" else 0)

        for b, outcomes in bucketed.items():
            n = len(outcomes)
            if n >= MIN_BUCKET_SAMPLES:
                win_rate = statistics.mean(outcomes)
                table[b] = {"n": n, "empirical_win_rate": win_rate}
                logger.info(
                    f"[CALIBRATION] bucket {b[0]:.0%}-{b[1]:.0%}: n={n}, "
                    f"empirical win rate {win_rate:.1%}"
                )
    except Exception as e:
        logger.warning(f"Calibration table build failed: {e}")
        table = {}

    _calibration_cache[cache_key] = (table, now)
    return table


async def calibrate_probability(model_yes_prob: float, direction: str) -> float:
    """
    Shrink an extreme model probability toward the bot's own empirically
    observed win rate for that bucket, when there's enough settled history
    to trust it (>= MIN_BUCKET_SAMPLES). No-op otherwise — returns the raw
    input unchanged, exactly as if this module weren't called.

    `direction` is the trade direction ("yes"/"no", or "up"/"down" from
    calculate_edge) this probability would be traded as — needed because
    model_yes_prob is always P(YES), but the empirical win rate has to be
    measured against whichever side is actually held.
    """
    wp = _win_probability(direction, model_yes_prob)
    bucket = _bucket_for(wp)
    if bucket is None:
        return model_yes_prob

    table = await get_calibration_table()
    entry = table.get(bucket)
    if entry is None:
        return model_yes_prob

    empirical = entry["empirical_win_rate"]
    calibrated_wp = SHRINK_TOWARD_EMPIRICAL * empirical + (1 - SHRINK_TOWARD_EMPIRICAL) * wp

    is_yes = direction in ("yes", "up")
    calibrated_yes_prob = calibrated_wp if is_yes else (1.0 - calibrated_wp)

    if abs(calibrated_yes_prob - model_yes_prob) > 0.01:
        logger.info(
            f"[CALIBRATION] {('YES' if is_yes else 'NO')} win-prob {wp:.1%} -> "
            f"{calibrated_wp:.1%} (bucket empirical {empirical:.1%}, n={entry['n']}) "
            f"| model_yes_prob {model_yes_prob:.1%} -> {calibrated_yes_prob:.1%}"
        )

    return calibrated_yes_prob
