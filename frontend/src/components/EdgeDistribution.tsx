import { useMemo } from 'react'
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts'
import type { WeatherSignal } from '../types'

interface Props {
  weatherSignals: WeatherSignal[]
}

const BUCKETS = ['0-2%', '2-5%', '5-10%', '10-20%', '20%+']

function getBucket(edge: number): string {
  const pct = Math.abs(edge) * 100
  if (pct < 2) return '0-2%'
  if (pct < 5) return '2-5%'
  if (pct < 10) return '5-10%'
  if (pct < 20) return '10-20%'
  return '20%+'
}

const CustomTooltip = ({ active, payload, label }: any) => {
  if (!active || !payload || !payload.length) return null
  return (
    <div className="bg-neutral-900 border border-[#23262e] rounded-lg px-3 py-2">
      <p className="text-xs text-neutral-400 mb-1">{label}</p>
      {payload.map((p: any) => (
        <p key={p.name} className="text-xs tabular-nums font-medium" style={{ color: p.color }}>
          {p.name}: {p.value}
        </p>
      ))}
    </div>
  )
}

export function EdgeDistribution({ weatherSignals }: Props) {
  const data = useMemo(() => {
    const counts: Record<string, number> = {}
    BUCKETS.forEach(b => { counts[b] = 0 })

    weatherSignals.forEach(s => {
      const bucket = getBucket(s.edge)
      counts[bucket]++
    })

    return BUCKETS.map(bucket => ({
      bucket,
      Signals: counts[bucket],
    }))
  }, [weatherSignals])

  if (weatherSignals.length === 0) {
    return (
      <div className="h-full flex items-center justify-center text-neutral-600 text-sm">
        No signals for distribution
      </div>
    )
  }

  return (
    <div className="h-full">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 8, right: 8, left: -10, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#23262e" vertical={false} />
          <XAxis
            dataKey="bucket"
            stroke="#7a7f8c"
            fontSize={12}
            tickLine={false}
            axisLine={false}
            fontFamily="JetBrains Mono"
          />
          <YAxis
            stroke="#7a7f8c"
            fontSize={12}
            tickLine={false}
            axisLine={false}
            allowDecimals={false}
            fontFamily="JetBrains Mono"
          />
          <Tooltip content={<CustomTooltip />} />
          <Bar dataKey="Signals" fill="#06b6d4" radius={[2, 2, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}
