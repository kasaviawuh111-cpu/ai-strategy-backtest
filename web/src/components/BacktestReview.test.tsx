import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import type {
  BacktestOptimizationCandidate,
  BacktestReviewResponse,
  StrategySpec,
} from '../shared/api/types'
import { BacktestReview } from './BacktestReview'

const strategy: StrategySpec = {
  schema_version: 'strategy.v1',
  catalog: { catalog_id: 'cn_a.signals', release_version: '2026.09.05' },
  instrument: { market: 'CN_A', symbol: '300059.SZ', position_mode: 'long_only' },
  entry: {
    type: 'indicator_condition',
    indicator_id: 'technical.macd',
    definition_version: '1.0.0',
    params: { fast: 8, slow: 21, signal: 5 },
    timeframe: '1d',
    evaluation_mode: 'bar_close_confirmed',
    trigger: 'golden_cross',
    value: null,
  },
  exit: {
    op: 'first_of',
    children: [{
      type: 'indicator_condition',
      indicator_id: 'technical.macd',
      definition_version: '1.0.0',
      params: { fast: 8, slow: 21, signal: 5 },
      timeframe: '1d',
      evaluation_mode: 'bar_close_confirmed',
      trigger: 'death_cross',
      value: null,
    }],
  },
  execution: {
    timezone: 'Asia/Shanghai',
    entry_policy: 'next_tradable_session_open',
    exit_policy: 'next_tradable_session_open',
    data_capability: 'daily_ohlcv',
    execution_resolution: '1d',
    evaluation_frequency: '1d_close',
    position_policy: 'single_position_no_pyramiding',
    t_plus_one: true,
  },
  backtest: { start: '2023-01-01', end: '2026-09-05', initial_cash_cny: 1_000_000 },
}

const candidate = (
  id: BacktestOptimizationCandidate['id'],
  title: string,
): BacktestOptimizationCandidate => ({
  id,
  title,
  diagnosis: '原参数在震荡区间的信号偏慢。',
  changeDimension: 'confirmation',
  expectedEffect: '检验更快参数是否能更早确认趋势。',
  tradeoff: '触发次数可能增加，交易成本与假信号也可能上升。',
  suggestedUtterance: `东方财富${title}买入，MACD 死叉卖出，回测近 3 年`,
  strategy,
  strategyHash: `sha256:${id === 'model-opt-1' ? 'a'.repeat(64) : 'b'.repeat(64)}`,
  modelSuggested: true,
})

const candidates: [BacktestOptimizationCandidate, BacktestOptimizationCandidate] = [
  candidate('model-opt-1', 'MACD 快线确认'),
  candidate('model-opt-2', '增加止损约束'),
]

const review: BacktestReviewResponse = {
  runId: 'run:review:1',
  sourceResultHash: `sha256:${'c'.repeat(64)}`,
  generatedAt: '2026-09-05T12:00:00Z',
  evidenceGrade: 'limited',
  evidenceReasons: ['有效交易样本只有 7 笔', '参数稳健性还需要额外窗口验证'],
  analysis: '收益主要来自少数趋势区间，震荡期出现多次往返交易。',
  conclusion: '现有样本不足以证明策略稳定，先分别验证确认参数和止损约束。',
  optimizationCandidates: candidates,
  modelProvenance: {
    provider: 'deepseek',
    model: 'deepseek-v4-pro',
    promptVersion: 'backtest-review.prompt.v1',
    schemaVersion: 'backtest-review.v1',
    responseHash: `sha256:${'d'.repeat(64)}`,
  },
  disclaimer: '历史回测与模型建议仅用于研究，不构成投资建议或真实交易指令',
}

