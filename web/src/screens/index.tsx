import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'

import type {
  CapacityMode,
  PriceLimitMode,
  StrategyDraft,
  StrategyIndicatorCondition,
  StrategyLeg,
} from '../shared/api/types'
import {
  MAXIMUM_INITIAL_CASH_CNY,
  MINIMUM_INITIAL_CASH_CNY,
} from '../shared/config/backtest'
import {
  BackIcon,
  Glyph,
  Notice,
  Row,
  Section,
  Tag,
  fmtPct,
  signClass,
} from '../components/primitives'
import { EquityChart } from '../components/EquityChart'
import { ExcessEquation } from '../components/ExcessEquation'
import { RuleTree } from '../components/SummaryCards'
import { numericResultConclusion } from '../result-conclusion'
import { secondaryMetric, strategyRuleTrees, toOrderRows } from '../view-model'
import type {
  BacktestMetrics,
  ChainNode,
  ChartMark,
  EditableRow,
  OrderStatus,
  RunEvidence,
  SeriesPoint,
  TradeRow,
} from '../types'

export function Page(
  { id, title, open, onBack, footer, children }:
  { id: string; title: string; open: boolean; onBack: () => void;
    footer?: ReactNode; children: ReactNode },
) {
  const pageRef = useRef<HTMLElement>(null)
  const wasOpen = useRef(false)
  const returnFocus = useRef<HTMLElement | null>(null)

  useEffect(() => {
    if (open && !wasOpen.current) {
      returnFocus.current = document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null
      window.setTimeout(() => pageRef.current
        ?.querySelector<HTMLButtonElement>('[data-back]')
        ?.focus(), 0)
    } else if (!open && wasOpen.current) {
      const target = returnFocus.current
      window.setTimeout(() => {
        if (target?.isConnected && !target.closest('[inert]')) target.focus()
      }, 0)
    }
    wasOpen.current = open
  }, [open])

  useEffect(() => {
    if (!open) return
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      event.preventDefault()
      onBack()
    }
    document.addEventListener('keydown', closeOnEscape)
    return () => document.removeEventListener('keydown', closeOnEscape)
  }, [onBack, open])

  return (
    <section
      ref={pageRef}
      className={`page${open ? ' in' : ''}`}
      id={`pg-${id}`}
      aria-hidden={!open}
      inert={!open}
    >
      <header className="navbar line">
        <button type="button" className="ico" data-back aria-label="返回" onClick={onBack}><BackIcon /></button>
        <h1>{title}</h1>
      </header>
      {children}
      <div className="dockbar">
        {footer}
        <div className="homebar"><i /></div>
      </div>
    </section>
  )
}

const updateLegParameter = (
  leg: StrategyLeg,
  conditionId: string,
  key: string,
  value: number,
): StrategyLeg => ({
  ...leg,
  conditions: leg.conditions.map((condition) => condition.kind === 'indicator'
    && condition.id === conditionId
    ? {
        ...condition,
        parameters: condition.parameters.map((parameter) =>
          parameter.key === key ? { ...parameter, value } : parameter,
        ),
      }
    : condition),
})

const priceLimitLabel: Record<PriceLimitMode, string> = {
  wait_for_unlock: '保守：无开板证据不成交',
  strict_no_fill_at_limit: '严格：不利涨跌停不成交',
  allow_limit_volume: '研究：允许限价容量估算',
}

