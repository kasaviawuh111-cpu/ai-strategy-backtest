import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Fragment, useEffect, useMemo, useRef, useState } from 'react'

import { FailureCard, ResultCard, RunningCard, StrategyCard } from './components/SummaryCards'
import { Bubble, Chip, Chips, DayDivider, Say, ThinkBlock, Turn, Typing } from './components/primitives'
import { ChainScreen, ExecutionDetailsScreen, ParamsScreen, ReportScreen } from './screens'
import { apiMode, backtestApi, strategyApi, systemApi } from './shared/api/client'
import { ApiError } from './shared/api/types'
import type {
  BacktestActivity,
  Clarification,
  ClarificationChoice,
  CompileRequest,
  Instrument as ApiInstrument,
  StrategyDraft,
} from './shared/api/types'
import {
  MAXIMUM_INITIAL_CASH_CNY,
  MINIMUM_INITIAL_CASH_CNY,
} from './shared/config/backtest'
import { DEFAULT_INSTRUMENT } from './shared/instrument-context'
import type {
  BacktestMetrics,
  ChartMark,
  EditableRow,
  FailureState,
  Instrument,
  RunEvidence,
  SeriesPoint,
  StrategySummary,
  TradeRow,
} from './types'
import {
  buildChain,
  assessStrategyCapabilities,
  summarizeExecution,
  summarizeRule,
  strategyRuleTrees,
  toBacktestMetrics,
  toChartMarks,
  toRunEvidence,
  toSeries,
  toStrategySummary,
  toTradeRows,
  toUiInstrument,
} from './view-model'
import './styles/app.css'

type Overlay = 'params' | 'report' | 'execution' | 'chain'

type ClarificationRecord = {
  question: string
  reason: string
  answer: string
}

type JourneySnapshot = {
  id: string
  utterance: string
  draft: StrategyDraft
  instrument: Instrument
  strategy: StrategySummary
  metrics: BacktestMetrics
  series: SeriesPoint[]
  marks: ChartMark[]
  trades: TradeRow[]
  evidence: RunEvidence
  activities: BacktestActivity[]
  clarification?: ClarificationRecord
}

const terminalStates = new Set(['succeeded', 'failed', 'cancelled'])
const cloneDraft = (draft: StrategyDraft): StrategyDraft => JSON.parse(JSON.stringify(draft)) as StrategyDraft

type QuickIconName = 'thinking' | 'skill' | 'task' | 'timer' | 'stock'

function QuickIcon({ name }: { name: QuickIconName }) {
  if (name === 'thinking') {
    return <svg viewBox="0 0 18 18" fill="none" aria-hidden="true">
      <path d="m7.1 11.2-1.6 1.6a2.7 2.7 0 0 1-3.8-3.8l2.5-2.5A2.7 2.7 0 0 1 8 6.4" stroke="currentColor" strokeWidth="1.55" strokeLinecap="round" />
      <path d="m10.9 6.8 1.6-1.6A2.7 2.7 0 0 1 16.3 9l-2.5 2.5a2.7 2.7 0 0 1-3.8.1" stroke="currentColor" strokeWidth="1.55" strokeLinecap="round" />
      <path d="m6.4 9 5.2.1" stroke="currentColor" strokeWidth="1.55" strokeLinecap="round" />
    </svg>
  }
  if (name === 'skill') {
    return <svg viewBox="0 0 18 18" fill="none" aria-hidden="true">
      <path d="M11.5 2.3a4 4 0 0 0-4.8 5.2l-4 4a1.7 1.7 0 0 0 2.4 2.4l4-4a4 4 0 0 0 5.2-4.8l-2.4 2.4-2.2-.5-.5-2.2 2.3-2.5Z" stroke="currentColor" strokeWidth="1.45" strokeLinejoin="round" />
      <path className="quick-accent" d="M14.5 1.2v2.4M13.3 2.4h2.4" strokeWidth="1.35" strokeLinecap="round" />
    </svg>
  }
  if (name === 'task') {
    return <svg viewBox="0 0 18 18" fill="none" aria-hidden="true">
      <rect x="2.1" y="4" width="13.8" height="11" rx="3" stroke="currentColor" strokeWidth="1.4" />
      <path d="M6 8.2h.01M12 8.2h.01M6.3 11.4c1.5 1.1 3.9 1.1 5.4 0M9 4V2.4" stroke="currentColor" strokeWidth="1.45" strokeLinecap="round" />
      <path className="quick-accent" d="m14.5 1 .35 1.05 1.05.35-1.05.35-.35 1.05-.35-1.05-1.05-.35 1.05-.35L14.5 1Z" strokeWidth=".7" strokeLinejoin="round" />
    </svg>
  }
  if (name === 'timer') {
    return <svg viewBox="0 0 18 18" fill="none" aria-hidden="true">
      <rect x="2.1" y="2.4" width="13.8" height="13.2" rx="2.8" stroke="currentColor" strokeWidth="1.4" />
      <path className="quick-accent" d="m4.6 6.2.8.8 1.4-1.6m-2.2 5 .8.8 1.4-1.6" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M9 6.2h4.2M9 10.4h4.2" stroke="currentColor" strokeWidth="1.35" strokeLinecap="round" />
    </svg>
  }
  return <svg viewBox="0 0 18 18" fill="none" aria-hidden="true">
    <path d="M3 3.2h8.7M4.8 7h5M6.5 10.8h1.7M3 3.2l3.5 4.1v3.5" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
    <circle cx="12.5" cy="11.5" r="3.1" stroke="currentColor" strokeWidth="1.4" />
    <path className="quick-accent" d="m14.8 13.8 2 2" strokeWidth="1.5" strokeLinecap="round" />
  </svg>
}

