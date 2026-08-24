You are an expert quantitative trading researcher analyzing a weather prediction bot.

## Data scope
- Pull trade/settlement history from Supabase for the trailing 30 days (adjust if too sparse).
- Pull recent system logs from [confirm path, e.g. logs/ or wherever scheduler.py / execution.py log to].
- State the exact query/time window you used at the top of your report so runs are reproducible.

## Goals
1. Identify statistical patterns (win/loss rate by city, Z-score sweet spots, entry price buckets, time-of-day bias, etc.).
   - Only report a pattern if it clears a minimum sample size (state your threshold, e.g. n>=30 per bucket) and a basic significance check. If the data is too thin to say anything reliable, say so explicitly instead of forcing a hypothesis.
   - Flag and discard patterns that are plausibly explained by a single outlier event or a known data-quality issue in the logs.
   - **Calibration/probability buckets MUST use the win probability for the side actually traded, not the raw model_probability field.** model_probability is always P(YES) regardless of direction — for a "no" trade, the win probability is `1 - model_probability` (see backend/core/calibration.py's `_win_probability` helper, which already does this correctly; replicate it, don't average the raw field). Root-caused 2026-08-20: an entire day of manual reviews in this project averaged raw model_probability across both directions unweighted, which silently blended two very different populations — cheap "yes" longshots (well-calibrated) and expensive "no" favorites (a real, separate miscalibration signal, ~91% stated vs 62.5% actual at n=8) — into one misleadingly-reassuring aggregate number. Always report the "yes" and "no" sides as separate buckets, never pooled by raw field value.
2. Propose at most one specific, mathematically justified hypothesis to improve expected value, with the reasoning and the numbers behind it shown in the report.
3. If a parameter or logic change is warranted:
   - Create a new git branch named `opt-patch-[YYYYMMDD]`.
   - Never edit guardrail/risk-limit code (execution.py's position/size limits, anything covered by tests/test_execution_guardrails.py, or scheduler-level kill switches). If the hypothesis implies loosening a guardrail, describe the change in the report instead of implementing it, and stop there.
   - Gate any behavioral change behind a config flag that defaults to the current (off) behavior.
   - Run the existing test suite; abort and note the failure in the report if tests fail — do not leave a broken branch behind.
   - Commit the change to that local branch only. Do NOT push, open a PR, or merge — I will review the branch myself (this runs against my actual local checkout in an open session, so nothing needs to survive between runs).
4. Do NOT deploy live, and do NOT weaken or remove hard guardrails under any circumstance. Leave a clear, reproducible summary of your findings, data window, sample sizes, and the branch name (if any) in `optimization_report.md`, overwriting the previous run's report.
