import { useEffect } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { X } from 'lucide-react'
import { platformStyles } from '../utils'

export interface SignalDetail {
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

interface Props {
  signal: SignalDetail | null
  onClose: () => void
  onSimulateTrade: (ticker: string) => void
  isSimulating: boolean
}

function Stat({ label, value, valueColor }: { label: string; value: string; valueColor?: string }) {
  return (
    <div className="rounded-xl bg-[#171a20] border border-[#23262e] px-4 py-3">
      <div className="text-[11px] text-neutral-500 uppercase tracking-wide mb-1">{label}</div>
      <div className={`text-xl font-bold tabular-nums ${valueColor ?? 'text-neutral-100'}`}>{value}</div>
    </div>
  )
}

export function SignalDetailModal({ signal, onClose, onSimulateTrade, isSimulating }: Props) {
  useEffect(() => {
    if (!signal) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [signal, onClose])

  const isYes = signal ? (signal.direction === 'yes' || signal.direction === 'above') : false
  const style = signal ? platformStyles[signal.platform.toLowerCase()] : undefined

  return (
    <AnimatePresence>
      {signal && (
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
                  <h2 className="text-lg font-extrabold text-white">{signal.title}</h2>
                  <p className="text-xs text-neutral-500 font-mono">{signal.ticker}</p>
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
                  value={signal.direction.toUpperCase()}
                  valueColor={isYes ? 'text-green-400' : 'text-red-400'}
                />
                <Stat
                  label="Edge"
                  value={`${signal.edge > 0 ? '+' : ''}${(signal.edge * 100).toFixed(1)}%`}
                  valueColor={signal.edge > 0 ? 'text-green-400' : signal.edge < 0 ? 'text-red-400' : undefined}
                />
                <Stat label="Confidence" value={`${(signal.confidence * 100).toFixed(0)}%`} />
                <Stat label="Model Prob." value={`${(signal.modelProb * 100).toFixed(1)}%`} />
                <Stat label="Market Prob." value={`${(signal.marketProb * 100).toFixed(1)}%`} />
                <Stat label="Kelly" value={`${(signal.kellyFraction * 100).toFixed(1)}%`} />
              </div>

              <div className="rounded-xl bg-[#171a20] border border-[#23262e] px-4 py-3">
                <div className="text-[11px] text-neutral-500 uppercase tracking-wide mb-1">Suggested Size</div>
                <div className="text-2xl font-bold tabular-nums text-blue-400">
                  {signal.suggestedSize > 0 ? `$${signal.suggestedSize.toFixed(0)}` : '—'}
                </div>
              </div>

              {signal.sources.length > 0 && (
                <div>
                  <div className="panel-header mb-1.5">Sources</div>
                  <div className="text-sm text-neutral-300">{signal.sources.join(', ')}</div>
                </div>
              )}

              {signal.reasoning && (
                <div>
                  <div className="panel-header mb-1.5">Reasoning</div>
                  <div className="text-sm text-neutral-400 leading-relaxed">{signal.reasoning}</div>
                </div>
              )}

              {signal.actionable && (
                <button
                  onClick={() => onSimulateTrade(signal.ticker)}
                  disabled={isSimulating}
                  className="w-full py-3 rounded-xl text-sm font-semibold uppercase tracking-wide bg-cyan-500/10 text-cyan-400 border border-cyan-500/20 hover:bg-cyan-500/20 disabled:opacity-50 transition-colors"
                >
                  {isSimulating ? 'Placing…' : 'Simulate Trade'}
                </button>
              )}
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  )
}
