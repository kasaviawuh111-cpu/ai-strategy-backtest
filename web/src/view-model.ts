import type {
  BacktestActivity,
  BacktestSummary,
  CapabilitiesResponse,
  EquityPoint,
  StrategyDraft,
  StrategyCondition,
  StrategySpecCondition,
  StrategySpecExitRule,
} from './shared/api/types'
import type {
  BacktestMetrics,
  ChainContext,
  ChainFact,
  ChainNode,
  ChartMark,
  Instrument,
  OrderRow,
  OrderStatus,
  RunEvidence,
  SeriesPoint,
  StrategyCapabilitySummary,
  StrategyEventFact,
  StrategyRuleNode,
  StrategySummary,
  TradeRow,
} from './types'

const percent = (value: number | null) => value == null ? null : value * 100

const operatorLabels = {
  all: '以下条件全部满足',
  any: '以下任一条件满足',
  first_of: '以下任一退出条件先发生',
  not: '以下条件不成立',
} as const

const eventAttributeLabels: Record<string, string> = {
  report_period: '报告期',
  replay_available_at: '首次可得时间',
  vendor_first_available_at: '供应商首次可得时间',
  first_available_at: '首次可得时间',
  available_at: '首次可得时间',
  time_quality: '时间质量',
  provider: '来源',
  source_url: '来源链接',
}

const parameterText = (condition: StrategyCondition): string[] => {
  if (condition.kind === 'indicator') {
    return condition.parameters.map((parameter) =>
      `${parameter.label} ${parameter.value}${parameter.unit ?? ''}`)
  }
  if (condition.kind === 'holding_period') {
    return [`持有 ${condition.sessions} 个 A 股交易日`, '从首次实际买入成交后开始计数']
  }
  if (condition.kind === 'position_return') {
    return [
      `${condition.exitTrigger === 'take_profit' ? '止盈' : '止损'}阈值 ${condition.thresholdPct}%`,
      '首次实际买入成交为基准 · 后复权日线收盘确认',
      '当前参数只读；修改请返回原话重新识别',
    ]
  }
  if (condition.kind === 'trailing_drawdown') {
    return [
      `高点回撤阈值 ${condition.thresholdPct}%`,
      '买入后后复权日线收盘高点为基准 · 收盘确认',
      '当前参数只读；修改请返回原话重新识别',
    ]
  }
  if (condition.kind === 'financial') {
    const unit = condition.unit === 'PERCENT' ? '%' : ''
    return [
      `条件 ${condition.comparator} ${condition.value}${unit}`,
      '历史当时可得的接口原始值',
    ]
  }
  const attributes = Object.entries(condition.attributes).map(([key, value]) =>
    `${eventAttributeLabels[key] ?? key.replaceAll('_', ' ')}：${String(value)}`)
  if (condition.documentText) {
    const predicate = condition.documentText
    return [
      `正文完整词“${predicate.term}”`,
      `计数 ${predicate.comparator === 'gte' ? '≥' : '>'} ${predicate.value} 次`,
      `${predicate.normalization.toUpperCase()} · ${predicate.match_mode}`,
      ...attributes,
    ]
  }
  return attributes
}

const fallbackLeaf = (label: string, detail: string, id: string): StrategyRuleNode => ({
  kind: 'leaf', id, label, detail, tone: 'indicator', parameters: [],
})

const leafNode = (
  condition: StrategyCondition | undefined,
  id: string,
  fallbackLabel: string,
): StrategyRuleNode => condition ? {
  kind: 'leaf',
  id,
  label: condition.label,
  detail: condition.trigger,
  tone: condition.kind === 'event' ? 'event'
    : condition.kind === 'indicator' || condition.kind === 'financial' ? 'indicator' : 'holding',
  parameters: parameterText(condition),
} : fallbackLeaf(fallbackLabel, '完整定义保留在服务端最终策略规则中。', id)

const buildConditionRule = (
  condition: StrategySpecCondition,
  leaves: StrategyCondition[],
  cursor: { value: number },
  path: string,
): StrategyRuleNode => {
  if (
    condition.type === 'indicator_condition'
    || condition.type === 'financial_condition'
    || condition.type === 'event_condition'
  ) {
    const leaf = leaves[cursor.value]
    cursor.value += 1
    const fallback = condition.type === 'event_condition'
      ? condition.event_code
      : condition.type === 'financial_condition'
        ? condition.metric_id
        : condition.indicator_id
    return leafNode(leaf, path, fallback)
  }
  if (condition.type === 'not') {
    return {
      kind: 'group', id: path, operator: 'not', label: operatorLabels.not,
      children: [buildConditionRule(condition.child, leaves, cursor, `${path}-not`)],
    }
  }
  return {
    kind: 'group',
    id: path,
    operator: condition.type,
    label: operatorLabels[condition.type],
    children: condition.children.map((child, index) =>
      buildConditionRule(child, leaves, cursor, `${path}-${index}`)),
  }
}

const buildExitRule = (
  condition: StrategySpecExitRule,
  leaves: StrategyCondition[],
  cursor: { value: number },
  path: string,
): StrategyRuleNode => {
  if (
    condition.type !== 'holding_period_exit'
    && condition.type !== 'position_return_exit'
    && condition.type !== 'trailing_drawdown_exit'
  ) {
    return buildConditionRule(condition, leaves, cursor, path)
  }
  const leaf = leaves[cursor.value]
  cursor.value += 1
  const fallbackLabel = condition.type === 'holding_period_exit'
    ? `持有 ${condition.sessions} 个交易日后退出`
    : condition.type === 'position_return_exit'
      ? `${condition.trigger === 'take_profit' ? '止盈' : '止损'} ${condition.threshold_pct}%`
      : `持仓后高点回撤 ${condition.threshold_pct}% 退出`
  return leafNode(leaf, path, fallbackLabel)
}

