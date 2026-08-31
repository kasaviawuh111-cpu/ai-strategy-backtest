/** 与 API v1 契约对齐的前端类型（SPEC 9.3） */

import type { BacktestSignalEvidence } from './shared/api/types'

/** SPEC 1.2 状态词 —— 任何对外文案都必须落在这组词上 */
export type StatusWord =
  | 'proved'
  | 'implemented'
  | 'partial'
  | 'mock_only'
  | 'research_only'
  | 'unavailable'
  | 'target'
  | 'rejected';

/** 编译状态（SPEC 5.2） */
export type CompileState = 'ready' | 'needs_clarification' | 'unsupported' | 'invalid';

/** 运行状态（SPEC 4.7）—— 前端只展示真实阶段，不伪造进度 */
export type RunPhase =
  | 'queued'
  | 'running:data'
  | 'running:signal'
  | 'running:execution'
  | 'running:report'
  | 'cancel_requested'
  | 'succeeded'
  | 'failed'
  | 'cancelled';

export const RUN_PHASE_LABEL: Record<string, string> = {
  queued: '等待计算资源',
  'running:data': '固定并读取数据',
  'running:signal': '计算技术与事件信号',
  'running:execution': '模拟 A 股订单与成交',
  'running:report': '生成回测报告',
  cancel_requested: '正在取消',
  cancelled: '已取消',
  failed: '回测失败',
  succeeded: '回测完成',
};

/** 活动类型（SPEC 4.9）—— 每一种都有独立的形状与文字，不靠颜色区分 */
export type ActivityKind = 'signal' | 'order' | 'fill' | 'partial_fill' | 'no_fill' | 'expired';

export interface Instrument {
  name: string;
  code: string;      // 000000.SH/SZ/BJ
  price?: string;     // 可选：不复权最新价，字符串保留原始精度
  changePct?: string; // 可选：带符号；API 未提供时不展示
}

/** 策略卡内可直接点开的四个核心事实。 */
export interface EditableRow {
  key: 'entry' | 'exit' | 'range' | 'cash';
  label: string;
  /** 一眼能读懂的说法，例如「近 5 年」。 */
  value: string;
  /** 可核对的精确值，例如「2021-08-30 至 2026-08-29」。 */
  sub?: string;
  kind?: 'buy' | 'sell';
}

export interface StrategySummary {
  title: string;
  /** 卡片本身就是表单；高级执行设置仍下沉到二级页。 */
  rows: EditableRow[];
  strategyHash: string;
  entryRule: StrategyRuleNode;
  exitRule: StrategyRuleNode;
  confirmation: string;
  earliestExecution: string;
  conditionCount: number;
  eventFacts: StrategyEventFact[];
}

export type StrategyRuleOperator = 'all' | 'any' | 'first_of' | 'not';

export type StrategyRuleNode =
  | {
      kind: 'group';
      id: string;
      operator: StrategyRuleOperator;
      label: string;
      children: StrategyRuleNode[];
    }
  | {
      kind: 'leaf';
      id: string;
      label: string;
      detail: string;
      tone: 'indicator' | 'event' | 'holding';
      parameters: string[];
    };

export interface StrategyEventFact {
  id: string;
  label: string;
  eventCode: string;
  firstAvailableAt: string | null;
  timeQuality: string | null;
  provider: string | null;
  sourceUrl: string | null;
  note: string;
}

export type CapabilityStageState = 'available' | 'conditional' | 'unavailable' | 'unknown' | 'demo';

export interface CapabilityStage {
  key: 'understand' | 'prepare' | 'snapshot';
  label: string;
  state: CapabilityStageState;
  detail: string;
}

export interface StrategyCapabilitySummary {
  stages: CapabilityStage[];
  canRun: boolean;
  needsPreparation: boolean;
  reason: string | null;
  eventCodes: string[];
  events: Array<{
    eventCode: string;
    label: string;
    catalog: CapabilityStageState;
    preparable: CapabilityStageState;
    pinnedSnapshot: CapabilityStageState;
    detail: string;
  }>;
}

export interface BacktestMetrics {
  total: number | null;   // 总收益 %
  bench: number | null;   // 同期持有 %
  excess: number | null;  // 超额 %
  /** 后端已审计的比较前提；没有双方已成交买入时不把收益差叫作超额。 */
  benchmarkComparisonStatus: 'comparable' | 'strategy_entry_not_filled' | 'benchmark_entry_not_filled' | 'benchmark_unavailable';
  mdd: number | null;     // 最大回撤 %（负数）
  trips: number;   // 完整交易数
  win: number | null;     // 胜率 %
  sharpe: number | null;
  ann: number | null;     // 年化收益 %
  initialCashCny: number | null;
  finalEquityCny: number;
  interpretation: string;
  dataRange: { start: string; end: string; sessions: number };
  warnings: string[];
  /** 只有 API 明确提供时才显示；前端不能从活动流推断。 */
  openShares: number | null;
}