const errorMessage = (error: unknown) => {
  if (error instanceof ApiError) {
    return error.problem.detail
  }
  if (error instanceof Error) return error.message
  return '操作未完成，请稍后重试。'
}

const errorCode = (error: unknown) => error instanceof ApiError ? error.problem.code : undefined

const failureFor = (
  error: unknown,
  fallbackKey: string,
  fallbackTitle: string,
  instrument: ApiInstrument,
  runId?: string,
): FailureState => {
  const code = errorCode(error) ?? (error instanceof Error ? error.message : '')
  const normalized = code.toLowerCase()

  if (normalized.includes('instrument_required')) {
    return {
      key: 'missing_stock',
      status: 'rejected',
      title: '缺少股票',
      bubble: '还不知道要回测哪只股票。',
      reason: `请使用当前股票“${instrument.name} ${instrument.symbol}”，或返回股票页后再试。`,
      actions: [`使用${instrument.name}`, '修改规则'],
    }
  }
  if (
    normalized.includes('document_text')
    && /(unavailable|missing|coverage|extraction|not_executable)/.test(normalized)
  ) {
    return {
      key: 'data_incomplete',
      status: 'partial',
      title: '已理解规则，但缺少报告正文数据',
      bubble: '买卖条件已经还原，但当前还不能安全回测。',
      reason: `${errorMessage(error)} 系统不会用公告标题代替正文，也不会猜测词频。请补齐可校验的报告正文快照后再运行。`,
      actions: ['修改规则', '使用技术示例'],
      runId,
    }
  }
  if (
    normalized.includes('no_candidate')
    || normalized.includes('no_supported_signal')
    || normalized.includes('not_recognized')
    || normalized.includes('template_not_published')
    || normalized.includes('compile_unsupported')
  ) {
    return {
      key: 'unsupported_strategy',
      status: 'rejected',
      title: '无法识别这条策略',
      bubble: '我还缺少能落到回测里的关键信息。',
      reason: '没有识别到当前可执行的技术指标或公告事件。请写清何时买入、何时卖出和回测区间，或先使用页面示例。',
      actions: ['修改规则', '使用技术示例'],
    }
  }
  if (
    normalized.includes('event_not_available_for_backtest')
    || normalized.includes('backtest_service_unavailable')
    || normalized.includes('capability')
  ) {
    return {
      key: 'capability_unavailable',
      status: 'unavailable',
      title: '规则已识别，但当前能力不能运行',
      bubble: '我保留了这条规则，不过现在还不能安全回测。',
      reason: `${errorMessage(error)} 你可以修改规则，或等固定快照与准备能力可用后再试。`,
      actions: ['修改规则', '使用技术示例'],
      runId,
    }
  }
  if (
    normalized.includes('no_validated_second_precision_time')
    || normalized.includes('time_quality')
    || normalized.includes('date_only')
  ) {
    return {
      key: 'event_time_insufficient',
      status: 'unavailable',
      title: '事件时间质量不足',
      bubble: '这条事件暂时不能用于正式回测。',
      reason: '目前无法证明事件在历史上的精确首次可得时间。为避免偷看未来数据，本次不触发交易。',
      actions: ['改用技术策略', '修改规则'],
      runId,
    }
  }
  if (
    normalized.includes('event_data_unavailable')
    || normalized.includes('events_were_not_included')
    || normalized.includes('acquisition_coverage')
    || normalized.includes('snapshot')
  ) {
    return {
      key: 'data_incomplete',
      status: 'partial',
      title: '数据不完整',
      bubble: '这次回测缺少可重放的数据。',
      reason: '当前快照没有覆盖这条策略需要的事件或行情，系统不会自动联网补数或放宽规则。',
      actions: ['改用技术策略', '修改规则'],
      runId,
    }
  }

  return {
    key: fallbackKey,
    status: 'rejected',
    title: fallbackTitle,
    bubble: fallbackTitle,
    reason: errorMessage(error),
    actions: runId ? ['重新读取', '修改规则'] : ['修改规则', '使用技术示例'],
    runId,
  }
}

