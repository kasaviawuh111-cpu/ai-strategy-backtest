import type { Instrument } from './api/types'

export const DEFAULT_INSTRUMENT: Instrument = {
  name: '东方财富',
  symbol: '300059.SZ',
  market: 'CN_A',
  exchange: 'SZSE',
}

export type InstrumentContextResolution = {
  instrument: Instrument
  source: 'stock_page' | 'standalone_default'
  error?: string
}

const expectedExchange = (code: string): { suffix: 'SH' | 'SZ' | 'BJ'; exchange: Instrument['exchange'] } | null => {
  if (/^(?:600|601|603|605|688|689)\d{3}$/.test(code)) return { suffix: 'SH', exchange: 'SSE' }
  if (/^(?:000|001|002|003|300|301)\d{3}$/.test(code)) return { suffix: 'SZ', exchange: 'SZSE' }
  if (/^(?:[48]\d{5}|920\d{3})$/.test(code)) return { suffix: 'BJ', exchange: 'BSE' }
  return null
}

/**
 * Turn a provider-grounded A-share code into the exact host instrument shape.
 * This accepts either a bare six-digit code or a code with an exchange suffix,
 * but never guesses across exchanges or accepts fund/index code ranges.
 */
export const toAshareInstrument = (
  rawSymbol: string,
  rawName?: string | null,
): Instrument | null => {
  const normalized = rawSymbol.trim().toUpperCase()
  const match = /^(\d{6})(?:\.(SH|SZ|BJ))?$/.exec(normalized)
  if (!match) return null
  const code = match[1] ?? ''
  const expected = expectedExchange(code)
  if (!expected || (match[2] && match[2] !== expected.suffix)) return null
  const symbol = `${code}.${expected.suffix}`
  return {
    symbol,
    name: rawName?.trim() || symbol,
    market: 'CN_A',
    exchange: expected.exchange,
  }
}

export const resolveInstrumentContext = (search: string): InstrumentContextResolution => {
  const params = new URLSearchParams(search)
  const rawSymbol = params.get('instrument_context') ?? params.get('symbol')
  if (!rawSymbol) {
    return { instrument: DEFAULT_INSTRUMENT, source: 'standalone_default' }
  }

  const symbol = rawSymbol.trim().toUpperCase()
  const match = /^(\d{6})\.(SH|SZ|BJ)$/.exec(symbol)
  const expected = match ? expectedExchange(match[1] ?? '') : null
  const requestedMarket = params.get('market')?.trim().toUpperCase()
  if (!match || !expected || match[2] !== expected.suffix || (requestedMarket && requestedMarket !== 'CN_A')) {
    return {
      instrument: DEFAULT_INSTRUMENT,
      source: 'stock_page',
      error: '股票页没有提供有效的 A 股代码，请使用 6 位沪、深、北交所代码。',
    }
  }

  // A URL label is not security/master-data evidence.  Until the host passes a
  // signed security-master identity, use the canonical symbol as the display
  // identity so `600519.SH&name=东方财富` cannot mislabel or misdirect a run.
  return {
    instrument: {
      name: symbol,
      symbol,
      market: 'CN_A',
      exchange: expected.exchange,
    },
    source: 'stock_page',
  }
}
