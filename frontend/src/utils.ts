/**
 * Utility functions for the weather trading bot dashboard
 */

// Kalshi weather series -> the human-readable slug segment its market page
// actually uses, e.g. kalshi.com/markets/kxhighchi/highest-temperature-in-chicago
// (confirmed live; the rest inferred from that same "highest-temperature-in-
// {city}" pattern — Kalshi's routing has historically resolved on the
// ticker segment alone even if a slug guess is off, but these aren't all
// individually verified).
const KALSHI_SERIES_SLUG: Record<string, string> = {
  KXHIGHNY:   'highest-temperature-in-new-york',
  KXHIGHCHI:  'highest-temperature-in-chicago',
  KXHIGHMIA:  'highest-temperature-in-miami',
  KXHIGHLAX:  'highest-temperature-in-los-angeles',
  KXHIGHDEN:  'highest-temperature-in-denver',
  KXHIGHTBOS: 'highest-temperature-in-boston',
}

export function getMarketUrl(platform: string, ticker: string, eventSlug?: string): string {
  const platformLower = platform.toLowerCase()

  if (platformLower === 'polymarket') {
    if (eventSlug) {
      return `https://polymarket.com/event/${eventSlug}`
    }
    return `https://polymarket.com/event/${ticker}`
  }

  if (platformLower === 'kalshi') {
    // The market ticker's own series prefix (e.g. "KXHIGHCHI" out of
    // "KXHIGHCHI-26AUG19-B93.5") is the page Kalshi actually serves —
    // there's no deep link to one specific bracket/date.
    const series = Object.keys(KALSHI_SERIES_SLUG).find(s => ticker.startsWith(s))
    if (series) {
      return `https://kalshi.com/markets/${series.toLowerCase()}/${KALSHI_SERIES_SLUG[series]}`
    }
    return `https://kalshi.com/markets/${ticker.split('-')[0].toLowerCase()}`
  }

  return '#'
}

export function formatCurrency(value: number, showSign = false): string {
  const formatted = new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 0,
    maximumFractionDigits: 2
  }).format(Math.abs(value))

  if (showSign && value !== 0) {
    return value >= 0 ? `+${formatted}` : `-${formatted}`
  }
  return value < 0 ? `-${formatted}` : formatted
}

export function formatPercent(value: number, decimals = 1): string {
  return `${(value * 100).toFixed(decimals)}%`
}

export const platformStyles: Record<string, { badge: string; icon: string; name: string }> = {
  polymarket: {
    badge: 'bg-purple-500/10 text-purple-400 border-purple-500/20',
    icon: 'P',
    name: 'Polymarket'
  },
  kalshi: {
    badge: 'bg-cyan-500/10 text-cyan-400 border-cyan-500/20',
    icon: 'K',
    name: 'Kalshi'
  }
}

export function getPnlColorClass(pnl: number | null): string {
  if (pnl === null) return 'text-neutral-500'
  if (pnl > 0) return 'text-green-500'
  if (pnl < 0) return 'text-red-500'
  return 'text-neutral-400'
}

export function formatCountdown(seconds: number): string {
  if (seconds <= 0) return 'Ended'
  const mins = Math.floor(seconds / 60)
  const secs = Math.floor(seconds % 60)
  return `${mins}:${secs.toString().padStart(2, '0')}`
}

// Kalshi weather series prefix -> friendly city name. Mirrors
// backend/data/kalshi_markets.py's CITY_SERIES so trade tickers can be
// shown as "Miami" instead of "KXHIGHMIA-26AUG19-B93.5" in dense lists.
const TICKER_CITY_PREFIXES: [string, string][] = [
  ['KXHIGHNY', 'NYC'],
  ['KXHIGHCHI', 'Chicago'],
  ['KXHIGHMIA', 'Miami'],
  ['KXHIGHLAX', 'LA'],
  ['KXHIGHDEN', 'Denver'],
  ['KXHIGHTBOS', 'Boston'],
]

export function cityFromTicker(ticker: string): string {
  const match = TICKER_CITY_PREFIXES.find(([prefix]) => ticker.startsWith(prefix))
  return match ? match[1] : ticker.split('-')[0] || ticker
}

// "KXHIGHMIA-26AUG19-B93.5" -> "93.5°F" (strips the leading B/T bracket letter).
export function bracketFromTicker(ticker: string): string {
  const part = ticker.split('-').pop() || ''
  const num = part.replace(/^[BT]/, '')
  return num ? `${num}°F` : ''
}

const MONTH_ABBR: Record<string, number> = {
  JAN: 0, FEB: 1, MAR: 2, APR: 3, MAY: 4, JUN: 5,
  JUL: 6, AUG: 7, SEP: 8, OCT: 9, NOV: 10, DEC: 11,
}

// Shared parse — the market's target date (the day the weather event
// resolves) as a real Date, or null if the ticker doesn't match the
// expected KXHIGH<city>-YYMONDD-... pattern. Backs both date formatters
// below plus the "settles today" highlighting in TradesTable.
function targetDateObjFromTicker(ticker: string): Date | null {
  const match = ticker.match(/-(\d{2})([A-Z]{3})(\d{2})-/)
  if (!match) return null
  const [, yy, mon, dd] = match
  const month = MONTH_ABBR[mon]
  if (month === undefined) return null
  return new Date(2000 + parseInt(yy, 10), month, parseInt(dd, 10))
}

// "KXHIGHMIA-26AUG19-B93.5" -> the market's target date, e.g.
// "Sun, Aug 19, 2026" — distinct from when the trade was placed. Used in
// the trade detail modal where there's room for the full form.
export function targetDateFromTicker(ticker: string): string | null {
  const date = targetDateObjFromTicker(ticker)
  if (!date) return null
  return date.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' })
}

// Compact form for table cells, e.g. "Aug 19" — no year/weekday.
export function targetDateShort(ticker: string): string | null {
  const date = targetDateObjFromTicker(ticker)
  if (!date) return null
  return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
}

// Numeric target date for sorting a table column by it — unparseable
// tickers sort last regardless of direction.
export function targetDateValueFromTicker(ticker: string): number {
  const date = targetDateObjFromTicker(ticker)
  return date ? date.getTime() : Number.POSITIVE_INFINITY
}

// Whether the market's target date is today or already in the past (the
// weather event has happened, even if settlement hasn't posted yet) —
// used to flag "this should resolve any time now" in the trades table.
export function isTargetDateTodayOrPast(ticker: string): boolean {
  const date = targetDateObjFromTicker(ticker)
  if (!date) return false
  const today = new Date()
  today.setHours(0, 0, 0, 0)
  return date <= today
}

export function debounce<T extends (...args: any[]) => void>(
  func: T,
  wait: number
): (...args: Parameters<T>) => void {
  let timeoutId: ReturnType<typeof setTimeout> | null = null

  return (...args: Parameters<T>) => {
    if (timeoutId) {
      clearTimeout(timeoutId)
    }
    timeoutId = setTimeout(() => {
      func(...args)
    }, wait)
  }
}
