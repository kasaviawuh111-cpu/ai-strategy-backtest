import { vi } from 'vitest'

import { backtestApi } from '../shared/api/client'
import { enableImmediateMockWaitForTests, mockApi } from '../shared/api/mock'

const terminalStates = new Set(['succeeded', 'failed', 'cancelled'])

/**
 * Keep the browser-facing Mock timing intact while letting integration tests
 * skip the decorative multi-poll progression. The real Mock record is still
 * created and every state transition still runs; they are simply drained in
 * the first query instead of waiting 700ms between presentation updates.
 */
export const settleMockRunOnFirstPoll = (
  { preserveFirstRunningState = false }: { preserveFirstRunningState?: boolean } = {},
) => {
  enableImmediateMockWaitForTests()
  let pollCount = 0
  return vi.spyOn(backtestApi, 'get').mockImplementation(async (runId) => {
    let run = await mockApi.getRun(runId)
    pollCount += 1
    if (preserveFirstRunningState && pollCount === 1) return run
    while (!terminalStates.has(run.state)) run = await mockApi.getRun(runId)
    return run
  })
}
