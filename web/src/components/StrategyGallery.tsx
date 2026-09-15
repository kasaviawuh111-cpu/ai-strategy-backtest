import { Fragment, useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import type { Instrument } from '../shared/api/types'
import type { StrategyExample } from '../shared/default-strategy-examples'
import { GALLERY_STRATEGIES, STRATEGY_CATEGORIES, curatedStrategyExample, type CuratedStrategy } from '../shared/curated-strategies'
import type { StockSearchResult } from './InlineStockEditor'
import type { CompletedReportSnapshot } from '../shared/recent-backtests'
import { fetchGallerySamples } from '../shared/strategy-gallery-samples'
import { StrategyPerformance } from './StrategyPerformance'
import '../styles/strategy-gallery.css'

function Arrow({ back = false }: { back?: boolean }) {
  return <svg width="18" height="18" viewBox="0 0 18 18" fill="none" aria-hidden="true">
    <path d={back ? 'm10 4-5 5 5 5M5 9h10' : 'm7 4 5 5-5 5'} stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
}

function Tags({ strategy }: { strategy: CuratedStrategy }) {
  return <span className="sg-tags"><span>{strategy.category}</span>
    {strategy.holding && <span>需要已有持仓</span>}</span>
}

function Rules({ strategy }: { strategy: CuratedStrategy }) {
  return <span className="sg-rules">
    <span className="sg-rule sg-rule--buy"><span>买入</span><span>{strategy.buy}</span></span>
    <span className="sg-rule sg-rule--sell"><span>卖出</span><span>{strategy.sell}</span></span>
  </span>
}

/** A workspace panel, not an iframe or a second app. Browsing never creates a draft. */
export function StrategyGallery({ active, entryKey = 0, disabled, onUse, onSearch, onOpenReport }: {
  active: boolean
  entryKey?: number
  disabled: boolean
  onUse: (example: StrategyExample) => void
  onSearch: (query: string, signal: AbortSignal) => Promise<StockSearchResult>
  onOpenReport: (snapshot: CompletedReportSnapshot) => void
}) {
  const samples = useQuery({ queryKey: ['strategy-gallery-samples'], queryFn: ({ signal }) => fetchGallerySamples(signal),
    enabled: active, staleTime: 60_000, retry: false, refetchOnWindowFocus: false })
  const [category, setCategory] = useState<string>('全部')
  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState<CuratedStrategy>()
  const [settingUp, setSettingUp] = useState(false)
  const [stockText, setStockText] = useState('')
  const [stock, setStock] = useState<Instrument>()
  const [holding, setHolding] = useState('')
  const [stockResults, setStockResults] = useState<StockSearchResult>()
  const [searching, setSearching] = useState(false)
  const [composing, setComposing] = useState(false)
  const [searchRetry, setSearchRetry] = useState(0)
  const [stockError, setStockError] = useState('')
  const [holdingError, setHoldingError] = useState('')
  const root = useRef<HTMLDivElement>(null)
  const heading = useRef<HTMLHeadingElement>(null)
  const lastCard = useRef<HTMLButtonElement | null>(null)
  const listScroll = useRef(0)
  const searchAbort = useRef<AbortController | null>(null)
  const searchProvider = useRef(onSearch)
  searchProvider.current = onSearch

  useEffect(() => () => searchAbort.current?.abort(), [])
  useEffect(() => {
    // Workspace navigation returns to the library, not an abandoned detail/form.
    // Keep library filters and the parent-owned conversation independent.
    searchAbort.current?.abort()
    searchAbort.current = null
    setSelected(undefined)
    setSettingUp(false)
    setStock(undefined)
    setStockText('')
    setStockResults(undefined)
    setHolding('')
    setStockError('')
    setHoldingError('')
    setSearching(false)
    setComposing(false)
    if (root.current) root.current.scrollTop = 0
  }, [active, entryKey])
  useEffect(() => {
    if (active) heading.current?.focus({ preventScroll: true })
  }, [active, selected, settingUp])

  const stopSearch = () => {
    searchAbort.current?.abort()
    searchAbort.current = null
    setSearching(false)
  }
  const open = (strategy: CuratedStrategy, trigger: HTMLButtonElement) => {
    lastCard.current = trigger
    listScroll.current = root.current?.scrollTop ?? 0
    setSelected(strategy)
    setSettingUp(false)
    setStock(undefined)
    setStockText('')
    setHolding('')
    setStockResults(undefined)
    setStockError('')
    setHoldingError('')
    if (root.current) root.current.scrollTop = 0
  }
  const back = () => {
    stopSearch()
    if (settingUp) setSettingUp(false)
    else {
      setSelected(undefined)
      requestAnimationFrame(() => {
        if (root.current) root.current.scrollTop = listScroll.current
        lastCard.current?.focus({ preventScroll: true })
      })
    }
  }
  useEffect(() => {
    if (!active || !settingUp || disabled || composing || stock || !stockText.trim()) return
    const controller = new AbortController()
    searchAbort.current = controller
    setSearching(true)
    const timer = window.setTimeout(async () => {
      try {
        const result = await searchProvider.current(stockText.trim(), controller.signal)
        if (controller.signal.aborted) return
        setStockResults(result)
        if (!result.items.length) setStockError('没有匹配的股票，请补充名称或代码。')
      } catch (error) {
        if (!controller.signal.aborted) setStockError(error instanceof Error ? error.message : '股票联想暂不可用，请重试。')
      } finally {
        if (searchAbort.current === controller) setSearching(false)
      }
    }, 300)
    return () => { window.clearTimeout(timer); controller.abort() }
  }, [active, settingUp, disabled, composing, stock, stockText, searchRetry])
  const validateHolding = () => {
    if (!selected?.holding) return true
    const minimum = 1
    const valid = /^\d+$/.test(holding.trim()) && Number.isSafeInteger(Number(holding)) && Number(holding) >= minimum
    setHoldingError(valid ? '' : '请填写大于0的整数持仓数量。')
    return valid
  }
  const normalizedQuery = query.trim().toLocaleLowerCase()
  const rows = GALLERY_STRATEGIES.filter(strategy => (category === '全部' || strategy.category === category)
    && [strategy.name, strategy.category, strategy.buy, strategy.sell, ...strategy.params].join(' ').toLocaleLowerCase().includes(normalizedQuery))
  const selectedSample = samples.data?.find(item => item.strategyId === selected?.id)
  const selectedUnavailable = selectedSample?.executionAvailable === false
  const stockOptionsVisible = !stock && Boolean(stockResults?.items.length)

  return <div ref={root} className="strategy-gallery" hidden={!active} aria-label="精选策略库">
    <div className="sg-inner">
      <section hidden={Boolean(selected)} aria-label="精选策略列表">
        <header className="sg-heading"><div><h1 ref={!selected ? heading : undefined} tabIndex={-1}>精选策略</h1>
          <p>看看历史表现，再把策略用在你的想法上。</p></div>
          <div className="sg-search"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5" stroke="currentColor" strokeWidth="1.5" /><path d="m16 16 4.5 4.5" stroke="currentColor" strokeWidth="1.5" /></svg>
            <input aria-label="搜索精选策略" type="search" value={query} maxLength={200} placeholder="搜索策略、指标或交易规则" onChange={event => setQuery(event.target.value)} />
          </div></header>
        <nav className="sg-filters" aria-label="策略分类">{STRATEGY_CATEGORIES.map(item =>
          <button key={item} type="button" aria-pressed={category === item} onClick={() => setCategory(item)}>{item}</button>)}</nav>
        <div className="sg-count"><span role="status" aria-live="polite">{rows.length === GALLERY_STRATEGIES.length ? `${GALLERY_STRATEGIES.length} 个策略 · 首批精选` : `${rows.length} 个匹配策略`}</span><span>样例表现不代表更换标的后的收益</span></div>
        {samples.isError && <div className="sg-load-error" role="status">样例回测暂时无法读取，策略规则仍可查看。
          <button type="button" onClick={() => void samples.refetch()}>重新加载</button></div>}
        <div className="sg-grid">{rows.map(strategy => {
          const sample = samples.data?.find(item => item.strategyId === strategy.id)
          return <article key={strategy.id} className="sg-card" aria-label={strategy.name}>
            <div className="sg-card-top"><button className="sg-title-link" type="button" aria-label={`查看${strategy.name}策略`}
              onClick={event => open(strategy, event.currentTarget)}><h2 className="sg-card-title">{strategy.name}</h2></button><Tags strategy={strategy} /></div>
            <p className="sg-sample-instrument">{sample?.instrument ? <>{sample.instrument.name}<span>{sample.instrument.symbol}</span></> : '样例标的待确认'}</p>
            <StrategyPerformance sample={sample} loading={samples.isFetching && !samples.data} />
            <div className="sg-card-actions">
              <button className="sg-report-link" type="button" onClick={event => {
                if (sample?.snapshot) { listScroll.current = root.current?.scrollTop ?? 0; lastCard.current = event.currentTarget; onOpenReport(sample.snapshot) }
                else open(strategy, event.currentTarget)
              }} aria-label={sample?.snapshot ? `查看${strategy.name}回测` : `查看${strategy.name}规则`}>
                {sample?.snapshot ? '查看回测' : '查看规则'}<Arrow /></button>
              <button className="sg-primary" type="button" disabled={disabled || samples.isPending || sample?.executionAvailable === false} aria-label={`使用${strategy.name}策略`}
                onClick={event => { open(strategy, event.currentTarget); setSettingUp(true) }}>{sample?.executionAvailable === false ? '暂不可回测' : '使用策略'}</button>
            </div>
          </article>
        })}</div>
        {rows.length === 0 && <div className="sg-empty"><h2>暂时没有匹配的策略</h2><p>可以换个关键词，或看看全部{GALLERY_STRATEGIES.length}个策略。</p>
          <button className="sg-secondary" type="button" onClick={() => { setQuery(''); setCategory('全部') }}>查看全部策略</button></div>}
        <p className="sg-footnote">历史回测不代表未来表现。样例的仓位与分红处理不同，不作为收益排名；具体口径见回测报告。更换股票或参数后需重新回测。</p>
      </section>
      {selected && <section aria-label={settingUp ? '使用策略' : '策略详情'}>
        <button className="sg-back" type="button" onClick={back}><Arrow back />{settingUp ? '返回策略详情' : '返回精选策略'}</button>
        <header className={`sg-detail-head${settingUp ? '' : ' sg-detail-head--overview'}`}><div><h1 ref={heading} tabIndex={-1}>{settingUp ? '选择回测股票' : selected.name}</h1>
          {settingUp ? <p>选好股票，生成后即可查看和调整策略。</p> : <Tags strategy={selected} />}</div>
          {!settingUp && <button className="sg-primary sg-detail-use" type="button" disabled={disabled || selectedUnavailable}
            onClick={() => { setSettingUp(true); if (root.current) root.current.scrollTop = 0 }}>{selectedUnavailable ? '暂不可回测' : '用这个策略'}<Arrow /></button>}
        </header>
        {settingUp ? <form className="sg-setup" onSubmit={event => {
          event.preventDefault()
          const holdingValid = validateHolding()
          if (!stock) { setStockError('请从联想结果中选择要使用的股票。'); return }
          if (disabled || selectedUnavailable || !holdingValid) return
          const example = curatedStrategyExample(selected, stock, holding.trim())
          const plan = selectedSample?.snapshot?.draft.strategySpec?.trading_plan
          if (plan && plan.kind !== 'scheduled' && plan.parameters.observation === 'daily_close')
            example.utterance += '按日线收盘观察价格条件，触发后下一交易日开盘起模拟委托，不使用盘中或分钟触发。'
          onUse(example)
          setSettingUp(false)
          setSelected(undefined)
          setStock(undefined)
          setStockText('')
          setHolding('')
          setStockResults(undefined)
          setStockError('')
          setHoldingError('')
          if (root.current) root.current.scrollTop = listScroll.current
        }}>
          <section className="sg-selected-rule"><h2>{selected.name}</h2><Rules strategy={selected} /></section>
          {selectedSample?.snapshot?.draft.strategySpec?.trading_plan && <p className="sg-help">{selectedSample.snapshot.draft.strategySpec.trading_plan.parameters.observation === 'minute_bar'
            ? '本样例使用分钟行情观察价格条件，触发后模拟委托。'
            : '本样例按日线收盘观察，带入时会保留这一方式，不作为盘中或分钟回测使用。'}</p>}
          <div className="sg-field"><label htmlFor="gallery-stock">回测标的</label>
            <div className={`sg-stock-picker${stockOptionsVisible ? ' is-open' : ''}`}>
              <div className="sg-stock-input"><input id="gallery-stock" placeholder="输入股票名称或代码" value={stockText} maxLength={80} disabled={disabled}
              aria-describedby="gallery-stock-status" onChange={event => { stopSearch(); setStockText(event.target.value); setStock(undefined); setStockResults(undefined); setStockError('') }}
              onCompositionStart={() => { stopSearch(); setComposing(true) }} onCompositionEnd={() => setComposing(false)}
              onKeyDown={event => { if (event.key === 'Enter') event.preventDefault() }} /></div>
              {stockOptionsVisible && <div className="sg-stock-options" aria-label="股票匹配结果">
                {stockResults?.items.map(item => <button className="sg-stock-option" key={item.symbol} type="button" disabled={disabled} onClick={() => { stopSearch(); setStock(item); setStockText(item.name); setStockError('') }}><span>{item.name}</span><span>{item.symbol}</span></button>)}
              </div>}
            </div>
            <div id="gallery-stock-status" className="sg-help" role="status">{stock ? `已选：${stock.name} ${stock.symbol}` : searching ? '正在查找…' : ''}</div>
            {stockError && <p className="sg-error" role="alert">{stockError}{!stockResults && stockText.trim() && <button type="button" className="sg-secondary" disabled={searching || disabled} onClick={() => { setStockError(''); setSearchRetry(value => value + 1) }}>重新匹配</button>}</p>}
          </div>
          {selected.holding && <div className="sg-field"><label htmlFor="gallery-holding">初始可卖持仓（股）</label>
            <input id="gallery-holding" type="text" inputMode="numeric" value={holding} onChange={event => setHolding(event.target.value)}
              onBlur={validateHolding} aria-invalid={Boolean(holdingError)} aria-describedby="gallery-holding-error" placeholder="请输入已有持仓数量" />
            <p className="sg-help">这是已有持仓的退出规则，不会自动买入。</p>
            <p id="gallery-holding-error" className="sg-error" role={holdingError ? 'alert' : undefined}>{holdingError}</p></div>}
          <p className="sg-help">下一步查看策略，确认后再开始回测。</p>
          <button className="sg-primary" type="submit" disabled={disabled || searching || !stock || selectedUnavailable}>生成策略<Arrow /></button>
        </form> : <>
          {selectedSample?.snapshot && <section className="sg-detail-performance">
            <StrategyPerformance sample={selectedSample} />
            <button className="sg-secondary" type="button" onClick={() => {
              if (selectedSample.snapshot) onOpenReport(selectedSample.snapshot)
            }}>查看完整回测<Arrow /></button>
          </section>}
          {selectedUnavailable && <p className="sg-unavailable-note" role="status">{selectedSample?.message}</p>}
          <div className="sg-detail-layout"><div>
            <section className="sg-detail-rule sg-detail-rule--buy"><h2>买入规则</h2><p>{selected.buy}</p></section>
            <section className="sg-detail-rule sg-detail-rule--sell"><h2>卖出规则</h2><p>{selected.sell}</p></section>
            <section className="sg-section"><h2>规则参数</h2><div className="sg-parameters">{selected.params.map(item => <span key={item}>{item}</span>)}</div></section>
            <section className="sg-section"><h2>执行顺序</h2><div className="sg-sequence">{selected.steps.map((item, index) => <Fragment key={item}>{index > 0 && <Arrow />}<span>{item}</span></Fragment>)}</div></section>
            <section className="sg-section"><h2>理解这条规则</h2><p>{selected.note}</p></section>
          </div></div>
          <p className="sg-detail-note">{selectedSample?.assumptions.join('；')} 条件类型不代表日内执行能力。样例表现仅对应报告中的股票、区间和设置，更换后需重新回测。</p>
          <div className="sg-mobile-action"><span>{selectedUnavailable ? '规则可查看，回测暂未接通' : <>选择标的后<br />继续检查设置</>}</span><button className="sg-primary" type="button" disabled={disabled || selectedUnavailable}
            onClick={() => { setSettingUp(true); if (root.current) root.current.scrollTop = 0 }}>{selectedUnavailable ? '暂不可回测' : '用这个策略'}<Arrow /></button></div>
        </>}
      </section>}
    </div>
  </div>
}
