import { useEffect, useRef, useState } from 'react'
import { SettingInfo } from './SettingInfo'
import { metricApi } from '../shared/api/client'
import { ApiError } from '../shared/api/types'
import type { IndicatorCapability } from '../shared/api/types'

type MetricSearchProps = {
  id: string; name: string; indicators: IndicatorCapability[]
  instrument: string; start: string; end: string
  onSelect: (item: IndicatorCapability) => void
  onQuery: (query: string, unit: string) => void
}

export function MetricSearch(props: MetricSearchProps) {
  const { id, name, instrument, start, end } = props
  return <MetricSearchSession key={`${id}:${name}:${instrument}:${start}:${end}`} {...props} />
}

function MetricSearchSession({ id, name, indicators, instrument, start, end, onSelect, onQuery }: MetricSearchProps) {
  const [query, setQuery] = useState(name)
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const [results, setResults] = useState<{ name: string; unit: string; note?: string }[]>([])
  const pending = useRef<AbortController | null>(null)
  useEffect(() => () => pending.current?.abort(), [])
  const matches = query.trim() ? indicators.filter(item => item.status !== 'unavailable'
    && `${item.display_name} ${item.indicator_id} ${item.description}`.toLowerCase()
      .includes(query.trim().toLowerCase())).slice(0, 6) : []
  const canQuery = indicators.some(item => item.indicator_id === 'provider.numeric' && item.status !== 'unavailable')
  const search = async () => {
    pending.current?.abort()
    const controller = new AbortController()
    pending.current = controller
    setBusy(true); setNotice(''); setResults([])
    try {
      const found = await metricApi.discover({ instrument_id: instrument,
        metric_query: query.trim(), start, end }, controller.signal)
      if (controller.signal.aborted) return
      setResults(found)
      setNotice(found.length ? '已取得匹配字段，选择后可设置比较条件；历史覆盖在回测前检查。'
        : '当前股票和区间尚未取得可用历史字段，暂不列入可选结果；原条件保留。')
    } catch (error) {
      if (controller.signal.aborted) return
      setNotice(error instanceof ApiError ? error.problem.detail ?? error.message
        : '指标查询暂未完成，原条件没有改变。')
    } finally {
      if (!controller.signal.aborted) setBusy(false)
    }
  }
  return <div className="metric-search">
    <SettingInfo label="指标搜索" help="输入指标名称或口语描述查找候选。选中后可继续调整周期、触发方式与阈值；能查询当前值不代表该指标已有可回测的历史数据。"><label htmlFor={`${id}-metric-search`}>指标</label></SettingInfo>
    <div className="metric-search-controls">
      <input id={`${id}-metric-search`} aria-label={`${id}指标搜索`} type="search" maxLength={200}
        value={query} placeholder="搜索指标名称、缩写或描述" autoComplete="off"
        onFocus={() => setOpen(true)} onChange={event => {
          pending.current?.abort(); setBusy(false); setQuery(event.target.value)
          setOpen(true); setResults([]); setNotice('')
        }} onKeyDown={event => {
          if (event.key === 'Escape') setOpen(false)
          if (event.key === 'Enter' && query.trim() && !busy && canQuery) {
            event.preventDefault(); void search()
          }
        }} />
      <button type="button" disabled={!query.trim() || busy || !canQuery} onClick={() => void search()}>
        {busy ? '查询中…' : '查询更多'}
      </button>
    </div>
    {open && matches.length > 0 ? <div className="metric-search-results" aria-label="指标联想">
      {matches.map(item => <button type="button" key={item.indicator_id} onClick={() => {
        pending.current?.abort(); setBusy(false); setResults([]); setNotice(''); onSelect(item); setOpen(false)
      }}>{item.display_name}</button>)}
    </div> : null}
    {results.length > 0 ? <div className="metric-search-results" aria-label="查询结果">
      {results.map(item => <button type="button" key={`${item.name}:${item.unit}`} onClick={() => {
        onQuery(item.name, item.unit); setResults([]); setOpen(false)
      }}>{item.name}{item.unit ? `（${item.unit}）` : ''}{item.note ? <small>{item.note}</small> : null}</button>)}
    </div> : null}
    {notice ? <p className="settings-help" role="status">{notice}</p> : null}
  </div>
}
