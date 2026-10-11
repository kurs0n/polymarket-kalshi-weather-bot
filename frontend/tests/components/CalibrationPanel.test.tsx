import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { CalibrationPanel } from '../../src/components/CalibrationPanel'
import type { CalibrationSummary } from '../../src/types'

describe('CalibrationPanel', () => {
  it('renders score breakdown and compares predicted vs actual edges', () => {
    const calibration: CalibrationSummary = {
      total_signals: 40,
      total_with_outcome: 25,
      accuracy: 0.64,
      avg_predicted_edge: 0.12,
      avg_actual_edge: 0.08,
      brier_score: 0.18
    }

    render(<CalibrationPanel calibration={calibration} />)

    expect(screen.getByText('64%')).toBeTruthy()
    expect(screen.getByText('Accuracy')).toBeTruthy()
    expect(screen.getByText('16/25')).toBeTruthy()
    expect(screen.getByText('0.180 (Good)')).toBeTruthy()
    expect(screen.getByText('Pred')).toBeTruthy()
    expect(screen.getByText('Actual')).toBeTruthy()
    expect(screen.getByText('12.0%')).toBeTruthy()
    expect(screen.getByText('8.0%')).toBeTruthy()
    expect(screen.getByText('40 tracked / 25 settled')).toBeTruthy()
  })
})