const draftValidation = (draft: StrategyDraft) => {
  const conditions = [...draft.entry.conditions, ...draft.exit.conditions]
  const intrabar = conditions.some((condition) =>
    condition.kind === 'indicator' && condition.evaluationMode === 'intrabar_streaming')
  const invalidParameter = conditions.some((condition) => condition.kind === 'indicator'
    && condition.parameters.some((parameter) => !Number.isFinite(parameter.value)
      || parameter.value < parameter.min
      || parameter.value > parameter.max
      || Boolean(parameter.integer && !Number.isInteger(parameter.value))))
  const invalidCash = !Number.isInteger(draft.backtest.initialCashCny)
    || draft.backtest.initialCashCny < MINIMUM_INITIAL_CASH_CNY
    || draft.backtest.initialCashCny > MAXIMUM_INITIAL_CASH_CNY
  const invalidWindow = draft.backtest.start > draft.backtest.end
  const invalidExecution = !Number.isFinite(draft.execution.participationRate)
    || draft.execution.participationRate <= 0
    || draft.execution.participationRate > 1
    || !Number.isFinite(draft.execution.allocationRatio)
    || draft.execution.allocationRatio <= 0
    || draft.execution.allocationRatio > 1
    || !Number.isFinite(draft.execution.slippageBps)
    || draft.execution.slippageBps < 0
    || draft.execution.slippageBps > 1_000
    || !Number.isFinite(draft.execution.commissionRate)
    || draft.execution.commissionRate < 0
    || !Number.isFinite(draft.execution.minimumCommissionCny)
    || draft.execution.minimumCommissionCny < 0
    || !Number.isInteger(draft.execution.maxExitAttempts)
    || draft.execution.maxExitAttempts < 1
    || draft.execution.maxExitAttempts > 1_000
    || !Number.isInteger(draft.execution.warmupCalendarDays)
    || draft.execution.warmupCalendarDays < 0
    || draft.execution.warmupCalendarDays > 3_650
    || !Number.isInteger(draft.execution.settlementExtensionDays)
    || draft.execution.settlementExtensionDays < 1
    || draft.execution.settlementExtensionDays > 365
  if (intrabar) return { valid: false, reason: '分钟行情与分钟撮合尚未接入，盘中策略暂不可运行。' }
  if (invalidParameter) return { valid: false, reason: '有指标参数超出允许范围，请打开参数页检查。' }
  if (invalidCash) return { valid: false, reason: '初始资金需为 1 万元至 10 亿元之间的整数。' }
  if (invalidWindow) return { valid: false, reason: '回测开始日期不能晚于结束日期。' }
  if (invalidExecution) return { valid: false, reason: '高级执行参数超出后端允许范围，请打开设置检查。' }
  return { valid: true, reason: undefined }
}

type AppProps = {
  instrument?: ApiInstrument
  instrumentContextError?: string
  onReturnToStockPage?: () => void
  onUseStandaloneExample?: () => void
}