describe('BacktestReview', () => {
  it('shows two brief lines and working candidate chips without model provenance', async () => {
    const user = userEvent.setup()
    const firstCandidate = candidates[0] as BacktestOptimizationCandidate
    const onRequest = vi.fn()
    const onRunCandidate = vi.fn()
    const onChangeInstrument = vi.fn()
    const onChangeRules = vi.fn()
    const { rerender } = render(
      <BacktestReview onRequest={onRequest} onRunCandidate={onRunCandidate} />,
    )

    await user.click(screen.getByRole('button', { name: 'AI 分析与优化' }))
    expect(onRequest).toHaveBeenCalledOnce()

    rerender(
      <BacktestReview
        isLoading
        onRequest={onRequest}
        onRunCandidate={onRunCandidate}
        onChangeInstrument={onChangeInstrument}
        onChangeRules={onChangeRules}
      />,
    )
    expect(screen.getByText(/我正在分析这次回测，并准备优化建议/)).toBeVisible()
    expect(screen.getByRole('button', { name: 'AI 正在分析' })).toBeDisabled()
    expect(screen.getByRole('group', { name: '推荐问题' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '换个条件再回测' }))
    await user.click(screen.getByRole('button', { name: '换只股票试试' }))
    expect(onChangeRules).toHaveBeenCalledOnce()
    expect(onChangeInstrument).toHaveBeenCalledOnce()
    expect(onRunCandidate).not.toHaveBeenCalled()
    onChangeInstrument.mockClear()

    rerender(
      <BacktestReview isLoading onRequest={onRequest} onRunCandidate={onRunCandidate}
        progress={[{ stage: 'model_reasoning', message: '模型仍在生成。', elapsedMs: 23000 }]} />,
    )
    expect(screen.getByText(/你可以先试试下面的问题/)).toBeVisible()
    expect(screen.getByRole('status', { name: '处理进度' })).toHaveTextContent('模型仍在生成。')
    expect(screen.getAllByText('处理过程')).toHaveLength(1)
    expect(screen.getByLabelText('处理过程内容')).toContainElement(screen.getByRole('status', { name: '处理进度' }))
    expect(screen.queryByText('模型思考')).not.toBeInTheDocument()
    expect(screen.queryByText('你可以接着问')).not.toBeInTheDocument()

    rerender(
      <BacktestReview
        error="深度模型暂时不可用"
        onRequest={onRequest}
        onRunCandidate={onRunCandidate}
      />,
    )
    expect(screen.getByRole('alert')).toHaveTextContent('深度模型暂时不可用')
    await user.click(screen.getByRole('button', { name: '重试 AI 分析' }))
    expect(onRequest).toHaveBeenCalledTimes(2)

    rerender(
      <BacktestReview
        review={review}
        describeCandidate={() => '由模型结构化策略对应的实际规则'}
        onRequest={onRequest}
        onRunCandidate={onRunCandidate}
        onChangeInstrument={onChangeInstrument}
      />,
    )
    expect(screen.getAllByTitle('由模型结构化策略对应的实际规则')).toHaveLength(2)
    expect(screen.queryByText(firstCandidate.suggestedUtterance)).not.toBeInTheDocument()
    expect(screen.getByText(review.analysis)).toBeInTheDocument()
    expect(screen.getByText(review.conclusion)).toBeInTheDocument()
    expect(screen.queryByText(/模型 provenance/)).not.toBeInTheDocument()
    expect(screen.queryByText(review.modelProvenance.responseHash)).not.toBeInTheDocument()
    expect(screen.queryByText(firstCandidate.diagnosis)).not.toBeInTheDocument()
    expect(screen.getByLabelText('AI 简短结论').querySelectorAll('p')).toHaveLength(2)

    await user.click(screen.getByRole('button', { name: firstCandidate.title }))
    expect(onRunCandidate).toHaveBeenCalledWith(firstCandidate)
    await user.click(screen.getByRole('button', { name: '换只股票试试' }))
    expect(onChangeInstrument).toHaveBeenCalledOnce()

    rerender(
      <BacktestReview
        review={review}
        runningCandidateId="model-opt-1"
        runError="新回测任务创建失败"
        onRequest={onRequest}
        onRunCandidate={onRunCandidate}
      />,
    )
    expect(screen.getByRole('button', { name: '正在创建优化回测' })).toBeDisabled()
    expect(screen.getByRole('alert')).toHaveTextContent('新回测任务创建失败')
  })
})
