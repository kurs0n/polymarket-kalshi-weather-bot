import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { FilterBar, type FilterState } from '../../src/components/FilterBar'

describe('FilterBar', () => {
  it('emits search changes while preserving the selected status', async () => {
    const user = userEvent.setup()
    const onFilterChange = vi.fn()
    function ControlledFilterBar() {
      const [filters, setFilters] = useState<FilterState>({ search: '', status: 'pending' })
      const handleFilterChange = (nextFilters: FilterState) => {
        onFilterChange(nextFilters)
        setFilters(nextFilters)
      }

      return <FilterBar filters={filters} onFilterChange={handleFilterChange} showStatus />
    }

    render(<ControlledFilterBar />)
    await user.type(screen.getByPlaceholderText('Search...'), 'Boston')

    expect(onFilterChange).toHaveBeenLastCalledWith({ search: 'Boston', status: 'pending' })
  })

  it('changes status and clears all active filters', async () => {
    const user = userEvent.setup()
    const onFilterChange = vi.fn()
    const filters: FilterState = { search: 'Boston', status: 'pending' }

    render(<FilterBar filters={filters} onFilterChange={onFilterChange} showStatus />)
    await user.selectOptions(screen.getByRole('combobox'), 'win')
    expect(onFilterChange).toHaveBeenLastCalledWith({ search: 'Boston', status: 'win' })

    await user.click(screen.getByRole('button', { name: 'Clear' }))
    expect(onFilterChange).toHaveBeenLastCalledWith({ search: '', status: 'all' })
  })
})