export const strategyRuleTrees = (draft: StrategyDraft) => {
  const entryCursor = { value: 0 }
  const exitCursor = { value: 0 }
  return {
    entry: buildConditionRule(
      draft.strategySpec.entry,
      draft.entry.conditions,
      entryCursor,
      'entry',
    ),
    exit: {
      kind: 'group',
      id: 'exit',
      operator: 'first_of',
      label: operatorLabels.first_of,
      children: draft.strategySpec.exit.children.map((condition, index) =>
        buildExitRule(condition, draft.exit.conditions, exitCursor, `exit-${index}`)),
    } satisfies StrategyRuleNode,
  }
}

export const summarizeRule = (rule: StrategyRuleNode): string => {
  if (rule.kind === 'leaf') return rule.label
  const text = rule.children.map(summarizeRule)
  if (rule.operator === 'not') return `非（${text[0] ?? '条件'}）`
  if (text.length === 1) return text[0] ?? rule.label
  if (rule.operator === 'first_of') return `任一先发生（${text.join(' / ')}）`
  const joiner = rule.operator === 'all' ? ' 且 ' : ' 或 '
  return `（${text.join(joiner)}）`
}

const attributeString = (
  attributes: Record<string, string | number | boolean>,
  keys: string[],
): string | null => {
  for (const key of keys) {
    const value = attributes[key]
    if (typeof value === 'string' && value.trim()) return value
  }
  return null
}

const eventFacts = (draft: StrategyDraft): StrategyEventFact[] => [
  ...draft.entry.conditions,
  ...draft.exit.conditions,
].flatMap((condition) => {
  if (condition.kind !== 'event') return []
  const firstAvailableAt = attributeString(condition.attributes, [
    'replay_available_at', 'vendor_first_available_at', 'first_available_at', 'available_at',
  ])
  const timeQuality = attributeString(condition.attributes, ['time_quality', 'timeQuality'])
  const provider = attributeString(condition.attributes, ['provider', 'source_provider', 'source'])
  const sourceUrl = attributeString(condition.attributes, ['source_url', 'sourceUrl'])
  return [{
    id: condition.id,
    label: condition.label,
    eventCode: condition.eventCode,
    firstAvailableAt,
    timeQuality,
    provider,
    sourceUrl,
    note: firstAvailableAt && timeQuality && provider
      ? '这些字段来自服务端策略属性；逐次触发仍以运行活动证据为准。'
      : '策略草稿只固定事件定义；具体来源、时间质量和首次可得时间在运行活动中返回，前端不补写。',
  }]
})

type CollectedSpecIds = {
  indicators: string[]
  events: string[]
  documentTextEvents: string[]
}

const collectSpecIds = (condition: StrategySpecCondition): CollectedSpecIds => {
  if (condition.type === 'indicator_condition') {
    return { indicators: [condition.indicator_id], events: [], documentTextEvents: [] }
  }
  if (condition.type === 'event_condition') {
    return {
      indicators: [],
      events: [condition.event_code],
      documentTextEvents: condition.document_text ? [condition.event_code] : [],
    }
  }
  if (condition.type === 'financial_condition') {
    return { indicators: [], events: [], documentTextEvents: [] }
  }
  if (condition.type === 'not') return collectSpecIds(condition.child)
  return condition.children.reduce((result, child) => {
    const next = collectSpecIds(child)
    result.indicators.push(...next.indicators)
    result.events.push(...next.events)
    result.documentTextEvents.push(...next.documentTextEvents)
    return result
  }, { indicators: [], events: [], documentTextEvents: [] } as CollectedSpecIds)
}

const strategyIds = (draft: StrategyDraft) => {
  const entry = collectSpecIds(draft.strategySpec.entry)
  const exit = draft.strategySpec.exit.children.reduce((result, condition) => {
    if (
      condition.type === 'holding_period_exit'
      || condition.type === 'position_return_exit'
      || condition.type === 'trailing_drawdown_exit'
    ) return result
    const next = collectSpecIds(condition)
    result.indicators.push(...next.indicators)
    result.events.push(...next.events)
    result.documentTextEvents.push(...next.documentTextEvents)
    return result
  }, { indicators: [], events: [], documentTextEvents: [] } as CollectedSpecIds)
  return {
    indicators: [...new Set([...entry.indicators, ...exit.indicators])],
    events: [...new Set([...entry.events, ...exit.events])],
    documentTextEvents: [
      ...new Set([...entry.documentTextEvents, ...exit.documentTextEvents]),
    ],
  }
}

