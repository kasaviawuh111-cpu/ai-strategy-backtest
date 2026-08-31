/**
 * 净值与回撤（SPEC 4.8）
 * - 上图只用一个收益率 Y 轴；下图单独展示回撤，避免尺度混淆。
 * - B 买入、S 卖出；未成交点使用空心虚线，不只靠颜色。
 * - 点位支持鼠标、触摸、Tab + Enter/Space；选中后由报告页定位到对应的那一笔委托，
 *   图表本身不再重复渲染一份点位明细。
 */
import { useMemo, useRef, useState, type KeyboardEvent } from 'react'

import { Notice, fmtPct, signClass } from './primitives'
import { BENCHMARK_LABEL } from './ExcessEquation'
import type { ChartMark, SeriesPoint } from '../types'

const W = 354
const H = 244
const PL = 28
const PR = 6
const RETURN_TOP = 10
const RETURN_BOTTOM = 164
const DRAWDOWN_TOP = 190
const DRAWDOWN_BOTTOM = 232

const C = {
  strategy: 'var(--color-primary)',
  benchmark: 'var(--ink-3)',
  excess: 'var(--color-info)',
  drawdown: 'var(--color-up)',
}

const markLabel: Record<ChartMark['kind'], string> = {
  buy: '买入成交',
  sell: '卖出成交',
  no_fill: '未成交或过期',
}

const formatTime = (value: string | null) => {
  if (!value) return '未保存'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  }).format(parsed).replaceAll('/', '-') + ' · 北京时间'
}