export function ParamsScreen(
  { open, onBack, draft, onChange, onReset, isLocked, focus }:
  { open: boolean; onBack: () => void; draft: StrategyDraft;
    onChange: (draft: StrategyDraft) => void; onReset: () => void; isLocked: boolean;
    focus: EditableRow['key'] | 'more' },
) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const rules = strategyRuleTrees(draft)
  const indicatorConditions = [...draft.entry.conditions, ...draft.exit.conditions]
    .filter((condition): condition is StrategyIndicatorCondition =>
      condition.kind === 'indicator' && condition.parameters.length > 0,
    )
  const hasReadOnlyExitRule = draft.exit.conditions.some((condition) =>
    condition.kind === 'holding_period'
    || condition.kind === 'position_return'
    || condition.kind === 'trailing_drawdown')

  const updateParameter = (conditionId: string, key: string, value: number) => {
    onChange({
      ...draft,
      entry: updateLegParameter(draft.entry, conditionId, key, value),
      exit: updateLegParameter(draft.exit, conditionId, key, value),
    })
  }
  const updateBacktest = (key: keyof StrategyDraft['backtest'], value: string | number) =>
    onChange({ ...draft, backtest: { ...draft.backtest, [key]: value } })
  const updateExecution = <K extends keyof StrategyDraft['execution']>(
    key: K,
    value: StrategyDraft['execution'][K],
  ) => onChange({ ...draft, execution: { ...draft.execution, [key]: value } })

  useEffect(() => {
    if (!open) return
    const target = scrollRef.current?.querySelector<HTMLElement>(`[data-focus="${focus}"]`)
    if (target && typeof target.scrollIntoView === 'function') {
      target.scrollIntoView({ block: 'start', behavior: 'smooth' })
    }
  }, [focus, open])

  return (
    <Page
      id="params"
      title="策略设置"
      open={open}
      onBack={onBack}
      footer={
        <div className="btns">
          <button type="button" className="btn ghost" onClick={onReset} disabled={isLocked}>重置为识别结果</button>
          <button type="button" className="btn solid" onClick={onBack}>完成</button>
        </div>
      }
    >
      <div className="scroll" ref={scrollRef}>
        <div className="sect">
          <div data-focus="entry" className="section-anchor" />
          <Section title="交易规则" aside="来自服务端最终策略规则">
            <div className="rule-settings">
              <span className="rule-side buy">买入</span>
              <RuleTree node={rules.entry} />
            </div>
            <span data-focus="exit" className="section-anchor" />
            <div className="rule-settings">
              <span className="rule-side sell">卖出</span>
              <RuleTree node={rules.exit} />
            </div>
            {hasReadOnlyExitRule ? (
              <Notice tone="info">
                持有期、止盈止损和移动回撤来自服务端最终规则，当前只读；如需调整，请返回修改原话并重新识别。
              </Notice>
            ) : null}
          </Section>

          {indicatorConditions.length > 0 ? (
            <Section title="指标参数" aside="不改也可以直接回测">
              {indicatorConditions.flatMap((condition) => condition.parameters.map((parameter) => (
                <label className="grow setting-row" key={`${condition.id}-${parameter.key}`}>
                  <span className="k">{condition.label}<small>{parameter.label}</small></span>
                  <input
                    className="settings-input"
                    type="number"
                    aria-label={`${condition.label} ${parameter.label}`}
                    min={parameter.min}
                    max={parameter.max}
                    step={parameter.integer ? 1 : 'any'}
                    value={parameter.value}
                    disabled={isLocked}
                    onChange={(event) => updateParameter(
                      condition.id,
                      parameter.key,
                      Number(event.target.value),
                    )}
                  />
                </label>
              )))}
            </Section>
          ) : null}

          <div data-focus="range" className="section-anchor" />
          <Section title="回测范围">
            <label className="grow setting-row">
              <span className="k">开始日期</span>
              <input className="settings-input settings-input--date" type="date" value={draft.backtest.start}
                max={draft.backtest.end} disabled={isLocked}
                onChange={(event) => updateBacktest('start', event.target.value)} />
            </label>
            <label className="grow setting-row">
              <span className="k">结束日期</span>
              <input className="settings-input settings-input--date" type="date" value={draft.backtest.end}
                min={draft.backtest.start} disabled={isLocked}
                onChange={(event) => updateBacktest('end', event.target.value)} />
            </label>
            <label className="grow setting-row">
              <span data-focus="cash" className="section-anchor" />
              <span className="k">初始资金<small>只影响手数、费用与容量，不代表真实账户</small></span>
              <span className="setting-with-unit">
                <input className="settings-input" type="number" aria-label="初始资金"
                  min={MINIMUM_INITIAL_CASH_CNY} max={MAXIMUM_INITIAL_CASH_CNY} step={10_000}
                  value={draft.backtest.initialCashCny} disabled={isLocked}
                  onChange={(event) => updateBacktest('initialCashCny', Number(event.target.value))} />
                <small>元</small>
              </span>
            </label>
          </Section>

          <div data-focus="more" className="section-anchor" />
          <Section title="成交与费用" aside="影响能否成交和成交价格">
            <Row label="信号与成交" value={draft.entry.conditions.some((condition) => condition.kind === 'event')
              ? '可靠秒级按实际首次可得；可信日期按收盘后可得，下一交易日委托'
              : '收盘确认 → 下一交易日开盘价代理（时间非精确）'} wrap />
            <Row label="A 股次日可卖规则" sub="当日买入不可卖出" value="固定开启" />
            <label className="grow setting-row">
              <span className="k">涨跌停处理<small>{priceLimitLabel[draft.execution.priceLimitMode]}</small></span>
              <select className="settings-select" value={draft.execution.priceLimitMode} disabled={isLocked}
                onChange={(event) => updateExecution('priceLimitMode', event.target.value as PriceLimitMode)}>
                <option value="wait_for_unlock">默认保守</option>
                <option value="strict_no_fill_at_limit">严格不成交</option>
                <option value="allow_limit_volume">限价容量估算</option>
              </select>
            </label>
            <label className="grow setting-row">
              <span className="k">单边滑点</span>
              <span className="setting-with-unit">
                <input className="settings-input" type="number" min={0} max={100}
                  value={draft.execution.slippageBps} disabled={isLocked}
                  onChange={(event) => updateExecution('slippageBps', Number(event.target.value))} />
                <small>基点</small>
              </span>
            </label>
            <label className="grow setting-row">
              <span className="k">佣金率</span>
              <span className="setting-with-unit">
                <input className="settings-input" type="number" min={0} max={1} step={0.01}
                  value={draft.execution.commissionRate * 100} disabled={isLocked}
                  onChange={(event) => updateExecution('commissionRate', Number(event.target.value) / 100)} />
                <small>%</small>
              </span>
            </label>
            <label className="grow setting-row">
              <span className="k">最低佣金</span>
              <span className="setting-with-unit">
                <input className="settings-input" type="number" min={0}
                  value={draft.execution.minimumCommissionCny} disabled={isLocked}
                  onChange={(event) => updateExecution('minimumCommissionCny', Number(event.target.value))} />
                <small>元</small>
              </span>
            </label>
          </Section>

          <details className="advanced-settings">
            <summary>
              <span>高级研究设置</span>
              <small>容量、仓位、退出重试与预热</small>
            </summary>
            <Section title="研究执行参数" aside="默认值已适合 Demo">
              <label className="grow setting-row">
                <span className="k">成交容量模式<small>缺少可验证容量时不会自动放宽</small></span>
                <select className="settings-select" aria-label="成交容量模式"
                  value={draft.execution.capacityMode} disabled={isLocked}
                  onChange={(event) => updateExecution('capacityMode', event.target.value as CapacityMode)}>
                  <option value="point_in_time_volume">默认：时点容量</option>
                  <option value="unlimited">研究：不设上限</option>
                </select>
              </label>
              <label className="grow setting-row">
                <span className="k">成交量参与率</span>
                <span className="setting-with-unit">
                  <input className="settings-input" type="number" aria-label="成交量参与率"
                    min={0.01} max={100} step={0.01} value={draft.execution.participationRate * 100}
                    disabled={isLocked || draft.execution.capacityMode === 'unlimited'}
                    onChange={(event) => updateExecution('participationRate', Number(event.target.value) / 100)} />
                  <small>%</small>
                </span>
              </label>
              <label className="grow setting-row">
                <span className="k">最大使用仓位</span>
                <span className="setting-with-unit">
                  <input className="settings-input" type="number" aria-label="最大使用仓位"
                    min={0.01} max={100} step={1} value={draft.execution.allocationRatio * 100}
                    disabled={isLocked}
                    onChange={(event) => updateExecution('allocationRatio', Number(event.target.value) / 100)} />
                  <small>%</small>
                </span>
              </label>
              <label className="grow setting-row">
                <span className="k">卖出未成交后重试</span>
                <select className="settings-select" aria-label="卖出未成交后重试"
                  value={draft.execution.retryUnfilledExits ? 'yes' : 'no'} disabled={isLocked}
                  onChange={(event) => updateExecution('retryUnfilledExits', event.target.value === 'yes')}>
                  <option value="yes">开启</option>
                  <option value="no">关闭</option>
                </select>
              </label>
              <label className="grow setting-row">
                <span className="k">最多卖出尝试次数</span>
                <input className="settings-input" type="number" aria-label="最多卖出尝试次数"
                  min={1} max={1000} step={1} value={draft.execution.maxExitAttempts}
                  disabled={isLocked || !draft.execution.retryUnfilledExits}
                  onChange={(event) => updateExecution('maxExitAttempts', Number(event.target.value))} />
              </label>
              <label className="grow setting-row">
                <span className="k">指标预热日历天数</span>
                <input className="settings-input" type="number" aria-label="指标预热日历天数"
                  min={0} max={3650} step={1} value={draft.execution.warmupCalendarDays}
                  disabled={isLocked}
                  onChange={(event) => updateExecution('warmupCalendarDays', Number(event.target.value))} />
              </label>
              <label className="grow setting-row">
                <span className="k">结算延长日历天数</span>
                <input className="settings-input" type="number" aria-label="结算延长日历天数"
                  min={1} max={365} step={1} value={draft.execution.settlementExtensionDays}
                  disabled={isLocked}
                  onChange={(event) => updateExecution('settlementExtensionDays', Number(event.target.value))} />
              </label>
              <label className="grow setting-row">
                <span className="k">稳健性检查</span>
                <select className="settings-select" aria-label="稳健性检查"
                  value={draft.execution.runRobustness ? 'yes' : 'no'} disabled={isLocked}
                  onChange={(event) => updateExecution('runRobustness', event.target.value === 'yes')}>
                  <option value="yes">开启</option>
                  <option value="no">关闭</option>
                </select>
              </label>
            </Section>
          </details>

          <div className="settings-note">
            <Notice tone="info">
              修改后开始回测会先保存新版本；页面与引擎使用同一份策略草稿。
            </Notice>
          </div>
        </div>
      </div>
    </Page>
  )
}