export const assessStrategyCapabilities = (
  draft: StrategyDraft,
  capabilities: CapabilitiesResponse | undefined,
  mode: 'mock' | 'live',
): StrategyCapabilitySummary => {
  const ids = strategyIds(draft)
  const eventLabel = (eventCode: string) => {
    const condition = [...draft.entry.conditions, ...draft.exit.conditions]
      .find((item) => item.kind === 'event' && item.eventCode === eventCode)
    return condition?.label.split('正文')[0]?.replace(/发布$/, '') || eventCode
  }
  if (mode === 'mock') {
    return {
      canRun: true,
      needsPreparation: false,
      reason: '这里只播放固定样例，不代表后台已完成规则理解、数据准备或回测计算。',
      eventCodes: ids.events,
      events: ids.events.map((eventCode) => ({
        eventCode,
        label: eventLabel(eventCode),
        catalog: 'demo',
        preparable: 'demo',
        pinnedSnapshot: 'demo',
        detail: '固定样例，不代表后台已有该事件数据。',
      })),
      stages: [
        { key: 'understand', label: '识别规则', state: 'demo', detail: '界面预览' },
        { key: 'prepare', label: '准备数据', state: 'demo', detail: '固定样例' },
        { key: 'snapshot', label: '运行回测', state: 'demo', detail: '固定样例' },
      ],
    }
  }
  if (!capabilities) {
    return {
      canRun: false,
      needsPreparation: false,
      reason: '暂时读不到后端能力说明，无法确认这条策略能否安全运行。',
      eventCodes: ids.events,
      events: ids.events.map((eventCode) => ({
        eventCode,
        label: eventLabel(eventCode),
        catalog: 'unknown',
        preparable: 'unknown',
        pinnedSnapshot: 'unknown',
        detail: '等待后端能力说明。',
      })),
      stages: [
        { key: 'understand', label: '能理解', state: 'available', detail: '服务端已生成最终策略规则' },
        { key: 'prepare', label: '能准备', state: 'unknown', detail: '等待能力说明' },
        { key: 'snapshot', label: '当前可跑', state: 'unknown', detail: '等待能力说明' },
      ],
    }
  }
  const indicators = new Map(capabilities.indicators.map((item) => [item.indicator_id, item]))
  const events = new Map(capabilities.events.map((item) => [item.event_code, item]))
  const documentTextEvents = new Set(ids.documentTextEvents)
  const eventRequirement = (eventCode: string) => {
    const item = events.get(eventCode)
    return documentTextEvents.has(eventCode) ? item?.document_text : item
  }
  const documentTextCatalogUnavailable = ids.documentTextEvents.some((eventCode) => {
    const item = events.get(eventCode)
    return item != null && item.document_text?.catalog_available !== true
  })
  const declared = ids.indicators.every((id) => {
    const item = indicators.get(id)
    return item?.status === 'stable' || item?.status === 'experimental'
  }) && ids.events.every((id) => {
    const item = events.get(id)
    return item != null
      && (!documentTextEvents.has(id) || item.document_text?.catalog_available === true)
  })
  const eventCapabilities = ids.events.map(eventRequirement)
  const snapshotBacked = ids.events.length === 0
    || eventCapabilities.every((item) => item?.backtest_available === true
      && item.availability_scope === 'pinned_snapshot')
  const canPrepare = ids.events.length === 0
    || eventCapabilities.every((item) => (
      item?.backtest_available === true && item.availability_scope === 'pinned_snapshot'
    ) || (
      item?.preparation_available === true && item.availability_scope === 'request_preparation'
    ))
  const needsPreparation = ids.events.length > 0 && canPrepare && !snapshotBacked
  const canRun = declared && capabilities.backtest_execution_available && canPrepare
  const reason = !declared
    ? documentTextCatalogUnavailable
      ? '规则已经由服务端生成，但能力接口没有声明该公告正文词频定义可用于回测。'
      : '规则已经由服务端生成，但能力接口尚未声明全部定义可用于提交回测。'
    : !capabilities.backtest_execution_available
      ? '后端当前没有可用的回测执行环境。规则可以查看，但不能提交运行。'
      : !canPrepare
        ? ids.documentTextEvents.length > 0
          ? '公告事件能被识别，但完整正文当前没有固定快照，后端也没有声明可按本次请求准备。'
          : '这类公告能被识别，但当前快照没有覆盖，后端也没有声明可按本次请求准备。'
        : needsPreparation
          ? '当前固定快照尚未覆盖；提交后后端会先按股票、区间和事件类型准备并校验数据。'
          : null
  return {
    canRun,
    needsPreparation,
    reason,
    eventCodes: ids.events,
    events: ids.events.map((eventCode) => {
      const item = events.get(eventCode)
      const requiresDocumentText = documentTextEvents.has(eventCode)
      const requirement = eventRequirement(eventCode)
      const pinned = requirement?.backtest_available === true
        && requirement.availability_scope === 'pinned_snapshot'
      const preparable = pinned || (requirement?.preparation_available === true
        && requirement.availability_scope === 'request_preparation')
      const catalog = !item
        ? 'unknown'
        : requiresDocumentText
          ? item.document_text?.catalog_available === true ? 'available' : 'unavailable'
          : 'available'
      return {
        eventCode,
        label: eventLabel(eventCode),
        catalog,
        preparable: preparable ? pinned ? 'available' : 'conditional' : 'unavailable',
        pinnedSnapshot: pinned ? 'available' : 'unavailable',
        detail: !item
          ? '最终策略规则已生成，但能力目录没有返回这一事件的可运行状态。'
          : requiresDocumentText && item.document_text?.catalog_available !== true
            ? '事件定义已存在，但能力接口没有声明公告正文词频 Catalog。'
          : pinned
              ? requiresDocumentText
                ? '当前固定快照已覆盖事件与完整正文。'
                : '当前固定快照已覆盖。'
              : preparable
                ? requiresDocumentText
                  ? '事件已识别；完整正文需按本次请求准备并校验。'
                  : '当前快照未覆盖；后端声明可按本次请求准备。'
                : requiresDocumentText
                  ? '事件已识别，但完整正文当前无覆盖，也没有请求准备能力。'
                  : '当前快照无覆盖，也没有请求准备能力。',
      }
    }),
    stages: [
      {
        key: 'understand', label: '能理解', state: 'available',
        detail: '服务端已生成最终策略规则',
      },
      {
        key: 'prepare', label: '能准备',
        state: canPrepare ? needsPreparation ? 'conditional' : 'available' : 'unavailable',
        detail: ids.events.length === 0 ? '无需事件采集' : needsPreparation ? '按请求准备' : canPrepare ? '已有覆盖' : '没有准备能力',
      },
      {
        key: 'snapshot', label: '当前可跑',
        state: !declared || !capabilities.backtest_execution_available
          ? 'unavailable'
          : snapshotBacked ? 'available' : canPrepare ? 'conditional' : 'unavailable',
        detail: !declared
          ? '能力目录尚未声明'
          : !capabilities.backtest_execution_available
          ? '执行环境不可用'
          : ids.events.length === 0
            ? '提交时校验股票区间'
            : snapshotBacked ? '固定快照已覆盖' : canPrepare ? '准备通过后可跑' : '当前无覆盖',
      },
    ],
  }
}

