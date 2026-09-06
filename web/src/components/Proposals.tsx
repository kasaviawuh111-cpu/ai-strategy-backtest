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
};

const ariaDescription = (item: ProposalItem): string | undefined => {
  if (item.disabled && item.disabledReason) return item.disabledReason;
  const details = [
    item.instrument,
    item.entry ? `买入：${item.entry}` : undefined,
    item.exit ? `卖出：${item.exit}` : undefined,
    item.paired ? undefined : item.detail,
  ].filter(Boolean);
  return details.length ? details.join('；') : undefined;
};

const Arrow = () => (
  <svg className="proposal-go" width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden>
    <path d="M6 4l4 4-4 4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
);

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
      {items.slice(0, 3).map((item) => (
        <button
          key={item.id}
          type="button"
          className="proposal"
          aria-label={item.title}
          aria-description={ariaDescription(item)}
          onClick={() => onPick(item.id)}
          disabled={item.disabled}
        >
          <span className="proposal-text">
            <span className="proposal-heading">
              <b>{item.title}</b>
            </span>
            {item.instrument ? <span className="proposal-instrument">{item.instrument}</span> : null}
            {item.entry || item.exit ? (
              <span className="proposal-conditions">
                {item.entry ? (
                  <span className="proposal-condition">
                    <small>买入</small>
                    <span>{item.entry}</span>
                  </span>
                ) : null}
                {item.exit ? (
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
          <Arrow />
        </button>
      ))}
    </div>
  );
}
