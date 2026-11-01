import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  ReferenceLine,
} from 'recharts'
import { motion } from 'framer-motion'
import type { EquityPoint } from '../types'

interface Props {
  data: EquityPoint[]
  initialBankroll: number
}

interface TooltipItem {
  value?: number
}

interface CustomTooltipProps {
  active?: boolean
  payload?: TooltipItem[]
  label?: string | number
}

const CustomTooltip = ({ active, payload, label }: CustomTooltipProps) => {
  if (!active || !payload || !payload.length) return null
  const value = payload[0].value ?? 0
  const isPositive = value >= 0

  return (
    <div className="bg-[#0a0a0a] border border-neutral-800 px-2 py-1.5">
      <p className="text-[10px] text-neutral-500 mb-0.5">{label}</p>
      <p className={`text-sm font-semibold tabular-nums ${isPositive ? 'text-green-500' : 'text-red-500'}`}>
        {isPositive ? '+' : ''}${value.toFixed(2)}
      </p>
    </div>
  )
}

export function EquityChart({ data, initialBankroll }: Props) {
  if (data.length === 0) {
    return (
      <div className="h-full flex flex-col items-center justify-center text-neutral-600">
        <p className="text-xs">No trade history</p>
        <p className="text-[10px] mt-0.5">Chart appears after settled trades</p>
      </div>
    )
  }

  const chartData = [
    { timestamp: 'Start', pnl: 0, bankroll: initialBankroll },
    ...data.map((d) => ({
      ...d,
      timestamp: new Date(d.timestamp).toLocaleDateString('en-US', { month: 'short', day: 'numeric' }),
    })),
  ]

  const currentPnl = data.length > 0 ? data[data.length - 1].pnl : 0
  const isPositive = currentPnl >= 0
  const minPnl = Math.min(0, ...data.map((d) => d.pnl))
  const maxPnl = Math.max(0, ...data.map((d) => d.pnl))
  const padding = Math.max(Math.abs(minPnl), Math.abs(maxPnl)) * 0.2

  const gradientId = `equityGradient-${isPositive ? 'green' : 'red'}`

  return (
    <motion.div
      className="h-full"
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      transition={{ duration: 0.5 }}
    >
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={chartData} margin={{ top: 5, right: 5, left: -15, bottom: 0 }}>
          <defs>
            <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%" stopColor={isPositive ? '#22c55e' : '#ef4444'} stopOpacity={0.25} />
              <stop offset="95%" stopColor={isPositive ? '#22c55e' : '#ef4444'} stopOpacity={0} />
            </linearGradient>
          </defs>

          <CartesianGrid strokeDasharray="3 3" stroke="#1a1a1a" vertical={false} />

          <XAxis
            dataKey="timestamp"
            stroke="#525252"
            fontSize={9}
            tickLine={false}
            axisLine={false}
            dy={5}
            fontFamily="JetBrains Mono"
          />

          <YAxis
            stroke="#525252"
            fontSize={9}
            tickLine={false}
            axisLine={false}
            tickFormatter={(value) => `$${value}`}
            domain={[minPnl - padding, maxPnl + padding]}
            dx={-5}
            fontFamily="JetBrains Mono"
          />

          <Tooltip content={<CustomTooltip />} />

          <ReferenceLine y={0} stroke="#262626" strokeDasharray="3 3" />

          <Area
            type="monotone"
            dataKey="pnl"
            stroke={isPositive ? '#22c55e' : '#ef4444'}
            strokeWidth={1.5}
            fill={`url(#${gradientId})`}
            animationDuration={800}
          />
        </AreaChart>
      </ResponsiveContainer>
    </motion.div>
  )
}
