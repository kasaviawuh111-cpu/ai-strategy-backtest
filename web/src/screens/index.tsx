import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { PricePlanEditor } from '../components/PricePlanEditor'
import { SettingInfo } from '../components/SettingInfo'

import type {
  CapacityMode,
  CapabilitiesResponse,
  PriceLimitMode,
  StrategyDraft,
  StrategyIndicatorCondition,
  StrategyLeg,
} from '../shared/api/types'
import {
  MAXIMUM_INITIAL_CASH_CNY,
  MINIMUM_INITIAL_CASH_CNY,
} from '../shared/config/backtest'
import { dataAsOfDate } from '../shared/api/contract'
import { MINIMUM_BACKTEST_DATE, validateBacktestDates } from '../shared/backtest-date-validation'
import {
  BackIcon,
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
import { RuleSpecEditor } from '../components/RuleSpecEditor'
import { NumericInput, NumericInputGroup } from '../components/NumericInput'
import { InlineStockEditor, type InlineStockEditorActions } from '../components/InlineStockEditor'
import { numericResultConclusion } from '../result-conclusion'
import { secondaryMetric, strategyRuleTrees, toOrderRows } from '../view-model'
import type {
  BacktestMetrics,
  ChartMark,
  EditableRow,
  OrderStatus,
  RunEvidence,
  SeriesPoint,
  TradeRow,
} from '../types'

function signalExecutionSummary(draft: StrategyDraft): string {
  if (draft.execution.evaluationFrequency === 'daily_close_and_minute_bar'
      || draft.exit.conditions.some((condition) => condition.kind === 'minute_protection')) {
    return '日线条件收盘后确认，下一交易日开盘尝试建仓；分钟保护在完成K线后确认，新委托下一分钟生效（非真实逐笔成交）'
  }
  return draft.entry.conditions.some((condition) => condition.kind === 'event')
    ? '可靠秒级按实际首次可得；可信日期按收盘后可得，下一交易日委托'
    : '收盘确认 → 下一交易日开盘价代理（时间非精确）'
}

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
      if (event.key !== 'Escape' || event.defaultPrevented) return
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

const priceLimitHelp: Record<PriceLimitMode, string> = {
  wait_for_unlock: '买入遇涨停开盘、卖出遇跌停开盘时，日线无法证明何时能排队成交，因此这笔委托不成交；即使当天曾开板，也不会猜成交时间。',
  strict_no_fill_at_limit: '买入遇涨停开盘、卖出遇跌停开盘时，直接判这笔委托不成交。当前日线模式下，与默认保守的成交结果通常相同，区别在拒绝原因。',
  allow_limit_volume: '研究用的偏乐观假定：允许按涨跌停价尝试成交，仍受资金、参与率和可用容量限制；没有容量证据时仍可能不成交，不代表真实排队能买到或卖出。',
}

export function ParamsScreen(
  { open, onBack, draft, onChange, onReset, isLocked, focus, stockEditor, conditionPath, capabilities }:
  { open: boolean; onBack: () => void; draft: StrategyDraft;
    onChange: (draft: StrategyDraft) => void; onReset: () => void; isLocked: boolean;
    focus: EditableRow['key'] | 'more'; stockEditor?: InlineStockEditorActions;
    conditionPath?: string; capabilities?: CapabilitiesResponse },
) {
  const [stockEditing, setStockEditing] = useState(false)
  const [numericReset, setNumericReset] = useState(0)
  const [numbersValid, setNumbersValid] = useState(true)
  if (!open && stockEditing) setStockEditing(false)
  const scrollRef = useRef<HTMLDivElement>(null)
  const startDateRef = useRef<HTMLInputElement>(null)
  const endDateRef = useRef<HTMLInputElement>(null)
  const [dateEdits, setDateEdits] = useState<{ draftId: string; start: string; end: string } | null>(null)
  if (dateEdits && (!open || dateEdits.draftId !== draft.id)) setDateEdits(null)
  const dates = dateEdits ?? draft.backtest
  const latestDate = dataAsOfDate()
  const dateValidation = validateBacktestDates(dates.start, dates.end, latestDate)
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

  const currentDateValues = () => ({
    start: startDateRef.current?.value ?? dates.start,
    end: endDateRef.current?.value ?? dates.end,
  })
  const syncDateInputs = () => setDateEdits({ draftId: draft.id, ...currentDateValues() })

  const finishEditing = () => {
    if (stockEditing) return
    // Numeric controls buffer incomplete text; never leave using the previous legal draft.
    const invalidNumbers = [...(scrollRef.current?.querySelectorAll<HTMLInputElement>('[data-numeric-input]') ?? [])]
      .filter((input) => !input.disabled && !input.checkValidity())
    if (invalidNumbers.length) {
      invalidNumbers[0]?.focus()
      return
    }
    // Native date controls may change their displayed value without React's change event.
    const currentDates = currentDateValues()
    const currentValidation = validateBacktestDates(currentDates.start, currentDates.end, latestDate)
    if (!currentValidation.valid) {
      setDateEdits({ draftId: draft.id, ...currentDates })
      const input = currentValidation.field === 'start' ? startDateRef : endDateRef
      input.current?.focus({ preventScroll: true })
      input.current?.scrollIntoView?.({ block: 'center', behavior: 'instant' })
      return
    }
    if (currentDates.start !== draft.backtest.start || currentDates.end !== draft.backtest.end) {
      onChange({ ...draft, backtest: { ...draft.backtest, ...currentDates } })
    }
    setDateEdits(null)
    onBack()
  }

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
      onBack={finishEditing}
      footer={
        <div className="btns">
          <button type="button" className="btn ghost" onClick={() => {
            setDateEdits(null)
            setNumericReset((value) => value + 1)
            onReset()
          }} disabled={isLocked || stockEditing}>重置为识别结果</button>
          <button type="button" className="btn solid" disabled={stockEditing} onClick={finishEditing}>完成</button>
        </div>
      }
    >
      <div className="scroll" ref={scrollRef}>
        <div className="sect">
          <Section title="股票">
            <div className="settings-stock">
              {stockEditor ? <InlineStockEditor key={`${draft.id}-${open}`} instrument={{ name: draft.instrument.name, code: draft.instrument.symbol }}
                disabled={isLocked || !numbersValid} {...stockEditor} onEditingChange={setStockEditing} />
                : <span>{draft.instrument.name} {draft.instrument.symbol}</span>}
            </div>
          </Section>
          <NumericInputGroup resetKey={`${draft.id}-${numericReset}`} onValidityChange={setNumbersValid}>
          <fieldset className="settings-fields" disabled={isLocked || stockEditing}>
          {draft.strategySpec.trading_plan ? <Section title="交易计划参数">
            <PricePlanEditor draft={draft} onChange={onChange} />
          </Section> : null}
          {!draft.strategySpec.trading_plan && (focus === 'entry' || focus === 'exit') ? <div data-focus={focus}><Section title={focus === 'entry' ? '编辑买入条件' : '编辑卖出条件'}>
            <RuleSpecEditor draft={draft} side={focus} path={conditionPath} capabilities={capabilities} onChange={onChange} />
          </Section></div> : null}
          <div data-focus="entry" className="section-anchor" />
          {focus !== 'entry' && focus !== 'exit' ? <Section title="交易规则" aside="来自服务端最终策略规则">
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
                点击审阅页中的具体卖出条件，可修改持有期、止盈止损和移动回撤参数。
              </Notice>
            ) : null}
          </Section> : null}

          {focus !== 'entry' && focus !== 'exit' && indicatorConditions.length > 0 ? (
            <Section title="指标参数" aside="不改也可以直接回测">
              {indicatorConditions.flatMap((condition) => condition.parameters.map((parameter) => (
                <div className="grow setting-row" key={`${condition.id}-${parameter.key}`}>
                  <span className="k"><SettingInfo label={parameter.label} /><small>{condition.label}</small></span>
                  <NumericInput
                    label={`${condition.label} ${parameter.label}`}
                    min={parameter.min}
                    max={parameter.max}
                    integer={parameter.integer}
                    value={parameter.value}
                    disabled={isLocked}
                    onValueChange={(value) => updateParameter(
                      condition.id,
                      parameter.key,
                      value,
                    )}
                  />
                </div>
              )))}
            </Section>
          ) : null}

          <div data-focus="range" className="section-anchor" />
          <Section title="回测范围">
            <div className="grow setting-row"><span className="k"><SettingInfo label="常用区间" /></span><select className="settings-select" aria-label="常用区间" value="custom"
              onChange={(event) => {
                if (event.target.value === 'custom') return
                const end = latestDate
                const start = new Date(`${end}T00:00:00Z`)
                start.setUTCDate(1)
                start.setUTCMonth(start.getUTCMonth() - Number(event.target.value))
                const day = Number(end.slice(-2))
                const last = new Date(Date.UTC(start.getUTCFullYear(), start.getUTCMonth() + 1, 0)).getUTCDate()
                start.setUTCDate(Math.min(day, last))
                setDateEdits({ draftId: draft.id, start: start.toISOString().slice(0, 10), end })
              }}><option value="custom">自定义日期</option><option value="6">近半年</option><option value="12">近一年</option><option value="36">近三年</option></select></div>
            <div className="grow setting-row">
              <span className="k"><SettingInfo label="开始日期" /></span>
              <input ref={startDateRef} className="settings-input settings-input--date" type="date" aria-label="开始日期"
                value={dates.start} min={MINIMUM_BACKTEST_DATE} max={latestDate} required disabled={isLocked}
                aria-invalid={dateValidation.field === 'start'}
                aria-describedby={dateValidation.field === 'start' ? 'backtest-date-error' : undefined}
                onInput={syncDateInputs} onChange={syncDateInputs} />
            </div>
            <div className="grow setting-row">
              <span className="k"><SettingInfo label="结束日期" /></span>
              <input ref={endDateRef} className="settings-input settings-input--date" type="date" aria-label="结束日期"
                value={dates.end} min={MINIMUM_BACKTEST_DATE} max={latestDate} required disabled={isLocked}
                aria-invalid={dateValidation.field === 'end'}
                aria-describedby={dateValidation.field === 'end' ? 'backtest-date-error' : undefined}
                onInput={syncDateInputs} onChange={syncDateInputs} />
            </div>
            {!dateValidation.valid ? (
              <div id="backtest-date-error" role="alert">
                <Notice>{dateValidation.reason} 更正后再点完成，当前输入尚未保存。</Notice>
              </div>
            ) : null}
            <div className="grow setting-row">
              <span data-focus="cash" className="section-anchor" />
              <span className="k"><SettingInfo label="初始资金" /><small>只影响手数、费用与容量，不代表真实账户</small></span>
              <span className="setting-with-unit">
                <NumericInput label="初始资金"
                  min={MINIMUM_INITIAL_CASH_CNY} max={MAXIMUM_INITIAL_CASH_CNY}
                  integer
                  value={draft.backtest.initialCashCny} disabled={isLocked}
                  onValueChange={(value) => updateBacktest('initialCashCny', value)} />
                <small>元</small>
              </span>
            </div>
          </Section>

          <div data-focus="more" className="section-anchor" />
          {!draft.strategySpec.trading_plan ? <Section title="成交与费用" aside="影响能否成交和成交价格">
            <Row label={<SettingInfo label="信号与成交" />} value={signalExecutionSummary(draft)} wrap />
            <div className="grow setting-row">
              <span className="k"><SettingInfo label="涨跌停处理" help={priceLimitHelp[draft.execution.priceLimitMode]} /></span>
              <select className="settings-select" aria-label="涨跌停处理" value={draft.execution.priceLimitMode} disabled={isLocked}
                onChange={(event) => updateExecution('priceLimitMode', event.target.value as PriceLimitMode)}>
                <option value="wait_for_unlock">默认保守</option>
                <option value="strict_no_fill_at_limit">严格不成交</option>
                <option value="allow_limit_volume">限价容量估算</option>
              </select>
            </div>
            <div className="grow setting-row">
              <span className="k"><SettingInfo label="单边比例滑点" /></span>
              <span className="setting-with-unit">
                <NumericInput label="单边滑点" min={0} max={100}
                  value={draft.execution.slippageBps} disabled={isLocked}
                  onValueChange={(value) => updateExecution('slippageBps', value)} />
                <small>基点</small>
              </span>
            </div>
            <div className="grow setting-row">
              <span className="k"><SettingInfo label="单边固定价差" /></span>
              <span className="setting-with-unit">
                <NumericInput label="单边固定价差" min={0}
                  value={draft.execution.slippageCny ?? 0} disabled={isLocked}
                  onValueChange={(value) => updateExecution('slippageCny', value)} />
                <small>元/股</small>
              </span>
            </div>
            <div className="grow setting-row">
              <span className="k"><SettingInfo label="佣金率" /></span>
              <span className="setting-with-unit">
                <NumericInput label="佣金率" min={0} max={1}
                  value={draft.execution.commissionRate * 100} disabled={isLocked}
                  onValueChange={(value) => updateExecution('commissionRate', value / 100)} />
                <small>%</small>
              </span>
            </div>
            <div className="grow setting-row">
              <span className="k"><SettingInfo label="最低佣金" /></span>
              <span className="setting-with-unit">
                <NumericInput label="最低佣金" min={0}
                  value={draft.execution.minimumCommissionCny} disabled={isLocked}
                  onValueChange={(value) => updateExecution('minimumCommissionCny', value)} />
                <small>元</small>
              </span>
            </div>
          </Section> : null}

          {!draft.strategySpec.trading_plan ? <details className="advanced-settings" open>
            <summary>
              <span>高级研究设置</span>
              <small>容量、仓位、退出重试与预热</small>
            </summary>
            <Section title="研究执行参数" aside="默认值已适合 Demo">
              <div className="grow setting-row">
                <span className="k">后续买入</span>
                <span>{draft.strategySpec.execution.position_policy === 'accumulate_on_new_entry_signal'
                  ? '每次新触发可继续买入；条件持续成立不重复买入'
                  : '持仓未清空时不再买入'}</span>
              </div>
              <div className="grow setting-row">
                <span className="k"><SettingInfo label="成交容量模式" /><small>缺少可验证容量时不会自动放宽</small></span>
                <select className="settings-select" aria-label="成交容量模式"
                  value={draft.execution.capacityMode} disabled={isLocked}
                  onChange={(event) => updateExecution('capacityMode', event.target.value as CapacityMode)}>
                  <option value="point_in_time_volume">默认：时点容量</option>
                  <option value="unlimited">研究：不设上限</option>
                </select>
              </div>
              <div className="grow setting-row">
              <span className="k"><SettingInfo label="成交量参与率" /></span>
                <span className="setting-with-unit">
                  <NumericInput label="成交量参与率"
                    min={0.01} max={100} value={draft.execution.participationRate * 100}
                    disabled={isLocked || draft.execution.capacityMode === 'unlimited'}
                    onValueChange={(value) => updateExecution('participationRate', value / 100)} />
                  <small>%</small>
                </span>
              </div>
              <div className="grow setting-row">
              <span className="k"><SettingInfo label="每次买入资金比例" /></span>
                <span className="setting-with-unit">
                  <NumericInput label="每次买入资金比例"
                    min={0.01} max={100} value={draft.execution.allocationRatio * 100}
                    disabled={isLocked}
                    onValueChange={(value) => updateExecution('allocationRatio', value / 100)} />
                  <small>%</small>
                </span>
              </div>
              <div className="grow setting-row">
              <span className="k"><SettingInfo label="卖出未成交后重试" /></span>
                <select className="settings-select" aria-label="卖出未成交后重试"
                  value={draft.execution.retryUnfilledExits ? 'yes' : 'no'} disabled={isLocked}
                  onChange={(event) => updateExecution('retryUnfilledExits', event.target.value === 'yes')}>
                  <option value="yes">开启</option>
                  <option value="no">关闭</option>
                </select>
              </div>
              <div className="grow setting-row">
              <span className="k"><SettingInfo label="最多卖出尝试次数" /></span>
                <NumericInput label="最多卖出尝试次数"
                  min={1} max={1000} integer value={draft.execution.maxExitAttempts}
                  disabled={isLocked || !draft.execution.retryUnfilledExits}
                  onValueChange={(value) => updateExecution('maxExitAttempts', value)} />
              </div>
              <div className="grow setting-row">
              <span className="k"><SettingInfo label="指标预热日历天数" /></span>
                <NumericInput label="指标预热日历天数"
                  min={0} max={3650} integer value={draft.execution.warmupCalendarDays}
                  disabled={isLocked}
                  onValueChange={(value) => updateExecution('warmupCalendarDays', value)} />
              </div>
              <div className="grow setting-row">
              <span className="k"><SettingInfo label="结算延长日历天数" /></span>
                <NumericInput label="结算延长日历天数"
                  min={1} max={365} integer value={draft.execution.settlementExtensionDays}
                  disabled={isLocked}
                  onValueChange={(value) => updateExecution('settlementExtensionDays', value)} />
              </div>
              <div className="grow setting-row">
              <span className="k"><SettingInfo label="稳健性检查" /></span>
                <select className="settings-select" aria-label="稳健性检查"
                  value={draft.execution.runRobustness ? 'yes' : 'no'} disabled={isLocked}
                  onChange={(event) => updateExecution('runRobustness', event.target.value === 'yes')}>
                  <option value="yes">开启</option>
                  <option value="no">关闭</option>
                </select>
              </div>
            </Section>
          </details> : null}

          </fieldset>
          </NumericInputGroup>

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
  filled: { label: '已成', hint: '已记录模拟成交', brief: '成交价未记录', settled: true },
  partial: { label: '部成', hint: '只成交了一部分，剩余数量没有成交', brief: '成交价未记录', settled: true },
  unfilled: { label: '未成', hint: '当天没有成交，可能受涨跌停、容量或次日可卖限制', brief: '未成交', settled: false },
  expired: { label: '已失效', hint: '委托有效期已结束；如有部分成交，已成交部分仍有效，剩余数量不再成交', brief: '已失效', settled: false },
}

