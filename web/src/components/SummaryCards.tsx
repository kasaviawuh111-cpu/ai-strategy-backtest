/**
 * 对话内的摘要卡 —— 首屏只放"一眼能读完"的信息，其余全部下沉到二级页。
 * SPEC 4.2：首屏不能出现完整参数表、成交成本表、数据来源长说明或运行证据。
 */
import { useState } from 'react';
import { Chevron, Notice, ThinkingStream, fmtPct, signClass } from './primitives';
import { InlineStockEditor, type InlineStockEditorActions } from './InlineStockEditor';
import { ExcessEquation } from './ExcessEquation';
import { MiniEquity } from './MiniEquity';
import { secondaryMetric } from '../view-model';
import type {
  BacktestMetrics,
  ChartMark,
  EditableRow,
  FailureState,
  Instrument,
  SeriesPoint,
  StrategyRuleNode,
  StrategySummary,
} from '../types';
import { RUN_PHASE_LABEL, type RunPhase } from '../types';
import { numericResultConclusion } from '../result-conclusion';

function CardFoot(
  { actions, settled }:
  { actions: Array<{ label: string; onClick?: () => void; mute?: boolean; disabled?: boolean }>; settled?: string },
) {
  if (settled) return <div className="settled">{settled}</div>;
  return (
    <div className="foot">
      {actions.map((a) => (
        <button key={a.label} type="button" className={a.mute ? 'mute' : undefined} onClick={a.onClick} disabled={a.disabled}>
          {a.label}
        </button>
      ))}
    </div>
  );
}

const operatorJoiner: Record<Extract<StrategyRuleNode, { kind: 'group' }>['operator'], string> = {
  all: '全部满足（且）',
  any: '任一满足（或）',
  first_of: '任一先触发，即卖出',
  not: '不满足',
};

function RuleNode({ node, compact = false }: { node: StrategyRuleNode; compact?: boolean }) {
  if (node.kind === 'leaf') {
    return (
      <div className={`rule-leaf rule-leaf--${node.tone}`}>
        <b>{node.label}</b>
        {!compact ? <span>{node.detail}</span> : null}
        {!compact && node.parameters.length > 0 ? (
          <small>参数：{node.parameters.join(' · ')}</small>
        ) : null}
      </div>
    );
  }
  return (
    <div className={`rule-group rule-group--${node.operator}`}>
      {node.children.length > 1 || node.operator === 'not' ? (
        <span className="rule-operator">{operatorJoiner[node.operator]}</span>
      ) : null}
      <div className="rule-children">
        {node.children.map((child) => <RuleNode key={child.id} node={child} compact={compact} />)}
      </div>
    </div>
  );
}

export function RuleTree({ node, compact = false }: { node: StrategyRuleNode; compact?: boolean }) {
  return <div className="rule-tree"><RuleNode node={node} compact={compact} /></div>;
}

const gridPlanSummary = (strategy: StrategySummary): Array<{ side: 'buy' | 'sell'; text: string }> => {
  const gridBuy = Boolean(strategy.gridReview?.buy.length)
  const gridSell = Boolean(strategy.gridReview?.sell.length)
  return [
    ...(gridBuy ? [{ side: 'buy' as const, text: '价格下跌时，按网格分批买入。' }] : []),
    ...(gridSell ? [{ side: 'sell' as const, text: '价格上涨时，按网格分批卖出。' }] : []),
  ]
}

