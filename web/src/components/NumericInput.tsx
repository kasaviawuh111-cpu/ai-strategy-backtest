import { createContext, useCallback, useContext, useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import './numeric-input.css'

/** Keep incomplete edits out of the executable draft without replacing what the user typed. */
export function NumericInput({ value, onValueChange, label, min, max, integer = false, disabled = false }: {
  value: number; onValueChange: (value: number) => void; label: string
  min?: number; max?: number; integer?: boolean; disabled?: boolean
}) {
  const { report: reportValidity, resetKey } = useContext(ValidityContext)
  const [edit, setEdit] = useState({ source: value, raw: String(value), touched: false, resetKey })
  if (edit.resetKey !== resetKey) {
    setEdit({ source: value, raw: String(value), touched: false, resetKey })
  } else if (edit.source !== value) {
    setEdit({ ...edit, source: value, raw: edit.raw.trim() !== '' && Number(edit.raw) === value ? edit.raw : String(value) })
  }
  const ref = useRef<HTMLInputElement>(null)
  const errorId = useId()
  const validate = (raw: string) => {
    if (raw.trim() === '') return '请填写数值'
    const next = Number(raw)
    if (!Number.isFinite(next)) return '请输入有效数字'
    if (integer && !Number.isInteger(next)) return '请输入整数'
    if (min !== undefined && next < min) return `不能小于 ${min}`
    if (max !== undefined && next > max) return `不能大于 ${max}`
    return ''
  }
  const error = validate(edit.raw)
  const visibleError = !disabled && edit.touched ? error : ''
  useLayoutEffect(() => {
    ref.current?.setCustomValidity(disabled ? '' : error)
  }, [disabled, error])
  useEffect(() => reportValidity(errorId, disabled || !error), [disabled, error, errorId, reportValidity])
  useEffect(() => () => reportValidity(errorId, true), [errorId, reportValidity])
  return <span className="numeric-field">
    <input ref={ref} className="settings-input" data-numeric-input type="number" inputMode={integer ? 'numeric' : 'decimal'}
      aria-label={label} value={edit.raw} min={min} max={max} step={integer ? 1 : 'any'} required disabled={disabled}
      aria-invalid={Boolean(visibleError)} aria-describedby={visibleError ? errorId : undefined}
      onChange={(event) => {
        const raw = event.currentTarget.value
        setEdit((current) => ({ ...current, raw, touched: false }))
        if (!validate(raw)) onValueChange(Number(raw))
      }}
      onBlur={() => setEdit((current) => ({ ...current, touched: true }))}
      onInvalid={(event) => {
        event.preventDefault()
        setEdit((current) => ({ ...current, touched: true }))
      }} />
    {visibleError ? <small id={errorId} className="numeric-field-error" role="alert">{visibleError}</small> : null}
  </span>
}
const ValidityContext = createContext<{ report: (id: string, valid: boolean) => void; resetKey?: string }>({ report: () => {} })

/** Optional status for adjacent save actions; completion still checks the mounted inputs. */
export function NumericInputGroup({ children, onValidityChange, resetKey }: {
  children: ReactNode; onValidityChange: (valid: boolean) => void; resetKey?: string
}) {
  const [invalid, setInvalid] = useState<Set<string>>(() => new Set())
  const report = useCallback((id: string, valid: boolean) => setInvalid((previous) => {
    if (previous.has(id) === !valid) return previous
    const next = new Set(previous)
    if (valid) next.delete(id)
    else next.add(id)
    return next
  }), [])
  const context = useMemo(() => ({ report, resetKey }), [report, resetKey])
  useEffect(() => onValidityChange(invalid.size === 0), [invalid.size, onValidityChange])
  return <ValidityContext.Provider value={context}>{children}</ValidityContext.Provider>
}