export interface SeriesPoint {
  date: string;
  /** API 返回的账户净值（当前 v2 为起点 100 的序列）。 */
  equity: number;
  strategy: number;
  benchmark: number | null;
  /** 从历史高点到当日的回撤，百分比、非比例。 */
  drawdown: number;
}

export interface ChartMark {
  index: number;
  kind: 'buy' | 'sell' | 'no_fill';
  activityId: string;
  side: 'buy' | 'sell';
  title: string;
  occurredAt: string;
  reason: string;
  signalAt: string | null;
  signalReason: string | null;
  orderAt: string | null;
  fillAt: string | null;
  price: number | null;
  quantity: number | null;
  priceLimitImpact: string;
  capacityImpact: string;
  tPlusOneImpact: string;
  timeQuality?: string | null;
  timeSemantics?: string | null;
  evidence: BacktestSignalEvidence[];
}

export interface TradeRow {
  id: string;
  kind: ActivityKind;
  side: 'buy' | 'sell';
  occurredAt: string;
  title: string;
  price?: number | null;
  quantity?: number | null;
  reason: string;
  chainId?: string | null;
  decisionId?: string | null;
  orderId?: string | null;
  fillId?: string | null;
  parentId?: string | null;
  capacityReasonCode?: string | null;
  timeQuality?: string | null;
  timeSemantics?: string | null;
  signalSemantics?: 'edge' | 'event' | 'state' | 'composite' | null;
  signalValiditySessions?: number | null;
  attemptNo?: number | null;
  originSignalId?: string | null;
  retryReason?: string | null;
  outcomeReason?: string | null;
  evidence: BacktestSignalEvidence[];
}

/**
 * 委托状态 —— 用券商柜台的说法，不自造词。
 * 已报 / 已成 / 部成 / 未成 / 废单，和用户在交易软件里看到的一致。
 */
export type OrderStatus = 'placed' | 'filled' | 'partial' | 'unfilled' | 'expired';

/**
 * 一笔委托（SPEC 4.9 的活动流按 order_id 聚合后的展示单位）。
 * 图上的一个 B/S 点，就对应这里的一行；两边不再各做一套选择器。
 */
export interface OrderRow {
  /** 这一行采用的终局活动 ID；同一委托的其他点位通过 activityIds 映射到本行。 */
  id: string;
  /** 属于这一笔委托的活动 ID；图上的成交、部分成交、未成交和过期点都据此定位。 */
  activityIds: string[];
  chainId: string | null;
  side: 'buy' | 'sell';
  status: OrderStatus;
  /** 触发这笔委托的信号名，例如「MACD 金叉确认」。 */
  title: string;
  signalAt: string | null;
  signalReason: string | null;
  orderAt: string | null;
  filledAt: string | null;
  /** 委托价（下单时报的价）。 */
  orderPrice: number | null;
  /** 成交均价；未成交时为空。 */
  price: number | null;
  quantity: number | null;
  /** 未成、部成或废单时的原因；已成时为空。 */
  outcomeNote: string | null;
  timeQuality?: string | null;
  timeSemantics?: string | null;
  /** 该链上全部活动，点开因果轨迹时用。 */
  activities: TradeRow[];
}

export interface ChainFact {
  label: string;
  value: string;
  href?: string;
}

export interface ChainNode {
  kind: ActivityKind | 'utterance' | 'normalized' | 'decision' | 'cash';
  title: string;
  timestamp?: string;
  detail: string;
  /** chain_id / decision_id / order_id / fill_id 等可复查身份 */
  ref: string;
  facts?: ChainFact[];
  /** 原始代码、ID 与校验值只在用户主动展开“技术详情”时展示。 */
  technicalFacts?: ChainFact[];
}

export interface ChainContext {
  metrics?: BacktestMetrics;
  series?: SeriesPoint[];
  evidence?: RunEvidence;
}

/** 运行身份（SPEC 6.6）—— 缺任何一项都不能标为可验收结果 */
export interface RunEvidence {
  runId: string;
  gitSha: string | null;        // 精确 40 位 clean SHA
  snapshotId: string | null;
  snapshotChecksum: string | null;
  dataSchemaVersion: string | null;
  producerSnapshotSchemaVersion: string | null;
  producerSnapshotId: string | null;
  catalog: string | null;
  strategyHash: string | null;
  engineVersion: string | null;
  executionAssumptions: Record<string, string>;
}

/** 空 / 失败 / 拒绝态（SPEC 4.10） */
export interface FailureState {
  key: string;
  // 这里原本还有一句「气泡文案」。卡片标题已经说明发生了什么，
  // 上面再放一句同义的旁白只是把同一件事用 AI 的口气再说一遍，已删。
  title: string;
  status: StatusWord;
  reason: string;    // 必须告诉用户具体缺了什么
  actions: string[]; // 下一步
  runId?: string;
}
