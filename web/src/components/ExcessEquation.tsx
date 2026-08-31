/**
 * 超额收益等式。
 *
 * 「相对同期持有 +36.69%」是一个结论，用户没法验算，也不知道它和上面的总收益是什么关系。
 * 拆成等式之后，三个数字互相解释：超额 = 策略收益 − 买入后一直持有的收益。
 * 术语保留（超额收益、买入持有是通用说法），但每个词下面给一行不需要背景知识的解释。
 */
import { fmtPct, signClass } from './primitives'

/** 买入持有基准的统一叫法，图例、KPI、等式必须一致，避免同一条线三种称呼。 */
export const BENCHMARK_LABEL = '买入后一直持有'

export function ExcessEquation(
  { total, bench, excess, compact = false }:
  { total: number | null; bench: number | null; excess: number | null; compact?: boolean },
) {
  // 三个数缺一个，等式就不成立，退回单个数字，不拿「—」凑格式。
  if (total == null || bench == null || excess == null) {
    return (
      <div className="equation equation--single">
        <div className="eq-lead">
          <b className={signClass(excess)}>{fmtPct(excess)}</b>
          <span>超额收益<small>本次没有完整记录对照收益，无法拆解</small></span>
        </div>
      </div>
    )
  }

  return (
    <div className={`equation${compact ? ' equation--compact' : ''}`}
      role="group" aria-label={`超额收益 ${fmtPct(excess)}，等于策略收益 ${fmtPct(total)} 减去${BENCHMARK_LABEL} ${fmtPct(bench)}`}>
      <div className="eq-lead">
        <b className={signClass(excess)}>{fmtPct(excess)}</b>
        <span>超额收益{compact ? null : <small>比一直持有多出来的部分</small>}</span>
      </div>
      <div className="eq-body" aria-hidden="true">
        <i className="eq-op">=</i>
        <div className="eq-term">
          <b className={signClass(total)}>{fmtPct(total)}</b>
          <span>策略收益</span>
        </div>
        <i className="eq-op">−</i>
        <div className="eq-term">
          <b className={signClass(bench)}>{fmtPct(bench)}</b>
          <span>{BENCHMARK_LABEL}</span>
        </div>
      </div>
    </div>
  )
}
