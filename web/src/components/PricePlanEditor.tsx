import type { PricePlan, StrategyDraft } from '../shared/api/types'
import { gridSpacingFor, planRules, priceRuleLabel, pricePlanExecution } from '../shared/price-plan'
import { NumericInput } from './NumericInput'
import { SettingInfo } from './SettingInfo'

const help: Record<string, string> = {
  price_comparison: '严格比较时，价格等于触发价不会触发；含等号时达到该价格即可触发。触发价不是保证成交价。',
  frequency: '按一次、每周或每月安排交易。休市顺延到下一个交易日；顺延到同一天的计划合并预算。',
  day: '周计划填1至7，1为周一；月计划填1至31，月份不足指定日期时取月末，再按交易日历顺延。',
  at: '事先安排在开盘或收盘尝试成交，不使用当天尚未发生的信息决定该笔交易。',
  side: '按计划买入或卖出；卖出仍受已有持仓、次日可卖和最低底仓约束。',
  budget_cny: '每期总预算包含成交费用，按整手计算可买数量。资金不足则跳过，不在以后自动补投。',
  anchor_mode: '默认取回测起始日的前一交易日收盘价。起始日休市时顺延至首个交易日，使用其昨收。修改回测区间后重新取价；也可明确选择手动价格。',
  anchor_price: '手动指定的网格基准价，单位元。后续格线围绕此价计算，不会自动替换成现价。',
  anchor_update: '触发后更新模式按本次触发格线价重设基准，不等成交；余量当日结束不跨日重报。固定和成交后模式仅用于兼容旧研究策略，不代表东财可选模式。',
  startup_mode: '等待跨格只响应启动后的价格变化；补到格位会在启动时尝试调整到对应仓位，仍受资金和可卖数量限制。',
  spacing_mode: '元价差是固定金额；基准价百分比按基准计算，触发更新模式使用每次更新后的基准；等比百分比使用相邻价格比率。百分比填 3 表示 3%。',
  spacing: '相邻格线之间的距离，按所选单位计算。买卖间距可以分别设置，不必对称。',
  buy_spacing: '向下触发买入的间距。按买入间距单位填写，例如元价差填 1 为 1 元，百分比填 3 为 3%。',
  sell_spacing: '向上触发卖出的间距。独立于买入间距，例如可以跌 3% 买、涨 1% 卖。',
  buy_spacing_mode: '买入间距的单位：元价差、基准价百分比或等比百分比。不会把元自动解释成 %。',
  sell_spacing_mode: '卖出间距的单位，可以与买入侧不同。百分比填 1 表示 1%。',
  lower_price: '网格适用的最低价格，单位元；价格越界后按计划边界处理，不会无限加仓。',
  upper_price: '网格适用的最高价格，单位元；与下界一起限定格线范围。',
  range_percent: '相对初始基准的单侧范围，5 表示下方5%至上方5%，不是每格间距。与层数和间距同时设置时必须一致。',
  levels_below: '初始基准下方的格数，按买入间距计算下界；不会改变每格间距。',
  levels_above: '初始基准上方的格数，按卖出间距计算上界；不会改变每格间距。',
  sizing_mode: '按股数指定每次委托量；按金额换算股数，费用另计。全部持仓退出按触发时实际持仓减去保留底仓计算，不按固定股数，也不绕过T+1。',
  order_shares: '每次网格委托的股票数量，不是手数。最终成交可能因资金、容量或可卖数量不足而减少。',
  order_amount_cny: '每次网格委托的目标金额，单位元，手续费另计；实际股数需按价格和交易单位换算。',
  initial_shares: '启动时计划建立的股数，会占用模拟资金，不是免费赠送的持仓；新买入部分仍受次日可卖限制。',
  opening_shares: '回测开始前已经持有且首日可卖的股票数量，不会生成一笔虚构买入。若成本未知，不能用于按成本止盈止损。',
  initial_capital_scope: '期初资金默认表示总资产，会先扣除期初持仓按首个价格标记的市值；只有明确说明现金另加底仓时才按两者相加。',
  min_shares: '希望保留的最低底仓股数，限制卖出后剩余持仓，不会凭空补足已有持仓。',
  max_shares: '允许持有的最大股数，限制继续买入；不是每笔委托股数。',
  max_position_cny: '持仓市值上限，单位元，可不设置。与最大股数同时设置时，买入还需同时满足两类约束。',
  price_mode: '选择开盘价代理、格线限价或固定限价。触发价不等于成交价；委托仍需满足价格可达与成交条件。',
  buy_limit: '买入愿意接受的最高委托价格，单位元。价格未满足条件时不保证成交。',
  sell_limit: '卖出愿意接受的最低委托价格，单位元。价格未满足条件时不保证成交。',
  limit_offset_cny: '相对于格线的委托价偏移，单位元；用于调整委托价格，不是移动网格触发条件。',
  commission_rate: '填写小数费率，不是百分数。按实际填写的费率乘以成交金额计算，再应用最低佣金。',
  minimum_commission_cny: '成交佣金的最低金额，单位元。未成交部分不产生交易佣金，归集方式以执行报告为准。',
  slippage_bps: '单边比例滑点。1 基点为 0.01%，买入加、卖出减；与固定价差同时设置时叠加。',
  slippage_cny: '单边固定不利价差，单位元/股，买入加、卖出减。只用这一项时将比例滑点设为 0。',
  repeat_cycles: '顺序条件单计划重复执行的轮数。前一环节未满足或未成交，不等于已完成一轮。',
  target_price: '价格达到该值时触发条件，单位元。触发后生成委托，不代表必然在该价格成交。',
  gap: '相对本条规则参照价格的触发幅度，单位由幅度单位决定。百分比填 5 表示 5%，元价差填 1 表示 1 元。',
  gap_unit: '区分固定元价差与相对参照价格的百分比，两种单位不能混用。',
  quantity: '本条委托的股数，不是手数；受可用资金、可卖持仓和交易单位约束。',
  amount_cny: '本条委托的目标金额，单位元；实际股数由成交价格及交易单位换算，费用另计。',
  limit_price: '可选的委托限价，单位元；买入不高于限价、卖出不低于限价。留空沿用计划默认委托口径。',
  sessions: '持有期的交易日数量，不计周末和休市日。到期触发退出，停牌或无法成交时不会虚构卖出。',
  activation_price: '达到此价格后才开始观察后续反弹或回落条件，单位元；不是最终委托成交价。',
  reference_mode: '相对价的起点。首个做T阶段可按回测首根完整K线开盘价固定；后续阶段通常按上一阶段实际成交均价。',
  direction: '指定向上还是向下达到价格条件，影响触发方向，不改变股票交易限制。',
}

