import { useEffect, useRef, useState, useCallback } from 'react'
import { Play, Pause, RefreshCw } from 'lucide-react'

interface LogEntry {
  timestamp: string
  type: 'info' | 'success' | 'warning' | 'error' | 'data' | 'trade' | 'heartbeat'
  message: string
  data?: Record<string, any>
}

interface Props {
  isRunning: boolean
  lastRun: string | null
  stats: {
    total_trades: number
    total_pnl: number
  }
  onStart?: () => void
  onStop?: () => void
  onScan?: () => void
}

const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000'
const WS_URL = API_URL.replace(/^http/, 'ws') + '/ws/events'

export function Terminal({ isRunning, lastRun, onStart, onStop, onScan }: Props) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const [logs, setLogs] = useState<LogEntry[]>([])
  const [cursorVisible, setCursorVisible] = useState(true)
  const [wsConnected, setWsConnected] = useState(false)
  const wsRef = useRef<WebSocket | null>(null)
  const reconnectTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  // Fetch initial events
  const fetchEvents = useCallback(async () => {
    try {
      const res = await fetch(`${API_URL}/api/events?limit=30`)
      if (res.ok) {
        const events = await res.json()
        setLogs(events.filter((e: LogEntry) => e.type !== 'heartbeat'))
      }
    } catch (err) {
      console.error('Failed to fetch events:', err)
    }
  }, [])

  // WebSocket connection
  useEffect(() => {
    const connectWs = () => {
      try {
        const ws = new WebSocket(WS_URL)
        wsRef.current = ws

        ws.onopen = () => {
          setWsConnected(true)
          setLogs(prev => [...prev, {
            timestamp: new Date().toISOString(),
            type: 'success',
            message: 'WebSocket connected'
          }])
        }

        ws.onmessage = (event) => {
          try {
            const data = JSON.parse(event.data)
            if (data.type === 'heartbeat') return // Skip heartbeats
            setLogs(prev => [...prev.slice(-100), data])
          } catch (e) {
            console.error('Failed to parse WebSocket message:', e)
          }
        }

        ws.onclose = () => {
          setWsConnected(false)
          wsRef.current = null
          // Attempt reconnect after 5 seconds
          reconnectTimeoutRef.current = setTimeout(connectWs, 5000)
        }

        ws.onerror = () => {
          ws.close()
        }
      } catch (err) {
        // Fallback to polling if WebSocket fails
        setWsConnected(false)
      }
    }

    // Initial fetch and connect
    fetchEvents()
    connectWs()

    return () => {
      if (wsRef.current) {
        wsRef.current.close()
      }
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current)
      }
    }
  }, [fetchEvents])

  // Polling fallback if WebSocket not connected
  useEffect(() => {
    if (wsConnected) return

    const interval = setInterval(fetchEvents, 5000)
    return () => clearInterval(interval)
  }, [wsConnected, fetchEvents])

  // Auto-scroll to bottom
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight
    }
  }, [logs])

  // Cursor blink
  useEffect(() => {
    const interval = setInterval(() => {
      setCursorVisible(v => !v)
    }, 500)
    return () => clearInterval(interval)
  }, [])

  const formatTime = (timestamp: string) => {
    try {
      return new Date(timestamp).toLocaleTimeString('en-US', { hour12: false })
    } catch {
      return '--:--:--'
    }
  }

  const getTypeColor = (type: LogEntry['type']) => {
    switch (type) {
      case 'success': return 'text-green-500'
      case 'error': return 'text-red-500'
      case 'warning': return 'text-amber-500'
      case 'data': return 'text-blue-500'
      case 'trade': return 'text-purple-500'
      default: return 'text-neutral-400'
    }
  }

  const getTypePrefix = (type: LogEntry['type']) => {
    switch (type) {
      case 'success': return '[OK]'
      case 'error': return '[ERR]'
      case 'warning': return '[WARN]'
      case 'data': return '[DATA]'
      case 'trade': return '[TRADE]'
      default: return '[INFO]'
    }
  }

  return (
    <div className="terminal h-full flex flex-col">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-2.5 border-b border-[#23262e]">
        <div className="flex items-center gap-2.5">
          <div className="flex gap-1.5">
            <div className="w-3 h-3 rounded-full bg-red-500/80" />
            <div className="w-3 h-3 rounded-full bg-yellow-500/80" />
            <div className="w-3 h-3 rounded-full bg-green-500/80" />
          </div>
          <span className="panel-header ml-1">System Log</span>
        </div>
        <div className="flex items-center gap-4">
          <div className="flex items-center gap-1.5">
            <div className={`w-2 h-2 rounded-full ${wsConnected ? 'bg-green-500' : 'bg-amber-500'}`} />
            <span className="text-xs text-neutral-500 uppercase tracking-wide">
              {wsConnected ? 'WS' : 'POLL'}
            </span>
          </div>
          <div className="flex items-center gap-1.5">
            {isRunning && <div className="live-dot" />}
            <span className="text-xs text-neutral-500 uppercase tracking-wide">
              {isRunning ? 'LIVE' : 'IDLE'}
            </span>
          </div>
        </div>
      </div>

      {/* Log content */}
      <div ref={scrollRef} className="flex-1 overflow-y-auto px-4 py-2.5 space-y-1 min-h-0">
        {logs.length === 0 ? (
          <div className="text-neutral-600 text-sm">Waiting for events...</div>
        ) : (
          logs.map((log, i) => (
            <div key={i} className="flex gap-3 text-sm leading-relaxed">
              <span className="text-neutral-600 tabular-nums shrink-0">
                {formatTime(log.timestamp)}
              </span>
              <span className={`shrink-0 font-medium ${getTypeColor(log.type)}`}>
                {getTypePrefix(log.type)}
              </span>
              <span className={getTypeColor(log.type)}>
                {log.message}
              </span>
            </div>
          ))
        )}

        {/* Cursor line */}
        <div className="flex gap-3 text-sm">
          <span className="text-neutral-600 tabular-nums">
            {formatTime(new Date().toISOString())}
          </span>
          <span className="text-green-500">{'>'}</span>
          <span className={`text-green-500 ${cursorVisible ? 'opacity-100' : 'opacity-0'}`}>_</span>
        </div>
      </div>

      {/* Footer with controls */}
      <div className="px-4 py-2.5 border-t border-[#23262e] flex justify-between items-center">
        <div className="flex items-center gap-2.5">
          {onStart && onStop && (
            <button
              onClick={isRunning ? onStop : onStart}
              className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold uppercase tracking-wide border transition-colors ${
                isRunning
                  ? 'border-amber-500/30 text-amber-400 hover:bg-amber-500/10'
                  : 'border-green-500/30 text-green-400 hover:bg-green-500/10'
              }`}
            >
              {isRunning ? <Pause className="w-3.5 h-3.5" /> : <Play className="w-3.5 h-3.5" />}
              {isRunning ? 'Pause' : 'Start'}
            </button>
          )}
          {onScan && (
            <button
              onClick={onScan}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold uppercase tracking-wide border border-blue-500/30 text-blue-400 hover:bg-blue-500/10 transition-colors"
            >
              <RefreshCw className="w-3.5 h-3.5" />
              Scan
            </button>
          )}
        </div>
        <div className="flex items-center gap-4">
          <span className="text-xs text-neutral-500">
            {lastRun ? `Last: ${formatTime(lastRun)}` : 'No scans'}
          </span>
          <span className="text-xs text-neutral-500 tabular-nums">
            {logs.length} entries
          </span>
        </div>
      </div>
    </div>
  )
}
