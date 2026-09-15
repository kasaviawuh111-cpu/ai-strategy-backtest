export type ProposalItem = {
  id: string;
  title: string;
  detail?: string;
  instrument?: string;
  entry?: string;
  exit?: string;
  disabled?: boolean;
  disabledReason?: string;
  paired?: boolean;
  gridFacts?: Array<{ label: string; value: string }>;
  independentGridSide?: 'entry' | 'exit';
};

const ariaDescription = (item: ProposalItem): string | undefined => {
  if (item.disabled && item.disabledReason) return item.disabledReason;
  const details = [
    item.instrument,
    ...(item.gridFacts?.map(fact => `${fact.label}：${fact.value}`) ?? []),
    item.entry ? `买入：${item.entry}` : undefined,
    item.exit ? `卖出：${item.exit}` : undefined,
    item.detail,
  ].filter(Boolean);
  return details.length ? details.join('；') : undefined;
};

const Arrow = () => (
  <svg className="proposal-go" width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden>
    <path d="M6 4l4 4-4 4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
);

// The stock already has its own line. Models may prefix it more than once.
function proposalHeading(item: ProposalItem): string {
  const stock = item.instrument?.split(/\s*[·・]\s*/)[0]?.trim();
  if (!stock) return item.title;
  let title = item.title.trim();
  while (title.startsWith(stock)) {
    const rest = title.slice(stock.length);
    if (!/^\s*[·・:：]/.test(rest)) break;
    const next = rest.replace(/^\s*[·・:：]\s*/, '');
    if (!next) break;
    title = next;
  }
  return title;
}

// Keep the comparison focused; the existing review page contains the full plan.
const gridComparisonFacts = new Set(['下跌多少买入', '上涨多少卖出', '价格区间', '每格委托']);

export function Proposals(
  { items, onPick }:
  { items: ProposalItem[]; onPick: (id: string) => void },
) {
  if (items.length === 0) return null;
  return (
    <div
      className="proposals"
      role="group"
      aria-label={items.some((item) => item.paired) ? '股票与策略组合' : '可选规则'}
    >
      {items.slice(0, 3).map((item) => {
        const hasStrategy = Boolean(item.gridFacts || item.entry || item.exit || item.paired);
        return (
        <button
          key={item.id}
          type="button"
          className={`proposal${hasStrategy ? ' proposal-strategy' : ''}${item.gridFacts ? ' proposal-grid' : ''}`}
          aria-label={item.title}
          aria-description={ariaDescription(item)}
          onClick={() => onPick(item.id)}
          disabled={item.disabled}
        >
          <span className="proposal-text">
            <span className="proposal-heading">
              <b>{proposalHeading(item)}</b>
              {!hasStrategy ? <Arrow /> : null}
            </span>
            {item.instrument ? <span className="proposal-instrument">{item.instrument}</span> : null}
            {item.paired && item.detail ? <span className="proposal-rationale">{item.detail}</span> : null}
            {item.gridFacts ? <span className="proposal-grid-facts">
              {item.gridFacts.filter(fact => gridComparisonFacts.has(fact.label)).map(fact => <span className="proposal-grid-fact" key={fact.label}>
                <small>{fact.label}</small><span>{fact.value}</span>
              </span>)}
            </span> : null}
            {(!item.gridFacts || item.independentGridSide) && (item.entry || item.exit) ? (
              <span className="proposal-conditions">
                {item.entry && (!item.gridFacts || item.independentGridSide === 'entry') ? (
                  <span className="proposal-condition">
                    <small>买入</small>
                    <span>{item.entry}</span>
                  </span>
                ) : null}
                {item.exit && (!item.gridFacts || item.independentGridSide === 'exit') ? (
                  <span className="proposal-condition">
                    <small>卖出</small>
                    <span>{item.exit}</span>
                  </span>
                ) : null}
              </span>
            ) : null}
            {item.disabled && item.disabledReason
              ? <span className="proposal-detail">{item.disabledReason}</span>
              : !item.paired && item.detail ? <span className="proposal-detail">{item.detail}</span> : null}
          </span>
          {hasStrategy ? <span className="proposal-action">查看并修改参数 <Arrow /></span> : null}
        </button>
        );
      })}
    </div>
  );
}
