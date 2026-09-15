import { mockApi } from './mock'
import type { GridRequest, GridResult } from '../../grid/types'
import { ApiError } from './types'
import {
  fromLiveDraftResponse,
  fromLiveClarificationAnswerResponse,
  fromBacktestOptimizationCandidate,
  mergeLiveRevision,
  toLiveBacktestBody,
  toLiveBacktestReferences,
  toLiveCompileBody,
  toLiveRevisionBody,
  toLiveExecutionSettings,
  setAvailableDataEnd,
} from './contract'
import type { LiveClarificationAnswerResponse, LiveDraftResponse } from './contract'
import type {
  ApiProblem,
  BacktestActivity,
  BacktestOptimizationCandidate,
  BacktestReviewResponse,
  BacktestRun,
  BacktestSummary,
  CapabilitiesResponse,
  ClarificationAnswerInput,
  ClarificationAnswerOutcome,
  CompileRequest,
  CompileResponse,
  EquityPoint,
  Instrument,
  StrategyDraft,
  StrategySpec,
  StrategySpecCondition,
} from './types'

// Fixtures require explicit local opt-in; published builds always call the API.
const useMock = import.meta.env.DEV && import.meta.env.VITE_USE_MOCK === 'true'
const baseUrl = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')
const REQUEST_TIMEOUT_MS = 20_000
// Model-backed operations have no browser wall-clock deadline. The backend
// detects upstream inactivity; a healthy model stream may take longer.
const DIALOGUE_TIMEOUT_MS = null
/** Backtest job creation keeps a separate bounded data-preparation budget. */
const BACKTEST_CREATE_TIMEOUT_MS = 300_000
const BACKTEST_REVIEW_TIMEOUT_MS = null
const DIALOGUE_PROGRESS_POLL_MS = 1_000
const DIALOGUE_PROGRESS_FINAL_TIMEOUT_MS = 1_000
const DIALOGUE_PROGRESS_LIMIT = 12
const IDEMPOTENT_POST_RETRY_DELAYS_MS = [250, 1_000] as const

// Anonymous browser scheduling key. This is not login or access authorization.
let previewClientId: string | undefined
const previewClient = (): string => {
  if (previewClientId) return previewClientId
  try {
    const saved = localStorage.getItem('backtest-preview-client')
    if (saved && /^[0-9a-f-]{36}$/.test(saved)) previewClientId = saved
  } catch { /* Private browsing may disable storage. */ }
  previewClientId ??= crypto.randomUUID()
  try { localStorage.setItem('backtest-preview-client', previewClientId) } catch { /* Keep in memory. */ }
  return previewClientId
}

export type DialogueProgressEvent = {
  stage: string
  message: string
  elapsedMs: number
  reasoning?: string
  reasoningTruncated?: boolean
}

export type PreviewPollRecovery = {
  status: 'retrying' | 'paused'
  message: string
  attempt: number
  resume?: () => void
}

export type DialogueProgressObserver = {
  signal?: AbortSignal
  onProgress: (events: readonly DialogueProgressEvent[]) => void
  onRecovery?: (state: PreviewPollRecovery | null) => void
}

type ProgressAware<T> = T & { dialogueProgress?: DialogueProgressObserver }

type LiveDialogueProgress = {
  events: Array<{
    stage: string; message: string; elapsed_ms: number
    reasoning?: string; reasoning_truncated?: boolean
  }>
  finished: boolean
}

const requestFailure = (error: unknown, timeoutMs: number | null): ApiError => {
  const timedOut = timeoutMs !== null && error instanceof DOMException && error.name === 'TimeoutError'
  const timeoutSeconds = Math.round((timeoutMs ?? 0) / 1_000)
  return new ApiError({
    type: 'about:blank',
    title: timedOut ? '请求响应超时' : '请求网络异常',
    status: 0,
    detail: timedOut
      ? `这次请求等待超过 ${timeoutSeconds} 秒，暂未收到完整结果。`
      : '这次请求遇到网络异常，暂未收到结果。',
    code: timedOut ? 'api_timeout' : 'api_network_unavailable',
  })
}

