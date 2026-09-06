import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { Say } from './primitives'

describe('source links in model replies', () => {
  it('links web references and preserves punctuation without rendering HTML or script links', () => {
    const { container } = render(<Say>{'参考 [新闻专题](https://example.test/a)。另见（https://example.test/b?q=1&x=2），尚未核验。<script>bad()</script> [错误](javascript:bad())'}</Say>)
    const links = screen.getAllByRole('link')
    expect(links).toHaveLength(2)
    expect(screen.getByRole('link', { name: '新闻专题' })).toHaveAttribute('href', 'https://example.test/a')
    expect(links[1]).toHaveAttribute('href', 'https://example.test/b?q=1&x=2')
    expect(links[1]).toHaveAttribute('rel', 'noreferrer noopener')
    expect(container.querySelector('script')).toBeNull()
    expect(container).toHaveTextContent('，尚未核验。')
    expect(container).toHaveTextContent('[错误](javascript:bad())')
  })
})
