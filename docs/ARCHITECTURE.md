# Weather Trading Bot Architecture

## Scope

The application on `main` is a weather-temperature trading bot. It scans supported Kalshi and Polymarket markets, estimates temperature-threshold probabilities with the Open-Meteo GFS ensemble, generates signals, records simulated trades, and settles resolved markets.

The configured cities are New York City, Chicago, Miami, Los Angeles, Denver, and Boston. The active city list is controlled by `WEATHER_CITIES`.

## Runtime Flow

```
APScheduler
    |
    +-- weather scan every 300 seconds
    |       |
    |       +-- fetch Polymarket weather markets
    |       +-- optionally fetch Kalshi KXHIGH markets
    |       +-- fetch Open-Meteo GFS ensemble forecasts
    |       +-- calculate probability, edge, confidence, and Kelly size
    |       +-- persist signals
    |       +-- create simulated trades for actionable signals
    |
    +-- settlement check every 1800 seconds
            |
            +-- check Polymarket or Kalshi resolution
            +-- calculate P&L
            +-- update trade and signal outcomes
            +-- update bankroll and calibration data
```

## Market Sources

### Polymarket

`backend/data/weather_markets.py` searches the Polymarket Gamma API for open weather events, parses temperature questions, and supports future or current dates only.

The parser extracts:

- City
- Target date
- High or low temperature metric
- Fahrenheit threshold
- Above or below direction
- YES and NO prices
- Volume

Only configured cities are accepted. Resolved markets and prices outside the `2%` to `98%` range are skipped.

### Kalshi

`backend/data/kalshi_markets.py` queries the configured `KXHIGH` series when Kalshi credentials are present:

| City | Series |
|------|--------|
| New York City | `KXHIGHNY` |
| Chicago | `KXHIGHCHI` |
| Miami | `KXHIGHMIA` |
| Los Angeles | `KXHIGHLAX` |
| Denver | `KXHIGHDEN` |
| Boston | `KXHIGHTBOS` |

The current Kalshi implementation parses high-temperature bracket tickers and uses the market's YES/NO prices. Kalshi is skipped when credentials are unavailable.

## Forecasting

`backend/data/weather.py` calls the Open-Meteo Ensemble API with the `gfs_seamless` model and Fahrenheit units. It requests daily maximum and minimum temperature for the target date and collects the control plus ensemble member values returned by the API.

The forecast stores:

- Per-member daily highs and lows
- Mean and standard deviation
- Number of members
- Fetch timestamp

Forecasts are cached per city and target date for 15 minutes. The model probability is the fraction of members above or below the contract threshold. Extreme probabilities are clipped to `5%` and `95%` before signal generation.

NWS station observations are fetched during settlement support for configured cities. The current market settlement path uses the platform's resolution API: Polymarket Gamma resolution data or Kalshi finalized market results.

## Signal Generation

For each parsed weather market:

1. Fetch the ensemble forecast for its city and date.
2. Select daily high or low members.
3. Count members above or below the threshold according to the market direction.
4. Compare the model probability with the market YES price.
5. Select YES or NO based on the larger edge.
6. Reject the entry when the selected price is above `WEATHER_MAX_ENTRY_PRICE`.
7. Calculate confidence from ensemble agreement.
8. Calculate the suggested fractional-Kelly size.

```python
up_edge = model_probability - market_probability
down_edge = (1 - model_probability) - (1 - market_probability)
```

A signal is actionable when `abs(edge) >= WEATHER_MIN_EDGE_THRESHOLD`. The default threshold is `8%`.

## Risk Controls and Simulation

Sizing uses fractional Kelly with the configured `KELLY_FRACTION`. The calculated fraction is capped at `5%` of bankroll and the trade size is capped at `MAX_TRADE_SIZE` and `WEATHER_MAX_TRADE_SIZE`, both `$100` by default.

The scheduler also enforces:

- Minimum trade size: `$10`
- Maximum actionable trades per scan: `3`
- Maximum pending trades: `20`
- Pending weather allocation: `$500`
- Daily loss circuit breaker: `$300`
- Simulation mode by default: `SIMULATION_MODE=true`

The scheduler records trades in SQLite through SQLAlchemy. It does not place live Polymarket orders; Kalshi market access is used for discovery and resolution when credentials are configured.

## Calibration and API

Signals record model probability, market price, edge, confidence, suggested size, source, and reasoning. Settled outcomes update signal correctness, trade results, P&L, bankroll, and dashboard calibration statistics.

The FastAPI service exposes dashboard, forecast, market, signal, scan, trade settlement, bot-control, and Kalshi-status endpoints. The React frontend consumes these endpoints to display forecasts, signals, trades, equity, calibration, and scheduler events.