export default function App({
  instrument = DEFAULT_INSTRUMENT,
  instrumentContextError,
  onReturnToStockPage = () => window.history.back(),
  onUseStandaloneExample = () => {
    const standalone = new URL(window.location.href)
    standalone.search = ''
    window.location.assign(standalone.toString())
  },
}: AppProps) {
  const queryClient = useQueryClient()
  const defaultUtterance = instrumentContextError
    ? ''
    : `${instrument.name} MACD 金叉买入，死叉卖出，回测近 5 年`
  const [utterance, setUtterance] = useState(defaultUtterance)
  const [submittedText, setSubmittedText] = useState<string>()
  const [draft, setDraft] = useState<StrategyDraft>()
  const [baselineDraft, setBaselineDraft] = useState<StrategyDraft>()
  const [clarification, setClarification] = useState<Clarification>()
  const [clarificationRecord, setClarificationRecord] = useState<ClarificationRecord>()
  const [runId, setRunId] = useState<string>()
  const [journeyHistory, setJourneyHistory] = useState<JourneySnapshot[]>([])
  const [reportSnapshot, setReportSnapshot] = useState<JourneySnapshot>()
  const [stack, setStack] = useState<Overlay[]>([])
  const [paramsFocus, setParamsFocus] = useState<EditableRow['key'] | 'more'>('entry')
  const [chainTitle, setChainTitle] = useState('交易因果轨迹')
  const [chainNodes, setChainNodes] = useState<ReturnType<typeof buildChain>>([])
  const [showScrollToBottom, setShowScrollToBottom] = useState(false)
  const scrollRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  const open = (overlay: Overlay) => setStack((current) =>
    current.includes(overlay) ? current : [...current, overlay])
  const openParams = (focus: EditableRow['key'] | 'more') => {
    setParamsFocus(focus)
    open('params')
  }
  const back = () => setStack((current) => current.slice(0, -1))

  const compileMutation = useMutation({
    mutationFn: ({ text, answer }: { text: string; answer?: CompileRequest['clarification'] }) =>
      strategyApi.compile({ instrument, utterance: text, clarification: answer }),
    onSuccess: (outcome) => {
      setRunId(undefined)
      setStack([])
      if (outcome.status === 'needs_clarification') {
        setDraft(undefined)
        setBaselineDraft(undefined)
        setClarification(outcome.clarification)
      } else {
        const nextDraft = cloneDraft(outcome.draft)
        setClarification(undefined)
        setDraft(nextDraft)
        setBaselineDraft(cloneDraft(nextDraft))
      }
    },
  })

  const capabilitiesQuery = useQuery({
    queryKey: ['capabilities'],
    queryFn: systemApi.capabilities,
    staleTime: 30_000,
    retry: apiMode === 'live' ? 1 : false,
  })

  const startMutation = useMutation({
    mutationFn: async (candidate: StrategyDraft) => {
      const savedDraft = await strategyApi.revise(candidate)
      const run = await backtestApi.create(savedDraft)
      return { savedDraft, run }
    },
    onSuccess: ({ savedDraft, run }) => {
      setDraft(savedDraft)
      setBaselineDraft(cloneDraft(savedDraft))
      queryClient.setQueryData(['backtest-run', run.id], run)
      setRunId(run.id)
      setStack([])
    },
  })

  const runQuery = useQuery({
    queryKey: ['backtest-run', runId],
    queryFn: () => backtestApi.get(runId as string),
    enabled: Boolean(runId),
    refetchInterval: (query) => {
      const state = query.state.data?.state
      return state && terminalStates.has(state) ? false : 700
    },
  })

  const cancelMutation = useMutation({
    mutationFn: () => backtestApi.cancel(runId as string),
    onSuccess: (run) => queryClient.setQueryData(['backtest-run', run.id], run),
    onSettled: () => {
      if (runId) void queryClient.invalidateQueries({ queryKey: ['backtest-run', runId] })
    },
  })

  const hasResult = runQuery.data?.state === 'succeeded'
  const summaryQuery = useQuery({
    queryKey: ['backtest-summary', runId],
    queryFn: () => backtestApi.summary(runId as string),
    enabled: Boolean(runId) && hasResult,
  })
  const seriesQuery = useQuery({
    queryKey: ['backtest-series', runId],
    queryFn: () => backtestApi.series(runId as string),
    enabled: Boolean(runId) && hasResult,
  })
  const activitiesQuery = useQuery({
    queryKey: ['backtest-activities', runId],
    queryFn: () => backtestApi.activities(runId as string),
    enabled: Boolean(runId) && hasResult,
  })

  const resultError = summaryQuery.error ?? seriesQuery.error ?? activitiesQuery.error
  const resultLoading = hasResult
    && (summaryQuery.isLoading || seriesQuery.isLoading || activitiesQuery.isLoading)
  const resultReady = Boolean(summaryQuery.data && seriesQuery.data && activitiesQuery.data)
  const isJourneyLocked = startMutation.isPending || Boolean(
    runQuery.data && !terminalStates.has(runQuery.data.state),
  )
  const validation = draft ? draftValidation(draft) : { valid: false, reason: undefined }

  const uiStrategy = useMemo(() => draft ? toStrategySummary(draft) : undefined, [draft])
  const capability = useMemo(() => draft
    ? assessStrategyCapabilities(draft, capabilitiesQuery.data, apiMode)
    : undefined, [capabilitiesQuery.data, draft])
  const metrics = useMemo(() => summaryQuery.data ? toBacktestMetrics(summaryQuery.data) : undefined,
    [summaryQuery.data])
  const series = useMemo(() => toSeries(seriesQuery.data ?? []), [seriesQuery.data])
  const trades = useMemo(() => toTradeRows(activitiesQuery.data ?? []), [activitiesQuery.data])
  const marks = useMemo(() => toChartMarks(series, activitiesQuery.data ?? []), [series, activitiesQuery.data])
  const evidence = useMemo(() => summaryQuery.data ? toRunEvidence(summaryQuery.data) : undefined,
    [summaryQuery.data])
  const canStart = Boolean(validation.valid && capability?.canRun && !capabilitiesQuery.isError)
  const startDisabledReason = validation.reason
    ?? (capabilitiesQuery.isError
      ? `${errorMessage(capabilitiesQuery.error)} 暂时读不到后端能力说明，不能确认这条策略是否可安全运行。`
      : capability?.reason ?? (capabilitiesQuery.isLoading ? '正在读取后端能力说明。' : undefined))

  const currentSnapshot = useMemo<JourneySnapshot | undefined>(() => {
    if (
      !runId
      || !submittedText
      || !draft
      || !uiStrategy
      || !metrics
      || !evidence
      || !resultReady
      || !activitiesQuery.data
    ) return undefined
    return {
      id: runId,
      utterance: submittedText,
      draft: cloneDraft(draft),
      instrument: toUiInstrument(draft),
      strategy: uiStrategy,
      metrics,
      series,
      marks,
      trades,
      evidence,
      activities: activitiesQuery.data,
      clarification: clarificationRecord,
    }
  }, [
    activitiesQuery.data,
    clarificationRecord,
    draft,
    evidence,
    marks,
    metrics,
    resultReady,
    runId,
    series,
    submittedText,
    trades,
    uiStrategy,
  ])

  const rememberCurrentJourney = () => {
    if (!currentSnapshot) return
    setJourneyHistory((current) => current.some((item) => item.id === currentSnapshot.id)
      ? current
      : [...current, currentSnapshot])
  }

  const resetForEdit = () => {
    setDraft(undefined)
    setBaselineDraft(undefined)
    setClarification(undefined)
    setClarificationRecord(undefined)
    setRunId(undefined)
    setSubmittedText(undefined)
    setReportSnapshot(undefined)
    setStack([])
    compileMutation.reset()
    startMutation.reset()
    window.setTimeout(() => inputRef.current?.focus(), 0)
  }

  const startNewCondition = () => {
    rememberCurrentJourney()
    resetForEdit()
  }

  const submitText = (text: string) => {
    const normalized = text.trim()
    if (!normalized || isJourneyLocked || instrumentContextError) return
    rememberCurrentJourney()
    setUtterance(normalized)
    setSubmittedText(normalized)
    setDraft(undefined)
    setBaselineDraft(undefined)
    setClarification(undefined)
    setClarificationRecord(undefined)
    setRunId(undefined)
    setReportSnapshot(undefined)
    setStack([])
    startMutation.reset()
    compileMutation.mutate({ text: normalized })
  }

  const handleClarify = (choice: ClarificationChoice) => {
    if (!clarification || !submittedText) return
    if (choice.action === 'edit_utterance') {
      resetForEdit()
      return
    }
    setClarificationRecord({
      question: clarification.question,
      reason: clarification.reason,
      answer: choice.label,
    })
    compileMutation.mutate({
      text: submittedText,
      answer: { id: clarification.id, choiceId: choice.id },
    })
  }

  const handleDraftChange = (nextDraft: StrategyDraft) => {
    setDraft(nextDraft)
    setRunId(undefined)
  }

  const openChain = (trade: TradeRow) => {
    const source = reportSnapshot ?? currentSnapshot
    if (!source) return
    setChainTitle(`${trade.title} · 因果轨迹`)
    setChainNodes(buildChain(source.draft, source.activities, trade, {
      metrics: source.metrics,
      series: source.series,
      evidence: source.evidence,
    }))
    open('chain')
  }

  const openCurrentReport = () => {
    if (!currentSnapshot) return
    setReportSnapshot(currentSnapshot)
    open('report')
  }

  const openHistoricalReport = (snapshot: JourneySnapshot) => {
    setReportSnapshot(snapshot)
    open('report')
  }

  const openExecutionDetails = () => open('execution')

  const refreshResults = () => {
    void summaryQuery.refetch()
    void seriesQuery.refetch()
    void activitiesQuery.refetch()
  }

  let failure: FailureState | undefined
  if (instrumentContextError) {
    failure = {
      key: 'missing_stock',
      status: 'rejected',
      title: '股票页上下文无效',
      bubble: '还不知道要回测哪只 A 股。',
      reason: instrumentContextError,
      actions: ['返回股票页', '使用东方财富示例'],
    }
  } else if (compileMutation.isError) {
    failure = failureFor(compileMutation.error, 'compile_failed', '这句话暂时不能还原', instrument)
  } else if (runQuery.isError) {
    failure = failureFor(runQuery.error, 'run_read_failed', '任务状态读取失败', instrument, runId)
  } else if (cancelMutation.isError) {
    failure = failureFor(cancelMutation.error, 'cancel_failed', '取消请求没有完成', instrument, runId)
  } else if (runQuery.data?.state === 'failed') {
    failure = failureFor(
      new Error(runQuery.data.error ?? '后台没有返回具体失败原因。'),
      'run_failed',
      '回测失败',
      instrument,
      runId,
    )
  } else if (runQuery.data?.state === 'cancelled') {
    failure = {
      key: 'cancelled', status: 'implemented', title: '用户取消', bubble: '已经取消这次回测。',
      reason: '任务已停止。修改规则或参数后可以重新提交。', actions: ['修改规则'], runId,
    }
  } else if (resultError) {
    failure = failureFor(resultError, 'result_failed', '结果读取不完整', instrument, runId)
  }

  const handleFailureAction = (index: number) => {
    if (failure?.key === 'missing_stock' && instrumentContextError) {
      if (index === 0) onReturnToStockPage()
      else onUseStandaloneExample()
    } else if (index === 0 && failure?.key === 'result_failed') refreshResults()
    else if (index === 0 && ['run_read_failed', 'cancel_failed'].includes(failure?.key ?? '')) {
      void runQuery.refetch()
    } else if (index === 0 && failure?.key === 'missing_stock' && submittedText) {
      compileMutation.mutate({ text: submittedText })
    } else if (
      failure?.key === 'data_incomplete'
      || failure?.key === 'event_time_insufficient'
      || (failure?.key === 'unsupported_strategy' && index === 1)
      || (failure?.key === 'compile_failed' && index === 1)
      || (failure?.key === 'run_failed' && index === 1)
    ) {
      submitText(defaultUtterance)
    } else resetForEdit()
  }

  const conditionCount = draft
    ? draft.entry.conditions.length + draft.exit.conditions.length
    : 0
  const apiLabel = apiMode === 'mock' ? '界面预览' : '回测服务'
  const activeOverlay = stack.at(-1)

  useEffect(() => {
    const node = scrollRef.current
    if (node) {
      node.scrollTop = node.scrollHeight
      setShowScrollToBottom(false)
    }
  }, [
    clarification,
    clarificationRecord,
    compileMutation.isPending,
    draft,
    journeyHistory.length,
    resultReady,
    runQuery.data?.state,
    submittedText,
  ])

  return (
    <div className="app" data-api-mode={apiMode} data-has-overlay={stack.length > 0 ? 'true' : 'false'}>
      <div className="screens">
        <section className="page base" id="pg-chat" aria-hidden={Boolean(activeOverlay)} inert={Boolean(activeOverlay)}>
          <header className="navbar host-navbar">
            <button type="button" className="ico" aria-label="返回股票页" onClick={onReturnToStockPage}>
              <svg width="24" height="24" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                <path d="M15 5l-7 7 7 7" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </button>
            <span className="logo">东方<br />财富</span>
            <span className="nav-title"><b>妙想AI</b><span>内容由AI生成</span></span>
            <span className={`mode-pill mode-pill--${apiMode}`}>{apiLabel}</span>
            <button type="button" className="ico" aria-label="更多">
              <svg width="22" height="22" viewBox="0 0 22 22" fill="none" aria-hidden="true">
                <path d="M3 6h16M3 11h16M3 16h16" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
              </svg>
            </button>
          </header>

          <div className="scroll" ref={scrollRef} onScroll={(event) => {
            const node = event.currentTarget
            setShowScrollToBottom(node.scrollHeight - node.scrollTop - node.clientHeight > 80)
          }}>
            <div className="stream" aria-live="polite">
              <DayDivider>当前会话</DayDivider>

              {journeyHistory.map((journey) => (
                <Fragment key={journey.id}>
                  <Turn mine><Bubble>{journey.utterance}</Bubble></Turn>
                  {journey.clarification ? (
                    <>
                      <Turn><Say>{journey.clarification.question}</Say></Turn>
                      <Turn mine><Bubble>{journey.clarification.answer}</Bubble></Turn>
                    </>
                  ) : null}
                  <Turn>
                    <ThinkBlock
                      title="已读懂你的规则"
                      meta="历史记录"
                      lines={[
                        `买入：${summarizeRule(journey.strategy.entryRule)}`,
                        `卖出：${summarizeRule(journey.strategy.exitRule)}`,
                        '已提交并完成回测，这张卡片保留为不可编辑的记录',
                      ]}
                    />
                    <Say>这条策略已经跑完：</Say>
                    <StrategyCard
                      instrument={journey.instrument}
                      strategy={journey.strategy}
                      onEditRow={() => undefined}
                      onOpenMore={() => undefined}
                      onRun={() => undefined}
                      settled="已完成回测"
                    />
                  </Turn>
                  <Turn>
                    <Say>结果保留在这里，方便和下一次对比。</Say>
                    <ResultCard
                      metrics={journey.metrics}
                      series={journey.series}
                      marks={journey.marks}
                      onOpenReport={() => openHistoricalReport(journey)}
                      historical
                    />
                  </Turn>
                </Fragment>
              ))}

              {journeyHistory.length > 0 && !submittedText ? <DayDivider>继续回测</DayDivider> : null}

              {!submittedText ? (
                <Turn>
                  <Say>
                    {instrumentContextError
                      ? '当前股票信息无法识别。请返回股票页重新选择，或明确使用示例股票。'
                      : journeyHistory.length > 0
                        ? <>上一次的结果保留在上面。直接说新的买卖规则，我接着往下还原。</>
                        : <>你在看 <b>{instrument.name}</b>。说一句你的交易规则，我把它变成一条能检查、能回测的策略。</>}
                  </Say>
                  {/*
                    示例只在冷启动时出现：它的作用是告诉第一次来的人「一句话可以写成什么样」。
                    跑过一轮之后用户已经知道怎么写，再把同样三条推一遍既占地方，
                    也像是在暗示「你应该选我给的这几个」。
                  */}
                  {!instrumentContextError && journeyHistory.length === 0 ? (
                    <Chips>
                      <Chip onClick={() => submitText(defaultUtterance)}>MACD 金叉买、死叉卖</Chip>
                      <Chip onClick={() => submitText(`${instrument.name}年度报告发布后买入，MACD 死叉卖出，回测近 5 年`)}>年报发布后买入</Chip>
                      <Chip onClick={() => submitText(`${instrument.name} RSI 低于 30 买入，高于 70 卖出，回测近 5 年`)}>RSI 超卖买、超买卖</Chip>
                    </Chips>
                  ) : null}
                </Turn>
              ) : <Turn mine><Bubble>{submittedText}</Bubble></Turn>}

              {clarificationRecord ? (
                <>
                  <Turn>
                    <ThinkBlock title="已确认规则" meta="回答已记录"
                      lines={[clarificationRecord.reason]} />
                    <Say>{clarificationRecord.question}</Say>
                  </Turn>
                  <Turn mine><Bubble>{clarificationRecord.answer}</Bubble></Turn>
                </>
              ) : null}

              {compileMutation.isPending ? <Turn><Typing /></Turn> : null}

              {clarification && !clarificationRecord && !compileMutation.isPending ? (
                <Turn>
                  <ThinkBlock title="还差一项确认" meta="只问这一次" lines={[
                    clarification.reason,
                    ...(clarification.recognized ?? []).map((item) => `${item.label}：${item.value}`),
                  ]} />
                  <Say>{clarification.question}</Say>
                  <Chips>
                    {clarification.choices.map((choice) => {
                      const unavailable = /盘中|分钟/.test(`${choice.label}${choice.description}`)
                      return <Chip key={choice.id} pin={choice.recommended ? '推荐' : undefined}
                        disabled={unavailable} title={unavailable ? '分钟数据与撮合尚未接入' : choice.description}
                        onClick={() => handleClarify(choice)}>{choice.label}{unavailable ? ' · 暂不可用' : ''}</Chip>
                    })}
                  </Chips>
                </Turn>
              ) : null}

              {draft && uiStrategy ? (
                <Turn>
                  <ThinkBlock
                    title="已读懂你的规则"
                    meta={`用到 ${conditionCount} 个条件`}
                    lines={[
                      `买入：${summarizeRule(strategyRuleTrees(draft).entry)}`,
                      `卖出：${summarizeRule(strategyRuleTrees(draft).exit)}`,
                      draft.entry.conditions.some((condition) => condition.kind === 'event')
                        ? '事件按首次可得时间触发；成交仍受交易日与 A 股规则约束'
                        : '技术信号按日线收盘确认；下一交易日只使用开盘价代理，成交时间非精确',
                      ...(apiMode === 'mock'
                        && draft.entry.conditions.some((condition) =>
                          condition.kind === 'event' && condition.documentText)
                        ? ['界面预览只展示规则识别和卡片结构；没有读取年报正文，也没有计算词频']
                        : []),
                    ]}
                  />
                  <Say>规则已经还原成下面这条策略，确认后再跑：</Say>
                  <StrategyCard
                    instrument={toUiInstrument(draft)}
                    strategy={uiStrategy}
                    onEditRow={openParams}
                    onOpenMore={() => openParams('more')}
                    onRun={() => canStart && startMutation.mutate(draft)}
                    isStarting={startMutation.isPending}
                    isLocked={isJourneyLocked}
                    settled={runQuery.data?.state === 'succeeded' ? '已完成回测' : undefined}
                    canStart={canStart}
                    disabledReason={startDisabledReason}
                    error={startMutation.isError ? errorMessage(startMutation.error) : undefined}
                    executionSummary={summarizeExecution(draft, baselineDraft)}
                  />
                  <p className="hint">要改哪一条，点开对应那行；也可以在下面直接说一句话重写。</p>
                </Turn>
              ) : null}

              {runQuery.data && !terminalStates.has(runQuery.data.state) ? (
                <Turn>
                  <Say>{apiMode === 'mock'
                    ? '这是界面预览，下面的阶段和结果来自固定样例。'
                    : '任务已提交。下面显示后台返回的处理阶段。'}</Say>
                  <RunningCard phase={runQuery.data.state} onCancel={() => cancelMutation.mutate()}
                    isCancelling={cancelMutation.isPending} isMock={apiMode === 'mock'} />
                </Turn>
              ) : null}

              {resultLoading ? <Turn><Say>计算已结束，正在读取报告数据。</Say><Typing /></Turn> : null}

              {draft && metrics && evidence && resultReady ? (
                <>
                  <Turn>
                    <Say>回测完成。</Say>
                    <ResultCard metrics={metrics} series={series} marks={marks}
                      onOpenReport={openCurrentReport} />
                  </Turn>
                  <Turn>
                    <Say>接下来：</Say>
                    <Chips>
                      <Chip onClick={startNewCondition}>换个条件再跑</Chip>
                      <Chip onClick={() => undefined} title="功能入口，暂未接入提醒服务">设成盯盘提醒</Chip>
                      <Chip onClick={() => undefined} title="功能入口，暂未接入股票切换">换只股票试试</Chip>
                    </Chips>
                  </Turn>
                </>
              ) : null}

              {failure ? <Turn><Say>{failure.bubble}</Say>
                <FailureCard state={failure} onAction={handleFailureAction} /></Turn> : null}
            </div>
          </div>

          {showScrollToBottom ? <button type="button" className="fab" aria-label="回到底部"
            onClick={() => {
              const node = scrollRef.current
              if (node) node.scrollTop = node.scrollHeight
              setShowScrollToBottom(false)
            }}>
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                <path d="M8 3v10M4 9l4 4 4-4" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </button> : null}

          <div className="dock">
            <div className="quick" aria-label="宿主能力">
              <button type="button" className="on" aria-pressed="true"><QuickIcon name="thinking" />深度思考</button>
              <button type="button"><QuickIcon name="skill" />技能</button>
              <button type="button"><QuickIcon name="task" />超级任务</button>
              <button type="button"><QuickIcon name="timer" />定时</button>
              <button type="button"><QuickIcon name="stock" />选股</button>
            </div>

            <div className="dock-row">
              <form className="inputbar" onSubmit={(event) => { event.preventDefault(); submitText(utterance) }}>
                <button type="button" className="ib" aria-label="键盘输入" onClick={() => inputRef.current?.focus()}>
                  <svg width="20" height="16" viewBox="0 0 20 16" fill="none" aria-hidden="true">
                    <rect x="1" y="2.5" width="18" height="11" rx="3" stroke="currentColor" strokeWidth="1.4" />
                    <path d="M5 6.5h.01M8 6.5h.01M11 6.5h.01M14 6.5h.01M6 10h8" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
                  </svg>
                </button>
                <input ref={inputRef} className="strategy-input" aria-label="交易规则" value={utterance}
                  disabled={isJourneyLocked || Boolean(instrumentContextError)} onChange={(event) => setUtterance(event.target.value)}
                  placeholder="说出什么时候买、什么时候卖" />
                <button type="submit" className="send" aria-label="识别交易规则"
                  disabled={isJourneyLocked || compileMutation.isPending || Boolean(instrumentContextError) || !utterance.trim()}>
                  <svg width="18" height="18" viewBox="0 0 18 18" fill="none" aria-hidden="true">
                    <path d="M4 9h10M10 5l4 4-4 4" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                </button>
              </form>
              <button type="button" className="trade-fab" aria-label="宿主买卖入口，本原型不执行交易" disabled>
                <span className="b">买</span><span className="s">卖</span>
              </button>
            </div>

            <p className="disclaimer">仅做历史回测，不构成投资建议</p>
            <div className="homebar"><i /></div>
          </div>
        </section>

        {draft ? (
          <ParamsScreen open={activeOverlay === 'params'} onBack={back} draft={draft}
            onChange={handleDraftChange}
            onReset={() => baselineDraft && setDraft(cloneDraft(baselineDraft))}
            isLocked={isJourneyLocked} focus={paramsFocus} />
        ) : null}
        {reportSnapshot ? (
          <ReportScreen open={activeOverlay === 'report'} onBack={back}
            metrics={reportSnapshot.metrics} series={reportSnapshot.series}
            marks={reportSnapshot.marks} trades={reportSnapshot.trades}
            evidence={reportSnapshot.evidence} onOpenChain={openChain}
            onOpenExecution={openExecutionDetails} mode={apiMode} />
        ) : null}
        {reportSnapshot ? (
          <ExecutionDetailsScreen open={activeOverlay === 'execution'} onBack={back}
            draft={reportSnapshot.draft} trades={reportSnapshot.trades}
            evidence={reportSnapshot.evidence} mode={apiMode} />
        ) : null}
        <ChainScreen open={activeOverlay === 'chain'} onBack={back} title={chainTitle} nodes={chainNodes} />
      </div>
    </div>
  )
}
