import { useEffect } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { X, ExternalLink } from 'lucide-react'
import type { Trade } from '../types'
import { platformStyles, getPnlColorClass, formatCurrency, getMarketUrl, cityFromTicker, bracketFromTicker, targetDateFromTicker } from '../utils'

interface Props {
  trade: Trade | null
  onClose: () => void
}

function Stat({ label, value, valueColor }: { label: string; value: string; valueColor?: string }) {
  return (
    <div className="rounded-xl bg-[#171a20] border border-[#23262e] px-4 py-3">
      <div className="text-[11px] text-neutral-500 uppercase tracking-wide mb-1">{label}</div>
      <div className={`text-xl font-bold tabular-nums ${valueColor ?? 'text-neutral-100'}`}>{value}</div>
    </div>
  )
}

export function TradeDetailModal({ trade, onClose }: Props) {
  useEffect(() => {
    if (!trade) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [trade, onClose])

  const isYes = trade?.direction === 'yes' || trade?.direction === 'up'
  const isPending = trade?.result === 'pending'
  const isWin = trade?.result === 'win'
  const style = trade ? platformStyles[trade.platform?.toLowerCase()] : undefined

  // Added 2026-08-22: surface early exits (trailing-stop / price-stop-loss /
  // METAR liquidations) distinctly from trades held to a real settlement,
  // and how much of the peak unrealised gain actually got captured — see
  // position_liquidator_job in scheduler.py.
  const wasEarlyExit = trade?.execution_type === 'liquidated'
  const realizedReturnPct = trade && trade.pnl != null && trade.size > 0 ? trade.pnl / trade.size : null
  const hasPeak = trade?.peak_gain_pct != null
  const exitLabel = isPending
    ? 'Still open'
    : wasEarlyExit
      ? 'Sold early (exit logic)'
      : 'Held to settlement'

  return (
    <AnimatePresence>
      {trade && (
        <motion.div
          className="fixed inset-0 z-50 flex items-center justify-center p-4"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          onClick={onClose}
        >
          <div className="absolute inset-0 bg-black/70 backdrop-blur-sm" />

          <motion.div
            className="relative panel w-full max-w-lg overflow-hidden"
            initial={{ opacity: 0, scale: 0.97, y: 8 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.97, y: 8 }}
            transition={{ duration: 0.15 }}
            onClick={e => e.stopPropagation()}
          >
            <div className="px-6 py-4 border-b border-[#23262e] flex items-center justify-between">
              <div className="flex items-center gap-2.5">
                {style && <span className={`platform-badge ${style.badge}`}>{style.icon}</span>}
                <div>
                  <h2 className="text-lg font-extrabold text-white">
                    {cityFromTicker(trade.market_ticker)} {bracketFromTicker(trade.market_ticker)}
                  </h2>
                  <p className="text-xs text-neutral-500 font-mono">{trade.market_ticker}</p>
                  {targetDateFromTicker(trade.market_ticker) && (
                    <p className="text-xs text-cyan-400 font-medium mt-0.5">
                      Market date: {targetDateFromTicker(trade.market_ticker)}
                    </p>
                  )}
                </div>
              </div>
              <button
                onClick={onClose}
                className="w-9 h-9 rounded-lg flex items-center justify-center text-neutral-400 hover:text-neutral-100 hover:bg-[#1c1f26] transition-colors shrink-0"
              >
                <X className="w-5 h-5" />
              </button>
            </div>

            <div className="px-6 py-5 space-y-4">
              <div className="grid grid-cols-3 gap-3">
                <Stat
                  label="Direction"
                  value={trade.direction.toUpperCase()}
                  valueColor={isYes ? 'text-green-400' : 'text-red-400'}
                />
                <Stat
                  label="Result"
                  value={isPending ? 'Pending' : isWin ? 'Win' : 'Loss'}
                  valueColor={isPending ? 'text-amber-400' : isWin ? 'text-green-400' : 'text-red-400'}
                />
                <Stat
                  label="P&L"
                  value={trade.pnl !== null ? formatCurrency(trade.pnl, true) : '—'}
                  valueColor={getPnlColorClass(trade.pnl)}
                />
                <Stat label="Entry Price" value={`${(trade.entry_price * 100).toFixed(0)}¢`} />
                <Stat label="Size" value={`$${trade.size.toFixed(0)}`} />
                <Stat
                  label="Edge at Entry"
                  value={trade.edge_at_entry != null ? `${(trade.edge_at_entry * 100).toFixed(1)}%` : '—'}
                />
                <Stat
                  label="Model Prob."
                  value={trade.model_probability != null ? `${(trade.model_probability * 100).toFixed(1)}%` : '—'}
                />
                <Stat
                  label="Confidence"
                  value={trade.confidence != null ? `${(trade.confidence * 100).toFixed(0)}%` : '—'}
                />
                <Stat
                  label="Placed"
                  value={new Date(trade.timestamp).toLocaleString('en-US', {
                    month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
                  })}
                />
                <Stat
                  label="Exit"
                  value={exitLabel}
                  valueColor={wasEarlyExit ? 'text-amber-400' : isPending ? 'text-neutral-400' : 'text-neutral-200'}
                />
                {hasPeak && (
                  <Stat
                    label="Peak Unrealised Gain"
                    value={`${trade.peak_gain_pct! >= 0 ? '+' : ''}${(trade.peak_gain_pct! * 100).toFixed(0)}%`}
                    valueColor={trade.peak_gain_pct! >= 0 ? 'text-green-400' : 'text-red-400'}
                  />
                )}
              </div>

              {wasEarlyExit && hasPeak && realizedReturnPct != null && (
                <div className="rounded-xl bg-amber-500/5 border border-amber-500/20 px-4 py-3">
                  <p className="text-xs text-amber-300/90 leading-relaxed">
                    {trade.peak_gain_pct! > 0 ? (
                      <>
                        Sold early via the exit logic — peaked at{' '}
                        <span className="font-semibold">+{(trade.peak_gain_pct! * 100).toFixed(0)}%</span>{' '}
                        unrealised gain, closed out at{' '}
                        <span className="font-semibold">
                          {realizedReturnPct >= 0 ? '+' : ''}{(realizedReturnPct * 100).toFixed(0)}%
                        </span>{' '}
                        realized — either the trailing stop locking in that gain before it reversed,
                        or a METAR call that the outcome was already physically decided.
                      </>
                    ) : (
                      <>
                        Sold early via the exit logic — this position never showed a real gain
                        (peaked at{' '}
                        <span className="font-semibold">{(trade.peak_gain_pct! * 100).toFixed(0)}%</span>),
                        so this was a stop-loss cutting a decline or a METAR call that the outcome
                        was already physically decided, not a protected profit.
                      </>
                    )}
                  </p>
                </div>
              )}

              <a
                href={getMarketUrl(trade.platform, trade.market_ticker, trade.event_slug ?? undefined)}
                target="_blank"
                rel="noopener noreferrer"
                className="flex items-center justify-center gap-2 w-full py-3 rounded-xl text-sm font-semibold uppercase tracking-wide bg-[#171a20] border border-[#23262e] hover:border-[#33363f] hover:bg-[#1c1f26] text-neutral-200 transition-colors"
              >
                <ExternalLink className="w-4 h-4" />
                View on {platformStyles[trade.platform?.toLowerCase()]?.name ?? trade.platform}
              </a>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  )
}