const labels: Record<string, string> = {
  price_comparison: '价格比较边界',
  frequency: '执行周期', day: '计划日期', at: '执行时点', side: '交易方向', budget_cny: '每期预算（元，含费用）',
  anchor_mode: '基准价来源', anchor_price: '初始基准价（元）', anchor_update: '基准更新',
  startup_mode: '启动方式', spacing_mode: '格距单位', spacing: '每格间距',
  buy_spacing: '买入间距', sell_spacing: '卖出间距',
  buy_spacing_mode: '买入间距单位', sell_spacing_mode: '卖出间距单位',
  lower_price: '网格下界（元）', upper_price: '网格上界（元）',
  range_percent: '单侧范围（%，可不设）', levels_below: '下方格数（可不设）', levels_above: '上方格数（可不设）',
  sizing_mode: '委托数量方式', order_shares: '每格股数', order_amount_cny: '每格金额（元）',
  initial_shares: '初始建仓（股）', opening_shares: '期初已有持仓（股）',
  initial_capital_scope: '期初资金口径', min_shares: '最小底仓（股）', max_shares: '最大持仓（股）',
  max_position_cny: '最大持仓市值（元，可不设）', price_mode: '委托价格方式',
  buy_limit: '买入限价（元）', sell_limit: '卖出限价（元）', limit_offset_cny: '格线委托价偏移（元）',
  commission_rate: '佣金费率（小数）', minimum_commission_cny: '最低佣金（元）',
  slippage_bps: '滑点（基点）',
  slippage_cny: '单边固定价差（元/股）',
  repeat_cycles: '执行轮数', target_price: '触发价（元）', gap: '触发幅度', gap_unit: '幅度单位',
  quantity: '委托股数', amount_cny: '委托金额（元）', limit_price: '委托限价（元，可不设）',
  sessions: '持有交易日数', activation_price: '开始观察价（元，可不设）', direction: '触发方向',
  reference_mode: '相对价基准',
}
const spacingOptions = { cny: '元价差', anchor_percent: '基准价百分比（等差）', percent: '百分比（等比）' }
const options: Record<string, Record<string, string>> = {
  price_comparison: { inclusive: '含等号（达到即触发）', strict: '严格比较（不含等号）' },
  frequency: { once: '仅一次', weekly: '每周', monthly: '每月' },
  at: { open: '开盘', close: '收盘' }, side: { buy: '买入', sell: '卖出' },
  anchor_mode: { previous_close: '回测起始日昨收价', latest_price: '行情最新价（历史兼容）', first_open: '回测起始开盘价', manual: '手动基准价' },
  anchor_update: { last_trigger: '触发后按格线价更新', fixed: '固定格线（旧研究模式）', last_fill: '成交价更新（旧研究模式）' },
  startup_mode: { wait_for_crossing: '只响应启动后的跨格变化', catch_up: '启动时补到对应格位' },
  spacing_mode: spacingOptions, buy_spacing_mode: spacingOptions, sell_spacing_mode: spacingOptions,
  sizing_mode: { shares: '按股数', amount: '按金额（费用另计）' },
  price_mode: { next_open: '下一交易日开盘价代理', grid_limit: '格线限价', fixed_limit: '固定限价' },
  gap_unit: { cny: '元价差', percent: '百分比' }, direction: { up: '向上达到', down: '向下达到' },
  reference_mode: { previous_fill: '上一阶段实际成交均价', first_observation: '回测首根完整K线开盘价' },
  initial_capital_scope: { total_equity: '期初总资产（含已有持仓）', cash_plus_opening_holdings: '现金另加已有持仓' },
}

