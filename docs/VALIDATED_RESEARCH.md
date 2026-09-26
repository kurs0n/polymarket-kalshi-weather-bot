# Validated Weather Bot Research

This document records the weather behavior currently reflected in the `main` branch implementation.

## Verified Forecast Source

The signal engine calls the Open-Meteo Ensemble API with the `gfs_seamless` model:

```text
https://ensemble-api.open-meteo.com/v1/ensemble
```

It requests daily maximum and minimum temperature in Fahrenheit for one city and one target date. The implementation collects the control forecast and returned ensemble-member values, then calculates:

- Daily high member values
- Daily low member values
- Mean temperature
- Standard deviation
- Number of members
- Threshold probabilities

The forecast cache is keyed by city and date and expires after 15 minutes.

## Verified Cities

The configured city map contains:

| Key | City | NWS station |
|-----|------|-------------|
| `nyc` | New York City | `KNYC` |
| `chicago` | Chicago | `KORD` |
| `miami` | Miami | `KMIA` |
| `los_angeles` | Los Angeles | `KLAX` |
| `denver` | Denver | `KDEN` |
| `boston` | Boston | `KBOS` |

The default `WEATHER_CITIES` value enables all six cities.

## Verified Market Parsers

### Polymarket

The Polymarket parser reads open events from the Gamma API and supports temperature questions that contain:

- A supported city or city alias
- A temperature, `°F`, or degrees value
- A date in month-name or numeric form
- High or low temperature wording
- Above or below wording

Markets for past dates, closed markets, missing prices, unsupported cities, and near-resolved prices are skipped.

### Kalshi

The Kalshi parser supports these high-temperature series:

```text
KXHIGHNY   KXHIGHCHI   KXHIGHMIA
KXHIGHLAX  KXHIGHDEN   KXHIGHTBOS
```

It parses bracket tickers in the form:

```text
SERIES-YYMONDD-B45.5
SERIES-YYMONDD-T45.5
```

Kalshi fetching is disabled when credentials are missing. The current implementation does not support a separate Kalshi low-temperature series.

## Verified Signal Calculation

The model probability is a fraction of ensemble members:

```text
above probability = members above threshold / total members
below probability = 1 - above probability
```

The result is clipped to `0.05` through `0.95`. Edge selection compares that probability with the market YES price:

```text
YES edge = model probability - market YES price
NO edge  = (1 - model probability) - (1 - market YES price)
```

The signal selects the larger edge. The default actionable threshold is `0.08`, and the selected entry price must be at most `0.70`.

Confidence is based on how one-sided the forecast members are around the market threshold. Signal records include the forecast source, member count, mean, standard deviation, reasoning, edge, and suggested size.

## Verified Risk and Scheduler Controls

Defaults from `backend/config.py` and `backend/core/scheduler.py`:

| Control | Default |
|---------|---------|
| Scan interval | 300 seconds |
| Settlement interval | 1800 seconds |
| Initial bankroll | `$10,000` |
| Fractional Kelly | `0.15` |
| Maximum trade size | `$100` |
| Minimum trade size | `$10` |
| Maximum trades per scan | `3` |
| Maximum pending trades | `20` |
| Pending weather allocation | `$500` |
| Daily loss limit | `$300` |
| Simulation mode | enabled |

Kelly sizing is capped at `5%` of bankroll before the dollar caps are applied. Existing open positions for the same market are not duplicated.

## Verified Settlement Behavior

Polymarket settlement uses Gamma market or event resolution data. Kalshi settlement uses a finalized or determined market result. Settled trades update:

- Trade result and P&L
- Bankroll and total P&L
- Winning-trade count
- Linked signal outcome and correctness

The current implementation records simulated trades in the database. It does not place live Polymarket orders.

## Remaining Validation Work

These items are not implemented or fully verified on `main`:

1. Live Polymarket order placement.
2. Additional forecast models beyond Open-Meteo `gfs_seamless`.
3. Separate Kalshi low-temperature markets.
4. Additional forecast models beyond the current Open-Meteo integration.

## References

- [Open-Meteo Ensemble API](https://open-meteo.com/en/docs/ensemble-api)
- [NWS API Documentation](https://www.weather.gov/documentation/services-web-api)
- [Kalshi API Documentation](https://docs.kalshi.com/welcome)
- [Polymarket API Endpoints](https://docs.polymarket.com/quickstart/reference/endpoints)