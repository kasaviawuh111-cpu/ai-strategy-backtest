import { useEffect, useId, useRef, useState } from 'react'

import type { Instrument as ApiInstrument } from '../shared/api/types'
import type { Instrument } from '../types'
import '../styles/inline-stock-editor.css'

export type StockSearchResult = { items: ApiInstrument[]; hasMore: boolean; isStaleCache?: boolean }
export type InlineStockEditorActions = {
  onSearch: (query: string, signal: AbortSignal) => Promise<StockSearchResult>
  onSave: (instrument: ApiInstrument, signal: AbortSignal) => Promise<void>
}

const failureMessage = (error: unknown) => error instanceof Error
  ? error.message : '请求没有完成，请稍后重试。'

/** A provider-verified security is the only value this field can submit. */
export function InlineStockEditor({ instrument, disabled, onSearch, onSave, onEditingChange }: {
  instrument: Instrument
  disabled?: boolean
  onEditingChange: (editing: boolean) => void
} & InlineStockEditorActions) {
  const id = useId()
  const [editing, setEditing] = useState(false)
  const [query, setQuery] = useState(instrument.name)
  const [result, setResult] = useState<StockSearchResult>({ items: [], hasMore: false })
  const [phase, setPhase] = useState<'idle' | 'searching' | 'ready' | 'saving' | 'error'>('idle')
  const [error, setError] = useState<string>()
  const [activeIndex, setActiveIndex] = useState(0)
  const input = useRef<HTMLInputElement>(null)
  const entry = useRef<HTMLButtonElement>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const searchAbort = useRef<AbortController | undefined>(undefined)
  const saveAbort = useRef<AbortController | undefined>(undefined)
  const version = useRef(0)
  const composing = useRef(false)
  const saving = useRef(false)
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      version.current += 1
      window.clearTimeout(timer.current)
      searchAbort.current?.abort()
      saveAbort.current?.abort()
    }
  }, [])

  useEffect(() => {
    if (editing) {
      input.current?.focus({ preventScroll: true })
      input.current?.select()
    }
  }, [editing])

  useEffect(() => {
    document.getElementById(`${id}-option-${activeIndex}`)?.scrollIntoView?.({ block: 'nearest' })
  }, [activeIndex, id])

  const stopRequests = () => {
    version.current += 1
    window.clearTimeout(timer.current)
    searchAbort.current?.abort()
    saveAbort.current?.abort()
    saving.current = false
  }

  const close = () => {
    stopRequests()
    setEditing(false)
    setError(undefined)
    setPhase('idle')
    setQuery(instrument.name)
    onEditingChange(false)
    // The entry returns in the next render; never send focus to the composer.
    window.setTimeout(() => {
      if (mounted.current) entry.current?.focus({ preventScroll: true })
    }, 0)
  }

  const search = (value: string, immediately = false) => {
    version.current += 1
    const requestVersion = version.current
    window.clearTimeout(timer.current)
    searchAbort.current?.abort()
    setResult({ items: [], hasMore: false })
    setActiveIndex(0)
    setError(undefined)
    const text = value.trim()
    if (!text || composing.current || text === instrument.name || text === instrument.code) {
      setPhase('idle')
      return
    }
    setPhase('searching')
    const controller = new AbortController()
    searchAbort.current = controller
    timer.current = window.setTimeout(async () => {
      try {
        const next = await onSearch(text, controller.signal)
        if (!mounted.current || controller.signal.aborted || requestVersion !== version.current) return
        setResult(next)
        setPhase('ready')
      } catch (cause) {
        if (!mounted.current || controller.signal.aborted || requestVersion !== version.current) return
        setError(`核对股票失败：${failureMessage(cause)}`)
        setPhase('error')
      }
    }, immediately ? 0 : 250)
  }

  const save = async (candidate: ApiInstrument) => {
    if (disabled || saving.current || composing.current) return
    if (candidate.symbol === instrument.code) {
      close()
      return
    }
    stopRequests()
    const requestVersion = version.current
    const controller = new AbortController()
    saveAbort.current = controller
    saving.current = true
    setPhase('saving')
    setError(undefined)
    try {
      await onSave(candidate, controller.signal)
      if (!mounted.current || controller.signal.aborted || requestVersion !== version.current) return
      close()
    } catch (cause) {
      if (!mounted.current || controller.signal.aborted || requestVersion !== version.current) return
      saving.current = false
      setError(`保存股票失败：${failureMessage(cause)}`)
      setPhase('error')
      input.current?.focus({ preventScroll: true })
    }
  }

  if (!editing) return (
    <div className="inline-stock-field">
      <button ref={entry} type="button" className="inline-stock-entry"
        aria-label={`修改股票：${instrument.name}（${instrument.code}）`} disabled={disabled}
        onClick={() => {
          if (disabled) return
          stopRequests()
          setQuery(instrument.name)
          setResult({ items: [], hasMore: false })
          setPhase('idle')
          setEditing(true)
          onEditingChange(true)
        }}>
        <span className="nm">{instrument.name}</span><span className="cd">{instrument.code}</span>
        <svg className="inline-stock-icon" viewBox="0 0 16 16" aria-hidden="true">
          <path d="m10.5 2.5 3 3M3 10l7.7-7.7a1.4 1.4 0 0 1 2 0l1 1a1.4 1.4 0 0 1 0 2L6 13l-3.7.7L3 10Z" />
        </svg>
      </button>
    </div>
  )

  const isSaving = phase === 'saving'
  return (
    <div className="inline-stock-field is-editing">
      <div className="inline-stock-controls">
        <div className="inline-stock-input-wrap">
          <input ref={input} className="inline-stock-input" aria-label="股票名称或代码"
            role="combobox" aria-autocomplete="list" aria-controls={`${id}-options`}
            aria-expanded={result.items.length > 0} aria-describedby={`${id}-status`}
            aria-activedescendant={result.items[activeIndex] ? `${id}-option-${activeIndex}` : undefined}
            value={query} disabled={disabled} readOnly={isSaving} aria-busy={isSaving} maxLength={32}
            placeholder="股票名称、代码或拼音" autoComplete="off" spellCheck={false}
            onCompositionStart={() => {
              composing.current = true
              search(query)
            }}
            onCompositionEnd={(event) => {
              composing.current = false
              setQuery(event.currentTarget.value)
              search(event.currentTarget.value)
            }}
            onChange={(event) => {
              if (isSaving || disabled) return
              setQuery(event.target.value); search(event.target.value)
            }}
            onKeyDown={(event) => {
              if (event.nativeEvent.isComposing || composing.current || event.keyCode === 229) return
              if (event.key === 'Escape') { event.preventDefault(); close(); return }
              if (disabled || isSaving) return
              if ((event.key === 'ArrowDown' || event.key === 'ArrowUp') && result.items.length) {
                event.preventDefault()
                setActiveIndex((index) => (index + (event.key === 'ArrowDown' ? 1 : -1)
                  + result.items.length) % result.items.length)
              }
              if (event.key === 'Enter') {
                event.preventDefault()
                if (query.trim() === instrument.name || query.trim() === instrument.code) close()
                else if (result.items[activeIndex]) void save(result.items[activeIndex])
                else if (phase !== 'searching') search(query, true)
              }
            }} />
          {query ? <button type="button" className="inline-stock-clear" aria-label="清空股票输入"
            disabled={disabled || isSaving} onClick={() => {
              setQuery(''); search(''); input.current?.focus({ preventScroll: true })
            }}>
            <svg viewBox="0 0 16 16" aria-hidden="true"><path d="m4 4 8 8m0-8-8 8" /></svg>
          </button> : null}
        </div>
        <button type="button" className="inline-stock-cancel" onClick={close}>取消</button>
      </div>
      <div className="inline-stock-dropdown">
        <div className="inline-stock-status" id={`${id}-status`} role={error ? 'alert' : 'status'}>
          {error ?? (isSaving ? '正在保存股票，原策略将保留'
            : phase === 'searching' ? '正在查找 A 股'
            : phase === 'ready' && result.items.length === 0 ? '没有找到匹配的 A 股，请补充名称或代码'
            : result.items.length > 0 ? result.isStaleCache
              ? '显示最近缓存的匹配，请选择股票' : '选择要使用的股票'
            : !query.trim() ? '输入股票名称、代码或拼音'
            : '修改名称或代码后选择股票，Esc 取消')}
        </div>
        {result.items.length > 0 && !isSaving ? (
          <div className="inline-stock-options" id={`${id}-options`} role="listbox" aria-label="匹配的 A 股">
            {result.items.map((candidate, index) => (
              <button type="button" key={candidate.symbol} id={`${id}-option-${index}`}
                className="inline-stock-option" role="option" aria-selected={index === activeIndex}
                aria-label={`${candidate.name} ${candidate.symbol}`}
                disabled={disabled} tabIndex={-1}
                onMouseDown={(event) => event.preventDefault()}
                onMouseEnter={() => setActiveIndex(index)} onClick={() => void save(candidate)}>
                <span>{candidate.name}</span><span className="cd">{candidate.symbol}</span>
              </button>
            ))}
          </div>
        ) : null}
        {error && result.items.length === 0 ? <button type="button" className="inline-stock-retry"
          disabled={disabled} onClick={() => search(query, true)}>重新查找</button> : null}
        {result.hasMore && !isSaving ? <div className="inline-stock-foot">继续输入可缩小范围</div> : null}
      </div>
    </div>
  )
}