const sharedAccountFields = new Set([
  'initial_cash_cny', 'initial_shares', 'opening_shares', 'initial_capital_scope',
  'min_shares', 'max_shares', 'max_position_cny', 'commission_rate',
  'minimum_commission_cny', 'slippage_bps', 'slippage_cny', 'stamp_tax_rate', 'transfer_fee_rate',
])

export function PricePlanEditor({ draft, onChange, leg }: {
  draft: StrategyDraft; onChange: (draft: StrategyDraft) => void; leg?: 'entry_plan' | 'exit_plan'
}) {
  const pair = draft.strategySpec.independent_plans
  const plan = leg ? pair?.[leg] : draft.strategySpec.trading_plan
  if (!plan) return null
  const p = plan.parameters
  const commit = (next: PricePlan) => {
    // Independent legs require the composed policy, including their event/financial
    // data capability. Editing plan parameters must not downgrade that contract.
    const execution = leg || draft.strategySpec.entry || draft.strategySpec.exit
      ? draft.strategySpec.execution : pricePlanExecution(next)
    const sharedChanges = Object.fromEntries(Object.entries(next.parameters).filter(([key, value]) =>
      sharedAccountFields.has(key) && value !== p[key]))
    const updatedPlans = pair && leg ? {
      ...pair, [leg]: next,
      [leg === 'entry_plan' ? 'exit_plan' : 'entry_plan']: {
        ...pair[leg === 'entry_plan' ? 'exit_plan' : 'entry_plan'],
        parameters: { ...pair[leg === 'entry_plan' ? 'exit_plan' : 'entry_plan'].parameters, ...sharedChanges },
      },
    } : null
    onChange({ ...draft,
      execution: { ...draft.execution, entryPolicy: execution.entry_policy,
        exitPolicy: execution.exit_policy, tPlusOne: execution.t_plus_one,
        dataCapability: execution.data_capability, evaluationFrequency: execution.evaluation_frequency,
        // Price-plan fees are the executable source of truth. Keep the shared
        // draft mirror aligned so save/review state can never describe another cost model.
        slippageBps: Number(next.parameters.slippage_bps ?? draft.execution.slippageBps),
        slippageCny: Number(next.parameters.slippage_cny ?? draft.execution.slippageCny ?? 0),
        commissionRate: Number(next.parameters.commission_rate ?? draft.execution.commissionRate),
        minimumCommissionCny: Number(next.parameters.minimum_commission_cny ?? draft.execution.minimumCommissionCny),
      },
      strategySpec: { ...draft.strategySpec,
        ...(updatedPlans ? { independent_plans: updatedPlans } : { trading_plan: next }), execution } })
  }
  const field = (key: string, value: unknown, update: (v: string | number | null) => void,
    prefix = '', allowAllPosition = false) => {
    if (!(key in labels) || leg && key === 'side'
      || leg === 'exit_plan' && sharedAccountFields.has(key)) return null
    const label = `${prefix}${labels[key]}`
    const optional = ['max_position_cny', 'limit_price', 'activation_price', 'range_percent', 'levels_below', 'levels_above'].includes(key)
    const integer = ['order_shares', 'initial_shares', 'opening_shares', 'min_shares', 'max_shares',
      'quantity', 'sessions', 'repeat_cycles', 'day', 'levels_below', 'levels_above'].includes(key)
    const positive = ['anchor_price', 'spacing', 'lower_price', 'upper_price', 'order_shares',
      'buy_spacing', 'sell_spacing',
      'order_amount_cny', 'max_shares', 'max_position_cny', 'buy_limit', 'sell_limit',
      'repeat_cycles', 'target_price', 'gap', 'quantity', 'amount_cny', 'limit_price',
      'sessions', 'activation_price', 'day', 'budget_cny', 'range_percent', 'levels_below', 'levels_above'].includes(key)
    const choices = key === 'price_mode' && p.observation === 'minute_bar'
      ? { ...options.price_mode, next_open: '下一分钟开盘价代理' }
      : key === 'sizing_mode' && plan.kind === 'scheduled'
      ? (p.side === 'sell' ? { shares: '按股数' } : { shares: '按股数', amount: '按预算（含费用）' })
      : key === 'sizing_mode' && allowAllPosition
      ? { ...options.sizing_mode, all_position: '全部持仓（保留底仓）' }
      : options[key]
    return <div className="setting-row grow" key={key}>
      <span className="k"><SettingInfo label={labels[key] ?? key} help={
        key === 'sizing_mode' && plan.kind === 'scheduled' ? '按预算买入时费用包含在每期预算中；卖出按股数安排。' : help[key]
      } /></span>
      {choices ? <select aria-label={label} value={String(value ?? '')}
        onChange={e => update(e.target.value)}>
        {Object.entries(choices).map(([v, text]) => <option key={v} value={v}>{text}</option>)}
      </select> : value == null ? <input className="settings-input" data-numeric-input
        type="number" aria-label={label} placeholder="未设置"
        min={positive ? integer ? 1 : 0.01 : 0} step={integer ? 1 : 'any'} required={!optional}
        onChange={e => { if (e.target.value !== '' && Number.isFinite(Number(e.target.value))
          && e.currentTarget.validity.valid) update(Number(e.target.value)) }} /> :
        <NumericInput label={label} value={Number(value)} min={positive ? integer ? 1 : 0.01 : 0}
          integer={integer}
          onValueChange={update} />}
      {optional && value != null ? <button type="button" aria-label={`清除${label}`}
        onClick={() => update(null)}>不设置</button> : null}
    </div>
  }
  const visible = (key: string) => !(
    leg === 'entry_plan' && key === 'sell_limit'
    || leg === 'exit_plan' && key === 'buy_limit'
    ||
    plan.kind === 'scheduled' && (key === 'day' && p.frequency === 'once'
      || key === 'quantity' && p.sizing_mode !== 'shares' || key === 'budget_cny' && p.sizing_mode !== 'amount')
    ||
    ['spacing', 'spacing_mode', 'buy_spacing', 'sell_spacing', 'buy_spacing_mode', 'sell_spacing_mode'].includes(key)
    ||
    key === 'anchor_price' && p.anchor_mode !== 'manual'
    || key === 'order_shares' && p.sizing_mode !== 'shares'
    || key === 'order_amount_cny' && p.sizing_mode !== 'amount'
    || ['buy_limit', 'sell_limit'].includes(key) && p.price_mode !== 'fixed_limit'
    || key === 'limit_offset_cny' && p.price_mode !== 'grid_limit'
  )
  const anchorKeys = ['anchor_mode', 'anchor_price', 'anchor_update', 'startup_mode']
  return <div className="price-plan-settings">
    <p className="settings-help">{plan.kind === 'scheduled'
      ? '按交易日历执行定期计划；金额包含费用，资金不足跳过，不自动补投。'
      : p.observation === 'minute_bar' ? '按完整分钟观察条件，触发后的下一根分钟尝试成交。'
      : '执行周期和撮合口径以回测报告为准。元价差和百分比分开；当天买入不增加当天可卖数量。'}</p>
    {Object.entries(p).filter(([key]) => visible(key) && anchorKeys.includes(key)).map(([key, value]) => field(key, value,
      v => commit({ ...plan, parameters: { ...p, [key]: v,
        ...(key === 'anchor_mode' ? { anchor_price: null } : {}) } })))}
    {plan.kind === 'grid' && p.anchor_mode === 'previous_close' ? <p className="settings-help">
      初始基准取回测起始日的前一交易日收盘价；休市顺延。
      {p.anchor_price == null ? '价格待历史数据核验，不使用最新价或开盘价替代。'
        : `当前为 ${p.anchor_price} 元，对应 ${p.anchor_quote_time_label ?? '待核验日期'}。`}
    </p> : null}
    {plan.kind === 'grid' && p.anchor_mode === 'latest_price' ? <p className="settings-help">
      {p.anchor_price == null ? '行情最新价待获取，不会使用起始开盘价替代。'
        : `已固定基准 ${p.anchor_price} 元；数据时间标签 ${p.anchor_quote_time_label ?? '未提供'}。当前价回看历史属于参数研究。`}
    </p> : null}
    {plan.kind === 'grid' ? <section className="price-plan-rule">
      <h3>买卖间距</h3>
      <p className="settings-help">买入和卖出可以设置不同幅度、不同单位，互不覆盖。</p>
      {(['buy', 'sell'] as const).filter(side => leg
        ? side === (leg === 'entry_plan' ? 'buy' : 'sell')
        : !(side === 'buy' ? draft.strategySpec.entry : draft.strategySpec.exit)).map(side => {
        const { mode, value } = gridSpacingFor(plan, side)
        return <div key={side}>
          {field(`${side}_spacing_mode`, mode, v => commit({ ...plan, parameters: {
            ...p, [`${side}_spacing_mode`]: v } }))}
          {field(`${side}_spacing`, value, v => commit({ ...plan, parameters: {
            ...p, [`${side}_spacing`]: v } }))}
        </div>
      })}
    </section> : null}
    {Object.entries(p).filter(([key]) => visible(key) && !anchorKeys.includes(key)).map(([key, value]) => field(key, value,
      v => commit({ ...plan, parameters: { ...p, [key]: v,
        ...(plan.kind === 'scheduled' && key === 'side' && v === 'sell' ? { sizing_mode: 'shares' } : {}),
        ...(plan.kind === 'scheduled' && key === 'frequency' && v === 'weekly' && Number(p.day) > 7 ? { day: 1 } : {}),
      } })))}
    {p.slippage_cny === undefined ? field('slippage_cny', 0,
      v => commit({ ...plan, parameters: { ...p, slippage_cny: v } })) : null}
    <p className="settings-help">固定价差与比例滑点均设置时叠加。只用固定价差时，将滑点基点设为 0。停牌日不成交；一字涨停买入、一字跌停卖出按未成交记录，部分成交只计算实际成交数量的费用。</p>
    {planRules(plan).map((rule, i) => <section className="price-plan-rule" key={i}>
      <h3>条件 {i + 1} · {priceRuleLabel(rule, Number(p.min_shares ?? 0))}</h3>
      {rule.group ? <p className="settings-help">互斥组「{rule.group}」：先触发者锁定，其余取消。</p> : null}
      {Object.entries({ ...rule, price_comparison: rule.price_comparison ?? 'inclusive' }).filter(([key]) => !(key === 'quantity' && ['amount', 'all_position'].includes(String(rule.sizing_mode))
        || key === 'amount_cny' && rule.sizing_mode !== 'amount'
        || key === 'direction' && !['price', 'relative_price'].includes(String(rule.kind))
        || key === 'reference_mode' && rule.kind !== 'relative_price'
        || ['gap', 'gap_unit'].includes(key) && ['price', 'holding_period'].includes(String(rule.kind))
        || key === 'sessions' && rule.kind !== 'holding_period'
        || key === 'price_comparison' && rule.kind !== 'price'
        || key === 'target_price' && rule.kind !== 'price'))
        .filter(([key, value]) => value != null
        || key === 'amount_cny' && rule.sizing_mode === 'amount' ||
        key === 'limit_price' || key === 'activation_price' && ['rebound', 'pullback'].includes(String(rule.kind)))
        .map(([key, value]) => field(key, value, v => commit({ ...plan, parameters: { ...p,
          rules: planRules(plan).map((item, n) => n === i ? { ...item, [key]: v,
            ...(key === 'side' && v === 'buy' && item.sizing_mode === 'all_position' ? { sizing_mode: 'shares' } : {}) } : item) } }), `条件${i + 1} `,
          rule.side === 'sell'))}
    </section>)}
    <p className="settings-help">初始建仓会实际扣除资金；最小底仓约束卖出，市值上限约束买入。</p>
  </div>
}
