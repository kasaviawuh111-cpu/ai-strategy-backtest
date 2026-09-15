import './usage-guide.css'

/** User-facing names, checked against capabilities and the Spec. Generic numeric
 * operators do not imply support for every financial metric. */
export function UsageGuide() {
  return <div className="usage-guide-entry">
    <button type="button" className="usage-guide-trigger" popoverTarget="usage-guide">
      <svg width="17" height="17" viewBox="0 0 18 18" fill="none" aria-hidden="true">
        <circle cx="9" cy="9" r="6.5" stroke="currentColor" strokeWidth="1.4" />
        <path d="M7.2 6.6a1.8 1.8 0 0 1 3.6 0c0 1.3-1.8 1.4-1.8 2.9M9 12h.01" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
      </svg>
      使用说明
    </button>
    <section id="usage-guide" className="usage-guide" popover="auto" aria-labelledby="usage-guide-title"
      onKeyDown={event => { if (event.key === 'Escape') event.stopPropagation() }}>
      <header className="usage-guide-header">
        <h2 id="usage-guide-title">使用说明</h2>
        <button type="button" className="usage-guide-close" popoverTarget="usage-guide" popoverTargetAction="hide" aria-label="关闭使用说明">
          <svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true">
            <path d="m5 5 10 10M15 5 5 15" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
          </svg>
        </button>
      </header>
      <h3>哪些股票</h3>
      <p>用于验证单只沪深 A 股的历史买卖规则。</p>
      <h3>怎么交易</h3>
      <p>目前支持单个交易策略单独使用。包括网格、定投、到价买卖、止盈止损、反弹买入与回落卖出，也可以设定持有几天后卖出。</p>
      <p><strong>技术指标条件单</strong> 也可以用。把指标条件说清楚，例如“20 日均线上穿 60 日均线时买入”，系统会按你的规则检查买卖时点。</p>
      <h3>指标与条件</h3>
      <p>支持行情、技术、财务、资金四类指标，直接说出你的买卖条件。</p>
      <div className="usage-guide-indicators">
        <p><strong>行情量价</strong> 股价、涨跌幅、区间新高、成交量、成交额、换手率等。</p>
        <p><strong>技术指标</strong> 均线 MA / EMA、MACD、RSI、KDJ、布林带、ATR 等。</p>
        <p><strong>财务估值</strong> 财务、盈利与估值指标，如市盈率 PE、净资产收益率 ROE 等。</p>
        <p><strong>资金流向</strong> 主力、超大单、大单净流入金额等。</p>
      </div>
      <p className="usage-guide-note">支持接入东方财富选股、查数中的更多数值指标，历史数据校验通过后即可用于回测。</p>
      <p className="usage-guide-note">暂不支持港美股、北交所、基金、多股组合、做空、退市股或实盘下单。</p>
    </section>
  </div>
}
