import type { ReactNode } from 'react'
import type { CapabilitiesResponse, StrategyDraft, StrategySpecCondition, StrategySpecExitRule } from '../shared/api/types'
import { strategySpecFromDraft, withEditedStrategySpec, triggerFallback } from '../shared/api/contract'

type Node = StrategySpecExitRule

export function RuleSpecEditor({ draft, side, path, capabilities, onChange }: {
  draft: StrategyDraft; side: 'entry' | 'exit'; path?: string; capabilities?: CapabilitiesResponse
  onChange: (draft: StrategyDraft) => void
}) {
  const spec = strategySpecFromDraft(draft)
  const commit = (next: typeof spec) => onChange(withEditedStrategySpec(draft, next, capabilities))
  const field = (label: string, value: number, update: (value: number) => void, min?: number, max?: number, step: number | 'any' = 'any') =>
    <label className="setting-row grow"><span className="k">{label}</span>
      <input className="settings-input" type="number" aria-label={label} value={value} min={min} max={max}
        step={step} onChange={(event) => {
          if (event.currentTarget.value === '' || !event.currentTarget.validity.valid) return
          update(event.currentTarget.valueAsNumber)
        }} /></label>
  const renderNode = (node: Node, id: string, replace: (node: Node) => void): ReactNode => {
    if (path && path !== side && id !== path && !id.startsWith(`${path}-`) && !path.startsWith(`${id}-`)) return null
    if (node.type === 'all' || node.type === 'any') return <div key={id}>
      <label className="setting-row"><span>条件关系</span><select aria-label={`${id}条件关系`} value={node.type}
        onChange={(event) => replace({ ...node, type: event.target.value as 'all' | 'any' })}>
        <option value="all">全部满足（且）</option><option value="any">任一满足（或）</option>
      </select></label>
      {node.children.map((child, i) => renderNode(child, `${id}-${i}`, (next) => replace({ ...node, children: node.children.map((item, index) => index === i ? next as StrategySpecCondition : item) })))}
    </div>
    if (node.type === 'not') return <div key={id}><p>不满足以下条件</p>{renderNode(node.child, `${id}-not`, (next) => replace({ ...node, child: next as StrategySpecCondition }))}</div>
    const ui = draft[side].conditions.find((item) => item.id === id)
    return <section className="rule-edit-condition" key={id}>
      <h3>{ui?.label ?? '编辑条件'}</h3>
      {node.type === 'indicator_condition' ? <>
        <label className="setting-row"><span>指标</span><select aria-label={`${id}指标`} value={node.indicator_id}
          disabled={!capabilities} onChange={(event) => {
            const item = capabilities?.indicators.find((candidate) => candidate.indicator_id === event.target.value)
            if (!item) return
            const trigger = item.triggers.includes(node.trigger) ? node.trigger : item.triggers[0]
            if (!trigger) return
            const definition = item.trigger_definitions.find((candidate) => candidate.id === trigger)
            replace({ ...node, indicator_id: item.indicator_id, definition_version: item.definition_version,
              params: Object.fromEntries(item.parameters.filter((parameter) => parameter.default !== null)
                .map((parameter) => [parameter.name, parameter.default as string | number | boolean])),
              trigger, value: definition?.value_requirement === 'forbidden' ? null : definition?.minimum ?? 0 })
          }}>
          {(capabilities?.indicators.filter((item) => item.status !== 'unavailable') ?? [])
            .map((item) => <option key={item.indicator_id} value={item.indicator_id}>{item.display_name}</option>)}
          {!capabilities ? <option value={node.indicator_id}>{ui?.kind === 'indicator' ? ui.indicatorId : node.indicator_id}</option> : null}
        </select></label>
        <label className="setting-row"><span>触发方式</span><select aria-label={`${id}触发方式`} value={node.trigger}
          onChange={(event) => {
            const definition = capabilities?.indicators.find((item) => item.indicator_id === node.indicator_id)?.trigger_definitions.find((item) => item.id === event.target.value)
            replace({ ...node, trigger: event.target.value, value: definition?.value_requirement === 'forbidden' ? null : node.value ?? definition?.minimum ?? 0 })
          }}>
          {(capabilities?.indicators.find((item) => item.indicator_id === node.indicator_id)?.triggers ?? [node.trigger])
            .map((trigger) => <option value={trigger} key={trigger}>{capabilities?.indicators.find((item) => item.indicator_id === node.indicator_id)?.trigger_definitions.find((item) => item.id === trigger)?.display_name ?? triggerFallback(trigger)}</option>)}
        </select></label>
        {Object.entries(node.params).filter(([,value]) => typeof value === 'number').map(([key, value]) => {
          const parameter = ui?.kind === 'indicator' ? ui.parameters.find((item) => item.key === key) : undefined
          return <div key={key}>{field(parameter?.label ?? key, value as number, (next) => replace({ ...node, params: { ...node.params, [key]: next } }), parameter?.min, parameter?.max, parameter?.integer ? 1 : 'any')}</div>
        })}
        {node.value !== null ? field('阈值', node.value, (next) => replace({ ...node, value: next })) : null}
      </> : node.type === 'holding_period_exit' ? field('持有交易日', node.sessions, (next) => replace({ ...node, sessions: next }), 1, 10000, 1)
        : node.type === 'position_return_exit' || node.type === 'trailing_drawdown_exit' ? field('幅度（%）', node.threshold_pct, (next) => replace({ ...node, threshold_pct: next }), 0.01, 100)
        : node.type === 'financial_condition' ? <>
          <label className="setting-row"><span>比较方式</span><select aria-label={`${id}财务比较方式`} value={node.comparator}
            onChange={(event) => replace({ ...node, comparator: event.target.value as typeof node.comparator })}>
            {Object.entries({ gt: '高于', gte: '不低于', lt: '低于', lte: '不高于', eq: '等于', ne: '不等于' })
              .map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select></label>
          {typeof node.value === 'number' ? field(`财务阈值（${node.unit}）`, node.value, (value) => replace({ ...node, value }))
            : <label className="setting-row"><span>比较值</span><input aria-label={`${id}财务比较值`} value={node.value}
              onChange={(event) => replace({ ...node, value: event.target.value })} /></label>}
          <p className="settings-help">沿用本条财务指标的报告期、单位和当时可知口径。</p>
        </> : node.type === 'event_condition' ? <>
          {Object.entries(node.attributes).map(([key, value]) => <div key={key}>
            {typeof value === 'number' ? field(`事件参数 ${key}`, value, (next) => replace({ ...node, attributes: { ...node.attributes, [key]: next } }))
              : typeof value === 'boolean' ? <label className="setting-row"><span>{key}</span><input type="checkbox" aria-label={`事件参数 ${key}`} checked={value}
                onChange={(event) => replace({ ...node, attributes: { ...node.attributes, [key]: event.target.checked } })} /></label>
              : <label className="setting-row"><span>{key}</span><input aria-label={`事件参数 ${key}`} value={value}
                onChange={(event) => replace({ ...node, attributes: { ...node.attributes, [key]: event.target.value } })} /></label>}
          </div>)}
          {node.document_text ? <>
            <label className="setting-row"><span>公告关键词</span><input aria-label="公告关键词" value={node.document_text.term}
              onChange={(event) => replace({ ...node, document_text: { ...node.document_text!, term: event.target.value } })} /></label>
            {field('关键词出现次数', node.document_text.value, (value) => replace({ ...node, document_text: { ...node.document_text!, value } }), 0, undefined, 1)}
          </> : null}
          {!Object.keys(node.attributes).length && !node.document_text ? <p className="settings-help">这条条件以事件发布为触发，没有额外参数。</p> : null}
        </> : null}
    </section>
  }
  return <div>
    {side === 'entry' ? renderNode(spec.entry, 'entry', (node) => commit({ ...spec, entry: node as StrategySpecCondition })) : <>
      <label className="setting-row"><span>卖出条件关系</span><select aria-label="卖出条件关系"
        value={spec.exit.op === 'all' || (spec.exit.children.length === 1 && spec.exit.children[0].type === 'all') ? 'all' : 'any'}
        disabled={spec.exit.children.length === 1 && !['all', 'any'].includes(spec.exit.children[0].type)}
        onChange={(event) => {
          const children = spec.exit.children.length === 1 && ['all', 'any'].includes(spec.exit.children[0].type)
            ? (spec.exit.children[0] as { children: StrategySpecCondition[] }).children
            : spec.exit.children as StrategySpecCondition[]
          commit({ ...spec, exit: { op: event.target.value === 'all' ? 'all' : 'first_of', children } })
        }}><option value="any">任一触发（或）</option><option value="all">全部满足（且）</option></select></label>
      {spec.exit.op === 'all' ? <p className="settings-help">每天收盘确认所有条件同时满足，下一交易日尝试卖出。持有期满后继续等待其他条件，不会到期就单独卖出。</p> : null}
      {spec.exit.children.map((node, i) => renderNode(node, `exit-${i}`, (next) => commit({ ...spec, exit: { ...spec.exit, children: spec.exit.children.map((item, index) => index === i ? next : item) } })))}
    </>}
  </div>
}
