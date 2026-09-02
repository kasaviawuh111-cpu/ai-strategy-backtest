export type Instrument = {
  symbol: string
  name: string
  market: 'CN_A'
  exchange: 'SZSE' | 'SSE' | 'BSE'
}

export type StrategyParameter = {
  key: string
  label: string
  value: number
  integer?: boolean
  unit?: string
  min: number
  max: number
}

export type StrategySpecIndicatorCondition = {
  type: 'indicator_condition'
  indicator_id: string
  definition_version: string
  params: Record<string, string | number | boolean>
  timeframe: '1d'
  evaluation_mode: 'bar_close_confirmed'
  trigger: string
  value: number | null
}

export type StrategySpecEventCondition = {
  type: 'event_condition'
  event_code: string
  definition_version: string
  trigger: 'published'
  attributes: Record<string, string | number | boolean>
  document_text?: StrategySpecDocumentTextPredicate | null
}

export type StrategySpecFinancialCondition = {
  type: 'financial_condition'
  metric_id: string
  definition_version: '1.0.0'
  report_type: 'q1' | 'semiannual' | 'q3' | 'annual' | null
  period_basis: 'single_quarter' | 'ytd_cumulative' | 'full_year' | 'ttm' | 'mrq' | 'point_in_time' | null
  statement_scope: 'consolidated' | 'parent_company' | null
  revision_policy: 'as_known_at_signal'
  comparator: 'gt' | 'gte' | 'lt' | 'lte' | 'eq' | 'ne'
  value: number | string
  unit: 'CNY' | 'CNY_PER_SHARE' | 'PERCENT' | 'RATIO' | 'TIMES'
}

export type StrategySpecDocumentTextPredicate = {
  metric_id: 'document.literal_mention_count'
  metric_version: '1.0.0'
  term: string
  normalization: 'nfkc'
  match_mode: 'ascii_token' | 'literal'
  case_sensitive: boolean
  comparator: 'gt' | 'gte'
  value: number
}

export type StrategySpecHoldingPeriodExit = {
  type: 'holding_period_exit'
  sessions: number
  anchor: 'first_entry_fill'
  count_mode: 'subsequent_trading_sessions'
  execution: 'target_session_open_proxy'
}

export type StrategySpecPositionReturnExit = {
  type: 'position_return_exit'
  trigger: 'take_profit' | 'stop_loss'
  threshold_pct: number
  anchor: 'first_entry_fill'
  observation: 'back_adjusted_daily_close'
  evaluation_mode: 'bar_close_confirmed'
  execution: 'next_tradable_session_open'
}

export type StrategySpecTrailingDrawdownExit = {
  type: 'trailing_drawdown_exit'
  threshold_pct: number
  anchor: 'first_entry_fill'
  peak_basis: 'back_adjusted_daily_close'
  evaluation_mode: 'bar_close_confirmed'
  execution: 'next_tradable_session_open'
}

export type StrategySpecCondition =
  | StrategySpecIndicatorCondition
  | StrategySpecFinancialCondition
  | StrategySpecEventCondition
  | { type: 'all' | 'any'; children: StrategySpecCondition[] }
  | { type: 'not'; child: StrategySpecCondition }

export type StrategySpecExitRule =
  | StrategySpecCondition
  | StrategySpecHoldingPeriodExit
  | StrategySpecPositionReturnExit
  | StrategySpecTrailingDrawdownExit

export type StrategySpec = {
  schema_version: 'strategy.v1'
  catalog: {
    catalog_id: string
    release_version: string
  }
  instrument: {
    market: 'CN_A'
    symbol: string
    position_mode: 'long_only'
  }
  entry: StrategySpecCondition
  exit: {
    op: 'first_of'
    children: StrategySpecExitRule[]
  }
  execution: {
    timezone: 'Asia/Shanghai'
    entry_policy: 'next_tradable_session_open'
    exit_policy: 'next_tradable_session_open'
    data_capability:
      | 'daily_ohlcv'
      | 'daily_ohlcv_events'
      | 'daily_ohlcv_financials'
      | 'daily_ohlcv_events_financials'
    execution_resolution: '1d'
    evaluation_frequency:
      | '1d_close'
      | 'event_available_plus_1d_close'
      | 'financial_available_plus_1d_close'
      | 'event_financial_available_plus_1d_close'
    position_policy: 'single_position_no_pyramiding'
    t_plus_one: true
  }
  backtest: {
    start: string
    end: string
    initial_cash_cny: number
  }
}

type StrategyConditionBase = {
  id: string
  label: string
  trigger: string
}

