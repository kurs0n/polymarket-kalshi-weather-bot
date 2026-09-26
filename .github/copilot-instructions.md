# Project Context

This repository is a weather-temperature prediction-market bot with a Python API/service and a React dashboard. The backend scans Polymarket and, when credentials are configured, Kalshi KXHIGH markets; estimates threshold probabilities from Open-Meteo GFS ensemble forecasts; creates signals and simulated trades; and settles resolved trades for P&L and calibration. It does not submit live orders. See [docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md) for the full runtime and risk-control flow.

## Architecture

- Backend: Python 3.10+, FastAPI, Pydantic Settings, SQLAlchemy, APScheduler, and async HTTP clients. Main ownership boundaries are `backend/api`, `backend/core`, `backend/data`, and `backend/models`.
- Frontend: React 18, TypeScript 5, Vite, TanStack Query, Axios, Tailwind CSS, and chart/map libraries under `frontend/src`.
- API contracts are defined by Pydantic response models in `backend/api/main.py` and TypeScript types/client code in `frontend/src/types.ts` and `frontend/src/api.ts`. Keep both sides aligned when changing payloads.
- SQLite is the default database; `DATABASE_URL` can select another SQLAlchemy-supported database. Kalshi is optional. Keep credentials in environment variables and out of source/log output.

## Working Rules

- Follow the owning module: routes and response models in `backend/api`, business orchestration/calculations in `backend/core`, external clients/parsers in `backend/data`, and persistence in `backend/models`.
- Preserve simulation mode and risk controls. Do not introduce or claim live order execution without an explicit product change.
- Preserve YES/NO outcome semantics and Fahrenheit units across market parsing, forecasts, signals, settlement, and UI display.
- Prefer focused changes and tests; avoid live external API calls in automated tests. Update `docs/ARCHITECTURE.md` when a material runtime or ownership boundary changes.

## Build And Test

- Backend tests: `python -m unittest discover -s tests` from the repository root; `./run_tests.sh` is the project wrapper.
- Frontend checks: `cd frontend && npm run build` (TypeScript check plus Vite build).
- Local services: run `uvicorn backend.api.main:app --reload --port 8000` from the root and `npm run dev` from `frontend/` (Vite port `3000`, `/api` proxy to port `8000`).
- Backend configuration is loaded from environment variables and `.env` by `backend/config.py`; never add real keys or local secrets to tracked files.
- If after changes, test or tests are not passing let human decide to change the tests or change the functionality.