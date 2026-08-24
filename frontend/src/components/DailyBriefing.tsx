import { useEffect, useMemo, useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { X, TrendingUp, TrendingDown, Clock, Wallet } from 'lucide-react'
import type { Trade, BotStats, EquityPoint } from '../types'
import { formatCurrency, getPnlColorClass, cityFromTicker, bracketFromTicker, platformStyles } from '../utils'
import { EquityChart } from './EquityChart'
import { TradeDetailModal } from './TradeDetailModal'

interface Props {
  isOpen: boolean
  onClose: () => void
  trades: Trade[]
  stats: BotStats
  equityCurve: EquityPoint[]
}

function startOfToday(): Date {
  const d = new Date()
  d.setHours(0, 0, 0, 0)
  return d
}

export function DailyBriefing({ isOpen, onClose, trades, stats, equityCurve }: Props) {
  const [selectedTrade, setSelectedTrade] = useState<Trade | null>(null)

  // Escape closes the trade detail first if one is open, otherwise the briefing.
  useEffect(() => {
    if (!isOpen) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      if (selectedTrade) setSelectedTrade(null)
      else onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [isOpen, onClose, selectedTrade])

  const todayStart = useMemo(() => startOfToday(), [])

  const todayTrades = useMemo(
    () => trades
      .filter(t => new Date(t.timestamp) >= todayStart)
      .sort((a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime()),
    [trades, todayStart]
  )

  const todayEquity = useMemo(
    () => equityCurve.filter(p => new Date(p.timestamp) >= todayStart),
    [equityCurve, todayStart]
  )

  const settledToday = todayTrades.filter(t => t.result === 'win' || t.result === 'loss')
  const wins = settledToday.filter(t => t.result === 'win').length
  const losses = settledToday.filter(t => t.result === 'loss').length
  const pending = todayTrades.filter(t => t.result === 'pending').length
  const todayPnl = settledToday.reduce((sum, t) => sum + (t.pnl ?? 0), 0)
  const openExposure = todayTrades.filter(t => t.result === 'pending').reduce((sum, t) => sum + t.size, 0)

  // A ticker seen more than once today is a scale-in (or, historically, the
  // duplicate-entry bug) on the same underlying bet, not a second decision —
  // count distinct bets separately from raw rows so the summary line doesn't
  // overstate how many independent calls were actually made.
  const distinctBets = new Set(todayTrades.map(t => t.market_ticker)).size
  const tickerSeenCount = new Map<string, number>()

  const summaryLine = todayTrades.length === 0
    ? "No activity yet today."
    : `${wins > 0 || losses > 0
        ? `${todayPnl >= 0 ? 'Up' : 'Down'} ${formatCurrency(Math.abs(todayPnl))} so far`
        : 'Nothing settled yet'
      } — ${wins}W/${losses}L across ${distinctBets} distinct bet${distinctBets === 1 ? '' : 's'}${
        pending > 0 ? `, ${pending} still open ($${openExposure.toFixed(0)} at risk)` : ''
      }.`

  const startOfDayBankroll = stats.bankroll - todayPnl

  return (
    <AnimatePresence>
      {isOpen && (
        <motion.div
          className="fixed inset-0 z-50 flex items-center justify-center p-4"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          onClick={onClose}
        >
          <div className="absolute inset-0 bg-black/70 backdrop-blur-sm" />

          <motion.div
            className="relative panel w-full max-w-4xl max-h-[85vh] flex flex-col overflow-hidden"
            initial={{ opacity: 0, scale: 0.97, y: 8 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.97, y: 8 }}
            transition={{ duration: 0.15 }}
            onClick={e => e.stopPropagation()}
          >
            {/* Header */}
            <div className="px-6 py-4 border-b border-[#23262e] flex items-center justify-between shrink-0">
              <div>
                <h2 className="text-xl font-extrabold text-white">Today's Briefing</h2>
                <p className="text-sm text-neutral-500">
                  {new Date().toLocaleDateString('en-US', { weekday: 'long', month: 'long', day: 'numeric' })}
                </p>
              </div>
              <button
                onClick={onClose}
                className="w-9 h-9 rounded-lg flex items-center justify-center text-neutral-400 hover:text-neutral-100 hover:bg-[#1c1f26] transition-colors"
              >
                <X className="w-5 h-5" />
              </button>
            </div>

            <div className="flex-1 overflow-y-auto px-6 py-5 space-y-5">
              {/* Plain-English summary */}
              <div className="text-base text-neutral-200 leading-relaxed">
                {summaryLine}
              </div>

              {/* Stat tiles */}
              <div className="grid grid-cols-4 gap-3">
                <div className="rounded-xl bg-[#171a20] border border-[#23262e] px-4 py-3">
                  <div className="flex items-center gap-1.5 text-neutral-500 text-xs uppercase tracking-wide mb-1">
                    {todayPnl >= 0 ? <TrendingUp className="w-3.5 h-3.5" /> : <TrendingDown className="w-3.5 h-3.5" />}
                    Today's P&L
                  </div>
                  <div className={`text-2xl font-bold tabular-nums ${getPnlColorClass(todayPnl)}`}>
                    {formatCurrency(todayPnl, true)}
                  </div>
                </div>
                <div className="rounded-xl bg-[#171a20] border border-[#23262e] px-4 py-3">
                  <div className="text-neutral-500 text-xs uppercase tracking-wide mb-1">Record</div>
                  <div className="text-2xl font-bold tabular-nums text-neutral-100">
                    {wins}<span className="text-green-400">W</span> {losses}<span className="text-red-400">L</span>
                  </div>
                </div>
                <div className="rounded-xl bg-[#171a20] border border-[#23262e] px-4 py-3">
                  <div className="flex items-center gap-1.5 text-neutral-500 text-xs uppercase tracking-wide mb-1">
                    <Clock className="w-3.5 h-3.5" /> Open
                  </div>
                  <div className="text-2xl font-bold tabular-nums text-amber-400">
                    {pending}
                    <span className="text-sm text-neutral-500 ml-1.5">${openExposure.toFixed(0)}</span>
                  </div>
                </div>
                <div className="rounded-xl bg-[#171a20] border border-[#23262e] px-4 py-3">
                  <div className="flex items-center gap-1.5 text-neutral-500 text-xs uppercase tracking-wide mb-1">
                    <Wallet className="w-3.5 h-3.5" /> Bankroll
                  </div>
                  <div className="text-2xl font-bold tabular-nums text-neutral-100">
                    ${stats.bankroll.toFixed(0)}
                  </div>
                </div>
              </div>

              {/* Intraday chart */}
              {todayEquity.length > 0 && (
                <div>
                  <div className="panel-header mb-2">Today's P&L</div>
                  <div className="h-32 panel p-2">
                    <EquityChart data={todayEquity} initialBankroll={startOfDayBankroll} />
                  </div>
                </div>
              )}

              {/* Today's trades */}
              <div>
                <div className="panel-header mb-2">Today's Activity ({todayTrades.length} entries)</div>
                {todayTrades.length === 0 ? (
                  <div className="text-sm text-neutral-500 py-6 text-center">
                    No trades placed yet today.
                  </div>
                ) : (
                  <div className="panel overflow-hidden">
                    <table className="w-full">
                      <thead>
                        <tr className="text-neutral-500 text-left text-xs border-b border-[#23262e]">
                          <th className="py-2.5 px-3 font-semibold w-8"></th>
                          <th className="py-2.5 px-3 font-semibold">Market</th>
                          <th className="py-2.5 px-3 font-semibold text-center">Dir</th>
                          <th className="py-2.5 px-3 font-semibold text-right">Entry</th>
                          <th className="py-2.5 px-3 font-semibold text-right">Model</th>
                          <th className="py-2.5 px-3 font-semibold text-right">Edge</th>
                          <th className="py-2.5 px-3 font-semibold text-right">Result</th>
                          <th className="py-2.5 px-3 font-semibold text-right">P&L</th>
                          <th className="py-2.5 px-3 font-semibold text-right">Time</th>
                        </tr>
                      </thead>
                      <tbody>
                        {todayTrades.map(t => {
                          const seenBefore = tickerSeenCount.get(t.market_ticker) ?? 0
                          tickerSeenCount.set(t.market_ticker, seenBefore + 1)
                          const isScaleIn = seenBefore > 0
                          const style = platformStyles[t.platform?.toLowerCase()]
                          const isYes = t.direction === 'yes' || t.direction === 'up'
                          const isPending = t.result === 'pending'
                          const isWin = t.result === 'win'
                          return (
                            <tr
                              key={t.id}
                              className="border-b border-neutral-800/50 last:border-b-0 hover:bg-neutral-800/30 text-sm cursor-pointer"
                              onClick={() => setSelectedTrade(t)}
                            >
                              <td className="py-2 px-3">
                                {style && <span className={`platform-badge ${style.badge}`}>{style.icon}</span>}
                              </td>
                              <td className="py-2 px-3">
                                <div className="flex items-center gap-2">
                                  <span className="text-neutral-200 font-medium">
                                    {cityFromTicker(t.market_ticker)} {bracketFromTicker(t.market_ticker)}
                                  </span>
                                  {isScaleIn && (
                                    <span className="px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase bg-blue-500/10 text-blue-400 border border-blue-500/20">
                                      scale-in
                                    </span>
                                  )}
                                </div>
                              </td>
                              <td className="py-2 px-3 text-center">
                                <span className={`text-xs font-semibold uppercase ${isYes ? 'text-green-500' : 'text-red-500'}`}>
                                  {t.direction}
                                </span>
                              </td>
                              <td className="py-2 px-3 text-right text-neutral-300 tabular-nums">
                                {(t.entry_price * 100).toFixed(0)}¢
                              </td>
                              <td className="py-2 px-3 text-right text-neutral-400 tabular-nums">
                                {t.model_probability != null ? `${(t.model_probability * 100).toFixed(0)}%` : '-'}
                              </td>
                              <td className="py-2 px-3 text-right text-neutral-300 tabular-nums">
                                {t.edge_at_entry != null ? `${(t.edge_at_entry * 100).toFixed(1)}%` : '-'}
                              </td>
                              <td className="py-2 px-3 text-right">
                                <span className={`text-[11px] font-semibold uppercase ${
                                  isPending ? 'text-amber-500' : isWin ? 'text-green-500' : 'text-red-500'
                                }`}>
                                  {isPending ? 'Pending' : isWin ? 'Win' : 'Loss'}
                                </span>
                              </td>
                              <td className="py-2 px-3 text-right tabular-nums font-semibold">
                                <span className={getPnlColorClass(t.pnl)}>
                                  {t.pnl !== null ? formatCurrency(t.pnl, true) : '-'}
                                </span>
                              </td>
                              <td className="py-2 px-3 text-right text-xs text-neutral-500 tabular-nums">
                                {new Date(t.timestamp).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' })}
                              </td>
                            </tr>
                          )
                        })}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            </div>
          </motion.div>
        </motion.div>
      )}
      <TradeDetailModal trade={selectedTrade} onClose={() => setSelectedTrade(null)} />
    </AnimatePresence>
  )
}
