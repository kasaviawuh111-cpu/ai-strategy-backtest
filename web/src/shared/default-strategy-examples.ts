import type { Instrument } from './api/types'

export type StrategyExample = {
  category: string
  utterance: string
  instrument?: Instrument
}

// Selection fills the exact displayed sentence; only Send starts a request.
export const DEFAULT_STRATEGY_EXAMPLES: readonly StrategyExample[] = [
  {
    category: '历史案例',
    utterance: '平安银行，KDJ金叉且RSI低于50买入，KDJ死叉卖出。',
    instrument: { name: '平安银行', symbol: '000001.SZ', market: 'CN_A', exchange: 'SZSE' },
  },
  {
    category: '随口一说',
    utterance: '我讨厌特朗普',
  },
  {
    category: '交易想法',
    utterance: '东方财富低买高卖',
    instrument: { name: '东方财富', symbol: '300059.SZ', market: 'CN_A', exchange: 'SZSE' },
  },
  {
    category: '历史案例',
    utterance: '东方财富，RSI上穿30买入，下穿55卖出。',
    instrument: { name: '东方财富', symbol: '300059.SZ', market: 'CN_A', exchange: 'SZSE' },
  },
  {
    category: '随口一说',
    utterance: '我是秦始皇',
  },
  {
    category: '交易想法',
    utterance: '同花顺网格策略',
    instrument: { name: '同花顺', symbol: '300033.SZ', market: 'CN_A', exchange: 'SZSE' },
  },
]
