import { useState, useEffect, Suspense, lazy, type ReactNode } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { RefreshCw, CalendarDays } from 'lucide-react'
import { fetchDashboard, runScan, simulateTrade, startBot, stopBot } from './api'
import { StatsCards } from './components/StatsCards'
import { SignalsTable } from './components/SignalsTable'
import { TradesTable } from './components/TradesTable'
import { EquityChart } from './components/EquityChart'
import { Terminal } from './components/Terminal'
import { CalibrationPanel } from './components/CalibrationPanel'
import { WeatherPanel } from './components/WeatherPanel'
import { EdgeDistribution } from './components/EdgeDistribution'
import { DailyBriefing } from './components/DailyBriefing'
import { getPnlColorClass, formatCurrency } from './utils'

const GlobeView = lazy(() => import('./components/GlobeView').then(m => ({ default: m.GlobeView })))

function LiveClock() {
  const [time, setTime] = useState(new Date())
  useEffect(() => {
    const interval = setInterval(() => setTime(new Date()), 1000)
    return () => clearInterval(interval)
  }, [])
  return (
    <span className="text-sm tabular-nums text-neutral-400">
      {time.toLocaleTimeString('en-US', { hour12: false })}
    </span>
  )
}

function RefreshBar({ interval }: { interval: number }) {
  const [progress, setProgress] = useState(100)

  useEffect(() => {
    setProgress(100)
    const step = 100 / (interval / 1000)
    const timer = setInterval(() => {
      setProgress(p => Math.max(0, p - step))
    }, 1000)
    return () => clearInterval(timer)
  }, [interval])

  return (
    <div className="refresh-bar w-16 rounded-full">
      <div className="refresh-fill" style={{ width: `${progress}%` }} />
    </div>
  )
}

/** Section wrapper used across the whole grid — gives every panel a
 * consistent card, header, and comfortable body padding so nothing on the
 * dashboard reads as a squeezed-in afterthought. */
function Panel({
  title,
  badge,
  className = '',
  children,
}: {
  title: string
  badge?: ReactNode
  className?: string
  children: ReactNode
}) {
  return (
    <div className={`panel flex flex-col min-h-0 min-w-0 ${className}`}>
      <div className="px-4 py-2.5 border-b border-[#23262e] flex items-center justify-between shrink-0">
        <span className="panel-header">{title}</span>
        {badge}
      </div>
      <div className="flex-1 min-h-0">{children}</div>
    </div>
  )
}

