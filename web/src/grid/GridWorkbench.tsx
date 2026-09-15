import { cloneElement, useEffect, useId, useState } from 'react'
import type { FormEvent, ReactElement } from 'react'
import { gridApi, instrumentApi } from '../shared/api/client'
import { dataAsOfDate } from '../shared/api/contract'
import type { Instrument } from '../shared/api/types'
import { validateBacktestDates } from '../shared/backtest-date-validation'
import type { GridParameters, GridResult } from './types'
import './grid.css'
import { STANDARD_INITIAL_CASH_CNY } from '../shared/config/backtest'

const defaults: GridParameters = {
  anchor_mode: 'first_open', startup_mode: 'wait_for_crossing',
  anchor_price: 20, lower_price: 15, upper_price: 25, spacing_mode: 'cny', spacing: 1,
  sizing_mode: 'shares', order_shares: 100, order_amount_cny: 5000,
  initial_shares: 500, min_shares: 100, max_shares: 2000, max_position_cny: null,
  price_mode: 'next_open', buy_limit: null, sell_limit: null, limit_offset_cny: 0,
  initial_cash_cny: STANDARD_INITIAL_CASH_CNY, commission_rate: 0.00025, minimum_commission_cny: 5,
  stamp_tax_rate: 0.0005, transfer_fee_rate: 0.00001, slippage_bps: 5,
}
const labels: Record<string, string> = {
  filled: '已成交', partial_fill: '部分成交', security_suspended: '停牌，未成交',
  adverse_price_limit: '涨跌停限制，未成交', minimum_inventory_or_t_plus_one: '底仓或当日可卖数量限制',
  limit_not_marketable_at_next_open: '开盘价格不满足限价',
  cash_position_or_lot_limit: '资金、持仓上限或最小申报数量限制',
  previous_session_liquidity_unavailable: '前一交易日流动性数据不足',
  outside_daily_price_limit: '委托价格超出当日涨跌停范围',
  insufficient_cash_after_fees: '扣除费用后资金不足',
  proceeds_do_not_cover_fees: '卖出金额不足以覆盖费用',
}
const money = (n: number) => Number(n).toLocaleString('zh-CN', { maximumFractionDigits: 2 })
const pct = (n: number) => `${(Number(n) * 100).toFixed(2)}%`
function Field({ label, children }: { label: string; children: ReactElement<{ id?: string }> }) {
  const id = useId()
  return <div className="grid-field"><label htmlFor={id}>{label}</label>{cloneElement(children, { id })}</div>
}

