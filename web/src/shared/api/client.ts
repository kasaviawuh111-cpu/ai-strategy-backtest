import { mockApi } from './mock'
import { ApiError } from './types'
import {
  fromLiveDraftResponse,
  mergeLiveRevision,
  toLiveBacktestBody,
  toLiveCompileBody,
  toLiveRevisionBody,
} from './contract'
import type { LiveDraftResponse } from './contract'
import type {
  ApiProblem,
  BacktestActivity,
  BacktestRun,
  BacktestSummary,
  CapabilitiesResponse,
  CompileRequest,
  CompileResponse,
  EquityPoint,
  StrategyDraft,
  StrategySpec,
  StrategySpecCondition,
} from './types'

const useMock = import.meta.env.VITE_USE_MOCK !== 'false'
const baseUrl = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')
const REQUEST_TIMEOUT_MS = 20_000

const requestFailure = (error: unknown): ApiError => {
  const timedOut = error instanceof DOMException && error.name === 'TimeoutError'
  return new ApiError({
    type: 'about:blank',
    title: timedOut ? '接口响应超时' : '无法连接回测服务',
    status: 0,
    detail: timedOut
      ? '回测服务超过 20 秒没有响应，请稍后重试。'
      : '浏览器没有连上回测服务，请检查网络、API 地址和跨域配置。',
    code: timedOut ? 'api_timeout' : 'api_network_unavailable',
  })
}

const request = async <T>(path: string, init?: RequestInit): Promise<T> => {
  const headers = new Headers(init?.headers)
  headers.set('Accept', 'application/json, application/problem+json')
  if (init?.body) headers.set('Content-Type', 'application/json')

  let response: Response
  try {
    response = await fetch(`${baseUrl}${path}`, {
      ...init,
      headers,
      signal: init?.signal ?? AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    })
  } catch (error) {
    if (error instanceof ApiError) throw error
    throw requestFailure(error)
  }

  if (!response.ok) {
    const fallback: ApiProblem = {
      type: 'about:blank',
      title: '请求失败',
      status: response.status,
      detail: `服务返回 ${response.status}，请稍后重试。`,
    }
    const payload: unknown = await response.json().catch(() => fallback)
    const problem = normalizeProblem(payload, fallback, response.status)
    throw new ApiError(problem)
  }

  try {
    return await response.json() as T
  } catch {
    throw new ApiError({
      type: 'about:blank',
      title: '接口响应格式错误',
      status: response.status,
      detail: '回测服务没有返回有效 JSON，请检查 API 网关或服务版本。',
      code: 'api_invalid_json',
    })
  }
}

const normalizeProblem = (payload: unknown, fallback: ApiProblem, status: number): ApiProblem => {
  if (!payload || typeof payload !== 'object') return fallback
  const value = payload as Record<string, unknown>
  if (value.error && typeof value.error === 'object') {
    const error = value.error as Record<string, unknown>
    return {
      type: 'about:blank',
      title: '请求未完成',
      status,
      detail: typeof error.message === 'string' ? error.message : fallback.detail,
      code: typeof error.code === 'string' ? error.code : undefined,
      requestId: typeof value.request_id === 'string' ? value.request_id : undefined,
    }
  }
  return {
    ...fallback,
    ...value,
    status,
    detail: typeof value.detail === 'string' ? value.detail : fallback.detail,
  }
}

export const MOCK_CAPABILITIES = {
  capability_source: 'mock_demo',
  demo_event_codes: ['event.financial_results.annual_report'],
  markets: ['CN_A'],
  input_modes: ['natural_language_zh'],
  strategy_scopes: ['single_instrument', 'long_only'],
  indicators: [],
  events: [{
    event_code: 'event.financial_results.annual_report',
    definition_version: '1.0.0',
    catalog_status: 'stable',
    status: 'unavailable',
    backtest_available: false,
    preparation_available: false,
    availability_scope: 'unavailable',
    unavailable_reason: 'snapshot_coverage_unavailable',
    document_text: {
      catalog_available: true,
      backtest_available: false,
      preparation_available: false,
      availability_scope: 'unavailable',
      unavailable_reason: 'snapshot_coverage_unavailable',
    },
    triggers: ['published'],
  }],
  execution_policies: ['next_tradable_session_open'],
  event_catalog_status: 'published',
  event_backtest_available: false,
  event_preparation_available: false,
  event_availability_scope: 'unavailable',
  backtest_execution_available: false,
  limits: {
    max_body_bytes: 16 * 1024,
    max_utterance_characters: 2000,
    max_instrument_context_characters: 32,
  },
} as const satisfies CapabilitiesResponse & {
  capability_source: 'mock_demo'
  demo_event_codes: readonly string[]
}

export const systemApi = {
  capabilities: (): Promise<CapabilitiesResponse> =>
    useMock
      ? Promise.resolve(MOCK_CAPABILITIES as CapabilitiesResponse)
      : request<CapabilitiesResponse>('/api/v1/capabilities', { cache: 'no-store' }),
}

const requireExecutionCapability = (capabilities: CapabilitiesResponse): void => {
  if (capabilities.backtest_execution_available) return
  throw new ApiError({
    type: 'about:blank',
    title: '当前不能运行回测',
    status: 503,
    detail: '后端当前没有可用的回测执行环境，请稍后重试。',
    code: 'backtest_service_unavailable',
  })
}

const eventCodesFromCondition = (condition: StrategySpecCondition): string[] => {
  if (condition.type === 'event_condition') return [condition.event_code]
  if (condition.type === 'indicator_condition') return []
  if (condition.type === 'not') return eventCodesFromCondition(condition.child)
  return condition.children.flatMap(eventCodesFromCondition)
}