export const toUiInstrument = (draft: StrategyDraft): Instrument => ({
  name: draft.instrument.name,
  code: draft.instrument.symbol,
})

/**
 * 回测区间的说法。
 * 「2021—2026」看起来像跨了六年，实际只有五年；用户真正想知道的是「跑了多久」。
 * 所以卡片上只写时长；精确起止日期在参数页里，要核对的人点进去看。
 */
export const describeBacktestWindow = (start: string, end: string) => {
  const exact = `${start} 至 ${end}`
  const from = Date.parse(`${start}T00:00:00Z`)
  const to = Date.parse(`${end}T00:00:00Z`)
  if (!Number.isFinite(from) || !Number.isFinite(to) || to <= from) {
    return { value: exact, sub: undefined }
  }
  const days = (to - from) / 86_400_000
  if (days < 60) return { value: `${Math.round(days)} 天`, sub: exact }
  const months = days / 30.44
  if (months < 22) return { value: `近 ${Math.round(months)} 个月`, sub: exact }
  const years = days / 365.25
  // 4.98 年写成「近 5 年」；1.5 年不四舍五入成 2 年，保留一位小数。
  const rounded = Math.abs(years - Math.round(years)) <= 0.12
    ? String(Math.round(years))
    : (Math.round(years * 10) / 10).toFixed(1)
  return { value: `近 ${rounded} 年`, sub: exact }
}

/**
 * 策略卡上「成交设置」那一行的右侧状态。
 *
 * 这一行不负责在首页把成交规则讲一遍——讲三行字既占地方，也没人在确认策略时读。
 * 它只需要回答两件事：这里可以改；现在是不是动过。想知道具体规则、想改，点进去。
 * 对照基准是「识别出来的那一版」，所以「已调整」= 用户自己改过的项。
 */
export const summarizeExecution = (
  draft: StrategyDraft,
  baseline?: StrategyDraft,
) => {
  if (!baseline) return '默认'
  const keys = Object.keys(draft.execution) as Array<keyof StrategyDraft['execution']>
  const changed = keys.filter((key) => draft.execution[key] !== baseline.execution[key]).length
  return changed === 0 ? '默认' : `已调整 ${changed} 项`
}

export const toStrategySummary = (draft: StrategyDraft): StrategySummary => {
  const rules = strategyRuleTrees(draft)
  const hasEvent = [...draft.entry.conditions, ...draft.exit.conditions]
    .some((condition) => condition.kind === 'event')
  return {
    title: draft.title.replace(/\s*·\s*日线$/, ''),
    rows: [
    { key: 'entry', label: '买入', value: summarizeRule(rules.entry), kind: 'buy' },
    { key: 'exit', label: '卖出', value: summarizeRule(rules.exit), kind: 'sell' },
    {
      key: 'range',
      label: '区间',
      value: describeBacktestWindow(draft.backtest.start, draft.backtest.end).value,
    },
    ],
    strategyHash: draft.strategyHash ?? `${draft.id}@r${draft.revision}`,
    entryRule: rules.entry,
    exitRule: rules.exit,
    confirmation: hasEvent ? '公告首次可得时间确认' : '日线收盘确认',
    earliestExecution: hasEvent
      ? '按 09:15 截止规则选择可用的日线开盘价代理；时间非精确'
      : '下一可交易日使用开盘价代理尝试成交',
    conditionCount: draft.entry.conditions.length + draft.exit.conditions.length,
    eventFacts: eventFacts(draft),
  }
}

export const toBacktestMetrics = (summary: BacktestSummary): BacktestMetrics => {
  const total = percent(summary.totalReturn)
  const bench = percent(summary.benchmarkReturn)
  // Old persisted results predate the audited comparison field.  Treat them
  // as unavailable instead of reconstructing an excess return on the client.
  const benchmarkComparisonStatus = summary.benchmarkComparisonStatus ?? 'benchmark_unavailable'
  return {
    total,
    bench,
    excess: benchmarkComparisonStatus === 'comparable' && total != null && bench != null
      ? total - bench
      : null,
    benchmarkComparisonStatus,
    mdd: percent(summary.maxDrawdown),
    trips: summary.tradeCount,
    win: percent(summary.winRate),
    sharpe: summary.sharpeRatio,
    ann: percent(summary.annualizedReturn),
    initialCashCny: summary.initialCashCny,
    finalEquityCny: summary.finalEquityCny,
    interpretation: summary.interpretation,
    dataRange: summary.dataRange,
    warnings: summary.warnings,
    openShares: null,
  }
}

export const toSeries = (points: EquityPoint[]): SeriesPoint[] => {
  if (points.length === 0) return []
  const strategyBase = points[0]?.equity ?? 0
  const benchmarkBase = points.find((point) => point.benchmark != null)?.benchmark ?? null
  return points.map((point) => ({
    date: point.date,
    equity: point.equity,
    strategy: strategyBase > 0 ? (point.equity / strategyBase - 1) * 100 : 0,
    benchmark: point.benchmark != null && benchmarkBase != null && benchmarkBase > 0
      ? (point.benchmark / benchmarkBase - 1) * 100
      : null,
    drawdown: point.drawdown * 100,
  }))
}

const nearestSeriesIndex = (series: SeriesPoint[], occurredAt: string) => {
  const target = Date.parse(occurredAt)
  if (!Number.isFinite(target)) return 0
  let bestIndex = 0
  let bestDistance = Number.POSITIVE_INFINITY
  series.forEach((point, index) => {
    const normalized = point.date.length === 7 ? `${point.date}-01` : point.date
    const pointTime = Date.parse(`${normalized}T00:00:00+08:00`)
    const distance = Math.abs(pointTime - target)
    if (distance < bestDistance) {
      bestDistance = distance
      bestIndex = index
    }
  })
  return bestIndex
}

