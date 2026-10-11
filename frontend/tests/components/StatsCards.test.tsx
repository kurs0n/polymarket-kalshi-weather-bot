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

  it('renders losing positions in red and keeps live status visible', () => {
    const stats: BotStats = {
      bankroll: 1200,
      total_trades: 4,
      winning_trades: 1,
      win_rate: 0.25,
      total_pnl: -150,
      is_running: true,
      last_run: null
    }

    const { container } = render(<StatsCards stats={stats} />)

    expect(screen.getByText('$1.2K')).toBeTruthy()
    expect(screen.getByText((_, element) => element?.textContent === '$150')).toBeTruthy()
    expect(screen.getByText((_, element) => element?.textContent === '-11.1%')).toBeTruthy()
    expect(screen.getByText('25%')).toBeTruthy()
    expect(screen.getByText('1/4')).toBeTruthy()
    expect(container.querySelector('.live-dot')).toBeTruthy()
  })

  it('formats mid-sized bankrolls without scientific notation and keeps blank returns neutral', () => {
    const stats: BotStats = {
      bankroll: 999,
      total_trades: 3,
      winning_trades: 2,
      win_rate: 0.6667,
      total_pnl: 0,
      is_running: false,
      last_run: null
    }

    render(<StatsCards stats={stats} />)

    expect(screen.getByText('$999')).toBeTruthy()
    expect(screen.getByText('+0.0%')).toBeTruthy()
    expect(screen.getByText('67%')).toBeTruthy()
    expect(screen.getByText('2/3')).toBeTruthy()
  })
})