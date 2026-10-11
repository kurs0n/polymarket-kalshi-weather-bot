import { render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { EquityChart } from '../../src/components/EquityChart'
import type { EquityPoint } from '../../src/types'

vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  AreaChart: ({ data, children }: {
    data: Array<{ timestamp: string; pnl: number; bankroll: number }>
    children: ReactNode
  }) => (
    <div>
      <div data-testid="equity-points">
        {data.map(point => `${point.timestamp}: ${point.pnl}`).join(', ')}
      </div>
      {Array.isArray(children) ? children[children.length - 1] : null}
    </div>
  ),
  Area: ({ stroke }: { stroke: string }) => <div data-testid="equity-area" data-stroke={stroke} />,
  XAxis: () => null,
  YAxis: () => null,
  CartesianGrid: () => null,
  Tooltip: () => null,
  ReferenceLine: () => null
}))

const equityData: EquityPoint[] = [
  { timestamp: '2026-09-27T12:00:00Z', pnl: 125, bankroll: 10125 },
  { timestamp: '2026-09-28T12:00:00Z', pnl: -30, bankroll: 9970 }
]

describe('EquityChart', () => {
  it('shows guidance when there is no settled-trade history', () => {
    render(<EquityChart data={[]} initialBankroll={10000} />)

    expect(screen.getByText('No trade history')).toBeTruthy()
    expect(screen.getByText('Chart appears after settled trades')).toBeTruthy()
  })

  it('plots the starting bankroll and formatted history using the current P&L color', () => {
    render(<EquityChart data={equityData} initialBankroll={10000} />)

    expect(screen.getByTestId('equity-points').textContent).toContain('Start: 0')
    expect(screen.getByTestId('equity-points').textContent).toContain('Sep 27: 125')
    expect(screen.getByTestId('equity-points').textContent).toContain('Sep 28: -30')
    expect(screen.getByTestId('equity-area').getAttribute('data-stroke')).toBe('#ef4444')
  })
})