export const toChartMarks = (
  series: SeriesPoint[],
  activities: BacktestActivity[],
): ChartMark[] => {
  if (series.length === 0) return []
  return activities.flatMap<ChartMark>((activity) => {
    const isMark = activity.kind === 'fill'
      || activity.kind === 'partial_fill'
      || activity.kind === 'unfilled'
      || activity.kind === 'expired'
    if (!isMark) return []
    const related = relatedActivities(activities, activity)
    const signal = related.find((candidate) => candidate.kind === 'signal')
    const order = [...related].reverse().find((candidate) => candidate.kind === 'order'
      && (!activity.orderId || candidate.orderId === activity.orderId))
    const fill = related.find((candidate) => (candidate.kind === 'fill'
      || candidate.kind === 'partial_fill') && candidate.id === activity.id)
    const common = {
      index: nearestSeriesIndex(series, activity.occurredAt),
      activityId: activity.id,
      side: activity.side,
      title: activity.title,
      occurredAt: activity.occurredAt,
      reason: activity.reason,
      signalAt: signal?.occurredAt ?? null,
      signalReason: signal?.reason ?? null,
      orderAt: order?.occurredAt ?? null,
      fillAt: fill?.occurredAt ?? null,
      price: activity.price ?? null,
      quantity: activity.quantity ?? null,
      priceLimitImpact: priceLimitImpact(related),
      capacityImpact: capacityImpact(activity, related),
      tPlusOneImpact: tPlusOneImpact(related),
      timeQuality: activity.timeQuality,
      timeSemantics: activity.timeSemantics,
      evidence: related.flatMap((candidate) => candidate.evidence ?? []),
    }
    if (activity.kind === 'fill' || activity.kind === 'partial_fill') {
      return [{
        kind: activity.side === 'buy' ? 'buy' as const : 'sell' as const,
        ...common,
      }]
    }
    return [{ kind: 'no_fill' as const, ...common }]
  })
}

export const toTradeRows = (activities: BacktestActivity[]): TradeRow[] =>
  activities.map((activity) => ({
    id: activity.id,
    kind: activity.kind === 'unfilled' ? 'no_fill' : activity.kind,
    side: activity.side,
    occurredAt: activity.occurredAt,
    title: activity.title,
    price: activity.price,
    quantity: activity.quantity,
    reason: activity.reason,
    chainId: activity.chainId,
    decisionId: activity.decisionId,
    orderId: activity.orderId,
    fillId: activity.fillId,
    parentId: activity.parentId,
    capacityReasonCode: activity.capacityReasonCode,
    timeQuality: activity.timeQuality,
    timeSemantics: activity.timeSemantics,
    signalSemantics: activity.signalSemantics,
    signalValiditySessions: activity.signalValiditySessions,
    attemptNo: activity.attemptNo,
    originSignalId: activity.originSignalId,
    retryReason: activity.retryReason,
    outcomeReason: activity.outcomeReason,
    evidence: activity.evidence ?? [],
  }))

const ORDER_STATUS_RANK: Record<OrderStatus, number> = {
  placed: 0, unfilled: 1, expired: 2, partial: 3, filled: 4,
}

const statusOf = (kind: TradeRow['kind']): OrderStatus | null => {
  if (kind === 'fill') return 'filled'
  if (kind === 'partial_fill') return 'partial'
  if (kind === 'no_fill') return 'unfilled'
  if (kind === 'expired') return 'expired'
  if (kind === 'order') return 'placed'
  return null
}

/**
 * 把活动流聚合成「一笔委托一行」。
 *
 * 原先图表下面有一套「上一点 / 选择点位 / 下一点 + 点位详情」，报告下面又有一份「每笔买卖」，
 * 两处讲同一件事，用户要在两套结构里对照。这里让列表成为唯一的清单：
 * 一行 = 一笔委托，从信号到成交的时间都收在行内，状态用柜台术语标注。
 * 图上的 B/S 点只负责定位到对应的那一行。
 */