export type StrategyIndicatorCondition = StrategyConditionBase & {
  kind: 'indicator'
  indicatorId: string
  timeframe: '1d'
  evaluationMode: 'bar_close_confirmed' | 'intrabar_streaming'
  parameters: StrategyParameter[]
}

export type StrategyEventCondition = StrategyConditionBase & {
  kind: 'event'
  eventCode: string
  attributes: Record<string, string | number | boolean>
  documentText?: StrategySpecDocumentTextPredicate | null
}

export type StrategyFinancialCondition = StrategyConditionBase & {
  kind: 'financial'
  metricId: string
  comparator: 'gt' | 'gte' | 'lt' | 'lte' | 'eq' | 'ne'
  value: number | string
  unit: 'CNY' | 'CNY_PER_SHARE' | 'PERCENT' | 'RATIO' | 'TIMES'
  reportType: 'q1' | 'semiannual' | 'q3' | 'annual' | null
  periodBasis: 'single_quarter' | 'ytd_cumulative' | 'full_year' | 'ttm' | 'mrq' | 'point_in_time' | null
}

export type StrategyHoldingPeriodCondition = StrategyConditionBase & {
  kind: 'holding_period'
  sessions: number
  anchor: 'first_entry_fill'
  countMode: 'subsequent_trading_sessions'
  execution: 'target_session_open_proxy'
}

export type StrategyPositionReturnCondition = StrategyConditionBase & {
  kind: 'position_return'
  exitTrigger: 'take_profit' | 'stop_loss'
  thresholdPct: number
  anchor: 'first_entry_fill'
  observation: 'back_adjusted_daily_close'
  evaluationMode: 'bar_close_confirmed'
  execution: 'next_tradable_session_open'
  editable: false
}

export type StrategyTrailingDrawdownCondition = StrategyConditionBase & {
  kind: 'trailing_drawdown'
  thresholdPct: number
  anchor: 'first_entry_fill'
  peakBasis: 'back_adjusted_daily_close'
  evaluationMode: 'bar_close_confirmed'
  execution: 'next_tradable_session_open'
  editable: false
}

export type StrategyCondition =
  | StrategyIndicatorCondition
  | StrategyFinancialCondition
  | StrategyEventCondition
  | StrategyHoldingPeriodCondition
  | StrategyPositionReturnCondition
  | StrategyTrailingDrawdownCondition

export type StrategyLeg = {
  operator: 'all' | 'any' | 'first_of'
  conditions: StrategyCondition[]
}

export type PriceLimitMode =
  | 'wait_for_unlock'
  | 'strict_no_fill_at_limit'
  | 'allow_limit_volume'

export type CapacityMode = 'point_in_time_volume' | 'unlimited'

export type ExecutionPolicy = {
  entryPolicy: 'next_tradable_session_open'
  exitPolicy: 'next_tradable_session_open'
  priceLimitMode: PriceLimitMode
  tPlusOne: true
  dataCapability:
    | 'daily_ohlcv'
    | 'daily_ohlcv_events'
    | 'daily_ohlcv_financials'
    | 'daily_ohlcv_events_financials'
  evaluationFrequency:
    | '1d_close'
    | 'event_available_plus_1d_close'
    | 'financial_available_plus_1d_close'
    | 'event_financial_available_plus_1d_close'
  capacityMode: CapacityMode
  participationRate: number
  allocationRatio: number
  commissionRate: number
  minimumCommissionCny: number
  slippageBps: number
  retryUnfilledExits: boolean
  maxExitAttempts: number
  warmupCalendarDays: number
  settlementExtensionDays: number
  runRobustness: boolean
}

export type BacktestWindow = {
  start: string
  end: string
  initialCashCny: number
}

export type StrategyDraft = {
  id: string
  revision: number
  /** 后端对最终 StrategySpec 计算的 canonical SHA-256；Mock 只能提供演示身份。 */
  strategyHash: string | null
  sourceText: string
  title: string
  instrument: Instrument
  confidence: number | null
  entry: StrategyLeg
  exit: StrategyLeg
  execution: ExecutionPolicy
  backtest: BacktestWindow
  assumptions: string[]
  warnings: string[]
  strategySpec: StrategySpec
  /** 只有 Live API 返回时才存在；前端不推断、不补造候选来源。 */
  interpretationEvidence?: {
    candidateProvenance: CandidateProvenanceItem | null
    grounding: CandidateGroundingPayload | null
    alternatives: CandidateAlternativeItem[]
    rejections: CandidateRejectionItem[]
  }
}

export type CandidateProvenanceItem = {
  source: 'bounded_provider'
  provider: string
  model: string
  prompt_version: string
  schema_version: string
  capability_projection_version: string
  capability_projection_hash: string
  upstream_pattern_commit: string
  candidate_rank: number
}