export function GridWorkbench() {
  const [params, setParams] = useState(defaults)
  const [stock, setStock] = useState('东方财富')
  const [instrument, setInstrument] = useState({ symbol: '300059.SZ', name: '东方财富' })
  const [choices, setChoices] = useState<Instrument[]>([])
  const [searchError, setSearchError] = useState('')
  const [end, setEnd] = useState(dataAsOfDate())
  const [start, setStart] = useState(`${Number(dataAsOfDate().slice(0, 4)) - 1}${dataAsOfDate().slice(4)}`)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [result, setResult] = useState<GridResult | null>(null)
  const update = <K extends keyof GridParameters>(key: K, value: GridParameters[K]) => {
    setParams(p => ({ ...p, [key]: value }))
    setError('')
  }
  useEffect(() => {
    if (stock === instrument.name || !stock.trim()) return
    const controller = new AbortController()
    const timer = window.setTimeout(() => {
      instrumentApi.search(stock, controller.signal).then(response => {
        setChoices(response.items)
        setSearchError(response.items.length ? '' : '未找到股票，请输入名称或代码。')
      }).catch(error => {
        if (!controller.signal.aborted) setSearchError(error instanceof Error ? error.message : '股票搜索暂未返回结果')
      })
    }, 250)
    return () => { window.clearTimeout(timer); controller.abort() }
  }, [stock, instrument.name])

  const number = (key: keyof GridParameters, label: string, step = 'any', nullable = false) =>
    <Field label={label}><input type="number" min="0" step={step} required={!nullable}
      value={params[key] ?? ''} onChange={event => update(key,
        event.target.value === '' && nullable ? null : Number(event.target.value),
      )} /></Field>

  const previewAnchor = params.anchor_mode === 'manual' ? params.anchor_price : (
    result?.initialization.anchor_mode === 'first_open' && result.request.instrument_id === instrument.symbol
      && stock === instrument.name && result.request.start === start && result.request.end === end
      ? Number(result.initialization.anchor_price) : null
  )
  const lines = previewAnchor == null ? [] : [-2, -1, 0, 1, 2].map(n => params.spacing_mode === 'percent'
    ? previewAnchor * Math.pow(1 + params.spacing / 100, n)
    : previewAnchor + n * params.spacing * (params.spacing_mode === 'cny' ? 1 : previewAnchor / 100))
    .filter(price => price > 0 && price >= params.lower_price && price <= params.upper_price)

  const run = async (event: FormEvent) => {
    event.preventDefault()
    const dates = validateBacktestDates(start, end)
    if (!dates.valid) { setError(dates.reason); return }
    if (stock !== instrument.name) { setError('请在搜索结果中选定本次股票。'); return }
    if (params.lower_price >= params.upper_price) {
      setError('网格上界须高于下界。'); return
    }
    if (params.anchor_mode === 'manual' && (params.anchor_price == null
      || params.anchor_price < params.lower_price || params.anchor_price > params.upper_price)) {
      setError('请将手动基准价设在网格上下界内。'); return
    }
    if (params.min_shares > params.max_shares || params.initial_shares > params.max_shares) {
      setError('初始建仓和最小底仓均不得超过最大持仓。'); return
    }
    setBusy(true); setError('')
    try {
      const report = await gridApi.run({ instrument_id: instrument.symbol, start, end,
        parameters: { ...params, anchor_price: params.anchor_mode === 'first_open' ? null : params.anchor_price } })
      setResult(report)
    } catch (error) {
      setError(error instanceof Error ? error.message : '暂未取得回测结果，参数已保留。')
    } finally { setBusy(false) }
  }
  const [low, high] = result ? [
    result.series.reduce((minimum, p) => Math.min(minimum, Number(p.equity_cny)), Infinity),
    result.series.reduce((maximum, p) => Math.max(maximum, Number(p.equity_cny)), -Infinity),
  ] : [0, 1]
  const points = result?.series.map((point, i) =>
    `${i / Math.max(1, result.series.length - 1) * 600},${120 - (Number(point.equity_cny) - low) / Math.max(1, high - low) * 100}`).join(' ')

  return <main className="grid-workbench">
    <header className="grid-header"><a href="/">← 返回对话</a><span>网格回测</span></header>
    <div className="grid-layout">
      <form onSubmit={run} className="grid-form">
        <h1>配置网格回测</h1>
        <p className="grid-intro">以下为可修改的示例参数。先确认股票、基准价与持仓范围，再用历史行情验证。</p>
        <fieldset disabled={busy}><legend>股票与回测区间</legend>
          <div className="grid-fields">
            <div className="grid-stock"><Field label="股票"><input value={stock} onChange={event => {
              setStock(event.target.value); setChoices([]); setSearchError('')
            }} autoComplete="off" /></Field>
              {stock === instrument.name ? <small>{instrument.symbol}</small> : null}
              {choices.length ? <div className="grid-stock-options">{choices.map(item =>
                <button type="button" key={item.symbol} onClick={() => {
                  setInstrument(item); setStock(item.name); setChoices([])
                }}>{item.name} <small>{item.symbol}</small></button>)}</div> : null}
              {searchError ? <p role="status">{searchError}</p> : null}
            </div>
            {number('initial_cash_cny', '初始总资金（元）', '1')}
            <Field label="开始日期"><input type="date" required value={start} onChange={e => setStart(e.target.value)} /></Field>
            <Field label="结束日期"><input type="date" required max={dataAsOfDate()} value={end} onChange={e => setEnd(e.target.value)} /></Field>
          </div>
        </fieldset>
        <fieldset disabled={busy}><legend>基准价与启动方式</legend><div className="grid-fields">
          <Field label="基准价来源"><select value={params.anchor_mode} onChange={e => update('anchor_mode', e.target.value as GridParameters['anchor_mode'])}>
            <option value="first_open">回测起始开盘价（默认）</option><option value="manual">手动固定基准价</option>
          </select></Field>
          {params.anchor_mode === 'manual' ? <>
            {number('anchor_price', '固定基准价（元）', '0.01')}
            <Field label="启动方式"><select value={params.startup_mode} onChange={e => update('startup_mode', e.target.value as GridParameters['startup_mode'])}>
              <option value="wait_for_crossing">只响应启动后的跨格变化</option><option value="catch_up">启动时补到对应格位</option>
            </select></Field>
          </> : null}
        </div><p className="grid-note">{params.anchor_mode === 'first_open'
          ? '取所选区间内首个可用交易日的不复权开盘价，整段回测固定不变；不是今天的现价。查询后会显示实际基准价。'
          : params.startup_mode === 'wait_for_crossing'
            ? '保留手动格线，忽略启动前的价差；只有启动后跨过新的格线才生成网格委托。'
            : '按启动价相对基准价的格数补格；仍在收盘确认、下一交易日开盘模拟，不会直接改变持仓。'}</p></fieldset>
        <fieldset disabled={busy}><legend>网格间距</legend><div className="grid-fields">
          <Field label="间距计算"><select value={params.spacing_mode} onChange={e => update('spacing_mode', e.target.value as GridParameters['spacing_mode'])}>
            <option value="cny">固定价差 · 元</option><option value="anchor_percent">固定基准价的百分比 · %</option><option value="percent">相邻格线等比 · %</option>
          </select></Field>
          {number('spacing', params.spacing_mode === 'cny' ? '每格价差（元）' : '每格间距（%，1 表示 1%）')}
          {number('lower_price', '网格下界（元）', '0.01')}{number('upper_price', '网格上界（元）', '0.01')}
        </div><p className="grid-note">上下界须包含基准价；超出范围时暂停新增网格委托，持仓保留。等比网格向上乘以（1＋间距），向下除以该比例。</p>
          {previewAnchor == null ? <p className="grid-note">查询历史行情后显示基准价附近格线。</p>
            : <div className="grid-lines" aria-label="基准价附近格线预览">{lines.map((p, i) => <span key={i}>{p.toFixed(2)} 元</span>)}</div>}
        </fieldset>
        <fieldset disabled={busy}><legend>每格委托</legend><div className="grid-fields">
          <Field label="下单方式"><select value={params.sizing_mode} onChange={e => update('sizing_mode', e.target.value as GridParameters['sizing_mode'])}>
            <option value="shares">固定股数</option><option value="amount">固定金额上限</option>
          </select></Field>
          {params.sizing_mode === 'shares' ? number('order_shares', '每格委托股数（股）', '1') : number('order_amount_cny', '每格委托金额（元，不含费用）')}
          <Field label="委托价格"><select value={params.price_mode} onChange={e => update('price_mode', e.target.value as GridParameters['price_mode'])}>
            <option value="next_open">下一交易日开盘价模拟</option><option value="grid_limit">按触发格线设置限价</option><option value="fixed_limit">自定义买卖限价</option>
          </select></Field>
          {params.price_mode === 'grid_limit' ? number('limit_offset_cny', '限价让价（元，买入加/卖出减）', '0.01') : null}
          {params.price_mode === 'fixed_limit' ? <>{number('buy_limit', '买入最高限价（元）', '0.01')}{number('sell_limit', '卖出最低限价（元）', '0.01')}</> : null}
        </div><p className="grid-note">金额按板块申报数量向下取整。限价单仅在次日开盘满足价格时模拟成交；不推测盘中成交。</p></fieldset>
        <fieldset disabled={busy}><legend>底仓与持仓上限</legend><div className="grid-fields">
          {number('initial_shares', '初始建仓（股）', '1')}{number('min_shares', '最小底仓（股）', '1')}
          {number('max_shares', '最大持仓（股）', '1')}{number('max_position_cny', '买入后持仓市值上限（元，可留空）', 'any', true)}
        </div><p className="grid-note">初始建仓从总资金中支付，并受 T+1 约束。底仓限制卖出，上限限制买入；未成交不改变持仓。</p></fieldset>
        <details><summary>成交费用与滑点</summary><fieldset disabled={busy}><div className="grid-fields">
          {number('commission_rate', '佣金率（小数）')}{number('minimum_commission_cny', '最低佣金（元）')}
          {number('slippage_bps', '滑点（基点，1 = 0.01%）')}
        </div></fieldset></details>
        <p className="grid-note">本轮为日线、不含分红送转的股价回测。交易数量、T+1、停牌与涨跌停按 A 股规则约束。</p>
        {error ? <p className="grid-error" role="alert">{error}</p> : null}
        <button className="grid-submit" disabled={busy} type="submit">{busy ? '正在查询行情并回测…' : '按这些参数回测'}</button>
      </form>
      <aside className="grid-report" aria-label="网格回测结果">
        {result ? <>
          <div className="grid-report-top"><h2>这次网格表现</h2><span>{result.provenance.instrument_id}</span></div>
          <p className="grid-note">对应上次已运行参数。修改左侧参数后，点击回测生成新结果。</p>
          <p className="grid-anchor-result">本次基准价 <strong>{money(result.initialization.anchor_price)} 元</strong><br />
            <small>{result.initialization.anchor_mode === 'first_open'
              ? `${result.initialization.reference_date} 开盘价 · 全程固定`
              : `手动设定 · ${result.initialization.startup_mode === 'catch_up' ? '启动补格' : '只响应新跨格'}`}</small></p>
          <div className="grid-metrics"><div><strong>{pct(result.summary.total_return)}</strong><span>股价策略收益</span></div>
            <div><strong>{pct(result.summary.max_drawdown)}</strong><span>最大回撤</span></div>
            <div><strong>{result.summary.filled_orders}</strong><span>成交笔数</span></div>
            <div><strong>{money(result.summary.total_fees_cny)}</strong><span>总费用（元）</span></div></div>
          <svg className="grid-chart" viewBox="0 0 600 140" role="img" aria-label="策略权益曲线"><polyline points={points} fill="none" stroke="currentColor" strokeWidth="2.5" /></svg>
          <p>期末权益 {money(result.summary.final_equity_cny)} 元 · 持仓 {result.summary.final_shares} 股</p>
          <details open><summary>计算口径</summary>{result.warnings.map(w => <p className="grid-note" key={w}>{w}</p>)}</details>
          <details><summary>已运行参数</summary><dl className="grid-run-params">
            <dt>回测区间</dt><dd>{result.request.start} 至 {result.request.end}</dd>
            <dt>基准与范围</dt><dd>{money(result.initialization.anchor_price)} 元 · {money(result.request.parameters.lower_price)}–{money(result.request.parameters.upper_price)} 元</dd>
            <dt>每格间距</dt><dd>{result.request.parameters.spacing_mode === 'cny' ? `${result.request.parameters.spacing} 元` : `${result.request.parameters.spacing}%（${result.request.parameters.spacing_mode === 'percent' ? '相邻格线等比' : '固定基准价'}）`}</dd>
            <dt>每格委托</dt><dd>{result.request.parameters.sizing_mode === 'shares' ? `${result.request.parameters.order_shares} 股` : `${money(result.request.parameters.order_amount_cny)} 元`}</dd>
            <dt>委托价格</dt><dd>{result.request.parameters.price_mode === 'fixed_limit'
              ? `买入不高于 ${money(result.request.parameters.buy_limit!)} 元，卖出不低于 ${money(result.request.parameters.sell_limit!)} 元`
              : result.request.parameters.price_mode === 'grid_limit' ? `格线限价，让价 ${money(result.request.parameters.limit_offset_cny)} 元` : '下一交易日开盘价模拟'}</dd>
            <dt>初始 / 最小 / 最大持仓</dt><dd>{result.request.parameters.initial_shares} / {result.request.parameters.min_shares} / {result.request.parameters.max_shares} 股</dd>
            <dt>持仓市值上限</dt><dd>{result.request.parameters.max_position_cny == null ? '不单独限制' : `${money(result.request.parameters.max_position_cny)} 元`}</dd>
            <dt>初始总资金</dt><dd>{money(result.request.parameters.initial_cash_cny)} 元</dd>
          </dl></details>
          <h3>委托与成交</h3><div className="grid-table"><table><thead><tr><th>日期</th><th>方向</th><th>成交</th><th>价格</th><th>状态</th></tr></thead><tbody>
            {result.orders.map((order, i) => <tr key={i}><td>{order.date}</td><td>{order.initial ? '建仓' : order.side === 'buy' ? '买入' : '卖出'}</td><td>{order.filled_quantity} / {order.requested_quantity} 股</td><td>{order.price == null ? '—' : money(order.price)}</td><td>{labels[order.reason] ?? '未成交，已保留原持仓'}</td></tr>)}
          </tbody></table></div>
          <small className="grid-note">历史行情：东方财富 · {['disk', 'memory'].includes(result.provenance.cache_status) ? '历史缓存，原查询于 ' : '查询于 '}{result.provenance.retrieved_at}</small>
        </> : <div className="grid-empty"><h2>网格回测结果</h2><p>设置参数并运行后，在这里查看收益、持仓与每次委托的成交情况。</p></div>}
      </aside>
    </div>
  </main>
}