/**
 * 委托状态：直接用券商柜台的词，用户在交易软件里见过。
 * hint 是给没见过的人的一句解释，不改叫法、不换成儿童用语。
 */
const ORDER_STATUS: Record<OrderStatus, {
  label: string; hint: string; brief: string; settled: boolean
}> = {
  placed: { label: '已报', hint: '委托已提交，这一天还没有成交记录', brief: '等待成交', settled: false },
  filled: { label: '已成', hint: '按委托数量全部成交', brief: '成交价未记录', settled: true },
  partial: { label: '部成', hint: '只成交了一部分，剩余数量没有成交', brief: '成交价未记录', settled: true },
  unfilled: { label: '未成', hint: '当天没有成交，可能受涨跌停、容量或次日可卖限制', brief: '未成交', settled: false },
  expired: { label: '废单', hint: '信号有效期内没有等到可成交的机会，委托作废', brief: '已作废', settled: false },
}

/** 列表里只需要「哪天几点」，秒和时区留给因果轨迹页。 */
const shortStamp = (value: string) => {
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value.slice(0, 16).replace('T', ' ')
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(parsed).replaceAll('/', '-')
}

const shortDate = (value: string) => shortStamp(value).slice(0, 10)

const providerLabels: Record<string, string> = {
  eastmoney: '东方财富公告',
  ifind: '同花顺 iFinD',
  rqdata: 'RQData',
  tushare: 'Tushare',
  web_archive: '网页存档',
  choice: 'Choice 行情快照',
  mock_sample: '固定样例',
}

