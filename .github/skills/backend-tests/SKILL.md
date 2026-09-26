---
name: backend-tests
description: 'Use when writing, running, debugging, or reviewing Python backend tests for configuration, market parsing, forecasts, signal sizing, settlement, database behavior, or FastAPI backend changes.'
---

# Backend Testing

## Test Setup

The backend suite uses Python's standard-library `unittest` framework. Tests live in `tests/` and are discovered from the repository root; `tests/` is not a Python package, so prefer unittest discovery over dotted module names.

Current coverage is organized by behavior:

- `test_config.py`: Pydantic settings validation and normalization.
- `test_market_parsing.py`: weather market title/data parsing and eligibility filters.
- `test_weather_forecast.py`: ensemble summaries and threshold probabilities.
- `test_sizing.py` and `test_weather_bankroll_sizing.py`: edge/Kelly sizing and bankroll-dependent signal sizing.
- `test_settlement.py`: trade settlement and P&L behavior.

## Workflow

1. Locate the owning implementation under `backend/config.py`, `backend/core/`, `backend/data/`, or `backend/models/`; write the test at the behavior boundary rather than duplicating implementation details.
2. Follow existing `unittest.TestCase` conventions. Use `unittest.mock` to replace network clients, database sessions, clocks, and provider dependencies; tests should not call Polymarket, Kalshi, Open-Meteo, or NWS.
3. Keep cases deterministic. Market parsing filters dates against the current day, so use dates relative to `date.today()` or control the date in the module under test rather than hard-coding a date that will expire.
4. For async functions, use the established `asyncio.run(...)` pattern in synchronous test cases unless the test suite adopts a different runner.
5. Cover meaningful boundaries: invalid/missing provider data, YES/NO outcomes, threshold equality, zero or capped sizes, unresolved markets, and database/session cleanup where applicable.
6. Keep fixtures small and explicit. Assert public behavior and persisted values; avoid coupling tests to log text or private helper call counts unless that interaction is the behavior under test.

## Commands

Run the full suite from the repository root:

```sh
python -m unittest discover -s tests
```

Run one test file with discovery, for example:

```sh
python -m unittest discover -s tests -p 'test_sizing.py'
```

`./run_tests.sh` is the repository wrapper for the full suite. Report failures accurately; do not change unrelated tests or implementation just to make the suite green. For API/schema changes, check that route response models and frontend TypeScript contracts remain compatible.