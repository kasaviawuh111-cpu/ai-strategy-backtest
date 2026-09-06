import type { BacktestOptimizationCandidate, BacktestReviewResponse } from '../shared/api/types'
import type { DialogueProgressEvent } from '../shared/api/client'
import { ModelReasoning } from './ModelReasoning'

type BacktestReviewProps = {
  review?: BacktestReviewResponse
  progress?: readonly DialogueProgressEvent[]
  describeCandidate?: (candidate: BacktestOptimizationCandidate) => string
  isLoading?: boolean
  error?: string
  onRequest: () => void
  onRunCandidate: (candidate: BacktestOptimizationCandidate) => void
  onChangeInstrument?: () => void
  onChangeRules?: () => void
  runningCandidateId?: BacktestOptimizationCandidate['id']
  runError?: string
  candidateActionsDisabled?: boolean
}

// Older reports may still contain the former long-form model response.
// Only shorten its display; never alter an optimization's executable strategy.
const shortSentence = (text: string): string => {
  const sentence = text.trim().match(/^[^。！？\n]+[。！？]?/u)?.[0] ?? text.trim()
  return sentence.length > 56 ? `${sentence.slice(0, 55)}…` : sentence
}

export function BacktestReview({
  review, progress = [], describeCandidate, isLoading = false, error,
  onRequest, onRunCandidate, onChangeInstrument, onChangeRules, runningCandidateId, runError,
  candidateActionsDisabled = false,
}: BacktestReviewProps) {
  const disabled = candidateActionsDisabled || Boolean(runningCandidateId)
  return (
    <section className="backtest-review" aria-labelledby="backtest-review-title">
      <div className="backtest-review__head">
        <h2 id="backtest-review-title">AI 分析与优化</h2>
        {!review && !error ? (
          <button type="button" onClick={onRequest} disabled={isLoading}>
            {isLoading ? 'AI 正在分析' : 'AI 分析与优化'}
          </button>
        ) : null}
      </div>
      {!review && !error ? (
        <p className="backtest-review__status" role="status">
          {isLoading
            ? '我正在分析这次回测，并准备优化建议。你可以先试试下面的问题。'
            : '回测已完成，可以开始 AI 分析，也可以先调整条件或换只股票继续。'}
        </p>
      ) : null}
      <ModelReasoning events={progress} active={isLoading} />
      {error ? (
        <div className="backtest-review__error" role="alert">
          <p>{error}</p>
          <button type="button" onClick={onRequest} disabled={isLoading}>重试 AI 分析</button>
        </div>
      ) : null}
      {!review && (onChangeRules || onChangeInstrument) ? (
        <div className="backtest-review__next">
          <div className="backtest-review__chips" role="group" aria-label="推荐问题">
            {onChangeRules ? (
              <button className="backtest-review__chip" type="button"
                onClick={onChangeRules} disabled={disabled}>换个条件再回测</button>
            ) : null}
            {onChangeInstrument ? (
              <button className="backtest-review__chip" type="button"
                onClick={onChangeInstrument} disabled={disabled}>换只股票试试</button>
            ) : null}
          </div>
        </div>
      ) : null}
      {review ? (
        <div className="backtest-review__result">
          <div className="backtest-review__copy" aria-label="AI 简短结论">
            <p>{shortSentence(review.analysis)}</p>
            <p>{shortSentence(review.conclusion)}</p>
          </div>
          <div>
            <h3>待验证的优化候选</h3>
            <div className="backtest-review__chips" role="group" aria-label="继续验证">
              {review.optimizationCandidates.slice(0, 3).map((candidate) => (
                <button
                  className="backtest-review__chip"
                  key={candidate.id}
                  type="button"
                  title={describeCandidate?.(candidate) ?? candidate.suggestedUtterance}
                  aria-description={`点击重新回测。${candidate.tradeoff}`}
                  onClick={() => onRunCandidate(candidate)}
                  disabled={disabled}
                >
                  {runningCandidateId === candidate.id ? '正在创建优化回测' : candidate.title}
                </button>
              ))}
              {onChangeInstrument ? (
                <button className="backtest-review__chip" type="button"
                  onClick={onChangeInstrument} disabled={disabled}>换只股票试试</button>
              ) : null}
            </div>
          </div>
        </div>
      ) : null}
      {runError ? <p className="backtest-review__error" role="alert">{runError}</p> : null}
    </section>
  )
}
