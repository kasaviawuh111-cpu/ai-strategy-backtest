import { render, screen } from '@testing-library/react'
import { ExcessEquation } from './ExcessEquation'

describe('ExcessEquation', () => {
  it('shows strategy minus benchmark equals percentage point difference in puzzle order', () => {
    const { container } = render(<ExcessEquation total={16.14} bench={110.23} excess={-94.09} />)
    expect(screen.getByRole('group')).toHaveAttribute('aria-label', expect.stringContaining('-94.09 个百分点'))
    const pieces = container.querySelectorAll('.eq-piece')
    expect(pieces).toHaveLength(3)
    expect(pieces[0]).toHaveTextContent('+16.14%策略收益率−')
    expect(pieces[1]).toHaveTextContent('+110.23%买入后一直持有收益率=')
    expect(pieces[2]).toHaveTextContent('-94.09个百分点交易策略超额')
  })
  it('does not compare when the benchmark is unaudited', () => {
    render(<ExcessEquation total={16.14} bench={110.23} excess={-94.09} comparisonStatus="benchmark_unavailable" />)
    expect(screen.queryByRole('group')).toBeNull()
    expect(screen.getByText('本次没有可审计的可比基准')).toBeInTheDocument()
  })
})
