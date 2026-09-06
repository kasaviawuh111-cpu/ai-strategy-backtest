/**
 * 三栏列宽的读写。
 *
 * 单独成文件：ColumnResizer.tsx 只导出组件，Fast Refresh 才能正常工作。
 */
import { useEffect } from 'react'

export type ResizerSide = 'rail' | 'review'

const STORAGE_KEY = 'backtest.columns.v1'

export const COLUMN_LIMITS: Record<ResizerSide, { min: number; max: number; label: string; fallback: number }> = {
  rail: { min: 200, max: 420, label: '左侧列表宽度', fallback: 264 },
  review: { min: 300, max: 560, label: '策略审阅栏宽度', fallback: 380 },
}

export const COLUMN_VAR: Record<ResizerSide, string> = {
  rail: '--rail-w',
  review: '--review-w',
}

type Widths = Partial<Record<ResizerSide, number>>

export const readStoredWidths = (): Widths => {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY)
    return raw ? (JSON.parse(raw) as Widths) : {}
  } catch {
    // 隐私模式 / 站点数据被禁时读不到。列宽只是顺手记住的偏好，用默认值即可。
    return {}
  }
}

export const writeStoredWidth = (side: ResizerSide, width: number) => {
  try {
    window.localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ ...readStoredWidths(), [side]: width }),
    )
  } catch {
    // 存不进去不影响这次会话的使用，不必打扰用户
  }
}

/** 把上次记住的列宽写回工作区的 CSS 变量。 */
export function useStoredColumnWidths(target: React.RefObject<HTMLElement | null>) {
  useEffect(() => {
    const node = target.current
    if (!node) return
    const stored = readStoredWidths()
    for (const side of ['rail', 'review'] as ResizerSide[]) {
      const value = stored[side]
      if (typeof value === 'number' && Number.isFinite(value)) {
        node.style.setProperty(COLUMN_VAR[side], `${value}px`)
      }
    }
  }, [target])
}