const timeQualityLabels: Record<string, string> = {
  exact: '来源记录的可得时间',
  vendor_observed: '供应商记录的首次可得时间',
  date_only: '可信日期；按当日收盘后可得，下一交易日委托',
  date_only_conservative: '可信日期；按当日收盘后可得，下一交易日委托',
  estimated_research_only: '估算时间（仅研究，不可交易）',
  daily_bar_open_proxy: '日线开盘价代理（时间非精确）',
  daily_bar_available_at_proxy: '日线整根可得价代理（时间非精确）',
}

const timestampPrecisionLabels: Record<string, string> = {
  second: '秒级',
  minute: '分钟级',
  hour: '小时级',
  date: '日期级',
}

const validationStatusLabels: Record<string, string> = {
  validated: '已通过来源校验',
  unverified: '未验证，不可交易',
  blocked_time_quality: '时间证据不足，不可交易',
  demonstration_only: '仅用于界面预览，未连接事件数据',
  quarantined: '已隔离，不进入严格回测',
  rejected: '未通过校验',
}

const entryValidityConfigs = [
  {
    key: 'edge_entry_validity_sessions',
    label: '一次触发信号',
    sub: '例如金叉；过期后不会沿用旧信号',
  },
  {
    key: 'event_entry_validity_sessions',
    label: '事件信号',
    sub: '后续更正尚未逐时建模，因此默认最保守',
  },
  {
    key: 'state_entry_validity_sessions',
    label: '持续状态信号',
    sub: '例如 RSI 低于阈值；下一交易日需重新判断',
  },
  {
    key: 'composite_entry_validity_sessions',
    label: '多个条件一起满足',
    sub: '组合条件不会跨日沿用旧判断',
  },
] as const

const entryValidityKeys = new Set<string>([
  ...entryValidityConfigs.map((item) => item.key),
  'entry_signal_validity_policy',
])

const executionAssumptionLabels: Record<string, string> = {
  allocation_ratio: '资金使用比例',
  benchmark_policy: '同期持有基准',
  capacity_mode: '成交容量模式',
  commission_rate: '佣金率',
  corporate_action_policy: '公司行动处理',
  dividend_tax_policy: '现金分红税费口径',
  fee_schedule_version: '交易费用规则',
  market_rule_version: 'A 股市场规则',
  max_exit_attempts: '最多卖出尝试次数',
  minimum_commission_cny: '最低佣金',
  opening_auction_policy: '开盘成交口径',
  participation_rate: '成交量参与率',
  price_limit_mode: '涨跌停处理',
  retry_unfilled_exits: '卖出未成交后继续尝试',
  rights_issue_policy: '配股处理',
  robustness_profile: '稳健性检验口径',
  resolution: '回测周期',
  slippage_bps: '单边滑点',
}

const percentValue = (value: string) => {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? `${(parsed * 100).toFixed(2).replace(/\.00$/u, '')}%` : '已记录'
}

const executionAssumptionValue = (key: string, value: string) => {
  if (key === 'allocation_ratio' || key === 'commission_rate' || key === 'participation_rate') {
    return percentValue(value)
  }
  if (key === 'slippage_bps') return `${value} 基点`
  if (key === 'minimum_commission_cny') return `${value} 元`
  if (key === 'max_exit_attempts') return `${value} 次`
  if (key === 'retry_unfilled_exits') return value === 'true' ? '开启' : '关闭'
  if (key === 'resolution') return value === '1d' ? '日线' : '已记录'
  if (key === 'price_limit_mode') {
    return priceLimitLabel[value as PriceLimitMode] ?? '已记录'
  }
  if (key === 'capacity_mode') {
    return value === 'point_in_time_volume'
      ? '上一已完成交易日成交量 × 参与率'
      : value === 'unlimited' ? '不设容量上限（研究模式）' : '已记录'
  }
  if (key === 'benchmark_policy') return '同资金、同费用和成交约束的买入持有账户'
  if (key === 'corporate_action_policy') return '登记日锁定权益，除权日确认应收，到账后结算'
  if (key === 'dividend_tax_policy') return '研究口径按毛额计算，暂不模拟个人持有期补税'
  if (key === 'rights_issue_policy') return '不注入外部资金，默认不参与配股'
  if (key === 'opening_auction_policy') return '使用日线开盘价代理，时间非精确'
  if (key === 'fee_schedule_version') return 'A 股现金股票费用规则'
  if (key === 'market_rule_version') return 'A 股现货市场规则'
  if (key === 'robustness_profile') return '执行假设稳健性检验'
  return '已记录'
}

