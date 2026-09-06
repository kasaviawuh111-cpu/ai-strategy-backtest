import type { ReactNode } from 'react'
import type { CapabilitiesResponse, StrategyDraft, StrategySpecCondition, StrategySpecExitRule } from '../shared/api/types'
import { strategySpecFromDraft, withEditedStrategySpec, triggerFallback } from '../shared/api/contract'

type Node = StrategySpecExitRule
const isCondition = (node: Node): node is StrategySpecCondition =>
  !['holding_period_exit', 'position_return_exit', 'trailing_drawdown_exit'].includes(node.type)

export function RuleSpecEditor({ draft, side, path, capabilities, onChange }: {
  draft: StrategyDraft; side: 'entry' | 'exit'; path?: string; capabilities?: CapabilitiesResponse
  onChange: (draft: StrategyDraft) => void
}) {
  const spec = strategySpecFromDraft(draft)
  const commit = (next: typeof spec) => onChange(withEditedStrategySpec(draft, next, capabilities))
  const field = (label: string, value: number, update: (value: number) => void, min?: number, max?: number) =>
    <label className="setting-row grow"><span className="k">{label}</span>
      <input className="settings-input" type="number" aria-label={label} value={value} min={min} max={max}
        step="any" onChange={(event) => {
          if (event.currentTarget.value === '' || !event.currentTarget.validity.valid) return
          update(event.currentTarget.valueAsNumber)
        }} /></label>
  const renderNode = (node: Node, id: string, replace: (node: Node) => void): ReactNode => {
    if (node.type === 'all' || node.type === 'any') return <div key={id}>
      <label className="setting-row"><span>条件关系</span><select aria-label={`${id}条件关系`} value={node.type}
        onChange={(event) => replace({ ...node, type: event.target.value as 'all' | 'any' })}>
        <option value="all">全部满足（且）</option><option value="any">任一满足（或）</option>
      </select></label>
      {node.children.map((child, i) => renderNode(child, `${id}-${i}`, (next) => replace({ ...node, children: node.children.map((item, index) => index === i ? next as StrategySpecCondition : item) })))}
    </div>
    if (node.type === 'not') return <div key={id}><p>不满足以下条件</p>{renderNode(node.child, `${id}-not`, (next) => replace({ ...node, child: next as StrategySpecCondition }))}</div>
    if (path && path !== side && !id.startsWith(path)) return null
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
          return <div key={key}>{field(parameter?.label ?? key, value as number, (next) => replace({ ...node, params: { ...node.params, [key]: next } }), parameter?.min, parameter?.max)}</div>
        })}
        {node.value !== null ? field('阈值', node.value, (next) => replace({ ...node, value: next })) : null}
      </> : node.type === 'holding_period_exit' ? field('持有交易日', node.sessions, (next) => replace({ ...node, sessions: next }), 1, 10000)
        : node.type === 'position_return_exit' || node.type === 'trailing_drawdown_exit' ? field('幅度（%）', node.threshold_pct, (next) => replace({ ...node, threshold_pct: next }), 0.01, 100)
        : <p>这类条件暂不支持参数面板编辑，请返回对话修改条件。</p>}
    </section>
  }
  return <div>
    {side === 'entry' ? renderNode(spec.entry, 'entry', (node) => commit({ ...spec, entry: node as StrategySpecCondition })) : <>
      <label className="setting-row"><span>卖出条件关系</span><select aria-label="卖出条件关系"
        value={spec.exit.children.length === 1 && spec.exit.children[0].type === 'all' ? 'all' : 'any'}
        disabled={!spec.exit.children.every(isCondition)}
        onChange={(event) => {
          const children = spec.exit.children.length === 1 && ['all', 'any'].includes(spec.exit.children[0].type)
            ? (spec.exit.children[0] as { children: StrategySpecCondition[] }).children
            : spec.exit.children as StrategySpecCondition[]
          commit({ ...spec, exit: { ...spec.exit, children: [{ type: event.target.value as 'all' | 'any', children }] } })
        }}><option value="any">任一触发（或）</option><option value="all">全部满足（且）</option></select></label>
      {!spec.exit.children.every(isCondition) ? <p className="settings-help">含止损、回撤或持有期的退出规则，当前引擎仅支持任一先触发；参数仍可逐条修改。</p> : null}
      {spec.exit.children.map((node, i) => renderNode(node, `exit-${i}`, (next) => commit({ ...spec, exit: { ...spec.exit, children: spec.exit.children.map((item, index) => index === i ? next : item) } })))}
    </>}
  </div>
}
