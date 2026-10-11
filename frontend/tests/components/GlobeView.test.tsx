import { render, screen } from '@testing-library/react'
import { forwardRef, useImperativeHandle } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { GlobeView } from '../../src/components/GlobeView'
import type { WeatherForecast, WeatherSignal } from '../../src/types'

vi.mock('react-globe.gl', () => ({
  default: forwardRef<object, {
    htmlElementsData: object[]
    htmlElement: (marker: object) => HTMLElement
  }>(({ htmlElementsData, htmlElement }, ref) => {
    useImperativeHandle(ref, () => ({
      pointOfView: vi.fn(),
      controls: () => ({ autoRotate: false, autoRotateSpeed: 0, enableZoom: true })
    }))

    return (
      <div data-testid="globe">
        {htmlElementsData.map((marker, index) => (
          <div
            key={index}
            dangerouslySetInnerHTML={{ __html: htmlElement(marker).outerHTML }}
          />
        ))}
      </div>
    )
  })
}))

const forecast: WeatherForecast = {
  city_key: 'nyc',
  city_name: 'New York City',
  target_date: '2026-09-27',
  mean_high: 78.4,
  std_high: 2.6,
  mean_low: 61,
  std_low: 3,
  num_members: 30,
  ensemble_agreement: 0.8
}

const signals: WeatherSignal[] = [
  {
    market_id: 'NYC-HIGH-80',
    city_key: 'nyc',
    city_name: 'New York City',
    target_date: '2026-09-27',
    threshold_f: 80,
    metric: 'high',
    direction: 'above',
    model_probability: 0.72,
    market_probability: 0.6,
    edge: 0.12,
    confidence: 0.8,
    suggested_size: 25,
    reasoning: 'Forecast supports the YES outcome',
    ensemble_mean: 78,
    ensemble_std: 3,
    ensemble_members: 30,
    actionable: true,
    platform: 'kalshi'
  },
  {
    market_id: 'NYC-HIGH-75',
    city_key: 'nyc',
    city_name: 'New York City',
    target_date: '2026-09-27',
    threshold_f: 75,
    metric: 'high',
    direction: 'above',
    model_probability: 0.65,
    market_probability: 0.6,
    edge: 0.05,
    confidence: 0.7,
    suggested_size: 10,
    reasoning: 'Smaller alternative edge',
    ensemble_mean: 78,
    ensemble_std: 3,
    ensemble_members: 30,
    actionable: false,
    platform: 'kalshi'
  }
]

describe('GlobeView', () => {
  it('renders city markers with forecast temperature and strongest actionable edge', () => {
    render(<GlobeView forecasts={[forecast]} signals={signals} />)

    expect(screen.getByText('NYC')).toBeTruthy()
    expect(screen.getByText('78F')).toBeTruthy()
    expect(screen.getByText('+12.0%')).toBeTruthy()
    expect(screen.getAllByText(/^(CHI|MIA|LA|DEN|BOS)$/)).toHaveLength(5)

    const marker = screen.getByText('NYC').closest('.city-marker')
    expect(marker?.querySelector('.marker-dot')?.getAttribute('style')).toContain('rgb(34, 197, 94)')
  })

  it('still renders all supported city markers without forecast data', () => {
    render(<GlobeView forecasts={[]} signals={[]} />)

    expect(screen.getByTestId('globe').querySelectorAll('.city-marker')).toHaveLength(6)
    expect(screen.getByText('NYC')).toBeTruthy()
    expect(screen.queryByText('78F')).toBeNull()
  })
})
