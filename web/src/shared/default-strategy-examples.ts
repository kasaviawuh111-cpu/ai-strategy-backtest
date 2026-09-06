import type { Instrument } from './api/types'

// Show and submit the same complete rule; dates and capital use normal settings.
export const DEFAULT_STRATEGY_EXAMPLES: ReadonlyArray<{
  utterance: string
  instrument: Instrument
}> = [
  {
    utterance: '贵州茅台，收盘价创前20日新高且成交量超过前20日均量1.5倍买入；从持仓后最高收盘价回撤8%或持有满40个交易日卖出。',
    instrument: { name: '贵州茅台', symbol: '600519.SH', market: 'CN_A', exchange: 'SSE' },
  },
  {
    utterance: '美的集团，14日RSI从30下方上穿30且当日成交额超过5亿元买入；14日RSI高于55、持仓亏损5%或持有满10个交易日卖出。',
    instrument: { name: '美的集团', symbol: '000333.SZ', market: 'CN_A', exchange: 'SZSE' },
  },
]
