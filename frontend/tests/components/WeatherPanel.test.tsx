import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { WeatherPanel } from '../../src/components/WeatherPanel'
import type { WeatherForecast, WeatherSignal } from '../../src/types'

describe('WeatherPanel', () => {
  it('renders an empty state when there is no weather data', () => {
    render(<WeatherPanel forecasts={[]} signals={[]} />)

    expect(screen.getByText('No weather data')).toBeTruthy()
  })

  it('groups signals by city and shows the strongest edge and agreement', () => {
    const forecasts: WeatherForecast[] = [
      {
        city_key: 'austin',
        city_name: 'Austin',
        target_date: '2026-09-27',
        mean_high: 82,
        std_high: 6,
        mean_low: 68,
        std_low: 5,
        num_members: 30,
        ensemble_agreement: 0.81
      }
    ]

    const signals: WeatherSignal[] = [
      {
        market_id: 'AUSTIN-HIGH-82',
        city_key: 'austin',
        city_name: 'Austin',
        target_date: '2026-09-27',
        threshold_f: 82,
        metric: 'high',
        direction: 'above',
        model_probability: 0.72,
        market_probability: 0.58,
        edge: 0.14,
        confidence: 0.82,
        suggested_size: 18,
        reasoning: 'Warm pattern remains elevated',
        ensemble_mean: 82,
        ensemble_std: 5,
        ensemble_members: 30,
        actionable: true,
        platform: 'kalshi'
      }
    ]

    render(<WeatherPanel forecasts={forecasts} signals={signals} />)

    expect(screen.getByText('Austin')).toBeTruthy()
    expect(screen.getByText('82F')).toBeTruthy()
    expect(screen.getByText('81%')).toBeTruthy()
    expect(screen.getByText('+14.0%')).toBeTruthy()
    expect(document.querySelector('.platform-badge')).toBeTruthy()
  })
})
