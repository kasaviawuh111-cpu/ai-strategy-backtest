import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Fragment, useEffect, useMemo, useRef, useState } from 'react'

import { FailureCard, ResultCard, RunningCard, StrategyCard } from './components/SummaryCards'
import { BacktestReview } from './components/BacktestReview'
import { StrategySlotComposer } from './components/StrategySlotComposer'
import { ModelReasoning } from './components/ModelReasoning'
import { ColumnResizer } from './components/ColumnResizer'
import { useStoredColumnWidths } from './components/column-widths'
import { Proposals, type ProposalItem } from './components/Proposals'
import {
  Bubble, Chip, Chips, DayDivider, Notice, Say, ThinkBlock,
  ThinkingStream, Turn,
} from './components/primitives'
import { ChainScreen, ExecutionDetailsScreen, ExecutionEntry, ParamsScreen, ReportBody } from './screens'
import { apiMode, backtestApi, instrumentApi, strategyApi, systemApi } from './shared/api/client'
import type { DialogueProgressEvent, DialogueProgressObserver } from './shared/api/client'
import { fromBacktestOptimizationCandidate, toLiveBacktestBody, toLiveRevisionBody } from './shared/api/contract'
import { ApiError } from './shared/api/types'
import type {
  BacktestActivity,
  BacktestOptimizationCandidate,
  BacktestReviewResponse,
  BacktestRun,
  Clarification,
  ClarificationData,
  ClarificationSuggestion,
  CompileRequest,
  ExecutionSettings,
  IdeaRouteProposal,
  Instrument as ApiInstrument,
  StrategyDraft,
} from './shared/api/types'
import {
  MAXIMUM_INITIAL_CASH_CNY,
  MINIMUM_INITIAL_CASH_CNY,
} from './shared/config/backtest'
import { validateBacktestDates } from './shared/backtest-date-validation'
import { DEFAULT_INSTRUMENT, toAshareInstrument } from './shared/instrument-context'
import { DEFAULT_STRATEGY_EXAMPLES } from './shared/default-strategy-examples'
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

type Overlay = 'params' | 'execution' | 'chain'

const executableRuleText = (draft: StrategyDraft): string => {
  const rules = toStrategySummary(draft).rows.slice(0, 2)
    .map((row) => `${row.label}：${row.value}`).join('；')
  return `${draft.instrument.name}；${rules}；${draft.backtest.start} 至 ${draft.backtest.end}`
}

type ClarificationMessage = {
  role: 'assistant' | 'user'
  text: string
  data?: ClarificationData
}

type ClarificationTarget = {
  draftId: string
  revision?: number
  originalRequest: CompileRequest
  executionContext?: Partial<ExecutionSettings>
}

type JourneySnapshot = {
  id: string
  utterance: string
  fromPanelEdit?: boolean
  draft: StrategyDraft
  instrument: Instrument
  strategy: StrategySummary
  metrics: BacktestMetrics
  series: SeriesPoint[]
  marks: ChartMark[]
  trades: TradeRow[]
  evidence: RunEvidence
  activities: BacktestActivity[]
  clarificationMessages: ClarificationMessage[]
  review?: BacktestReviewResponse
}

/**
 * 策略名。必须由「编译出来的策略」推导，不能用用户第一句原话：
 * 多轮澄清之后，最终规则可能和第一句毫无关系（线上出现过标题是「你可以干啥」）。
 * 名字跟着要执行的东西走，才不会和实际跑的规则对不上。
 */
const strategyTitle = (instrument: Instrument, strategy: StrategySummary): string =>
  `${instrument.name} ${summarizeRule(strategy.entryRule)}买入，`
  + `${summarizeRule(strategy.exitRule)}卖出`

const terminalStates = new Set(['succeeded', 'failed', 'cancelled'])
/** 点「开始回测」时替用户发出的那句话；和按钮文案保持同一个词。 */
const RUN_COMMAND = '开始回测'
const cloneDraft = (draft: StrategyDraft): StrategyDraft => JSON.parse(JSON.stringify(draft)) as StrategyDraft
const sameExecutableDraft = (candidate: StrategyDraft, baseline: StrategyDraft): boolean =>
  JSON.stringify({
    strategy: toLiveRevisionBody(candidate).strategy,
    config: toLiveBacktestBody(candidate).config,
  }) === JSON.stringify({
    strategy: toLiveRevisionBody(baseline).strategy,
    config: toLiveBacktestBody(baseline).config,
  })
const instrumentClarificationIds = new Set([
  'instrument_required',
  'instrument_unconfirmed',
  'instrument_resolution_unavailable',
])
const isInstrumentClarification = (
  clarification: Clarification | undefined,
): boolean =>
  Boolean(clarification && instrumentClarificationIds.has(clarification.id))

const visibleClarificationChoices = (clarification: Clarification): typeof clarification.choices =>
  clarification.choices.filter((choice) => choice.action !== 'edit_utterance')

const ENTRY_ACTION = /(?:买入|买进|建仓|开仓|低吸|抄底)/
const EXIT_ACTION = /(?:卖出|卖掉|退出|平仓|清仓|止盈|止损)/

/**
 * 观点候选只能展示服务端给出的完整买卖句。标题或假设本身不是规则，
 * 前端也不把“趋势确认”之类抽象方向补成可执行语句。
 */
const isCompleteIdeaProposal = (proposal: IdeaRouteProposal): boolean => {
  const utterance = proposal.suggested_utterance.trim()
  return Boolean(
    proposal.entry_summary.trim()
    && proposal.exit_summary.trim()
    && ENTRY_ACTION.test(utterance)
    && EXIT_ACTION.test(utterance),
  )
}

const orderedIdeaProposals = (
  clarification: Clarification | undefined,
  suggestions: ClarificationSuggestion[],
): IdeaRouteProposal[] => {
  const proposals = clarification?.ideaRoute?.proposals ?? []
  if (!proposals.length) return []
  const byId = new Map(proposals.map((proposal) => [proposal.id, proposal]))
  const ranked = suggestions
    .map((suggestion) => byId.get(suggestion.id))
    .filter((proposal): proposal is IdeaRouteProposal => Boolean(proposal))
  const rankedIds = new Set(ranked.map((proposal) => proposal.id))
  return [...ranked, ...proposals.filter((proposal) => !rankedIds.has(proposal.id))]
    .filter(isCompleteIdeaProposal)
    .slice(0, 3)
}

const ideaProposalInstrument = (
  clarification: Clarification,
  target: ClarificationTarget | undefined,
  proposal: IdeaRouteProposal,
): string | undefined => {
  const symbol = proposal.instrument_symbol
    ?? clarification.ideaRoute?.asset_mapping.instrument_symbol
    ?? undefined
  if (!symbol) return undefined
  const groundedChoice = clarification.choices.find((choice) =>
    choice.id === proposal.id && choice.instrumentSymbol === symbol && choice.instrumentName)
  const targetInstrument = target?.originalRequest.instrument
  const targetIsGrounded = target?.originalRequest.instrumentContextSource !== 'standalone_default'
    && targetInstrument?.symbol === symbol
  const name = proposal.instrument_name ?? groundedChoice?.instrumentName
    ?? (targetIsGrounded ? targetInstrument?.name : undefined)
  return name ? `${name} · ${symbol}` : symbol
}

const ideaProposalCards = (
  clarification: Clarification | undefined,
  target: ClarificationTarget | undefined,
  suggestions: ClarificationSuggestion[],
): ProposalItem[] => {
  if (!clarification?.ideaRoute) return []
  return orderedIdeaProposals(clarification, suggestions).map((proposal) => ({
    id: proposal.id,
    title: proposal.pairing_reason
      ? `${proposal.instrument_name ?? proposal.instrument_symbol} · ${proposal.title}`
      : proposal.title,
    paired: Boolean(proposal.pairing_reason),
    detail: proposal.pairing_reason ?? undefined,
    instrument: ideaProposalInstrument(clarification, target, proposal),
    entry: proposal.entry_summary,
    exit: proposal.exit_summary,
  }))
}

const clarificationAnswerMessage = (
  assistantMessage: string,
): string => assistantMessage

type DisplayDataTable = {
  title?: string
  columns: string[]
  rows: unknown[][]
}

const providerTable = (table: Record<string, unknown>): DisplayDataTable | undefined => {
  const title = typeof table.title === 'string' ? table.title : undefined
  const rawTable = table.rawTable
  const source = rawTable && typeof rawTable === 'object' && !Array.isArray(rawTable)
    ? rawTable as Record<string, unknown>
    : table
  const rawColumns = Array.isArray(source.headers)
    ? source.headers
    : Array.isArray(source.columns) ? source.columns : []
  const columns = rawColumns.map((value) => String(value))
  const rawRows = Array.isArray(source.data)
    ? source.data
    : Array.isArray(source.rows) ? source.rows : []
  if (!columns.length || !rawRows.length) return undefined
  const rows = rawRows.map((row) => {
    if (Array.isArray(row)) return row
    if (row && typeof row === 'object') {
      const record = row as Record<string, unknown>
      return columns.map((column) => record[column])
    }
    return [row]
  })
  return { title, columns, rows }
}

const currentDataTables = (data: ClarificationData): DisplayDataTable[] => {
  if (data.kind === 'screen') {
    return [{
      title: `${data.screen.assetType}选股结果`,
      columns: data.screen.columns,
      rows: data.screen.rows.map((row) => data.screen.columns.map((column) => row[column])),
    }]
  }
  if (data.kind === 'finance') {
    return data.finance.tables.map(providerTable).filter((table): table is DisplayDataTable => Boolean(table))
  }
  const screen = data.screenedFinance.screen
  return [
    {
      title: `先选出 ${data.screenedFinance.entities.length} 只标的`,
      columns: screen.columns,
      rows: screen.rows.map((row) => screen.columns.map((column) => row[column])),
    },
    ...data.screenedFinance.batches
      .flatMap((batch) => batch.tables)
      .map(providerTable)
      .filter((table): table is DisplayDataTable => Boolean(table)),
  ]
}

const firstText = (
  row: Record<string, unknown>,
  keys: readonly string[],
): string | undefined => {
  for (const key of keys) {
    const value = row[key]
    if (typeof value === 'string' || typeof value === 'number') {
      const text = String(value).trim()
      if (text) return text
    }
  }
  return undefined
}

const screenRowInstruments = (
  rows: Array<Record<string, unknown>>,
): ApiInstrument[] => rows.flatMap((row) => {
  const code = firstText(row, ['证券代码', '股票代码', '代码'])
  if (!code) return []
  const instrument = toAshareInstrument(
    code,
    firstText(row, ['证券简称', '股票简称', '股票名称', '名称', '简称', '股票']),
  )
  return instrument ? [instrument] : []
})

