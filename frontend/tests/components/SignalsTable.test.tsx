import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { WeatherSignal } from '../../src/types'
import { SignalsTable } from '../../src/components/SignalsTable'

const signals: WeatherSignal[] = [
  {
    market_id: 'AUSTIN-HIGH-80',
    city_key: 'austin',
    city_name: 'Austin',
    target_date: '2026-09-27',
    threshold_f: 80,
    metric: 'high',
    direction: 'above',
    model_probability: 0.8,
    market_probability: 0.6,
    edge: 0.2,
    confidence: 0.9,
    suggested_size: 25,
    reasoning: 'Ensemble favors a warm day',
    ensemble_mean: 84,
    ensemble_std: 3,
    ensemble_members: 30,
    actionable: true,
    platform: 'kalshi'
  },
  {
    market_id: 'DENVER-HIGH-65',
    city_key: 'denver',
    city_name: 'Denver',
    target_date: '2026-09-27',
    threshold_f: 65,
    metric: 'high',
    direction: 'below',
    model_probability: 0.55,
    market_probability: 0.5,
    edge: 0.05,
    confidence: 0.7,
    suggested_size: 10,
    reasoning: 'Forecast is close to the threshold',
    ensemble_mean: 64,
    ensemble_std: 4,
    ensemble_members: 30,
    actionable: true,
    platform: 'kalshi'
  }
]

describe('SignalsTable', () => {
  it('renders an empty state when there are no weather signals', () => {
    render(<SignalsTable weatherSignals={[]} onSimulateTrade={vi.fn()} isSimulating={false} />)

    expect(screen.getByText('No weather signals')).toBeTruthy()
  })

  it('sorts by edge and sends the selected signal to the trade callback', async () => {
    const user = userEvent.setup()
    const onSimulateTrade = vi.fn()
    render(<SignalsTable weatherSignals={signals} onSimulateTrade={onSimulateTrade} isSimulating={false} />)

    let rows = screen.getAllByRole('row')
    expect(rows[1].textContent).toContain('Austin high above 80F')
    expect(rows[2].textContent).toContain('Denver high below 65F')

    await user.click(screen.getByText('Edge'))
    rows = screen.getAllByRole('row')
    expect(rows[1].textContent).toContain('Denver high below 65F')
    expect(rows[2].textContent).toContain('Austin high above 80F')

    const denverRow = rows[1]
    await user.click(within(denverRow).getByRole('button', { name: 'Trade' }))
    expect(onSimulateTrade).toHaveBeenCalledWith('DENVER-HIGH-65')
  })

  it('disables trade actions while a simulation is in progress', () => {
    render(<SignalsTable weatherSignals={signals} onSimulateTrade={vi.fn()} isSimulating />)

    for (const button of screen.getAllByRole('button', { name: 'Trade' })) {
      expect(button.hasAttribute('disabled')).toBe(true)
    }
  })
})