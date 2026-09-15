import type { PricePlan, StrategySpec } from './api/types'

// Keep edited plans consistent with the server's execution_for_price_plan contract.
export function pricePlanExecution(plan: PricePlan): StrategySpec['execution'] {
  const common = { timezone: 'Asia/Shanghai', position_policy: 'bounded_inventory', t_plus_one: true } as const
  if (plan.kind === 'scheduled') {
    const policy = plan.parameters.at === 'close' ? 'scheduled_session_close' : 'scheduled_session_open'
    if (Array.isArray(plan.parameters.exit_rules) && plan.parameters.exit_rules.length) return {
      ...common, entry_policy: policy, exit_policy: 'next_bar_order_activation', data_capability: 'minute_ohlcv',
      execution_resolution: '1m', evaluation_frequency: '1m_bar' }
    return { ...common, entry_policy: policy, exit_policy: policy, data_capability: 'daily_ohlcv',
      execution_resolution: '1d', evaluation_frequency: 'pre_session_schedule' }
  }
  if (plan.parameters.observation === 'minute_bar') return { ...common,
    entry_policy: 'next_bar_order_activation', exit_policy: 'next_bar_order_activation',
    data_capability: 'minute_ohlcv', execution_resolution: '1m', evaluation_frequency: '1m_bar' }
  if (plan.kind === 'grid' && plan.parameters.observation == null) return { ...common,
    entry_policy: 'server_selected_grid', exit_policy: 'server_selected_grid',
    data_capability: 'server_selected', execution_resolution: 'server_selected', evaluation_frequency: 'server_selected' }
  return { ...common, entry_policy: 'next_tradable_session_open', exit_policy: 'next_tradable_session_open',
    data_capability: 'daily_ohlcv', execution_resolution: '1d', evaluation_frequency: '1d_close' }
}

type Rule = Record<string, string | number | null>
export function gridAnchorLabel(plan: PricePlan) {
  const p = plan.parameters
  if (p.anchor_mode === 'previous_close') return p.anchor_price == null
    ? '回测起始日昨收价（待获取）'
    : `起始日昨收 ${p.anchor_price}元${p.anchor_quote_time_label ? `（${p.anchor_quote_time_label}）` : ''}`
  if (p.anchor_mode === 'first_open') return '回测起始开盘价'
  if (p.anchor_mode === 'latest_price') return p.anchor_price == null
    ? '行情最新价（待获取）' : `行情最新价 ${p.anchor_price}元`
  return p.anchor_price == null ? '手动基准价（待填写）' : `${p.anchor_price}元`
}
/** Relative to these candidates only; mixed units without an anchor are not comparable. */
export function gridComparisonTitles(plans: Array<PricePlan | null | undefined>): Array<string | undefined> {
  const grids = plans.filter((p): p is PricePlan => p?.kind === 'grid')
  const cnyOnly = grids.every(p => ['buy', 'sell'].every(side => gridSpacingFor(p, side as 'buy' | 'sell').mode === 'cny'))
  const gaps = plans.map(p => {
    if (p?.kind !== 'grid') return null
    const values = (['buy', 'sell'] as const).map(side => {
      const { mode, value } = gridSpacingFor(p, side)
      const n = Number(value), anchor = Number(p.parameters.anchor_price)
      if (!Number.isFinite(n) || n <= 0) return NaN
      if (mode === 'cny') return cnyOnly ? n : anchor > 0 ? n / anchor : NaN
      return mode === 'percent' && side === 'buy' ? n / (100 + n) : n / 100
    })
    return values.every(Number.isFinite) ? values : null
  })
  return gaps.map(gap => {
    if (!gap || gaps.some(g => !g) || gaps.length < 2) return undefined
    const others = gaps.filter(g => g !== gap) as number[][]
    const smaller = others.every(g => gap[0]! <= g[0]! && gap[1]! <= g[1]!)
    const larger = others.every(g => gap[0]! >= g[0]! && gap[1]! >= g[1]!)
    if (smaller && !larger) return '较小波动就触发'
    if (larger && !smaller) return '等更大波动再触发'
    if (others.some(g => gap[0]! > g[0]! && gap[1]! > g[1]!) &&
        others.some(g => gap[0]! < g[0]! && gap[1]! < g[1]!)) return '等中等波动再触发'
    return undefined
  })
}
export function gridGapLabel(plan: PricePlan, side: 'buy' | 'sell'): string {
  const { mode, value } = gridSpacingFor(plan, side)
  if (mode === 'cny') return `${value}元`
  if (mode !== 'percent') return `基准价的${value}%`
  const exact = side === 'buy' ? 100 * Number(value) / (100 + Number(value)) : Number(value)
  const rounded = Number(exact.toFixed(2))
  return `${Math.abs(exact - rounded) > 1e-8 ? '约' : ''}${rounded}%`
}

