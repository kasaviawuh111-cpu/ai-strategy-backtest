import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import type { StrategyDraft } from '../shared/api/types'
import type {
  BacktestMetrics,
  ChartMark,
  RunEvidence,
  SeriesPoint,
  TradeRow,
} from '../types'
import { ExecutionDetailsScreen, ReportScreen } from './index'

const draft = {
  entry: { conditions: [{ kind: 'event' }] },
  execution: {
    priceLimitMode: 'wait_for_unlock', tPlusOne: true, slippageBps: 5,
    commissionRate: 0.0003, minimumCommissionCny: 5,
  },
} as StrategyDraft

const metrics: BacktestMetrics = {
  total: -2.54, bench: -39.23, excess: 36.69, mdd: -58.19, trips: 1,
  win: null, sharpe: null, ann: null, initialCashCny: 1_000_000,
  finalEquityCny: 974_600, interpretation: '样本不足，不能证明策略有效。',
  dataRange: { start: '2024-01-01', end: '2024-12-31', sessions: 241 },
  warnings: [], openShares: null,
}

const series: SeriesPoint[] = [
  { date: '2024-03-14', equity: 100, strategy: 0, benchmark: 0, drawdown: 0 },
  { date: '2024-03-15', equity: 99, strategy: -1, benchmark: -2, drawdown: -1 },
]

const mark: ChartMark = {
  index: 1, kind: 'buy', activityId: 'fill:1', side: 'buy', title: '买入成交',
  occurredAt: '2024-03-15T09:30:00+08:00', reason: '使用日线开盘价代理',
  signalAt: '2024-03-14T20:57:33+08:00', signalReason: '年报首次可得',
  orderAt: '2024-03-15T09:15:00+08:00', fillAt: '2024-03-15T09:30:00+08:00',
  price: 13.82, quantity: 3_800, priceLimitImpact: '未标记', capacityImpact: '未标记',
  tPlusOneImpact: '未阻断', evidence: [],
}

const trades: TradeRow[] = [{
  id: 'signal:1', kind: 'signal', side: 'buy', occurredAt: '2024-03-14T20:57:33+08:00',
  title: '年度报告首次可得', reason: '年报已到达可用时点', chainId: 'decision:1',
  evidence: [{
    type: 'event_observation', id: 'AN202403141626765931',
    sourceEventId: 'AN202403141626765931', provider: 'eastmoney',
    sourceUrl: 'https://data.eastmoney.com/notices/detail/300059/AN202403141626765931.html',
    availableAt: '2024-03-14T20:57:33+08:00', timeQuality: 'vendor_observed',
    timestampPrecision: 'second',
    validationStatus: 'validated',
    rawResponseSha256: '5afd37736349f7adcf8104dc4e0c9e33339e680e371cef9dd2be2bb38cebcb43',
  }],
}, {
  id: 'fill:1', kind: 'fill', side: 'buy', occurredAt: '2024-03-15T09:30:00+08:00',
  title: '买入成交', reason: '使用日线开盘价代理', chainId: 'decision:1',
  price: 13.82, quantity: 3_800,
  timeQuality: 'daily_bar_open_proxy',
  timeSemantics: '日线 OHLCV 的开盘价代理记录边界，不代表已观测到该时刻的真实成交',
  evidence: [],
}]

const runEvidence: RunEvidence = {
  runId: 'run:mock', gitSha: 'a'.repeat(40), snapshotId: 'composite:v2',
  producerSnapshotId: `composite:${'e'.repeat(64)}`,
  snapshotChecksum: `sha256:${'b'.repeat(64)}`,
  dataSchemaVersion: 'local-parquet.market-data.v3',
  producerSnapshotSchemaVersion: 'ashare-lab.composite-research-snapshot.v2',
  catalog: `sha256:${'c'.repeat(64)}`, strategyHash: `sha256:${'d'.repeat(64)}`,
  engineVersion: '0.3.0', executionAssumptions: {
    edge_entry_validity_sessions: '3',
    event_entry_validity_sessions: '1',
    state_entry_validity_sessions: '1',
    entry_signal_validity_policy: 'edge_event_state.composite_fail_closed.retryable_day_orders.v3',
  },
}