const entryValidityRows = (assumptions: Record<string, string>) => {
  const compositeFallback = assumptions.entry_signal_validity_policy
    ?.includes('composite_fail_closed') ? '1' : undefined
  return entryValidityConfigs.flatMap((item) => {
    const rawValue = assumptions[item.key]
      ?? (item.key === 'composite_entry_validity_sessions' ? compositeFallback : undefined)
    if (rawValue == null) return []
    const sessions = Number(rawValue)
    const value = Number.isInteger(sessions) && sessions > 0
      ? `${sessions} 个交易日内 · 最多 ${sessions} 次尝试`
      : rawValue
    return [{ ...item, value }]
  })
}

const shortIdentity = (value: string) => value.length <= 26
  ? value
  : `${value.slice(0, 14)}…${value.slice(-8)}`

const safeSourceUrl = (value: string | null | undefined) =>
  value && /^https?:\/\//i.test(value) ? value : null

const ACCEPTED_DATA_SCHEMA = 'local-parquet.market-data.v3'
const ACCEPTED_PRODUCER_SNAPSHOT_SCHEMA = 'ashare-lab.composite-research-snapshot.v2'
const SHA256_IDENTITY = /^sha256:[0-9a-f]{64}$/
const COMPOSITE_SNAPSHOT_IDENTITY = /^composite:[0-9a-f]{64}$/

const hasCompleteIdentity = (evidence: RunEvidence) => Boolean(
  evidence.dataSchemaVersion === ACCEPTED_DATA_SCHEMA
  && evidence.producerSnapshotSchemaVersion === ACCEPTED_PRODUCER_SNAPSHOT_SCHEMA
  && COMPOSITE_SNAPSHOT_IDENTITY.test(evidence.producerSnapshotId ?? '')
  && evidence.snapshotId
  && SHA256_IDENTITY.test(evidence.snapshotChecksum ?? '')
  && /^[0-9a-f]{40}$/.test(evidence.gitSha ?? '')
  && SHA256_IDENTITY.test(evidence.catalog ?? '')
  && SHA256_IDENTITY.test(evidence.strategyHash ?? ''),
)