const abortReason = (signal?: AbortSignal | null): unknown =>
  signal?.reason ?? new DOMException('Aborted', 'AbortError')

// AbortController works in older mobile webviews without AbortSignal.any/timeout.
const withRequestSignal = async <T>(
  signal: AbortSignal | null | undefined,
  timeoutMs: number | null,
  operation: (signal: AbortSignal) => Promise<T>,
): Promise<T> => {
  const controller = new AbortController()
  const abort = () => controller.abort(abortReason(signal))
  if (signal?.aborted) abort()
  else signal?.addEventListener('abort', abort, { once: true })
  const timer = timeoutMs === null ? undefined : window.setTimeout(() => {
    controller.abort(new DOMException('Request timed out', 'TimeoutError'))
  }, timeoutMs)
  try {
    if (controller.signal.aborted) throw abortReason(controller.signal)
    return await operation(controller.signal)
  } finally {
    window.clearTimeout(timer)
    signal?.removeEventListener('abort', abort)
  }
}

const waitForPreviewPoll = (milliseconds: number, signal?: AbortSignal | null): Promise<void> =>
  new Promise((resolve, reject) => {
    const timer = window.setTimeout(done, milliseconds)
    function cleanup() { window.clearTimeout(timer); signal?.removeEventListener('abort', abort) }
    function done() { cleanup(); resolve() }
    function abort() { cleanup(); reject(abortReason(signal)) }
    if (signal?.aborted) abort()
    else signal?.addEventListener('abort', abort, { once: true })
  })

const notifyRecovery = (
  observer: DialogueProgressObserver | undefined, state: PreviewPollRecovery | null,
) => {
  try { observer?.onRecovery?.(state) } catch { /* UI must not discard an accepted request. */ }
}

const pausePreviewPoll = (
  observer: DialogueProgressObserver, attempt: number, reason: string, signal?: AbortSignal | null,
): Promise<void> => new Promise((resolve, reject) => {
  let settled = false
  function cleanup() {
    window.removeEventListener('online', resume)
    signal?.removeEventListener('abort', abort)
  }
  function resume() {
    if (settled) return
    settled = true
    cleanup()
    notifyRecovery(observer, { status: 'retrying', attempt: 0,
      message: '正在继续查询本次结果，不会重复提交。' })
    resolve()
  }
  function abort() {
    if (settled) return
    settled = true
    cleanup()
    reject(abortReason(signal))
  }
  if (signal?.aborted) { abort(); return }
  window.addEventListener('online', resume)
  signal?.addEventListener('abort', abort, { once: true })
  notifyRecovery(observer, { status: 'paused', attempt, resume,
    message: `${reason}，后台任务可能仍在处理。可以点击继续查询；不会重复提交。` })
})

const transientPreviewStatuses = new Set([408, 429, 502, 503, 504])
const previewQueryFailureReason = (error: unknown, status: number | undefined): string => {
  if (error instanceof DOMException && error.name === 'TimeoutError') return '结果查询响应超时'
  if (status !== undefined && transientPreviewStatuses.has(status)) {
    return status === 429 ? '结果查询频率受限（HTTP 429）'
      : `结果查询服务暂时异常（HTTP ${status}）`
  }
  if (status !== undefined) return '已收到结果查询响应，但内容未完整返回或格式无效'
  return error instanceof TypeError ? '结果查询连接暂时中断' : '结果查询暂时未完成'
}
const isBusinessProblem = (response: Response, payload: unknown): boolean => {
  if (response.headers.get('Content-Type')?.includes('application/problem+json')) return true
  if (!payload || typeof payload !== 'object') return false
  const value = payload as Record<string, unknown>
  return typeof value.code === 'string' || typeof value.detail === 'string'
    || (value.error !== undefined && value.error !== null)
}

