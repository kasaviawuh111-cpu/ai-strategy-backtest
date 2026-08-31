import { DEFAULT_INSTRUMENT, resolveInstrumentContext } from './instrument-context'

describe('stock-page instrument context', () => {
  it('keeps the standalone default only when the host provides no stock', () => {
    expect(resolveInstrumentContext('')).toEqual({
      instrument: DEFAULT_INSTRUMENT,
      source: 'standalone_default',
    })
  })

  it.each([
    ['?symbol=600519.SH&name=贵州茅台', '600519.SH', 'SSE'],
    ['?instrument_context=000001.SZ&instrument_name=平安银行', '000001.SZ', 'SZSE'],
    ['?symbol=920001.BJ&name=北交所样例', '920001.BJ', 'BSE'],
  ])('accepts an arbitrary A-share page context: %s', (search, symbol, exchange) => {
    expect(resolveInstrumentContext(search)).toMatchObject({
      source: 'stock_page',
      instrument: { symbol, name: symbol, exchange, market: 'CN_A' },
    })
  })

  it('never trusts a URL display name as a security-master identity', () => {
    expect(resolveInstrumentContext('?symbol=600519.SH&name=东方财富')).toMatchObject({
      instrument: { symbol: '600519.SH', name: '600519.SH' },
    })
  })

  it.each([
    '?symbol=AAPL.US&name=Apple',
    '?symbol=300059.SH&name=错配交易所',
    '?symbol=399001.SZ&name=指数',
    '?symbol=900901.SH&name=B股',
    '?symbol=300059.SZ&market=US',
  ])('fails closed instead of switching to another stock: %s', (search) => {
    const outcome = resolveInstrumentContext(search)
    expect(outcome.source).toBe('stock_page')
    expect(outcome.error).toMatch(/有效的 A 股代码/)
  })
})
