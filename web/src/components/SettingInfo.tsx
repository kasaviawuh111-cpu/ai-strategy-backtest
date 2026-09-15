import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'

/** One anchored, dismissible explanation. Portals avoid scroll-container clipping. */
export function SettingInfo({ label, children, help }: { label: string; children?: ReactNode; help?: string }) {
  const [open, setOpen] = useState(false)
  const id = useId()
  const button = useRef<HTMLButtonElement>(null)
  const panel = useRef<HTMLDivElement>(null)
  const [position, setPosition] = useState({ left: 0, top: 0 })
  const updatePosition = useCallback(() => {
    if (!button.current || !panel.current) return
    const anchor = button.current.getBoundingClientRect()
    const row = button.current.closest('.setting-row, .grow')?.getBoundingClientRect() ?? anchor
    const below = window.innerHeight - row.bottom - 16
    panel.current.style.maxHeight = `${Math.max(80, Math.max(below, row.top - 20))}px`
    const bounds = panel.current.getBoundingClientRect()
    // Prefer below the entire row so the explanation never covers its input.
    const top = below >= bounds.height || row.top < bounds.height + 16
      ? row.bottom + 8 : row.top - bounds.height - 8
    setPosition({
      left: Math.max(12, Math.min(anchor.left, window.innerWidth - bounds.width - 12)),
      top: Math.max(12, Math.min(top, window.innerHeight - bounds.height - 12)),
    })
  }, [])
  useLayoutEffect(() => {
    if (!open) return
    updatePosition()
    panel.current?.focus({ preventScroll: true })
  }, [open, help, updatePosition])
  useEffect(() => {
    if (!open) return
    const outside = (event: Event) => {
      if (event.target instanceof Node && !panel.current?.contains(event.target)
        && !button.current?.contains(event.target)) setOpen(false)
    }
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        event.stopImmediatePropagation()
        setOpen(false)
        button.current?.focus({ preventScroll: true })
      }
    }
    const another = (event: Event) => {
      if ((event as CustomEvent<string>).detail !== id) setOpen(false)
    }
    const reposition = (event: Event) => {
      // Section navigation and focus can still be scrolling after the click.
      // Keep the explanation anchored; a scroll is not a dismissal gesture.
      if (!(event.target instanceof Node && panel.current?.contains(event.target))) updatePosition()
    }
    document.addEventListener('pointerdown', outside, true)
    document.addEventListener('focusin', outside)
    document.addEventListener('keydown', escape, true)
    window.addEventListener('setting-info-open', another)
    window.addEventListener('scroll', reposition, true)
    window.addEventListener('resize', reposition)
    return () => {
      document.removeEventListener('pointerdown', outside, true)
      document.removeEventListener('focusin', outside)
      document.removeEventListener('keydown', escape, true)
      window.removeEventListener('setting-info-open', another)
      window.removeEventListener('scroll', reposition, true)
      window.removeEventListener('resize', reposition)
    }
  }, [open, id, updatePosition])
  return <span className="setting-info">
    <span className="setting-info-heading"><span className="setting-info-title">{children ?? label}</span><button ref={button} type="button"
      className="setting-info-button" aria-label={`了解${label}`} aria-expanded={open}
      aria-controls={open ? id : undefined} aria-haspopup="dialog" onClick={event => {
        event.preventDefault()
        if (!open) window.dispatchEvent(new CustomEvent('setting-info-open', { detail: id }))
        setOpen(value => !value)
      }}>
      <svg viewBox="0 0 20 20" width="18" height="18" fill="none" aria-hidden="true">
        <circle cx="10" cy="10" r="7.5" stroke="currentColor" strokeWidth="1.4" />
        <path d="M10 9v5" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
        <circle cx="10" cy="6" r=".9" fill="currentColor" />
      </svg>
    </button></span>
    {open ? createPortal(<div ref={panel} id={id} className="setting-info-popover"
      role="dialog" aria-modal="false" aria-label={`${label}说明`} tabIndex={-1}
      style={{ left: position.left, top: position.top }}>
      <div className="setting-info-popover-heading"><strong>{label}</strong>
        <button type="button" aria-label="关闭说明" onClick={() => { setOpen(false); button.current?.focus() }}>
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="m4 4 8 8M12 4l-8 8" stroke="currentColor" strokeWidth="1.5" /></svg>
        </button>
      </div>
      <p>{help ?? settingHelp(label)}</p>
    </div>, document.body) : null}
  </span>
}

