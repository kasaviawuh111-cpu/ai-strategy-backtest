/**
 * 净值与回撤（SPEC 4.8）
 * - 上图只用一个收益率 Y 轴；下图单独展示回撤，避免尺度混淆。
 * - B 买入、S 卖出；仅展示实际成交，密集成交合并，完整委托留在列表。
 * - 点位支持鼠标、触摸、Tab + Enter/Space；选中后由报告页定位到对应的那一笔委托，
 *   图表本身不再重复渲染一份点位明细。
 */
import { useMemo, useRef, useState, type KeyboardEvent } from 'react'

import { Notice, fmtPct, signClass } from './primitives'
import { BENCHMARK_LABEL } from './ExcessEquation'
import type { ChartMark, SeriesPoint } from '../types'

const W = 354
/* 回撤只是「收益曲线的副作用」，不该和净值抢版面：
   它占整幅高度的 ~13%，比原来的 17% 更接近它在阅读里的分量。 */
const H = 236
/* PL 会按 Y 轴标签的最大位数再动态放宽，避免 "-105%" 撞进绘图区。 */
const PL_BASE = 30
const PR = 8
const RETURN_TOP = 10
const RETURN_BOTTOM = 168
const DRAWDOWN_TOP = 192
const DRAWDOWN_BOTTOM = 224
/* 让 Y 轴刻度自适应量级：稀疏区间宽了就换更大的步长，避免 -50%..305% 挤成一团。 */
const chooseGridStep = (range: number) => {
  const target = Math.max(range, 0.01) / 4
  const magnitude = 10 ** Math.floor(Math.log10(target))
  return ([1, 2, 2.5, 5, 10].find(value => value * magnitude >= target) ?? 10) * magnitude
}

const isFiniteValue = (value: number | null | undefined): value is number =>
  typeof value === 'number' && Number.isFinite(value)
const optionalPct = (value: number | null | undefined) => isFiniteValue(value) ? fmtPct(value) : '—'