function App() {
  const queryClient = useQueryClient()
  const [briefingOpen, setBriefingOpen] = useState(false)

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['dashboard'],
    queryFn: fetchDashboard,
    refetchInterval: 10000,
  })

  const scanMutation = useMutation({
    mutationFn: runScan,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['dashboard'] }),
  })

  const tradeMutation = useMutation({
    mutationFn: simulateTrade,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['dashboard'] }),
  })

  const startMutation = useMutation({
    mutationFn: startBot,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['dashboard'] }),
  })

  const stopMutation = useMutation({
    mutationFn: stopBot,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['dashboard'] }),
  })

  const recentTrades = data?.recent_trades ?? []
  const weatherSignals = data?.weather_signals ?? []
  const weatherForecasts = data?.weather_forecasts ?? []

  const stats = data?.stats ?? {
    is_running: false,
    last_run: null,
    total_trades: 0,
    total_pnl: 0,
    bankroll: 10000,
    winning_trades: 0,
    win_rate: 0
  }
  const equityCurve = data?.equity_curve ?? []
  const calibration = data?.calibration ?? null
  const tailCalibration = data?.tail_calibration ?? []

  const actionableCount = weatherSignals.filter(s => s.actionable).length

  const todayStartMs = new Date(new Date().setHours(0, 0, 0, 0)).getTime()
  const todayPnl = recentTrades
    .filter(t => new Date(t.timestamp).getTime() >= todayStartMs && (t.result === 'win' || t.result === 'loss'))
    .reduce((sum, t) => sum + (t.pnl ?? 0), 0)

  if (isLoading) {
    return (
      <div className="h-screen bg-black flex items-center justify-center">
        <div className="text-center">
          <div className="relative w-12 h-12 mx-auto mb-4">
            <div className="absolute inset-0 border-2 border-neutral-800 rounded-full" />
            <div className="absolute inset-0 border-2 border-transparent border-t-cyan-500 rounded-full animate-spin" />
          </div>
          <div className="text-sm text-neutral-500 uppercase tracking-widest font-mono">Initializing</div>
        </div>
      </div>
    )
  }

  if (error || !data) {
    return (
      <div className="h-screen bg-black flex items-center justify-center">
        <div className="text-center">
          <div className="text-red-500 text-base uppercase mb-3 tracking-wider">Connection Error</div>
          <button
            onClick={() => refetch()}
            className="px-4 py-2 bg-neutral-900 border border-neutral-700 rounded-lg text-neutral-300 text-sm uppercase tracking-wider hover:bg-neutral-800 transition-colors"
          >
            Retry
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="h-screen text-neutral-200 flex flex-col overflow-hidden" style={{ background: 'var(--background)' }}>
      {/* ===== HEADER ===== */}
      <header className="shrink-0 border-b border-[#1a1c22] relative">
        <div className="scan-line" />

        {/* Row 1: identity + controls — always visible, never competes with
            the stat tiles for width, so these buttons can't get pushed
            off-screen regardless of how wide StatsCards renders. */}
        <div className="px-5 py-3 flex items-center gap-5">
          <div className="flex items-center gap-2.5 shrink-0">
            <h1 className="text-lg font-extrabold text-white tracking-tight whitespace-nowrap">
              Weather Terminal
            </h1>
            <span className={`px-2 py-1 rounded-full text-[11px] font-bold uppercase tracking-wide ${
              stats.is_running
                ? 'bg-green-500/10 text-green-400 border border-green-500/20'
                : 'bg-neutral-800 text-neutral-500 border border-neutral-700'
            }`}>
              {stats.is_running ? 'Live' : 'Idle'}
            </span>
            <span className="px-2 py-1 rounded-full text-[11px] font-bold uppercase tracking-wide bg-amber-500/10 text-amber-400 border border-amber-500/20">
              Sim
            </span>
          </div>

          <div className="flex-1" />

          <div className="flex items-center gap-3 shrink-0">
            <button
              onClick={() => setBriefingOpen(true)}
              className="flex items-center gap-2 px-4 py-2.5 bg-[#171a20] border border-[#23262e] rounded-xl hover:border-[#33363f] hover:bg-[#1c1f26] text-sm font-semibold uppercase tracking-wide transition-colors whitespace-nowrap"
            >
              <CalendarDays className="w-4 h-4 text-neutral-400" />
              <span className="text-neutral-200">Today</span>
              {todayPnl !== 0 && (
                <span className={`tabular-nums normal-case font-bold ${getPnlColorClass(todayPnl)}`}>
                  {formatCurrency(todayPnl, true)}
                </span>
              )}
            </button>
            <button
              onClick={() => scanMutation.mutate()}
              disabled={scanMutation.isPending}
              className="flex items-center gap-2 px-4 py-2.5 bg-[#171a20] border border-[#23262e] rounded-xl hover:border-[#33363f] hover:bg-[#1c1f26] text-neutral-200 text-sm font-semibold uppercase tracking-wide transition-colors disabled:opacity-50 whitespace-nowrap"
            >
              <RefreshCw className={`w-4 h-4 ${scanMutation.isPending ? 'animate-spin' : ''}`} />
              {scanMutation.isPending ? 'Scanning' : 'Scan'}
            </button>
            <LiveClock />
          </div>
        </div>

        {/* Row 2: stat tiles — free to wrap on narrower windows without
            ever touching the controls above. */}
        <div className="px-5 pb-3 flex items-center gap-2.5 flex-wrap">
          <StatsCards stats={stats} />
        </div>
      </header>

      {/* ===== MAIN GRID ===== */}
      <div className="flex-1 min-h-0 grid grid-cols-[360px_1fr_400px] grid-rows-[1fr] gap-3 p-3">

        {/* ===== LEFT COLUMN ===== */}
        <div className="flex flex-col min-h-0 min-w-0 gap-3">
          <Panel
            title="Equity Curve"
            className="flex-1"
            badge={
              <span className={`text-sm font-semibold tabular-nums ${stats.total_pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                {stats.total_pnl >= 0 ? '+' : ''}${stats.total_pnl.toFixed(0)}
              </span>
            }
          >
            <div className="h-full p-2">
              <EquityChart data={equityCurve} initialBankroll={stats.bankroll - stats.total_pnl} />
            </div>
          </Panel>

          {calibration && calibration.total_with_outcome > 0 && (
            <div className="shrink-0">
              <Panel
                title="Calibration"
                badge={<span className="text-xs text-neutral-500 tabular-nums">{calibration.total_with_outcome} settled</span>}
              >
                <div className="p-4">
                  <CalibrationPanel calibration={calibration} tailCalibration={tailCalibration} />
                </div>
              </Panel>
            </div>
          )}
        </div>

        {/* ===== CENTER COLUMN ===== */}
        <div className="flex flex-col min-h-0 min-w-0 gap-3">
          <div className="panel relative overflow-hidden min-w-0" style={{ height: '56%' }}>
            <Suspense fallback={
              <div className="w-full h-full flex items-center justify-center bg-black rounded-xl">
                <span className="text-sm text-neutral-600 uppercase tracking-wider">Loading Globe...</span>
              </div>
            }>
              <GlobeView forecasts={weatherForecasts} signals={weatherSignals} />
            </Suspense>
            <div className="absolute top-3 left-3 z-10">
              <div className="px-3 py-1.5 rounded-lg bg-black/80 border border-[#23262e] text-xs backdrop-blur-sm">
                <span className="text-neutral-400 uppercase tracking-wider mr-2">Markets</span>
                <span className="text-cyan-400 tabular-nums font-semibold">{actionableCount} actionable</span>
              </div>
            </div>
          </div>

          <div className="flex-1 min-h-0 min-w-0 grid grid-cols-2 grid-rows-[1fr] gap-3">
            <Panel title="Edge Distribution">
              <div className="h-full p-2">
                <EdgeDistribution weatherSignals={weatherSignals} />
              </div>
            </Panel>

            <Panel
              title="Forecasts"
              badge={<span className="px-2 py-0.5 rounded-full text-[10px] font-bold uppercase bg-cyan-500/10 text-cyan-400 border border-cyan-500/20">WX</span>}
            >
              <div className="h-full overflow-y-auto p-1">
                <WeatherPanel forecasts={weatherForecasts} signals={weatherSignals} />
              </div>
            </Panel>
          </div>
        </div>

        {/* ===== RIGHT COLUMN ===== */}
        <div className="flex flex-col min-h-0 min-w-0 gap-3">
          <Panel
            title="Signals"
            className="flex-1"
            badge={<span className="text-sm text-cyan-400 tabular-nums font-semibold">{weatherSignals.length} WX</span>}
          >
            <div className="h-full overflow-y-auto">
              <SignalsTable
                weatherSignals={weatherSignals}
                onSimulateTrade={(ticker) => tradeMutation.mutate(ticker)}
                isSimulating={tradeMutation.isPending}
              />
            </div>
          </Panel>

          <Panel
            title="Trades"
            className="flex-1"
            badge={
              <span className="flex items-center gap-2">
                <span className="text-sm text-neutral-500 tabular-nums">{recentTrades.length}</span>
                {/* Added 2026-08-23: at-a-glance count of early exits (trailing-stop /
                    price-stop-loss / METAR liquidations) in the currently loaded trades,
                    so this doesn't require opening every row to notice the pattern. */}
                {recentTrades.some(t => t.execution_type === 'liquidated') && (
                  <span
                    className="text-[10px] font-semibold uppercase tracking-wide text-amber-400 bg-amber-500/10 border border-amber-500/30 rounded px-1.5 py-0.5 tabular-nums"
                    title="Trades sold early via the exit logic, not held to a real settlement"
                  >
                    {recentTrades.filter(t => t.execution_type === 'liquidated').length} early
                  </span>
                )}
              </span>
            }
          >
            <div className="h-full overflow-y-auto">
              <TradesTable trades={recentTrades} />
            </div>
          </Panel>
        </div>
      </div>

      {/* ===== SYSTEM LOG — always visible, full width, never squeezed ===== */}
      <div className="shrink-0 px-3 pb-3" style={{ height: '260px' }}>
        <Terminal
          isRunning={stats.is_running}
          lastRun={stats.last_run}
          stats={{ total_trades: stats.total_trades, total_pnl: stats.total_pnl }}
          onStart={() => startMutation.mutate()}
          onStop={() => stopMutation.mutate()}
          onScan={() => scanMutation.mutate()}
        />
      </div>

      {/* ===== FOOTER ===== */}
      <footer className="shrink-0 border-t border-[#1a1c22] px-4 py-1.5 flex items-center justify-between">
        <span className="text-xs text-neutral-600 font-mono">
          Open-Meteo | Polymarket + Kalshi
        </span>
        <div className="flex items-center gap-3">
          <RefreshBar interval={10000} />
          <span className="text-xs text-neutral-600 font-mono">Weather Temperature</span>
          <div className="flex items-center gap-1.5">
            <div className="w-2 h-2 rounded-full bg-green-500" />
            <span className="text-xs text-neutral-500 font-mono">Connected</span>
          </div>
        </div>
      </footer>

      <DailyBriefing
        isOpen={briefingOpen}
        onClose={() => setBriefingOpen(false)}
        trades={recentTrades}
        stats={stats}
        equityCurve={equityCurve}
      />
    </div>
  )
}

export default App
