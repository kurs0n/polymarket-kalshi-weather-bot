import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { Terminal } from '../../src/components/Terminal'

describe('Terminal', () => {
  it('shows the idle state and invokes start and scan callbacks', async () => {
    const user = userEvent.setup()
    const onStart = vi.fn()
    const onStop = vi.fn()
    const onScan = vi.fn()

    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({
      ok: true,
      json: async () => []
    })))

    class MockWebSocket {
      static instances: MockWebSocket[] = []
      onopen: (() => void) | null = null
      onmessage: ((event: { data: string }) => void) | null = null
      onclose: (() => void) | null = null
      onerror: (() => void) | null = null
      readyState = 1

      constructor(public url: string) {
        MockWebSocket.instances.push(this)
        setTimeout(() => this.onopen?.(), 0)
      }

      close() {
        this.onclose?.()
      }
    }

    vi.stubGlobal('WebSocket', MockWebSocket as any)

    render(
      <Terminal
        isRunning={false}
        lastRun={null}
        stats={{ total_trades: 3, total_pnl: 12 }}
        onStart={onStart}
        onStop={onStop}
        onScan={onScan}
      />
    )

    expect(screen.getByText('IDLE')).toBeTruthy()
    expect(screen.getByText('No scans')).toBeTruthy()

    await user.click(screen.getByRole('button', { name: /start/i }))
    await user.click(screen.getByRole('button', { name: /scan/i }))

    expect(onStart).toHaveBeenCalledTimes(1)
    expect(onStop).not.toHaveBeenCalled()
    expect(onScan).toHaveBeenCalledTimes(1)
  })
})
