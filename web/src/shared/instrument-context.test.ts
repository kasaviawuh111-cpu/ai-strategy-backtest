import { DEFAULT_INSTRUMENT, resolveInstrumentContext, toAshareInstrument } from './instrument-context'

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
    ['?symbol=302132.SZ&name=中航成飞', '302132.SZ', 'SZSE'],
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

  it('normalizes a provider-grounded bare code for an explicit analysis override', () => {
    expect(toAshareInstrument('300033', '同花顺')).toEqual({
      symbol: '300033.SZ',
      name: '同花顺',
      market: 'CN_A',
      exchange: 'SZSE',
    })
  })

  it.each(['302132', '302132.SZ'])('accepts the exact replacement code: %s', (symbol) => {
    expect(toAshareInstrument(symbol, '中航成飞')).toEqual({
      symbol: '302132.SZ',
      name: '中航成飞',
      market: 'CN_A',
      exchange: 'SZSE',
    })
  })

  it.each([
    '300059.SH', '399001.SZ', '510300.SH', 'AAPL.US',
    '302131', '302133', '302131.SZ', '302133.SZ', '302132.SH', '302132.BJ',
  ])(
    'rejects a mismatched or non-stock analysis override: %s',
    (symbol) => expect(toAshareInstrument(symbol)).toBeNull(),
  )

  it.each([
    '?symbol=AAPL.US&name=Apple',
    '?symbol=300059.SH&name=错配交易所',
    '?symbol=399001.SZ&name=指数',
    '?symbol=900901.SH&name=B股',
    '?symbol=300059.SZ&market=US',
    '?symbol=302131.SZ',
    '?symbol=302133.SZ',
    '?symbol=302132.SH',
    '?symbol=302132.BJ',
  ])('fails closed instead of switching to another stock: %s', (search) => {
    const outcome = resolveInstrumentContext(search)
    expect(outcome.source).toBe('stock_page')
    expect(outcome.error).toMatch(/有效的 A 股代码/)
  })
})
