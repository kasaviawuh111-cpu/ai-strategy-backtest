import type { Instrument } from './api/types'
import type { StrategyExample } from './default-strategy-examples'

export type CuratedStrategy = {
  id: number
  name: string
  category: string
  kind: string
  buy: string
  sell: string
  params: readonly string[]
  note: string
  steps: readonly string[]
  holding?: boolean
  visible?: boolean
}

// User-supplied templates, not an executable-capability whitelist or a ranking.
export const CURATED_STRATEGIES: readonly CuratedStrategy[] = [
  {
    "id": 1,
    "name": "趋势回踩",
    "category": "趋势跟随",
    "kind": "日线条件",
    "buy": "20日均线高于60日均线，当天下跌但收盘仍在20日线上方",
    "sell": "跌破20日线，或持有10个交易日",
    "params": [
      "趋势均线 20 / 60 日",
      "最长持有 10 个交易日"
    ],
    "note": "“下跌”与“收盘仍在20日线上方”需要同时满足；卖出条件满足任一项即可。",
    "steps": [
      "确认均线趋势",
      "等待下跌回踩",
      "触发退出条件"
    ]
  },
  {
    "id": 2,
    "name": "均线跟随",
    "category": "趋势跟随",
    "kind": "日线条件",
    "buy": "5日均线上穿20日均线",
    "sell": "5日均线下穿20日均线",
    "params": [
      "短均线 5 日",
      "长均线 20 日"
    ],
    "note": "这里要求发生上穿或下穿，不是只判断一条均线在另一条上方或下方。",
    "steps": [
      "5日线上穿20日线",
      "买入",
      "5日线下穿20日线后卖出"
    ]
  },
  {
    "id": 3,
    "name": "MACD转强",
    "category": "趋势跟随",
    "kind": "日线条件",
    "buy": "MACD（12、26、9）金叉",
    "sell": "MACD死叉",
    "params": [
      "快线参数 12",
      "慢线参数 26",
      "信号参数 9"
    ],
    "note": "沿用所给 MACD 参数与金叉、死叉条件，不添加其他过滤规则。",
    "steps": [
      "MACD金叉",
      "买入",
      "MACD死叉后卖出"
    ]
  },
  {
    "id": 4,
    "name": "RSI超卖修复",
    "category": "超卖修复",
    "kind": "日线条件",
    "buy": "RSI（14）从下方重新上穿30",
    "sell": "RSI高于70，或持有10个交易日",
    "params": [
      "RSI 周期 14",
      "买入上穿 30",
      "卖出高于 70",
      "最长持有 10 个交易日"
    ],
    "note": "买点是从下方重新上穿30，不是RSI低于30时就买；两个卖出条件满足任一项即可。",
    "steps": [
      "RSI从下方上穿30",
      "买入",
      "RSI高于70或持满10日"
    ]
  },
  {
    "id": 5,
    "name": "布林下轨修复",
    "category": "超卖修复",
    "kind": "日线条件",
    "buy": "收盘重新上穿布林下轨，参数20日、2倍标准差",
    "sell": "收盘上穿中轨，或持有10个交易日",
    "params": [
      "布林周期 20 日",
      "标准差倍数 2",
      "最长持有 10 个交易日"
    ],
    "note": "买卖均保留“收盘上穿”的含义，不替换成盘中触碰轨道。",
    "steps": [
      "收盘重新上穿下轨",
      "买入",
      "收盘上穿中轨或持满10日"
    ]
  },
  {
    "id": 6,
    "name": "KDJ＋RSI组合",
    "category": "超卖修复",
    "kind": "日线条件",
    "buy": "KDJ（9、3、3）金叉，同时RSI（14）低于50",
    "sell": "KDJ死叉",
    "params": [
      "KDJ 参数 9 / 3 / 3",
      "RSI 周期 14",
      "RSI 买入阈值 50"
    ],
    "note": "KDJ金叉和RSI低于50需要同时满足；卖出仅按KDJ死叉判断。",
    "steps": [
      "KDJ金叉且RSI低于50",
      "买入",
      "KDJ死叉后卖出"
    ]
  },
  {
    "id": 7,
    "name": "区间突破",
    "category": "区间突破",
    "kind": "日线条件",
    "buy": "收盘突破此前20个交易日最高价，窗口不含当天",
    "sell": "跌破此前10个交易日最低价，窗口不含当天",
    "params": [
      "买入窗口 20 个交易日",
      "卖出窗口 10 个交易日",
      "窗口不含当天"
    ],
    "note": "计算此前最高价和最低价时都排除当天，不将当天的数据放回比较窗口。",
    "steps": [
      "突破此前20日最高价",
      "买入",
      "跌破此前10日最低价后卖出"
    ]
  },
  {
    "id": 8,
    "name": "网格交易",
    "category": "网格与价差",
    "kind": "价格条件",
    "buy": "相对当前基准，每下跌基准价的3%买100股",
    "sell": "相对当前基准，每上涨基准价的3%卖100股",
    "params": [
      "触发后按格线价更新基准",
      "买卖间距 基准价的 3%",
      "每笔 100 股"
    ],
    "note": "初始基准取回测起始日的前一交易日收盘价，触发后按格线价更新。请模型另给明确建仓建议；旧单当日保留，新条件可继续触发，休眠不自动恢复。",
    "steps": [
      "确认初始基准和建仓计划",
      "每下跌一档买入",
      "每上涨一档卖出"
    ]
  },
  {
    "id": 10,
    "name": "先买后卖",
    "category": "网格与价差",
    "kind": "价格条件",
    "buy": "相对初始参考价下跌1.5%，买100股",
    "sell": "较实际买入价上涨1.5%，卖100股",
    "params": [
      "先下跌 1.5% 买入",
      "再上涨 1.5% 卖出",
      "每笔 100 股"
    ],
    "note": "先买后卖：卖出条件以实际买入价为基准，不继续沿用初始参考价。",
    "steps": [
      "相对初始参考价下跌",
      "买入100股",
      "相对实际买入价上涨后卖出"
    ]
  },
  {
    "id": 11,
    "name": "反弹买入",
    "category": "网格与价差",
    "kind": "价格条件",
    "buy": "从回测开始跟踪低点，反弹1.5%买100股",
    "sell": "较买入价上涨8%止盈，或下跌5%止损，先触发者执行",
    "params": [
      "低点反弹 1.5%",
      "买入 100 股",
      "止盈 8%",
      "止损 5%"
    ],
    "note": "从回测开始跟踪低点；止盈与止损按先触发者执行。如果同一根K线内同时触发，需要结合可用数据判断先后。",
    "steps": [
      "从回测开始跟踪低点",
      "反弹1.5%后买入",
      "止盈或止损，先触发者执行"
    ]
  },
  {
    "id": 12,
    "name": "高位回落保护",
    "visible": false,
    "category": "持仓保护",
    "kind": "价格条件",
    "buy": "不含买入规则，针对已有持仓",
    "sell": "从回测开始跟踪高点，回落2%卖出持仓",
    "params": [
      "从回测开始跟踪高点",
      "回落 2% 卖出",
      "仅卖出规则"
    ],
    "holding": true,
    "note": "这是已有持仓的退出规则，不会自动创建买入条件。使用前需要明确要保护的标的和初始持仓。",
    "steps": [
      "已有持仓",
      "从回测开始跟踪高点",
      "从高点回落2%后卖出"
    ]
  }
]

// Temporarily withdrawn templates remain available to existing sample/history records.
export const GALLERY_STRATEGIES = CURATED_STRATEGIES.filter(strategy => strategy.visible !== false)
export const STRATEGY_CATEGORIES = ['全部', ...new Set(GALLERY_STRATEGIES.map(strategy => strategy.category))]

/** Preserve the exact rules; the normal compile/prepare path remains authoritative. */
export function curatedStrategyExample(
  strategy: CuratedStrategy, instrument: Instrument, holding: string,
): StrategyExample {
  return {
    category: strategy.category,
    instrument,
    utterance: `${instrument.name}（${instrument.symbol}），${strategy.name}。买入规则：${strategy.buy}；卖出规则：${strategy.sell}。`
      + (strategy.holding ? `初始可卖持仓${holding}股。` : '')
      + (strategy.id === 8 ? `补充执行规则：${strategy.note}` : ''),
  }
}