export type CandidateGroundingItem = {
  path: string
  start: number
  end: number
  text: string
}

export type CandidateGroundingPayload = {
  matched_spans: string[]
  spans: CandidateGroundingItem[]
}

export type CandidateAlternativeItem = {
  candidate_rank: number
  strategy_hash: string
}

export type CandidateRejectionItem = {
  candidate_rank: number
  diagnostic_code: string
}

export type IdeaRouteProposal = {
  id: string
  title: string
  hypothesis: string
  entry_summary: string
  exit_summary: string
  suggested_utterance: string
  capability_ids: string[]
  assumptions: string[]
  confidence: number
}

export type IdeaRoute = {
  schema_version: 'idea-route.v1'
  understanding: string
  hypothesis: string
  asset_mapping: {
    instrument_symbol: string | null
    relation: 'current_page_proxy' | 'unbound'
    rationale: string
    evidence_status: 'host_context_only' | 'instrument_required'
  }
  proposals: IdeaRouteProposal[]
  provenance?: {
    source: 'bounded_provider'
    provider: string
    model: string
    prompt_version: string
    schema_version: string
    capability_projection_version: string
    capability_projection_hash: string
    upstream_pattern_commit: string
  } | null
}

export type ClarificationChoice = {
  id: string
  label: string
  description: string
  recommended?: boolean
  action?: 'submit_clarification' | 'edit_utterance' | 'replace_and_compile'
  suggestedUtterance?: string
  /** 服务端已确认的候选标的；仅用于下一次编译上下文。 */
  instrumentSymbol?: string
  /** 与上述代码同次服务端 grounding 得到的公司名。 */
  instrumentName?: string
}

export type Clarification = {
  id: string
  question: string
  reason: string
  choices: ClarificationChoice[]
  /** 仅用于把已经识别的片段留在卡片里；不等于可执行 StrategySpec。 */
  recognized?: Array<{ label: string; value: string }>
  /** 观点引导只生成待用户选择的候选；选择前没有 StrategySpec，也不能回测。 */
  ideaRoute?: IdeaRoute
}

export type CompileRequest = {
  instrument: Instrument
  instrumentContextSource?: 'stock_page' | 'standalone_default'
  utterance: string
  clarification?: { id: string; choiceId: string }
}

export type CompileResponse =
  | { status: 'compiled'; draft: StrategyDraft }
  | {
      status: 'needs_clarification'
      draftId: string
      /** Live drafts are revision-bound; Mock responses may omit this legacy field. */
      revision?: number
      clarification: Clarification
    }

export type ClarificationSuggestion = {
  id: string
  title: string
  preview: string
}

export type ClarificationAnswerInput = {
  draftId: string
  revision?: number
  answer: string
  originalRequest: CompileRequest
  clarification: Clarification
}

export type ClarificationAnswerOutcome = {
  replyKind: 'accepted' | 'clarification'
  assistantMessage: string
  suggestions: ClarificationSuggestion[]
  outcome: CompileResponse
}

export type CapabilityParameter = {
  name: string
  value_type: 'integer' | 'number' | 'string' | 'boolean'
  required: boolean
  default: string | number | boolean | null
  minimum: number | null
  maximum: number | null
  choices: Array<string | number | boolean>
  display_name: string | null
  unit: string | null
}

export type CapabilityTriggerDefinition = {
  id: string
  display_name: string | null
  description: string | null
  value_requirement: 'required' | 'forbidden'
  minimum: number | null
  maximum: number | null
  unit: string | null
  exclusive_minimum: boolean
  exclusive_maximum: boolean
}

export type IndicatorCapability = {
  indicator_id: string
  definition_version: string
  status: 'stable' | 'experimental' | 'unavailable'
  display_name: string
  description: string
  warmup_bars: number
  timeframes: ['1d']
  evaluation_modes: ['bar_close_confirmed']
  triggers: string[]
  parameters: CapabilityParameter[]
  trigger_definitions: CapabilityTriggerDefinition[]
}

export type EventDocumentTextCapability = {
  catalog_available: boolean
  backtest_available: boolean
  preparation_available: boolean
  availability_scope: 'pinned_snapshot' | 'request_preparation' | 'unavailable'
  unavailable_reason:
    | 'not_catalog_available'
    | 'snapshot_coverage_unavailable'
    | 'preparation_required'
    | null
}

export type EventCapability = {
  event_code: string
  definition_version: string
  catalog_status: 'stable'
  status: 'available' | 'unavailable'
  backtest_available: boolean
  preparation_available: boolean
  availability_scope: 'pinned_snapshot' | 'request_preparation' | 'unavailable'
  unavailable_reason: 'snapshot_coverage_unavailable' | 'preparation_required' | null
  /** Required by capabilities v2; optional here only so older responses fail closed instead of crashing. */
  document_text?: EventDocumentTextCapability
  triggers: ['published']
}