/** 策略确认卡：首层只保留买入、卖出、区间；具体参数与高级设置下沉。 */
export function StrategyCard(
  { instrument, strategy, onEditRow, onOpenMore, onRun, isStarting, isLocked, settled, editableAfterRun = false, canStart = true, checkingSettings = false, disabledReason, blockedLabel, error, executionSummary, stockEditor, onRetryConnection, retryLabel = '重新检查连接' }:
  { instrument: Instrument; strategy: StrategySummary;
    onEditRow: (key: EditableRow['key'], path?: string) => void; onOpenMore: () => void; onRun: () => void;
    isStarting?: boolean; isLocked?: boolean; settled?: string; editableAfterRun?: boolean; canStart?: boolean; checkingSettings?: boolean; disabledReason?: string; blockedLabel?: string; error?: string;
    onRetryConnection?: () => void;
    retryLabel?: string;
    /** 当前高级设置的一句话摘要，由 view-model 的 summarizeExecution 生成。 */
    executionSummary?: string; stockEditor?: InlineStockEditorActions },
) {
  const [stockEditing, setStockEditing] = useState(false);
  const isReadOnly = Boolean(isLocked || (settled && !editableAfterRun));
  const fieldsLocked = isReadOnly || stockEditing;
  const advancedSummary = executionSummary ?? (strategy.pricePlanKind ? '按交易计划' : '默认');
  return (
    <section className={`mcard${settled ? ' is-settled' : ''}${stockEditing ? ' is-stock-editing' : ''}`}>
      <div className="pad">
        <div className="symbol">
          {stockEditor ? <InlineStockEditor instrument={instrument} disabled={isReadOnly}
            {...stockEditor} onEditingChange={setStockEditing} /> : <>
            <span className="nm">{instrument.name}</span>
            <span className="cd">{instrument.code}</span>
          </>}
          {instrument.price ? (
            <span className="px">
              <b>{instrument.price}</b>
              {instrument.changePct ? <span className="up">{instrument.changePct}</span> : null}
            </span>
          ) : null}
          {/* 高级设置是单一入口：交易计划、成交成本和研究执行语义都在二级页。 */}
          <button
            type="button"
            className={`exec-entry${fieldsLocked ? ' off' : ''}`}
            onClick={onOpenMore}
            disabled={fieldsLocked}
          >
            高级设置<span className="v">{advancedSummary}</span>
            <Chevron />
          </button>
        </div>
        <div className="condition-sections">
          {strategy.gridReview ? gridPlanSummary(strategy).map(({ side, text }) => <section
            className={`condition-section ${side}`} key={`grid-${side}`} aria-label={`${side === 'buy' ? '买入' : '卖出'}条件`}>
            <header className="condition-heading"><span className="lb">{side === 'buy' ? '买入' : '卖出'}</span></header>
            <p className="condition-trigger-note">{text}</p>
          </section>) : null}
          {strategy.rows.map((row) => {
            if (strategy.gridReview && (row.key === 'entry' || row.key === 'exit')
              && (row.key === 'entry' ? strategy.gridReview.buy : strategy.gridReview.sell).length > 0) return null
            const root = row.key === 'entry' ? strategy.entryRule : row.key === 'exit' ? strategy.exitRule : null;
            // first_of with a single market-condition group delegates to that group.
            // Do not label an ALL group as OR just because the exit envelope is first_of.
            const node = root?.kind === 'group' && root.operator === 'first_of' && root.children.length === 1
              ? root.children[0] : root;
            if (node) {
              const children = node.kind === 'group' && node.operator !== 'not' ? node.children : [node];
              const operator = node.kind === 'group' ? node.operator : 'all';
              return <section className={`condition-section ${row.kind}`} key={row.key} aria-label={`${row.label}条件`}>
                <header className="condition-heading"><span className="lb">{row.label}</span>
                  {strategy.pricePlanKind !== 'grid' && <button type="button" className="condition-relation" disabled={fieldsLocked}
                    onClick={() => onEditRow(row.key, node.id)}>
                    {strategy.pricePlanKind ? strategy.pricePlanKind === 'scheduled' ? '按日历安排 · 休市顺延'
                      : strategy.pricePlanKind === 'conditional' && !strategy.hasSequentialPricePlanStages
                        ? row.value.startsWith('建仓：') ? '区间开始时建仓'
                          : row.value.startsWith('期初已有') ? '已有底仓' : '满足条件时触发'
                        : '按编号阶段执行 · 同组先触发者生效'
                      : children.length > 1 ? operator === 'all' ? `全部满足才${row.label}（且）`
                      : operator === 'not' ? '不满足以下条件' : `任一触发就${row.label}（或）` : '满足条件时触发'}<Chevron />
                  </button>}
                </header>
                {row.key === 'entry' && strategy.entryTriggerNote ? (
                  <p className="condition-trigger-note">{strategy.entryTriggerNote}</p>
                ) : null}
                <div className={`erows${strategy.pricePlanKind === 'grid' ? ' erows--readonly' : ''}`}>
                  {children.map((child) => strategy.pricePlanKind === 'grid'
                    ? <div key={child.id} className="erow">
                      <div className="val"><RuleTree node={child} compact /></div>
                    </div>
                    : <button key={child.id} type="button" className="erow"
                    disabled={fieldsLocked} onClick={() => onEditRow(row.key, child.id)}>
                    <div className="val"><RuleTree node={child} compact /></div><Chevron />
                  </button>)}
                </div>
              </section>;
            }
            return (
            <button
              key={row.key}
              type="button"
              className="condition-range"
              onClick={() => onEditRow(row.key)}
              disabled={fieldsLocked}
            >
              <span className="lb">{row.label}</span>
              <div className="val">
                {row.key === 'entry' ? <RuleTree node={strategy.entryRule} compact />
                  : row.key === 'exit' ? <RuleTree node={strategy.exitRule} compact /> : row.value}
                {row.sub ? <small>{row.sub}</small> : null}
              </div>
              <Chevron />
            </button>
          )})}
        </div>
      </div>
      <CardFoot settled={settled} actions={[
        {
          label: isStarting ? '正在准备回测' : checkingSettings ? '检查设置中' : isLocked ? '回测运行中' : canStart ? '开始回测' : blockedLabel ?? '请检查设置',
          onClick: onRun,
          disabled: isLocked || stockEditing || !canStart,
        },
        ...(!canStart && onRetryConnection ? [{
          label: checkingSettings ? '正在重新检查' : retryLabel,
          onClick: onRetryConnection,
          disabled: isLocked || stockEditing || checkingSettings,
        }] : []),
      ]} />
      {!canStart && disabledReason ? <div className="card-note" role="status" aria-live="polite">{disabledReason}</div> : null}
      {error ? <div className="card-error" role="alert">{error}</div> : null}
    </section>
  );
}

