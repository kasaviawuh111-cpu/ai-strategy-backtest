export type ProposalItem = {
  id: string;
  title: string;
  detail?: string;
  disabled?: boolean;
  disabledReason?: string;
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
    <div className="proposals" role="group" aria-label="可选规则">
      {items.slice(0, 3).map((item) => (
        <button
          key={item.id}
          type="button"
          className="proposal"
          onClick={() => onPick(item.id)}
          disabled={item.disabled}
        >
          <span className="proposal-text">
            <b>{item.title}</b>
            {item.disabled && item.disabledReason
              ? <span>{item.disabledReason}</span>
              : item.detail ? <span>{item.detail}</span> : null}
          </span>
          <Arrow />
        </button>
      ))}
    </div>
  );
}
