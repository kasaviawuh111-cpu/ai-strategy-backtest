import { describe, expect, it } from 'vitest'
import { gridProposalPresentation, gridComparisonTitles, gridReviewPresentation, priceRuleLabel, pricePlanTitle, pricePlanSides } from './price-plan'

it('相对价格范围展示真实基准计算结果而非占位边界', () => {
  const plan = { kind: 'grid' as const, parameters: { anchor_price: 49.8, range_percent: 20, lower_price: 0.01, upper_price: 1000000 } }
  const before = structuredClone(plan)
  const range = (p: typeof plan) => gridProposalPresentation(p).gridFacts.find(f => f.label === '价格区间')?.value
  expect(range(plan)).toBe('39.84–59.76元（基准上下20%）')
  expect(range({ ...plan, parameters: { ...plan.parameters, lower_price: 45 } })).toBe('45–59.76元（基准上下20%）')
  expect(plan).toEqual(before)
})

it('网格名称区分间距单位、买卖差异和编辑后的每格数量', () => {
  const plan = { kind: 'grid' as const, parameters: { spacing_mode: 'cny', spacing: 1, order_shares: 100 } }
  expect(pricePlanTitle(plan)).toBe('网格·1元·100股/格')
  expect(pricePlanTitle({ ...plan, parameters: { ...plan.parameters, order_shares: 200 } })).toBe('网格·1元·200股/格')
  expect(pricePlanTitle({ ...plan, parameters: { spacing_mode: 'anchor_percent', spacing: 1 } })).toBe('网格·每格基准价的1%')
  expect(pricePlanTitle({ ...plan, parameters: { spacing_mode: 'percent', spacing: 2 } })).toBe('网格·跌约1.96%买，涨2%卖')
  expect(pricePlanTitle({ ...plan, parameters: { buy_spacing: 1, sell_spacing: 2 } })).toBe('网格·买1元/卖2元')
  expect(plan.parameters.order_shares).toBe(100)
})

it('期初已有持仓不能显示为空仓，新增建仓不等于当日可卖', () => {
  const plan = { kind: 'grid' as const, parameters: { opening_shares: 500, initial_shares: 0 } }
  const held = gridReviewPresentation(plan).initialization
  expect(held).toContain('期初已有可卖持仓500股')
  expect(held).not.toContain('空仓启动')
  const adding = gridReviewPresentation({ ...plan, parameters: { ...plan.parameters, initial_shares: 200 } }).initialization
  expect(adding).toContain('提交买入200股')
  expect(adding).toContain('休眠且不自动恢复')
  expect(plan.parameters.initial_shares).toBe(0)
})

describe('候选效果按实际双向间距比较', () => {
  const grid = (spacing_mode: string, spacing: number, anchor_price?: number) => ({
    kind: 'grid' as const, parameters: { spacing_mode, spacing, anchor_price: anchor_price ?? null },
  })
  it('同花顺1元、1%、2%按统一基准比较，保留原始参数', () => {
    const plans = [grid('cny', 1, 365.11), grid('anchor_percent', 1, 365.11), grid('percent', 2, 365.11)]
    const before = structuredClone(plans)
    expect(gridComparisonTitles(plans)).toEqual(['较小波动就触发', '等中等波动再触发', '等更大波动再触发'])
    expect(plans).toEqual(before)
  })
  it('混合单位缺基准、相同间距不虚构效果排名', () => {
    expect(gridComparisonTitles([grid('cny', 1), grid('percent', 2)])).toEqual([undefined, undefined])
    expect(gridComparisonTitles([grid('cny', 1), grid('cny', 1)])).toEqual([undefined, undefined])
  })
})

describe('网格审阅参数保持原义', () => {
  it('起始日昨收直接展示价格和来源日期，缺失时不展示假价格', () => {
    expect(gridReviewPresentation({ kind: 'grid', parameters: {
      anchor_mode: 'previous_close', anchor_price: 18.31, anchor_quote_time_label: '2026-09-11',
    } }).anchor).toBe('起始日昨收 18.31元（2026-09-11）')
    expect(gridReviewPresentation({ kind: 'grid', parameters: {
      anchor_mode: 'previous_close', anchor_price: null,
    } }).anchor).toBe('回测起始日昨收价（待获取）')
  })
  it('保留双向不同间距、按金额委托及零底仓，不改写原参数', () => {
    const plan = { kind: 'grid' as const, parameters: {
      anchor_mode: 'latest_price', anchor_price: 18.31,
      buy_spacing_mode: 'anchor_percent', buy_spacing: 3,
      sell_spacing_mode: 'cny', sell_spacing: 1,
      sizing_mode: 'amount', order_amount_cny: 20000, order_shares: 100,
      max_shares: 10000, min_shares: 0,
    } }
    const original = structuredClone(plan)
    expect(gridReviewPresentation(plan)).toEqual({
      anchor: '行情最新价 18.31元',
      initialization: '空仓启动，等待买点；不提前建仓。触发卖出时若可卖股份不足（含当天新买入），卖出侧将休眠且不自动恢复；买入侧仍按规则运行。',
      buy: [
        { label: '买入间距', value: '基准价的3%' },
        { label: '每下跨一格', value: '买入20000元' },
        { label: '最多持有', value: '10000股' },
      ],
      sell: [
        { label: '卖出间距', value: '1元' },
        { label: '每上跨一格', value: '卖出20000元' },
        { label: '至少保留', value: '0股' },
      ],
    })
    expect(plan).toEqual(original)
  })
  it('最新价缺失不填入假价格，等比间距不冒充基准百分比', () => {
    const result = gridReviewPresentation({ kind: 'grid', parameters: {
      anchor_mode: 'latest_price', spacing_mode: 'percent', spacing: 5,
      order_shares: 200, max_shares: 2000, min_shares: 200,
    } })
    expect(result.anchor).toBe('行情最新价（待获取）')
    expect(result.buy[0]).toEqual({ label: '买入间距', value: '约4.76%' })
    expect(result.sell[0]).toEqual({ label: '卖出间距', value: '5%' })
    expect(result.buy[1]).toEqual({ label: '每下跨一格', value: '买入200股' })
    expect(result.sell[2]).toEqual({ label: '至少保留', value: '200股' })
  })
  it('历史起始价与手动基准价保持区别', () => {
    expect(gridReviewPresentation({ kind: 'grid', parameters: {
      anchor_mode: 'first_open', anchor_price: 85,
    } }).anchor).toBe('回测起始开盘价')
    expect(gridReviewPresentation({ kind: 'grid', parameters: {
      anchor_mode: 'manual', anchor_price: 85,
    } }).anchor).toBe('85元')
  })
})