export const toOrderRows = (trades: TradeRow[]): OrderRow[] => {
  const orderActivities = new Map(
    trades.filter((trade) => trade.kind === 'order').map((trade) => [trade.id, trade]),
  )
  const signals = trades.filter((trade) => trade.kind === 'signal')
  const groups = new Map<string, TradeRow[]>()
  for (const trade of trades) {
    if (!statusOf(trade.kind)) continue
    const parentOrder = trade.parentId ? orderActivities.get(trade.parentId) : undefined
    const key = trade.orderId
      ? `order-id:${trade.orderId}`
      : parentOrder?.orderId
        ? `order-id:${parentOrder.orderId}`
        : parentOrder
          ? `order-activity:${parentOrder.id}`
          : trade.kind === 'order'
            ? `order-activity:${trade.id}`
            : `outcome-activity:${trade.id}`
    const bucket = groups.get(key)
    if (bucket) bucket.push(trade)
    else groups.set(key, [trade])
  }

  const rows: OrderRow[] = []
  for (const group of groups.values()) {
    const ordered = [...group].sort((left, right) =>
      left.occurredAt.localeCompare(right.occurredAt))
    const order = [...ordered].reverse().find((item) => item.kind === 'order')
    const chainId = order?.chainId ?? ordered.find((item) => item.chainId)?.chainId ?? null
    const decisionId = order?.decisionId
      ?? ordered.find((item) => item.decisionId)?.decisionId
      ?? null
    const firstOrderTime = order?.occurredAt ?? ordered[0]?.occurredAt ?? ''
    const signal = signals
      .filter((item) => item.side === (order?.side ?? ordered[0]?.side)
        && item.occurredAt <= firstOrderTime
        && ((chainId && item.chainId === chainId)
          || (!chainId && decisionId && item.decisionId === decisionId)))
      .sort((left, right) => right.occurredAt.localeCompare(left.occurredAt))[0]
    // 同一委托可能先部分成交、再让剩余数量过期；此时整笔仍应显示“部成”。
    const outcome = ordered
      .filter((item) => statusOf(item.kind) && item.kind !== 'order')
      .sort((left, right) =>
        ORDER_STATUS_RANK[statusOf(right.kind) as OrderStatus]
        - ORDER_STATUS_RANK[statusOf(left.kind) as OrderStatus]
        || right.occurredAt.localeCompare(left.occurredAt))[0]
    const anchor = outcome ?? order ?? signal ?? ordered[0]
    if (!anchor) continue
    const status = statusOf(anchor.kind) ?? 'placed'
    const filled = status === 'filled' || status === 'partial'
    const activities = [...(signal ? [signal] : []), ...ordered]
      .filter((item, index, all) => all.findIndex((candidate) => candidate.id === item.id) === index)
      .sort((left, right) => left.occurredAt.localeCompare(right.occurredAt))
    rows.push({
      id: anchor.id,
      activityIds: ordered.map((item) => item.id),
      chainId,
      side: anchor.side,
      status,
      title: signal?.title ?? anchor.title,
      signalAt: signal?.occurredAt ?? null,
      signalReason: signal?.reason ?? null,
      orderAt: order?.occurredAt ?? null,
      filledAt: filled ? anchor.occurredAt : null,
      orderPrice: order?.price ?? null,
      price: filled ? anchor.price ?? null : null,
      quantity: anchor.quantity ?? null,
      outcomeNote: filled ? null : anchor.outcomeReason ?? anchor.reason ?? null,
      timeQuality: anchor.timeQuality,
      timeSemantics: anchor.timeSemantics,
      activities,
    })
  }

  return rows.sort((left, right) => {
    const l = left.orderAt ?? left.signalAt ?? left.filledAt ?? ''
    const r = right.orderAt ?? right.signalAt ?? right.filledAt ?? ''
    return l.localeCompare(r)
  })
}

/**
 * 结果卡与报告顶部的第二个 KPI。
 *
 * 原来这一格放「37 回合 / 完整买卖」。回合数只说明这条策略操作得多不多，
 * 它不回答用户此刻真正的问题——「这策略靠不靠谱」。所以主位让给胜率：
 * 37 次里有多少次是赚钱的。回合数没有丢，它是胜率的分母，降级成注解。
 *
 * 胜率缺失时退到年化收益（把多年的总收益折成每年，便于横向比较），
 * 两个都没有才退回回合数——那时它至少还是一个有值的事实。
 */
export const secondaryMetric = (metrics: BacktestMetrics) => {
  if (metrics.win != null) {
    return {
      value: `${metrics.win.toFixed(metrics.win % 1 === 0 ? 0 : 1)}%`,
      label: '胜率',
      note: `赚钱回合占比 · 共 ${metrics.trips} 个回合`,
    }
  }
  if (metrics.ann != null) {
    return {
      value: `${metrics.ann > 0 ? '+' : ''}${metrics.ann.toFixed(2)}%`,
      label: '年化收益',
      note: '把整段收益折算成每年',
    }
  }
  return { value: `${metrics.trips} 回合`, label: '完整买卖', note: '买进又卖出算一次' }
}

export const toRunEvidence = (summary: BacktestSummary): RunEvidence => ({
  runId: summary.runId,
  gitSha: summary.runEvidence?.codeRevision ?? null,
  snapshotId: summary.runEvidence?.dataSnapshotId ?? null,
  snapshotChecksum: summary.runEvidence?.dataSnapshotChecksum ?? null,
  dataSchemaVersion: summary.runEvidence?.dataSchemaVersion ?? null,
  producerSnapshotSchemaVersion:
    summary.runEvidence?.producerSnapshotSchemaVersion ?? null,
  producerSnapshotId: summary.runEvidence?.producerSnapshotId ?? null,
  catalog: summary.runEvidence?.catalogHash ?? null,
  strategyHash: summary.runEvidence?.strategyHash ?? null,
  engineVersion: summary.runEvidence?.engineVersion ?? null,
  executionAssumptions: summary.runEvidence?.executionAssumptions ?? {},
})

const activityKind = (kind: BacktestActivity['kind']): ChainNode['kind'] =>
  kind === 'unfilled' ? 'no_fill' : kind

const activityRef = (activity: BacktestActivity) => [
  activity.chainId && `chain ${activity.chainId}`,
  activity.decisionId && `decision ${activity.decisionId}`,
  activity.orderId && `order ${activity.orderId}`,
  activity.fillId && `fill ${activity.fillId}`,
  activity.parentId && `parent ${activity.parentId}`,
  activity.originSignalId && `origin ${activity.originSignalId}`,
  activity.attemptNo && `attempt ${activity.attemptNo}`,
].filter(Boolean).join(' · ') || activity.id

const relatedActivities = (activities: BacktestActivity[], selected: Pick<BacktestActivity, 'id' | 'chainId'>) => {
  const related = selected.chainId
    ? activities.filter((activity) => activity.chainId === selected.chainId)
    : activities.filter((activity) => activity.id === selected.id)
  return related.slice().sort((left, right) => left.occurredAt.localeCompare(right.occurredAt))
}

const combinedActivityText = (activities: BacktestActivity[]) =>
  activities.map((activity) => `${activity.title} ${activity.reason}`).join('；')

const priceLimitImpact = (activities: BacktestActivity[]) => {
  const text = combinedActivityText(activities)
  if (/daily_unlock_timing_unknown/i.test(text)) return '触及不利涨跌停；日线数据无法确认开板时点'
  if (/one_price_limit_up|limit_up|一字涨停|涨停/i.test(text)) return '受涨停限制；日线数据无法证明排队位置'
  if (/one_price_limit_down|limit_down|一字跌停|跌停/i.test(text)) return '受跌停限制；日线数据无法证明排队位置'
  return '活动记录未标记涨跌停影响'
}

