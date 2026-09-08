import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Pagination, pageWindow } from './Pagination'

describe('pageWindow', () => {
  it('keeps the window inside the available pages', () => {
    expect(pageWindow(1, 3)).toEqual([1, 2, 3])
    expect(pageWindow(1, 20)).toEqual([1, 2, 3, 4, 5])
    expect(pageWindow(20, 20)).toEqual([16, 17, 18, 19, 20])
    expect(pageWindow(10, 20)).toEqual([8, 9, 10, 11, 12])
    expect(pageWindow(1, 0)).toEqual([])
  })
})

describe('Pagination', () => {
  it('derives page count from the document-level total', () => {
    render(<Pagination page={1} size={20} total={37} onChange={() => {}} />)
    expect(screen.getByRole('button', { name: '2' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '3' })).not.toBeInTheDocument()
  })

  it('renders nothing when everything fits on one page', () => {
    const { container } = render(<Pagination page={1} size={20} total={12} onChange={() => {}} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('disables the edges and reports the current page to assistive tech', () => {
    render(<Pagination page={1} size={20} total={100} onChange={() => {}} />)
    expect(screen.getByRole('button', { name: '이전' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '다음' })).toBeEnabled()
    expect(screen.getByRole('button', { name: '1' })).toHaveAttribute('aria-current', 'page')
  })

  it('reports the requested page', async () => {
    const onChange = vi.fn()
    render(<Pagination page={2} size={20} total={100} onChange={onChange} />)
    await userEvent.click(screen.getByRole('button', { name: '3' }))
    expect(onChange).toHaveBeenCalledWith(3)
    await userEvent.click(screen.getByRole('button', { name: '이전' }))
    expect(onChange).toHaveBeenCalledWith(1)
  })
})