const pollPreviewResult = async (
  location: string, signal?: AbortSignal | null, observer?: DialogueProgressObserver,
): Promise<{ response: Response; payload: unknown }> => {
  let retries = 0
  let delay = 1_000
  try {
    for (;;) {
      await waitForPreviewPoll(delay, signal)
      let responseStatus: number | undefined
      try {
        const result = await withRequestSignal(signal, REQUEST_TIMEOUT_MS, async (pollSignal) => {
          const response = await fetch(`${baseUrl}${location}`, {
            headers: { Accept: 'application/json, application/problem+json' },
            signal: pollSignal, redirect: 'error',
          })
          responseStatus = response.status
          // A retained GET result can safely be re-read after a truncated body.
          const payload: unknown = response.ok ? await response.json()
            : await response.json().catch(() => undefined)
          if (transientPreviewStatuses.has(response.status) && !isBusinessProblem(response, payload)) {
            throw new Error('Transient preview query response')
          }
          return { response, payload }
        })
        notifyRecovery(observer, null)
        retries = 0
        delay = 1_000
        if (result.response.status !== 202 || result.response.headers.get('X-Preview-Pending') !== '1') {
          return result
        }
      } catch (error) {
        if (signal?.aborted) throw abortReason(signal)
        const reason = previewQueryFailureReason(error, responseStatus)
        if (retries < 3) {
          delay = 1_000 * 2 ** retries
          retries += 1
          notifyRecovery(observer, { status: 'retrying', attempt: retries,
            message: `${reason}，正在重试查询（${retries}/3）；不会重复提交。` })
        } else if (observer?.onRecovery) {
          await pausePreviewPoll(observer, retries, reason, signal)
          retries = 0
          delay = 0
        } else {
          throw new ApiError({ type: 'about:blank', title: '结果查询中断', status: 0,
            detail: '多次查询仍未取得结果，后台可能仍在处理；本次未重复提交。',
            code: 'preview_poll_interrupted' })
        }
      }
    }
  } finally {
    notifyRecovery(observer, null)
  }
}

const request = async <T>(
  path: string,
  init?: RequestInit,
  timeoutMs: number | null = REQUEST_TIMEOUT_MS,
  observer?: DialogueProgressObserver,
): Promise<T> => {
  const headers = new Headers(init?.headers)
  headers.set('Accept', 'application/json, application/problem+json')
  if (init?.body) headers.set('Content-Type', 'application/json')
  if (import.meta.env.VITE_PRIVATE_PREVIEW === 'true' && init?.method === 'POST') {
    headers.set('Prefer', 'respond-async')
    headers.set('X-Preview-Client-ID', previewClient())
  }

  const signal = init?.signal ?? observer?.signal
  // Keep cancellation attached until the response body has been consumed, not
  // only until fetch resolves its headers. A progress body's stream can stall.
  return withRequestSignal(signal, timeoutMs, async (requestSignal) => {
    let response: Response
    let responsePayload: unknown
    let hasResponsePayload = false
    const canRetryPost = init?.method?.toUpperCase() === 'POST'
      && headers.has('Idempotency-Key')
    let networkAttempt = 0
    for (;;) {
      try {
        response = await fetch(`${baseUrl}${path}`, { ...init, headers, signal: requestSignal })
        break
      } catch (error) {
        if (signal?.aborted) throw abortReason(signal)
        if (error instanceof ApiError) throw error
        if (!canRetryPost || networkAttempt >= IDEMPOTENT_POST_RETRY_DELAYS_MS.length) {
          throw requestFailure(requestSignal.aborted ? abortReason(requestSignal) : error, timeoutMs)
        }
        networkAttempt += 1
        notifyRecovery(observer, {
          status: 'retrying', attempt: networkAttempt,
          message: `连接短暂中断，正在重试本次请求（${networkAttempt}/${IDEMPOTENT_POST_RETRY_DELAYS_MS.length}），不会重复提交。`,
        })
        await waitForPreviewPoll(
          IDEMPOTENT_POST_RETRY_DELAYS_MS[networkAttempt - 1] ?? 0,
          requestSignal,
        )
      }
    }

    if (import.meta.env.VITE_PRIVATE_PREVIEW === 'true'
        && response.status === 202 && response.headers.get('X-Preview-Pending') === '1') {
      const location = response.headers.get('Location') ?? ''
      if (!/^\/api\/v1\/preview-requests\/[0-9a-f-]{36}$/.test(location)) {
        throw new ApiError({ type: 'about:blank', title: '请求状态异常', status: 502,
          detail: '服务未返回有效的分析查询地址。', code: 'preview_invalid_location' })
      }
      const result = await pollPreviewResult(location, requestSignal, observer)
      response = result.response
      responsePayload = result.payload
      hasResponsePayload = true
    }

    if (!response.ok) {
      const fallback: ApiProblem = {
        type: 'about:blank',
        title: '请求失败',
        status: response.status,
        detail: `服务返回 ${response.status}，请稍后重试。`,
      }
      const payload: unknown = hasResponsePayload ? responsePayload : await response.json().catch(() => {
        if (requestSignal.aborted) throw abortReason(requestSignal)
        return fallback
      })
      const problem = normalizeProblem(payload, fallback, response.status)
      throw new ApiError(problem)
    }

    try {
      if (hasResponsePayload) return responsePayload as T
      return await response.json() as T
    } catch {
      if (requestSignal.aborted) throw abortReason(requestSignal)
      throw new ApiError({
        type: 'about:blank',
        title: '接口响应格式错误',
        status: response.status,
        detail: '请求链路返回内容异常，系统暂未取得可用结果。',
        code: 'api_invalid_json',
      })
    }
  })
}