const explanations: Record<string, string> = {
  快线: 'MACD 快速 EMA 的周期，与慢线一起计算 DIF。修改后会改变指标数值和交叉时机。',
  慢线: 'MACD 慢速 EMA 的周期，通常应大于快线周期；具体范围由当前指标约束。',
  信号线: '对 MACD 的 DIF 继续平滑得到信号线的周期，影响金叉、死叉确认。',
  快线周期: '较短移动平均线的观察周期。与慢线比较产生交叉或相对高低条件。',
  慢线周期: '较长移动平均线的观察周期。快慢线的周期关系须满足当前指标要求。',
  标准差倍数: '布林带中，上下轨距离中轨的标准差倍数；越大轨道通常越宽。不是涨跌幅百分比。',
  计算常数: 'CCI 计算分母中的缩放常数，影响指标数值大小；不是交易价格或手续费。',
  第一均线周期: 'BBI 四条简单移动平均线中第一条的观察周期；四条均线平均形成 BBI。',
  第二均线周期: 'BBI 四条简单移动平均线中第二条的观察周期；应满足四个周期依次递增的约束。',
  第三均线周期: 'BBI 四条简单移动平均线中第三条的观察周期；应满足四个周期依次递增的约束。',
  第四均线周期: 'BBI 四条简单移动平均线中第四条的观察周期，也是四者中最长的窗口。',
  连续天数: '要求条件连续成立的交易日数量；中间一次不满足会打断连续计数。具体是否排除无成交日，以指标口径为准。',
  成交量倍数: '当前成交量相对于前序基准成交量的倍数。例如 2 表示两倍，不是 2%。',
  左侧确认根数: '判断价格局部高低点时向左比较的 K 线根数。它与右侧确认窗口共同定义拐点。',
  右侧确认根数: '拐点之后需要等待的 K 线根数。必须等这些数据出现才能确认，不能把信号回填到拐点当天。',
  最小间隔根数: '参与背离比较的两个价格拐点至少相隔的 K 线根数。',
  最大间隔根数: '参与背离比较的两个价格拐点最多相隔的 K 线根数。超过此范围不组成该背离。',
  'OBV 变化阈值': '两个拐点间 OBV 反向变化的最低幅度，以基准平均成交量的倍数衡量；不是百分比，也不是直接填写股数。',
  平均成交量周期: '计算背离幅度所用基准平均成交量的窗口；它用于把 OBV 变化换算为可比较的成交量倍数。',
  短周期: '趋势状态判断中短期均线的观察窗口，与长周期均线及趋势强度共同判断。',
  长周期: '趋势状态判断中长期均线的观察窗口，应大于短周期。',
  斜率观察周期: '将当前短期均线与多少根 K 线之前的短期均线比较，用于判断均线向上或向下。',
  'ADX 周期': '计算 ADX 趋势强度所用的周期；它不等于趋势连续确认天数。',
  'ADX 阈值': '趋势状态判断要求达到的 ADX 强度值。ADX 衡量强弱，不单独表示上涨或下跌。',
  确认天数: '上涨或下跌趋势条件需要连续成立的有效交易日数；不满足时不确认该趋势。',
  稳定预热根数: '趋势状态指标开始输出前要求积累的有效 K 线数量，不是要求某个趋势持续这么多根。还需满足各指标自己的预热要求。',
  年化交易日数: '将区间波动率换算为年化波动率时使用的交易日数量，通过平方根缩放；不改变真实交易日历。',
  价格口径: '指标计算选择开盘价、最高价、最低价或收盘价；这是信号输入，不代表委托成交价。',
  'K 值平滑周期': 'KDJ 中平滑 RSV 得到 K 值的参数，通常为 3；数值越大，新数据的权重越小，变化通常越平缓。它不同于计算最高价、最低价窗口的观察周期。实际指标来源与算法口径以报告为准。',
  'D 值平滑周期': 'KDJ 中继续平滑 K 值得到 D 值的参数，通常为 3；会影响 K、D 交叉及 J 值。修改它不会改变 RSV 的观察窗口，实际指标来源与算法口径以报告为准。',
  常用区间: '快速填写开始和结束日期；选择后仍可单独修改日期。实际回测以已完成且数据可用的交易日为准。',
  开始日期: '本次回测的起点。指标预热可以读取起点之前的数据，但预热期不计入本次收益区间。',
  结束日期: '本次回测区间的终点。不要使用尚未完整发布的行情；日线收盘信号还需要后续可交易日才能尝试成交。',
  初始资金: '回测使用的模拟资金，单位为元。影响可买股数、手续费及资金是否足够，不代表真实账户余额。',
  信号与成交: '产生信号不等于成交。日线条件通常在收盘确认，下一交易日按声明的价格口径尝试成交；日线不能还原盘中成交顺序。',
  'A 股次日可卖规则': '当日新买入股票不能当日卖出；只有可卖持仓能参与卖出。买入信号不会取消这个限制。',
  涨跌停处理: '控制缺少开板或可成交证据时如何处理委托。默认采用保守估计；限价容量估算也不代表能排到真实交易队列。停牌时不成交。',
  单边比例滑点: '按价格比例模拟不利成交偏差：买入加、卖出减。1 基点＝0.01%，10 基点＝0.1%。与固定价差同时设置时叠加。',
  单边固定价差: '每股的固定不利价差，单位元/股。例如填 0.02，买入价格加 0.02 元，卖出减 0.02 元，再受限价及交易边界约束。只用固定价差时把比例滑点设为 0。',
  佣金率: '按成交金额计算的佣金比例。此框单位为 %，填写 0.025 表示千分之0.25（万分之2.5）；未成交数量不收取成交佣金。其他税费以报告执行口径为准。',
  最低佣金: '佣金计算的最低金额，单位为元。费用归集方式以本次执行报告为准；模拟结果不代表券商实际账单。',
  成交容量模式: '时点容量模式根据当时已知的成交量限制成交数量；不设上限仅用于研究，会忽略流动性约束，可能高估可成交数量。',
  成交量参与率: '委托最多使用可用成交量的比例。例如填 5 表示 5%，不是账户仓位的 5%。日线可能使用前一交易日成交量代理，具体以报告为准。',
  每次买入资金比例: '每次新的买入触发使用当时剩余现金的比例。50% 表示先用一半剩余现金，再次触发时使用届时剩余现金的一半。100% 允许使用全部剩余现金，仍受费用、申报数量和成交容量约束。',
  卖出未成交后重试: '控制已触发的持有期退出、日线持仓保护在未完成时是否继续尝试。普通指标卖出失败后结束，不沿用旧信号；分钟保护按其独立规则重试。这不是网络请求重试，也不保证最终成交。',
  最多卖出尝试次数: '限制日线持仓保护的卖出尝试次数，不影响普通指标单。持有期退出开启重试后会保留余量，按后续交易日开盘继续尝试至区间结束，不受此次数上限截断。未卖出的持仓仍计入期末资产。',
  指标预热日历天数: '在回测起点前额外读取的日历天数，用于计算均线等指标。包含周末与节假日，不等于指标周期的交易日数。',
  结算延长日历天数: '在策略区间之后预留的日历天数，用于处理尚未完成的退出与结算。不是延长信号生成区间，也不是强制保证清仓。',
  稳健性检查: '额外检查执行成本等假设变化对结果的影响。开启后可能增加计算时间，不代表策略未来表现有保证。',
  条件关系: '“且”要求所有条件同时满足；“或”只需任一条件满足。修改关系会改变触发时机。',
  卖出条件关系: '“或”在任一卖出条件满足时触发；“且”需要所有条件同时满足。持有期与其他条件使用“且”时，到期不一定立即卖出。',
  触发方式: '决定如何比较指标：持续高于或低于，与从一侧穿越到另一侧不是同一种条件。触发后仍需等待执行时点。',
  比较方式: '高于和低于不包含等于；不低于和不高于包含等于。比较值必须与指标使用同一单位。',
  阈值单位: '数值比较所用的单位，例如元、%、次。单位必须与指标口径一致；金额和百分比不可直接互换。',
  共同单位: '左右两条指标序列转换到此单位后比较；不能把不同量纲的数字直接比较。',
  指标名称与口径: '说明要查询的指标及周期、报告期等口径。系统会检查所选区间的历史数据是否可用于回测。',
  公告关键词: '在指定历史正文中检索的文字。能查询公告不等于能回放历史正文；缺少历史正文时不会伪造触发结果。',
}

export function settingHelp(label: string): string {
  if (explanations[label]) return explanations[label]
  if (/周期|period|交易日/.test(label)) return '控制指标观察窗口或持有时长。按交易日定义的参数不包含周末和休市日；更长周期通常需要更多预热历史。持有期满只是退出条件，实际卖出仍受可卖持仓和成交约束。'
  if (/阈值|幅度|比较值/.test(label)) return '与当前条件的指标或收益值比较的数值，必须按该条件标注的单位填写。只有明确标为 % 的输入框，5 才表示 5%；指标点数、元和倍数不能按百分比换算。修改后会改变条件触发时机。'
  if (/指标与口径/.test(label)) return '填写这一侧指标及其周期等口径；两侧按共同单位比较，仅使用当时可获得的历史值。'
  return `用于调整当前条件的“${label}”。沿用本条规则的指标口径与单位；修改后影响条件计算，不会绕过数据和成交限制。`
}