function gridDescriptiveTitle(plan: PricePlan): string {
  const buy = gridGapLabel(plan, 'buy'), sell = gridGapLabel(plan, 'sell')
  return `跌${buy}买，涨${sell}卖`
}

export function gridProposalPresentation(plan: PricePlan, strategyTitle?: string,
  legs?: Pick<StrategySpec, 'entry' | 'exit'>) {
  const p = plan.parameters
  const unresolvedRange = p.anchor_price == null && (p.range_percent != null || p.levels_below != null || p.levels_above != null)
  const rangeConstraints = [
    p.range_percent != null ? `基准上下${p.range_percent}%` : '',
    p.levels_below != null ? `下方${p.levels_below}格` : '',
    p.levels_above != null ? `上方${p.levels_above}格` : '',
  ].filter(Boolean).join('；')
  let rangeLabel = Number(p.lower_price) <= 1 && Number(p.upper_price) >= 1000000
    ? '不另设价格上下限' : `${p.lower_price}–${p.upper_price}元`
  if (rangeConstraints) {
    rangeLabel = `${rangeConstraints}（${unresolvedRange ? '价格待基准确定后计算' : '按初始基准计算'}）`
    if (!unresolvedRange && p.range_percent != null && p.levels_below == null && p.levels_above == null) {
      const anchor = Number(p.anchor_price), fraction = Number(p.range_percent) / 100
      const lower = Math.max(Number(p.lower_price), anchor * (1 - fraction))
      const upper = Math.min(Number(p.upper_price), anchor * (1 + fraction))
      if ([anchor, lower, upper].every(Number.isFinite) && anchor > 0 && lower < upper) {
        const lo = Math.floor((lower + 1e-9) * 100) / 100
        const hi = Math.ceil((upper - 1e-9) * 100) / 100
        rangeLabel = `${lo}–${hi}元（${rangeConstraints}）`
      }
    }
  }
  return {
    // Rank only comparable gaps; otherwise describe the actual directional movements.
    title: legs?.exit ? '网格买入＋独立卖出' : legs?.entry ? '独立买入＋网格卖出'
      : strategyTitle?.trim() || gridDescriptiveTitle(plan),
    independentGridSide: legs?.exit ? 'exit' as const : legs?.entry ? 'entry' as const : undefined,
    gridFacts: [
      ...(!legs?.entry ? [{ label: '下跌多少买入', value: gridGapLabel(plan, 'buy') }] : []),
      ...(!legs?.exit ? [{ label: '上涨多少卖出', value: gridGapLabel(plan, 'sell') }] : []),
      { label: '初始基准', value: gridAnchorLabel(plan) },
      { label: '基准更新', value: p.anchor_update === 'last_trigger' ? '触发后按格线价更新，不等成交' : p.anchor_update === 'last_fill' ? '跟随实际成交价' : '不随成交价重置' },
      { label: '价格区间', value: rangeLabel },
      { label: '每格委托', value: p.sizing_mode === 'amount' ? `${p.order_amount_cny}元` : `${p.order_shares}股` },
      { label: '建仓计划', value: Number(p.initial_shares) > 0 ? `首个交易日开盘提交买入${p.initial_shares}股，当日有效` : '未安排预先建仓，等待买入条件' },
      { label: '底仓 / 上限', value: `${p.min_shares} / ${p.max_shares}股` },
    ],
  }
}
export const planRules = (plan: PricePlan): Rule[] => Array.isArray(plan.parameters.rules) ? plan.parameters.rules : []

const conditionalRuleGroups = (plan: PricePlan) => {
  const rules = planRules(plan)
  const groups: Rule[][] = []
  for (let index = 0; index < rules.length;) {
    const first = rules[index]
    if (!first) break
    const members = [first]
    index++
    while (first.group && index < rules.length) {
      const next = rules[index]
      if (!next || next.group !== first.group) break
      members.push(next)
      index++
    }
    groups.push(members)
  }
  return groups
}

export const conditionalStageCount = (plan: PricePlan) =>
  plan.kind === 'conditional' ? conditionalRuleGroups(plan).length : 0
