import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { BotStats } from '../../src/types'
import { StatsCards } from '../../src/components/StatsCards'

describe('StatsCards', () => {
  it('formats bankroll, return, win rate, and trade count', () => {
    const stats: BotStats = {
      bankroll: 2500,
      total_trades: 10,
      winning_trades: 6,
      win_rate: 0.6,
      total_pnl: 300,
      is_running: true,
      last_run: null
    }

    render(<StatsCards stats={stats} />)

    expect(screen.getByText('$2.5K')).toBeTruthy()
    expect(screen.getByText('+$300')).toBeTruthy()
    expect(screen.getByText('+13.6%')).toBeTruthy()
    expect(screen.getByText('60%')).toBeTruthy()
    expect(screen.getByText('6/10')).toBeTruthy()
    expect(screen.getByText('10')).toBeTruthy()
  })

  it('shows zero values for a fresh bankroll with no trades', () => {
    const stats: BotStats = {
      bankroll: 0,
      total_trades: 0,
      winning_trades: 0,
      win_rate: 0,
      total_pnl: 0,
      is_running: false,
      last_run: null
    }

    render(<StatsCards stats={stats} />)

    expect(screen.getByText('$0')).toBeTruthy()
    expect(screen.getByText('+0.0%')).toBeTruthy()
    expect(screen.getByText('0%')).toBeTruthy()
    expect(screen.getByText('0/0')).toBeTruthy()
  })
})