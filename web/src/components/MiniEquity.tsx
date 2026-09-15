/**
 * 结果卡里的迷你净值曲线。
 *
 * 对话里的结果卡原来只有四个数字，用户必须点进报告才知道这条曲线长什么样、
 * 买卖点打在哪里。这里把「形状」提到外面：一条策略线、一条买入持有虚线、以及 B/S 点。
 *
 * 它是缩略图，不是图表：不做坐标轴、不做 tooltip、不可点单个点，
 * 想看明细就点卡片进报告，避免在会话里再长出第二套交互。
 */
import { useMemo } from 'react'

import type { ChartMark, SeriesPoint } from '../types'

const W = 320
const H = 76
const PAD_Y = 9

export function MiniEquity(
  { series, marks, label }:
  { series: SeriesPoint[]; marks: ChartMark[]; label: string },
) {
  const geo = useMemo(() => {
    if (series.length < 2) return null
    const strategy = series.map((point) => point.strategy)
    const hasBenchmark = series.some((point) => point.benchmark != null)
    const benchmark = series.map((point) => point.benchmark ?? 0)
    const values = hasBenchmark ? strategy.concat(benchmark) : strategy
    const lo = values.reduce((minimum, value) => Math.min(minimum, value), values[0] ?? 0)
    const hi = values.reduce((maximum, value) => Math.max(maximum, value), values[0] ?? 0)
    const span = hi - lo || 1
    const X = (index: number) => (W * index) / (series.length - 1)
    const Y = (value: number) => PAD_Y + (H - PAD_Y * 2) * (1 - (value - lo) / span)
    const line = (input: number[]) => input
      .map((value, index) => `${index ? 'L' : 'M'}${X(index).toFixed(1)} ${Y(value).toFixed(1)}`)
      .join(' ')
    return { strategy, benchmark, hasBenchmark, X, Y, line }
  }, [series])

  if (!geo) return null

  const visibleMarks = marks.filter((mark) =>
    mark.kind !== 'no_fill' && mark.index >= 0 && mark.index < series.length)

  return (
    <div className="mini-equity">
      <svg viewBox={`0 0 ${W} ${H}`} className="mini-equity-svg" role="img" aria-label={label}>
        {geo.hasBenchmark ? (
          <path d={geo.line(geo.benchmark)} fill="none" stroke="var(--ink-4)"
            strokeWidth={1.4} strokeDasharray="4 4" strokeLinecap="round" />
        ) : null}
        <path d={geo.line(geo.strategy)} fill="none" stroke="var(--color-primary)"
          strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" />
        {visibleMarks.map((mark) => {
          const x = geo.X(mark.index)
          const y = geo.Y(geo.strategy[mark.index] ?? 0)
          return (
            <g key={mark.activityId}>
              <circle cx={x} cy={y} r={5.4}
                fill={mark.side === 'buy' ? 'var(--color-up)' : 'var(--color-down)'}
                stroke="var(--surface)" strokeWidth={1.4} />
              <text x={x} y={y + 2.3} textAnchor="middle" fontFamily="var(--font-num)"
                fontSize={6.6} fontWeight={700} fill="#fff">
                {mark.side === 'buy' ? 'B' : 'S'}
              </text>
            </g>
          )
        })}
      </svg>
      <div className="mini-equity-legend">
        <span><i className="ln ln--strategy" />本策略</span>
        {geo.hasBenchmark ? <span><i className="ln ln--bench" />买入后一直持有</span> : null}
        <span className="mini-equity-marks">
          <i className="bs bs--buy">B</i> 买入 <i className="bs bs--sell">S</i> 卖出
        </span>
      </div>
    </div>
  )
}