export function pricePlanTitle(plan: PricePlan): string {
  if (plan.kind === 'grid') {
    const buy = gridSpacingFor(plan, 'buy'), sell = gridSpacingFor(plan, 'sell')
    const gap = ({ mode, value }: typeof buy) => mode === 'cny' ? `${value}元`
      : mode === 'percent' ? '' : `每格基准价的${value}%`
    const spacing = buy.mode === 'percent' || sell.mode === 'percent'
      ? gridDescriptiveTitle(plan)
      : buy.mode === sell.mode && Number(buy.value) === Number(sell.value)
      ? gap(buy) : `买${gap(buy)}/卖${gap(sell)}`
    const p = plan.parameters
    const size = p.sizing_mode === 'amount' ? p.order_amount_cny == null ? '' : `${p.order_amount_cny}元/格`
      : p.order_shares == null ? '' : `${p.order_shares}股/格`
    return `网格·${spacing}${size ? `·${size}` : ''}`
  }
  if (plan.kind === 'scheduled') {
    const cadence = ({ monthly: '每月', weekly: '每周', once: '单次' } as Record<string, string>)[String(plan.parameters.frequency)] ?? '定期'
    return `${cadence}${plan.parameters.side === 'sell' ? '计划卖出' : '计划买入'}`
  }
  const names: Record<string, string> = {
    rebound: '反弹', pullback: '回落', take_profit: '止盈', stop_loss: '止损',
    holding_period: '持有期退出', price: '价格条件', relative_price: '价差条件',
  }
  const labels = [...new Set(planRules(plan).map(rule => {
    const name = names[String(rule.kind)] ?? '其他条件'
    return ['rebound', 'pullback', 'price', 'relative_price'].includes(String(rule.kind))
      ? `${name}${rule.side === 'buy' ? '买入' : '卖出'}` : name
  }))]
  return labels.length ? labels.slice(0, 3).join(' / ') + (labels.length > 3 ? '等组合' : '') : '条件交易计划'
}
export function gridSpacingFor(plan: PricePlan, side: 'buy' | 'sell') {
  return { mode: plan.parameters[`${side}_spacing_mode`] ?? plan.parameters.spacing_mode ?? 'cny',
    value: plan.parameters[`${side}_spacing`] ?? plan.parameters.spacing ?? 1 }
}
/** Structured review facts; do not split the prose summary or alter executable parameters. */
export function gridReviewPresentation(plan: PricePlan, legs?: Pick<StrategySpec, 'entry' | 'exit'>) {
  const p = plan.parameters
  const gap = (side: 'buy' | 'sell') => gridGapLabel(plan, side)
  const quantity = p.sizing_mode === 'amount' ? `${p.order_amount_cny}元` : `${p.order_shares}股`
  return {
    anchor: gridAnchorLabel(plan),
    initialization: (Number(p.opening_shares) > 0 ? `期初已有可卖持仓${p.opening_shares}股；` : '')
      + (Number(p.initial_shares) > 0
        ? `首个交易日开盘提交买入${p.initial_shares}股，当日有效；按实际成交计仓，次日可卖。`
        : Number(p.opening_shares) > 0 ? '不额外提前建仓，等待网格触发。' : '空仓启动，等待买点；不提前建仓。')
      + (legs?.exit ? '卖出按独立退出规则执行，不使用网格卖出。' : legs?.entry
        ? '买入按独立买入规则执行；没有可卖持仓时等待，不提交网格卖单。'
        : '触发卖出时若可卖股份不足（含当天新买入），卖出侧将休眠且不自动恢复；买入侧仍按规则运行。'),
    buy: legs?.entry ? [] : [
      { label: '买入间距', value: gap('buy') },
      { label: '每下跨一格', value: `买入${quantity}` },
      { label: '最多持有', value: `${p.max_shares}股` },
    ],
    sell: legs?.exit ? [] : [
      { label: '卖出间距', value: gap('sell') },
      { label: '每上跨一格', value: `卖出${quantity}` },
      { label: '至少保留', value: `${p.min_shares}股` },
    ],
  }
}
export function priceRuleLabel(rule: Rule, minShares = 0) {
  const gap = `${rule.gap ?? ''}${rule.gap_unit === 'cny' ? '元' : '%'}`
  const quantity = rule.sizing_mode === 'all_position'
    ? minShares > 0 ? `除保留的${minShares}股外的全部可卖持仓` : '全部可卖持仓'
    : rule.sizing_mode === 'amount' ? `${rule.amount_cny}元` : `${rule.quantity}股`
  const action = `${rule.side === 'buy' ? '买入' : '卖出'}${quantity}`
  const labels: Record<string, string> = {
    price: `价格${rule.direction === 'down'
      ? rule.price_comparison === 'strict' ? '低于' : '不高于'
      : rule.price_comparison === 'strict' ? '高于' : '不低于'}${rule.target_price}元`,
    rebound: `从观察低点反弹${gap}`, pullback: `从观察高点回落${gap}`,
    take_profit: `较持仓成交均价上涨${gap}`, stop_loss: `较持仓成交均价下跌${gap}`,
    holding_period: `每批实际买入后满${rule.sessions}个交易日`,
    relative_price: `较${rule.reference_mode === 'first_observation' ? '首次观察价' : '上一阶段成交均价'}${rule.direction === 'down' ? '下跌' : '上涨'}${gap}`,
  }
  return `${labels[String(rule.kind)] ?? '条件触发'}，${action}`
}
export function pricePlanSides(plan: PricePlan, legs?: Pick<StrategySpec, 'entry' | 'exit'>) {
  if (plan.kind === 'scheduled') {
    const p = plan.parameters
    const cadence = p.frequency === 'once' ? '区间首个交易日' : p.frequency === 'monthly'
      ? `每月${p.day}日（月末不足则取月末）` : `每周${['', '一', '二', '三', '四', '五', '六', '日'][Number(p.day)]}`
    const amount = p.sizing_mode === 'amount' ? `${p.budget_cny}元（含费用预算）` : `${p.quantity}股`
    const rule = `${cadence}，${p.at === 'close' ? '收盘' : '开盘'}${p.side === 'sell' ? '卖出' : '买入'}${amount}；休市顺延`
    const entry = p.side === 'buy' ? [rule] : []
    if (p.buy_on_start) entry.unshift(`区间首个交易日买入${amount}；与当日定投重合时只买一次`)
    const exits = Array.isArray(p.exit_rules) ? p.exit_rules.map(r => priceRuleLabel(r, Number(p.min_shares ?? 0))) : []
    return { entry, exit: p.side === 'sell' ? [rule] : exits.map(r => p.frequency === 'once' ? r : `${r}；卖出后继续按期定投`) }
  }
  if (plan.kind === 'conditional') {
    const groups = conditionalRuleGroups(plan)
    const sides: { entry: string[]; exit: string[] } = { entry: [], exit: [] }
    if (Number(plan.parameters.initial_shares) > 0) {
      sides.entry.push(`建仓：区间首个交易日开盘提交买入${plan.parameters.initial_shares}股，当日有效；成交后形成持仓，次日可卖`)
    } else if (Number(plan.parameters.opening_shares) > 0) {
      sides.entry.push(`期初已有可卖持仓${plan.parameters.opening_shares}股；不新增买入`)
    }
    groups.forEach((members, index) => {
      const conditions = `${members.length > 1 ? '任一先触发（' : ''}${members.map(rule => priceRuleLabel(rule, Number(plan.parameters.min_shares ?? 0))).join(' 或 ')}${members.length > 1 ? '）' : ''}`
      const onlySide = members.every(rule => rule.side === members[0]?.side) ? members[0]?.side : null
      const label = groups.length > 1
        ? `阶段${index + 1}：${conditions}`
        : `${onlySide === 'buy' ? '买入条件' : onlySide === 'sell' ? '卖出条件' : '触发条件'}：${conditions}`
      if (members.some(r => r.side === 'buy')) sides.entry.push(label)
      if (members.some(r => r.side === 'sell')) sides.exit.push(label)
    })
    return sides
  }
  const p = plan.parameters
  const gap = (side: 'buy' | 'sell') => gridGapLabel(plan, side)
  const anchor = gridAnchorLabel(plan)
  const quantity = p.sizing_mode === 'amount' ? `${p.order_amount_cny}元` : `${p.order_shares}股`
  return {
    entry: [...(Number(p.initial_shares) > 0 ? [`建仓：首个交易日开盘提交买入${p.initial_shares}股，当日有效；成交后形成持仓，次日可卖`] : []),
      ...(!legs?.entry ? [`基准${anchor}，买入间距${gap('buy')}；每下跨一格买入${quantity}，最多持有${p.max_shares}股`] : [])],
    exit: legs?.exit ? [] : [`卖出间距${gap('sell')}；每上跨一格卖出${quantity}，至少保留${p.min_shares}股`],
  }
}
