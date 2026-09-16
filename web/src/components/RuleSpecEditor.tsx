import type { ReactNode } from 'react'
import type { CapabilitiesResponse, StrategyDraft, StrategySpecCondition, StrategySpecExitRule } from '../shared/api/types'
import { strategySpecFromDraft, withEditedStrategySpec, triggerFallback, seriesCompareTriggerLabel } from '../shared/api/contract'
import { NumericInput } from './NumericInput'
import { SettingInfo } from './SettingInfo'
import { MetricSearch } from './MetricSearch'
import { INDICATOR_NAMES } from '../shared/indicator-labels'

type Node = StrategySpecExitRule

export function RuleSpecEditor({ draft, side, path, capabilities, onChange }: {
  draft: StrategyDraft; side: 'entry' | 'exit'; path?: string; capabilities?: CapabilitiesResponse
  onChange: (draft: StrategyDraft) => void
}) {
  const spec = strategySpecFromDraft(draft)
  const commit = (next: typeof spec) => onChange(withEditedStrategySpec(draft, next, capabilities))
  const field = (label: string, value: number, update: (value: number) => void, min?: number, max?: number,
    step: number | 'any' = 'any', inputLabel = label) =>
    <div className="setting-row grow"><span className="k"><SettingInfo label={label} /></span>
      <NumericInput label={inputLabel} value={value} min={min} max={max} integer={step === 1}
        onValueChange={update} /></div>
  const renderNode = (node: Node, id: string, replace: (node: Node) => void): ReactNode => {
    if (path && path !== side && id !== path && !id.startsWith(`${path}-`) && !path.startsWith(`${id}-`)) return null
    if (node.type === 'all' || node.type === 'any') return <div key={id}>
      <div className="setting-row"><span className="k"><SettingInfo label="条件关系" /></span><select aria-label={`${id}条件关系`} value={node.type}
        onChange={(event) => replace({ ...node, type: event.target.value as 'all' | 'any' })}>
        <option value="all">全部满足（且）</option><option value="any">任一满足（或）</option>
      </select></div>
      {node.children.map((child, i) => renderNode(child, `${id}-${i}`, (next) => replace({ ...node, children: node.children.map((item, index) => index === i ? next as StrategySpecCondition : item) })))}
    </div>
    if (node.type === 'not') return <div key={id}><p>不满足以下条件</p>{renderNode(node.child, `${id}-not`, (next) => replace({ ...node, child: next as StrategySpecCondition }))}</div>
    const ui = draft[side].conditions.find((item) => item.id === id)
    return <section className="rule-edit-condition" key={id}>
      <h3>{ui?.label ?? '编辑条件'}</h3>
      {node.type === 'indicator_condition' ? <>
        <MetricSearch key={`${id}:${node.indicator_id}:${spec.instrument.symbol}:${spec.backtest.start}:${spec.backtest.end}`}
          id={id} name={node.indicator_id === 'provider.numeric' ? String(node.params.metric_query ?? '')
          : capabilities?.indicators.find(item => item.indicator_id === node.indicator_id)?.display_name ?? node.indicator_id}
          indicators={capabilities?.indicators ?? []} instrument={spec.instrument.symbol}
          start={spec.backtest.start} end={spec.backtest.end} onSelect={item => {
            const trigger = item.triggers.includes(node.trigger) ? node.trigger : item.triggers[0]
            if (!trigger) return
            const definition = item.trigger_definitions.find((candidate) => candidate.id === trigger)
            replace({ ...node, indicator_id: item.indicator_id, definition_version: item.definition_version,
              params: Object.fromEntries(item.parameters.filter((parameter) => parameter.default !== null)
                .map((parameter) => [parameter.name, parameter.default as string | number | boolean])),
              trigger, value: item.indicator_id === 'provider.series_compare' || definition?.value_requirement === 'forbidden' ? null : definition?.minimum ?? 0 })
          }} onQuery={(metric_query, unit) => {
            const item = capabilities?.indicators.find(candidate => candidate.indicator_id === 'provider.numeric')
            if (!item) return
            const trigger = item.triggers.includes('gt') ? 'gt' : item.triggers[0]
            if (!trigger) return
            replace({ ...node, indicator_id: item.indicator_id, definition_version: item.definition_version,
              params: { metric_query, unit }, trigger, value: 0 })
          }} />
        {node.indicator_id === 'provider.numeric' ? <>
          <div className="setting-row grow setting-row--text"><span className="k"><SettingInfo label="指标名称与口径" /></span><input className="settings-input settings-input--text" type="text"
            aria-label={`${id}指标名称与口径`} maxLength={200}
            value={String(node.params.metric_query ?? '')}
            onChange={(event) => replace({ ...node, params: { ...node.params, metric_query: event.target.value } })} />
          </div>
          <div className="setting-row grow setting-row--text"><span className="k"><SettingInfo label="阈值单位" /></span><input className="settings-input settings-input--text" type="text"
            aria-label={`${id}阈值单位`} maxLength={32}
            value={String(node.params.unit ?? '')}
            onChange={(event) => replace({ ...node, params: { ...node.params, unit: event.target.value } })} />
          </div>
        </> : null}
        {node.indicator_id === 'provider.series_compare' ? <>
          {([
            ['left_metric_query', '左侧指标与口径'],
            ['right_metric_query', '右侧指标与口径'],
            ['unit', '共同单位'],
          ] as const).map(([key, label]) => <div className="setting-row grow setting-row--text" key={key}>
            <span className="k"><SettingInfo label={label} /></span><input className="settings-input settings-input--text" type="text"
              aria-label={`${id}${label}`} maxLength={key === 'unit' ? 32 : 200}
              value={String(node.params[key] ?? '')}
              onChange={(event) => replace({ ...node, value: null, params: { ...node.params, [key]: event.target.value } })} />
          </div>)}
        </> : null}
        <div className="setting-row"><span className="k"><SettingInfo label="触发方式" /></span><select aria-label={`${id}触发方式`} value={node.trigger}
          onChange={(event) => {
            const definition = capabilities?.indicators.find((item) => item.indicator_id === node.indicator_id)?.trigger_definitions.find((item) => item.id === event.target.value)
            replace({ ...node, trigger: event.target.value, value: node.indicator_id === 'provider.series_compare' || definition?.value_requirement === 'forbidden' ? null : node.value ?? definition?.minimum ?? 0 })
          }}>
          {(capabilities?.indicators.find((item) => item.indicator_id === node.indicator_id)?.triggers ?? [node.trigger])
            .map((trigger) => <option value={trigger} key={trigger}>{node.indicator_id === 'provider.series_compare' ? seriesCompareTriggerLabel(trigger) : capabilities?.indicators.find((item) => item.indicator_id === node.indicator_id)?.trigger_definitions.find((item) => item.id === trigger)?.display_name ?? triggerFallback(trigger)}</option>)}
        </select></div>
        {Object.entries(node.params).filter(([,value]) => typeof value === 'number').map(([key, value]) => {
          const parameter = ui?.kind === 'indicator' ? ui.parameters.find((item) => item.key === key) : undefined
          const baseLabel = parameter?.label ?? key
          const displayLabel = baseLabel === '周期'
            ? `${INDICATOR_NAMES[node.indicator_id] ?? '指标'} 周期`
            : baseLabel
          return <div key={key}>{field(displayLabel, value as number, (next) => replace({ ...node, params: { ...node.params, [key]: next } }), parameter?.min, parameter?.max, parameter?.integer ? 1 : 'any', baseLabel)}</div>
        })}
        {node.indicator_id !== 'provider.series_compare' && node.value !== null ? field('阈值', node.value, (next) => replace({ ...node, value: next })) : null}
      </> : node.type === 'holding_period_exit' ? field('持有交易日', node.sessions, (next) => replace({ ...node, sessions: next }), 1, 10000, 1)
        : node.type === 'position_return_exit' || node.type === 'trailing_drawdown_exit' ? field('幅度（%）', node.threshold_pct, (next) => replace({ ...node, threshold_pct: next }), 0.01, 100)
        : node.type === 'minute_protection_exit' ? <>
          {node.take_profit_pct != null ? field('止盈（%）', node.take_profit_pct, (next) => replace({ ...node, take_profit_pct: next }), 0.01, 10000) : null}
          {node.stop_loss_pct != null ? field('止损（%）', node.stop_loss_pct, (next) => replace({ ...node, stop_loss_pct: next }), 0.01, 99.99) : null}
          {node.trailing_drawdown_pct != null ? field('移动回撤（%）', node.trailing_drawdown_pct, (next) => replace({ ...node, trailing_drawdown_pct: next }), 0.01, 99.99) : null}
          {node.limit_price_cny != null ? field('退出限价（元）', node.limit_price_cny, (next) => replace({ ...node, limit_price_cny: next }), 0.01) : null}
          <p className="settings-help">完成分钟K线触发，委托下一分钟生效；同根止盈止损同时出现时保守按止损。</p>
        </>
        : node.type === 'financial_condition' ? <>
          <div className="setting-row"><span className="k"><SettingInfo label="比较方式" /></span><select aria-label={`${id}财务比较方式`} value={node.comparator}
            onChange={(event) => replace({ ...node, comparator: event.target.value as typeof node.comparator })}>
            {Object.entries({ gt: '高于', gte: '不低于', lt: '低于', lte: '不高于', eq: '等于', ne: '不等于' })
              .map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select></div>
          {typeof node.value === 'number' ? field(`财务阈值（${node.unit}）`, node.value, (value) => replace({ ...node, value }))
            : <div className="setting-row"><span className="k"><SettingInfo label="比较值" /></span><input aria-label={`${id}财务比较值`} value={node.value}
              onChange={(event) => replace({ ...node, value: event.target.value })} /></div>}
          <p className="settings-help">沿用本条财务指标的报告期、单位和当时可知口径。</p>
        </> : node.type === 'event_condition' ? <>
          {Object.entries(node.attributes).map(([key, value]) => <div key={key}>
            {typeof value === 'number' ? field(`事件参数 ${key}`, value, (next) => replace({ ...node, attributes: { ...node.attributes, [key]: next } }))
              : typeof value === 'boolean' ? <div className="setting-row"><span className="k"><SettingInfo label={key} /></span><input type="checkbox" aria-label={`事件参数 ${key}`} checked={value}
                onChange={(event) => replace({ ...node, attributes: { ...node.attributes, [key]: event.target.checked } })} /></div>
              : <div className="setting-row"><span className="k"><SettingInfo label={key} /></span><input aria-label={`事件参数 ${key}`} value={value}
                onChange={(event) => replace({ ...node, attributes: { ...node.attributes, [key]: event.target.value } })} /></div>}
          </div>)}
          {node.document_text ? <>
            <div className="setting-row"><span className="k"><SettingInfo label="公告关键词" /></span><input aria-label="公告关键词" value={node.document_text.term}
              onChange={(event) => replace({ ...node, document_text: { ...node.document_text!, term: event.target.value } })} /></div>
            {field('关键词出现次数', node.document_text.value, (value) => replace({ ...node, document_text: { ...node.document_text!, value } }), 0, undefined, 1)}
          </> : null}
          {!Object.keys(node.attributes).length && !node.document_text ? <p className="settings-help">这条条件以事件发布为触发，没有额外参数。</p> : null}
        </> : null}
    </section>
  }
  if (!spec.entry || !spec.exit) return null
  const exit = spec.exit
  return <div>
    {side === 'entry' ? renderNode(spec.entry, 'entry', (node) => commit({ ...spec, entry: node as StrategySpecCondition })) : <>
      <div className="setting-row"><span className="k"><SettingInfo label="卖出条件关系" /></span><select aria-label="卖出条件关系"
        value={spec.exit.op === 'all' || (spec.exit.children.length === 1 && spec.exit.children[0]?.type === 'all') ? 'all' : 'any'}
        disabled={spec.exit.children.length <= 1 && !['all', 'any'].includes(spec.exit.children[0]?.type ?? '')}
        onChange={(event) => {
          const children = exit.children.length === 1 && ['all', 'any'].includes(exit.children[0]?.type ?? '')
            ? (exit.children[0] as { children: StrategySpecCondition[] }).children
            : exit.children as StrategySpecCondition[]
          commit({ ...spec, exit: { op: event.target.value === 'all' ? 'all' : 'first_of', children } })
        }}><option value="any">任一触发（或）</option><option value="all">全部满足（且）</option></select></div>
      {spec.exit.op === 'all' ? <p className="settings-help">每天收盘确认所有条件同时满足，下一交易日尝试卖出。持有期满后继续等待其他条件，不会到期就单独卖出。</p> : null}
      {exit.children.map((node, i) => renderNode(node, `exit-${i}`, (next) => commit({ ...spec, exit: { ...exit, children: exit.children.map((item, index) => index === i ? next : item) } })))}
    </>}
  </div>
}
