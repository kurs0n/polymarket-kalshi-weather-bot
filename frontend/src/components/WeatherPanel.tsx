import type { WeatherForecast, WeatherSignal } from '../types'
import { platformStyles } from '../utils'

interface Props {
  forecasts: WeatherForecast[]
  signals: WeatherSignal[]
}

function AgreementBar({ value }: { value: number }) {
  const pct = Math.max(0, Math.min(100, value * 100))
  const color = value > 0.7 ? '#22c55e' : value > 0.5 ? '#d97706' : '#dc2626'
  return (
    <div className="edge-bar w-12">
      <div className="edge-fill" style={{ width: `${pct}%`, backgroundColor: color }} />
    </div>
  )
}

export function WeatherPanel({ forecasts, signals }: Props) {
  if (forecasts.length === 0 && signals.length === 0) {
    return (
      <div className="h-full flex items-center justify-center text-neutral-600 text-sm">
        No weather data
      </div>
    )
  }

  // Keyed by city_key + target_date, not city_key alone — a city can have
  // more than one forecast row in play (e.g. today's and tomorrow's markets
  // both actionable at once), and pairing a signal with the wrong date's
  // forecast is exactly the bug this was fixed to avoid.
  const dateKey = (cityKey: string, targetDate: string) => `${cityKey}|${targetDate}`
  const signalsByCityDate = new Map<string, WeatherSignal[]>()
  signals.forEach(s => {
    const key = dateKey(s.city_key, s.target_date)
    const existing = signalsByCityDate.get(key) || []
    existing.push(s)
    signalsByCityDate.set(key, existing)
  })

  return (
    <div className="space-y-1 overflow-y-auto max-h-full">
      {forecasts.map(f => {
        const citySignals = signalsByCityDate.get(dateKey(f.city_key, f.target_date)) || []
        const actionable = citySignals.filter(s => s.actionable)
        const bestEdge = citySignals.length > 0
          ? citySignals.reduce((a, b) => Math.abs(a.edge) > Math.abs(b.edge) ? a : b)
          : null

        // Model breakdown for the tooltip — only the sources that were
        // actually fetched/blended for this forecast are listed.
        const weightsStr = f.model_weights
          ? Object.entries(f.model_weights).map(([m, w]) => `${m}=${w.toFixed(2)}`).join(' ')
          : null
        const breakdown = [
          `GFS ${f.mean_high.toFixed(1)}F`,
          f.hrrr_high != null ? `HRRR ${f.hrrr_high.toFixed(1)}F` : null,
          f.ecmwf_high != null ? `ECMWF ${f.ecmwf_high.toFixed(1)}F` : null,
          f.nws_high != null ? `NWS ${f.nws_high.toFixed(1)}F` : null,
          f.bias_correction_f !== 0 ? `bias ${f.bias_correction_f >= 0 ? '+' : ''}${f.bias_correction_f.toFixed(1)}F` : null,
          weightsStr ? `weights: ${weightsStr}` : null,
        ].filter(Boolean).join(' | ')

        return (
          <div
            key={`${f.city_key}-${f.target_date}`}
            className={`flex items-center gap-3 px-3 py-2.5 rounded-lg ${
              actionable.length > 0 ? 'border-l-2 border-l-green-500 bg-green-500/5' : 'border-l-2 border-l-transparent'
            }`}
          >
            <div className="w-14 shrink-0">
              <div className="text-xs font-semibold text-neutral-200">{f.city_name}</div>
              <div className="text-[10px] text-neutral-500">{f.target_date.slice(5)}</div>
            </div>
            <div className="flex-1 flex items-center gap-3 text-xs tabular-nums" title={breakdown}>
              <span className="text-neutral-300">
                {f.effective_mean_high.toFixed(0)}F
                <span className="text-neutral-500 ml-0.5">+/-{f.std_high.toFixed(0)}</span>
                {Math.abs(f.effective_mean_high - f.mean_high) >= 0.5 && (
                  <span className="text-neutral-500 ml-1">
                    (GFS {f.mean_high.toFixed(0)}F)
                  </span>
                )}
              </span>
              <AgreementBar value={f.ensemble_agreement} />
              <span className={`${f.ensemble_agreement > 0.7 ? 'text-green-500' : 'text-amber-500'}`}>
                {(f.ensemble_agreement * 100).toFixed(0)}%
              </span>
            </div>
            <div className="flex items-center gap-2 shrink-0">
              {bestEdge && (
                <span className={`text-xs font-semibold tabular-nums ${bestEdge.edge > 0 ? 'text-green-500' : 'text-red-500'}`}>
                  {bestEdge.edge > 0 ? '+' : ''}{(bestEdge.edge * 100).toFixed(1)}%
                </span>
              )}
              {citySignals.length > 0 && citySignals[0].platform && (
                <span className={`platform-badge ${
                  platformStyles[citySignals[0].platform.toLowerCase()]?.badge || 'bg-neutral-800 text-neutral-400 border-neutral-700'
                }`}>
                  {platformStyles[citySignals[0].platform.toLowerCase()]?.icon || '?'}
                </span>
              )}
            </div>
          </div>
        )
      })}
    </div>
  )
}
