# Weather Trading Bot

A weather-temperature trading bot that finds pricing inefficiencies on **Kalshi** and **Polymarket** using ensemble forecasts. Features a React dashboard for signals, trades, and calibration.

![Python](https://img.shields.io/badge/python-3.10+-blue) ![React](https://img.shields.io/badge/react-18+-61DAFB) ![TypeScript](https://img.shields.io/badge/typescript-5.0+-blue) ![License](https://img.shields.io/badge/license-MIT-green)

**100% free to run** — No paid APIs required. Kalshi credentials are optional (needed only for Kalshi markets).

## Strategy

Scans weather temperature markets on **Kalshi** (KXHIGH series) and **Polymarket** every 5 minutes. Uses 31-member GFS ensemble forecasts from Open-Meteo to estimate the probability of temperature thresholds being exceeded. Trades when edge > 8%.

Kalshi markets are auto-discovered via `KXHIGHNY`, `KXHIGHCHI`, `KXHIGHMIA`, `KXHIGHLAX`, `KXHIGHDEN`, `KXHIGHTBOS`.

### Key Features

- **Ensemble Weather Forecasting** — 31-member GFS ensemble from Open-Meteo
- **Multi-Platform Trading** — Kalshi (KXHIGH) and Polymarket
- **Kelly Criterion Sizing** — Fractional Kelly with per-trade caps
- **Signal Calibration** — Predictions vs outcomes with Brier score
- **Simulation Mode** — Paper trading with virtual bankroll and equity curve

## Quick Start

### 1. Backend Setup

```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
uvicorn backend.api.main:app --reload --port 8000
```

Backend: http://localhost:8000 · API docs: http://localhost:8000/docs

### 2. Frontend Setup

```bash
cd frontend
npm install
npm run dev
```

Frontend: http://localhost:5173

## How It Works

1. Fetch open weather markets from Kalshi (KXHIGH series) and Polymarket (Gamma API)
2. Fetch 31-member GFS ensemble forecasts from Open-Meteo
3. Count fraction of members above/below the market's temperature threshold
4. That fraction = model probability (e.g. 28/31 members above 70°F ≈ 90%)
5. Compare to market price; trade when |edge| > 8%
6. Confidence = ensemble agreement (how one-sided the members are)

```
edge = model_probability - market_probability
```

## Configuration

Copy `.env.example` to `.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `WEATHER_ENABLED` | `true` | Enable weather scanning |
| `WEATHER_CITIES` | `nyc,chicago,miami,los_angeles,denver,boston` | Cities to trade |
| `WEATHER_MIN_EDGE_THRESHOLD` | `0.08` | Minimum edge (8%) |
| `WEATHER_MAX_ENTRY_PRICE` | `0.70` | Max entry price |
| `WEATHER_MAX_TRADE_SIZE` | `100` | Max $ per trade |
| `WEATHER_SCAN_INTERVAL_SECONDS` | `300` | Scan every 5 min |
| `KALSHI_ENABLED` | `true` | Include Kalshi markets |
| `KALSHI_API_KEY_ID` | — | Kalshi API key ID |
| `KALSHI_PRIVATE_KEY_PATH` | — | Path to RSA private key |
| `INITIAL_BANKROLL` | `10000` | Simulation bankroll |
| `KELLY_FRACTION` | `0.15` | Fractional Kelly |

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/dashboard` | Full dashboard payload |
| `GET /api/weather/forecasts` | Ensemble forecasts by city |
| `GET /api/weather/markets` | Active weather markets |
| `GET /api/weather/signals` | Current weather signals |
| `POST /api/run-scan` | Manual weather scan |
| `POST /api/settle-trades` | Settle resolved trades |
| `POST /api/bot/start` / `stop` | Control scheduler |
| `GET /api/kalshi/status` | Kalshi auth check |

## License

MIT