export function ReportScreen(
  { open, onBack, metrics, series, marks, trades, evidence, onOpenChain,
    onOpenExecution, mode = 'mock' }:
  { open: boolean; onBack: () => void; metrics: BacktestMetrics;
    series: SeriesPoint[]; marks: ChartMark[]; trades: TradeRow[]; evidence: RunEvidence;
    onOpenChain: (trade: TradeRow) => void; onOpenExecution: () => void;
    mode?: 'mock' | 'live' },
) {
  const [pagination, setPagination] = useState({ runId: evidence.runId, count: 20 })
  const [selection, setSelection] = useState<{
    runId: string;
    orderId: string | null;
    activityId: string | null;
  }>({ runId: evidence.runId, orderId: null, activityId: null })
  const listRef = useRef<HTMLDivElement>(null)
  const identityComplete = hasCompleteIdentity(evidence)
  const identityStatus = identityComplete ? 'implemented' : 'partial'
  const identityLabel = identityComplete ? '身份已记录' : '身份不完整'

  const orders = useMemo(() => toOrderRows(trades), [trades])
  const secondary = secondaryMetric(metrics)
  const visibleCount = pagination.runId === evidence.runId ? pagination.count : 20
  const selectedOrderId = selection.runId === evidence.runId ? selection.orderId : null
  const selectedMarkId = selection.runId === evidence.runId ? selection.activityId : null
  const visibleOrders = orders.slice(0, visibleCount)
  const remaining = orders.length - visibleOrders.length
  const filledCount = orders.filter((order) =>
    order.status === 'filled' || order.status === 'partial').length

  /** 图上点一个 B/S 点，就把下面列表滚到那一笔并高亮，而不是另开一块明细。 */
  const focusOrder = (activityId: string) => {
    const index = orders.findIndex((order) => order.activityIds.includes(activityId))
    const orderId = index >= 0 ? orders[index]?.id ?? null : null
    setSelection({ runId: evidence.runId, orderId, activityId })
    if (index >= visibleCount) {
      setPagination({ runId: evidence.runId, count: Math.ceil((index + 1) / 20) * 20 })
    }
    window.setTimeout(() => {
      if (!orderId) return
      const node = listRef.current?.querySelector<HTMLElement>(`[data-order-id="${orderId}"]`)
      const reducedMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
      node?.scrollIntoView({ block: 'center', behavior: reducedMotion ? 'auto' : 'smooth' })
    }, 0)
  }

  return (
    <Page id="report" title="回测报告" open={open} onBack={onBack}>
      <div className="scroll">
        <div className="sect report-flow">
          <section className="report-summary" aria-labelledby="report-conclusion">
            <div className="report-summary-meta">
              <span>{metrics.dataRange.start} 至 {metrics.dataRange.end}</span>
              {mode === 'live' ? <Tag status={identityStatus}>{identityLabel}</Tag> : null}
            </div>
            <h2 id="report-conclusion">{numericResultConclusion(metrics)}</h2>
            <div className="headline">
              <span className={`big ${signClass(metrics.total)}`}>{fmtPct(metrics.total)}</span>
              <span className="lb">策略总收益</span>
            </div>
            {/* 超额不再是一个孤立的 KPI：拆成等式，用户能看出它和上面那个数字的关系。 */}
            <ExcessEquation
              total={metrics.total}
              bench={metrics.bench}
              excess={metrics.excess}
              comparisonStatus={metrics.benchmarkComparisonStatus}
            />
            <div className="kpis kpis--pair" aria-label="风险与胜率">
              <div className="kpi"><b>{fmtPct(metrics.mdd)}</b><span>最大回撤<small>从最高点往下跌最多的一段</small></span></div>
              <div className="kpi"><b>{secondary.value}</b><span>{secondary.label}<small>{secondary.note}</small></span></div>
            </div>
          </section>

          <section id="report-performance" aria-labelledby="report-performance-title">
            <div className="sect-h"><h2 id="report-performance-title">净值与回撤</h2><em>点 B/S 定位到下面那一笔</em></div>
            <div className="report-chart">
              <EquityChart
                series={series}
                marks={marks}
                comparisonAvailable={metrics.benchmarkComparisonStatus === 'comparable'}
                selectedMarkId={selectedMarkId}
                onSelectMark={(mark) => focusOrder(mark.activityId)}
              />
            </div>
          </section>

          {/*
            排版对齐券商 App 的委托列表：方向/时间 · 触发信号 · 委托价 · 状态/成交价。
            券商那一列「名称/代码」在这里没有信息量（单股回测，每行都是同一只），
            换成触发这笔委托的信号，列的节奏保持一致。委托数量按需求不展示。
            长解释和逐笔时间都收进因果轨迹页，列表只负责一眼扫完。
          */}
          <section id="report-trades" aria-labelledby="report-trades-title">
            <div className="sect-h">
              <h2 id="report-trades-title">每笔委托</h2>
              <em>{orders.length} 笔 · 成交 {filledCount} 笔 · 点行看轨迹</em>
            </div>
            <div className="order-table" ref={listRef}>
              <div className="order-head" aria-hidden="true">
                <span>方向 / 时间</span>
                <span>信号</span>
                <span className="num-col">委托价</span>
                <span className="num-col">状态 / 成交价</span>
              </div>
              {orders.length === 0 ? <div className="empty-row">
                区间内没有产生任何委托。请延长回测区间，或检查买入/卖出条件是否能在该区间成立。
              </div> : null}
              {visibleOrders.map((order) => {
                const status = ORDER_STATUS[order.status]
                const active = order.id === selectedOrderId
                const stamp = order.orderAt ?? order.signalAt ?? order.filledAt
                return (
                  <button
                    key={order.id}
                    type="button"
                    data-order-id={order.id}
                    className={`order-row${active ? ' is-active' : ''}`}
                    aria-current={active ? 'true' : undefined}
                    aria-label={`${order.side === 'buy' ? '买入' : '卖出'} ${order.title} ${status.label}`}
                    onClick={() => {
                      const anchor = order.activities.find((item) => item.id === order.id)
                        ?? order.activities.find((item) => item.kind !== 'signal')
                        ?? order.activities[0]
                      const chartMark = marks.find((mark) => mark.activityId === anchor?.id)
                        ?? marks.find((mark) => order.activityIds.includes(mark.activityId))
                      setSelection({
                        runId: evidence.runId,
                        orderId: order.id,
                        activityId: chartMark?.activityId ?? null,
                      })
                      if (anchor) onOpenChain(anchor)
                    }}
                  >
                    <span className="oc oc-side">
                      <b className={order.side}>{order.side === 'buy' ? '买入' : '卖出'}</b>
                      <small>{stamp ? shortStamp(stamp) : '时间未记录'}</small>
                    </span>
                    <span className="oc oc-signal">
                      <b>{order.title}</b>
                      <small>{order.signalAt ? `${shortDate(order.signalAt)} 确认` : '信号未记录'}</small>
                    </span>
                    <span className="oc oc-num">
                      <b>{order.orderPrice != null ? `¥${order.orderPrice.toFixed(2)}` : '—'}</b>
                    </span>
                    <span className="oc oc-num">
                      <b className={`order-status order-status--${order.status}`} title={status.hint}>
                        {status.label}
                      </b>
                      <small>{order.price != null ? `¥${order.price.toFixed(2)}` : status.brief}</small>
                    </span>
                  </button>
                )
              })}
              {remaining > 0 ? (
                <button type="button" className="order-more"
                  onClick={() => setPagination({ runId: evidence.runId, count: visibleCount + 20 })}>
                  再看 {Math.min(20, remaining)} 笔 · 还剩 {remaining} 笔
                </button>
              ) : null}
            </div>
          </section>

          <Section title="更多信息">
            <Row label="成交规则与数据依据" sub="涨跌停、费用、信号来源与运行身份"
              onClick={onOpenExecution} />
          </Section>
        </div>
      </div>
    </Page>
  )
}

