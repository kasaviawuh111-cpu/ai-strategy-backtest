import { useEffect, useRef, useState } from 'react'
import { DEFAULT_STRATEGY_EXAMPLES, type StrategyExample } from '../shared/default-strategy-examples'

/** Repeated visual copies wrap seamlessly; each idea has one accessible button. */
export function StrategyExamples({ inputHasText, onChoose }: {
  inputHasText: boolean
  onChoose: (example: StrategyExample) => void
}) {
  const lanes = useRef<Array<HTMLDivElement | null>>([])
  const initialized = useRef(false)
  const positions = useRef([0, 0])
  const [touching, setTouching] = useState(false)
  const [hovered, setHovered] = useState(false)
  const [focused, setFocused] = useState(false)
  const [reducedMotion, setReducedMotion] = useState(() =>
    window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false)
  const paused = touching || inputHasText || hovered || focused || reducedMotion

  useEffect(() => {
    const media = window.matchMedia?.('(prefers-reduced-motion: reduce)')
    const update = () => setReducedMotion(media?.matches ?? false)
    media?.addEventListener?.('change', update)
    return () => media?.removeEventListener?.('change', update)
  }, [])

  useEffect(() => {
    if (paused || typeof requestAnimationFrame === 'undefined') return
    let frame = 0
    let previous = 0
    if (!initialized.current) {
      const second = lanes.current[1]
      if (second) second.scrollLeft = Math.min(72, second.scrollWidth - second.clientWidth)
      initialized.current = true
    }
    lanes.current.forEach((lane, index) => { positions.current[index] = lane?.scrollLeft ?? 0 })
    const step = (now: number) => {
      const elapsed = previous ? Math.min(now - previous, 50) / 1000 : 0
      previous = now
      lanes.current.forEach((lane, index) => {
        if (!lane) return
        const groups = lane.querySelectorAll<HTMLElement>('.home-example-group')
        const period = groups[1] && groups[0] ? groups[1].offsetLeft - groups[0].offsetLeft : 0
        if (period <= 0) return
        const position = ((positions.current[index] ?? 0) + elapsed * 10) % period
        positions.current[index] = position
        lane.scrollLeft = position
      })
      frame = requestAnimationFrame(step)
    }
    frame = requestAnimationFrame(step)
    return () => cancelAnimationFrame(frame)
  }, [paused])

  return <section className="home-examples" aria-label="策略示例" data-paused={paused}
    data-reduced-motion={reducedMotion}>
    <div className="home-example-lanes"
      onMouseEnter={() => setHovered(true)} onMouseLeave={() => setHovered(false)}
      onFocusCapture={event => {
        setFocused(true)
        if (event.target instanceof HTMLElement && typeof event.target.scrollIntoView === 'function') {
          event.target.scrollIntoView({ block: 'nearest', inline: 'nearest' })
        }
      }}
      onBlur={event => {
        if (!event.currentTarget.contains(event.relatedTarget)) setFocused(false)
      }}
      onTouchStart={() => setTouching(true)}
      onTouchEnd={() => setTouching(false)}
      onTouchCancel={() => setTouching(false)}
      onPointerDown={event => {
        if (event.pointerType === 'touch' || event.pointerType === 'pen') setTouching(true)
      }}
      onPointerUp={() => setTouching(false)}
      onPointerCancel={() => setTouching(false)}
      onPointerLeave={() => setTouching(false)}>
      {[0, 1].map(row => <div className="home-example-lane" key={row}
        ref={node => { lanes.current[row] = node }}>
        <div className="home-example-track">
          {[0, 1, 2].map(copy => <div className="home-example-group" key={copy} aria-hidden={copy > 0 || undefined}>
          {DEFAULT_STRATEGY_EXAMPLES.filter((_, index) => index % 2 === row).map(example => (
            <button type="button" className="home-example" key={example.utterance}
              tabIndex={copy > 0 ? -1 : undefined}
              aria-label={example.utterance} onClick={() => onChoose(example)}>
              <small>{example.category}</small><span>{example.utterance}</span>
            </button>
          ))}
          </div>)}
        </div>
      </div>)}
    </div>
  </section>
}
