import { formatDistanceToNow } from 'date-fns'
import { ArrowUpDown, ArrowUp, ArrowDown } from 'lucide-react'
import { useState, useMemo } from 'react'
import type { Trade } from '../types'
import { platformStyles, targetDateShort, targetDateValueFromTicker, isTargetDateTodayOrPast } from '../utils'
import { TradeDetailModal } from './TradeDetailModal'

interface Props {
  trades: Trade[]
}

type SortKey = 'timestamp' | 'size' | 'pnl' | 'result' | 'settles'
type SortDir = 'asc' | 'desc'

export function TradesTable({ trades }: Props) {
  const [sortKey, setSortKey] = useState<SortKey>('timestamp')
  const [sortDir, setSortDir] = useState<SortDir>('desc')
  const [selected, setSelected] = useState<Trade | null>(null)

  const handleSort = (key: SortKey) => {
    if (sortKey === key) {
      setSortDir(sortDir === 'asc' ? 'desc' : 'asc')
    } else {
      setSortKey(key)
      setSortDir('desc')
    }
  }

  const sortedTrades = useMemo(() => {
    return [...trades].sort((a, b) => {
      let aVal: number | string, bVal: number | string
      switch (sortKey) {
        case 'timestamp':
          aVal = new Date(a.timestamp).getTime()
          bVal = new Date(b.timestamp).getTime()
          break
        case 'size':
          aVal = a.size; bVal = b.size; break
        case 'pnl':
          aVal = a.pnl ?? 0; bVal = b.pnl ?? 0; break
        case 'result':
          aVal = a.result; bVal = b.result; break
        case 'settles':
          aVal = targetDateValueFromTicker(a.market_ticker)
          bVal = targetDateValueFromTicker(b.market_ticker)
          break
        default: return 0
      }
      if (typeof aVal === 'string') {
        return sortDir === 'asc'
          ? aVal.localeCompare(bVal as string)
          : (bVal as string).localeCompare(aVal)
      }
      return sortDir === 'asc' ? aVal - (bVal as number) : (bVal as number) - aVal
    })
  }, [trades, sortKey, sortDir])

  const SortIcon = ({ column }: { column: SortKey }) => {
    if (sortKey !== column) return <ArrowUpDown className="w-3 h-3 text-neutral-600" />
    return sortDir === 'asc'
      ? <ArrowUp className="w-3 h-3 text-amber-500" />
      : <ArrowDown className="w-3 h-3 text-amber-500" />
  }

  if (trades.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-8 text-neutral-600">
        <p className="text-sm">No trades yet</p>
        <p className="text-xs mt-1">Trades will appear here</p>
      </div>
    )
  }

  return (
    <>
    <table className="w-full">
      <thead className="sticky top-0 z-10" style={{ background: 'var(--card)' }}>
        <tr className="text-neutral-500 text-left text-xs border-b border-[#23262e]">
          <th className="py-2.5 px-2 font-semibold w-8"></th>
          <th
            className="py-2.5 px-2 font-semibold cursor-pointer hover:text-neutral-300"
            onClick={() => handleSort('result')}
          >
            <div className="flex items-center gap-1">
              St <SortIcon column="result" />
            </div>
          </th>
          <th className="py-2.5 px-2 font-semibold">Market</th>
          <th
            className="py-2.5 px-2 font-semibold text-right cursor-pointer hover:text-neutral-300"
            onClick={() => handleSort('settles')}
            title="The date the weather event resolves — different from when the trade was placed"
          >
            <div className="flex items-center justify-end gap-1">
              Settles <SortIcon column="settles" />
            </div>
          </th>
          <th className="py-2.5 px-2 font-semibold text-center">Dir</th>
          <th
            className="py-2.5 px-2 font-semibold text-right cursor-pointer hover:text-neutral-300"
            onClick={() => handleSort('size')}
          >
            <div className="flex items-center justify-end gap-1">
              Size <SortIcon column="size" />
            </div>
          </th>
          <th
            className="py-2.5 px-2 font-semibold text-right cursor-pointer hover:text-neutral-300"
            onClick={() => handleSort('pnl')}
          >
            <div className="flex items-center justify-end gap-1">
              P&L <SortIcon column="pnl" />
            </div>
          </th>
          <th
            className="py-2.5 px-2 font-semibold text-right cursor-pointer hover:text-neutral-300"
            onClick={() => handleSort('timestamp')}
          >
            <div className="flex items-center justify-end gap-1">
              Time <SortIcon column="timestamp" />
            </div>
          </th>
        </tr>
      </thead>
      <tbody>
        {sortedTrades.map((trade) => {
            const isPending = trade.result === 'pending'
            const isWin = trade.result === 'win'
            const isYes = trade.direction === 'yes' || trade.direction === 'up'
            const style = platformStyles[trade.platform?.toLowerCase()]
            const settles = targetDateShort(trade.market_ticker)
            const settlesDue = isPending && isTargetDateTodayOrPast(trade.market_ticker)

            return (
              <tr
                key={trade.id}
                className="border-b border-neutral-800/50 hover:bg-neutral-800/30 text-sm cursor-pointer"
                onClick={() => setSelected(trade)}
              >
                <td className="py-2 px-2">
                  {style && (
                    <span className={`platform-badge ${style.badge}`}>
                      {style.icon}
                    </span>
                  )}
                </td>
                <td className="py-2 px-2">
                  <div className="flex flex-col gap-0.5">
                    <span className={`text-[11px] font-semibold uppercase ${
                      isPending ? 'text-amber-500' : isWin ? 'text-green-500' : 'text-red-500'
                    }`}>
                      {isPending ? 'PND' : isWin ? 'WIN' : 'LOSS'}
                    </span>
                    {/* Added 2026-08-23: live unrealised gain for still-open
                        positions — peak_gain_pct updates every scan cycle even
                        while pending, so this shows how the position is
                        trending without needing to click in. */}
                    {isPending && trade.peak_gain_pct != null && (
                      <span
                        className={`text-[10px] font-medium tabular-nums ${
                          trade.peak_gain_pct >= 0 ? 'text-green-500/80' : 'text-red-500/80'
                        }`}
                        title="Highest unrealised gain seen so far this trade (updates every scan cycle)"
                      >
                        peak {trade.peak_gain_pct >= 0 ? '+' : ''}{(trade.peak_gain_pct * 100).toFixed(0)}%
                      </span>
                    )}
                  </div>
                </td>
                <td className="py-2 px-2">
                  <div className="flex items-center gap-1.5 max-w-[150px]">
                    <span className="text-neutral-300 truncate" title={trade.event_slug || trade.market_ticker}>
                      {trade.event_slug || trade.market_ticker}
                    </span>
                    {trade.execution_type === 'liquidated' && (
                      <span
                        className="shrink-0 text-[9px] font-bold uppercase tracking-wide text-amber-400 bg-amber-500/10 border border-amber-500/30 rounded px-1 py-0.5"
                        title={
                          trade.peak_gain_pct != null
                            ? `Sold early — peaked at ${trade.peak_gain_pct >= 0 ? '+' : ''}${(trade.peak_gain_pct * 100).toFixed(0)}% before exiting`
                            : 'Sold early via the exit logic (trailing-stop / stop-loss / METAR), not held to settlement'
                        }
                      >
                        early
                      </span>
                    )}
                  </div>
                </td>
                <td className="py-2 px-2 text-right text-xs tabular-nums">
                  {settles ? (
                    <span
                      className={settlesDue ? 'text-cyan-400 font-semibold' : 'text-neutral-400'}
                      title={settlesDue ? 'Target date has passed — settlement should post soon' : 'Date the weather event resolves'}
                    >
                      {settles}
                    </span>
                  ) : (
                    <span className="text-neutral-600">—</span>
                  )}
                </td>
                <td className="py-2 px-2 text-center">
                  <span className={`text-xs font-semibold uppercase ${isYes ? 'text-green-500' : 'text-red-500'}`}>
                    {trade.direction}
                  </span>
                </td>
                <td className="py-2 px-2 text-right text-neutral-300 tabular-nums">
                  ${trade.size.toFixed(0)}
                </td>
                <td className="py-2 px-2 text-right">
                  {trade.pnl !== null ? (
                    <span className={`font-semibold tabular-nums ${
                      trade.pnl >= 0 ? 'text-green-500' : 'text-red-500'
                    }`}>
                      {trade.pnl >= 0 ? '+' : ''}${trade.pnl.toFixed(0)}
                    </span>
                  ) : (
                    <span className="text-neutral-600">-</span>
                  )}
                </td>
                <td className="py-2 px-2 text-right text-xs text-neutral-500 tabular-nums">
                  {formatDistanceToNow(new Date(trade.timestamp), { addSuffix: false })}
                </td>
              </tr>
            )
          })}
      </tbody>
    </table>

    <TradeDetailModal trade={selected} onClose={() => setSelected(null)} />
    </>
  )
}