const capacityLabels: Record<string, string> = {
  capacity_previous_session_volume_proxy: '上一交易日已完成成交量 × 参与率',
  capacity_explicit_point_in_time_observation: '使用委托前已可得的成交量观测',
  capacity_unlimited_explicit: '用户显式关闭容量上限',
  point_in_time_capacity_unknown: '委托前无可验证容量数据，保守不成交',
}

const capacityImpact = (activity: BacktestActivity, related: BacktestActivity[]) => {
  const reasonCode = activity.capacityReasonCode
    ?? related.find((candidate) => candidate.capacityReasonCode)?.capacityReasonCode
  return reasonCode ? capacityLabels[reasonCode] ?? '已记录成交容量限制' : '活动记录未保存容量影响'
}

const tPlusOneImpact = (activities: BacktestActivity[]) => {
  const text = combinedActivityText(activities)
  if (/t_plus_one_restricted|T\+1.*(?:限制|未满足)/i.test(text)) return '持仓未满足 A 股次日可卖规则，阻断卖出'
  if (/(?:满足|已满足)\s*T\+1|T\+1.*(?:满足|未阻断)/i.test(text)) return '已满足 A 股次日可卖规则，未阻断本次成交'
  return '活动记录未标记次日可卖限制'
}

const evidenceProviderLabels: Record<string, string> = {
  eastmoney: '东方财富公告',
  ifind: '同花顺 iFinD',
  rqdata: 'RQData',
  tushare: 'Tushare',
  web_archive: '网页存档',
  choice: 'Choice 行情快照',
  mock_sample: '固定样例',
}

const evidenceTimeQualityLabels: Record<string, string> = {
  exact: '秒级精确时间',
  vendor_observed: '供应商秒级首次可得',
  date_only: '可信日期；按当日收盘后可得，下一交易日委托',
  date_only_conservative: '可信日期；按当日收盘后可得，下一交易日委托',
  estimated_research_only: '估算时间（仅研究，不可交易）',
  daily_bar_open_proxy: '日线开盘价代理（时间非精确）',
  daily_bar_available_at_proxy: '日线整根可得价代理（时间非精确）',
}

const validationStatusLabels: Record<string, string> = {
  validated: '已通过来源校验',
  unverified: '未验证，不可交易',
  blocked_time_quality: '时间证据不足，不可交易',
  demonstration_only: '仅用于界面预览，未连接事件数据',
  quarantined: '已隔离，不进入严格回测',
  rejected: '未通过校验',
}

const labelOrRecorded = (labels: Record<string, string>, value: string | null | undefined) => {
  if (!value) return '未记录'
  return labels[value.toLowerCase()] ?? '已记录，可在技术详情中查看'
}

const evidenceFacts = (activity: BacktestActivity): ChainFact[] =>
  (activity.evidence ?? []).flatMap((item, index) => {
    const suffix = (activity.evidence?.length ?? 0) > 1 ? ` ${index + 1}` : ''
    const facts: ChainFact[] = [
      {
        label: `事件来源${suffix}`,
        value: labelOrRecorded(evidenceProviderLabels, item.provider),
        href: item.sourceUrl ?? undefined,
      },
      { label: `首次可得${suffix}`, value: item.availableAt },
      { label: `时间质量${suffix}`, value: labelOrRecorded(evidenceTimeQualityLabels, item.timeQuality) },
      { label: `时间精度${suffix}`, value: item.timestampPrecision ?? '未记录' },
      { label: `校验状态${suffix}`, value: labelOrRecorded(validationStatusLabels, item.validationStatus) },
    ]
    return facts
  })

const evidenceTechnicalFacts = (activity: BacktestActivity): ChainFact[] =>
  (activity.evidence ?? []).flatMap((item, index) => {
    const suffix = (activity.evidence?.length ?? 0) > 1 ? ` ${index + 1}` : ''
    return [
      { label: `数据来源代码${suffix}`, value: item.provider ?? '未记录' },
      { label: `时间质量代码${suffix}`, value: item.timeQuality ?? '未记录' },
      { label: `时间精度代码${suffix}`, value: item.timestampPrecision ?? '未记录' },
      { label: `校验状态代码${suffix}`, value: item.validationStatus ?? '未记录' },
      { label: `来源事件编号${suffix}`, value: item.sourceEventId ?? item.id },
      { label: `原始响应校验${suffix}`, value: item.rawResponseSha256 ?? '未记录' },
    ]
  })

const executionTimeFacts = (activity: BacktestActivity): ChainFact[] => [
  ...(activity.timeQuality
    ? [{ label: '成交时间质量', value: labelOrRecorded(evidenceTimeQualityLabels, activity.timeQuality) }]
    : []),
  ...(activity.timeSemantics
    ? [{ label: '成交时间说明', value: activity.timeSemantics }]
    : []),
]

const signalSemanticsLabels: Record<NonNullable<BacktestActivity['signalSemantics']>, string> = {
  edge: '边沿信号（只在条件由假变真时触发）',
  event: '事件信号',
  state: '状态信号（当前条件为真）',
  composite: '组合条件信号',
}

const retryReasonLabels: Record<string, string> = {
  one_price_limit_up: '前次委托受一字涨停限制，本次继续尝试',
  one_price_limit_down: '前次委托受一字跌停限制，本次继续尝试',
  suspended: '前次委托遇到停牌，本次继续尝试',
  capacity_exhausted: '前次委托受成交容量限制，本次继续尝试',
}

const outcomeReasonLabels: Record<string, string> = {
  matched_at_open: '本次委托按日线开盘价代理成交',
  one_price_limit_up: '本次因一字涨停未成交',
  one_price_limit_down: '本次因一字跌停未成交',
  suspended: '本次因停牌未成交',
  expired: '本次委托已过期',
}

