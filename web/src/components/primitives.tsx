/* eslint-disable react-refresh/only-export-components -- shared visual primitives and formatters */
/** 基础件：气泡、chip、行、分组、提示、状态徽章、形状图元 */
import { useId, useState } from 'react';
import type { ReactNode } from 'react';
import type { ActivityKind, StatusWord } from '../types';
import type { DialogueProgressEvent } from '../shared/api/client';
import { ModelReasoning } from './ModelReasoning';

/* ---------- 数字格式 ---------- */
export const fmtPct = (v: number | null) => v == null ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(2)}%`;
export const signClass = (v: number | null) => v == null ? '' : (v > 0 ? 'up' : v < 0 ? 'down' : '');
export const fmtCny = (v: number | null) => v == null
  ? '—'
  : new Intl.NumberFormat('zh-CN', {
      style: 'currency', currency: 'CNY', maximumFractionDigits: 2,
    }).format(v);

/* ---------- 图标 ---------- */
export const Chevron = () => (
  <svg className="chev" width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden>
    <path d="M6 4l4 4-4 4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
);
export const Caret = () => (
  <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden>
    <path d="M4 6l4 4 4-4" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
);
export const BackIcon = () => (
  <svg width="24" height="24" viewBox="0 0 24 24" fill="none" aria-hidden>
    <path d="M15 5l-7 7 7 7" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
);

/**
 * 活动形状图元（SPEC 4.9）
 * 信号 / 委托 / 成交 / 部分成交 / 未成交 / 过期 形状与文字都不同，绝不只靠颜色区分。
 */
export function Glyph({ kind }: { kind: ActivityKind | 'utterance' | 'normalized' | 'cash' }) {
  const label: Record<string, string> = {
    signal: '信号', order: '委托', fill: '成交', partial_fill: '部分成交',
    no_fill: '未成交', expired: '订单过期', utterance: '原话', normalized: '规范化', cash: '资金',
  };
  const shape: Record<string, ReactNode> = {
    signal: <path d="M7 .8 13.2 7 7 13.2.8 7z" fill="none" stroke="currentColor" strokeWidth="1.5" />,
    order: <rect x=".9" y=".9" width="11.2" height="11.2" rx="1.6" fill="none" stroke="currentColor" strokeWidth="1.5" />,
    fill: <circle cx="6.5" cy="6.5" r="5.6" fill="currentColor" />,
    partial_fill: <>
      <circle cx="6.5" cy="6.5" r="5.6" fill="none" stroke="currentColor" strokeWidth="1.5" />
      <path d="M6.5.9a5.6 5.6 0 010 11.2z" fill="currentColor" />
    </>,
    no_fill: <>
      <circle cx="6.5" cy="6.5" r="5.6" fill="none" stroke="currentColor" strokeWidth="1.5" />
      <path d="M4 4l5 5M9 4l-5 5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </>,
    expired: <circle cx="6.5" cy="6.5" r="5.6" fill="none" stroke="currentColor" strokeWidth="1.5" strokeDasharray="2.4 2.2" />,
    cash: <path d="M6.5 1v11M3 4.2h7M3 7h7" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />,
    utterance: <path d="M2 10.5h9M2 7.5h9M2 4.5h6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />,
  };
  shape.normalized = shape.utterance;
  const box = kind === 'signal' ? 14 : 13;
  return (
    <svg width={box} height={box} viewBox={`0 0 ${box} ${box}`} role="img" aria-label={label[kind] ?? ''}>
      {shape[kind] ?? shape.fill}
    </svg>
  );
}

const WarnIcon = () => (
  <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden>
    <path d="M7 1.2 13.2 12.4H.8z" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round" />
    <path d="M7 5.4v3.1M7 10.3h.01" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
  </svg>
);
const InfoIcon = () => (
  <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden>
    <circle cx="7" cy="7" r="6.2" stroke="currentColor" strokeWidth="1.3" />
    <path d="M7 6.2v4M7 4.1h.01" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
  </svg>
);

/* ---------- 提示条 ---------- */
export function Notice({ tone = 'warn', children }: { tone?: 'warn' | 'info'; children: ReactNode }) {
  return (
    <div className={`notice${tone === 'info' ? ' n-info' : ''}`}>
      {tone === 'info' ? <InfoIcon /> : <WarnIcon />}
      <span>{children}</span>
    </div>
  );
}

/* ---------- 状态徽章（SPEC 1.2 状态词） ---------- */
const TAG_CLASS: Record<StatusWord, string> = {
  proved: 't-impl', implemented: 't-impl', partial: 't-mock', mock_only: 't-mock',
  research_only: 't-target', unavailable: 't-mock', target: 't-target', rejected: 't-reject',
};
export const Tag = ({ status, children }: { status: StatusWord; children?: ReactNode }) => (
  <span className={`tag ${TAG_CLASS[status]}`}>{children ?? status}</span>
);

/* ---------- 对话 ---------- */
/** 用户消息保留靠右的浅色气泡。 */
export const Bubble = ({ children }: { children: ReactNode }) => (
  <div className="bubble me"><p>{children}</p></div>
);

/** 系统回答直接排版，不再套一层灰色聊天气泡。 */
export const Say = ({ children }: { children: ReactNode }) => (
  <div className="say"><p>{typeof children === 'string' ? replyLinks(children) : children}</p></div>
);

/** Link source text without interpreting HTML or non-web URL schemes. */
function replyLinks(text: string): ReactNode[] {
  const links = /\[([^\]\n]+)\]\((https?:\/\/[^\s<>"')]+)\)|(https?:\/\/[^\s<>"'\])（）【】。，；！？、]+)/g;
  const parts: ReactNode[] = [];
  let cursor = 0;
  for (const match of text.matchAll(links)) {
    const raw = match[2] ?? match[3];
    if (!raw) continue;
    const url = match[2] ? raw : raw.replace(/[.,;!?]+$/, '');
    parts.push(text.slice(cursor, match.index));
    parts.push(<a key={match.index} href={url} target="_blank" rel="noreferrer noopener">
      {match[1] ?? url}
    </a>);
    if (!match[2]) parts.push(raw.slice(url.length));
    cursor = match.index + match[0].length;
  }
  parts.push(text.slice(cursor));
  return parts;
}

/** 一个回合：用户靠右，AI 靠左；AI 回合内可叠加折叠头、气泡、chips、卡片 */
/** `id` 只用于左栏点历史时的锚点跳转（scrollIntoView），不参与样式。 */
export const Turn = ({ mine, id, children }: { mine?: boolean; id?: string; children: ReactNode }) => (
  <div className={`turn${mine ? ' me' : ''}`} id={id}>{children}</div>
);

export const DayDivider = ({ children }: { children: ReactNode }) => (
  <div className="daydiv">{children}</div>
);

/**
 * One process disclosure replaces repeated task, status and reasoning headings.
 */
export function ThinkingStream(
  { status = '处理中', label = '处理进度', action, summary = [], recovery }:
  {
    status?: ReactNode;
    label?: string;
    action?: ReactNode;
    summary?: readonly DialogueProgressEvent[];
    recovery?: import('../shared/api/client').PreviewPollRecovery | null;
  },
) {
  return (
    <div className="thinking-stream" aria-label={label}>
      <ModelReasoning events={summary} active
        fallbackStatus={status} progressLabel={label} recovery={recovery} />
      {action ? <div className="thinking-action" aria-label={typeof status === 'string' ? status : undefined}>{action}</div> : null}
    </div>
  );
}

/**
 * 已完成处理记录的折叠头。
 *
 * 默认标题是「处理记录」；历史策略等不是运行记录的内容必须显式覆盖标题，
 * 避免把事后摘要冒充成模型思考或服务执行步骤。
 */
export const THINK_TITLE = '处理记录';

export function ThinkBlock(
  { meta, lines, title = THINK_TITLE }:
  { meta: string; lines: string[]; title?: ReactNode },
) {
  const [open, setOpen] = useState(false);
  const bodyId = useId();
  return (
    <div className="thinkwrap">
      <button
        type="button"
        className="think"
        aria-expanded={open}
        aria-controls={bodyId}
        onClick={() => setOpen((v) => !v)}
      >
        <span className="think-title">{title}</span>
        <span className="meta">{meta}<Caret /></span>
      </button>
      <div id={bodyId} className={`thinkbody${open ? ' open' : ''}`} hidden={!open}>
        {lines.map((l) => <div key={l}>{l}</div>)}
      </div>
    </div>
  );
}

/* ---------- Chip ---------- */
export function Chip(
  { children, onClick, disabled, pin, title }:
  { children: ReactNode; onClick?: () => void; disabled?: boolean; pin?: string; title?: string },
) {
  return (
    <button type="button" className="chip" onClick={onClick} disabled={disabled} title={title}>
      {pin ? <span className="pin">{pin}</span> : null}
      {children}
    </button>
  );
}
export const Chips = ({ children }: { children: ReactNode }) => <div className="chips">{children}</div>;

/* ---------- 分组与行（二级页主结构） ---------- */
export const Section = ({ title, aside, children }:
  { title: string; aside?: ReactNode; children: ReactNode }) => (
  <>
    <div className="sect-h"><h2>{title}</h2>{aside ? <em>{aside}</em> : null}</div>
    <div className="group">{children}</div>
  </>
);

export function Row(
  { n, label, sub, value, wrap, onClick }:
  { n?: number; label: ReactNode; sub?: ReactNode; value?: ReactNode; wrap?: boolean; onClick?: () => void },
) {
  const inner = (
    <>
      {n ? <span className="n">{n}</span> : null}
      <span className="k">{label}{sub ? <small>{sub}</small> : null}</span>
      {value != null ? <span className={`v${wrap ? ' wrap' : ''}`}>{value}</span> : null}
      {onClick ? <Chevron /> : null}
    </>
  );
  return onClick
    ? <button type="button" className="grow" onClick={onClick}>{inner}</button>
    : <div className="grow">{inner}</div>;
}
