---
name: backend
description: 'Use when changing or debugging the Python backend, FastAPI routes, weather market adapters, Open-Meteo forecasting, signal generation, Kelly sizing, APScheduler jobs, settlement, SQLAlchemy models, configuration, or backend tests.'
---

# Backend Engineering

## Architecture Map

- `backend/api/main.py` owns the FastAPI app, startup/shutdown hooks, dashboard routes, and API response models.
- `backend/core/scheduler.py` runs periodic scans and settlement jobs. It enforces trade limits and persists simulated trades.
- `backend/core/weather_signals.py` coordinates market discovery, forecasts, edge/confidence calculation, signal persistence, and sizing.
- `backend/core/sizing.py` owns shared edge and fractional-Kelly calculations; `backend/core/settlement.py` resolves outcomes and updates P&L/calibration.
- `backend/data/` contains Polymarket Gamma, optional Kalshi, Open-Meteo ensemble, and NWS data access and parsing.
- `backend/models/database.py` defines SQLAlchemy models, engine/session setup, and schema initialization. `backend/config.py` loads validated environment settings with Pydantic Settings.

See [the architecture guide](../../../docs/ARCHITECTURE.md) for the runtime flow, market sources, signal semantics, and risk controls.

## Working Guidelines

1. Trace behavior to the module that owns the decision. Keep HTTP routing in `api`, orchestration in `core`, external I/O and parsing in `data`, and persistence definitions in `models`.
2. Preserve the weather pipeline's units and semantics: thresholds and forecasts are Fahrenheit; model probability is the YES probability; signal direction is `yes` or `no`; settlement values are `1.0` for YES and `0.0` for NO. Reuse `calculate_edge` and `calculate_kelly_size` rather than duplicating formulas.
3. Treat trades as simulated records. This repository does not place live Polymarket orders. Kalshi credentials are optional and are used for configured market discovery/resolution; do not imply that market access means order execution.
4. Keep external API calls asynchronous where the owning code is async, apply timeouts, handle unavailable or malformed provider data, and avoid turning transient provider failures into fabricated forecasts, signals, or settlements.
5. Read/write through the existing SQLAlchemy session patterns and close sessions reliably. If changing persistent fields, inspect `init_db`/`ensure_schema` and existing database expectations; do not assume a migration framework exists.
6. Add or update focused `unittest` cases for parsing, probability, sizing, settlement, or configuration behavior. Prefer mocked provider responses over live network calls.
7. Keep secrets in environment configuration; never hard-code or print credentials. Add settings through `backend/config.py` with validation when needed.

## Verification

- Run the focused test module for the changed behavior, for example `python -m unittest discover -s tests -p 'test_sizing.py'`.
- Run the full backend suite with `python -m unittest discover -s tests` (also exposed by `./run_tests.sh`).
- For API/schema changes, verify response models and the frontend contract in `frontend/src/types.ts` and `frontend/src/api.ts`.