import { useEffect, useRef, useState } from 'react'

import type { StrategyDraft } from '../shared/api/types'
import { toStrategySummary } from '../view-model'

type Slot = 'stock' | 'entry' | 'exit'
const labels: Record<Slot, string> = { stock: '股票', entry: '买入', exit: '卖出' }

export function StrategySlotComposer({ draft, mode, disabled, onSubmit, onClear }: {
  draft: StrategyDraft
  mode: 'stock' | 'rules'
  disabled: boolean
  onSubmit: (text: string, options: { editCurrentStrategy: boolean; rerun: boolean }) => void
  onClear: () => void
}) {
  // Seed from the executed draft, not the first utterance before clarification.
  const [original] = useState(() => {
    const rows = toStrategySummary(draft).rows
    const { name, symbol } = draft.instrument
    return {
      stock: name === symbol ? symbol : `${name}（${symbol}）`,
      entry: rows.find((row) => row.key === 'entry')?.value ?? '',
      exit: rows.find((row) => row.key === 'exit')?.value ?? '',
    }
  })
  const [values, setValues] = useState(original)
  const controls = useRef<Partial<Record<Slot, HTMLTextAreaElement | null>>>({})
  const slots: Slot[] = ['stock', 'entry', 'exit']
  const changes = slots.filter((slot) => values[slot].trim() !== original[slot])
  const canSubmit = changes.length > 0 && slots.every((slot) => values[slot].trim())

  useEffect(() => {
    const control = controls.current[mode === 'stock' ? 'stock' : 'entry']
    control?.focus()
    control?.select()
  }, [mode])

  useEffect(() => {
    const resize = () => Object.values(controls.current).forEach((control) => {
      if (!control) return
      control.style.height = 'auto'
      control.style.height = `${control.scrollHeight + 2}px`
    })
    resize()
    window.addEventListener('resize', resize)
    return () => window.removeEventListener('resize', resize)
  }, [values])

  const submit = () => {
    if (!canSubmit || disabled) return
    // Only send the user's edits. The saved parent draft retains untouched rules,
    // dates and execution settings without asking the model to reconstruct them.
    const text = changes.map((slot) => slot === 'stock'
      ? `股票换成${values.stock.trim()}`
      : `${labels[slot]}条件改为：${values[slot].trim()}`).join('。')
    onSubmit(`${text}。其他条件和回测设置保持不变，按新条件重新回测。`, {
      // Stock changes are edits too: the server binds the verified identity
      // to this saved strategy instead of treating the text as stock discovery.
      editCurrentStrategy: true, rerun: true,
    })
  }

  return (
    <form className="strategy-slots" aria-label="编辑回测条件"
      onSubmit={(event) => { event.preventDefault(); submit() }}>
      <div className="strategy-slots__fields">
        {slots.map((slot) => (
          <div className="strategy-slot" key={slot}>
            <label htmlFor={`strategy-slot-${slot}`}>{labels[slot]}</label>
            <textarea id={`strategy-slot-${slot}`} rows={1}
              ref={(element) => { controls.current[slot] = element }}
              value={values[slot]} disabled={disabled}
              placeholder={slot === 'stock' ? '填写股票名称或代码' : `填写${labels[slot]}条件`}
              onChange={(event) => setValues((current) => ({ ...current, [slot]: event.target.value }))}
              onFocus={(event) => event.currentTarget.select()}
              onClick={(event) => event.currentTarget.select()}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                  event.preventDefault()
                  submit()
                }
              }} />
          </div>
        ))}
      </div>
      <div className="strategy-slots__footer">
        <span>点选整段，直接替换</span>
        <button type="button" className="strategy-slots__clear" disabled={disabled} onClick={onClear}>清空</button>
        <button type="submit" className="send" aria-label="识别交易规则" disabled={disabled || !canSubmit}>
          <svg width="18" height="18" viewBox="0 0 18 18" fill="none" aria-hidden="true">
            <path d="M9 14V4M4.6 8.4 9 4l4.4 4.4" stroke="currentColor" strokeWidth="1.8"
              strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </button>
      </div>
    </form>
  )
}
