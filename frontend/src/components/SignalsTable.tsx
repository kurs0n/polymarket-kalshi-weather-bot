import { ArrowUpDown, ArrowUp, ArrowDown } from 'lucide-react'
import { useState, useMemo } from 'react'
import type { WeatherSignal } from '../types'
import { platformStyles } from '../utils'
import { SignalDetailModal } from './SignalDetailModal'

interface Props {
  weatherSignals: WeatherSignal[]
  onSimulateTrade: (ticker: string) => void
  isSimulating: boolean
}

type SortKey = 'edge' | 'model_probability' | 'suggested_size'
type SortDir = 'asc' | 'desc'

interface UnifiedSignal {
  key: string
  ticker: string
  title: string
  platform: string
  direction: string
  edge: number
  modelProb: number
  marketProb: number
  confidence: number
  kellyFraction: number
  suggestedSize: number
  sources: string[]
  reasoning: string
  actionable: boolean
}

function PlatformBadge({ platform }: { platform: string }) {
  const style = platformStyles[platform.toLowerCase()]
  if (!style) return null
  return (
    <span className={`platform-badge ${style.badge}`}>
      {style.icon}
    </span>
  )
}

function EdgeBar({ edge }: { edge: number }) {
  const absEdge = Math.abs(edge) * 100
  const width = Math.min(100, absEdge * 5)
  const color = edge > 0.05 ? '#22c55e' : edge > 0 ? '#22c55e80' : '#dc2626'
  return (
    <div className="edge-bar">
      <div className="edge-fill" style={{ width: `${width}%`, backgroundColor: color }} />
    </div>
  )
}