const delayUntilNextProgressPoll = (signal: AbortSignal): Promise<void> => new Promise((resolve) => {
  if (signal.aborted) {
    resolve()
    return
  }
  const timer = window.setTimeout(done, DIALOGUE_PROGRESS_POLL_MS)
  signal.addEventListener('abort', done, { once: true })

  function done() {
    window.clearTimeout(timer)
    signal.removeEventListener('abort', done)
    resolve()
  }
})

const normalizedDialogueProgress = (
  snapshot: LiveDialogueProgress,
): readonly DialogueProgressEvent[] => snapshot.events
  .filter((event) => (
    typeof event.stage === 'string'
    && typeof event.message === 'string'
    && event.message.trim().length > 0
    && Number.isFinite(event.elapsed_ms)
    && event.elapsed_ms >= 0
  ))
  .slice(-DIALOGUE_PROGRESS_LIMIT)
  .map((event) => ({
    stage: event.stage,
    message: event.message.trim(),
    elapsedMs: event.elapsed_ms,
    ...(typeof event.reasoning === 'string' ? {
      reasoning: event.reasoning.slice(-60_000),
      reasoningTruncated: event.reasoning_truncated === true,
    } : {}),
  }))

const pollDialogueProgress = async (
  progressId: string,
  observer: DialogueProgressObserver,
  signal: AbortSignal,
): Promise<boolean> => {
  while (!signal.aborted) {
    try {
      const snapshot = await request<LiveDialogueProgress>(
        `/api/v1/dialogue-progress/${encodeURIComponent(progressId)}`,
        { signal },
      )
      if (signal.aborted) return false
      const events = normalizedDialogueProgress(snapshot)
      try {
        observer.onProgress(events)
      } catch {
        // Rendering progress is best-effort and must never fail the real request.
      }
      if (snapshot.finished) return true
    } catch {
      // A 404 before the request registers, an unavailable poll, or an aborted
      // component never changes the compile/clarification result.
      if (signal.aborted) return false
    }
    await delayUntilNextProgressPoll(signal)
  }
  return false
}

const readFinalDialogueProgress = async (
  progressId: string,
  observer: DialogueProgressObserver,
): Promise<void> => {
  if (observer.signal?.aborted) return
  const controller = new AbortController()
  const abort = () => controller.abort()
  const timer = window.setTimeout(abort, DIALOGUE_PROGRESS_FINAL_TIMEOUT_MS)
  observer.signal?.addEventListener('abort', abort, { once: true })
  try {
    // The main response can arrive between polls. Read its last real snapshot
    // once, without keeping a completed conversation waiting on the network.
    const snapshot = await request<LiveDialogueProgress>(
      `/api/v1/dialogue-progress/${encodeURIComponent(progressId)}`,
      { signal: controller.signal }, null,
    )
    if (!controller.signal.aborted && !observer.signal?.aborted) {
      observer.onProgress(normalizedDialogueProgress(snapshot))
    }
  } catch {
    // Progress must never replace a successful result or its original error.
  } finally {
    window.clearTimeout(timer)
    observer.signal?.removeEventListener('abort', abort)
  }
}