/** 列表只显示「哪天几点」，不改变原始活动的时间精度。 */
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
  eastmoney_mx_finance_data: '东方财富查数 Skill',
  eastmoney: '东方财富公告',
  ifind: '同花顺 iFinD',
  rqdata: 'RQData',
  tushare: 'Tushare',
  web_archive: '网页存档',
  choice: 'Choice 行情快照',
  mock_sample: '固定样例',
}

const timeQualityLabels: Record<string, string> = {
  daily_close_simulation: '日线收盘模拟时点，不是供应商实测发布时刻',
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
  local_formula_on_provider_ohlcv: '基于 Skill 原始日线按明确周期计算',
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
  timeQuality: '成交时间口径',
  capacity: '实际成交容量',
  producerSnapshotId: '来源快照',
  corporateActions: '公司行动数据状态',
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
  slippage_cny: '单边固定价差',
}

const percentValue = (value: string) => {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? `${(parsed * 100).toFixed(2).replace(/\.00$/u, '')}%` : '已记录'
}

const executionAssumptionValue = (key: string, value: string) => {
  if (key === 'timeQuality') {
    return ({ minute_bar_end_proxy: '分钟K线末端时间代理（非真实逐笔成交）',
      daily_bar_proxy: '日线开盘价代理（时间非精确）' } as Record<string, string>)[value]
      ?? `记录口径：${value}`
  }
  if (key === 'producerSnapshotId') return value
  if (key === 'corporateActions') {
    return value === 'source_export_loaded' ? '已加载来源导出记录；不代表全部历史完整'
      : value === 'unknown' ? '尚未确认' : `记录状态：${value}`
  }
  if (key === 'capacity') {
    const [basis, ratio] = value.split(':')
    const label = basis === 'previous_completed_minute_volume' ? '上一已完成分钟成交量'
      : basis === 'first_completed_minute_volume_for_opening_order_else_previous_completed_minute_volume'
        ? '首笔开盘委托按首根完整分钟成交量，后续按上一已完成分钟成交量'
      : basis === 'previous_completed_session_volume' ? '上一已完成交易日成交量' : null
    if (label && ratio && Number.isFinite(Number(ratio))) return `${label} × ${percentValue(ratio)}`
    return value === 'unlimited_ohlc_research' ? '不设容量上限（OHLC研究模式）' : `记录口径：${value}`
  }
  if (key === 'allocation_ratio' || key === 'commission_rate' || key === 'participation_rate') {
    return percentValue(value)
  }
  if (key === 'slippage_bps') return `${value} 基点`
  if (key === 'slippage_cny') return `${value} 元/股`
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

// Input bundles are not composite Parquet snapshots. Validate each declared
// content-addressed schema without treating a recorded input as a release.
const hasContentAddressedInputIdentity = (evidence: RunEvidence) => {
  const prefix = evidence.dataSchemaVersion === 'mx-share-input.v1' ? 'mx-share:'
    : evidence.dataSchemaVersion === 'price-plan-input.v1' ? 'price-plan-input:' : null
  if (!prefix || !evidence.snapshotId?.startsWith(prefix)) return false
  const digest = evidence.snapshotId.slice(prefix.length)
  return /^[0-9a-f]{64}$/.test(digest)
    && evidence.snapshotChecksum === `sha256:${digest}`
    && SHA256_IDENTITY.test(evidence.catalog ?? '')
    && SHA256_IDENTITY.test(evidence.strategyHash ?? '')
}
const identityLabelFor = (evidence: RunEvidence) => evidence.skillData ? 'Skill 来源已记录'
  : hasCompleteIdentity(evidence) ? '身份已记录'
  : hasContentAddressedInputIdentity(evidence) ? '数据版本已记录' : '身份不完整'

/**
 * 报告正文。抽出来是为了让它既能就地展开在对话里，也能被二级页包起来——
 * 同一份内容，两种容器，不做第二套实现。
 */
export function ReportBody(
  { metrics, series, marks, trades, evidence,
    onOpenExecution, mode = 'mock', showExecutionEntry = true }:
  { metrics: BacktestMetrics;
    series: SeriesPoint[]; marks: ChartMark[]; trades: TradeRow[]; evidence: RunEvidence;
    onOpenExecution: () => void;
    mode?: 'mock' | 'live';
    /**
     * 「成交规则与数据依据」讲的是这条策略怎么执行，属于工作流而不是结果。
     * 详情页把它放到工作流分区，所以那里传 false；独立的报告页仍然自带这个入口。
     */
    showExecutionEntry?: boolean },
) {
  const [pagination, setPagination] = useState({ runId: evidence.runId, count: 20 })
  const [allOrdersRun, setAllOrdersRun] = useState<string | null>(null)
  const [selection, setSelection] = useState<{
    runId: string;
    orderId: string | null;
    activityId: string | null;
  }>({ runId: evidence.runId, orderId: null, activityId: null })
  const listRef = useRef<HTMLDivElement>(null)
  const identityComplete = hasCompleteIdentity(evidence)
  const identityStatus = identityComplete || evidence.skillData ? 'implemented' : 'partial'
  const identityLabel = identityLabelFor(evidence)

  const orders = useMemo(() => toOrderRows(trades), [trades])
  const secondary = secondaryMetric(metrics)
  const visibleCount = pagination.runId === evidence.runId ? pagination.count : 20
  const selectedOrderId = selection.runId === evidence.runId ? selection.orderId : null
  const selectedMarkId = selection.runId === evidence.runId ? selection.activityId : null
  const filledOrders = orders.filter((order) => order.status === 'filled' || order.status === 'partial')
  const filledCount = filledOrders.length
  const showingAll = allOrdersRun === evidence.runId
  const listedOrders = showingAll ? orders : filledOrders
  const visibleOrders = listedOrders.slice(0, visibleCount)
  const remaining = listedOrders.length - visibleOrders.length
  const unfilledReasons = new Map<string, number>()
  for (const order of orders) {
    if (order.status === 'filled' || order.status === 'partial') continue
    const reason = order.outcomeNote ?? (order.status === 'placed' ? '委托已提交，尚未记录成交结果' : '记录未提供具体原因')
    unfilledReasons.set(reason, (unfilledReasons.get(reason) ?? 0) + 1)
  }

  /** 图上点一个 B/S 点，就把下面列表滚到那一笔并高亮，而不是另开一块明细。 */
  const focusOrder = (activityId: string) => {
    const order = orders.find((item) => item.activityIds.includes(activityId))
    const orderId = order?.id ?? null
    const revealAll = order != null && !filledOrders.includes(order)
    if (revealAll) setAllOrdersRun(evidence.runId)
    const index = (showingAll || revealAll ? orders : filledOrders).findIndex(item => item.id === orderId)
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
        <div className="sect report-flow">
          <section className="report-summary" aria-labelledby="report-conclusion">
            <div className="report-summary-meta">
              <span>{metrics.dataRange.start} 至 {metrics.dataRange.end}</span>
              {mode === 'live' && !evidence.skillData ? <Tag status={identityStatus}>{identityLabel}</Tag> : null}
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
            列表保留关键信息，与图中的买卖点互相定位。
          */}
          <section id="report-trades" aria-labelledby="report-trades-title">
            <div className="sect-h">
              <h2 id="report-trades-title">每笔委托</h2>
              <em>{orders.length} 笔 · 成交 {filledCount} 笔</em>
            </div>
            {orders.length > filledCount ? <div className="order-outcomes">
              <details>
                <summary>{orders.length - filledCount} 笔尚无成交，查看原因汇总</summary>
                <ul>{[...unfilledReasons].map(([reason, count]) => <li key={reason}>{reason} · {count} 笔</li>)}</ul>
              </details>
              <button type="button" className="order-more" onClick={() => {
                setAllOrdersRun(showingAll ? null : evidence.runId)
                setPagination({ runId: evidence.runId, count: 20 })
              }}>{showingAll ? '只看有成交的委托' : `查看全部委托（${orders.length}笔）`}</button>
            </div> : null}
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
              {orders.length > 0 && !showingAll && filledCount === 0 ? <div className="empty-row">
                本次没有成交。未成交原因已汇总在上方，可查看全部委托核对；未隐藏或删除原始记录。
              </div> : null}
              {visibleOrders.map((order) => {
                const status = ORDER_STATUS[order.status]
                const active = order.id === selectedOrderId
                const stamp = order.orderAt ?? order.filledAt ?? order.signalAt
                const stampKind = order.orderAt ? '委托' : order.filledAt ? '成交' : '信号'
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
                    }}
                  >
                    <span className="oc oc-side">
                      <b className={order.side}>{order.side === 'buy' ? '买入' : '卖出'}</b>
                      <small title={stamp ? `${stampKind}时间：${stamp}${order.filledAt ? `；成交时间：${order.filledAt}` : ''}` : undefined}>{stamp ? `${stampKind} ${shortStamp(stamp)}` : '时间未记录'}</small>
                    </span>
                    <span className="oc oc-signal">
                      <b>{order.title}</b>
                      <small>{order.title === '定期计划触发' ? '按计划日期执行，休市顺延'
                        : order.signalAt ? `${shortDate(order.signalAt)} 确认` : '信号未记录'}</small>
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
                    {active && order.outcomeNote ? <span className="order-outcome-note">{order.outcomeNote}</span> : null}
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

          {showExecutionEntry ? (
            <Section title="更多信息">
              <Row label="成交规则与数据依据" sub="涨跌停、费用、信号来源与运行身份"
                onClick={onOpenExecution} />
            </Section>
          ) : null}
          {metrics.executionNote ? (
            <section className="report-risk" aria-label="回测说明与风险提示">
              <h3>回测说明与风险提示</h3>
              <p>{metrics.executionNote}</p>
            </section>
          ) : null}
        </div>
  )
}

/** 「成交规则与数据依据」入口。详情页放在工作流分区，报告页放在末尾。 */
export function ExecutionEntry({ onOpen }: { onOpen: () => void }) {
  return (
    <Section title="更多信息">
      <Row label="成交规则与数据依据" sub="涨跌停、费用、信号来源与运行身份" onClick={onOpen} />
    </Section>
  )
}

/** 报告的二级页容器。App 现在就地展开报告，这个包装留给独立打开报告的场景。 */
export function ReportScreen(
  { open, onBack, ...body }:
  { open: boolean; onBack: () => void } & Parameters<typeof ReportBody>[0],
) {
  return (
    <Page id="report" title="回测报告" open={open} onBack={onBack}>
      <div className="scroll">
        <ReportBody {...body} />
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
  const identityStatus = identityComplete || evidence.skillData ? 'implemented' : 'partial'
  const identityLabel = identityLabelFor(evidence)
  const validityRows = entryValidityRows(evidence.executionAssumptions)

  // 只保留真正有值的身份项；缺失的合并成一句说明，不用一行一个「未记录」占屏。
  const identityFields = evidence.skillData ? [
    { label: '数据来源', value: '东方财富选股 / 查数 Skill' },
    { label: '股票代码', value: evidence.skillData.instrumentId },
    { label: '历史数据', value: `${evidence.skillData.historyStart} 至 ${evidence.skillData.historyEnd} · ${evidence.skillData.historyRows} 行` },
    { label: 'Skill 直接指标', value: `${evidence.skillData.indicatorSeries} 组 · ${evidence.skillData.indicatorPoints} 个数据点` },
    ...(evidence.skillData.derivedIndicatorEvidence ?? []).map((item) => ({
      label: `本地计算 · ${({ 'price.rolling_high': '滚动新高', 'volume.relative': '相对成交量', 'technical.ma': '均线' } as Record<string, string>)[item.indicatorId] ?? item.indicatorId}`,
      value: `${item.formula}；参数 ${item.parameters}`,
    })),
    { label: '取数时间', value: evidence.skillData.retrievedAt },
    { label: '收益口径', value: '供应商复权序列模拟收益；不是逐笔分红到账的实盘账户' },
  ] : [
    { label: '代码版本', value: evidence.gitSha },
    { label: '数据快照', value: evidence.snapshotId },
    ...(evidence.dataSchemaVersion === 'mx-share-input.v1' ? [] : [
      { label: '生产快照', value: evidence.producerSnapshotId },
      { label: '快照生成结构版本', value: evidence.producerSnapshotSchemaVersion },
    ]),
    { label: '数据结构版本', value: evidence.dataSchemaVersion },
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
          <Row label="信号与成交" value={signalExecutionSummary(draft)} wrap />
          <Row label="涨跌停" value={priceLimitLabel[draft.execution.priceLimitMode]} wrap />
          <Row label="滑点与佣金" sub={`最低佣金 ${draft.execution.minimumCommissionCny} 元`}
            value={`${draft.execution.slippageBps} 基点${draft.execution.slippageCny ? ` + ${draft.execution.slippageCny} 元/股` : ''} · ${(draft.execution.commissionRate * 100).toFixed(3)}%`} wrap />
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
          {evidence.gitSha?.endsWith('+dirty') ? <Row label="代码状态"
            value="本地修改版，尚未固定为发布版本；数据版本记录不代表已通过发布验收。" wrap /> : null}
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
            {evidence.skillData
              ? '本次指标来自东方财富查数 Skill，按收盘确认、次日开盘模拟执行；取数范围和时间见上方记录。'
              : '当前结果没有保存信号来源或首次可得时间，不能将来源补写为已验证。'}
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
