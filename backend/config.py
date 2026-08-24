"""Configuration settings for the weather trading bot."""
import os
from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Database (SQLite for Phase 1, PostgreSQL for production)
    DATABASE_URL: str = "sqlite:///./tradingbot.db"

    # API Keys (optional)
    POLYMARKET_API_KEY: Optional[str] = None

    # Kalshi API
    KALSHI_API_KEY_ID: Optional[str] = None
    KALSHI_PRIVATE_KEY_PATH: Optional[str] = None
    KALSHI_ENABLED: bool = True

    # Bot settings
    SIMULATION_MODE: bool = True
    INITIAL_BANKROLL: float = 10000.0
    KELLY_FRACTION: float = 0.15  # Fractional Kelly
    # Unused for sizing as of 2026-08-20 — was a flat-dollar cap inside
    # calculate_kelly_size() that went stale as bankroll grew past its
    # original ~$30 test value, silently overriding KELLY_MAX_TRADE_FRACTION
    # (the actual, bankroll-relative ceiling) below. Kept only so .env
    # doesn't need to drop the var; safe to delete outright later.
    MAX_TRADE_SIZE: float = 100.0
    DAILY_LOSS_LIMIT: float = 300.0
    MAX_TOTAL_PENDING_TRADES: int = 20

    # Settlement
    SETTLEMENT_INTERVAL_SECONDS: int = 1800  # 30 min

    # Weather trading settings
    WEATHER_ENABLED: bool = True
    WEATHER_SCAN_INTERVAL_SECONDS: int = 300  # 5 min
    WEATHER_MIN_EDGE_THRESHOLD: float = 0.08  # 8%
    WEATHER_MAX_ENTRY_PRICE: float = 0.70
    # Unused for sizing as of 2026-08-20 — same issue as MAX_TRADE_SIZE
    # above, stacked a second time on top of it. KELLY_MAX_TRADE_FRACTION
    # is the real ceiling now. Kept only for .env compatibility.
    WEATHER_MAX_TRADE_SIZE: float = 100.0
    WEATHER_CITIES: str = "nyc,chicago,miami,los_angeles,denver,boston"

    # Low-temperature markets — added 2026-08-23, "Part 1" of the low-temp
    # scope (market discovery: LOW_SERIES in kalshi_markets.py, metric now
    # correctly derived per-ticker instead of hardcoded "high"). Defaults
    # OFF: the exit safety net (METAR-based stop-loss / early-settlement
    # detection) is still high-only ("Part 2" of that scope, not yet
    # built) — flipping this on would let the bot generate and trade real
    # low-temp signals with no equivalent physical-invalidation protection
    # for them. Gated per this project's standing convention (see
    # WEATHER_EARLY_EXITS_ENABLED) rather than left implicitly live the
    # moment the discovery code merges. Flip on only once Part 2 is done,
    # or with that gap explicitly accepted.
    WEATHER_LOW_TEMP_ENABLED: bool = False

    # Per-scan trading limits — 2026-08-15: pulled out of scheduler.py so
    # bankroll changes (INITIAL_BANKROLL) don't also require code edits here.
    # WEATHER_MIN_TRADE_SIZE in particular only makes sense relative to the
    # bankroll: it must stay well below Kelly's max-per-trade output
    # (bankroll * KELLY_MAX_TRADE_FRACTION) or it silently overrides Kelly's
    # sizing and every trade comes out identical regardless of confidence
    # (this happened at $30 bankroll with a $10 floor — the floor was above
    # Kelly's entire possible output range).
    WEATHER_MIN_TRADE_SIZE: float = 1.0
    WEATHER_MAX_TRADES_PER_SCAN: int = 3
    # Fraction of total capital (free bankroll + already-open positions)
    # allowed in open weather positions at once. A flat dollar cap here
    # goes stale the moment INITIAL_BANKROLL changes (see scheduler.py).
    WEATHER_MAX_ALLOCATION_PCT: float = 0.30  # 30%

    # Kelly sizing cap — max fraction of bankroll any single trade can use,
    # applied after KELLY_FRACTION. This is the ceiling counterpart to
    # WEATHER_MIN_TRADE_SIZE above: together they define the dollar range
    # confidence can actually size within at the current bankroll.
    KELLY_MAX_TRADE_FRACTION: float = 0.05  # 5%

    # Absolute liquidity cap — added 2026-08-20 after a bootstrap Monte
    # Carlo simulation showed KELLY_MAX_TRADE_FRACTION alone (a pure % of
    # bankroll) doesn't bound position size once bankroll compounds, while
    # real Kalshi weather-market order books do: live depth checked that
    # day ranged ~$94-$1,300 within 5c of the touch, several city/date
    # brackets at the thin end. $200 sits below what the deeper markets
    # can absorb and close to (sometimes above) the thinnest ones — a
    # single conservative number, not a per-market real-time check. Revisit
    # if it's regularly the binding constraint on markets confirmed to have
    # more real depth, or once real-time orderbook depth can be queried
    # per-trade instead of assumed.
    WEATHER_LIQUIDITY_CAP: float = 200.0

    # Position-liquidator exit thresholds.
    #
    # Root-caused 2026-08-17: WEATHER_PROFIT_TARGET_PCT used to be a flat
    # sell-immediately level. On a 3c tail-bet entry, +50% is still just
    # 4.5c — nowhere near "this is basically decided" — yet a real position
    # (KXHIGHMIA-26AUG18-B93.5, two $100 entries at 3c) was sold the instant
    # it hit 5c for a ~$67 gain, when holding to a winning settlement would
    # have paid ~$3,233. This is now the ACTIVATION threshold for a trailing
    # stop instead of an immediate sell: once gain has ever reached this
    # level, the position is only closed once it retraces
    # WEATHER_TRAILING_STOP_PCT off its peak — letting a genuine tail-bet
    # winner keep running while still protecting the gain already banked.
    WEATHER_PROFIT_TARGET_PCT: float = 0.50  # trailing-stop activates once unrealised gain ≥ 50%
    # Minimum trail width (percentage points of gain price can retrace from
    # peak before selling) once the trailing stop has activated. This is now
    # a FLOOR, not the whole story — see WEATHER_TRAILING_STOP_RATIO below,
    # which is what actually decides the trail width on most trades.
    WEATHER_TRAILING_STOP_PCT: float = 0.20
    # Root-caused 2026-08-21: a flat 20pp trail (the old WEATHER_TRAILING_STOP_PCT
    # behavior) is fine for a position that peaked at, say, 60% gain, but on a
    # cheap longshot that peaked at 700%+ it fires almost immediately after
    # any pullback and locks in a tiny fraction of what the position was
    # actually worth — e.g. KXHIGHCHI-26AUG18-B84.5 (7c entry, peaked +71%)
    # sold at +51%, only ~4% of its eventual full-settlement payout. The
    # trail width now SCALES with how far the position ran: give back at
    # most this fraction of the peak gain (floored at WEATHER_TRAILING_STOP_PCT
    # so a modest peak still gets a firm minimum trail). A position that
    # peaked at 725% (real trade: KXHIGHTBOS-26AUG20-B87.5, which had zero
    # exit protection while WEATHER_EARLY_EXITS_ENABLED was off and rode
    # that peak all the way down to a total loss) would, with this ratio,
    # sell once it retraces to ~471% gain — banking the large majority of a
    # genuine winner instead of either an early nickel-and-dime exit or a
    # full round-trip to zero.
    WEATHER_TRAILING_STOP_RATIO: float = 0.35
    # Root-caused 2026-08-16: was 0.80 — on a 3-5c entry, being down 80%
    # means the price is already at ~1c with a $0.00 bid, i.e. the stop-loss
    # only fired once there was nothing left to sell into. Confirmed live on
    # two real positions (NY, Boston) that the market had already fully
    # priced as losses before this threshold would have triggered. Lowered
    # so it fires while the position is still down meaningfully but there's
    # realistically still a bid to sell into.
    WEATHER_PRICE_STOP_LOSS_PCT: float = 0.35  # sell when unrealised loss ≥ 35%

    # Extra edge cushion required to trade an expensive "favorite" entry —
    # added 2026-08-21. At entry price p, breakeven win rate is p itself (a
    # win only pays back (1-p)/p while a loss costs the full stake), so
    # expensive entries need to be right much more often just to break even,
    # with far less room for error than a cheap longshot. Our own "NO"-
    # favorite trades (64-70c) showed 62.5% actual wins against a ~67%
    # breakeven at those prices at n=8 — too small a sample to prove the
    # model's stated probability is wrong (95% CI [30.6%, 86.3%] still
    # contains breakeven), but the point estimate already sits on the wrong
    # side of it. This is deliberately NOT a change to model_probability
    # itself (that stays exactly what the ensemble computes) — it's a
    # generic risk-management response to the asymmetric payout at high
    # prices, independent of whether this specific miscalibration is real.
    # calibrate_probability() in calibration.py is the mechanism that
    # actually corrects the model once this bucket clears MIN_BUCKET_SAMPLES
    # (30) — this is the stopgap for the gap between "flagged" and "enough
    # data to trust a correction."
    WEATHER_HIGH_PRICE_THRESHOLD: float = 0.60   # entries at/above this price count as "favorites"
    WEATHER_HIGH_PRICE_MIN_EDGE: float = 0.15    # ...and need this much edge instead of WEATHER_MIN_EDGE_THRESHOLD

    # Testing toggle, added 2026-08-18 on user request. When False, every
    # PRICE/PROJECTION-driven early exit is disabled — trailing profit-stop,
    # price stop-loss, and the METAR-trend-projection stop — so every
    # position rides to a real decided outcome (actual Kalshi settlement, or
    # a physically-locked-in result: the day's high already past the
    # bracket boundary, or live METAR already past the kill-switch buffer —
    # those aren't guesses, they're already-certain results, so they still
    # fire regardless of this flag).
    #
    # Root-caused 2026-08-18: since the Aug 15 reset, settled trades showed
    # an average win of ~$61 against a theoretical full-settlement payout
    # of ~$580+ at those entry prices (20 of 23 settled trades were early
    # liquidations, not real settlements) — the raw hit rate (35%, well
    # above the ~15-20% breakeven those entry prices imply) suggested real
    # entry edge, but price-driven early exits were clipping winners down
    # to a fraction of their value, making P&L alone impossible to read as
    # "is the entry edge real" vs. "did we exit well." Turned off to get
    # that clean read on entry quality alone.
    #
    # Turned back on 2026-08-21: 3 days off proved the entry-quality point
    # AND surfaced the real cost of leaving it off — KXHIGHTBOS-26AUG20-B87.5
    # peaked at +725% unrealised gain with nothing able to lock any of it in,
    # then reversed to a full loss. WEATHER_TRAILING_STOP_RATIO above (added
    # the same day) is what makes it safe to re-enable: the old flat-pp trail
    # this was turned off to avoid (clipping cheap longshots to a sliver of
    # their value) is now a trail that scales with how far the position has
    # run, so this no longer has to choose between "clip every winner" and
    # "protect nothing."
    WEATHER_EARLY_EXITS_ENABLED: bool = True

    class Config:
        env_file = ".env"


settings = Settings()