/** 运行卡（SPEC 4.7）：只展示真实阶段，不伪造算法进度 */
export function RunningCard(
  { phase, progressLabel, onCancel, isCancelling, isMock = false }:
  {
    phase: RunPhase;
    progressLabel?: string;
    onCancel: () => void;
    isCancelling?: boolean;
    isMock?: boolean;
  },
) {
  const canCancel = !['succeeded', 'failed', 'cancelled'].includes(phase);
  const phaseLabel = RUN_PHASE_LABEL[phase];
  const fallbackStatus = phase === 'cancel_requested' ? phaseLabel : `正在${phaseLabel}`;
  const status = !isMock && progressLabel?.trim() ? progressLabel.trim() : fallbackStatus;
  return (
    <ThinkingStream
      status={isMock ? `预览：${status}` : status}
      label="回测进度"
      action={(
        <button
          type="button"
          className="thinking-cancel"
          onClick={onCancel}
          disabled={!canCancel || isCancelling}
        >
          {isCancelling || phase === 'cancel_requested' ? '正在取消' : '取消回测'}
        </button>
      )}
    />
  );
}

/**
 * 结果卡：先用精确数字回答盈亏，再把曲线形状和买卖点直接露在会话里。
 *
 * 之前这里只有四个数字，用户想知道「这条策略是怎么走出来的」必须先点进报告。
 * 现在缩略曲线 + B/S 点在卡上就能看到，报告页负责的是逐笔明细，不再是「第一次看到图」。
 */
export function ResultCard(
  { metrics, onOpenReport, historical = false, series = [], marks = [] }:
  { metrics: BacktestMetrics; onOpenReport: () => void; historical?: boolean;
    series?: SeriesPoint[]; marks?: ChartMark[] },
) {
  const filledMarks = marks.filter((mark) => mark.kind !== 'no_fill');
  const secondary = secondaryMetric(metrics);
  return (
    <section className={`mcard${historical ? ' history-result' : ''}`}>
      <div className="pad">
        <p className="ttl">{historical ? '历史回测结果' : '回测结果'}</p>
        <p className="result-conclusion">{numericResultConclusion(metrics)}</p>
        <div className="headline">
          <span className={`big ${signClass(metrics.total)}`}>{fmtPct(metrics.total)}</span>
          <span className="lb">策略总收益</span>
        </div>

        <MiniEquity
          series={series}
          marks={marks}
          label={`策略累计收益 ${fmtPct(metrics.total)}，区间内 ${filledMarks.length} 个买卖点，点开可看每一笔`}
        />

        <ExcessEquation
          total={metrics.total}
          bench={metrics.bench}
          excess={metrics.excess}
          comparisonStatus={metrics.benchmarkComparisonStatus}
          compact
        />

        <div className="kpis kpis--pair">
          {/* 最大回撤是风险量，不套涨跌色，避免绿色被读成“好消息” */}
          <div className="kpi"><b>{fmtPct(metrics.mdd)}</b><span>最大回撤<small>从最高点往下跌最多的一段</small></span></div>
          <div className="kpi"><b>{secondary.value}</b><span>{secondary.label}<small>{secondary.note}</small></span></div>
        </div>
      </div>
      <CardFoot actions={[
        { label: historical ? '查看这次报告' : '查看完整报告', onClick: onOpenReport },
      ]} />
    </section>
  );
}

/** 空 / 失败 / 拒绝态卡（SPEC 4.10）：说清缺什么 + 给下一步 */
export function FailureCard(
  { state, onAction }: { state: FailureState; onAction?: (action: FailureState['actions'][number]['action']) => void },
) {
  const statusLabel: Record<FailureState['status'], string> = {
    proved: '已验证',
    implemented: '已停止',
    partial: '信息不完整',
    mock_only: '预览状态',
    research_only: '研究模式',
    unavailable: '暂不可用',
    target: '待接入',
    rejected: '无法执行',
  }
  return (
    <section className="mcard">
      <div className="pad">
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <b style={{ fontSize: 16, lineHeight: '22px' }}>{state.title}</b>
          <span style={{ marginLeft: 'auto' }}>
            <span className={`tag ${state.status === 'rejected' ? 't-reject'
              : state.status === 'target' ? 't-target'
              : state.status === 'implemented' ? 't-impl' : 't-mock'}`}>{statusLabel[state.status]}</span>
          </span>
        </div>
        <p style={{ margin: '8px 0 0', fontSize: 14, lineHeight: '22px', color: 'var(--ink-2)' }}>
          {state.reason}
        </p>
        {state.runId ? (
          <Notice tone="info">问题编号 <code>{state.runId}</code>，反馈问题时可附上。</Notice>
        ) : null}
      </div>
      <CardFoot actions={state.actions.map(({ label, action }, i) => ({
        label, mute: i > 0, onClick: onAction ? () => onAction(action) : undefined,
      }))} />
    </section>
  );
}
