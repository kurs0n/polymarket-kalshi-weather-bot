# Weather Bot Research

## Current Implementation Decision

The bot currently uses one forecast source for live signal generation: the Open-Meteo Ensemble API with the `gfs_seamless` model. This keeps the first version simple and makes the probability calculation reproducible.

The bot scans weather temperature markets on Polymarket and, when credentials are configured, Kalshi. It is configured for six US cities:

- New York City
- Chicago
- Miami
- Los Angeles
- Denver
- Boston

## Open-Meteo Ensemble API

The implementation calls:

```text
https://ensemble-api.open-meteo.com/v1/ensemble
```

Request parameters include:

- City latitude and longitude
- `daily=temperature_2m_max,temperature_2m_min`
- `temperature_unit=fahrenheit`
- One target date
- `models=gfs_seamless`

The response contains the control forecast and ensemble-member values. The bot collects the daily high and low values, calculates their mean and standard deviation, and counts members against a contract threshold.

The forecast cache lasts 15 minutes for each city and target date.

## Market Research

### Polymarket

The bot uses the Gamma API to discover open weather events and parse temperature markets. A market must contain a supported city, a temperature-related phrase, a Fahrenheit threshold, and a recognizable date.

Supported question concepts include:

- Daily high temperature
- Daily low temperature
- Above or below a threshold

The scanner ignores past dates, closed markets, unsupported cities, missing prices, and markets whose YES price is below `2%` or above `98%`.

Resolution is checked through the Gamma API using the event slug or market ID. A closed market resolves YES when its first outcome price is above `99%` and NO when it is below `1%`.

### Kalshi

Kalshi access is optional and requires `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH`. The bot queries open `KXHIGH` series for the configured cities, handles pagination, parses bracket tickers, and uses Kalshi's finalized result for settlement.

Current series:

| City | Series ticker |
|------|---------------|
| New York City | `KXHIGHNY` |
| Chicago | `KXHIGHCHI` |
| Miami | `KXHIGHMIA` |
| Los Angeles | `KXHIGHLAX` |
| Denver | `KXHIGHDEN` |
| Boston | `KXHIGHTBOS` |

The current Kalshi parser supports high-temperature bracket tickers using `B` and `T` boundaries. It does not implement a separate Kalshi low-temperature series.

## Probability and Edge

For an above-threshold YES market:

```text
model probability = members above threshold / total members
```

For a below-threshold YES market, the complementary probability is used. The probability is clipped to the range `5%` to `95%` before comparison with the market price.

```text
YES edge = model probability - market YES price
NO edge  = (1 - model probability) - (1 - market YES price)
```

The larger edge determines the suggested direction. The signal is actionable only when the absolute edge reaches the configured `8%` threshold and the selected entry price does not exceed `70%` by default.

## Settlement and Validation Notes

The settlement layer checks the platform's own resolution result rather than deriving a result from a forecast. It records the settlement value, win/loss result, P&L, bankroll update, and linked signal outcome.

NWS station observations are available in the weather data module for the six configured cities. They are useful as weather observation data, but the current trade settlement path uses Polymarket or Kalshi market resolution.

## Useful References

- [Open-Meteo Ensemble API](https://open-meteo.com/en/docs/ensemble-api)
- [Open-Meteo weather API](https://open-meteo.com/)
- [NWS API](https://www.weather.gov/documentation/services-web-api)
- [Kalshi API Documentation](https://docs.kalshi.com/welcome)
- [Kalshi Weather Markets](https://help.kalshi.com/markets/popular-markets/weather-markets)
- [Polymarket API Endpoints](https://docs.polymarket.com/quickstart/reference/endpoints)