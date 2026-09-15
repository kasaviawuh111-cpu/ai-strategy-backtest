import type { ExecutionSettings } from '../api/types'

export const STANDARD_INITIAL_CASH_CNY = 1_000_000
export const MINIMUM_INITIAL_CASH_CNY = 10_000
export const MAXIMUM_INITIAL_CASH_CNY = 1_000_000_000

/** User-configurable execution settings; strategy-owned policies stay on the DSL. */
export const DEFAULT_EXECUTION_SETTINGS = {
  priceLimitMode: 'wait_for_unlock',
  capacityMode: 'point_in_time_volume',
  participationRate: 0.05,
  allocationRatio: 1,
  commissionRate: 0.00025,
  minimumCommissionCny: 5,
  slippageBps: 5,
  retryUnfilledExits: true,
  maxExitAttempts: 20,
  warmupCalendarDays: 180,
  settlementExtensionDays: 14,
  runRobustness: true,
} satisfies ExecutionSettings