describe('current report screen', () => {
  it('puts one conclusion and the key metrics first without showing capital or secondary tabs', () => {
    const { container } = render(<ReportScreen open onBack={vi.fn()} metrics={{
      ...metrics,
      warnings: [
        '固定样例：收益、交易与事件用于界面预览，不来自回测服务。',
        '历史表现不代表未来收益。',
      ],
    }}
      series={series} marks={[mark]} trades={trades} evidence={runEvidence}
      onOpenChain={vi.fn()} onOpenExecution={vi.fn()} mode="mock" />)

    expect(screen.queryByText('界面预览')).not.toBeInTheDocument()
    expect(screen.getByRole('heading', {
      name: '策略亏损 2.54%，同样的钱买入后一直持有亏损 39.23%，相对少亏 36.69 个百分点。',
    })).toBeInTheDocument()
    for (const label of ['策略总收益', '超额收益', '最大回撤', '完整买卖']) {
      expect(screen.getByText(label)).toBeInTheDocument()
    }
    expect(screen.queryByText('初始资金')).not.toBeInTheDocument()
    expect(screen.queryByText('期末资产')).not.toBeInTheDocument()
    expect(screen.queryByRole('tablist')).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '净值与回撤' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '每笔委托' })).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /成交规则与数据依据/ })).toHaveLength(1)
    expect(container.querySelectorAll('.report-risk .notice')).toHaveLength(0)
    expect(screen.queryByText(/风险提示/)).not.toBeInTheDocument()
    expect(screen.queryByText('proved')).not.toBeInTheDocument()
  })

  it('shows provider, time quality, source link, response hash, snapshot and Git identity', () => {
    render(<ExecutionDetailsScreen open onBack={vi.fn()} draft={draft}
      trades={trades} evidence={runEvidence} mode="live" />)

    expect(screen.getByRole('link', { name: /东方财富公告/ })).toHaveAttribute(
      'href', 'https://data.eastmoney.com/notices/detail/300059/AN202403141626765931.html',
    )
    expect(screen.getByText(/供应商记录的首次可得时间/)).toBeInTheDocument()
    expect(screen.getByText(/时间精度：秒级/)).toBeInTheDocument()
    expect(screen.getByText(/5afd37736349f7…8cebcb43/)).toBeInTheDocument()
    expect(screen.getByText(/composite:v2/)).toBeInTheDocument()
    expect(screen.getByText(`composite:${'e'.repeat(64)}`)).toBeInTheDocument()
    expect(screen.getByText('数据结构版本').closest('.grow'))
      .toHaveTextContent('local-parquet.market-data.v3')
    expect(screen.getByText('快照生成结构版本').closest('.grow'))
      .toHaveTextContent('ashare-lab.composite-research-snapshot.v2')
    expect(screen.getByText('a'.repeat(40))).toBeInTheDocument()
    expect(screen.getByText('身份已记录')).toBeInTheDocument()
    expect(screen.getByText('一次触发信号')).toBeInTheDocument()
    expect(screen.getByText('事件信号')).toBeInTheDocument()
    expect(screen.getByText('持续状态信号')).toBeInTheDocument()
    expect(screen.getByText('多个条件一起满足')).toBeInTheDocument()
    expect(screen.getByText('3 个交易日内 · 最多 3 次尝试')).toBeInTheDocument()
    expect(screen.getAllByText('1 个交易日内 · 最多 1 次尝试')).toHaveLength(3)
  })

  it('shows trusted date fallback without making estimated or unverified time tradable', () => {
    const timingTrades: TradeRow[] = [{
      ...trades[0]!,
      evidence: [
        {
          ...trades[0]!.evidence[0]!,
          timeQuality: 'date_only',
          validationStatus: 'validated',
        },
        {
          ...trades[0]!.evidence[0]!,
          id: 'AN-unverified',
          sourceEventId: 'AN-unverified',
          timeQuality: 'estimated_research_only',
          validationStatus: 'unverified',
        },
      ],
    }, trades[1]!]

    render(<ExecutionDetailsScreen open onBack={vi.fn()} draft={draft}
      trades={timingTrades} evidence={runEvidence} mode="live" />)

    expect(screen.getByText(/可信日期；按当日收盘后可得，下一交易日委托/)).toBeVisible()
    expect(screen.getByText(/估算时间（仅研究，不可交易）/)).toBeVisible()
    expect(screen.getByText(/未验证，不可交易/)).toBeVisible()
    expect(screen.queryByText(/不可用于严格回测/)).not.toBeInTheDocument()
  })

  it('passes the backend execution-time quality into the causal trace instead of claiming an exact open fill', async () => {
    const user = userEvent.setup()
    const onOpenChain = vi.fn()
    render(<ReportScreen open onBack={vi.fn()} metrics={metrics}
      series={series} marks={[mark]} trades={trades} evidence={runEvidence}
      onOpenChain={onOpenChain} onOpenExecution={vi.fn()} mode="live" />)

    expect(screen.getByRole('button', { name: /买入成交.*回车键/ })).toHaveTextContent('B')
    await user.click(screen.getByRole('button', { name: /买入 年度报告首次可得 已成/ }))
    expect(onOpenChain).toHaveBeenCalledWith(expect.objectContaining({
      id: 'fill:1',
      timeQuality: 'daily_bar_open_proxy',
      timeSemantics: expect.stringContaining('不代表已观测到该时刻的真实成交'),
    }))
    expect(screen.queryByText(/09:25/)).not.toBeInTheDocument()
  })

  it('rejects a complete-looking legacy v1 snapshot identity', () => {
    render(<ReportScreen open onBack={vi.fn()} metrics={metrics}
      series={series} marks={[mark]} trades={trades}
      evidence={{
        ...runEvidence,
        producerSnapshotSchemaVersion: 'ashare-lab.composite-research-snapshot.v1',
      }}
      onOpenChain={vi.fn()} onOpenExecution={vi.fn()} mode="live" />)

    expect(screen.getByText('身份不完整')).toBeInTheDocument()
    expect(screen.queryByText('身份已记录')).not.toBeInTheDocument()
  })

  it('rejects a producer schema accidentally reported as the market-data schema', () => {
    render(<ReportScreen open onBack={vi.fn()} metrics={metrics}
      series={series} marks={[mark]} trades={trades}
      evidence={{
        ...runEvidence,
        dataSchemaVersion: 'ashare-lab.composite-research-snapshot.v2',
      }}
      onOpenChain={vi.fn()} onOpenExecution={vi.fn()} mode="live" />)

    expect(screen.getByText('身份不完整')).toBeInTheDocument()
    expect(screen.queryByText('身份已记录')).not.toBeInTheDocument()
  })

  it.each<[string, Partial<RunEvidence>]>([
    ['snapshot ID', { snapshotId: null }],
    ['snapshot checksum', { snapshotChecksum: `sha256:${'b'.repeat(63)}` }],
    ['data schema', { dataSchemaVersion: null }],
    ['producer schema', { producerSnapshotSchemaVersion: null }],
    ['producer snapshot ID', { producerSnapshotId: null }],
    ['producer snapshot ID format', { producerSnapshotId: `choice:${'e'.repeat(64)}` }],
    ['Git SHA', { gitSha: 'a'.repeat(39) }],
    ['catalog identity', { catalog: null }],
    ['catalog identity format', { catalog: `sha256:${'c'.repeat(63)}` }],
    ['strategy identity', { strategyHash: null }],
    ['strategy identity format', { strategyHash: 'not-a-sha256-identity' }],
  ])('does not mark the run identity complete without a valid %s', (_label, override) => {
    render(<ReportScreen open onBack={vi.fn()} metrics={metrics}
      series={series} marks={[mark]} trades={trades}
      evidence={{ ...runEvidence, ...override }} onOpenChain={vi.fn()}
      onOpenExecution={vi.fn()} mode="live" />)

    expect(screen.getByText('身份不完整')).toBeInTheDocument()
    expect(screen.queryByText('身份已记录')).not.toBeInTheDocument()
  })

  it('gives an actionable explanation when there are no trades', () => {
    render(<ReportScreen open onBack={vi.fn()} metrics={{ ...metrics, trips: 0 }}
      series={series} marks={[]} trades={[]} evidence={runEvidence}
      onOpenChain={vi.fn()} onOpenExecution={vi.fn()} mode="live" />)

    expect(screen.getByRole('heading', { name: /完整买卖 0 回合/ })).toBeInTheDocument()
    expect(screen.getByText(/请延长回测区间/)).toBeInTheDocument()
  })

  it('reveals long activity lists progressively instead of rendering every row at once', async () => {
    const user = userEvent.setup()
    const manyTrades = Array.from({ length: 25 }, (_, index): TradeRow[] => ([{
      ...trades[0]!, id: `signal:${index}`, chainId: `decision:${index}`,
      title: `活动 ${index + 1}`,
    }, {
      ...trades[1]!, id: `fill:${index}`, chainId: `decision:${index}`,
      orderId: `order:${index}`,
    }])).flat()
    render(<ReportScreen open onBack={vi.fn()} metrics={metrics}
      series={series} marks={[mark]} trades={manyTrades} evidence={runEvidence}
      onOpenChain={vi.fn()} onOpenExecution={vi.fn()} mode="live" />)

    expect(screen.getByText('活动 20')).toBeInTheDocument()
    expect(screen.queryByText('活动 21')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /再看 5 笔/ }))
    expect(screen.getByText('活动 25')).toBeInTheDocument()
  })
})
