import type { ReactNode } from 'react'
import { Wallet, TrendingUp, TrendingDown, Target, Activity } from 'lucide-react'
import type { BotStats } from '../types'

interface Props {
  stats: BotStats
}

function StatTile({
  icon,
  label,
  value,
  valueColor,
  sub,
}: {
  icon: ReactNode
  label: string
  value: string
  valueColor?: string
  sub?: string
}) {
  return (
    <div className="flex items-center gap-3 px-4 py-2.5 rounded-xl bg-[#171a20] border border-[#23262e]">
      <div className="w-8 h-8 rounded-lg bg-black/30 flex items-center justify-center text-neutral-400 shrink-0">
        {icon}
      </div>
      <div className="leading-tight">
        <div className="text-[11px] text-neutral-500 uppercase tracking-wider">{label}</div>
        <div className="flex items-baseline gap-1.5">
          <span className={`text-lg font-bold tabular-nums ${valueColor ?? 'text-neutral-100'}`}>
            {value}
          </span>
          {sub && <span className="text-xs text-neutral-500 tabular-nums">{sub}</span>}
        </div>
      </div>
    </div>
  )
}

export function StatsCards({ stats }: Props) {
  const winRate = stats.total_trades > 0 ? (stats.winning_trades / stats.total_trades * 100) : 0
  const returnPercent = stats.bankroll - stats.total_pnl > 0
    ? ((stats.total_pnl / (stats.bankroll - stats.total_pnl)) * 100)
    : 0
  const isProfit = stats.total_pnl >= 0

  return (
    <>
      <StatTile
        icon={<Wallet className="w-4 h-4" />}
        label="Bankroll"
        value={`$${stats.bankroll >= 1000 ? (stats.bankroll / 1000).toFixed(1) + 'K' : stats.bankroll.toFixed(0)}`}
      />
      <StatTile
        icon={isProfit ? <TrendingUp className="w-4 h-4" /> : <TrendingDown className="w-4 h-4" />}
        label="P&L"
        value={`${isProfit ? '+' : ''}$${Math.abs(stats.total_pnl).toFixed(0)}`}
        valueColor={isProfit ? 'text-green-400' : 'text-red-400'}
        sub={`${returnPercent >= 0 ? '+' : ''}${returnPercent.toFixed(1)}%`}
      />
      <StatTile
        icon={<Target className="w-4 h-4" />}
        label="Win Rate"
        value={`${winRate.toFixed(0)}%`}
        valueColor={winRate >= 55 ? 'text-green-400' : winRate >= 45 ? 'text-yellow-400' : 'text-red-400'}
        sub={`${stats.winning_trades}/${stats.total_trades}`}
      />
      <StatTile
        icon={<Activity className="w-4 h-4" />}
        label="Trades"
        value={String(stats.total_trades)}
        sub={stats.is_running ? 'live' : undefined}
      />
    </>
  )
}