it('条件计划按连续互斥组和阶段展示，不把止盈止损写为同时满足', () => {
  const sides = pricePlanSides({ kind: 'conditional', parameters: { rules: [
    { kind: 'rebound', side: 'buy', gap: 2, quantity: 100 },
    { kind: 'take_profit', side: 'sell', gap: 5, quantity: 100, group: 'protect' },
    { kind: 'stop_loss', side: 'sell', gap: 3, quantity: 100, group: 'protect' },
    { kind: 'rebound', side: 'buy', gap: 1, quantity: 100 },
  ] } })
  expect(sides.entry).toHaveLength(2)
  expect(sides.entry[1]).toContain('阶段3')
  expect(sides.exit).toHaveLength(1)
  expect(sides.exit[0]).toContain('阶段2：任一先触发（')
  expect(sides.exit[0]).toContain(' 或 ')
  expect(sides.exit[0]).not.toContain('protect')
})

it('条件计划首日真实建仓显示在买入侧，不被只有卖出的阶段数组掩盖', () => {
  const plan = { kind: 'conditional' as const, parameters: { initial_shares: 10000, rules: [
    { kind: 'take_profit', side: 'sell', gap: 20, gap_unit: 'percent', quantity: 1000 },
  ] } }
  const before = structuredClone(plan)
  const sides = pricePlanSides(plan)
  expect(sides.entry).toEqual(['建仓：区间首个交易日开盘提交买入10000股，当日有效；成交后形成持仓，次日可卖'])
  expect(sides.exit).toEqual(['卖出条件：较持仓成交均价上涨20%，卖出1000股'])
  expect(plan).toEqual(before)
})

it('已有底仓与未设置买入都不伪装成首日买入', () => {
  const rules = [{ kind: 'take_profit', side: 'sell', gap: 20, gap_unit: 'percent', quantity: 100 }]
  expect(pricePlanSides({ kind: 'conditional', parameters: { opening_shares: 1000, rules } }).entry)
    .toEqual(['期初已有可卖持仓1000股；不新增买入'])
  expect(pricePlanSides({ kind: 'conditional', parameters: { rules } }).entry).toEqual([])
})

it('单次买入后的退出不承诺继续定投', () => {
  const sides = pricePlanSides({ kind: 'scheduled', parameters: { frequency: 'once', side: 'buy',
    sizing_mode: 'amount', budget_cny: 10000, at: 'open', exit_rules: [
      { kind: 'take_profit', side: 'sell', gap: 20, gap_unit: 'percent', sizing_mode: 'all_position' },
    ] } })
  expect(sides.entry[0]).toContain('区间首个交易日')
  expect(sides.exit[0]).not.toContain('定投')
})

describe('价格计划标题', () => {
  it('按实际频率与方向区分计划，不把卖出叫定投', () => {
    expect(pricePlanTitle({ kind: 'scheduled', parameters: { frequency: 'monthly', side: 'buy' } })).toBe('每月计划买入')
    expect(pricePlanTitle({ kind: 'scheduled', parameters: { frequency: 'weekly', side: 'sell' } })).toBe('每周计划卖出')
    expect(pricePlanTitle({ kind: 'scheduled', parameters: { frequency: 'once', side: 'buy' } })).toBe('单次计划买入')
  })
  it('反弹、回落及持仓保护都保留，不凭价差推断做T顺序', () => {
    expect(pricePlanTitle({ kind: 'conditional', parameters: { rules: [
      { kind: 'rebound', side: 'buy' }, { kind: 'pullback', side: 'sell' }, { kind: 'stop_loss', side: 'sell' },
    ] } })).toBe('反弹买入 / 回落卖出 / 止损')
    expect(pricePlanTitle({ kind: 'conditional', parameters: { rules: [
      { kind: 'relative_price', side: 'sell' }, { kind: 'relative_price', side: 'buy' },
    ] } })).toBe('价差条件卖出 / 价差条件买入')
  })
})

describe('relative price rule reference', () => {
  const rule = { kind: 'relative_price', direction: 'up', gap: 2, gap_unit: 'percent', side: 'sell', quantity: 200 }
  it('uses the explicitly selected first observation reference', () => {
    expect(priceRuleLabel({ ...rule, reference_mode: 'first_observation' })).toBe('较首次观察价上涨2%，卖出200股')
  })
  it('preserves the engine default and explicit previous-fill reference', () => {
    for (const reference of [undefined, 'previous_fill']) {
      expect(priceRuleLabel({ ...rule, ...(reference ? { reference_mode: reference } : {}) })).toBe('较上一阶段成交均价上涨2%，卖出200股')
    }
  })
})