/** Only expose actions for provider-grounded A-share identities. */
const currentDataInstruments = (data: ClarificationData): ApiInstrument[] => {
  let instruments: ApiInstrument[] = []
  if (data.kind === 'screen' && data.screen.assetType === 'A股') {
    instruments = screenRowInstruments(data.screen.rows)
  } else if (data.kind === 'screened_finance') {
    instruments = data.screenedFinance.entities.flatMap((entity) => {
      if (entity.assetType !== 'A股') return []
      const instrument = toAshareInstrument(entity.code, entity.name)
      return instrument ? [instrument] : []
    })
  }
  const unique = new Map(instruments.map((item) => [item.symbol, item]))
  return [...unique.values()].slice(0, 8)
}

const researchSourceUrl = (url: string): string | undefined => {
  try {
    const parsed = new URL(url)
    return parsed.protocol === 'https:' || parsed.protocol === 'http:' ? parsed.href : undefined
  } catch {
    return undefined
  }
}

function IdeaResearchEvidence({ clarification }: { clarification: Clarification }) {
  const research = clarification.ideaRoute?.research
  if (!research?.sources.length) return null
  return (
    <details className="idea-research" aria-label="联网分析依据">
      <summary>查看参考来源</summary>
        <div className="idea-research__sources" aria-label="联网信息来源">
          {research.sources.slice(0, 5).map((source) => {
            const href = researchSourceUrl(source.url)
            return href ? (
              <a key={source.source_id} href={href} target="_blank" rel="noreferrer">
                {source.title}
              </a>
            ) : null
          })}
        </div>
    </details>
  )
}