export function ExecutionDetailsScreen(
  { open, onBack, draft, trades, evidence, mode = 'mock' }:
  { open: boolean; onBack: () => void; draft: StrategyDraft; trades: TradeRow[];
    evidence: RunEvidence; mode?: 'mock' | 'live' },
) {
  const sourceEvidence = Array.from(
    new Map(
      trades.flatMap((trade) => trade.evidence)
        .map((item) => [`${item.type}:${item.id}:${item.availableAt}`, item] as const),
    ).values(),
  )
  const identityComplete = hasCompleteIdentity(evidence)
  const identityStatus = identityComplete ? 'implemented' : 'partial'
  const identityLabel = identityComplete ? '身份已记录' : '身份不完整'
  const validityRows = entryValidityRows(evidence.executionAssumptions)

  // 只保留真正有值的身份项；缺失的合并成一句说明，不用一行一个「未记录」占屏。
  const identityFields = [
    { label: '代码版本', value: evidence.gitSha },
    { label: '数据快照', value: evidence.snapshotId },
    { label: '生产快照', value: evidence.producerSnapshotId },
    { label: '数据结构版本', value: evidence.dataSchemaVersion },
    { label: '快照生成结构版本', value: evidence.producerSnapshotSchemaVersion },
    { label: '快照校验值', value: evidence.snapshotChecksum },
    { label: '规则目录校验值', value: evidence.catalog },
    { label: '策略校验值', value: evidence.strategyHash },
    { label: '回测引擎版本', value: evidence.engineVersion },
  ]
  const recordedIdentity = identityFields
    .flatMap((item) => item.value ? [{ label: item.label, value: item.value }] : [])
  const missingIdentity = identityFields
    .flatMap((item) => item.value ? [] : [item.label])
  const otherAssumptions = Object.entries(evidence.executionAssumptions)
    .filter(([key]) => !entryValidityKeys.has(key))

  return (
    <Page id="execution" title="成交规则与数据依据" open={open} onBack={onBack}>
      <div className="scroll"><div className="sect">
        <Section title="成交规则">
          <Row label="信号与成交" value={draft.entry.conditions.some((condition) => condition.kind === 'event')
            ? '可靠秒级按实际首次可得；可信日期按收盘后可得，下一交易日委托'
            : '收盘确认 → 下一交易日开盘价代理（时间非精确）'} wrap />
          <Row label="A 股次日可卖规则" value="固定开启" />
          <Row label="涨跌停" value={priceLimitLabel[draft.execution.priceLimitMode]} wrap />
          <Row label="滑点与佣金" sub={`最低佣金 ${draft.execution.minimumCommissionCny} 元`}
            value={`${draft.execution.slippageBps} 基点 · ${(draft.execution.commissionRate * 100).toFixed(3)}%`} wrap />
          {otherAssumptions.map(([key, value]) => (
            <Row key={key} label={executionAssumptionLabels[key] ?? '其他执行参数'}
              value={executionAssumptionValue(key, value)} wrap />
          ))}
        </Section>
        {validityRows.length > 0 ? <Section title="买入信号能等多久" aside="按交易日尝试">
          {validityRows.map((item) => (
            <Row key={item.key} label={item.label} sub={item.sub} value={item.value} wrap />
          ))}
        </Section> : null}
        {/*
          原来这里固定铺十行，其中九行是「未记录」——占了整屏，没有传递任何信息。
          现在只列真正记录到的项；缺的那些合并成一句话说清楚缺什么、影响是什么，
          不用一行一个空值来假装完整。
        */}
        <Section title="运行身份" aside={mode === 'live'
          ? <Tag status={identityStatus}>{identityLabel}</Tag>
          : undefined}>
          <Row label="回测任务编号" sub="报给工程时带上这个就够了" value={evidence.runId} wrap />
          {recordedIdentity.map((item) => (
            <Row key={item.label} label={item.label} value={item.value} wrap />
          ))}
          {missingIdentity.length > 0 ? (
            <div className="grow identity-missing">
              <span className="k">
                还没有记录的项
                <small>{missingIdentity.join('、')}</small>
              </span>
              <span className="v wrap">
                {mode === 'live'
                  ? '缺少这些版本与校验值，这次结果只能查看，不能作为可复现的验收证据。'
                  : '界面预览不带版本与校验值。'}
              </span>
            </div>
          ) : null}
        </Section>
        <Section title="信号来源与时间" aside={`${sourceEvidence.length} 条证据`}>
          {sourceEvidence.length === 0 ? <div className="empty-row">
            当前结果没有保存信号来源、首次可得时间或原始响应校验值，不能将来源补写为已验证。
          </div> : sourceEvidence.map((item) => {
            const href = safeSourceUrl(item.sourceUrl)
            const provider = providerLabels[item.provider?.toLowerCase() ?? ''] ?? item.provider ?? '来源未记录'
            const validationStatus = validationStatusLabels[item.validationStatus?.toLowerCase() ?? '']
              ?? (item.validationStatus ? '已记录，可在技术详情中查看' : '校验状态未记录')
            return (
              <div className="grow source-evidence" key={`${item.type}:${item.id}:${item.availableAt}`}>
                <span className="k">
                  {href ? <a href={href} target="_blank" rel="noreferrer">{provider} ↗</a>
                    : <span className="source-provider">{provider}</span>}
                  <small>首次可得：{item.availableAt}</small>
                  <small>时间质量：{timeQualityLabels[item.timeQuality?.toLowerCase() ?? ''] ?? '已记录，可在技术详情中查看'} · 时间精度：{timestampPrecisionLabels[item.timestampPrecision ?? ''] ?? '未记录'} · {validationStatus}</small>
                </span>
                <details className="technical-details">
                  <summary>技术详情</summary>
                  <div className="technical-row">数据来源代码：<code>{item.provider ?? '未记录'}</code></div>
                  <div className="technical-row">时间质量代码：<code>{item.timeQuality ?? '未记录'}</code></div>
                  <div className="technical-row">时间精度代码：<code>{item.timestampPrecision ?? '未记录'}</code></div>
                  <div className="technical-row">校验状态代码：<code>{item.validationStatus ?? '未记录'}</code></div>
                  <div className="technical-row">来源事件编号：<code>{shortIdentity(item.sourceEventId ?? item.id)}</code></div>
                  <div className="technical-row">原始响应校验：<code>{item.rawResponseSha256 ? shortIdentity(item.rawResponseSha256) : '未记录'}</code></div>
                </details>
              </div>
            )
          })}
        </Section>
        {mode === 'live' && !identityComplete ? <div className="settings-note"><Notice>
          缺少完整快照校验值或 40 位代码版本，这份结果只能查看，不能标为已验证。
        </Notice></div> : null}
      </div></div>
    </Page>
  )
}