export function EquityChart(
  { series, marks, comparisonAvailable = true, selectedMarkId = null, onSelectMark }:
  { series: SeriesPoint[]; marks: ChartMark[];
    comparisonAvailable?: boolean;
    /** 选中状态由报告页持有：图上的点和下面的委托列表是同一个选中。 */
    selectedMarkId?: string | null;
    onSelectMark?: (mark: ChartMark) => void },
) {
  const [showMarks, setShowMarks] = useState(true)
  const [hover, setHover] = useState<number | null>(null)
  const svgRef = useRef<SVGSVGElement>(null)

  const geo = useMemo(() => {
    const strategy = series.map((point) => point.strategy)
    const hasBenchmark = series.some((point) => point.benchmark != null)
    const benchmark = series.map((point) => point.benchmark ?? 0)
    const excess = series.map((point) => !comparisonAvailable || point.benchmark == null
      ? 0
      : +(point.strategy - point.benchmark).toFixed(2))
    const drawdown = series.map((point) => Math.min(0, point.drawdown))
    const allReturns = hasBenchmark
      ? strategy.concat(benchmark, comparisonAvailable ? excess : [])
      : strategy
    const minimum = allReturns.length > 0 ? Math.min(...allReturns) : 0
    const maximum = allReturns.length > 0 ? Math.max(...allReturns) : 0
    const lo = Math.floor(minimum / 10) * 10 - 5
    const rawHi = Math.ceil(maximum / 10) * 10 + 5
    const hi = rawHi <= lo ? lo + 10 : rawHi
    const drawdownLo = Math.min(-1, ...drawdown)
    const X = (index: number) => PL + ((W - PL - PR) * index) / Math.max(1, series.length - 1)
    const returnY = (value: number) => RETURN_TOP + (RETURN_BOTTOM - RETURN_TOP)
      * (1 - (value - lo) / (hi - lo))
    const drawdownY = (value: number) => DRAWDOWN_TOP + (DRAWDOWN_BOTTOM - DRAWDOWN_TOP)
      * (1 - (value - drawdownLo) / Math.max(1, -drawdownLo))
    const line = (values: number[], Y: (value: number) => number) => values
      .map((value, index) => `${index ? 'L' : 'M'}${X(index).toFixed(1)} ${Y(value).toFixed(1)}`)
      .join(' ')
    const grid: number[] = []
    for (let value = lo; value <= hi; value += 10) grid.push(value)
    return {
      strategy, benchmark, excess, drawdown, hasBenchmark, comparisonAvailable, lo, hi, drawdownLo,
      X, returnY, drawdownY, line, grid,
    }
  }, [comparisonAvailable, series])

  const last = Math.max(0, series.length - 1)
  const selectedMark = marks.find((mark) => mark.activityId === selectedMarkId) ?? null
  const activeIndex = selectedMark?.index ?? hover
  const tooltipPlacement = activeIndex == null
    ? null
    : geo.X(activeIndex) <= W / 2 ? 'right' : 'left'

  const point = (clientX: number) => {
    const rect = svgRef.current?.getBoundingClientRect()
    if (!rect || rect.width <= 0) return
    const px = ((clientX - rect.left) / rect.width) * W
    const index = Math.round(((px - PL) / (W - PL - PR)) * Math.max(0, series.length - 1))
    setHover(Math.max(0, Math.min(series.length - 1, index)))
  }

  const selectMark = (mark: ChartMark) => {
    setHover(mark.index)
    onSelectMark?.(mark)
  }

  const activateWithKeyboard = (event: KeyboardEvent<SVGGElement>, mark: ChartMark) => {
    if (event.key !== 'Enter' && event.key !== ' ') return
    event.preventDefault()
    selectMark(mark)
  }

  if (series.length === 0) {
    return <div className="chart-empty">本次结果没有保存净值与回撤序列。请重新读取结果，仍缺失时检查数据快照。</div>
  }

  const lastStrategy = geo.strategy[last] ?? 0
  const lastBenchmark = geo.benchmark[last] ?? 0
  const lastExcess = geo.excess[last] ?? 0
  const activeStrategy = activeIndex == null ? 0 : geo.strategy[activeIndex] ?? 0
  const activeBenchmark = activeIndex == null ? 0 : geo.benchmark[activeIndex] ?? 0
  const activeExcess = activeIndex == null ? 0 : geo.excess[activeIndex] ?? 0
  const activeDrawdown = activeIndex == null ? 0 : geo.drawdown[activeIndex] ?? 0

  return (
    <>
      <div className="legend">
        <span><i style={{ borderColor: C.strategy }} />本策略 {fmtPct(lastStrategy)}</span>
        {geo.hasBenchmark ? <span><i style={{ borderColor: C.benchmark, borderTopStyle: 'dashed' }} />{BENCHMARK_LABEL} {fmtPct(lastBenchmark)}</span> : null}
        {geo.hasBenchmark && geo.comparisonAvailable ? <span><i style={{ borderColor: C.excess }} />超额 {fmtPct(lastExcess)}</span> : null}
        <span><i style={{ borderColor: C.drawdown }} />回撤 {fmtPct(geo.drawdown[last] ?? 0)}</span>
        <label className="toggle">
          <input type="checkbox" checked={showMarks} onChange={(event) => setShowMarks(event.target.checked)} />
          <span className="sw" />
          买卖点
        </label>
      </div>

      <div
        className="chartwrap"
        onMouseMove={(event) => point(event.clientX)}
        onMouseLeave={() => { if (!selectedMark) setHover(null) }}
        onTouchStart={(event) => { const touch = event.touches[0]; if (touch) point(touch.clientX) }}
        onTouchMove={(event) => { const touch = event.touches[0]; if (touch) point(touch.clientX) }}
      >
        <svg
          ref={svgRef}
          className="chart"
          viewBox={`0 0 ${W} ${H}`}
          data-plot-left={PL}
          data-view-width={W}
          role="img"
          aria-label={geo.hasBenchmark
            ? `累计收益率与回撤曲线：本策略 ${fmtPct(lastStrategy)}，同期持有 ${fmtPct(lastBenchmark)}，最大回撤 ${fmtPct(Math.min(...geo.drawdown))}`
            : `累计收益率与回撤曲线：本策略 ${fmtPct(lastStrategy)}，本次没有同期持有基准`}
        >
          {geo.grid.map((value) => (
            <g key={value}>
              <line x1={PL} x2={W - PR} y1={geo.returnY(value)} y2={geo.returnY(value)}
                stroke="var(--hairline-2)" strokeWidth={0.5}
                strokeDasharray={value === 0 ? undefined : '3 3'} />
              <text x={PL - 5} y={geo.returnY(value) + 3.5} textAnchor="end" fontSize={10} fill="var(--ink-3)">{value}%</text>
            </g>
          ))}

          {geo.hasBenchmark ? (
            <>
              {geo.comparisonAvailable ? <>
                <path d={`${geo.line(geo.excess, geo.returnY)} L ${geo.X(last).toFixed(1)} ${geo.returnY(0).toFixed(1)} L ${PL} ${geo.returnY(0).toFixed(1)} Z`}
                  fill="rgba(59,132,255,.14)" />
                <path d={geo.line(geo.excess, geo.returnY)} fill="none" stroke={C.excess} strokeWidth={1.4} strokeOpacity={0.7} />
              </> : null}
              <path d={geo.line(geo.benchmark, geo.returnY)} fill="none" stroke={C.benchmark} strokeWidth={2}
                strokeDasharray="5 4" strokeLinecap="round" />
            </>
          ) : null}
          <path d={geo.line(geo.strategy, geo.returnY)} fill="none" stroke={C.strategy} strokeWidth={2}
            strokeLinecap="round" strokeLinejoin="round" />

          {showMarks ? (
            <g aria-label="交易点位">
              {marks.filter((mark) => mark.index >= 0 && mark.index < series.length).map((mark) => {
                const x = geo.X(mark.index)
                const y = geo.returnY(geo.strategy[mark.index] ?? 0)
                const active = selectedMarkId === mark.activityId
                return (
                  <g
                    key={mark.activityId}
                    role="button"
                    tabIndex={0}
                    aria-label={`${markLabel[mark.kind]}，${formatTime(mark.occurredAt)}，按回车键定位委托`}
                    aria-pressed={active}
                    onClick={() => selectMark(mark)}
                    onKeyDown={(event) => activateWithKeyboard(event, mark)}
                    style={{ cursor: 'pointer' }}
                  >
                    <circle cx={x} cy={y} r={12} fill="transparent" />
                    <circle
                      cx={x}
                      cy={y}
                      r={6.2}
                      fill={mark.kind === 'no_fill'
                        ? 'var(--surface)'
                        : mark.side === 'buy' ? 'var(--color-up)' : 'var(--color-down)'}
                      stroke={mark.kind === 'no_fill' ? 'var(--ink-3)' : 'var(--surface)'}
                      strokeWidth={active ? 2.6 : 1.5}
                      strokeDasharray={mark.kind === 'no_fill' ? '2 1.6' : undefined}
                    />
                    <text
                      x={x}
                      y={y + 2.6}
                      textAnchor="middle"
                      fontFamily="var(--font-num)"
                      fontSize={7.5}
                      fontWeight={700}
                      fill={mark.kind === 'no_fill' ? 'var(--ink-3)' : '#fff'}
                      pointerEvents="none"
                    >
                      {mark.side === 'buy' ? 'B' : 'S'}
                    </text>
                  </g>
                )
              })}
            </g>
          ) : null}

          <circle cx={geo.X(last)} cy={geo.returnY(lastStrategy)} r={3.4}
            fill={C.strategy} stroke="var(--surface)" strokeWidth={1.6} pointerEvents="none" />

          {activeIndex != null ? (
            <line x1={geo.X(activeIndex)} x2={geo.X(activeIndex)} y1={RETURN_TOP} y2={DRAWDOWN_BOTTOM}
              stroke="var(--ink-4)" strokeWidth={0.8} strokeDasharray="3 3" />
          ) : null}

          <text x={PL} y={DRAWDOWN_TOP - 8} fontSize={10} fill="var(--ink-3)">账户回撤</text>
          <line x1={PL} x2={W - PR} y1={geo.drawdownY(0)} y2={geo.drawdownY(0)}
            stroke="var(--hairline-2)" strokeWidth={0.5} />
          <path d={`${geo.line(geo.drawdown, geo.drawdownY)} L ${geo.X(last).toFixed(1)} ${geo.drawdownY(0).toFixed(1)} L ${PL} ${geo.drawdownY(0).toFixed(1)} Z`}
            fill="rgba(246,47,63,.10)" />
          <path d={geo.line(geo.drawdown, geo.drawdownY)} fill="none" stroke={C.drawdown} strokeWidth={1.4} />
          <text x={PL - 5} y={geo.drawdownY(0) + 3} textAnchor="end" fontSize={9} fill="var(--ink-3)">0%</text>
          <text x={PL - 5} y={DRAWDOWN_BOTTOM} textAnchor="end" fontSize={9} fill="var(--ink-3)">{geo.drawdownLo.toFixed(0)}%</text>
        </svg>

        {activeIndex != null && tooltipPlacement ? (
          <div className={`tip on tip--${tooltipPlacement}`} data-placement={tooltipPlacement} style={{ top: 6 }}>
            <div className="d">{series[activeIndex]?.date ?? ''}</div>
            <div className="r"><em><i style={{ borderColor: C.strategy }} />本策略</em><b className={signClass(activeStrategy)}>{fmtPct(activeStrategy)}</b></div>
            {geo.hasBenchmark ? <div className="r"><em><i style={{ borderColor: C.benchmark }} />{BENCHMARK_LABEL}</em><b className={signClass(activeBenchmark)}>{fmtPct(activeBenchmark)}</b></div> : null}
            {geo.hasBenchmark && geo.comparisonAvailable ? <div className="r"><em><i style={{ borderColor: C.excess }} />超额</em><b className={signClass(activeExcess)}>{fmtPct(activeExcess)}</b></div> : null}
            <div className="r"><em><i style={{ borderColor: C.drawdown }} />回撤</em><b className="down">{fmtPct(activeDrawdown)}</b></div>
          </div>
        ) : null}
      </div>

      <div className="axis-x">
        {[...new Set([0, Math.floor(last / 4), Math.floor(last / 2), Math.floor((3 * last) / 4), last])].map((index) => (
          <span key={index}>{(series[index]?.date ?? series[0]?.date ?? '').slice(0, 7)}</span>
        ))}
      </div>

      {/*
        这里原来有一套「上一点 / 选择点位 / 下一点」加一块「点位详情」，
        和下面的委托列表是同一批数据的第二套入口。现在只保留图上的点：
        点一下就定位到下面对应的那一笔，明细在列表里看，不在图下面再开一份。
      */}
      {marks.length === 0 ? (
        <div className="settings-note">
          <Notice>这段区间没有成交或未成交点位。可以延长回测区间，或检查买卖条件是否过严。</Notice>
        </div>
      ) : null}
    </>
  )
}
