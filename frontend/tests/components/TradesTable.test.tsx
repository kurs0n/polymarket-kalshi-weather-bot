import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { TradesTable } from '../../src/components/TradesTable'
import type { Trade } from '../../src/types'

const trades: Trade[] = [
  {
    id: 1,
    market_ticker: 'AUS-82-ABOVE',
    platform: 'kalshi',
    event_slug: 'Austin 82F above',
    direction: 'yes',
    entry_price: 0.58,
    size: 120,
    timestamp: '2026-01-01T12:00:00Z',
    settled: true,
    result: 'win',
    pnl: 20
  },
  {
    id: 2,
    market_ticker: 'DEN-65-BELOW',
    platform: 'polymarket',
    event_slug: 'Denver 65F below',
    direction: 'no',
    entry_price: 0.46,
    size: 40,
    timestamp: '2026-01-02T12:00:00Z',
    settled: true,
    result: 'loss',
    pnl: -15
  },
  {
    id: 3,
    market_ticker: 'PHX-95-ABOVE',
    platform: 'kalshi',
    event_slug: 'Phoenix 95F above',
    direction: 'yes',
    entry_price: 0.52,
    size: 80,
    timestamp: '2026-01-03T12:00:00Z',
    settled: false,
    result: 'pending',
    pnl: null
  }
]

describe('TradesTable', () => {
  it('renders an empty state when no trades are available', () => {
    render(<TradesTable trades={[]} />)

    expect(screen.getByText('No trades yet')).toBeTruthy()
    expect(screen.getByText('Trades will appear here')).toBeTruthy()
  })

  it('shows the winning trade and can sort by size', async () => {
    const user = userEvent.setup()
    render(<TradesTable trades={trades} />)

    expect(screen.getByText('WIN')).toBeTruthy()
    expect(screen.getByText('+$20')).toBeTruthy()

    await user.click(screen.getByText('Size'))

    const rows = screen.getAllByRole('row')
    expect(rows[1].textContent).toContain('Austin 82F above')
    expect(rows[1].textContent).toContain('$120')
    expect(rows[2].textContent).toContain('Phoenix 95F above')
  })
})