const withDialogueProgress = async <T>(
  observer: DialogueProgressObserver | undefined,
  operation: (progressId?: string) => Promise<T>,
): Promise<T> => {
  if (observer === undefined || observer.signal?.aborted) return operation()

  let progressId: string
  try {
    progressId = crypto.randomUUID()
  } catch {
    return operation()
  }

  const polling = new AbortController()
  const stopPolling = () => polling.abort()
  observer.signal?.addEventListener('abort', stopPolling, { once: true })
  let latestEvents: readonly DialogueProgressEvent[] = []
  const trackedObserver = { ...observer, onProgress: (events: readonly DialogueProgressEvent[]) => {
    latestEvents = events
    observer.onProgress(events)
  } }
  let poll: Promise<boolean> | undefined
  let failed = false
  try {
    // Dispatch the work before asking the server for its progress record.
    // A network race may still return 404; polling remains best-effort.
    const result = operation(progressId)
    poll = pollDialogueProgress(progressId, trackedObserver, polling.signal)
    return await result
  } catch (error) {
    failed = true
    throw error
  } finally {
    stopPolling()
    const finished = await poll
    if (poll !== undefined && !finished) await readFinalDialogueProgress(progressId, trackedObserver)
    if (failed && !observer.signal?.aborted && latestEvents.at(-1)?.stage !== 'failed') {
      try {
        observer.onProgress([...latestEvents.slice(-(DIALOGUE_PROGRESS_LIMIT - 1)), {
          stage: 'failed', message: '本轮请求未完成，请查看错误说明。',
          elapsedMs: latestEvents.at(-1)?.elapsedMs ?? 0,
        }])
      } catch { /* Rendering cannot replace the original request failure. */ }
    }
    notifyRecovery(observer, null)
    observer.signal?.removeEventListener('abort', stopPolling)
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
      details: normalizeProblemDetails(error.details),
    }
  }
  return {
    ...fallback,
    ...value,
    status,
    detail: typeof value.detail === 'string' ? value.detail : fallback.detail,
    details: normalizeProblemDetails(value.details),
  }
}

const normalizeProblemDetails = (value: unknown): ApiProblem['details'] => {
  if (!Array.isArray(value)) return undefined
  return value.flatMap((item: unknown) => {
    if (!item || typeof item !== 'object') return []
    const detail = item as Record<string, unknown>
    if (typeof detail.message !== 'string' || !detail.message.trim()) return []
    return [{
      message: detail.message,
      location: typeof detail.location === 'string' ? detail.location : undefined,
      type: typeof detail.type === 'string' ? detail.type : undefined,
    }]
  })
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
      : request<CapabilitiesResponse>('/api/v1/capabilities', { cache: 'no-store' }).then(value => {
        setAvailableDataEnd(value.available_data_end)
        return value
      }),
}

const requireExecutionCapability = (capabilities: CapabilitiesResponse): void => {
  if (capabilities.backtest_execution_available) return
  throw new ApiError({
    type: 'about:blank',
    title: '当前不能运行回测',
    status: 503,
    detail: '系统暂未准备好执行这次回测，规则和参数可以继续保留或修改。',
    code: 'backtest_service_unavailable',
  })
}

const eventCodesFromCondition = (condition: StrategySpecCondition | null): string[] => {
  if (!condition) return []
  if (condition.type === 'event_condition') return [condition.event_code]
  if (condition.type === 'indicator_condition' || condition.type === 'financial_condition') return []
  if (condition.type === 'not') return eventCodesFromCondition(condition.child)
  return condition.children.flatMap(eventCodesFromCondition)
}

