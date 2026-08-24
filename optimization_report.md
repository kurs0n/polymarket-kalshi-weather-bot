# Quant Review — 2026-08-22 (probability-model fix implemented: "between" brackets)

**Data window:** Supabase `trades` table, all history through 2026-08-22. 33 unique settled markets. This entry documents a code change made following direct user-directed investigation into why expensive "NO" favorite bets kept losing despite the 2026-08-21 edge-threshold fix.

Test suite: `python -m pytest tests/` → **125 passed** (120 existing + 5 new), 0 failed.

## Root cause found: narrow "between" brackets were using a coarse, structurally noisy probability estimate

Splitting the "NO"-favorite bucket by how *extreme* the stated confidence was (not just pooling ≥60¢ together) found the real signal:

| Confidence level | n | Stated avg | Actual win rate | 95% Wilson CI |
|---|---|---|---|---|
| 80-90% | 4 | 83.9% | 75.0% | [30.1%, 95.4%] — stated value inside |
| ≥95% | 6 | 96.9% | **33.3%** | [9.7%, 70.0%] — stated value outside |

The ≥95% bucket is where all the damage is, and it's already statistically significant at n=6 given the size of the gap.

**Mechanism:** every one of these trades was a narrow "between" bracket (e.g. "Chicago high between 84°F", a ~1°F window), with the ensemble mean only 0.6-0.9σ away — not a deep tail. `EnsembleForecast.probability_high_between()` computed this as a difference of two `probability_high_above()` calls, each of which blends a parametric (Student-t) estimate with the raw ensemble member headcount — and at a full 31-member ensemble, the headcount gets **100% of the weight** (`w = min((n/31)**0.5, 1.0) = 1.0`), the parametric estimate contributes nothing. A 31-member headcount is far too coarse a ruler for "did it land in this specific narrow window": real GFS ensemble spread is often tighter than the true forecast-error uncertainty (that's the whole reason `TEMP_UNCERTAINTY_FLOOR_F` exists), so it's easy for all 31 raw members to land on the same side of a 1°F band even when the properly-widened uncertainty says the honest probability is an ordinary 10-30%, not 3% or 97%.

Verified directly: a synthetic ensemble matching the real trades' logged stats (mean 86.5°F, tight raw std 0.7°F, 84-85°F band) produced a literal **0.0%** from the old count-blended method — a real trade settled at that boundary — vs **10.5%** from the parametric-only estimate.

## Fix implemented

`probability_high_between()` / `probability_low_between()` now use the parametric (Student-t) estimate only, via new `_p_dist_high_above()` / `_p_dist_low_above()` helpers — no count blend. `probability_high_above()` / `probability_high_below()` (simple threshold contracts, working correctly per calibration data) are **unchanged** — the count blend is appropriate there, where a large, stable fraction of the ensemble is expected on each side; it's only narrow-band "between" contracts where a discrete headcount is the wrong tool.

Deliberately did **not** try to hand-tune a second correction for the residual gap (parametric-only estimate is ~10% here vs. observed 33-43%) — not enough data (n=6) to distinguish a real second effect from noise; watch it after this fix rather than fit further on this sample.

## Actions taken
- `backend/data/weather.py`: refactored `probability_high_above`/`probability_low_above` to share new `_p_dist_high_above`/`_p_dist_low_above` parametric-only helpers; `probability_high_between`/`probability_low_between` now use those helpers directly instead of delegating to the count-blended above/below methods.
- `tests/test_probability_engine.py`: added `TestBetweenBracketsIgnoreCount` (5 tests) — direct regression for the degenerate 0.0% case, confirms simple above/below behavior is untouched.
- Full test suite: 125/125 passed.
- Not touched: `calculate_kelly_size`, position/size guardrails, `execution.py`, exit logic — this is purely a probability-estimation change in `weather.py`.
- Nothing pushed — local checkout only.