// Use the granularity actually saved in the series; never invent intraday timestamps.
const dateParts = (value: string) => {
  if (!/[T ]\d{2}:\d{2}/.test(value)) return { date: value.slice(0, 10), time: '' }
  const parsed = new Date(value)
  if (!Number.isFinite(parsed.getTime())) return { date: value.slice(0, 10), time: value.slice(11, 16) }
  const parts = new Intl.DateTimeFormat('sv-SE', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  }).formatToParts(parsed)
  const part = (type: string) => parts.find(item => item.type === type)?.value ?? ''
  return { date: `${part('year')}-${part('month')}-${part('day')}`, time: `${part('hour')}:${part('minute')}` }
}

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
  const [expandedMarks, setExpandedMarks] = useState<ChartMark[]>([])
  const svgRef = useRef<SVGSVGElement>(null)

  const geo = useMemo(() => {
    const strategy = series.map((point) => point.strategy)
    const hasBenchmark = comparisonAvailable && series.some((point) => isFiniteValue(point.benchmark))
    const benchmark = series.map((point) => comparisonAvailable && isFiniteValue(point.benchmark) ? point.benchmark : null)
    const excess = series.map((point) => {
      if (!comparisonAvailable || !isFiniteValue(point.benchmark) || point.benchmark <= -100) return null
      const value = ((1 + point.strategy / 100) / (1 + point.benchmark / 100) - 1) * 100
      return isFiniteValue(value) ? value : null
    })
    const drawdown = series.map((point) => Math.min(0, point.drawdown))
    const allReturns = [...strategy, ...benchmark, ...(comparisonAvailable ? excess : [])].filter(isFiniteValue)
    const minimum = allReturns.reduce((lo, value) => Math.min(lo, value), allReturns[0] ?? 0)
    const maximum = allReturns.reduce((hi, value) => Math.max(hi, value), allReturns[0] ?? 0)
    const rawRange = Math.max(0.1, maximum - minimum)
    const step = chooseGridStep(rawRange)
    const lo = Math.floor((minimum - rawRange * 0.08) / step) * step
    const rawHi = Math.ceil((maximum + rawRange * 0.08) / step) * step
    const hi = rawHi <= lo ? lo + step : rawHi
    const minimumDrawdown = drawdown.reduce((lo, value) => Math.min(lo, value), 0)
    const drawdownLo = Math.min(-1, minimumDrawdown)
    const grid: number[] = []
    for (let value = lo; value <= hi + 1e-9; value += step) grid.push(Number(value.toFixed(6)))
    // Include decimals, minus sign and % rather than measuring integer digits only.
    const labelLength = Math.max(...grid.map(value => `${value}%`.length), `${drawdownLo.toFixed(0)}%`.length)
    const PL = Math.max(PL_BASE, labelLength * 5.5 + 8)
    const X = (index: number) => PL + ((W - PL - PR) * index) / Math.max(1, series.length - 1)
    const returnY = (value: number) => RETURN_TOP + (RETURN_BOTTOM - RETURN_TOP)
      * (1 - (value - lo) / (hi - lo))
    const drawdownY = (value: number) => DRAWDOWN_TOP + (DRAWDOWN_BOTTOM - DRAWDOWN_TOP)
      * (1 - (value - drawdownLo) / Math.max(1, -drawdownLo))
    const line = (values: (number | null)[], Y: (value: number) => number) => values
      .map((value, index) => isFiniteValue(value)
        ? `${index && isFiniteValue(values[index - 1]) ? 'L' : 'M'}${X(index).toFixed(1)} ${Y(value).toFixed(1)}` : '')
      .join(' ')
    return {
      strategy, benchmark, excess, drawdown, hasBenchmark, comparisonAvailable, lo, hi, drawdownLo, minimumDrawdown,
      PL, X, returnY, drawdownY, line, grid,
    }
  }, [comparisonAvailable, series])

  const last = Math.max(0, series.length - 1)
  const dates = useMemo(() => series.map(point => dateParts(point.date)), [series])
  const firstDate = dates[0]?.date ?? ''
  const lastDate = dates[last]?.date ?? ''
  const sameDay = dates.length > 0 && dates.every(point => point.date === firstDate)
  const intraday = dates.some(point => point.time) && (sameDay || Date.parse(lastDate) - Date.parse(firstDate) <= 7 * 86400000)
  const timeContext = `${sameDay ? firstDate : `${firstDate} 至 ${lastDate}`} · 北京时间`
  const sameYear = dates.length > 0 && dates.every(point => point.date.slice(0, 4) === firstDate.slice(0, 4))
  const shortRange = dates.length > 0 && (dates.every(point => point.date.slice(0, 7) === firstDate.slice(0, 7))
    || Date.parse(lastDate) - Date.parse(firstDate) < 100 * 86400000)
  const tickIndices = [...new Set([0, Math.floor(last / 4), Math.floor(last / 2), Math.floor(3 * last / 4), last])]
  const clusters = useMemo(() => {
    const groups: ChartMark[][] = []
    const filled = marks.filter(mark => mark.kind !== 'no_fill' && mark.index >= 0 && mark.index < series.length)
      .sort((a, b) => a.index - b.index)
    for (const mark of filled) {
      const previous = groups[groups.length - 1]
      if (previous?.[0] && geo.X(mark.index) - geo.X(previous[0].index) < 18) previous.push(mark)
      else groups.push([mark])
    }
    return groups
  }, [marks, geo, series.length])
  const selectedMark = marks.find((mark) => mark.activityId === selectedMarkId) ?? null
  const activeIndex = selectedMark?.index ?? hover
  const tooltipPlacement = activeIndex == null
    ? null
    : geo.X(activeIndex) <= W / 2 ? 'right' : 'left'

  const point = (clientX: number) => {
    const rect = svgRef.current?.getBoundingClientRect()
    if (!rect || rect.width <= 0) return
    const px = ((clientX - rect.left) / rect.width) * W
    const index = Math.round(((px - geo.PL) / (W - geo.PL - PR)) * Math.max(0, series.length - 1))
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
  const lastBenchmark = geo.benchmark[last]
  const lastExcess = geo.excess[last]
  const activeStrategy = activeIndex == null ? 0 : geo.strategy[activeIndex] ?? 0
  const activeBenchmark = activeIndex == null ? null : geo.benchmark[activeIndex]
  const activeExcess = activeIndex == null ? null : geo.excess[activeIndex]
  const activeDrawdown = activeIndex == null ? 0 : geo.drawdown[activeIndex] ?? 0

  return (
    <>
      <div className="legend equity-legend">
        <span><i style={{ borderColor: C.strategy }} />本策略 {fmtPct(lastStrategy)}</span>
        {geo.hasBenchmark ? <span><i style={{ borderColor: C.benchmark, borderTopStyle: 'dashed' }} />{BENCHMARK_LABEL} {optionalPct(lastBenchmark)}</span> : null}
        {geo.hasBenchmark && geo.comparisonAvailable ? <span><i style={{ borderColor: C.excess }} />超额 {optionalPct(lastExcess)}</span> : null}
        <span><i style={{ borderColor: C.drawdown }} />回撤 {fmtPct(geo.drawdown[last] ?? 0)}</span>
        <label className="toggle">
          <input type="checkbox" checked={showMarks} onChange={(event) => { setShowMarks(event.target.checked); setExpandedMarks([]) }} />
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
          data-plot-left={geo.PL}
          data-view-width={W}
          role="img"
          aria-label={geo.hasBenchmark
            ? `累计收益率与回撤曲线：本策略 ${fmtPct(lastStrategy)}，同期持有 ${optionalPct(lastBenchmark)}，最大回撤 ${fmtPct(geo.minimumDrawdown)}`
            : `累计收益率与回撤曲线：本策略 ${fmtPct(lastStrategy)}，本次没有同期持有基准`}
        >
          {geo.grid.map((value) => (
            <g key={value}>
              <line x1={geo.PL} x2={W - PR} y1={geo.returnY(value)} y2={geo.returnY(value)}
                stroke="var(--hairline-2)" strokeWidth={0.5}
                strokeDasharray={value === 0 ? undefined : '3 3'} />
              <text x={geo.PL - 5} y={geo.returnY(value) + 3.5} textAnchor="end" fontSize={9} fill="var(--ink-3)">{value}%</text>
            </g>
          ))}

          {geo.hasBenchmark ? (
            <>
              {geo.comparisonAvailable ? <>
                {geo.excess.every(isFiniteValue) ? <path d={`${geo.line(geo.excess, geo.returnY)} L ${geo.X(last).toFixed(1)} ${geo.returnY(Math.max(geo.lo, Math.min(geo.hi, 0))).toFixed(1)} L ${geo.PL} ${geo.returnY(Math.max(geo.lo, Math.min(geo.hi, 0))).toFixed(1)} Z`}
                  fill="var(--fill-excess)" /> : null}
                <path d={geo.line(geo.excess, geo.returnY)} fill="none" stroke={C.excess} strokeWidth={1.4} strokeOpacity={0.7} />
              </> : null}
              <path d={geo.line(geo.benchmark, geo.returnY)} fill="none" stroke={C.benchmark} strokeWidth={2}
                strokeDasharray="5 4" strokeLinecap="round" />
              {/* A single available sample between gaps is a dot, not an invisible move command. */}
              {(['benchmark', ...(geo.comparisonAvailable ? ['excess'] : [])] as ('benchmark' | 'excess')[]).flatMap(kind =>
                geo[kind].map((value, index, values) => isFiniteValue(value)
                  && !isFiniteValue(values[index - 1]) && !isFiniteValue(values[index + 1])
                  ? <circle key={`${kind}-${index}`} data-isolated-series={kind} cx={geo.X(index)} cy={geo.returnY(value)}
                    r={2.2} fill={C[kind]} pointerEvents="none" /> : null))}
            </>
          ) : null}
          <path d={geo.line(geo.strategy, geo.returnY)} fill="none" stroke={C.strategy} strokeWidth={2}
            strokeLinecap="round" strokeLinejoin="round" />

          {showMarks ? (
            <g aria-label="交易点位">
              {clusters.map((group) => {
                const mark = group[0]
                if (!mark) return null
                const x = geo.X(mark.index)
                const y = geo.returnY(geo.strategy[mark.index] ?? 0)
                const active = selectedMarkId === mark.activityId
                return (
                  <g
                    key={mark.activityId}
                    role="button"
                    tabIndex={0}
                    className="chart-trade-mark"
                    aria-label={group.length > 1 ? `${group.length}笔成交，展开明细` : `${markLabel[mark.kind]}，${formatTime(mark.occurredAt)}，按回车键定位委托`}
                    aria-pressed={active}
                    onClick={() => { if (group.length > 1) setExpandedMarks(group); else selectMark(mark) }}
                    onKeyDown={(event) => {
                      if (group.length > 1 && (event.key === 'Enter' || event.key === ' ')) { event.preventDefault(); setExpandedMarks(group) }
                      else activateWithKeyboard(event, mark)
                    }}
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
                      {group.length > 1 ? group.length : mark.side === 'buy' ? 'B' : 'S'}
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
              stroke="var(--ink-4)" strokeWidth={0.8} strokeDasharray="3 3" pointerEvents="none" />
          ) : null}

          <text x={geo.PL} y={DRAWDOWN_TOP - 7} fontSize={8.5} fill="var(--ink-3)">账户回撤</text>
          <line x1={geo.PL} x2={W - PR} y1={geo.drawdownY(0)} y2={geo.drawdownY(0)}
            stroke="var(--hairline-2)" strokeWidth={0.5} />
          <path d={`${geo.line(geo.drawdown, geo.drawdownY)} L ${geo.X(last).toFixed(1)} ${geo.drawdownY(0).toFixed(1)} L ${geo.PL} ${geo.drawdownY(0).toFixed(1)} Z`}
            fill="var(--fill-drawdown)" />
          <path d={geo.line(geo.drawdown, geo.drawdownY)} fill="none" stroke={C.drawdown} strokeWidth={1.4} />
          <text x={geo.PL - 5} y={geo.drawdownY(0) + 3} textAnchor="end" fontSize={9} fill="var(--ink-3)">0%</text>
          <text x={geo.PL - 5} y={DRAWDOWN_BOTTOM} textAnchor="end" fontSize={9} fill="var(--ink-3)">{geo.drawdownLo.toFixed(0)}%</text>
        </svg>

        {activeIndex != null && tooltipPlacement ? (
          <div className={`tip on tip--${tooltipPlacement}`} data-placement={tooltipPlacement} style={{ top: 6 }}>
            <div className="d">{series[activeIndex]?.date ?? ''}</div>
            <div className="r"><em><i style={{ borderColor: C.strategy }} />本策略</em><b className={signClass(activeStrategy)}>{fmtPct(activeStrategy)}</b></div>
            {geo.hasBenchmark ? <div className="r"><em><i style={{ borderColor: C.benchmark }} />{BENCHMARK_LABEL}</em><b className={isFiniteValue(activeBenchmark) ? signClass(activeBenchmark) : ''}>{optionalPct(activeBenchmark)}</b></div> : null}
            {geo.hasBenchmark && geo.comparisonAvailable ? <div className="r"><em><i style={{ borderColor: C.excess }} />超额</em><b className={isFiniteValue(activeExcess) ? signClass(activeExcess) : ''}>{optionalPct(activeExcess)}</b></div> : null}
            <div className="r"><em><i style={{ borderColor: C.drawdown }} />回撤</em><b className="down">{fmtPct(activeDrawdown)}</b></div>
          </div>
        ) : null}
      </div>

      {showMarks && expandedMarks.length > 0 ? <div className="chart-trade-details" aria-label="合并成交明细">
        <div>{expandedMarks.length}笔成交 <button type="button" onClick={() => setExpandedMarks([])}>收起</button></div>
        {expandedMarks.map(mark => <button type="button" key={mark.activityId} onClick={() => selectMark(mark)}>
          {markLabel[mark.kind]} · {formatTime(mark.occurredAt)} · 定位委托
        </button>)}
      </div> : null}

      <div className="axis-x" aria-label={intraday ? timeContext : '日期'}
        style={{ marginLeft: `${geo.PL / W * 100}%`, marginRight: `${PR / W * 100}%` }}>
        {tickIndices.map((index) => {
          const value = dates[index]
          if (!value) return null
          return <span key={index} title={series[index]?.date}
            style={{ left: `${last ? index / last * 100 : 0}%`, transform: index === 0 ? 'none' : index === last ? 'translateX(-100%)' : 'translateX(-50%)' }}>
            {intraday && sameDay ? value.time : shortRange ? value.date.slice(5) : value.date.slice(0, 7)}
            {intraday && !sameDay ? <small>{value.time}</small> : null}
            {!intraday && shortRange && !sameYear ? <small>{value.date.slice(0, 4)}</small> : null}
          </span>
        })}
      </div>
      {intraday ? <div className="axis-context">{timeContext}</div> : null}

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
