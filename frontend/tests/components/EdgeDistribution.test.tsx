import { render, screen } from '@testing-library/react'
import { beforeAll, describe, expect, it, vi } from 'vitest'
import type { WeatherSignal } from '../../src/types'

vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }: { children: any }) => <div>{children}</div>,
  BarChart: ({ data }: { data: Array<{ bucket: string }> }) => (
    <div>
      {data.map(item => (
        <span key={item.bucket}>{item.bucket}</span>
      ))}
    </div>
  ),
  CartesianGrid: () => null,
  XAxis: () => null,
  YAxis: () => null,
  Tooltip: () => null,
  Bar: () => null,
}))

import { EdgeDistribution } from '../../src/components/EdgeDistribution'

beforeAll(() => {
  class ResizeObserverMock {
    observe() {}
    unobserve() {}
    disconnect() {}
  }

  vi.stubGlobal('ResizeObserver', ResizeObserverMock)
})

const signals: WeatherSignal[] = [
  {
    market_id: 'A',
    city_key: 'austin',
    city_name: 'Austin',
    target_date: '2026-09-27',
    threshold_f: 80,
    metric: 'high',
    direction: 'above',
    model_probability: 0.7,
    market_probability: 0.55,
    edge: 0.15,
    confidence: 0.8,
    suggested_size: 20,
    reasoning: 'Warm',
    ensemble_mean: 82,
    ensemble_std: 4,
    ensemble_members: 30,
    actionable: true,
    platform: 'kalshi'
  },
  {
    market_id: 'B',
    city_key: 'denver',
    city_name: 'Denver',
    target_date: '2026-09-27',
    threshold_f: 65,
    metric: 'low',
    direction: 'below',
    model_probability: 0.72,
    market_probability: 0.6,
    edge: 0.12,
    confidence: 0.75,
    suggested_size: 18,
    reasoning: 'Cold',
    ensemble_mean: 60,
    ensemble_std: 3,
    ensemble_members: 30,
    actionable: true,
    platform: 'polymarket'
  },
  {
    market_id: 'C',
    city_key: 'phoenix',
    city_name: 'Phoenix',
    target_date: '2026-09-27',
    threshold_f: 95,
    metric: 'high',
    direction: 'above',
    model_probability: 0.82,
    market_probability: 0.7,
    edge: 0.03,
    confidence: 0.8,
    suggested_size: 12,
    reasoning: 'Hot',
    ensemble_mean: 94,
    ensemble_std: 2,
    ensemble_members: 30,
    actionable: true,
    platform: 'kalshi'
  }
]

describe('EdgeDistribution', () => {
  it('renders a placeholder when there are no signals', () => {
    render(<EdgeDistribution weatherSignals={[]} />)

    expect(screen.getByText('No signals for distribution')).toBeTruthy()
  })

  it('groups edges into the expected buckets for the chart', () => {
    render(<EdgeDistribution weatherSignals={signals} />)

    expect(screen.getByText('10-20%')).toBeTruthy()
    expect(screen.getByText('2-5%')).toBeTruthy()
    expect(screen.queryByText('No signals for distribution')).toBeNull()
  })
})