const signalExecutionFacts = (activity: BacktestActivity): ChainFact[] => [
  ...(activity.signalSemantics
    ? [{ label: '信号语义', value: signalSemanticsLabels[activity.signalSemantics] }]
    : []),
  ...(activity.signalValiditySessions != null
    ? [{ label: '信号有效期', value: `${activity.signalValiditySessions} 个交易日委托机会` }]
    : []),
  ...(activity.attemptNo != null
    ? [{ label: '委托尝试', value: `第 ${activity.attemptNo} 次` }]
    : []),
  ...(activity.retryReason
    ? [{ label: '重试原因', value: retryReasonLabels[activity.retryReason] ?? '已记录重试原因' }]
    : []),
  ...(activity.outcomeReason
    ? [{ label: '本次结果', value: outcomeReasonLabels[activity.outcomeReason] ?? '已记录委托结果' }]
    : []),
]

const activityTechnicalFacts = (activity: BacktestActivity): ChainFact[] => [
  ...(activity.capacityReasonCode
    ? [{ label: '成交容量代码', value: activity.capacityReasonCode }]
    : []),
  ...(activity.timeQuality
    ? [{ label: '成交时间质量代码', value: activity.timeQuality }]
    : []),
  ...(activity.originSignalId
    ? [{ label: '源信号编号', value: activity.originSignalId }]
    : []),
  ...(activity.retryReason
    ? [{ label: '重试原因代码', value: activity.retryReason }]
    : []),
  ...(activity.outcomeReason
    ? [{ label: '本次结果代码', value: activity.outcomeReason }]
    : []),
]

const fmtCny = (value: number | null | undefined) => value == null
  ? '未记录'
  : new Intl.NumberFormat('zh-CN', {
      style: 'currency', currency: 'CNY', maximumFractionDigits: 2,
    }).format(value)

export const buildChain = (
  draft: StrategyDraft,
  activities: BacktestActivity[],
  selected: TradeRow,
  context: ChainContext = {},
): ChainNode[] => {
  const related = relatedActivities(activities, selected)
  const rules = strategyRuleTrees(draft)
  const normalized = [
    `买入：${summarizeRule(rules.entry)}`,
    `卖出：${summarizeRule(rules.exit)}`,
    `确认：${draft.execution.evaluationFrequency === '1d_close'
      ? '日线收盘'
      : draft.execution.evaluationFrequency === 'financial_available_plus_1d_close'
        ? '财务数据首次可得后的日线收盘'
        : draft.execution.evaluationFrequency === 'event_financial_available_plus_1d_close'
          ? '事件与财务数据均可得后的日线收盘'
          : '事件首次可得'}`,
  ].join(' · ')
  const activityNodes: ChainNode[] = []
  const emittedDecisions = new Set<string>()
  related.forEach((activity) => {
    activityNodes.push({
      kind: activityKind(activity.kind),
      title: activity.title,
      timestamp: activity.occurredAt,
      detail: [
        activity.reason,
        activity.price != null ? `价格 ¥${activity.price.toFixed(2)}` : null,
        activity.quantity != null ? `数量 ${activity.quantity.toLocaleString('zh-CN')} 股` : null,
        activity.capacityReasonCode ? `容量：${capacityImpact(activity, related)}` : null,
      ].filter(Boolean).join(' · '),
      ref: activityRef(activity),
      facts: [
        ...evidenceFacts(activity),
        ...executionTimeFacts(activity),
        ...signalExecutionFacts(activity),
      ],
      technicalFacts: [
        ...evidenceTechnicalFacts(activity),
        ...activityTechnicalFacts(activity),
      ],
    })
    if (activity.kind === 'signal' && activity.decisionId
      && !emittedDecisions.has(activity.decisionId)) {
      emittedDecisions.add(activity.decisionId)
      activityNodes.push({
        kind: 'decision',
        title: `形成${activity.side === 'buy' ? '买入' : '卖出'}决策`,
        timestamp: activity.occurredAt,
        detail: '已确认信号被转换为下一步委托决策。',
        ref: `decision ${activity.decisionId} · parent ${activity.id}`,
      })
    }
  })

  const accountIndex = context.series?.length
    ? nearestSeriesIndex(context.series, selected.occurredAt)
    : -1
  const accountPoint = accountIndex >= 0 ? context.series?.[accountIndex] : undefined
  const accountFacts: ChainFact[] = [
    { label: '该交易日净值', value: accountPoint ? `${accountPoint.equity.toFixed(2)}（起点 100）` : '未记录' },
    { label: '该交易日回撤', value: accountPoint ? `${accountPoint.drawdown.toFixed(2)}%` : '未记录' },
    { label: '回测期末资产', value: fmtCny(context.metrics?.finalEquityCny) },
    { label: '数据快照', value: context.evidence?.snapshotId ?? '未记录' },
    { label: '代码版本', value: context.evidence?.gitSha ?? '未记录' },
  ]

  return [
    {
      kind: 'utterance',
      title: '用户原话',
      detail: draft.sourceText,
      ref: 'utterance',
    },
    {
      kind: 'normalized',
      title: '规范化条件',
      detail: normalized,
      ref: `${draft.id}@r${draft.revision}`,
    },
    ...activityNodes,
    {
      kind: 'cash',
      title: '账户净值记录',
      timestamp: accountPoint?.date,
      detail: accountPoint
        ? '仅展示结果接口返回的当日账户净值和回撤；当前接口未返回逐笔可用现金，前端不反推。'
        : '当前结果未保存该笔对应的净值点或逐笔现金，前端不反推。',
      ref: `account ${context.evidence?.runId ?? selected.chainId ?? selected.id}`,
      facts: accountFacts,
    },
  ]
}