const eventCodesFromStrategy = (strategy: StrategySpec): string[] => [
  ...eventCodesFromCondition(strategy.entry),
  ...(strategy.exit?.children ?? []).flatMap((condition) =>
      condition.type === 'holding_period_exit'
      || condition.type === 'position_return_exit'
      || condition.type === 'trailing_drawdown_exit'
      || condition.type === 'minute_protection_exit'
      ? []
      : eventCodesFromCondition(condition)),
]

const documentTextEventCodesFromCondition = (condition: StrategySpecCondition | null): string[] => {
  if (!condition) return []
  if (condition.type === 'event_condition') {
    return condition.document_text ? [condition.event_code] : []
  }
  if (condition.type === 'indicator_condition' || condition.type === 'financial_condition') return []
  if (condition.type === 'not') return documentTextEventCodesFromCondition(condition.child)
  return condition.children.flatMap(documentTextEventCodesFromCondition)
}

const documentTextEventCodesFromStrategy = (strategy: StrategySpec): string[] => [
  ...documentTextEventCodesFromCondition(strategy.entry),
  ...(strategy.exit?.children ?? []).flatMap((condition) =>
      condition.type === 'holding_period_exit'
      || condition.type === 'position_return_exit'
      || condition.type === 'trailing_drawdown_exit'
      || condition.type === 'minute_protection_exit'
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

export const instrumentApi = {
  search: async (query: string, signal: AbortSignal): Promise<{
    items: Instrument[]; hasMore: boolean; isStaleCache?: boolean
  }> => {
    const response = await request<{
      items: Array<{ symbol: string; name: string; exchange: 'SH' | 'SZ' | 'BJ' }>
      source: string
      has_more: boolean
      cache_status?: 'live' | 'fresh_cache' | 'stale_cache'
    }>(`/api/v1/market/instruments?query=${encodeURIComponent(query.trim())}&limit=8`, {
      signal: AbortSignal.any([signal, AbortSignal.timeout(REQUEST_TIMEOUT_MS)]),
    })
    const exchanges = { SH: 'SSE', SZ: 'SZSE', BJ: 'BSE' } as const
    if (!['eastmoney_security_search', 'eastmoney_instrument_directory'].includes(response.source)
      || !Array.isArray(response.items)
      || typeof response.has_more !== 'boolean'
      || response.items.some(item => !item || typeof item.name !== 'string' || !item.name.trim()
        || !Object.hasOwn(exchanges, item.exchange)
        || typeof item.symbol !== 'string'
        || !/^\d{6}\.(SH|SZ|BJ)$/.test(item.symbol)
        || !item.symbol.endsWith(`.${item.exchange}`))) {
      throw new ApiError({ type: 'about:blank', title: '股票搜索结果无效', status: 502,
        detail: '股票服务返回的名称或代码不完整，请重新查找。', code: 'instrument_search_invalid' })
    }
    return {
      items: response.items.map(item => ({ symbol: item.symbol, name: item.name,
        market: 'CN_A', exchange: exchanges[item.exchange] })),
      hasMore: response.has_more,
      ...(response.cache_status === 'stale_cache' ? { isStaleCache: true } : {}),
    }
  },
}

export const gridApi = {
  run: (body: GridRequest, signal?: AbortSignal) => request<GridResult>(
    '/api/v1/grid/backtests', {
      method: 'POST', body: JSON.stringify(body), signal,
      // This endpoint only computes a report; repeating cannot place real orders.
      headers: { 'Idempotency-Key': crypto.randomUUID() },
    }, BACKTEST_CREATE_TIMEOUT_MS,
  ),
}

export const strategyApi = {
  compile: async (
    input: ProgressAware<CompileRequest>,
    parentDraftId?: string,
  ): Promise<CompileResponse> => {
    if (useMock) return mockApi.compile(input)
    return withDialogueProgress(input.dialogueProgress, async (progressId) => {
      const headers = new Headers()
      if (parentDraftId) headers.set('X-Conversation-Parent-Draft-ID', parentDraftId)
      if (progressId) {
        headers.set('X-Dialogue-Progress-ID', progressId)
        headers.set('Idempotency-Key', `draft:${progressId}`)
      }
      const [response, capabilities] = await Promise.all([
        request<LiveDraftResponse>('/api/v1/strategy-drafts', {
          method: 'POST',
          headers,
          signal: input.dialogueProgress?.signal,
          body: JSON.stringify(toLiveCompileBody(parentDraftId
            ? input : { ...input, executionSettings: undefined })),
        }, DIALOGUE_TIMEOUT_MS, input.dialogueProgress),
        // Metadata improves labels, but it is not a prerequisite for understanding
        // or producing the server-owned StrategySpec. The page performs a separate
        // capability check before it allows a run to start.
        systemApi.capabilities().catch(() => undefined),
      ])
      // “能理解”与“当前可回测”是两个阶段。编译成功后由页面单独展示
      // catalog / preparation / pinned snapshot，不能在这里把已识别规则吞成错误。
      return fromLiveDraftResponse(response, input, capabilities)
    })
  },

  answerClarification: async (
    input: ProgressAware<ClarificationAnswerInput>,
  ): Promise<ClarificationAnswerOutcome> => {
    if (useMock) return mockApi.answerClarification(input)
    if (!Number.isInteger(input.revision) || (input.revision ?? 0) < 1) {
      throw new ApiError({
        type: 'about:blank',
        title: '澄清版本无效',
        status: 409,
        detail: '这轮澄清没有绑定有效的策略版本，请重新提交原始规则。',
        code: 'strategy_draft_revision_missing',
      })
    }
    return withDialogueProgress(input.dialogueProgress, async (progressId) => {
      const headers = new Headers()
      if (progressId) headers.set('X-Dialogue-Progress-ID', progressId)
      const [response, capabilities] = await Promise.all([
        request<LiveClarificationAnswerResponse>(
          `/api/v1/strategy-drafts/${encodeURIComponent(input.draftId)}`
          + `/revisions/${input.revision}/clarification-answers`,
          { method: 'POST', headers, signal: input.dialogueProgress?.signal, body: JSON.stringify({
            answer: input.answer,
            ...(input.executionSettings !== undefined
              ? { execution_settings: toLiveExecutionSettings(input.executionSettings) } : {}),
            ...toLiveBacktestReferences(input),
          }) },
          DIALOGUE_TIMEOUT_MS,
          input.dialogueProgress,
        ),
        systemApi.capabilities().catch(() => undefined),
      ])
      return fromLiveClarificationAnswerResponse(response, input, capabilities)
    })
  },

  revise: async (draft: StrategyDraft, recoverIfMissing = false, signal?: AbortSignal): Promise<StrategyDraft> => {
    if (useMock) return mockApi.revise(draft)
    const capabilities = await systemApi.capabilities()
    signal?.throwIfAborted()
    requireStrategyCapability(draft.strategySpec, capabilities)
    const response = await request<LiveDraftResponse>(
      `/api/v1/strategy-drafts/${encodeURIComponent(draft.id)}/revisions`,
      { method: 'POST', ...(signal ? { signal } : {}), body: JSON.stringify({
        ...toLiveRevisionBody(draft),
        ...(recoverIfMissing ? { recover_if_missing: true } : {}),
      }) }, BACKTEST_CREATE_TIMEOUT_MS,
    )
    return mergeLiveRevision(response, draft, capabilities)
  },
}

export const metricApi = {
  discover: async (input: { instrument_id: string; metric_query: string; start: string; end: string }, signal?: AbortSignal) => {
    const result = await request<{
      instrument_id: string
      instrument_verified: boolean
      status: 'discovered' | 'unavailable'
      candidate_table_indices: number[]
      tables: { table_index: number; instrument_verified: boolean; issues: string[]; fields: {
        return_name: string | null; display_name: string | null; unit: string | null
        issues: string[]; values: (string | number | null)[]
      }[] }[]
    }>('/api/v1/market/series-discovery', {
      method: 'POST', signal, body: JSON.stringify(input),
    }, BACKTEST_CREATE_TIMEOUT_MS)
    if (!result.instrument_verified || result.instrument_id !== input.instrument_id
      || result.status !== 'discovered') return []
    const fields = result.tables.filter(table => table.instrument_verified && table.issues.length === 0
      && result.candidate_table_indices.includes(table.table_index)).flatMap(table => table.fields)
      .filter(field => field.issues.length === 0 && field.unit?.trim()
        && field.values.some(value => value !== null))
    return fields.map(field => ({ name: field.display_name || field.return_name || input.metric_query,
      unit: field.unit ?? '', note: '已返回历史数据，执行前仍需检查' })).filter((field, index, all) =>
      all.findIndex(other => other.name === field.name && other.unit === field.unit) === index)
  },
}

export const backtestApi = {
  prepare: async (draft: StrategyDraft, signal?: AbortSignal): Promise<{ ready: true }> => {
    if (signal?.aborted) throw abortReason(signal)
    if (useMock) return { ready: true }
    const prepared = await request<{ ready: boolean }>('/api/v1/backtest-runs/prepare', {
      method: 'POST', signal, body: JSON.stringify(toLiveBacktestBody(draft)),
    }, BACKTEST_CREATE_TIMEOUT_MS)
    if (prepared.ready !== true) {
      throw new ApiError({ type: 'about:blank', title: '策略准备未完成', status: 502,
        detail: '服务没有返回已准备好的结果，请重新检查条件后重试。',
        code: 'backtest_preparation_incomplete' })
    }
    return { ready: true }
  },

  create: async (
    draft: StrategyDraft, options: { refreshData?: boolean } = {},
  ): Promise<BacktestRun> => {
    if (useMock) return mockApi.createRun(draft)
    const capabilities = await systemApi.capabilities()
    requireStrategyCapability(draft.strategySpec, capabilities)
    return request('/api/v1/backtest-runs', {
      method: 'POST',
      body: JSON.stringify(toLiveBacktestBody(draft, options)),
    }, BACKTEST_CREATE_TIMEOUT_MS)
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

  review: async (
    runId: string, observer?: DialogueProgressObserver,
    references?: Pick<CompileRequest, 'relatedRunIds' | 'relatedReviews'>,
  ): Promise<BacktestReviewResponse> => {
    if (useMock) {
      throw new ApiError({
        type: 'about:blank',
        title: 'AI 分析需要真实模型',
        status: 503,
        detail: '界面预览没有连接真实模型，不会生成或伪造优化建议。',
        code: 'backtest_review_model_unavailable',
      })
    }
    const body = references ? toLiveBacktestReferences(references) : undefined
    return withDialogueProgress(observer, (progressId) => request<BacktestReviewResponse>(
      `/api/v1/backtest-runs/${encodeURIComponent(runId)}/review`,
      { method: 'POST', headers: progressId ? { 'X-Dialogue-Progress-ID': progressId } : undefined,
        signal: observer?.signal,
        ...(body && Object.keys(body).length ? { body: JSON.stringify(body) } : {}) },
      BACKTEST_REVIEW_TIMEOUT_MS,
      observer,
    ))
  },

  createOptimization: async (
    candidate: BacktestOptimizationCandidate,
    sourceDraft: StrategyDraft,
  ): Promise<{ draft: StrategyDraft; run: BacktestRun }> => {
    if (useMock) {
      throw new ApiError({
        type: 'about:blank',
        title: '优化回测需要真实服务',
        status: 503,
        detail: '界面预览不会执行模型优化策略。',
        code: 'backtest_optimization_live_required',
      })
    }
    const capabilities = await systemApi.capabilities()
    requireStrategyCapability(candidate.strategy, capabilities)
    // Save the exact model-authored DSL as the current server revision first.
    // Follow-up language edits must see this strategy, not the old baseline.
    const draft = await strategyApi.revise(
      fromBacktestOptimizationCandidate(candidate, sourceDraft, capabilities),
      true,
    )
    const run = await request<BacktestRun>('/api/v1/backtest-runs', {
      method: 'POST',
      body: JSON.stringify(toLiveBacktestBody(draft)),
    }, BACKTEST_CREATE_TIMEOUT_MS)
    return {
      draft,
      run,
    }
  },
}

export const apiMode = useMock ? 'mock' : 'live'
