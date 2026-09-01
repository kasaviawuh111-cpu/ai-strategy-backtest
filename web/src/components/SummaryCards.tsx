/**
 * 对话内的摘要卡 —— 首屏只放"一眼能读完"的信息，其余全部下沉到二级页。
 * SPEC 4.2：首屏不能出现完整参数表、成交成本表、数据来源长说明或运行证据。
 */
import { Chevron, Notice, ThinkingStream, fmtPct, signClass } from './primitives';
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
  StrategyCapabilitySummary,
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
  all: '并且',
  any: '或者',
  first_of: '任一先发生',
  not: '不满足',
};

function RuleNode({ node, compact = false }: { node: StrategyRuleNode; compact?: boolean }) {
  if (node.kind === 'leaf') {
    return (
      <div className={`rule-leaf rule-leaf--${node.tone}`}>
        <b>{node.label}</b>
        {!compact ? <span>{node.detail}</span> : null}
        {!compact && node.parameters.length > 0 ? (
          <small>{node.parameters.join(' · ')}</small>
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

const capabilityStateLabel: Record<StrategyCapabilitySummary['stages'][number]['state'], string> = {
  available: '已具备',
  conditional: '按请求准备',
  unavailable: '不可用',
  unknown: '待确认',
  demo: '预览',
};

export function CapabilityCard(
  { capability, mode, onEdit }:
  { capability: StrategyCapabilitySummary; mode: 'mock' | 'live'; onEdit?: () => void },
) {
  return (
    <section className="mcard capability-card" aria-label="策略能力状态">
      <div className="pad">
        <div className="capability-head">
          <b>{mode === 'mock' ? '界面预览说明' : '这条策略当前能走到哪一步'}</b>
          <span>{mode === 'mock' ? '不代表后台可用' : '来自能力接口'}</span>
        </div>
        <ol className="capability-stages">
          {capability.stages.map((stage, index) => (
            <li key={stage.key} className={`cap-${stage.state}`}>
              <i>{index + 1}</i>
              <span><b>{stage.label}</b><small>{stage.detail}</small></span>
              <em>{capabilityStateLabel[stage.state]}</em>
            </li>
          ))}
        </ol>
        {capability.events.map((event) => (
          <div className="event-capability" key={event.eventCode}>
            <b>{event.label}</b>
            <span>Catalog {capabilityStateLabel[event.catalog]}</span>
            <span>可准备 {capabilityStateLabel[event.preparable]}</span>
            <span>固定快照 {capabilityStateLabel[event.pinnedSnapshot]}</span>
            <small>{event.detail}</small>
          </div>
        ))}
        {capability.reason ? <p className="capability-reason">{capability.reason}</p> : null}
      </div>
      {!capability.canRun && onEdit ? <CardFoot actions={[{ label: '修改规则', onClick: onEdit }]} /> : null}
    </section>
  );
}

export function RecognizedCard(
  { items }: { items: Array<{ label: string; value: string }> },
) {
  if (items.length === 0) return null;
  return (
    <section className="mcard recognized-card" aria-label="已经识别的规则片段">
      <div className="pad">
        <p className="ttl">已经替你保留</p>
        {items.map((item) => (
          <div className="recognized-row" key={`${item.label}:${item.value}`}>
            <span>{item.label}</span><b>{item.value}</b>
          </div>
        ))}
        <Notice tone="info">这些是你已经说清楚的部分，我先留着；补完直接接上，不用重说一遍。</Notice>
      </div>
    </section>
  );
}

export function PreparationCard(
  { stage, needsPreparation, isMock = false }:
  { stage: 'saving' | 'preparing'; needsPreparation: boolean; isMock?: boolean },
) {
  const title = stage === 'saving'
    ? '正在保存最终策略版本'
    : needsPreparation ? '正在准备这次事件数据' : '正在校验固定数据并提交任务';
  return (
    <section className="mcard preparation-card" aria-live="polite">
      <div className="pad">
        <div className="phase-now"><span>{isMock ? `预览：${title}` : title}</span><code>提交中</code></div>
        <p>{isMock
          ? '这里模拟提交等待，不代表后台正在采集或计算。'
          : needsPreparation
            ? '后端正在按股票、区间和年度报告条件准备并校验数据；只有准备通过才会创建回测任务。'
            : '前端正在等待服务端确认策略版本和任务身份，不伪造后台进度。'}</p>
      </div>
    </section>
  );
}

export function NextStepCard(
  { onEdit, onStress, onTrades }:
  { onEdit: () => void; onStress: () => void; onTrades: () => void },
) {
  return (
    <section className="mcard next-step-card">
      <div className="pad">
        <p className="ttl">接下来可以继续验证</p>
        <p>这些动作只帮助检查历史规则，不给出买卖建议。</p>
      </div>
      <CardFoot actions={[
        { label: '修改规则', onClick: onEdit, mute: true },
        { label: '压力测试', onClick: onStress, mute: true },
        { label: '查看交易', onClick: onTrades },
      ]} />
    </section>
  );
}

/** 策略确认卡：首层只保留买入、卖出、区间；本金与高级成交设置下沉。 */
export function StrategyCard(
  { instrument, strategy, onEditRow, onOpenMore, onRun, isStarting, isLocked, settled, canStart = true, disabledReason, error, executionSummary }:
  { instrument: Instrument; strategy: StrategySummary;
    onEditRow: (key: EditableRow['key']) => void; onOpenMore: () => void; onRun: () => void;
    isStarting?: boolean; isLocked?: boolean; settled?: string; canStart?: boolean; disabledReason?: string; error?: string;
    /** 当前成交设置的一句话后果，由 view-model 的 summarizeExecution 生成。 */
    executionSummary?: string },
) {
  const isReadOnly = Boolean(isLocked || settled);
  return (
    <section className={`mcard${settled ? ' is-settled' : ''}`}>
      <div className="pad">
        <div className="symbol">
          <span className="nm">{instrument.name}</span>
          <span className="cd">{instrument.code}</span>
          {instrument.price ? (
            <span className="px">
              <b>{instrument.price}</b>
              {instrument.changePct ? <span className="up">{instrument.changePct}</span> : null}
            </span>
          ) : null}
          {/*
            成交设置是入口，不是一行说明。放在标题行右上角：想改的人找得到，
            不想改的人一眼跳过，卡片不再为它多占一整行 48px。
          */}
          <button
            type="button"
            className={`exec-entry${isReadOnly ? ' off' : ''}`}
            onClick={onOpenMore}
            disabled={isReadOnly}
          >
            成交设置<span className="v">{executionSummary ?? '默认'}</span>
            <Chevron />
          </button>
        </div>
        <div className="erows">
          {strategy.rows.map((row) => (
            <button
              key={row.key}
              type="button"
              className={`erow${row.kind ? ` ${row.kind}` : ''}${isReadOnly ? ' off' : ''}`}
              onClick={() => onEditRow(row.key)}
              disabled={isReadOnly}
            >
              <span className="lb">{row.label}</span>
              <span className="val">
                {row.value}
                {row.sub ? <small>{row.sub}</small> : null}
              </span>
              <Chevron />
            </button>
          ))}
        </div>
      </div>
      <CardFoot settled={settled} actions={[
        {
          label: isStarting ? '正在创建任务' : isLocked ? '回测运行中' : canStart ? '开始回测' : '请检查设置',
          onClick: onRun,
          disabled: isLocked || !canStart,
        },
      ]} />
      {!canStart && disabledReason ? <div className="card-note">{disabledReason}</div> : null}
      {error ? <div className="card-error" role="alert">{error}</div> : null}
    </section>
  );
}

/** 运行卡（SPEC 4.7）：只展示真实阶段，不伪造算法进度 */
export function RunningCard(
  { phase, onCancel, isCancelling, isMock = false }:
  { phase: RunPhase; onCancel: () => void; isCancelling?: boolean; isMock?: boolean },
) {
  const canCancel = !['succeeded', 'failed', 'cancelled'].includes(phase);
  const phaseLabel = RUN_PHASE_LABEL[phase];
  const status = phase === 'cancel_requested' ? phaseLabel : `正在${phaseLabel}`;
  return (
    <ThinkingStream
      title="运行这次历史回测"
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
  { state, onAction }: { state: FailureState; onAction?: (index: number) => void },
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
          <Notice tone="info">任务编号 <code>{state.runId}</code>，报给工程时带上这个就够了。</Notice>
        ) : null}
      </div>
      <CardFoot actions={state.actions.map((label, i) => ({
        label, mute: i > 0, onClick: onAction ? () => onAction(i) : undefined,
      }))} />
    </section>
  );
}
