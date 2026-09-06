/**
 * 三栏之间的拖拽分隔条。
 *
 * 不做成 grid 子项：一旦它有自己的命名区域，某些状态下模板里没有那块区域时
 * 就会被隐式自动摆放，把整个版面挤坏（这个坑踩过一次）。
 * 改成绝对定位，left/right 跟随驱动列宽的同一个 CSS 变量，
 * 分隔条永远贴在真实的列边界上，且完全不参与 grid 布局。
 */
import {
  COLUMN_LIMITS,
  COLUMN_VAR,
  writeStoredWidth,
  type ResizerSide,
} from './column-widths'

export function ColumnResizer(
  { side, target }: { side: ResizerSide; target: React.RefObject<HTMLElement | null> },
) {
  const limits = COLUMN_LIMITS[side]

  const currentWidth = (node: HTMLElement) => {
    const value = Number.parseFloat(node.style.getPropertyValue(COLUMN_VAR[side]))
    return Number.isFinite(value) ? value : limits.fallback
  }

  const apply = (node: HTMLElement, width: number) => {
    const clamped = Math.min(limits.max, Math.max(limits.min, Math.round(width)))
    node.style.setProperty(COLUMN_VAR[side], `${clamped}px`)
    return clamped
  }

  const onPointerDown = (event: React.PointerEvent<HTMLDivElement>) => {
    const node = target.current
    if (!node) return
    event.preventDefault()
    event.currentTarget.setPointerCapture(event.pointerId)
    const box = node.getBoundingClientRect()
    let latest = currentWidth(node)

    // 直接写：改一个 CSS 变量很便宜，浏览器每帧也只重排一次。
    // 用 rAF 节流的代价是标签页被节流时（后台、隐藏）拖动整个僵住。
    const move = (moveEvent: PointerEvent) => {
      latest = apply(node, side === 'rail'
        ? moveEvent.clientX - box.left
        : box.right - moveEvent.clientX)
    }
    const up = () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
      writeStoredWidth(side, latest)
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
  }

  /** 分隔条是操作控件，不能只有鼠标能用。 */
  const onKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    const node = target.current
    if (!node) return
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return
    event.preventDefault()
    const step = (event.shiftKey ? 32 : 8) * (event.key === 'ArrowLeft' ? -1 : 1)
    // 左栏往右拖是变宽，右栏往右拖是变窄，方向相反
    const delta = side === 'rail' ? step : -step
    writeStoredWidth(side, apply(node, currentWidth(node) + delta))
  }

  return (
    <div
      className={`col-resizer col-resizer--${side}`}
      role="separator"
      tabIndex={0}
      aria-orientation="vertical"
      aria-label={limits.label}
      onPointerDown={onPointerDown}
      onKeyDown={onKeyDown}
    >
      <i aria-hidden="true" />
    </div>
  )
}