const eventCodesFromStrategy = (strategy: StrategySpec): string[] => [
  ...eventCodesFromCondition(strategy.entry),
  ...strategy.exit.children.flatMap((condition) =>
    condition.type === 'holding_period_exit'
      || condition.type === 'position_return_exit'
      || condition.type === 'trailing_drawdown_exit'
      ? []
      : eventCodesFromCondition(condition)),
]

const documentTextEventCodesFromCondition = (condition: StrategySpecCondition): string[] => {
  if (condition.type === 'event_condition') {
    return condition.document_text ? [condition.event_code] : []
  }
  if (condition.type === 'indicator_condition') return []
  if (condition.type === 'not') return documentTextEventCodesFromCondition(condition.child)
  return condition.children.flatMap(documentTextEventCodesFromCondition)
}

const documentTextEventCodesFromStrategy = (strategy: StrategySpec): string[] => [
  ...documentTextEventCodesFromCondition(strategy.entry),
  ...strategy.exit.children.flatMap((condition) =>
    condition.type === 'holding_period_exit'
      || condition.type === 'position_return_exit'
      || condition.type === 'trailing_drawdown_exit'
      ? []
      : documentTextEventCodesFromCondition(condition)),
]

export const requireStrategyCapability = (
  strategy: StrategySpec,
  capabilities: CapabilitiesResponse,
): void => {
  requireExecutionCapability(capabilities)
  const byCode = new Map(capabilities.events.map((item) => [item.event_code, item]))
  const documentTextCodes = new Set(documentTextEventCodesFromStrategy(strategy))
  for (const eventCode of new Set(eventCodesFromStrategy(strategy))) {
    const capability = byCode.get(eventCode)
    const snapshotBacked = capability?.backtest_available === true
      && capability.availability_scope === 'pinned_snapshot'
    const requestPreparable = capability?.preparation_available === true
      && capability.availability_scope === 'request_preparation'
    if (!snapshotBacked && !requestPreparable) {
      throw new ApiError({
        type: 'about:blank',
        title: '这类事件暂不可回测',
        status: 422,
        detail: '当前快照没有这类事件，后端也没有声明可按本次股票和区间准备数据。',
        code: 'event_not_available_for_backtest',
      })
    }
    if (!documentTextCodes.has(eventCode)) continue
    const documentText = capability?.document_text
    const documentSnapshotBacked = documentText?.backtest_available === true
      && documentText.availability_scope === 'pinned_snapshot'
    const documentRequestPreparable = documentText?.preparation_available === true
      && documentText.availability_scope === 'request_preparation'
    if (documentText?.catalog_available === true
      && (documentSnapshotBacked || documentRequestPreparable)) continue
    throw new ApiError({
      type: 'about:blank',
      title: '公告正文暂不可用于回测',
      status: 422,
      detail: '这条规则需要公告完整正文与词频数据，但当前快照没有覆盖，后端也没有声明可按本次请求准备。',
      code: 'event_document_text_not_available_for_backtest',
    })
  }
}

export const strategyApi = {
  compile: async (input: CompileRequest): Promise<CompileResponse> => {
    if (useMock) return mockApi.compile(input)
    const [response, capabilities] = await Promise.all([
      request<LiveDraftResponse>('/api/v1/strategy-drafts', {
        method: 'POST',
        body: JSON.stringify(toLiveCompileBody(input)),
      }),
      // Metadata improves labels, but it is not a prerequisite for understanding
      // or producing the server-owned StrategySpec. The page performs a separate
      // capability check before it allows a run to start.
      systemApi.capabilities().catch(() => undefined),
    ])
    // “能理解”与“当前可回测”是两个阶段。编译成功后由页面单独展示
    // catalog / preparation / pinned snapshot，不能在这里把已识别规则吞成错误。
    return fromLiveDraftResponse(response, input, capabilities)
  },

  revise: async (draft: StrategyDraft): Promise<StrategyDraft> => {
    if (useMock) return mockApi.revise(draft)
    const capabilities = await systemApi.capabilities()
    requireStrategyCapability(draft.strategySpec, capabilities)
    const response = await request<LiveDraftResponse>(
      `/api/v1/strategy-drafts/${encodeURIComponent(draft.id)}/revisions`,
      { method: 'POST', body: JSON.stringify(toLiveRevisionBody(draft)) },
    )
    return mergeLiveRevision(response, draft, capabilities)
  },
}

export const backtestApi = {
  create: async (draft: StrategyDraft): Promise<BacktestRun> => {
    if (useMock) return mockApi.createRun(draft)
    const capabilities = await systemApi.capabilities()
    requireStrategyCapability(draft.strategySpec, capabilities)
    return request('/api/v1/backtest-runs', {
      method: 'POST',
      body: JSON.stringify(toLiveBacktestBody(draft)),
    })
  },

  get: (runId: string): Promise<BacktestRun> =>
    useMock ? mockApi.getRun(runId) : request(`/api/v1/backtest-runs/${runId}`),

  cancel: (runId: string): Promise<BacktestRun> =>
    useMock
      ? mockApi.cancelRun(runId)
      : request(`/api/v1/backtest-runs/${runId}/cancel`, { method: 'POST' }),

  summary: (runId: string): Promise<BacktestSummary> =>
    useMock ? mockApi.getSummary(runId) : request(`/api/v1/backtest-runs/${runId}/summary`),

  series: (runId: string): Promise<EquityPoint[]> =>
    useMock ? mockApi.getSeries(runId) : request(`/api/v1/backtest-runs/${runId}/series`),

  activities: (runId: string): Promise<BacktestActivity[]> =>
    useMock ? mockApi.getActivities(runId) : request(`/api/v1/backtest-runs/${runId}/trades`),
}

export const apiMode = useMock ? 'mock' : 'live'
