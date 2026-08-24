import type { CalibrationSummary, TailCalibrationBucket } from '../types'

interface Props {
  calibration: CalibrationSummary
  tailCalibration?: TailCalibrationBucket[]
}

export function CalibrationPanel({ calibration, tailCalibration = [] }: Props) {
  const accuracyPct = (calibration.accuracy * 100).toFixed(0)
  const accuracyColor = calibration.accuracy >= 0.55 ? '#22c55e' : calibration.accuracy < 0.50 ? '#dc2626' : '#a1a1aa'

  const brierLabel = calibration.brier_score <= 0.20 ? 'Good' : calibration.brier_score <= 0.25 ? 'OK' : 'Poor'
  const brierColor = calibration.brier_score <= 0.20 ? '#22c55e' : calibration.brier_score <= 0.25 ? '#d97706' : '#dc2626'

  const predEdge = (calibration.avg_predicted_edge * 100).toFixed(1)
  const actualEdge = (calibration.avg_actual_edge * 100).toFixed(1)

  return (
    <div className="space-y-3">
      {/* Accuracy - large display */}
      <div className="flex items-center gap-4">
        <div className="text-3xl font-bold tabular-nums" style={{ color: accuracyColor }}>
          {accuracyPct}%
        </div>
        <div className="text-xs text-neutral-500 leading-tight">
          <div className="uppercase tracking-wide">Accuracy</div>
          <div className="tabular-nums text-neutral-400">
            {Math.round(calibration.accuracy * calibration.total_with_outcome)}/{calibration.total_with_outcome}
          </div>
        </div>
      </div>

      {/* Brier + Edge comparison */}
      <div className="flex items-center justify-between text-xs">
        <div>
          <span className="text-neutral-500">Brier: </span>
          <span className="tabular-nums font-medium" style={{ color: brierColor }}>
            {calibration.brier_score.toFixed(3)} ({brierLabel})
          </span>
        </div>
      </div>

      {/* Predicted vs Actual edge bars */}
      <div className="space-y-1.5">
        <div className="flex items-center gap-2 text-xs">
          <span className="text-neutral-500 w-12 shrink-0">Pred</span>
          <div className="flex-1 meter-bar">
            <div
              className="meter-fill"
              style={{
                width: `${Math.min(100, Math.abs(calibration.avg_predicted_edge) * 500)}%`,
                backgroundColor: '#d97706'
              }}
            />
          </div>
          <span className="tabular-nums text-amber-500 w-12 text-right font-medium">{predEdge}%</span>
        </div>
        <div className="flex items-center gap-2 text-xs">
          <span className="text-neutral-500 w-12 shrink-0">Actual</span>
          <div className="flex-1 meter-bar">
            <div
              className="meter-fill"
              style={{
                width: `${Math.min(100, Math.abs(calibration.avg_actual_edge) * 500)}%`,
                backgroundColor: calibration.avg_actual_edge >= 0 ? '#22c55e' : '#dc2626'
              }}
            />
          </div>
          <span
            className="tabular-nums w-12 text-right font-medium"
            style={{ color: calibration.avg_actual_edge >= 0 ? '#22c55e' : '#dc2626' }}
          >
            {actualEdge}%
          </span>
        </div>
      </div>

      <div className="text-xs text-neutral-500 tabular-nums">
        {calibration.total_signals} tracked / {calibration.total_with_outcome} settled
      </div>

      {/* Tail calibration — see backend/core/calibration.py. Only buckets
          with enough settled history to be trusted show up here; this
          panel is the only place the live adjustment currently applied to
          extreme-probability signals is visible at all. */}
      {tailCalibration.length > 0 && (
        <div className="pt-2 mt-1 border-t border-[#23262e] space-y-1">
          <div className="text-xs text-neutral-500 uppercase tracking-wider">Tail calibration</div>
          {tailCalibration.map(b => (
            <div key={b.bucket} className="flex items-center justify-between text-xs tabular-nums">
              <span className="text-neutral-500">{b.bucket}</span>
              <span className="text-neutral-300">{(b.empirical_win_rate * 100).toFixed(0)}% actual</span>
              <span className="text-neutral-500">n={b.n}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
