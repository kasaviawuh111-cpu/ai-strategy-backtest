import { useLayoutEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import type { DialogueProgressEvent } from '../shared/api/client'

const stageLabels: Record<string, string> = {
  received: '正在理解你的想法', model: '正在分析', model_reasoning: '正在分析',
  model_output: '正在整理结果', validation: '正在核对策略', model_repair: '正在修正格式',
  web_search: '正在检索', stock_screening: '正在寻找股票',
  stock_strategy_pairing: '正在匹配方案', stock_data_enrichment: '继续补查数据',
  stock_data_retry: '继续补查数据', stock_data_enriched: '正在匹配方案',
  stock_strategy_pairs_ready: '方案已准备好',
}

/** A single optional process disclosure; never invent missing provider text. */
export function ModelReasoning({ events, active = false, fallbackStatus, progressLabel = '处理进度' }: {
  events: readonly DialogueProgressEvent[]
  active?: boolean
  fallbackStatus?: ReactNode
  progressLabel?: string
}) {
  const streams = events.filter((event) => Boolean(event.reasoning))
  const [open, setOpen] = useState(active)
  const previousActive = useRef(active)
  const body = useRef<HTMLDivElement>(null)
  const followTail = useRef(true)
  const text = streams.map((event) => event.reasoning).join('\n\n')
  const hasText = Boolean(text.trim())
  const waitingInBody = active && !hasText && open
  const details = events.filter(event => ![
    'model', 'model_reasoning', 'model_output', 'received', 'complete', 'strategy_direction',
  ].includes(event.stage)).slice(-4)
  const latest = events.filter(event => event.stage !== 'strategy_direction').at(-1)
  const status = active
    ? (latest ? stageLabels[latest.stage] ?? latest.message : fallbackStatus ?? '正在理解你的想法')
    : latest?.stage === 'failed' ? '未完成' : '已完成'
  useLayoutEffect(() => {
    if (previousActive.current !== active) {
      setOpen(active)
      followTail.current = true
      previousActive.current = active
    }
  }, [active])
  useLayoutEffect(() => {
    if (open && followTail.current && body.current) {
      body.current.scrollTop = body.current.scrollHeight
    }
  }, [text, open])
  if (!active && !events.length) return null
  return (
    <details className="model-reasoning" open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary><span>处理过程</span><span role={active && !waitingInBody ? 'status' : undefined}
        aria-label={active && !waitingInBody ? progressLabel : undefined}
        aria-live={active && !waitingInBody ? 'polite' : undefined}>
          {waitingInBody ? null : status}
        </span></summary>
      {(active && open) || hasText ? <div className="model-reasoning__body" ref={body} tabIndex={0}
        aria-label="处理过程内容"
        onScroll={(event) => {
          const box = event.currentTarget
          followTail.current = box.scrollHeight - box.scrollTop - box.clientHeight < 40
        }}>
        {streams.some((event) => event.reasoningTruncated)
          ? <p className="model-reasoning__hint">内容较长，仅显示最近返回的部分。</p> : null}
        <p role={waitingInBody ? 'status' : undefined}
          aria-label={waitingInBody ? progressLabel : undefined}
          aria-live={waitingInBody ? 'polite' : undefined}>
          {hasText ? text : <>{status}<span className="dots" aria-hidden="true"><i /><i /><i /></span></>}
        </p>
      </div> : null}
      {details.length ? <ul className="model-reasoning__events">
        {details.map((event, index) => <li key={`${event.stage}-${index}`}>{event.message}</li>)}
      </ul> : null}
    </details>
  )
}