export function ChainScreen(
  { open, onBack, title, nodes }:
  { open: boolean; onBack: () => void; title: string; nodes: ChainNode[] },
) {
  return (
    <Page id="chain" title={title} open={open} onBack={onBack}>
      <div className="scroll"><div className="sect">
        <div className="sect-h"><h2>从你那句话到账户变化</h2><em>每一步都能查</em></div>
        <div className="chain">
          {nodes.length === 0 ? <div className="empty-row">
            这条活动没有保存完整的可追溯关系。请返回报告选择其他活动，或重新读取结果。
          </div> : null}
          {nodes.map((node, index) => (
            <div className="node" key={`${node.ref}-${index}`}>
              <span className="mk">{node.kind === 'decision' ? (
                <svg width="14" height="14" viewBox="0 0 14 14" role="img" aria-label="决策">
                  <path d="M7 1.1 12.9 7 7 12.9 1.1 7Z" fill="none" stroke="currentColor" strokeWidth="1.5" />
                  <path d="m4.7 7 1.5 1.5 3.2-3.2" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
              ) : <Glyph kind={node.kind} />}</span>
              <span className="ct">
                <b>{node.title}</b>
                {node.timestamp ? <span className="ts">{node.timestamp}</span> : null}
                <p>{node.detail}</p>
                {node.facts?.map((fact) => {
                  const href = safeSourceUrl(fact.href)
                  return <p key={`${fact.label}:${fact.value}`}><strong>{fact.label}：</strong>{href
                    ? <a href={href} target="_blank" rel="noreferrer">{fact.value} ↗</a>
                    : fact.value}</p>
                })}
              </span>
            </div>
          ))}
        </div>
        {nodes.length > 0 ? <details className="technical-details chain-technical-details">
          <summary>技术详情</summary>
          {nodes.map((node, index) => (
            <div className="technical-row" key={`${node.ref}:technical:${index}`}>
              <strong>步骤 {index + 1}</strong><br />
              <span>链路身份：<code>{node.ref}</code></span>
              {node.technicalFacts?.map((fact) => (
                <span key={`${fact.label}:${fact.value}`}><br />{fact.label}：<code>{fact.value}</code></span>
              ))}
            </div>
          ))}
        </details> : null}
        <div className="settings-note"><Notice tone="info">
          信号、决策、委托和成交的关系以回测结果保存的链路为准；未返回的逐笔现金不会由前端补算。
        </Notice></div>
      </div></div>
    </Page>
  )
}