export type CapabilitiesResponse = {
  markets: ['CN_A']
  input_modes: ['natural_language_zh']
  strategy_scopes: ['single_instrument', 'long_only']
  indicators: IndicatorCapability[]
  events: EventCapability[]
  execution_policies: ['next_tradable_session_open']
  event_catalog_status: 'published'
  event_backtest_available: boolean
  event_preparation_available: boolean
  event_availability_scope: 'pinned_snapshot' | 'request_preparation' | 'unavailable'
  backtest_execution_available: boolean
  limits: {
    max_body_bytes: number
    max_utterance_characters: 2000
    max_instrument_context_characters: 32
  }
}

export type RunState =
  | 'queued'
  | 'running:data'
  | 'running:signal'
  | 'running:execution'
  | 'running:report'
  | 'cancel_requested'
  | 'succeeded'
  | 'failed'
  | 'cancelled'

export type BacktestRun = {
  id: string
  state: RunState
  progress: number
  progressLabel: string
  createdAt: string
  updatedAt: string
  fingerprint: string
  error: string | null
  resultAvailable: boolean
  resultHash?: string | null
  replayed?: boolean
  cancellationRequested?: true
}

export type BacktestSummary = {
  runId: string
  totalReturn: number | null
  benchmarkReturn: number | null
  benchmarkComparisonStatus: 'comparable' | 'strategy_entry_not_filled' | 'benchmark_entry_not_filled' | 'benchmark_unavailable'
  annualizedReturn: number | null
  maxDrawdown: number | null
  sharpeRatio: number | null
  winRate: number | null
  tradeCount: number
  initialCashCny: number | null
  finalEquityCny: number
  interpretation: string
  dataRange: { start: string; end: string; sessions: number }
  warnings: string[]
  runEvidence?: BacktestRunEvidence | null
}

export type BacktestRunEvidence = {
  strategyHash: string
  catalogHash: string
  dataSnapshotId: string
  dataSnapshotChecksum: string
  dataSchemaVersion: string
  producerSnapshotSchemaVersion: string | null
  producerSnapshotId: string | null
  codeRevision: string
  engineVersion: string
  executionAssumptions: Record<string, string>
}

export type EquityPoint = {
  date: string
  equity: number
  benchmark: number | null
  drawdown: number
}

export type ActivityKind =
  | 'signal'
  | 'order'
  | 'fill'
  | 'partial_fill'
  | 'unfilled'
  | 'expired'
export type TradeSide = 'buy' | 'sell'

export type BacktestSignalEvidence = {
  type: string
  id: string
  availableAt: string
  sourceEventId?: string | null
  provider?: string | null
  sourceUrl?: string | null
  timeQuality?: string | null
  timestampPrecision?: 'second' | 'minute' | 'hour' | 'date' | null
  validationStatus?: string | null
  rawResponseSha256?: string | null
}

export type BacktestActivity = {
  id: string
  kind: ActivityKind
  occurredAt: string
  side: TradeSide
  title: string
  price?: number | null
  quantity?: number | null
  status:
    | 'confirmed'
    | 'submitted'
    | 'filled'
    | 'partially_filled'
    | 'cancelled'
    | 'expired'
  reason: string
  chainId?: string | null
  decisionId?: string | null
  orderId?: string | null
  fillId?: string | null
  parentId?: string | null
  capacityReasonCode?: string | null
  /** 成交时点的数据质量，例如 daily_bar_open_proxy；由后端返回，前端不得推断。 */
  timeQuality?: string | null
  /** 对 occurredAt / 成交时点口径的解释；由后端返回，前端原样展示。 */
  timeSemantics?: string | null
  /** 信号是边沿、事件、持续状态还是组合条件；由执行引擎判定。 */
  signalSemantics?: 'edge' | 'event' | 'state' | 'composite' | null
  signalValiditySessions?: number | null
  attemptNo?: number | null
  originSignalId?: string | null
  retryReason?: string | null
  outcomeReason?: string | null
  evidence?: BacktestSignalEvidence[]
}

export type BacktestResultBundle = {
  summary: BacktestSummary
  series: EquityPoint[]
  activities: BacktestActivity[]
}

export type ApiProblem = {
  type: string
  title: string
  status: number
  detail: string
  code?: string
  requestId?: string
}

export class ApiError extends Error {
  readonly problem: ApiProblem

  constructor(problem: ApiProblem) {
    super(problem.detail)
    this.name = 'ApiError'
    this.problem = problem
  }
}