function CurrentDataResult({
  data,
  onAnalyze,
}: {
  data: ClarificationData
  onAnalyze?: (instrument: ApiInstrument) => void
}) {
  // A direct finance lookup is already summarized in the assistant sentence.
  // Keep the structured payload in state for auditability, but do not expose
  // provider field names, hashes or transport metadata in the conversation.
  if (data.kind === 'finance') return null
  const tables = currentDataTables(data)
  const instruments = currentDataInstruments(data)
  return (
    <div className="current-data" aria-label="实时查询结果">
      {tables.map((table, index) => (
        <section className="current-data__table" key={`${table.title ?? '查询结果'}-${index}`}>
          {table.title ? <h3>{table.title}</h3> : null}
          <div className="current-data__scroll">
            <table>
              <thead>
                <tr>{table.columns.slice(0, 8).map((column) => <th key={column}>{column}</th>)}</tr>
              </thead>
              <tbody>
                {table.rows.slice(0, 12).map((row, rowIndex) => (
                  <tr key={rowIndex}>
                    {table.columns.slice(0, 8).map((_, columnIndex) => (
                      <td key={columnIndex}>{String(row[columnIndex] ?? '—')}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {table.rows.length > 12 ? <p className="current-data__more">已展示前 12 条，共 {table.rows.length} 条</p> : null}
        </section>
      ))}
      {onAnalyze && instruments.length ? (
        <div className="current-data__actions" aria-label="选择标的继续分析">
          <p>选一只继续：先联网核实公开信息，再给出可由你确认的回测策略。</p>
          {instruments.map((instrument) => (
            <button
              type="button"
              key={instrument.symbol}
              onClick={() => onAnalyze(instrument)}
            >
              联网分析并给回测策略：{instrument.name} · {instrument.symbol}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  )
}

// The server returns one complete model reply; choices remain separate UI controls.
const clarificationMessage = (clarification: Clarification): string => clarification.question

const clarificationPlaceholder = (clarification: Clarification | undefined): string => {
  if (!clarification) return '说出什么时候买、什么时候卖'
  if (clarification.instrumentSuggestions?.length) return '也可以输入你想用的股票'
  if (isInstrumentClarification(clarification)) return '输入股票名称或 6 位代码'
  if (clarification.id === 'entry_rule_not_recognized') return '补充什么时候买入'
  if (clarification.id === 'exit_rule_not_recognized') return '补充什么时候卖出'
  if (clarification.ideaRoute
    && clarification.ideaRoute.asset_mapping.instrument_symbol == null) {
    return '选个方向，或说说你想用哪只股票'
  }
  if (clarification.ideaRoute) return '回复序号，或直接说完整规则'
  return '补充完整规则，或直接换一种说法'
}

const contextRecoveryMessage = (error: unknown): string | undefined => {
  const code = errorCode(error)
  if (code === 'conversation_parent_draft_not_found' || code === 'strategy_draft_not_found') {
    return '这轮策略的服务端记录已无法读取，本次修改和回测未执行。请从原报告进入“换个条件再回测”或“换只股票试试”；没有报告时，请新建会话并重新输入完整策略。'
  }
  if (code === 'conversation_parent_draft_stale' || code === 'strategy_draft_revision_stale') {
    return '这轮策略版本已更新，本次修改未提交。请从原报告重新进入修改；没有报告时，请新建会话并重新输入完整策略。'
  }
  if (code === 'strategy_edit_context_required') {
    return '没有找到这次修改对应的原策略，本次未执行回测。请从原报告重新进入修改；没有报告时，请新建会话并重新输入完整策略。'
  }
  if (code === 'backtest_review_context_unavailable') {
    return '这版优化建议的服务端记录已无法读取，尚未执行新回测。请回到原报告，点击“重试 AI 分析”后重新选择。'
  }
  if (code === 'backtest_review_result_changed') {
    return '回测结果已更新，原优化建议不能直接沿用。请回到原报告，点击“重试 AI 分析”后重新选择。'
  }
  if (code === 'backtest_run_not_found') return '这次回测记录已无法读取，请重新发起回测。'
  return undefined
}

const isReviewContextError = (error: unknown): boolean =>
  errorCode(error) === 'backtest_review_context_unavailable'
  || errorCode(error) === 'backtest_review_result_changed'

const compileRecoveryMessage = (error: unknown): string => {
  const contextMessage = contextRecoveryMessage(error)
  if (contextMessage) return contextMessage
  const code = errorCode(error)?.toLowerCase() ?? ''
  if (code === 'api_timeout') {
    return '这次识别等待超时了，请原样再发一次。'
  }
  if (code === 'api_network_unavailable') {
    return '这次没有连上本地回测服务，请确认后端仍在运行后原样重试。'
  }
  if (code === 'skill_indicator_unavailable' || code.startsWith('live_market_data_')) {
    return errorMessage(error)
  }
  if (code.startsWith('candidate_provider_')) return errorMessage(error)
  if (code === 'previous_session_limit_up_capability_unavailable') {
    return '我已理解你想用“前一交易日涨停”作为买入条件。当前回测还不能可靠执行这个信号，也不会用单日涨 10% 代替。请在下方改用“价格突破”、“涨跌幅”或“MACD / 均线”条件，并说清卖出方式。'
  }
  if (code.includes('document_text')) {
    return '我已理解你想用报告正文作为条件，但当前固定数据还不能审计这段正文。系统不会用公告标题代替，也不会猜测词频。请在下方改用价格、涨跌幅或技术指标条件。'
  }
  if (
    code.includes('no_candidate')
    || code.includes('no_supported_signal')
    || code.includes('not_recognized')
    || code.includes('compile_unsupported')
  ) {
    return '我还没能把这句话还原成完整的买卖规则。请在下方用一句话补充股票、买入和卖出条件；可以从价格阈值、涨跌幅，或 MACD / 均线 / RSI 中选一个方向。'
  }
  if (code.includes('capability') || code.includes('unavailable')) {
    return errorMessage(error)
  }
  return `策略生成请求未完成：${errorMessage(error)}`
}

const errorMessage = (error: unknown) => {
  const contextMessage = contextRecoveryMessage(error)
  if (contextMessage) return contextMessage
  if (error instanceof ApiError) {
    return error.problem.detail
  }
  if (error instanceof Error) return error.message
  return '操作未完成，请稍后重试。'
}

const errorCode = (error: unknown) => error instanceof ApiError ? error.problem.code : undefined

const backtestFailureMessage = (run: BacktestRun): string => {
  // Only the indicator stage identifies the exact Skill. History preparation
  // can call both screening and finance lookup, so do not guess its origin.
  const source = run.progressLabel.includes('获取东方财富指标')
    ? '东方财富查数 Skill'
    : '东方财富选股/查数流程'
  switch (run.error) {
    case 'skill_mx_auth_failed':
    case 'skill_mx_read_timeout':
    case 'skill_mx_connect_timeout':
    case 'skill_mx_transport_error':
    case 'skill_mx_http_error':
    case 'skill_mx_no_data':
      // These labels are built by the server from safe tool/reason/status
      // fields, never from provider response prose or exception messages.
      return run.progressLabel
    case 'skill_MxSaasProviderAuthError':
      return `${source}授权失败，本次回测未完成。`
    case 'skill_MxSaasProviderUnavailableError':
      return `${source}连接失败，请稍后重试。`
    case 'skill_MxSaasProviderNoDataError':
      return `${source}未返回本次回测所需的数据。`
    case 'skill_MxSaasProviderDataError':
      return `${source}返回的数据未通过本次回测的数据检查，回测已停止。`
    case 'skill_MxDailyHistoryError':
      return '东方财富历史数据读取或检查失败，本次回测未完成。'
    case 'skill_history_fields_missing':
      return run.progressLabel
    default:
      return run.error ?? '后台没有返回具体失败原因。'
  }
}

const failureFor = (
  error: unknown,
  fallbackKey: string,
  fallbackTitle: string,
  instrument: ApiInstrument,
  runId?: string,
): FailureState => {
  const code = errorCode(error) ?? (error instanceof Error ? error.message : '')
  const normalized = code.toLowerCase()

  if (normalized === 'skill_indicator_unavailable') {
    return {
      key: 'capability_unavailable',
      status: 'unavailable',
      title: '历史指标数据暂不可用',
      reason: errorMessage(error),
      actions: ['修改规则', '使用技术示例'],
      runId,
    }
  }
  if (normalized.includes('instrument_required')) {
    return {
      key: 'missing_stock',
      status: 'rejected',
      title: '缺少股票',
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
      title: '缺少报告正文数据',
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
      reason: '没有识别到当前可执行的技术指标或公告事件。请写清何时买入、何时卖出和回测区间，或先使用页面示例。',
      actions: ['修改规则', '使用技术示例'],
    }
  }
  if (
    normalized.includes('event_not_available_for_backtest')
    || normalized.includes('financial_data_unavailable')
    || normalized.includes('backtest_service_unavailable')
    || normalized.includes('capability')
  ) {
    return {
      key: 'capability_unavailable',
      status: 'unavailable',
      title: '暂时无法回测',
      reason: errorMessage(error),
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
      reason: '当前快照没有覆盖这条策略需要的事件或行情，系统不会自动联网补数或放宽规则。',
      actions: ['改用技术策略', '修改规则'],
      runId,
    }
  }

  return {
    key: fallbackKey,
    status: 'rejected',
    title: fallbackTitle,
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
  const dateValidation = validateBacktestDates(draft.backtest.start, draft.backtest.end)
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
  if (!dateValidation.valid) return dateValidation
  if (invalidExecution) return { valid: false, reason: '高级执行参数超出后端允许范围，请打开设置检查。' }
  return { valid: true, reason: undefined }
}

type AppProps = {
  instrument?: ApiInstrument
  instrumentContextSource?: 'stock_page' | 'standalone_default'
  instrumentContextError?: string
  /**
   * 宿主返回入口。顶栏移除后本页不再自绘返回键——嵌入方自己有导航，
   * 独立打开时浏览器后退就够了。保留在类型里，免得调用方要改签名。
   */
  onReturnToStockPage?: () => void
  onUseStandaloneExample?: () => void
}

export default function App({
  instrument = DEFAULT_INSTRUMENT,
  instrumentContextSource = 'standalone_default',
  instrumentContextError,
}: AppProps) {
  const queryClient = useQueryClient()
  const maCrossExample = `${instrument.name}5日均线上穿20日均线买入，5日均线下穿20日均线卖出`
  const [utterance, setUtterance] = useState('')
  const [strategySlots, setStrategySlots] = useState<{ draft: StrategyDraft; mode: 'stock' | 'rules' }>()
  const rerunAfterEdit = useRef(false)
  const refreshEditedRun = useRef(false)
  const [submittedText, setSubmittedText] = useState<string>()
  const [fromPanelEdit, setFromPanelEdit] = useState(false)
  const [draft, setDraft] = useState<StrategyDraft>()
  const currentDraftRef = useRef<StrategyDraft | undefined>(draft)
  useEffect(() => { currentDraftRef.current = draft }, [draft])
  const [baselineDraft, setBaselineDraft] = useState<StrategyDraft>()
  const [clarification, setClarification] = useState<Clarification>()
  const [clarificationTarget, setClarificationTarget] = useState<ClarificationTarget>()
  const [clarificationMessages, setClarificationMessages] = useState<ClarificationMessage[]>([])
  const [clarificationPrompt, setClarificationPrompt] = useState<string>()
  const [clarificationData, setClarificationData] = useState<ClarificationData>()
  const [clarificationSuggestions, setClarificationSuggestions] = useState<ClarificationSuggestion[]>([])
  const [clarificationRequestFailed, setClarificationRequestFailed] = useState(false)
  const [runId, setRunId] = useState<string>()
  /**
   * 点「开始回测」是用户下的指令，所以它应该像用户说的一句话那样进入对话流，
   * 而不是卡片自己悄悄变成运行态——否则回看会话时，这一步没有留下任何痕迹。
   */
  const [runCommand, setRunCommand] = useState<string>()
  const [journeyHistory, setJourneyHistory] = useState<JourneySnapshot[]>([])
  const exposedReviewReferences = useRef<NonNullable<CompileRequest['relatedReviews']>>([])
  const rememberReviewReference = (review: BacktestReviewResponse) => {
    const reference = { runId: review.runId, responseHash: review.modelProvenance.responseHash }
    exposedReviewReferences.current = [
      ...exposedReviewReferences.current.filter(item => item.runId !== reference.runId
        || item.responseHash !== reference.responseHash),
      reference,
    ].slice(-20)
  }
  // Browser-tab lifetime only. A new strategy keeps this opaque server draft
  // lineage; an explicit new conversation clears it. Never persist it globally.
  const [conversationTailDraftId, setConversationTailDraftId] = useState<string>()
  const [reportSnapshot, setReportSnapshot] = useState<JourneySnapshot>()
  const [reviewContextError, setReviewContextError] = useState<{ runId: string; message: string }>()

  const [stack, setStack] = useState<Overlay[]>([])
  const [paramsFocus, setParamsFocus] = useState<EditableRow['key'] | 'more'>('entry')
  const [chainTitle, setChainTitle] = useState('交易因果轨迹')
  const [chainNodes, setChainNodes] = useState<ReturnType<typeof buildChain>>([])
  const [showScrollToBottom, setShowScrollToBottom] = useState(false)
  /**
   * 策略审阅栏的展开态。桌面端是常驻右栏（这个值只控制手机端那块贴底面板），
   * 新策略识别出来时自动展开——刚生成就该被看见。
   */
  const [reviewOpen, setReviewOpen] = useState(false)
  /**
   * 主区呈现哪一种版式：
   *   'chat'   —— 左栏 + 对话（+ 可选的策略审阅栏），也就是两栏 / 三栏
   *   'detail' —— 左栏 + 一整块策略详情，中间的对话让位（回测跑完后的落点）
   * 详情态不是「另一个页面」，只是同一块工作区换了一种排布，所以做成状态而不是路由。
   */
  const [view, setView] = useState<'chat' | 'detail'>('chat')
  const [detailTab, setDetailTab] = useState<'flow' | 'report'>('flow')
  /** 详情区显示哪一次回测：默认当前这次，左栏点历史时切过去。 */
  const [detailJourneyId, setDetailJourneyId] = useState<string>()
  const [dialogueProgress, setDialogueProgress] = useState<readonly DialogueProgressEvent[]>([])
  const dialogueProgressAbortRef = useRef<AbortController | null>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const workspaceRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  useStoredColumnWidths(workspaceRef)

  useEffect(() => () => dialogueProgressAbortRef.current?.abort(), [])

  const beginDialogueProgress = (): DialogueProgressObserver => {
    dialogueProgressAbortRef.current?.abort()
    const controller = new AbortController()
    dialogueProgressAbortRef.current = controller
    setDialogueProgress([])
    return {
      signal: controller.signal,
      onProgress: (events) => {
        if (!controller.signal.aborted) setDialogueProgress(events.slice(-12))
      },
    }
  }

  const open = (overlay: Overlay) => setStack((current) =>
    current.includes(overlay) ? current : [...current, overlay])
  const openParams = (focus: EditableRow['key'] | 'more') => {
    setParamsFocus(focus)
    open('params')
  }
  const back = () => setStack((current) => current.slice(0, -1))

  const compileRequestFor = ({ text, instrumentOverride }: {
    text: string
    instrumentOverride?: ApiInstrument
  }): CompileRequest => ({
    instrument: instrumentOverride ?? instrument,
    instrumentContextSource: instrumentOverride ? 'stock_page' : instrumentContextSource,
    utterance: text,
  })

  const compileMutation = useMutation({
    mutationFn: ({ text, instrumentOverride, parentDraftId, editCurrentStrategy, executionContext,
      relatedRunIds, relatedReview, relatedReviews }: {
      text: string
      instrumentOverride?: ApiInstrument
      parentDraftId?: string
      editCurrentStrategy?: boolean
      executionContext?: Partial<ExecutionSettings>
      relatedRunIds?: string[]
      relatedReview?: CompileRequest['relatedReview']
      relatedReviews?: CompileRequest['relatedReviews']
    }) => {
      const request = {
        ...compileRequestFor({ text, instrumentOverride }),
        editCurrentStrategy,
        executionSettings: parentDraftId ? executionContext : undefined,
        relatedRunIds,
        relatedReview,
        relatedReviews,
        dialogueProgress: beginDialogueProgress(),
      }
      return parentDraftId
        ? strategyApi.compile(request, parentDraftId)
        : strategyApi.compile(request)
    },
    onSuccess: (outcome, variables) => {
      // Even a rerun chip may need clarification or a user-requested pause.
      // Only the resolved server turn can authorize the new calculation.
      rerunAfterEdit.current = outcome.status === 'compiled' && outcome.runRequested === true
      refreshEditedRun.current = outcome.status === 'compiled'
        && outcome.runRequested === true && outcome.refreshData === true
      setConversationTailDraftId(
        outcome.status === 'compiled' ? outcome.draft.id : outcome.draftId,
      )
      setClarificationRequestFailed(false)
      setRunId(undefined)
      setStack([])
      setRunCommand(undefined)
      // 新的识别结果永远属于对话；识别出草稿就顺手把审阅栏打开
      setView('chat')
      setDetailJourneyId(undefined)
      setReviewOpen(outcome.status !== 'needs_clarification')
      if (outcome.status === 'needs_clarification') {
        setDraft(undefined)
        setBaselineDraft(undefined)
        setClarification(outcome.clarification)
        const review = outcome.clarification.backtestReview
        if (review) {
          rememberReviewReference(review)
          setJourneyHistory(current => current.map(item => item.id === review.runId
            ? { ...item, review } : item))
        }
        setClarificationTarget({
          draftId: outcome.draftId,
          revision: outcome.revision,
          originalRequest: compileRequestFor(variables),
          executionContext: outcome.executionSettings
            ?? (outcome.isStrategyEdit ? variables.executionContext : undefined),
        })
        setClarificationMessages([])
        setClarificationSuggestions([])
        setClarificationPrompt(outcome.assistantMessage ?? clarificationMessage(outcome.clarification))
        setClarificationData(outcome.data)
        setUtterance('')
        window.setTimeout(() => inputRef.current?.focus(), 0)
      } else {
        const nextDraft = cloneDraft(outcome.draft)
        // New servers return saved settings, including explicit fee edits.
        // Only legacy responses need the browser's previous configuration.
        if (outcome.executionSettings === undefined && outcome.isStrategyEdit && variables.executionContext) {
          nextDraft.execution = { ...nextDraft.execution, ...variables.executionContext }
        }
        setClarification(undefined)
        setClarificationTarget(undefined)
        setClarificationSuggestions([])
        setClarificationPrompt(undefined)
        setClarificationData(undefined)
        setClarificationMessages(outcome.assistantMessage?.trim()
          ? [{ role: 'assistant', text: outcome.assistantMessage }]
          : [])
        setDraft(nextDraft)
        setBaselineDraft(cloneDraft(nextDraft))
      }
    },
    onError: (error, variables) => {
      if (isReviewContextError(error) && variables.relatedReview) {
        setReviewContextError({ runId: variables.relatedReview.runId, message: errorMessage(error) })
      }
      setUtterance(variables.text)
      window.setTimeout(() => inputRef.current?.focus(), 0)
    },
  })

  const answerMutation = useMutation({
    mutationFn: ({ answer, target, pendingClarification, relatedRunIds, relatedReview, relatedReviews }: {
      answer: string
      inputText: string
      target: ClarificationTarget
      pendingClarification: Clarification
      relatedRunIds?: string[]
      relatedReview?: CompileRequest['relatedReview']
      relatedReviews?: CompileRequest['relatedReviews']
    }) => strategyApi.answerClarification({
      draftId: target.draftId,
      revision: target.revision,
      answer,
      executionSettings: target.executionContext,
      relatedRunIds,
      relatedReview,
      relatedReviews,
      originalRequest: target.originalRequest,
      clarification: pendingClarification,
      dialogueProgress: beginDialogueProgress(),
    }),
    onSuccess: (turn, variables) => {
      // A clarification can confirm the pending run or explicitly cancel it.
      // Use the resolved turn's intent, never an earlier slot's run flag.
      rerunAfterEdit.current = turn.outcome.status === 'compiled'
        && turn.outcome.runRequested === true
      refreshEditedRun.current = turn.outcome.status === 'compiled'
        && turn.outcome.runRequested === true && turn.outcome.refreshData === true
      setConversationTailDraftId(
        turn.outcome.status === 'compiled'
          ? turn.outcome.draft.id
          : turn.outcome.draftId,
      )
      setClarificationRequestFailed(false)
      const assistantText = clarificationAnswerMessage(turn.assistantMessage)
      setRunId(undefined)
      setStack([])
      setRunCommand(undefined)
      setView('chat')
      setDetailJourneyId(undefined)
      setReviewOpen(turn.outcome.status !== 'needs_clarification')
      if (turn.outcome.status === 'needs_clarification') {
        setDraft(undefined)
        setBaselineDraft(undefined)
        setClarification(turn.outcome.clarification)
        const review = turn.outcome.clarification.backtestReview
        if (review) {
          rememberReviewReference(review)
          setJourneyHistory(current => current.map(item => item.id === review.runId
            ? { ...item, review } : item))
        }
        setClarificationTarget({
          draftId: turn.outcome.draftId,
          revision: turn.outcome.revision,
          originalRequest: variables.target.originalRequest,
          executionContext: turn.outcome.executionSettings
            ?? (turn.outcome.isStrategyEdit ? variables.target.executionContext : undefined),
        })
        setClarificationSuggestions(turn.suggestions)
        setClarificationPrompt(assistantText)
        setClarificationData(turn.data)
      } else {
        const nextDraft = cloneDraft(turn.outcome.draft)
        if (turn.outcome.executionSettings === undefined
          && turn.outcome.isStrategyEdit && variables.target.executionContext) {
          nextDraft.execution = { ...nextDraft.execution, ...variables.target.executionContext }
        }
        const selectedProposal = variables.pendingClarification.ideaRoute?.proposals.find(
          (proposal) => proposal.id === variables.answer && proposal.pairing_reason
            && proposal.instrument_symbol === nextDraft.instrument.symbol,
        )
        const verifiedName = selectedProposal?.instrument_name?.trim()
        if (verifiedName && (!nextDraft.instrument.name.trim()
          || nextDraft.instrument.name === nextDraft.instrument.symbol)) {
          // READY may omit the paired proposal's name; preserve only this exact selection.
          nextDraft.instrument.name = verifiedName
        }
        setClarification(undefined)
        setClarificationTarget(undefined)
        setClarificationSuggestions([])
        setClarificationPrompt(undefined)
        setClarificationData(undefined)
        if (assistantText.trim() || turn.data) {
          setClarificationMessages((current) => [
            ...current,
            { role: 'assistant', text: assistantText, data: turn.data },
          ])
        }
        setDraft(nextDraft)
        setBaselineDraft(cloneDraft(nextDraft))
      }
      setUtterance('')
      window.setTimeout(() => inputRef.current?.focus(), 0)
    },
    onError: (error, variables) => {
      // A failed second-turn request must not leave the previous turn's cards
      // visible underneath a new error message. They belong to an older turn
      // and make a transport failure look like a fresh model recommendation.
      setClarificationRequestFailed(true)
      setClarificationSuggestions([])
      setClarificationPrompt(compileRecoveryMessage(error))
      setClarificationData(undefined)
      if (isReviewContextError(error) && variables.relatedReview) {
        setReviewContextError({ runId: variables.relatedReview.runId, message: errorMessage(error) })
      }
      setUtterance(variables.inputText)
      window.setTimeout(() => inputRef.current?.focus(), 0)
    },
  })

  const capabilitiesQuery = useQuery({
    queryKey: ['capabilities'],
    queryFn: systemApi.capabilities,
    staleTime: 30_000,
    retry: apiMode === 'live' ? 1 : false,
  })

  const startMutation = useMutation({
    mutationFn: async ({ candidate, refreshData = false }: {
      candidate: StrategyDraft; refreshData?: boolean
    }) => {
      const dateValidation = validateBacktestDates(candidate.backtest.start, candidate.backtest.end)
      if (!dateValidation.valid) throw new Error(dateValidation.reason)
      const savedDraft = baselineDraft && sameExecutableDraft(candidate, baselineDraft)
        ? candidate
        : await strategyApi.revise(candidate)
      const run = await backtestApi.create(savedDraft, { refreshData })
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
    /*
     * 回测是后台任务，用户十有八九会切到别处等它跑完。
     * react-query 默认在页面不可见时暂停轮询，那样切回来之前任务看着就是卡死的——
     * 这是状态查询，不是省流量的列表刷新，必须在后台继续问。
     */
    refetchIntervalInBackground: true,
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

  const [reviewProgress, setReviewProgress] = useState<{
    runId: string; events: readonly DialogueProgressEvent[]
  }>()
  const reviewProgressAbortRef = useRef<AbortController | null>(null)
  useEffect(() => () => reviewProgressAbortRef.current?.abort(), [])
  const reviewMutation = useMutation({
    mutationFn: (source: JourneySnapshot): Promise<BacktestReviewResponse> => {
      reviewProgressAbortRef.current?.abort()
      const controller = new AbortController()
      reviewProgressAbortRef.current = controller
      setReviewProgress({ runId: source.id, events: [] })
      return backtestApi.review(source.id, {
        signal: controller.signal,
        onProgress: (events) => {
          if (!controller.signal.aborted) setReviewProgress({ runId: source.id, events })
        },
      }, conversationReferences(source))
    },
    onSuccess: (review) => {
      rememberReviewReference(review)
      setJourneyHistory((current) => current.map((item) => item.id === review.runId
        ? { ...item, review } : item))
      setReviewContextError((current) => current?.runId === review.runId ? undefined : current)
    },
  })
  const autoReviewedRunIds = useRef(new Set<string>())

  const optimizationMutation = useMutation({
    mutationFn: async ({ source, candidate }: {
      source: JourneySnapshot
      candidate: BacktestOptimizationCandidate
    }) => ({
      source,
      candidate,
      ...await backtestApi.createOptimization(candidate, source.draft),
    }),
    onSuccess: ({ source, candidate, draft: optimizedDraft, run }) => {
      setConversationTailDraftId(optimizedDraft.id)
      setJourneyHistory((current) => current.some((item) => item.id === source.id)
        ? current
        : [...current, source])
      setUtterance('')
      setSubmittedText(`回测优化方案「${candidate.title}」：${executableRuleText(optimizedDraft)}`)
      setFromPanelEdit(false)
      setDraft(optimizedDraft)
      setBaselineDraft(cloneDraft(optimizedDraft))
      setClarification(undefined)
      setClarificationTarget(undefined)
      setClarificationMessages([])
      setClarificationSuggestions([])
      setClarificationPrompt(undefined)
      setClarificationData(undefined)
      setClarificationRequestFailed(false)
      queryClient.setQueryData(['backtest-run', run.id], run)
      setRunId(run.id)
      setRunCommand('开始优化回测')
      setReportSnapshot(undefined)
      setStack([])
      setDetailJourneyId(undefined)
      setReviewOpen(false)
      setView('chat')
    },
  })

  const resumeStrategyMutation = useMutation({
    mutationFn: ({ source }: { source: JourneySnapshot; mode: 'stock' | 'rules' }) =>
      strategyApi.revise(source.draft, true),
    onSuccess: (saved, { mode }) => {
      setConversationTailDraftId(saved.id)
      startNewCondition()
      setUtterance('')
      setStrategySlots({ draft: saved, mode })
    },
  })

  const resultError = summaryQuery.error ?? seriesQuery.error ?? activitiesQuery.error
  const resultLoading = hasResult
    && (summaryQuery.isLoading || seriesQuery.isLoading || activitiesQuery.isLoading)
  const resultReady = Boolean(summaryQuery.data && seriesQuery.data && activitiesQuery.data)
  const isJourneyLocked = compileMutation.isPending
    || answerMutation.isPending
    || startMutation.isPending
    || optimizationMutation.isPending
    || resumeStrategyMutation.isPending
    || Boolean(
    runQuery.data && !terminalStates.has(runQuery.data.state),
  )
  const validation = draft ? draftValidation(draft) : { valid: false, reason: undefined }
  const needsLimitUpRuleRewrite = compileMutation.isError
    && errorCode(compileMutation.error) === 'previous_session_limit_up_capability_unavailable'

  const uiStrategy = useMemo(() => draft ? toStrategySummary(draft) : undefined, [draft])
  const readyAssistantMessageIndex = draft && uiStrategy
    && clarificationMessages.at(-1)?.role === 'assistant' ? clarificationMessages.length - 1 : -1
  const readyAssistantMessage = clarificationMessages[readyAssistantMessageIndex]
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
  const startEditedRun = startMutation.mutate
  useEffect(() => {
    if (!rerunAfterEdit.current || !draft || !canStart || isJourneyLocked
      || compileMutation.isPending || clarification || runId) return
    rerunAfterEdit.current = false
    setRunCommand('按新条件重新回测')
    setReviewOpen(false)
    const refreshData = refreshEditedRun.current
    refreshEditedRun.current = false
    startEditedRun({ candidate: draft, refreshData })
  }, [draft, canStart, isJourneyLocked, compileMutation.isPending, clarification, runId, startEditedRun])
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
      fromPanelEdit,
      draft: cloneDraft(draft),
      instrument: toUiInstrument(draft),
      strategy: uiStrategy,
      metrics,
      series,
      marks,
      trades,
      evidence,
      activities: activitiesQuery.data,
      clarificationMessages,
      review: reviewMutation.data?.runId === runId ? reviewMutation.data : undefined,
    }
  }, [
    activitiesQuery.data,
    clarificationMessages,
    draft,
    evidence,
    fromPanelEdit,
    marks,
    metrics,
    resultReady,
    runId,
    series,
    submittedText,
    trades,
    uiStrategy,
    reviewMutation.data,
  ])

  const conversationReferences = (source?: JourneySnapshot): {
    relatedRunIds: string[]; relatedReviews: NonNullable<CompileRequest['relatedReviews']>
  } => {
    let relatedRunIds = [...new Set([
      ...journeyHistory.map(item => item.id),
      ...(currentSnapshot ? [currentSnapshot.id] : []),
      ...(source ? [source.id] : []),
    ])].slice(-20)
    // A manually opened older report remains the source of its own review.
    if (source && !relatedRunIds.includes(source.id)) {
      relatedRunIds = [source.id, ...relatedRunIds.slice(-19)]
    }
    return {
      relatedRunIds,
      relatedReviews: exposedReviewReferences.current
        .filter(reference => relatedRunIds.includes(reference.runId)),
    }
  }

  // Show the finished report immediately; request the real model independently.
  // Once per new run, never on history navigation or repeated status polling.
  const requestReview = reviewMutation.mutate
  useEffect(() => {
    if (apiMode !== 'live' || !currentSnapshot || reviewMutation.isPending
      || autoReviewedRunIds.current.has(currentSnapshot.id)) return
    autoReviewedRunIds.current.add(currentSnapshot.id)
    requestReview(currentSnapshot)
  }, [currentSnapshot, requestReview, reviewMutation.isPending])

  const rememberCurrentJourney = () => {
    if (!currentSnapshot) return
    setJourneyHistory((current) => current.some((item) => item.id === currentSnapshot.id)
      ? current
      : [...current, currentSnapshot])
  }

  const resetForEdit = () => {
    rerunAfterEdit.current = false
    refreshEditedRun.current = false
    setStrategySlots(undefined)
    setDraft(undefined)
    setBaselineDraft(undefined)
    setClarification(undefined)
    setClarificationTarget(undefined)
    setClarificationMessages([])
    setClarificationSuggestions([])
    setClarificationPrompt(undefined)
    setClarificationData(undefined)
    setRunId(undefined)
    setRunCommand(undefined)
    setSubmittedText(undefined)
    setFromPanelEdit(false)
    setReportSnapshot(undefined)
    setStack([])
    compileMutation.reset()
    answerMutation.reset()
    startMutation.reset()
    optimizationMutation.reset()
    window.setTimeout(() => inputRef.current?.focus(), 0)
  }

  const startNewCondition = () => {
    setView('chat')
    setReviewOpen(false)
    setDetailJourneyId(undefined)
    rememberCurrentJourney()
    resetForEdit()
  }

  const startNewConversation = () => {
    exposedReviewReferences.current = []
    setConversationTailDraftId(undefined)
    setReviewContextError(undefined)
    setJourneyHistory([])
    setUtterance('')
    setView('chat')
    setReviewOpen(false)
    setDetailJourneyId(undefined)
    resetForEdit()
  }

  const submitText = (
    text: string,
    options: {
      instrumentOverride?: ApiInstrument; proposalId?: string
      editCurrentStrategy?: boolean; rerun?: boolean
    } = {},
  ) => {
    const normalized = text.trim()
    if (!normalized || isJourneyLocked) return
    rerunAfterEdit.current = Boolean(options.rerun)
    refreshEditedRun.current = false
    // Send references only; the server loads the verified report facts itself.
    const { relatedRunIds, relatedReviews } = conversationReferences()
    const displayedReview = clarification?.backtestReview
      ?? [...journeyHistory, ...(currentSnapshot ? [currentSnapshot] : [])]
      .reverse().filter(snapshot => relatedRunIds.includes(snapshot.id))
      .map(snapshot => snapshot.review ?? (reviewMutation.data?.runId === snapshot.id
        ? reviewMutation.data : undefined)).find(Boolean)
    const relatedReview = displayedReview && relatedRunIds.includes(displayedReview.runId) ? {
      runId: displayedReview.runId, responseHash: displayedReview.modelProvenance.responseHash,
    } : undefined
    setStrategySlots(undefined)
    if (clarification && clarificationTarget && !options.instrumentOverride) {
      const prompt = clarificationPrompt ?? clarificationMessage(clarification)
      setClarificationMessages((current) => [
        ...current,
        { role: 'assistant', text: prompt, data: clarificationData },
        { role: 'user', text: normalized },
      ])
      setClarificationPrompt(undefined)
      setClarificationData(undefined)
      setClarificationSuggestions([])
      setClarificationRequestFailed(false)
      setUtterance('')
      setDraft(undefined)
      setBaselineDraft(undefined)
      setRunId(undefined)
      setRunCommand(undefined)
      setReportSnapshot(undefined)
      setStack([])
      startMutation.reset()
      answerMutation.mutate({
        answer: options.proposalId ?? normalized,
        inputText: normalized,
        target: clarificationTarget,
        pendingClarification: clarification,
        relatedRunIds,
        relatedReview,
        relatedReviews,
      })
      return
    }
    rememberCurrentJourney()
    setUtterance('')
    setSubmittedText(normalized)
    setFromPanelEdit(false)
    setDraft(undefined)
    setBaselineDraft(undefined)
    setClarification(undefined)
    setClarificationTarget(undefined)
    setClarificationMessages([])
    setClarificationSuggestions([])
    setClarificationPrompt(undefined)
    setClarificationData(undefined)
    setRunId(undefined)
    setRunCommand(undefined)
    setReportSnapshot(undefined)
    setStack([])
    answerMutation.reset()
    startMutation.reset()
    optimizationMutation.reset()
    compileMutation.mutate({
      text: normalized,
      instrumentOverride: options.instrumentOverride,
      parentDraftId: conversationTailDraftId,
      editCurrentStrategy: options.editCurrentStrategy,
      executionContext: strategySlots?.draft.execution ?? draft?.execution,
      relatedRunIds,
      relatedReview,
      relatedReviews,
    })
  }

  const analyzeCurrentInstrument = (selected: ApiInstrument) => {
    const identity = selected.name === selected.symbol
      ? selected.symbol
      : `${selected.name}（${selected.symbol}）`
    submitText(
      `分析${identity}的相关公开信息，给我几个可回测策略`,
      { instrumentOverride: selected },
    )
  }

  const submitClarificationChoice = (choice: Clarification['choices'][number]) => {
    const suggested = choice.suggestedUtterance ?? choice.label
    if (clarification?.id === 'strategy_edit_clarification') {
      // These candidates finish the pending edit. Recompiling with a new
      // instrument context would drop its exact parent revision and intent.
      submitText(suggested)
      return
    }
    const routeSymbol = clarification?.ideaRoute?.asset_mapping.instrument_symbol ?? undefined
    const targetInstrument = clarificationTarget?.originalRequest.instrument
    const targetIsGrounded = clarificationTarget?.originalRequest.instrumentContextSource !== 'standalone_default'
    const groundedInstrument = choice.instrumentName
      ?? choice.instrumentSymbol
      ?? (routeSymbol && targetInstrument?.symbol === routeSymbol ? targetInstrument.name : routeSymbol)
      ?? (targetIsGrounded ? targetInstrument?.name : undefined)
    const answer = groundedInstrument && !suggested.includes(groundedInstrument)
      ? `${groundedInstrument}${suggested}`
      : suggested
    const proposalInstrument = choice.instrumentSymbol
      ? toAshareInstrument(choice.instrumentSymbol, choice.instrumentName)
      : null
    const targetAlreadyGrounded = Boolean(
      proposalInstrument
      && targetIsGrounded
      && targetInstrument?.symbol === proposalInstrument.symbol,
    )
    const routeAlreadyGrounded = Boolean(
      proposalInstrument
      && routeSymbol === proposalInstrument.symbol,
    )
    submitText(
      answer,
      proposalInstrument && !targetAlreadyGrounded && !routeAlreadyGrounded
        ? { instrumentOverride: proposalInstrument }
        : undefined,
    )
  }

  const submitClarificationSuggestion = (suggestion: ClarificationSuggestion) => {
    const routeSymbol = clarification?.ideaRoute?.asset_mapping.instrument_symbol ?? undefined
    const targetInstrument = clarificationTarget?.originalRequest.instrument
    const targetIsGrounded = clarificationTarget?.originalRequest.instrumentContextSource !== 'standalone_default'
    const groundedInstrument = routeSymbol && targetInstrument?.symbol === routeSymbol
      ? targetInstrument.name
      : routeSymbol ?? (targetIsGrounded ? targetInstrument?.name : undefined)
    const answer = groundedInstrument && !suggestion.preview.includes(groundedInstrument)
      ? `${groundedInstrument}${suggestion.preview}`
      : suggestion.preview
    submitText(answer)
  }

  const submitIdeaProposal = (proposalId: string) => {
    const proposal = clarification?.ideaRoute?.proposals.find((item) => item.id === proposalId)
    if (proposal && isCompleteIdeaProposal(proposal) && clarificationTarget) {
      const title = proposal.title.trim() || proposal.suggested_utterance
      const displayText = proposal.pairing_reason
        ? `${proposal.instrument_name ?? proposal.instrument_symbol} · ${title}`
        : title
      submitText(displayText, {
        proposalId: proposal.id,
      })
      return
    }
    const choice = clarification?.choices.find((item) => item.id === proposalId)
    if (choice) {
      submitClarificationChoice(choice)
      return
    }
    const suggestion = clarificationSuggestions.find((item) => item.id === proposalId)
    if (suggestion) {
      submitClarificationSuggestion(suggestion)
      return
    }
    if (proposal && isCompleteIdeaProposal(proposal)) {
      submitText(proposal.suggested_utterance)
    }
  }

  const handleDraftChange = (nextDraft: StrategyDraft) => {
    rememberCurrentJourney()
    // A completed reply belongs to its archived run, not the newly edited draft.
    if (currentSnapshot) {
      setClarificationMessages([])
      setFromPanelEdit(true)
      dialogueProgressAbortRef.current?.abort()
      setDialogueProgress([])
    }
    setDraft(nextDraft)
    setRunId(undefined)
    setRunCommand(undefined)
  }

  const saveInlineStock = async (candidate: ApiInstrument, signal: AbortSignal) => {
    const source = draft
    if (!source || isJourneyLocked || signal.aborted) return
    const edited = cloneDraft(source)
    edited.instrument = { ...candidate }
    edited.strategySpec = { ...edited.strategySpec,
      instrument: { ...edited.strategySpec.instrument, symbol: candidate.symbol } }
    const saved = await strategyApi.revise(edited, false, signal)
    // A new conversation or any intervening draft edit owns the current UI.
    if (signal.aborted || currentDraftRef.current !== source) return
    rerunAfterEdit.current = false
    refreshEditedRun.current = false
    handleDraftChange(saved)
    setBaselineDraft(cloneDraft(saved))
    setConversationTailDraftId(saved.id)
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

  /**
   * 看报告 = 切到详情态的报告分区。
   * 报告是这次回测的产物，它的位置在详情里；对话只负责记录「跑过这一次」，
   * 不再在对话里展开第二份同样的报告。
   */
  const openReportDetail = (snapshot: JourneySnapshot | undefined) => {
    if (!snapshot) return
    setReportSnapshot(snapshot)
    setDetailJourneyId(journeyHistory.some((item) => item.id === snapshot.id) ? snapshot.id : undefined)
    setDetailTab('report')
    setView('detail')
  }
  const detailSnapshot = detailJourneyId
    ? journeyHistory.find((item) => item.id === detailJourneyId) ?? currentSnapshot
    : currentSnapshot
  const detailReview = detailSnapshot?.review ?? (detailSnapshot && reviewMutation.data?.runId === detailSnapshot.id
    ? reviewMutation.data
    : undefined)
  const detailReviewIsActive = reviewMutation.variables?.id === detailSnapshot?.id
  const detailOptimizationIsActive = optimizationMutation.variables?.source.id === detailSnapshot?.id

  /** 报告里的下钻（因果轨迹 / 成交规则）仍然用二级页：它们是从报告再往里的一层。 */
  const reportBodyProps = (snapshot: JourneySnapshot) => ({
    metrics: snapshot.metrics,
    series: snapshot.series,
    marks: snapshot.marks,
    trades: snapshot.trades,
    evidence: snapshot.evidence,
    onOpenChain: openChain,
    onOpenExecution: () => { setReportSnapshot(snapshot); openExecutionDetails() },
    mode: apiMode as 'mock' | 'live',
  })

  const openExecutionDetails = () => open('execution')

  const refreshResults = () => {
    void summaryQuery.refetch()
    void seriesQuery.refetch()
    void activitiesQuery.refetch()
  }

  let failure: FailureState | undefined
  if (runQuery.isError && !resultReady) {
    failure = failureFor(runQuery.error, 'run_read_failed', '任务状态读取失败', instrument, runId)
  } else if (cancelMutation.isError) {
    failure = failureFor(cancelMutation.error, 'cancel_failed', '取消请求没有完成', instrument, runId)
  } else if (runQuery.data?.state === 'failed') {
    failure = failureFor(
      new Error(backtestFailureMessage(runQuery.data)),
      'run_failed',
      '回测失败',
      instrument,
      runId,
    )
  } else if (runQuery.data?.state === 'cancelled') {
    failure = {
      key: 'cancelled', status: 'implemented', title: '用户取消',
      reason: '任务已停止。修改规则或参数后可以重新提交。', actions: ['修改规则'], runId,
    }
  } else if (resultError) {
    failure = failureFor(resultError, 'result_failed', '结果读取不完整', instrument, runId)
  }

  const handleFailureAction = (index: number) => {
    if (index === 0 && failure?.key === 'result_failed') refreshResults()
    else if (index === 0 && ['run_read_failed', 'cancel_failed'].includes(failure?.key ?? '')) {
      void runQuery.refetch()
    } else if (failure?.key === 'run_failed') {
      // Retrying a failed calculation must submit the same draft/settings,
      // not clear the conversation or compile an unrelated example.
      if (!draft || startMutation.isPending) return
      setRunId(undefined)
      setStack([])
      setView('chat')
      if (index === 0) {
        setRunCommand('重新回测')
        startMutation.mutate({ candidate: cloneDraft(draft), refreshData: true })
      } else {
        setRunCommand(undefined)
        startMutation.reset()
        setReviewOpen(true)
      }
    } else if (
      failure?.key === 'data_incomplete'
      || failure?.key === 'event_time_insufficient'
    ) {
      submitText(maCrossExample)
    } else resetForEdit()
  }

  const apiLabel = apiMode === 'mock' ? '界面预览' : '回测服务'
  const activeOverlay = stack.at(-1)
  const compileRecovery = compileMutation.isError
    ? compileRecoveryMessage(compileMutation.error)
    : undefined
  const activeIdeaProposalCards = ideaProposalCards(
    clarification,
    clarificationTarget,
    clarificationSuggestions,
  )
  const hasPairedProposals = activeIdeaProposalCards.some((proposal) => proposal.paired)
  const instrumentSuggestions = (clarification?.instrumentSuggestions?.length
    ? clarification.instrumentSuggestions
    : clarification?.instrumentSuggestion ? [clarification.instrumentSuggestion] : [])
    .filter((item, index, items) => items.findIndex(other => other.symbol === item.symbol) === index)
    .slice(0, 3)
  const pendingStrategyDirection = dialogueProgress
    .filter((event) => event.stage === 'strategy_direction').at(-1)?.message
  const pendingProcessingEvents = dialogueProgress
    .filter((event) => event.stage !== 'strategy_direction')

  useEffect(() => {
    if (!instrumentContextError) return
    window.setTimeout(() => inputRef.current?.focus(), 0)
  }, [instrumentContextError])

  useEffect(() => {
    const node = scrollRef.current
    if (node) {
      node.scrollTop = node.scrollHeight
      setShowScrollToBottom(false)
    }
  }, [
    clarification,
    clarificationMessages.length,
    clarificationPrompt,
    answerMutation.isPending,
    compileMutation.isPending,
    draft,
    journeyHistory.length,
    resultReady,
    runQuery.data?.state,
    submittedText,
  ])

  /*
   * 回测跑完，落点是策略详情：这时候人要看的是结果，不是再回对话里找那张卡。
   *
   * 这一步只能写成 effect：结果就绪是三个查询各自返回后才成立的派生状态，
   * react-query v5 的查询没有 onSuccess 回调可挂，没有事件可以承接它。
   * 识别成功那一次切版式已经放进 mutation 的 onSuccess 了，不走 effect。
   */
  const settledRunId = resultReady ? runId : undefined
  useEffect(() => {
    if (!settledRunId) return
    // eslint-disable-next-line react-hooks/set-state-in-effect -- 见上：异步结果就绪没有对应事件
    setDetailJourneyId(undefined)
    setDetailTab('report')
    setView('detail')
  }, [settledRunId])

  return (
    <div className="app" data-api-mode={apiMode} data-has-overlay={stack.length > 0 ? 'true' : 'false'}>
      <div className="screens">
        <section className="page base" id="pg-chat" aria-hidden={Boolean(activeOverlay)} inert={Boolean(activeOverlay)}
          data-cold={!submittedText && journeyHistory.length === 0 ? 'true' : 'false'}
          data-view={view}
          data-review={view === 'chat' && reviewOpen && draft && uiStrategy ? 'true' : 'false'}>
          {/*
            顶栏按 Public 的产品截图重做：左边是产品自己的字标，右边是「当前上下文 +
            状态」，中间用一条竖发丝线分开——对应它那条 "Buying power … | Brokerage"。
            原来那条宿主导航（返回键 / 东方财富徽标 / 妙想AI / 汉堡菜单）整条去掉了。
          */}
          <header className="topbar">
            <span className="wordmark"><i aria-hidden="true" />策略回测</span>
            {/*
              右侧留空。当前股票和运行模式在下面都各有落点（策略卡带股票、
              详情头部带模式），顶栏再挂一遍只是重复，且把视线拉到一个
              不需要操作的角落。
            */}
          </header>

          <div className="workspace" ref={workspaceRef}>
            {/* 分隔条绝对定位、不占 grid 区域，见 ColumnResizer 里的说明 */}
            <ColumnResizer side="rail" target={workspaceRef} />
            <ColumnResizer side="review" target={workspaceRef} />
          {/*
            左栏对应 Public 的 Agents 侧栏：标题 + 新建 + 历史列表。
            手机端整条收起（历史本来就在对话流里按时间排着，不需要再来一份导航）。
          */}
          <aside className="rail" aria-label="策略与历史">
            <div className="rail-head">
              <h2>回测策略</h2>
            </div>
            <button type="button" className="rail-new" onClick={() => startNewCondition()}
              disabled={isJourneyLocked}>
              <svg width="17" height="17" viewBox="0 0 18 18" fill="none" aria-hidden="true">
                <path d="M10.6 2.9H4.2A1.7 1.7 0 0 0 2.5 4.6v9.2a1.7 1.7 0 0 0 1.7 1.7h9.2a1.7 1.7 0 0 0 1.7-1.7V7.4"
                  stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
                <path d="m12.4 2.2 3.4 3.4-4.6 1.2 1.2-4.6Z" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
              </svg>
              新建策略
            </button>
            <div className="rail-sect">本次会话</div>
            <button type="button" className="rail-new" onClick={startNewConversation}
              disabled={isJourneyLocked} aria-label="新建会话并清空上下文">
              <svg width="17" height="17" viewBox="0 0 18 18" fill="none" aria-hidden="true">
                <path d="M14.5 9a5.5 5.5 0 1 1-1.6-3.9M14.5 3.8v3.6h-3.6"
                  stroke="currentColor" strokeWidth="1.4" strokeLinecap="round"
                  strokeLinejoin="round" />
              </svg>
              新建会话（清空上下文）
            </button>
            <nav className="rail-list">
              {journeyHistory.map((journey) => (
                <button key={journey.id} type="button"
                  className={`rail-item${view === 'detail' && detailSnapshot?.id === journey.id ? ' is-active' : ''}`}
                  onClick={() => { setDetailJourneyId(journey.id); setDetailTab('report'); setView('detail') }}>
                  <i aria-hidden="true" />
                  <span>{strategyTitle(journey.instrument, journey.strategy)}</span>
                </button>
              ))}
              {draft && uiStrategy && submittedText ? (
                <button type="button"
                  className={`rail-item${view === 'chat' ? ' is-active' : ''}`}
                  onClick={() => { setDetailJourneyId(undefined); setView('chat'); setReviewOpen(true) }}>
                  <i aria-hidden="true" />
                  <span>{strategyTitle(toUiInstrument(draft), uiStrategy)}</span>
                </button>
              ) : null}
              {journeyHistory.length === 0 && !submittedText ? (
                <p className="rail-empty">还没有回测记录</p>
              ) : null}
            </nav>
          </aside>

          <div className="scroll" ref={scrollRef} onScroll={(event) => {
            const node = event.currentTarget
            setShowScrollToBottom(node.scrollHeight - node.scrollTop - node.clientHeight > 80)
          }}>
            <div className="stream" aria-live="polite">
              <DayDivider>当前会话</DayDivider>

              {journeyHistory.map((journey) => (
                <Fragment key={journey.id}>
                  {!journey.fromPanelEdit
                    ? <Turn id={`journey-${journey.id}`} mine><Bubble>{journey.utterance}</Bubble></Turn>
                    : null}
                  {journey.clarificationMessages.map((message, index) => (
                    <Turn key={`${journey.id}-clarification-${index}`} mine={message.role === 'user'}>
                      {message.role === 'user'
                        ? <Bubble>{message.text}</Bubble>
                        : <Say>{message.text}</Say>}
                    </Turn>
                  ))}
                  <Turn id={journey.fromPanelEdit ? `journey-${journey.id}` : undefined}>
                    <ThinkBlock
                      title="策略与回测摘要"
                      meta="历史记录"
                      lines={[
                        `买入：${summarizeRule(journey.strategy.entryRule)}`,
                        `卖出：${summarizeRule(journey.strategy.exitRule)}`,
                        '已提交并完成回测，这张卡片保留为不可编辑的记录',
                      ]}
                    />
                    <StrategyCard
                      instrument={journey.instrument}
                      strategy={journey.strategy}
                      onEditRow={() => undefined}
                      onOpenMore={() => undefined}
                      onRun={() => undefined}
                      settled="已完成回测"
                      executionSummary={summarizeExecution(journey.draft)}
                    />
                  </Turn>
                  {/* 历史回合里也保留那句「开始回测」，往回翻时这一步不会凭空消失 */}
                  <Turn mine><Bubble>{RUN_COMMAND}</Bubble></Turn>
                  <Turn>
                    <ResultCard
                      metrics={journey.metrics}
                      series={journey.series}
                      marks={journey.marks}
                      onOpenReport={() => openReportDetail(journey)}
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
                      ? '还没有识别到当前股票。你可以直接输入股票名称或 6 位代码，也可以连同买入和卖出条件一起告诉我。'
                      : strategySlots
                        ? '改一下下面的股票或买卖条件，再继续回测。'
                      : journeyHistory.length > 0
                        ? <>说出新的买卖规则，继续回测。</>
                        : <>想怎么交易？用一句话告诉我，我来帮你把它变成可回测的策略。</>}
                  </Say>
                  {/*
                    示例只在冷启动时出现：它的作用是告诉第一次来的人「一句话可以写成什么样」。
                    跑过一轮之后用户已经知道怎么写，再把同样两条推一遍既占地方，
                    也像是在暗示「你应该选我给的这几个」。
                  */}
                </Turn>
              ) : !fromPanelEdit ? <Turn mine><Bubble>{submittedText}</Bubble></Turn> : null}

              {clarificationMessages.map((message, index) => index === readyAssistantMessageIndex ? null : (
                <Turn key={`clarification-${index}`} mine={message.role === 'user'}>
                  {message.role === 'user'
                    ? <Bubble>{message.text}</Bubble>
                    : <Say>{message.text}</Say>}
                  {message.role === 'assistant' && message.data
                    ? <CurrentDataResult data={message.data} onAnalyze={analyzeCurrentInstrument} />
                    : null}
                </Turn>
              ))}

              {compileMutation.isPending ? (
                <Turn>
                  {pendingStrategyDirection ? <Say>{pendingStrategyDirection}</Say> : null}
                  <ThinkingStream
                    status={pendingStrategyDirection ? '正在准备组合' : '正在理解你的想法'}
                    summary={pendingProcessingEvents}
                  />
                </Turn>
              ) : null}

              {answerMutation.isPending ? (
                <Turn>
                  {pendingStrategyDirection ? <Say>{pendingStrategyDirection}</Say> : null}
                  <ThinkingStream
                    status={pendingStrategyDirection ? '正在准备组合' : '正在理解你的想法'}
                    summary={pendingProcessingEvents}
                  />
                </Turn>
              ) : null}

              {clarificationPrompt && !compileMutation.isPending && !answerMutation.isPending ? (
                <Turn>
                  <ModelReasoning events={dialogueProgress} />
                  <Say>{clarificationPrompt}</Say>
                  {!clarificationRequestFailed && clarification?.backtestReview ? (() => {
                    const review = clarification.backtestReview
                    const source = [...journeyHistory, ...(currentSnapshot ? [currentSnapshot] : [])]
                      .find(item => item.id === review.runId)
                    return <>
                      <Chips>{review.optimizationCandidates.slice(0, 3).map(candidate => (
                        <Chip key={candidate.id} disabled={isJourneyLocked || !source}
                          onClick={() => {
                            if (source) optimizationMutation.mutate({ source, candidate })
                          }}>用「{candidate.title}」再回测</Chip>
                      ))}</Chips>
                      {optimizationMutation.isError
                        ? <Say>{errorMessage(optimizationMutation.error)}</Say> : null}
                    </>
                  })() : null}
                  {!clarificationRequestFailed && instrumentSuggestions.length
                    && (!hasPairedProposals || clarification?.instrumentSuggestions?.length) ? (
                    <div className="stock-suggestions" aria-label="可选股票">
                      {instrumentSuggestions.some(item => item.source === 'eastmoney_mx_screener') ? (
                        <details className="idea-research stock-suggestions__evidence">
                          <summary>东方财富选股依据</summary>
                          {instrumentSuggestions.map(item => (
                            <p key={item.symbol}><strong>{item.name ?? item.symbol}</strong>：{item.evidence}</p>
                          ))}
                          <p className="stock-suggestions__time">取数时间：{instrumentSuggestions[0]?.retrieved_at}</p>
                        </details>
                      ) : null}
                      <Chips>
                        {instrumentSuggestions.map(item => (
                          <Chip key={item.symbol} disabled={isJourneyLocked}
                            onClick={() => {
                              const proposal = clarification?.ideaRoute?.proposals
                                .find(option => option.instrument_symbol === item.symbol)
                              if (proposal) submitIdeaProposal(proposal.id)
                              else submitText(`用${item.name ? `${item.name}（${item.symbol}）` : item.symbol}`)
                            }}>
                            {item.name ?? item.symbol}
                          </Chip>
                        ))}
                      </Chips>
                    </div>
                  ) : null}
                  {clarificationData ? (
                    <CurrentDataResult
                      data={clarificationData}
                      onAnalyze={analyzeCurrentInstrument}
                    />
                  ) : null}
                  {!clarificationRequestFailed && clarification?.provisionalDraft ? (() => {
                    const provisional = clarification.provisionalDraft
                    const trees = strategyRuleTrees(provisional)
                    return (
                      <div className="provisional-strategy" data-testid="provisional-strategy">
                        <div className="provisional-strategy__meta">按你的口语推测 · 等你确认</div>
                        <div className="provisional-strategy__rule">
                          <span>买入</span>{summarizeRule(trees.entry)}
                        </div>
                        <div className="provisional-strategy__rule">
                          <span>卖出</span>{summarizeRule(trees.exit)}
                        </div>
                        <div className="provisional-strategy__note">
                          {clarification.provisionalNote ?? '这只是暂时理解，不会自动开始回测。'}
                        </div>
                      </div>
                    )
                  })() : null}
                  {!clarificationRequestFailed && clarification?.ideaRoute ? (
                    <>
                      <IdeaResearchEvidence clarification={clarification} />
                      {activeIdeaProposalCards.length > 0 && !clarification.instrumentSuggestions?.length ? (
                        <Proposals
                          items={activeIdeaProposalCards}
                          onPick={submitIdeaProposal}
                        />
                      ) : null}
                    </>
                  ) : !clarificationRequestFailed && clarificationSuggestions.length > 0 ? (
                    <Proposals
                      items={clarificationSuggestions.map((suggestion) => ({
                        id: suggestion.id,
                        title: suggestion.title,
                        detail: suggestion.preview,
                      }))}
                      onPick={(id) => {
                        const suggestion = clarificationSuggestions.find((item) => item.id === id)
                        if (suggestion) submitClarificationSuggestion(suggestion)
                      }}
                    />
                  ) : !clarificationRequestFailed
                    && clarification && visibleClarificationChoices(clarification).length ? (
                    <Proposals
                      items={visibleClarificationChoices(clarification).map((choice) => ({
                        id: choice.id,
                        title: choice.label,
                        detail: choice.description,
                      }))}
                      onPick={(id) => {
                        const choice = visibleClarificationChoices(clarification)
                          .find((item) => item.id === id)
                        if (choice) submitClarificationChoice(choice)
                      }}
                    />
                  ) : null}
                </Turn>
              ) : null}

              {draft && uiStrategy ? (
                <Turn>
                  <ModelReasoning events={dialogueProgress} />
                  {readyAssistantMessage ? <Say>{readyAssistantMessage.text}</Say> : null}
                  {readyAssistantMessage?.data
                    ? <CurrentDataResult data={readyAssistantMessage.data} onAnalyze={analyzeCurrentInstrument} />
                    : null}
                  {apiMode === 'mock' && draft.entry.conditions.some((condition) =>
                    condition.kind === 'event' && condition.documentText) ? (
                      <Notice tone="info">
                        界面预览只展示规则识别和卡片结构；没有读取年报正文，也没有计算词频。
                      </Notice>
                    ) : null}
                  <button type="button" className="preview-card" onClick={() => setReviewOpen(true)}>
                    <span className="preview-eyebrow">预览策略</span>
                      <span className="preview-name">{strategyTitle(toUiInstrument(draft), uiStrategy)}</span>
                    <svg width="18" height="18" viewBox="0 0 18 18" fill="none" aria-hidden="true">
                      <path d="M1.6 9S4.5 3.8 9 3.8 16.4 9 16.4 9 13.5 14.2 9 14.2 1.6 9 1.6 9Z"
                        stroke="currentColor" strokeWidth="1.3" />
                      <circle cx="9" cy="9" r="2.1" fill="currentColor" />
                    </svg>
                  </button>
                </Turn>
              ) : null}

              {runCommand ? <Turn mine><Bubble>{runCommand}</Bubble></Turn> : null}

              {startMutation.isPending ? (
                <Turn>
                  <ThinkingStream
                    status="正在准备历史回测"
                  />
                </Turn>
              ) : null}

              {runQuery.data && !terminalStates.has(runQuery.data.state) ? (
                <Turn>
                  <RunningCard phase={runQuery.data.state} progressLabel={runQuery.data.progressLabel}
                    onCancel={() => cancelMutation.mutate()}
                    isCancelling={cancelMutation.isPending} isMock={apiMode === 'mock'} />
                </Turn>
              ) : null}

              {resultLoading ? (
                <Turn>
                  <ThinkingStream
                    status="正在整理回测结果"
                  />
                </Turn>
              ) : null}

              {draft && metrics && evidence && resultReady ? (
                <>
                  <Turn>
                    <ResultCard metrics={metrics} series={series} marks={marks}
                      onOpenReport={() => openReportDetail(currentSnapshot)} />
                  </Turn>
                </>
              ) : null}



              {failure ? (
                <Turn><FailureCard state={failure} onAction={handleFailureAction} /></Turn>
              ) : null}

              {compileRecovery ? <Turn><Say>{compileRecovery}</Say></Turn> : null}
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

          {/*
            底部只留输入区。买卖、语音、「+」、深度思考那一排能力胶囊、iOS home 指示条
            都是宿主外壳，不属于这个产品——留着只会让人以为这里能下单。
          */}
          {/*
            策略详情 —— 对应 Public 创建完 Agent 后落到的那一页：
            返回、大衬线标题、一句话描述、标签、运行信息条，然后是 tab 切换的正文。
            它和对话共用同一块工作区，只是 grid 把中间那栏收掉了。
          */}
          {detailSnapshot ? (
            <section className="detail" aria-label="策略详情">
              <div className="detail-inner">
                <div className="detail-top">
                  <button type="button" className="detail-back" aria-label="回到对话"
                    onClick={() => setView('chat')}>
                    <svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true">
                      <path d="M12 4.5 6.5 10l5.5 5.5" stroke="currentColor" strokeWidth="1.6"
                        strokeLinecap="round" strokeLinejoin="round" />
                    </svg>
                  </button>
                  <h1 className="detail-title">
                    {strategyTitle(detailSnapshot.instrument, detailSnapshot.strategy)}
                  </h1>
                  <button type="button" className="detail-edit"
                    onClick={() => { setView('chat'); setReviewOpen(true) }}>
                    编辑策略
                  </button>
                </div>
                {/*
                  只留一行身份信息。买卖规则在「工作流」分区里逐条列着，数据区间既是
                  工作流的一个步骤、报告里也自带一行——头部再抄一遍就是三处同义重复。
                */}
                <p className="detail-meta">
                  <b>{detailSnapshot.instrument.name}</b>
                  <span className="num">{detailSnapshot.instrument.code}</span>
                  <em>{apiLabel}</em>
                </p>

                <div className="detail-tabs" role="tablist" aria-label="策略详情分区">
                  <button type="button" role="tab" id="detail-tab-flow"
                    aria-selected={detailTab === 'flow'} aria-controls="detail-panel-flow"
                    className={detailTab === 'flow' ? 'on' : undefined}
                    onClick={() => setDetailTab('flow')}>工作流</button>
                  <button type="button" role="tab" id="detail-tab-report"
                    aria-selected={detailTab === 'report'} aria-controls="detail-panel-report"
                    className={detailTab === 'report' ? 'on' : undefined}
                    onClick={() => setDetailTab('report')}>回测报告</button>
                </div>

                <div className="detail-panel" id="detail-panel-flow" role="tabpanel"
                  aria-labelledby="detail-tab-flow" hidden={detailTab !== 'flow'}>
                  <h2 className="detail-sub">这条策略怎么跑</h2>
                  <div className="erows erows--readonly">
                    {detailSnapshot.strategy.rows.map((row) => (
                      <div key={row.key} className={`erow${row.kind ? ` ${row.kind}` : ''} off`}>
                        <span className="lb">{row.label}</span>
                        <span className="val">
                          {row.value}
                          {row.sub ? <small>{row.sub}</small> : null}
                        </span>
                      </div>
                    ))}
                  </div>
                  <ExecutionEntry onOpen={() => {
                    setReportSnapshot(detailSnapshot)
                    openExecutionDetails()
                  }} />
                </div>

                <div className="detail-panel" id="detail-panel-report" role="tabpanel"
                  aria-labelledby="detail-tab-report" hidden={detailTab !== 'report'}>
                  <div id="pg-report">
                    <h2 className="sr-only">回测报告</h2>
                    <ReportBody {...reportBodyProps(detailSnapshot)} showExecutionEntry={false} />
                    <BacktestReview
                      review={detailReview}
                      describeCandidate={(candidate) => executableRuleText(
                        fromBacktestOptimizationCandidate(candidate, detailSnapshot.draft, capabilitiesQuery.data),
                      )}
                      progress={reviewProgress?.runId === detailSnapshot.id
                        ? reviewProgress.events : undefined}
                      isLoading={detailReviewIsActive && reviewMutation.isPending}
                      error={detailReviewIsActive && reviewMutation.isError
                        ? errorMessage(reviewMutation.error)
                        : reviewContextError?.runId === detailSnapshot.id
                          ? reviewContextError.message : undefined}
                      onRequest={() => reviewMutation.mutate(detailSnapshot)}
                      onChangeInstrument={() => resumeStrategyMutation.mutate({
                        source: detailSnapshot, mode: 'stock',
                      })}
                      onChangeRules={() => resumeStrategyMutation.mutate({
                        source: detailSnapshot, mode: 'rules',
                      })}
                      onRunCandidate={(candidate) => optimizationMutation.mutate({
                        source: detailSnapshot,
                        candidate,
                      })}
                      runningCandidateId={detailOptimizationIsActive && optimizationMutation.isPending
                        ? optimizationMutation.variables?.candidate.id
                        : undefined}
                      runError={detailOptimizationIsActive && optimizationMutation.isError
                        ? errorMessage(optimizationMutation.error)
                        : resumeStrategyMutation.isError ? errorMessage(resumeStrategyMutation.error)
                        : undefined}
                      candidateActionsDisabled={isJourneyLocked}
                    />
                  </div>
                </div>


              </div>
            </section>
          ) : null}

          {/*
            策略审阅栏 —— 对应 Public 的「Review your Agent」。
            桌面端是常驻右栏，手机端塌成输入框上方的可折叠面板；同一份 DOM，
            靠 .workspace 的 grid-template-areas 换位置，没有为断点分叉的 JSX。
          */}
          {draft && uiStrategy ? (
            <aside className="review" aria-label="策略审阅">
              <div className="review-head">
                <span className="review-eyebrow">审阅你的策略</span>
                <span className="review-title">{strategyTitle(toUiInstrument(draft), uiStrategy)}</span>
                <button type="button" className="review-close" aria-label="收起策略审阅"
                  onClick={() => setReviewOpen(false)}>
                  <svg width="18" height="18" viewBox="0 0 18 18" fill="none" aria-hidden="true">
                    <path d="m4.6 4.6 8.8 8.8M13.4 4.6l-8.8 8.8" stroke="currentColor"
                      strokeWidth="1.5" strokeLinecap="round" />
                  </svg>
                </button>
              </div>
              <div className="review-body">
                <StrategyCard
                  instrument={toUiInstrument(draft)}
                  strategy={uiStrategy}
                  stockEditor={{ onSearch: instrumentApi.search, onSave: saveInlineStock }}
                  onEditRow={openParams}
                  onOpenMore={() => openParams('more')}
                  onRun={() => {
                    if (!canStart) return
                    setRunCommand(RUN_COMMAND)
                    startMutation.mutate({ candidate: draft })
                  }}
                  isStarting={startMutation.isPending}
                  isLocked={isJourneyLocked}
                  settled={runQuery.data?.state === 'succeeded' ? '已完成回测' : undefined}
                  editableAfterRun
                  canStart={canStart}
                  disabledReason={startDisabledReason}
                  error={startMutation.isError ? errorMessage(startMutation.error) : undefined}
                  executionSummary={summarizeExecution(draft)}
                />
              </div>
            </aside>
          ) : null}

          <div className="dock">
            {strategySlots ? (
              <StrategySlotComposer key={`${strategySlots.draft.id}@${strategySlots.draft.revision}`} {...strategySlots}
                disabled={isJourneyLocked || compileMutation.isPending}
                onSubmit={submitText}
                onClear={() => {
                  setStrategySlots(undefined)
                  setUtterance('')
                  window.setTimeout(() => inputRef.current?.focus(), 0)
                }} />
            ) : <form className="inputbar" onSubmit={(event) => { event.preventDefault(); submitText(utterance) }}>
              <input ref={inputRef} className="strategy-input" aria-label="交易规则" value={utterance}
                disabled={isJourneyLocked} onChange={(event) => setUtterance(event.target.value)}
                placeholder={needsLimitUpRuleRewrite
                  ? '改用价格、涨跌幅或技术指标条件'
                  : clarification
                    ? clarificationPlaceholder(clarification)
                    : compileMutation.isError
                      ? '补充完整规则，或直接换一种说法'
                      : instrumentContextError
                        ? '输入股票名称、买入和卖出条件'
                        : '说出什么时候买、什么时候卖'} />
              <button type="submit" className="send" aria-label="识别交易规则"
                disabled={isJourneyLocked || compileMutation.isPending || !utterance.trim()}>
                <svg width="18" height="18" viewBox="0 0 18 18" fill="none" aria-hidden="true">
                  <path d="M9 14V4M4.6 8.4 9 4l4.4 4.4" stroke="currentColor" strokeWidth="1.8"
                    strokeLinecap="round" strokeLinejoin="round" />
                </svg>
              </button>
            </form>}

            {/* 冷启动的示例句排在输入框下方，对应 Public 首屏输入框下那排分类 chip */}
            {!submittedText && !instrumentContextError && journeyHistory.length === 0 ? (
              <div className="home-examples" aria-label="策略示例">
                <Chips>
                  {DEFAULT_STRATEGY_EXAMPLES.map(example => (
                    <Chip key={example.instrument.symbol} onClick={() => submitText(example.utterance, {
                      instrumentOverride: example.instrument,
                    })}>{example.utterance}</Chip>
                  ))}
                </Chips>
              </div>
            ) : null}

            <p className="disclaimer">仅做历史回测，不构成投资建议</p>
          </div>

          {/* 冷启动时给下方留一段空白，让「标题 + 输入框」落在视觉中线偏上 */}
          <div className="coldpad" aria-hidden="true" />
          </div>
        </section>

        {draft ? (
          <ParamsScreen open={activeOverlay === 'params'} onBack={back} draft={draft}
            onChange={handleDraftChange}
            onReset={() => baselineDraft && setDraft(cloneDraft(baselineDraft))}
            isLocked={isJourneyLocked} focus={paramsFocus} />
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