export function SignalsTable({ weatherSignals, onSimulateTrade, isSimulating }: Props) {
  const [sortKey, setSortKey] = useState<SortKey>('edge')
  const [sortDir, setSortDir] = useState<SortDir>('desc')
  const [selected, setSelected] = useState<UnifiedSignal | null>(null)

  const unified: UnifiedSignal[] = useMemo(() => {
    return weatherSignals.map(s => ({
      key: `wx-${s.market_id}`,
      ticker: s.market_id,
      title: `${s.city_name} ${s.metric} ${s.direction} ${s.threshold_f}F`,
      platform: s.platform || 'kalshi',
      direction: s.direction,
      edge: s.edge,
      modelProb: s.model_probability,
      marketProb: s.market_probability,
      confidence: s.confidence,
      kellyFraction: s.kelly_fraction,
      suggestedSize: s.suggested_size,
      sources: s.sources,
      reasoning: s.reasoning,
      actionable: s.actionable,
    }))
  }, [weatherSignals])

  const handleSort = (key: SortKey) => {
    if (sortKey === key) {
      setSortDir(sortDir === 'asc' ? 'desc' : 'asc')
    } else {
      setSortKey(key)
      setSortDir('desc')
    }
  }

  const sorted = useMemo(() => {
    return [...unified].sort((a, b) => {
      if (a.actionable !== b.actionable) return a.actionable ? -1 : 1
      let aVal: number, bVal: number
      switch (sortKey) {
        case 'edge':
          aVal = Math.abs(a.edge); bVal = Math.abs(b.edge); break
        case 'model_probability':
          aVal = a.modelProb; bVal = b.modelProb; break
        case 'suggested_size':
          aVal = a.suggestedSize; bVal = b.suggestedSize; break
        default: return 0
      }
      return sortDir === 'asc' ? aVal - bVal : bVal - aVal
    })
  }, [unified, sortKey, sortDir])

  const SortIcon = ({ column }: { column: SortKey }) => {
    if (sortKey !== column) return <ArrowUpDown className="w-3 h-3 text-neutral-600" />
    return sortDir === 'asc'
      ? <ArrowUp className="w-3 h-3 text-cyan-400" />
      : <ArrowDown className="w-3 h-3 text-cyan-400" />
  }

  if (unified.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-8 text-neutral-600">
        <p className="text-sm">No weather signals</p>
        <p className="text-xs mt-1 text-neutral-700">Run a scan or wait for next cycle</p>
      </div>
    )
  }

  return (
    <>
    <table className="w-full">
      <thead className="sticky top-0 z-10" style={{ background: 'var(--card)' }}>
        <tr className="text-neutral-500 text-left text-xs border-b border-[#23262e]">
          <th className="py-2.5 px-2 font-semibold w-8"></th>
          <th className="py-2.5 px-2 font-semibold">Signal</th>
          <th className="py-2.5 px-2 font-semibold text-center w-10">Dir</th>
          <th
            className="py-2.5 px-2 font-semibold text-right cursor-pointer hover:text-neutral-300"
            onClick={() => handleSort('edge')}
          >
            <div className="flex items-center justify-end gap-1">
              Edge <SortIcon column="edge" />
            </div>
          </th>
          <th className="py-2.5 px-2 font-semibold text-right w-10"></th>
          <th
            className="py-2.5 px-2 font-semibold text-right cursor-pointer hover:text-neutral-300"
            onClick={() => handleSort('model_probability')}
          >
            <div className="flex items-center justify-end gap-1">
              Mod <SortIcon column="model_probability" />
            </div>
          </th>
          <th
            className="py-2.5 px-2 font-semibold text-right cursor-pointer hover:text-neutral-300"
            onClick={() => handleSort('suggested_size')}
          >
            <div className="flex items-center justify-end gap-1">
              Size <SortIcon column="suggested_size" />
            </div>
          </th>
          <th className="py-2.5 px-2 font-semibold text-right w-12"></th>
        </tr>
      </thead>
      <tbody>
        {sorted.map((sig) => {
            const isYes = sig.direction === 'yes' || sig.direction === 'above'

            return (
                <tr
                  key={sig.key}
                  className={`border-b border-neutral-800/50 hover:bg-neutral-800/30 text-sm cursor-pointer ${
                    sig.actionable ? '' : 'opacity-40'
                  }`}
                  onClick={() => setSelected(sig)}
                >
                  <td className="py-2 px-2">
                    <PlatformBadge platform={sig.platform} />
                  </td>
                  <td className="py-2 px-2">
                    <span className="text-neutral-300 truncate block max-w-[150px]" title={sig.title}>
                      {sig.title}
                    </span>
                  </td>
                  <td className="py-2 px-2 text-center">
                    <span className={`text-xs font-semibold uppercase ${isYes ? 'text-green-500' : 'text-red-500'}`}>
                      {sig.direction}
                    </span>
                  </td>
                  <td className="py-2 px-2 text-right">
                    <span className={`font-semibold tabular-nums ${
                      sig.edge > 0 ? 'text-green-500' : sig.edge < 0 ? 'text-red-500' : 'text-neutral-600'
                    }`}>
                      {sig.edge === 0 ? '-' : `${Math.abs(sig.edge * 100).toFixed(1)}%`}
                    </span>
                  </td>
                  <td className="py-2 px-2">
                    <EdgeBar edge={sig.edge} />
                  </td>
                  <td className="py-2 px-2 text-right text-neutral-300 tabular-nums">
                    {(sig.modelProb * 100).toFixed(0)}%
                  </td>
                  <td className="py-2 px-2 text-right text-blue-400 tabular-nums">
                    {sig.suggestedSize > 0 ? `$${sig.suggestedSize.toFixed(0)}` : '-'}
                  </td>
                  <td className="py-2 px-2 text-right">
                    {sig.actionable && (
                      <button
                        onClick={(e) => { e.stopPropagation(); onSimulateTrade(sig.ticker) }}
                        disabled={isSimulating}
                        className="px-2.5 py-1 rounded-md text-[11px] font-semibold uppercase bg-cyan-500/10 text-cyan-400 border border-cyan-500/20 hover:bg-cyan-500/20 disabled:opacity-50"
                      >
                        Trade
                      </button>
                    )}
                  </td>
                </tr>
            )
          })}
      </tbody>
    </table>

    <SignalDetailModal
      signal={selected}
      onClose={() => setSelected(null)}
      onSimulateTrade={(ticker) => { onSimulateTrade(ticker); setSelected(null) }}
      isSimulating={isSimulating}
    />
    </>
  )
}
