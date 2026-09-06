"""Deterministic analysis for confirmed A/H-share portfolio-review records.

The account ledger is the source of truth.  Optional historical market data may
add evidence to an already verified highlight, but it never replaces imported
account values or participates in account-return arithmetic.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from typing import Literal, TypedDict
from zoneinfo import ZoneInfo

from ashare_lab.domain.market_data import (
    AshareInstrumentCodeError,
    PriceBasis,
    normalize_a_share_instrument,
)
from ashare_lab.domain.shared import InstrumentId
from ashare_lab.ports.market_data import DataRequirements, DateRange, MarketDataRepository
from ashare_lab.ports.portfolio_highlight_narrative import (
    VerifiedPerformanceEvidence,
    VerifiedPortfolioHighlight,
)

CapabilityGrade = Literal["available", "partial", "unavailable"]


class PortfolioReviewInputError(ValueError):
    """Raised when otherwise well-typed records contradict each other."""


@dataclass(frozen=True, slots=True)
class ReviewAccount:
    period_start: date
    period_end: date
    base_currency: str
    timezone: str


@dataclass(frozen=True, slots=True)
class HoldingRecord:
    source_id: str | None
    content_sha256: str | None
    as_of: datetime
    market: str
    symbol: str
    name: str
    quantity: Decimal
    market_price: Decimal
    average_cost: Decimal | None
    currency: str
    fx_to_base: Decimal | None


@dataclass(frozen=True, slots=True)
class TradeRecord:
    source_id: str | None
    content_sha256: str | None
    executed_at: datetime
    timestamp_precision: str
    market: str
    symbol: str
    name: str | None
    side: str
    quantity: Decimal
    price: Decimal
    fees: Decimal
    currency: str
    fx_to_base: Decimal | None
    realized_pnl: Decimal | None


@dataclass(frozen=True, slots=True)
class CashFlowRecord:
    source_id: str | None
    content_sha256: str | None
    occurred_at: datetime
    kind: str
    amount: Decimal
    currency: str
    fx_to_base: Decimal | None
    external: bool


@dataclass(frozen=True, slots=True)
class DailyEquityRecord:
    source_id: str | None
    content_sha256: str | None
    at: date
    equity_base: Decimal
    external_inflow_base: Decimal | None
    external_outflow_base: Decimal | None


@dataclass(frozen=True, slots=True)
class MarketTickRecord:
    source_id: str | None
    content_sha256: str | None
    at: datetime
    granularity: str
    market: str | None
    symbol: str | None
    last_price: Decimal | None
    account_equity_base: Decimal | None
    currency: str | None
    fx_to_base: Decimal | None


@dataclass(frozen=True, slots=True)
class PortfolioReviewInput:
    profile: str
    import_method: str
    confirmed: bool
    account: ReviewAccount
    holdings: tuple[HoldingRecord, ...]
    trades: tuple[TradeRecord, ...]
    cash_flows: tuple[CashFlowRecord, ...]
    daily_equity: tuple[DailyEquityRecord, ...]
    market_ticks: tuple[MarketTickRecord, ...]


@dataclass(frozen=True, slots=True)
class PortfolioReviewAnalysis:
    """JSON-shaped analysis whose Decimal and time values remain typed."""

    payload: dict[str, object]


class _UnresolvedDraft(TypedDict):
    source_index: int
    reason: str


class _TradeDraft(TypedDict):
    source_id: str
    content_sha256: str
    executed_at: datetime
    timestamp_precision: str
    market: str
    symbol: str
    name: str | None
    side: str
    quantity: Decimal
    price: Decimal
    fees: Decimal
    currency: str
    fx_to_base: Decimal | None
    realized_pnl: Decimal | None


class _CashFlowDraft(TypedDict):
    source_id: str
    content_sha256: str
    occurred_at: datetime
    kind: str
    amount: Decimal
    currency: str
    fx_to_base: Decimal | None
    external: bool


class _HoldingDraft(TypedDict):
    source_id: str
    content_sha256: str
    as_of: datetime
    market: str
    symbol: str
    name: str
    quantity: Decimal
    market_price: Decimal
    average_cost: Decimal | None
    currency: str
    fx_to_base: Decimal | None


class _BalanceDraft(TypedDict):
    source_id: str
    content_sha256: str
    as_of: datetime
    market: str
    symbol: str
    name: str
    quantity: Decimal
    market_price: Decimal | None
    average_cost: Decimal | None
    currency: str
    fx_to_base: Decimal | None


class _PositionBalanceDraft(_BalanceDraft):
    reconstructable_holding: bool


class _CapabilityData(TypedDict):
    grade: CapabilityGrade
    available: bool
    reason: str
    granularity: str | None


class _SnapshotPosition(TypedDict):
    as_of: datetime
    source_id: str | None
    content_sha256: str | None
    market: str
    symbol: str
    name: str
    quantity: Decimal
    market_price: Decimal
    average_cost: Decimal | None
    currency: str
    fx_to_base: Decimal
    market_value: Decimal
    market_value_base: Decimal
    unrealized_pnl_base: Decimal | None


class _HoldingProjection(_SnapshotPosition):
    weight_pct: Decimal


class _TradeEvent(TypedDict):
    source_index: int
    source_id: str | None
    content_sha256: str | None
    executed_at: datetime
    timestamp_precision: str
    market: str
    symbol: str
    name: str | None
    side: str
    quantity: Decimal
    price: Decimal
    fees: Decimal
    currency: str
    fx_to_base: Decimal
    notional_base: Decimal
    fees_base: Decimal
    realized_pnl_base: Decimal | None
    realized_pnl_source: str | None


class _AttributionEntry(TypedDict):
    kind: str
    source_index: int
    source_id: str | None
    content_sha256: str | None
    at: datetime
    market: str
    symbol: str
    name: str
    amount_base: Decimal
    calculation_method: str
    evidence_fields: tuple[str, ...]


class _NormalizedAttributionEntry(_AttributionEntry):
    share_of_verified_pnl: Decimal | None


class _CashEvent(TypedDict):
    source_index: int
    source_id: str | None
    content_sha256: str | None
    occurred_at: datetime
    kind: str
    amount: Decimal
    currency: str
    fx_to_base: Decimal
    external: bool
    amount_base: Decimal
    external_flow_base: Decimal


class _PerformancePoint(TypedDict):
    at: date
    source_id: str | None
    content_sha256: str | None
    equity_base: Decimal
    external_inflow_base: Decimal
    external_outflow_base: Decimal
    external_flow_base: Decimal
    period_return: Decimal | None
    linked_index: Decimal
    drawdown: Decimal


class _PerformanceData(TypedDict):
    method: str
    granularity: str
    twr: Decimal
    max_drawdown: Decimal
    start_equity_base: Decimal
    end_equity_base: Decimal
    points: list[_PerformancePoint]


class _AttributionData(TypedDict):
    method: str
    coverage: CapabilityGrade
    verified_pnl_base: Decimal
    entries: list[_NormalizedAttributionEntry]


class _MarketEvent(TypedDict):
    source_index: int
    source_id: str | None
    content_sha256: str | None
    at: datetime
    granularity: str
    market: str | None
    symbol: str | None
    last_price: Decimal | None
    account_equity_base: Decimal | None
    currency: str | None
    fx_to_base: Decimal | None
    last_price_base: Decimal | None


class _MarketReplay(TypedDict):
    timestamp_origin: str
    granularities: tuple[str, ...]
    interpolation: str
    events: list[_MarketEvent]


class _Highlight(TypedDict):
    kind: str
    type: str
    at: datetime
    market: str
    symbol: str
    name: str
    amount_base: Decimal
    impact_base: Decimal
    impact_pct: Decimal | None
    title: str
    subtitle: str
    evidence: str
    calculation_method: str
    evidence_fields: tuple[str, ...]
    verification: str
    trade_source_index: int
    source_id: str | None
    content_sha256: str | None
    start_tick_index: int | None
    end_tick_index: int | None


class _ReplayFrame(TypedDict):
    at: datetime
    kind: str
    title: str
    market: str | None
    symbol: str | None
    account_equity_base: Decimal | None
    last_price: Decimal | None
    return_pct: Decimal | None
    caption: str
    evidence: str
    source_kind: str
    source_index: int
    source_id: str | None
    content_sha256: str | None
    is_highlight: bool
    battle_cry: str | None


class _SeriesPoint(TypedDict):
    at: date
    source_id: str | None
    content_sha256: str | None
    equity_base: Decimal
    return_pct: Decimal | None
    drawdown_pct: Decimal


class _FifoLot(TypedDict):
    quantity: Decimal
    unit_cost_base: Decimal


_HEADER_ALIASES = {
    "occurred_at": (
        "executed_at",
        "trade_time",
        "trade_date",
        "as_of",
        "date",
        "datetime",
        "成交时间",
        "成交日期",
        "发生日期",
        "业务日期",
        "持仓时间",
        "持仓日期",
        "截至日期",
        "报告日期",
        "日期",
    ),
    "symbol": ("symbol", "code", "security_code", "stock_code", "证券代码", "股票代码", "代码"),
    "name": ("name", "security_name", "stock_name", "证券名称", "证券简称", "股票名称", "名称"),
    "business": (
        "side",
        "business",
        "business_type",
        "trade_type",
        "买卖方向",
        "买卖标志",
        "业务名称",
        "业务类型",
        "操作",
    ),
    "quantity": (
        "quantity",
        "qty",
        "trade_quantity",
        "position_quantity",
        "holding_quantity",
        "成交数量",
        "成交股数",
        "发生数量",
        "持仓数量",
        "股票余额",
        "数量",
    ),
    "price": ("price", "trade_price", "成交价格", "成交价", "价格"),
    "amount": ("amount", "trade_amount", "发生金额", "成交金额", "资金发生数", "金额"),
    "realized_pnl": ("realized_pnl", "realized_profit", "已实现盈亏", "实现盈亏", "清算盈亏"),
    "currency": ("currency", "币种", "货币"),
    "base_currency": ("base_currency", "本位币", "基准币种"),
    "market": ("market", "exchange", "市场", "交易市场", "交易所"),
    "fx_to_base": ("fx_to_base", "exchange_rate", "汇率", "折算汇率"),
    "balance_quantity": (
        "balance_quantity",
        "share_balance",
        "股份余额",
        "证券余额",
        "股票余额",
        "剩余数量",
    ),
    "market_price": ("market_price", "last_price", "最新价", "市价", "收盘价"),
    "market_value": ("market_value", "市值", "证券市值"),
    "average_cost": ("average_cost", "cost_price", "成本价", "摊薄成本价"),
}
_FEE_HEADERS = (
    "fees",
    "fee",
    "commission",
    "stamp_tax",
    "transfer_fee",
    "手续费",
    "佣金",
    "印花税",
    "过户费",
    "规费",
)
_BUY_WORDS = {"buy", "b", "买入", "证券买入", "股票买入"}
_SELL_WORDS = {"sell", "s", "卖出", "证券卖出", "股票卖出"}
_FLOW_WORDS = {
    "deposit": {"deposit", "入金", "银证转入", "银行转证券", "资金转入"},
    "withdrawal": {"withdrawal", "出金", "银证转出", "证券转银行", "资金转出"},
    "dividend": {"dividend", "红利", "股息", "派息", "红利入账"},
    "interest": {"interest", "利息", "结息"},
    "fee": {"fee", "费用", "手续费"},
    "tax": {"tax", "税费", "红利税", "印花税"},
}
_CORPORATE_ACTION_WORDS = (
    "送股",
    "转增",
    "配股",
    "拆股",
    "合股",
    "红股",
    "stocksplit",
    "stockdividend",
    "rightsissue",
    "shareconsolidation",
)
_UNSUPPORTED_CASH_PROFILE_WORDS = (
    "融资",
    "融券",
    "卖券还款",
    "买券还券",
    "margin",
    "shortsell",
)


def infer_broker_ledger_import(
    rows: Sequence[Mapping[str, object]],
    *,
    current_holding_rows: Sequence[Mapping[str, object]] = (),
    import_method: str = "csv_xlsx",
) -> dict[str, object]:
    """Map broker-shaped table rows into one editable, evidence-aware import draft.

    The caller is expected to decode CSV/XLSX into rows.  This function owns the
    broker-header semantics and never assumes that a trade net position is a
    reconciled current holding.
    """

    trades: list[_TradeDraft] = []
    cash_flows: list[_CashFlowDraft] = []
    unresolved_rows: list[_UnresolvedDraft] = []
    warnings: list[str] = []
    event_times: list[datetime] = []
    record_currencies: set[str] = set()
    declared_base_currencies: set[str] = set()
    markets: set[str] = set()
    last_balances: dict[tuple[str, str], _BalanceDraft] = {}
    opening_position_gaps: set[str] = set()

    for source_index, raw_row in enumerate(rows):
        row = _canonical_import_row(raw_row)
        row_hash = _mapping_hash(row)
        business = _text_value(_field(row, "business"))
        business_key = _business_key(business)
        if any(word in business_key for word in _UNSUPPORTED_CASH_PROFILE_WORDS):
            unresolved_rows.append(
                {
                    "source_index": source_index,
                    "reason": (
                        "margin, short-sale, or debt-repayment records are unsupported "
                        "by the cash-account profile"
                    ),
                }
            )
            continue
        if any(word in business_key for word in _CORPORATE_ACTION_WORDS):
            unresolved_rows.append(
                {
                    "source_index": source_index,
                    "reason": "company action requires a dedicated corporate-action record",
                }
            )
            continue

        raw_symbol = _text_value(_field(row, "symbol"))
        raw_market = _text_value(_field(row, "market"))
        market, symbol = _infer_market_and_symbol(raw_symbol, raw_market)
        occurred_at = _parse_import_datetime(
            _field(row, "occurred_at"),
            market=market,
        )
        currency = _infer_record_currency(_field(row, "currency"), market=market)
        base_currency = _parse_currency(_field(row, "base_currency"))
        if base_currency is not None:
            declared_base_currencies.add(base_currency)
        fx = _optional_decimal(_field(row, "fx_to_base"))
        flow_kind = _classify_flow(business)
        side = _classify_side(business)

        if side is not None:
            missing: list[str] = []
            if occurred_at is None:
                missing.append("transaction date")
            if market is None or symbol is None:
                missing.append("security code/market")
            quantity = _optional_decimal(_field(row, "quantity"))
            price = _optional_decimal(_field(row, "price"))
            if quantity is None or quantity <= 0:
                missing.append("positive quantity")
            if price is None or price <= 0:
                missing.append("positive trade price")
            if currency is None:
                missing.append("currency")
            if fx is not None and fx <= 0:
                missing.append("positive exchange rate")
            if missing:
                unresolved_rows.append(
                    {
                        "source_index": source_index,
                        "reason": "missing " + ", ".join(missing),
                    }
                )
                continue
            if occurred_at is None or market is None or symbol is None:
                raise TypeError("validated trade identity is missing")
            if quantity is None or price is None or currency is None:
                raise TypeError("validated trade amounts are missing")
            fees = _sum_fee_columns(row)
            realized_pnl = _optional_decimal(_field(row, "realized_pnl"))
            name = _text_value(_field(row, "name"))
            trade: _TradeDraft = {
                "source_id": f"broker-row-{source_index + 1}",
                "content_sha256": f"sha256:{row_hash}",
                "executed_at": occurred_at,
                "timestamp_precision": _import_time_precision(_field(row, "occurred_at")),
                "market": market,
                "symbol": symbol,
                "name": name,
                "side": side,
                "quantity": quantity,
                "price": price,
                "fees": fees,
                "currency": currency,
                "fx_to_base": fx,
                "realized_pnl": realized_pnl,
            }
            trades.append(trade)
            event_times.append(occurred_at)
            record_currencies.add(currency)
            markets.add(market)
            key = (market, symbol)
            balance_issue = _remember_balance(
                last_balances,
                key=key,
                row=row,
                source_id=f"broker-row-{source_index + 1}",
                content_sha256=f"sha256:{row_hash}",
                at=occurred_at,
                market=market,
                symbol=symbol,
                name=name,
                currency=currency,
                fx=fx,
            )
            if balance_issue is not None:
                unresolved_rows.append(
                    {
                        "source_index": source_index,
                        "reason": balance_issue,
                    }
                )
            continue

        if flow_kind is not None:
            amount = _optional_decimal(_field(row, "amount"))
            missing = []
            if occurred_at is None:
                missing.append("cash-flow date")
            if amount is None or amount == 0:
                missing.append("non-zero amount")
            if currency is None:
                missing.append("currency")
            if fx is not None and fx <= 0:
                missing.append("positive exchange rate")
            if missing:
                unresolved_rows.append(
                    {
                        "source_index": source_index,
                        "reason": "missing " + ", ".join(missing),
                    }
                )
                continue
            if occurred_at is None or amount is None or currency is None:
                raise TypeError("validated cash flow is missing")
            cash_flows.append(
                {
                    "source_id": f"broker-row-{source_index + 1}",
                    "content_sha256": f"sha256:{row_hash}",
                    "occurred_at": occurred_at,
                    "kind": flow_kind,
                    "amount": abs(amount),
                    "currency": currency,
                    "fx_to_base": fx,
                    "external": flow_kind in {"deposit", "withdrawal"},
                }
            )
            event_times.append(occurred_at)
            record_currencies.add(currency)
            continue

        unresolved_rows.append(
            {
                "source_index": source_index,
                "reason": "unrecognized broker business type",
            }
        )

    running_quantities: dict[tuple[str, str], Decimal] = {}
    for trade in sorted(trades, key=lambda item: item["executed_at"]):
        key = (trade["market"], trade["symbol"])
        signed = trade["quantity"] if trade["side"] == "buy" else -trade["quantity"]
        running_quantities[key] = running_quantities.get(key, Decimal("0")) + signed
        if running_quantities[key] < 0:
            opening_position_gaps.add(trade["symbol"])

    (
        supplemental_holdings,
        holding_unresolved,
        _holding_times,
        _holding_currencies,
        _holding_markets,
    ) = _parse_current_holdings(current_holding_rows, fallback_time=max(event_times, default=None))
    unresolved_rows.extend(holding_unresolved)
    position_balances = _position_balances(last_balances)
    broker_holdings = _holdings_from_broker_balances(last_balances)
    holdings = _merge_current_holdings(
        broker_holdings=broker_holdings,
        supplemental_holdings=supplemental_holdings,
    )
    holding_source = (
        "supplemental_current_holdings"
        if supplemental_holdings
        else "broker_reported_closing_balance"
        if broker_holdings
        else None
    )
    event_times.extend(item["as_of"] for item in holdings)
    record_currencies.update(item["currency"] for item in holdings)
    markets.update(item["market"] for item in holdings)

    if not event_times:
        raise PortfolioReviewInputError("broker import contains no usable dated records")
    timezone = "Asia/Shanghai" if "CN_A" in markets or not markets else "Asia/Hong_Kong"
    account_zone = ZoneInfo(timezone)
    period_start = min(item.astimezone(account_zone).date() for item in event_times)
    period_end = max(item.astimezone(account_zone).date() for item in event_times)
    base_currency = _infer_base_currency(
        declared=declared_base_currencies,
        record_currencies=record_currencies,
        trades=trades,
        cash_flows=cash_flows,
        holdings=holdings,
    )

    reconciliation_reasons: list[str] = []
    holding_snapshot_times = {item["as_of"] for item in holdings}
    inconsistent_holding_snapshot = len(holding_snapshot_times) > 1
    if inconsistent_holding_snapshot:
        reconciliation_reasons.append(
            "当前持仓并非同一时点快照；请提供同一 as_of 时间的持仓后再确认"
        )
    if opening_position_gaps:
        reconciliation_reasons.append(
            "成交序列出现先卖后买，需要期初持仓或更早流水："
            + "、".join(sorted(opening_position_gaps))
        )
    if not holdings:
        reconciliation_reasons.append(
            "成交净额不能证明当前持仓；请补当前持仓截图/表格，或提供带证券余额和市价的完整交割流水"
        )
    has_unresolved_company_action = any(
        "company action" in reason["reason"] for reason in unresolved_rows
    )
    if has_unresolved_company_action:
        reconciliation_reasons.append("存在公司行动记录，需补充公司行动明细后才能完整对账")
    if base_currency is None:
        reconciliation_reasons.append("混合币种流水未提供可验证的本位币/折算汇率")
    missing_fx_records = (
        ()
        if base_currency is None
        else tuple(
            item
            for item in (*trades, *cash_flows, *holdings)
            if item["currency"] != base_currency and item.get("fx_to_base") is None
        )
    )
    if missing_fx_records:
        reconciliation_reasons.append("非本位币记录缺少历史折算汇率")
    invalid_base_fx_records = (
        ()
        if base_currency is None
        else tuple(
            item
            for item in (*trades, *cash_flows, *holdings)
            if item["currency"] == base_currency
            and item.get("fx_to_base") not in {None, Decimal("1")}
        )
    )
    if invalid_base_fx_records:
        reconciliation_reasons.append("本位币记录的折算汇率必须为空或等于 1")

    account = (
        None
        if base_currency is None
        else {
            "period_start": period_start,
            "period_end": period_end,
            "base_currency": base_currency,
            "timezone": timezone,
        }
    )
    can_analyze = (
        bool(trades or cash_flows or holdings)
        and account is not None
        and not missing_fx_records
        and not invalid_base_fx_records
        and not inconsistent_holding_snapshot
        and not has_unresolved_company_action
    )
    status = (
        "ready_for_confirmation"
        if can_analyze and not reconciliation_reasons
        else "needs_base_currency"
        if base_currency is None
        else "needs_supplement"
    )
    return {
        "profile": "CN_A_HK_CASH_V1",
        "security_identity_scope": "syntactic_a_h_normalization_only",
        "import_metadata": {
            "method": import_method,
            "confirmed": False,
        },
        "inferred_account": {
            "period_start": period_start,
            "period_end": period_end,
            "base_currency": base_currency,
            "timezone": timezone,
            "markets": tuple(sorted(markets)),
        },
        "draft": {
            "account": account,
            "holdings": tuple(holdings),
            "trades": tuple(trades),
            "cash_flows": tuple(cash_flows),
            "daily_equity": (),
            "market_ticks": (),
        },
        "position_balances": tuple(position_balances),
        "reconciliation": {
            "status": status,
            "can_analyze_after_confirmation": can_analyze,
            "needs_current_holdings": not bool(holdings),
            "needs_opening_positions": bool(opening_position_gaps),
            "holding_source": holding_source,
            "reasons": tuple(reconciliation_reasons),
        },
        "unresolved_rows": tuple(unresolved_rows),
        "warnings": tuple(warnings),
    }


def _header_key(value: object) -> str:
    return re.sub(r"[\s_\-./\\()（）\[\]【】]+", "", str(value)).casefold()


def _canonical_import_row(row: Mapping[str, object]) -> dict[str, object]:
    return {_header_key(key): value for key, value in row.items()}


def _field(row: Mapping[str, object], field_name: str) -> object | None:
    return next(
        (row[key] for alias in _HEADER_ALIASES[field_name] if (key := _header_key(alias)) in row),
        None,
    )


def _mapping_hash(row: Mapping[str, object]) -> str:
    encoded = json.dumps(
        row,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _text_value(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    text = str(value).strip()
    if not text or text in {"--", "—", "N/A", "n/a"}:
        return None
    negative = text.startswith("(") and text.endswith(")")
    cleaned = (
        text.strip("()")
        .replace(",", "")
        .replace("，", "")
        .replace("¥", "")
        .replace("￥", "")
        .replace("HK$", "")
        .replace("RMB", "")
        .replace("CNY", "")
        .replace("HKD", "")
        .strip()
    )
    try:
        parsed = Decimal(cleaned)
    except InvalidOperation:
        return None
    if not parsed.is_finite():
        return None
    return -parsed if negative else parsed


def _sum_fee_columns(row: Mapping[str, object]) -> Decimal:
    aggregate = next(
        (
            _optional_decimal(row[_header_key(name)])
            for name in ("fees", "fee", "手续费")
            if _header_key(name) in row
        ),
        None,
    )
    if aggregate is not None:
        return abs(aggregate)
    total = Decimal("0")
    for name in _FEE_HEADERS:
        key = _header_key(name)
        value = _optional_decimal(row.get(key))
        if value is not None:
            total += abs(value)
    return total


def _infer_market_and_symbol(
    raw_symbol: str | None,
    raw_market: str | None,
) -> tuple[str | None, str | None]:
    if raw_symbol is None:
        return None, None
    symbol = raw_symbol.strip().upper()
    if re.fullmatch(r"\d+\.0+", symbol):
        symbol = symbol.split(".", maxsplit=1)[0]

    raw_suffix = symbol.rsplit(".", maxsplit=1)[1] if "." in symbol else None
    symbol_market = (
        "HK" if raw_suffix == "HK" else "CN_A" if raw_suffix in {"SH", "SZ", "BJ"} else None
    )
    if raw_suffix is not None and symbol_market is None:
        return None, None

    hinted_market, hinted_suffix, recognized_hint = _parse_import_market_hint(raw_market)
    if raw_market is not None and raw_market.strip() and not recognized_hint:
        return None, None
    if symbol_market is not None and hinted_market is not None and symbol_market != hinted_market:
        return None, None
    if raw_suffix in {"SH", "SZ", "BJ"} and hinted_suffix not in {None, raw_suffix}:
        return None, None

    market = symbol_market or hinted_market
    code = symbol.rsplit(".", maxsplit=1)[0] if raw_suffix is not None else symbol
    if market == "HK":
        if code.isdigit() and 1 <= len(code) <= 5:
            return "HK", f"{int(code):05d}.HK"
        return None, None
    if market == "CN_A":
        if not code.isdigit() or not 1 <= len(code) <= 6:
            return None, None
        padded = code.zfill(6)
        suffix = raw_suffix or hinted_suffix
        candidate = f"{padded}.{suffix}" if suffix else padded
        try:
            return "CN_A", normalize_a_share_instrument(candidate).value
        except AshareInstrumentCodeError:
            return None, None

    # Without a market/suffix, six digits can pass the strict mainland code
    # gate and exactly five digits follow the public HK identifier format.
    # Shorter bare numbers are ambiguous after spreadsheets remove leading
    # zeroes, so the importer deliberately asks for a market instead of guessing.
    try:
        return "CN_A", normalize_a_share_instrument(symbol).value
    except AshareInstrumentCodeError:
        pass
    if code.isdigit() and len(code) == 5:
        return "HK", f"{int(code):05d}.HK"
    return None, None


def _parse_import_market_hint(
    raw_market: str | None,
) -> tuple[str | None, str | None, bool]:
    if raw_market is None or not raw_market.strip():
        return None, None, True
    key = _header_key(raw_market)
    if key in {"hk", "hkg", "hkex", "sehk", "xhongkong"} or any(
        token in key for token in ("香港", "港股", "港交所")
    ):
        return "HK", None, True
    if key in {"sh", "sha", "sse", "xshg"} or any(
        token in key for token in ("上海", "沪市", "沪a", "上交所", "科创")
    ):
        return "CN_A", "SH", True
    if key in {"sz", "sza", "szse", "xshe"} or any(
        token in key for token in ("深圳", "深市", "深a", "深交所", "创业")
    ):
        return "CN_A", "SZ", True
    if key in {"bj", "bja", "bse", "xbse"} or any(
        token in key for token in ("北京", "北交所", "北证")
    ):
        return "CN_A", "BJ", True
    if key in {"a", "a股", "cna", "china", "mainland", "沪深", "沪深a股", "内地"}:
        return "CN_A", None, True
    return None, None, False


def _parse_import_datetime(value: object, *, market: str | None) -> datetime | None:
    zone = ZoneInfo("Asia/Hong_Kong" if market == "HK" else "Asia/Shanghai")
    if isinstance(value, datetime):
        return value.replace(tzinfo=zone) if value.tzinfo is None else value
    if isinstance(value, date):
        return datetime.combine(value, time(0), tzinfo=zone)
    text = _text_value(value)
    if text is None:
        return None
    normalized = _normalize_import_datetime_text(text)
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                parsed = datetime.strptime(normalized, pattern)
                break
            except ValueError:
                continue
        else:
            return None
    return parsed.replace(tzinfo=zone) if parsed.tzinfo is None else parsed


def _import_time_precision(value: object) -> str:
    if isinstance(value, datetime):
        return "exact"
    if isinstance(value, date):
        return "date_only"
    text = _text_value(value)
    if text is None:
        return "date_only"
    normalized = _normalize_import_datetime_text(text)
    return "date_only" if re.fullmatch(r"(?:\d{8}|\d{4}-\d{2}-\d{2})", normalized) else "exact"


def _normalize_import_datetime_text(value: str) -> str:
    normalized = value.replace("/", "-")
    if re.fullmatch(r"\d{8}", normalized):
        return f"{normalized[:4]}-{normalized[4:6]}-{normalized[6:]}"
    matched = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})(.*)", normalized)
    if matched is None:
        return normalized
    year, month, day, suffix = matched.groups()
    return f"{year}-{int(month):02d}-{int(day):02d}{suffix}"


def _business_key(value: str | None) -> str:
    return "" if value is None else re.sub(r"\s+", "", value).casefold()


def _classify_side(value: str | None) -> str | None:
    key = _business_key(value)
    if key in _BUY_WORDS or "买入" in key:
        return "buy"
    if key in _SELL_WORDS or "卖出" in key:
        return "sell"
    return None


def _classify_flow(value: str | None) -> str | None:
    key = _business_key(value)
    for kind, aliases in _FLOW_WORDS.items():
        if key in aliases or any(alias in key for alias in aliases if len(alias) >= 2):
            return kind
    return None


def _parse_currency(value: object) -> str | None:
    raw = _text_value(value)
    if raw is None:
        return None
    normalized = raw.upper().replace(" ", "")
    if normalized in {"CNY", "RMB", "人民币", "人民币元", "元"}:
        return "CNY"
    if normalized in {"HKD", "HK$", "港币", "港元"}:
        return "HKD"
    return None


def _infer_record_currency(value: object, *, market: str | None) -> str | None:
    explicit = _parse_currency(value)
    if explicit is not None:
        return explicit
    return "CNY" if market == "CN_A" else "HKD" if market == "HK" else None


def _remember_balance(
    balances: dict[tuple[str, str], _BalanceDraft],
    *,
    key: tuple[str, str],
    row: Mapping[str, object],
    source_id: str,
    content_sha256: str,
    at: datetime,
    market: str,
    symbol: str,
    name: str | None,
    currency: str,
    fx: Decimal | None,
) -> str | None:
    quantity = _optional_decimal(_field(row, "balance_quantity"))
    if quantity is None:
        return None
    if quantity < 0:
        return "closing balance requires a non-negative quantity"
    average_cost = _optional_decimal(_field(row, "average_cost"))
    if average_cost is not None and average_cost < 0:
        return "closing balance requires a non-negative average cost"
    if fx is not None and fx <= 0:
        return "closing balance requires a positive exchange rate"
    market_price = _optional_decimal(_field(row, "market_price"))
    market_value = _optional_decimal(_field(row, "market_value"))
    if market_price is None and market_value is not None and quantity > 0:
        market_price = market_value / quantity
    candidate: _BalanceDraft = {
        "source_id": source_id,
        "content_sha256": content_sha256,
        "as_of": at,
        "market": market,
        "symbol": symbol,
        "name": name or symbol,
        "quantity": quantity,
        "market_price": market_price,
        "average_cost": average_cost,
        "currency": currency,
        "fx_to_base": fx,
    }
    previous = balances.get(key)
    if previous is None or previous["as_of"] <= at:
        balances[key] = candidate
    return None


def _position_balances(
    balances: Mapping[tuple[str, str], _BalanceDraft],
) -> list[_PositionBalanceDraft]:
    projected: list[_PositionBalanceDraft] = []
    for _key, item in sorted(balances.items()):
        market_price = item["market_price"]
        projected.append(
            {
                **item,
                "reconstructable_holding": (
                    item["quantity"] > 0 and market_price is not None and market_price > 0
                ),
            }
        )
    return projected


def _holdings_from_broker_balances(
    balances: Mapping[tuple[str, str], _BalanceDraft],
) -> list[_HoldingDraft]:
    reconstructable: list[tuple[tuple[str, str], _BalanceDraft]] = []
    for key, item in sorted(balances.items()):
        market_price = item["market_price"]
        if item["quantity"] > 0 and market_price is not None and market_price > 0:
            reconstructable.append((key, item))
    # Keep every broker-reported closing balance so a supplemental row can
    # replace one symbol without discarding the others.  The import-level
    # reconciliation gate separately rejects a merged set that is not one
    # exact as-of snapshot.
    holdings: list[_HoldingDraft] = []
    for index, (_key, item) in enumerate(reconstructable):
        quantity = item["quantity"]
        market_price = item["market_price"]
        if market_price is None:
            raise TypeError("reconstructable broker balance is malformed")
        parent_source_id = item["source_id"] or f"broker-balance-{index + 1}"
        child_source_id = f"{parent_source_id}:closing-holding"
        child_hash_payload = "|".join(
            (
                item["content_sha256"],
                "closing-holding",
                item["market"],
                item["symbol"],
            )
        ).encode("utf-8")
        holdings.append(
            {
                "source_id": child_source_id,
                "content_sha256": f"sha256:{hashlib.sha256(child_hash_payload).hexdigest()}",
                "as_of": item["as_of"],
                "market": item["market"],
                "symbol": item["symbol"],
                "name": item["name"],
                "quantity": quantity,
                "market_price": market_price,
                "average_cost": item.get("average_cost"),
                "currency": item["currency"],
                "fx_to_base": item.get("fx_to_base"),
            }
        )
    return holdings


def _merge_current_holdings(
    *,
    broker_holdings: Sequence[_HoldingDraft],
    supplemental_holdings: Sequence[_HoldingDraft],
) -> list[_HoldingDraft]:
    """Overlay explicit supplemental rows without discarding other broker balances."""

    merged = {(item["market"], item["symbol"]): item for item in broker_holdings}
    latest_supplemental: dict[tuple[str, str], _HoldingDraft] = {}
    for item in supplemental_holdings:
        key = (item["market"], item["symbol"])
        previous = latest_supplemental.get(key)
        if previous is None or previous["as_of"] <= item["as_of"]:
            latest_supplemental[key] = item
    merged.update(latest_supplemental)
    return [merged[key] for key in sorted(merged)]


def _parse_current_holdings(
    rows: Sequence[Mapping[str, object]],
    *,
    fallback_time: datetime | None,
) -> tuple[
    list[_HoldingDraft],
    list[_UnresolvedDraft],
    list[datetime],
    set[str],
    set[str],
]:
    holdings: list[_HoldingDraft] = []
    unresolved: list[_UnresolvedDraft] = []
    times: list[datetime] = []
    currencies: set[str] = set()
    markets: set[str] = set()
    for source_index, raw_row in enumerate(rows):
        row = _canonical_import_row(raw_row)
        market, symbol = _infer_market_and_symbol(
            _text_value(_field(row, "symbol")),
            _text_value(_field(row, "market")),
        )
        currency = _infer_record_currency(_field(row, "currency"), market=market)
        as_of = _parse_import_datetime(_field(row, "occurred_at"), market=market) or fallback_time
        quantity = _optional_decimal(_field(row, "quantity"))
        market_price = _optional_decimal(_field(row, "market_price"))
        market_value = _optional_decimal(_field(row, "market_value"))
        average_cost = _optional_decimal(_field(row, "average_cost"))
        fx = _optional_decimal(_field(row, "fx_to_base"))
        if (
            market_price is None
            and market_value is not None
            and quantity is not None
            and quantity > 0
        ):
            market_price = market_value / quantity
        missing: list[str] = []
        if market is None or symbol is None:
            missing.append("security code/market")
        if as_of is None:
            missing.append("holding date")
        if quantity is None or quantity <= 0:
            missing.append("positive holding quantity")
        if market_price is None or market_price <= 0:
            missing.append("market price or market value")
        if currency is None:
            missing.append("currency")
        if average_cost is not None and average_cost < 0:
            missing.append("non-negative average cost")
        if fx is not None and fx <= 0:
            missing.append("positive exchange rate")
        if missing:
            unresolved.append(
                {
                    "source_index": source_index,
                    "reason": "current holding missing " + ", ".join(missing),
                }
            )
            continue
        if (
            market is None
            or symbol is None
            or as_of is None
            or quantity is None
            or market_price is None
            or currency is None
        ):
            raise TypeError("validated current holding is missing")
        row_hash = _mapping_hash(row)
        holdings.append(
            {
                "source_id": f"holding-row-{source_index + 1}",
                "content_sha256": f"sha256:{row_hash}",
                "as_of": as_of,
                "market": market,
                "symbol": symbol,
                "name": _text_value(_field(row, "name")) or symbol,
                "quantity": quantity,
                "market_price": market_price,
                "average_cost": average_cost,
                "currency": currency,
                "fx_to_base": fx,
            }
        )
        times.append(as_of)
        currencies.add(currency)
        markets.add(market)
    return holdings, unresolved, times, currencies, markets


def _infer_base_currency(
    *,
    declared: set[str],
    record_currencies: set[str],
    trades: Sequence[_TradeDraft],
    cash_flows: Sequence[_CashFlowDraft],
    holdings: Sequence[_HoldingDraft],
) -> str | None:
    if len(declared) == 1:
        return next(iter(declared))
    if len(declared) > 1:
        return None
    if len(record_currencies) == 1:
        return next(iter(record_currencies))
    records = (*trades, *cash_flows, *holdings)
    candidates = [
        currency
        for currency in sorted(record_currencies)
        if all(
            item.get("currency") == currency or item.get("fx_to_base") is not None
            for item in records
        )
    ]
    return candidates[0] if len(candidates) == 1 else None


def analyze_portfolio_review(source: PortfolioReviewInput) -> PortfolioReviewAnalysis:
    """Analyze one confirmed import without persistence or external calls."""

    if not source.confirmed:
        raise PortfolioReviewInputError("portfolio import must be confirmed before analysis")

    positions, total_market_value = _snapshot(source)
    trade_events, trade_attribution = _trades(source)
    cash_events = _cash_flows(source)
    performance, performance_reason = _performance(source)
    performance_grade, performance_capability_reason = _performance_capability(
        source,
        performance,
        unavailable_reason=performance_reason,
    )
    attribution, attribution_grade, attribution_reason = _attribution(
        source,
        trade_attribution,
    )
    imported_market_replay = _market_ticks(source)

    tick_granularities = set(imported_market_replay["granularities"])
    if imported_market_replay["events"]:
        market_replay_grade: CapabilityGrade = "partial"
        market_replay_reason = (
            "customer-imported observations carry a tick label, but provider granularity "
            "and redistribution rights are not independently verified"
            if "tick" in tick_granularities
            else "customer-imported market observations are bar-level, not tick-level"
        )
        market_replay_granularity: str | None = ",".join(sorted(tick_granularities))
    else:
        market_replay_grade = "unavailable"
        market_replay_reason = "no market observations were imported"
        market_replay_granularity = None

    exact_trade_times = bool(trade_events) and all(
        event["timestamp_precision"] == "exact" for event in trade_events
    )
    capabilities: dict[str, _CapabilityData] = {
        "snapshot": _capability(
            "available" if positions else "unavailable",
            (
                "holdings are confirmed; A/H symbols passed syntactic normalization only"
                if positions
                else "no reconciled current holdings were supplied or reconstructed"
            ),
            "point_in_time" if positions else None,
        ),
        "trade_replay": _capability(
            "available" if trade_events else "unavailable",
            (
                "exact trade timestamps were imported"
                if exact_trade_times
                else "broker records provide trade dates but not exact execution times"
                if trade_events
                else "no trade records were imported"
            ),
            "executed_at" if exact_trade_times else "date" if trade_events else None,
        ),
        "performance": _capability(
            performance_grade,
            performance_capability_reason,
            "1d" if performance is not None else None,
        ),
        "attribution": _capability(
            attribution_grade,
            attribution_reason,
            "broker_reported_or_fifo_derived_realized_pnl" if attribution is not None else None,
        ),
        "market_tick_replay": _capability(
            market_replay_grade,
            market_replay_reason,
            market_replay_granularity,
        ),
    }

    warnings: list[str] = []
    if not positions:
        warnings.append(
            "snapshot unavailable: broker transactions alone do not prove current holdings"
        )
    if performance is None:
        warnings.append(f"performance unavailable: {performance_reason}")
    elif performance_grade == "partial":
        warnings.append(f"performance is partial: {performance_capability_reason}")
    if attribution_grade == "partial":
        warnings.append(
            "attribution is partial because some sells have neither broker-reported P&L "
            "nor complete imported FIFO cost coverage"
        )
    if attribution_grade == "unavailable":
        warnings.append(
            "historical attribution and highlights require broker-reported P&L or a sell "
            "fully covered by earlier imported buys"
        )
    if market_replay_grade == "partial":
        warnings.append(
            "market observations remain customer-labelled/imported and are not verified true ticks"
        )

    highlights = _highlights(trade_attribution)
    replay_frames = _replay_frames(
        source,
        trade_events=trade_events,
        cash_events=cash_events,
        performance=performance,
        market_events=imported_market_replay["events"],
        highlights=highlights,
    )
    net_external_flow = _summary_external_flow(source, cash_events)
    snapshot_value: Decimal | None = total_market_value if positions else None
    summary = {
        "total_market_value_base": snapshot_value,
        "ending_equity_base": performance["end_equity_base"] if performance is not None else None,
        "net_external_flow_base": net_external_flow,
        "external_flow_base": net_external_flow,
        "period_return_pct": (None if performance is None else _as_percent(performance["twr"])),
        "twr_pct": None if performance is None else _as_percent(performance["twr"]),
        "max_drawdown_pct": (
            None if performance is None else _as_percent(performance["max_drawdown"])
        ),
    }
    series = _series_projection(performance)
    holding_projection = _holding_projection(positions, total_market_value)
    capability_projection: dict[str, bool] = {
        key: value["grade"] == "available" for key, value in capabilities.items()
    }
    record_count = (
        len(source.holdings)
        + len(source.trades)
        + len(source.cash_flows)
        + len(source.daily_equity)
        + len(source.market_ticks)
    )

    payload: dict[str, object] = {
        "profile": source.profile,
        "import_metadata": {
            "method": source.import_method,
            "confirmed": True,
        },
        "validity": {
            "valid": True,
            "confirmed": True,
            "record_count": record_count,
            "security_identity_scope": "syntactic_a_h_normalization_only",
        },
        "account": {
            "period_start": source.account.period_start,
            "period_end": source.account.period_end,
            "base_currency": source.account.base_currency,
            "timezone": source.account.timezone,
        },
        "period": {
            "start": source.account.period_start,
            "end": source.account.period_end,
        },
        "summary": summary,
        "capabilities": capability_projection,
        "data_capabilities": capabilities,
        "data_quality": {
            "level": "confirmed_import",
            "warnings": tuple(warnings),
        },
        "narrative_status": {
            "available": False,
            "mode": "on_demand",
            "reason": "server-side research API key is not configured",
        },
        "snapshot": (
            {
                "as_of": max(item.as_of for item in source.holdings),
                "total_market_value_base": total_market_value,
                "positions": positions,
            }
            if positions
            else None
        ),
        "holdings": holding_projection,
        "trade_replay": {
            "granularity": "executed_at" if exact_trade_times else "date",
            "events": trade_events,
        },
        "cash_flow_replay": {
            "granularity": "occurred_at",
            "events": cash_events,
        },
        "performance": performance,
        "series": series,
        "attribution": attribution,
        "market_tick_replay": imported_market_replay,
        "replay_ticks": replay_frames,
        "highlights": highlights,
        "warnings": tuple(warnings),
    }
    return PortfolioReviewAnalysis(payload=payload)


def build_verified_highlight(
    source: PortfolioReviewInput,
    highlight_index: int,
    *,
    market_data: MarketDataRepository | None = None,
) -> VerifiedPortfolioHighlight:
    """Rebuild one model-safe highlight from ledger evidence and optional daily bars."""

    if not source.confirmed:
        raise PortfolioReviewInputError("portfolio import must be confirmed before analysis")
    _trade_events, attribution_entries = _trades(source)
    raw_highlights = _highlights(attribution_entries)
    if not 0 <= highlight_index < len(raw_highlights):
        raise PortfolioReviewInputError("highlight_index does not select a verified highlight")
    selected = raw_highlights[highlight_index]
    amount = selected["amount_base"]
    occurred_at = selected["at"]
    symbol = selected["symbol"]
    market = selected["market"]
    source_id = selected.get("source_id")
    content_sha256 = selected.get("content_sha256")
    evidence_id = (
        str(source_id)
        if isinstance(source_id, str) and source_id
        else f"ledger_highlight_{highlight_index}"
    )
    record_identity = ""
    if isinstance(content_sha256, str) and content_sha256:
        record_identity = f" 记录哈希 {content_sha256}。"
    calculation_method = selected["calculation_method"]
    performance_statement = (
        (
            f"{occurred_at.date().isoformat()} 客户导入成交记录显示卖出 {symbol}，"
            f"券商报告已实现盈亏 {_decimal_text(amount)} "
            f"{source.account.base_currency}。{record_identity}"
        )
        if calculation_method == "broker_reported"
        else (
            f"{occurred_at.date().isoformat()} 客户导入成交记录显示卖出 {symbol}；"
            "该笔卖出此前导入的买入数量足以覆盖，系统按 FIFO 成本法，使用成交价、"
            f"费用与逐笔汇率计算已实现盈亏 {_decimal_text(amount)} "
            f"{source.account.base_currency}。这是导入流水上的成本法结果，不是券商"
            f"直接提供的已实现盈亏，并假设导入区间内无未记录公司行动。{record_identity}"
        )
    )
    evidence: list[VerifiedPerformanceEvidence] = [
        VerifiedPerformanceEvidence(
            evidence_id=evidence_id,
            statement=performance_statement,
        )
    ]
    if market_data is not None and market == "CN_A":
        evidence.extend(
            _daily_market_evidence(
                source,
                symbol=symbol,
                occurred_at=occurred_at,
                market_data=market_data,
            )
        )
    return VerifiedPortfolioHighlight(
        symbol=symbol,
        name=str(selected["name"]),
        market=market,
        action="卖出并实现盈利",
        occurred_at=occurred_at,
        performance_evidence=tuple(evidence),
    )


def _daily_market_evidence(
    source: PortfolioReviewInput,
    *,
    symbol: str,
    occurred_at: datetime,
    market_data: MarketDataRepository,
) -> tuple[VerifiedPerformanceEvidence, ...]:
    """Load immutable daily bars through the project's existing snapshot boundary."""

    instrument = InstrumentId(symbol)
    event_date = occurred_at.astimezone(ZoneInfo(source.account.timezone)).date()
    period = DateRange(source.account.period_start, min(source.account.period_end, event_date))
    requirements = DataRequirements(
        instruments=(instrument,),
        datasets=("daily_ohlcv",),
    )
    snapshot = market_data.pin_snapshot(requirements, period)
    bars = tuple(market_data.load_daily_bars(snapshot, instrument, period))
    if not bars:
        return ()
    ordered = tuple(
        sorted(
            (item for item in bars if item.available_at <= occurred_at),
            key=lambda item: item.session_date,
        )
    )
    if not ordered:
        return ()
    if any(item.price_basis is not PriceBasis.UNADJUSTED for item in ordered):
        raise PortfolioReviewInputError("historical market evidence must use unadjusted prices")
    event_bar = next(
        (item for item in reversed(ordered) if item.session_date <= event_date),
        None,
    )
    if event_bar is None:
        return ()
    currency = event_bar.close.currency
    evidence = [
        VerifiedPerformanceEvidence(
            evidence_id="trusted_daily_close",
            statement=(
                f"受信历史快照显示 {symbol} 在高光日最近交易日 "
                f"{event_bar.session_date.isoformat()} 的未复权收盘价为 "
                f"{_decimal_text(event_bar.close.amount)} {currency}；该价格只作市场背景，"
                f"known_as_of={occurred_at.isoformat()}。"
            ),
        )
    ]
    first = ordered[0]
    last = ordered[-1]
    if first.session_date != last.session_date and first.close.amount > 0:
        change_pct = (last.close.amount / first.close.amount - Decimal("1")) * Decimal("100")
        evidence.append(
            VerifiedPerformanceEvidence(
                evidence_id="trusted_daily_window",
                statement=(
                    f"受信历史快照覆盖 {first.session_date.isoformat()} 至 "
                    f"{last.session_date.isoformat()}，{symbol} 未复权收盘价区间变化 "
                    f"{_decimal_text(change_pct)}%；这不是客户账户收益，"
                    f"known_as_of={occurred_at.isoformat()}。"
                ),
            )
        )
    return tuple(evidence)


def _capability(
    grade: CapabilityGrade,
    reason: str,
    granularity: str | None,
) -> _CapabilityData:
    return {
        "grade": grade,
        "available": grade != "unavailable",
        "reason": reason,
        "granularity": granularity,
    }


def _fx(currency: str, fx_to_base: Decimal | None, base_currency: str) -> Decimal:
    if currency == base_currency:
        return Decimal("1")
    if fx_to_base is None or fx_to_base <= 0:
        raise PortfolioReviewInputError(
            f"positive fx_to_base is required for {currency} records in a {base_currency} account"
        )
    return fx_to_base


def _snapshot(
    source: PortfolioReviewInput,
) -> tuple[list[_SnapshotPosition], Decimal]:
    positions: list[_SnapshotPosition] = []
    total = Decimal("0")
    for item in sorted(source.holdings, key=lambda value: (value.market, value.symbol)):
        fx = _fx(item.currency, item.fx_to_base, source.account.base_currency)
        market_value = item.quantity * item.market_price
        market_value_base = market_value * fx
        total += market_value_base
        unrealized_pnl_base = (
            None
            if item.average_cost is None
            else (item.market_price - item.average_cost) * item.quantity * fx
        )
        positions.append(
            {
                "as_of": item.as_of,
                "source_id": item.source_id,
                "content_sha256": item.content_sha256,
                "market": item.market,
                "symbol": item.symbol,
                "name": item.name,
                "quantity": item.quantity,
                "market_price": item.market_price,
                "average_cost": item.average_cost,
                "currency": item.currency,
                "fx_to_base": fx,
                "market_value": market_value,
                "market_value_base": market_value_base,
                "unrealized_pnl_base": unrealized_pnl_base,
            }
        )
    return positions, total


def _holding_projection(
    positions: list[_SnapshotPosition],
    total_market_value: Decimal,
) -> list[_HoldingProjection]:
    projected: list[_HoldingProjection] = []
    for item in positions:
        market_value = item["market_value_base"]
        projected.append(
            {
                **item,
                "weight_pct": (
                    Decimal("0")
                    if total_market_value == 0
                    else market_value / total_market_value * Decimal("100")
                ),
            }
        )
    return projected


def _trades(
    source: PortfolioReviewInput,
) -> tuple[list[_TradeEvent], list[_AttributionEntry]]:
    events: list[_TradeEvent] = []
    attribution: list[_AttributionEntry] = []
    fifo_lots: dict[tuple[str, str], list[_FifoLot]] = {}
    incomplete_fifo_symbols: set[tuple[str, str]] = set()
    ordered = sorted(
        enumerate(source.trades),
        key=lambda pair: (pair[1].executed_at, pair[0]),
    )
    account_zone = ZoneInfo(source.account.timezone)
    trade_day_sides: dict[tuple[str, str, date], set[str]] = {}
    imprecise_trade_days: set[tuple[str, str, date]] = set()
    for _source_index, trade in ordered:
        day_key = (
            trade.market,
            trade.symbol,
            trade.executed_at.astimezone(account_zone).date(),
        )
        trade_day_sides.setdefault(day_key, set()).add(trade.side)
        if trade.timestamp_precision == "date_only":
            imprecise_trade_days.add(day_key)
    ambiguous_fifo_days = {
        day_key
        for day_key, sides in trade_day_sides.items()
        if len(sides) > 1 and day_key in imprecise_trade_days
    }
    for source_index, item in ordered:
        fx = _fx(item.currency, item.fx_to_base, source.account.base_currency)
        key = (item.market, item.symbol)
        day_key = (
            item.market,
            item.symbol,
            item.executed_at.astimezone(account_zone).date(),
        )
        if day_key in ambiguous_fifo_days:
            incomplete_fifo_symbols.add(key)
            fifo_lots.setdefault(key, []).clear()
        broker_realized_pnl_base = None if item.realized_pnl is None else item.realized_pnl * fx
        fifo_realized_pnl_base: Decimal | None = None
        if item.side == "buy":
            if key not in incomplete_fifo_symbols:
                total_cost_base = (item.quantity * item.price + item.fees) * fx
                fifo_lots.setdefault(key, []).append(
                    {
                        "quantity": item.quantity,
                        "unit_cost_base": total_cost_base / item.quantity,
                    }
                )
        else:
            lots = fifo_lots.setdefault(key, [])
            fifo_cost_base = (
                None if key in incomplete_fifo_symbols else _consume_fifo_cost(lots, item.quantity)
            )
            if fifo_cost_base is None:
                incomplete_fifo_symbols.add(key)
                lots.clear()
            elif broker_realized_pnl_base is None:
                proceeds_base = item.quantity * item.price * fx
                fifo_realized_pnl_base = proceeds_base - item.fees * fx - fifo_cost_base

        realized_pnl_base = (
            broker_realized_pnl_base
            if broker_realized_pnl_base is not None
            else fifo_realized_pnl_base
        )
        realized_pnl_source = (
            "broker_reported"
            if broker_realized_pnl_base is not None
            else "fifo_derived"
            if fifo_realized_pnl_base is not None
            else None
        )
        events.append(
            {
                "source_index": source_index,
                "source_id": item.source_id,
                "content_sha256": item.content_sha256,
                "executed_at": item.executed_at,
                "timestamp_precision": item.timestamp_precision,
                "market": item.market,
                "symbol": item.symbol,
                "name": item.name,
                "side": item.side,
                "quantity": item.quantity,
                "price": item.price,
                "fees": item.fees,
                "currency": item.currency,
                "fx_to_base": fx,
                "notional_base": item.quantity * item.price * fx,
                "fees_base": item.fees * fx,
                "realized_pnl_base": realized_pnl_base,
                "realized_pnl_source": realized_pnl_source,
            }
        )
        if item.side == "sell" and realized_pnl_base is not None:
            if realized_pnl_source is None:
                raise TypeError("realized P&L source is missing")
            evidence_fields = (
                ("trades.realized_pnl", "trades.fx_to_base")
                if realized_pnl_source == "broker_reported"
                else (
                    "trades.quantity",
                    "trades.price",
                    "trades.fees",
                    "trades.fx_to_base",
                    "fifo_from_imported_trades",
                )
            )
            attribution.append(
                {
                    "kind": "trade_realized_pnl",
                    "source_index": source_index,
                    "source_id": item.source_id,
                    "content_sha256": item.content_sha256,
                    "at": item.executed_at,
                    "market": item.market,
                    "symbol": item.symbol,
                    "name": item.name or item.symbol,
                    "amount_base": realized_pnl_base,
                    "calculation_method": realized_pnl_source,
                    "evidence_fields": evidence_fields,
                }
            )
    return events, attribution


def _consume_fifo_cost(lots: list[_FifoLot], quantity: Decimal) -> Decimal | None:
    if sum((lot["quantity"] for lot in lots), start=Decimal("0")) < quantity:
        return None
    remaining = quantity
    cost_base = Decimal("0")
    while remaining > 0:
        lot = lots[0]
        consumed = min(remaining, lot["quantity"])
        cost_base += consumed * lot["unit_cost_base"]
        lot["quantity"] -= consumed
        remaining -= consumed
        if lot["quantity"] == 0:
            lots.pop(0)
    return cost_base


def _cash_flows(source: PortfolioReviewInput) -> list[_CashEvent]:
    events: list[_CashEvent] = []
    ordered = sorted(
        enumerate(source.cash_flows),
        key=lambda pair: (pair[1].occurred_at, pair[0]),
    )
    for source_index, item in ordered:
        fx = _fx(item.currency, item.fx_to_base, source.account.base_currency)
        amount_base = item.amount * fx
        signed_external = _signed_external_flow(item, amount_base)
        events.append(
            {
                "source_index": source_index,
                "source_id": item.source_id,
                "content_sha256": item.content_sha256,
                "occurred_at": item.occurred_at,
                "kind": item.kind,
                "amount": item.amount,
                "currency": item.currency,
                "fx_to_base": fx,
                "external": item.external,
                "amount_base": amount_base,
                "external_flow_base": signed_external,
            }
        )
    return events


def _signed_external_flow(item: CashFlowRecord, amount_base: Decimal) -> Decimal:
    if not item.external:
        return Decimal("0")
    if item.kind in {"deposit", "in_kind_transfer_in"}:
        return amount_base
    if item.kind in {"withdrawal", "in_kind_transfer_out"}:
        return -amount_base
    raise PortfolioReviewInputError(f"cash-flow kind {item.kind!r} cannot be external")


def _summary_external_flow(
    source: PortfolioReviewInput,
    cash_events: Sequence[_CashEvent],
) -> Decimal:
    """Use cash records as the ledger and add only unmatched explicit equity flows."""

    total = sum(
        (event["external_flow_base"] for event in cash_events),
        start=Decimal("0"),
    )
    if not source.daily_equity:
        return total

    account_zone = ZoneInfo(source.account.timezone)
    equity_records = sorted(source.daily_equity, key=lambda item: item.at)
    for index, current in enumerate(equity_records):
        if current.external_inflow_base is None and current.external_outflow_base is None:
            continue
        interval_start = source.account.period_start if index == 0 else equity_records[index - 1].at
        include_start = index == 0
        matching = tuple(
            event
            for event in cash_events
            if (
                interval_start <= event["occurred_at"].astimezone(account_zone).date() <= current.at
                if include_start
                else interval_start
                < event["occurred_at"].astimezone(account_zone).date()
                <= current.at
            )
        )
        cash_inflow = sum(
            (max(event["external_flow_base"], Decimal("0")) for event in matching),
            start=Decimal("0"),
        )
        cash_outflow = sum(
            (max(-event["external_flow_base"], Decimal("0")) for event in matching),
            start=Decimal("0"),
        )
        explicit_inflow = current.external_inflow_base or Decimal("0")
        explicit_outflow = current.external_outflow_base or Decimal("0")
        if cash_inflow or cash_outflow:
            if cash_inflow != explicit_inflow or cash_outflow != explicit_outflow:
                raise PortfolioReviewInputError(
                    "daily_equity external inflow/outflow conflicts with imported cash flows "
                    f"for summary interval ending {current.at.isoformat()}"
                )
            continue
        total += explicit_inflow - explicit_outflow
    return total


def _performance(
    source: PortfolioReviewInput,
) -> tuple[_PerformanceData | None, str]:
    records = sorted(source.daily_equity, key=lambda item: item.at)
    if len(records) < 2:
        return None, "at least two daily_equity records are required"

    account_zone = ZoneInfo(source.account.timezone)
    external_flows: list[tuple[date, Decimal, Decimal]] = []
    for item in source.cash_flows:
        amount_base = item.amount * _fx(
            item.currency,
            item.fx_to_base,
            source.account.base_currency,
        )
        signed = _signed_external_flow(item, amount_base)
        if signed:
            external_flows.append(
                (
                    item.occurred_at.astimezone(account_zone).date(),
                    max(signed, Decimal("0")),
                    max(-signed, Decimal("0")),
                )
            )

    first_inflow = records[0].external_inflow_base or Decimal("0")
    first_outflow = records[0].external_outflow_base or Decimal("0")
    points: list[_PerformancePoint] = [
        {
            "at": records[0].at,
            "source_id": records[0].source_id,
            "content_sha256": records[0].content_sha256,
            "equity_base": records[0].equity_base,
            "external_inflow_base": first_inflow,
            "external_outflow_base": first_outflow,
            "external_flow_base": first_inflow - first_outflow,
            "period_return": None,
            "linked_index": Decimal("1"),
            "drawdown": Decimal("0"),
        }
    ]
    linked_index = Decimal("1")
    peak = linked_index
    max_drawdown = Decimal("0")
    for previous, current in pairwise(records):
        derived_inflow = sum(
            (
                inflow
                for flow_date, inflow, _outflow in external_flows
                if previous.at < flow_date <= current.at
            ),
            start=Decimal("0"),
        )
        derived_outflow = sum(
            (
                outflow
                for flow_date, _inflow, outflow in external_flows
                if previous.at < flow_date <= current.at
            ),
            start=Decimal("0"),
        )
        explicit_flows = (
            current.external_inflow_base is not None or current.external_outflow_base is not None
        )
        if explicit_flows:
            inflow = current.external_inflow_base or Decimal("0")
            outflow = current.external_outflow_base or Decimal("0")
            if (derived_inflow or derived_outflow) and (
                inflow != derived_inflow or outflow != derived_outflow
            ):
                raise PortfolioReviewInputError(
                    "daily_equity external inflow/outflow conflicts with imported cash flows "
                    f"for interval ({previous.at.isoformat()}, {current.at.isoformat()}]"
                )
        else:
            inflow = derived_inflow
            outflow = derived_outflow

        denominator = previous.equity_base + inflow
        numerator = current.equity_base + outflow
        if denominator <= 0 or numerator <= 0:
            return None, "cash-flow-adjusted equity must stay positive for every linked interval"
        period_return = numerator / denominator - Decimal("1")
        linked_index *= Decimal("1") + period_return
        if linked_index <= 0:
            return None, "linked return index must remain positive"
        peak = max(peak, linked_index)
        drawdown = linked_index / peak - Decimal("1")
        max_drawdown = min(max_drawdown, drawdown)
        points.append(
            {
                "at": current.at,
                "source_id": current.source_id,
                "content_sha256": current.content_sha256,
                "equity_base": current.equity_base,
                "external_inflow_base": inflow,
                "external_outflow_base": outflow,
                "external_flow_base": inflow - outflow,
                "period_return": period_return,
                "linked_index": linked_index,
                "drawdown": drawdown,
            }
        )

    return (
        {
            "method": "linked_daily_ttwror.external_flows.v1",
            "granularity": "1d",
            "twr": linked_index - Decimal("1"),
            "max_drawdown": max_drawdown,
            "start_equity_base": records[0].equity_base,
            "end_equity_base": records[-1].equity_base,
            "points": points,
        },
        "",
    )


def _performance_capability(
    source: PortfolioReviewInput,
    performance: _PerformanceData | None,
    *,
    unavailable_reason: str,
) -> tuple[CapabilityGrade, str]:
    if performance is None:
        return "unavailable", unavailable_reason
    points = performance["points"]
    covers_period = (
        len(points) >= 3
        and points[0]["at"] == source.account.period_start
        and points[-1]["at"] == source.account.period_end
    )
    if covers_period:
        return (
            "available",
            "TTWROR links equity observations across the account period after separating "
            "external inflows and outflows",
        )
    return (
        "partial",
        "TTWROR and drawdown cover only the imported observation dates; at least three "
        "samples including both account-period endpoints are required for full coverage",
    )


def _series_projection(performance: _PerformanceData | None) -> list[_SeriesPoint]:
    if performance is None:
        return []
    raw_points = performance["points"]
    series: list[_SeriesPoint] = []
    for item in raw_points:
        period_return = item["period_return"]
        drawdown = item["drawdown"]
        series.append(
            {
                "at": item["at"],
                "source_id": item["source_id"],
                "content_sha256": item["content_sha256"],
                "equity_base": item["equity_base"],
                "return_pct": None if period_return is None else _as_percent(period_return),
                "drawdown_pct": _as_percent(drawdown),
            }
        )
    return series


def _attribution(
    source: PortfolioReviewInput,
    entries: list[_AttributionEntry],
) -> tuple[_AttributionData | None, CapabilityGrade, str]:
    expected_evidence = sum(1 for item in source.trades if item.side == "sell")
    if expected_evidence == 0:
        return None, "unavailable", "no sell records were imported"
    if not entries:
        return (
            None,
            "unavailable",
            "sell records have neither broker-reported realized P&L nor complete imported "
            "FIFO purchase-cost coverage",
        )
    grade: CapabilityGrade = "available" if len(entries) == expected_evidence else "partial"
    broker_count = sum(1 for entry in entries if entry["calculation_method"] == "broker_reported")
    fifo_count = len(entries) - broker_count
    if grade == "partial":
        reason = (
            f"attributed {len(entries)} of {expected_evidence} sells: {broker_count} broker-"
            f"reported and {fifo_count} FIFO-derived; remaining sells lack complete cost evidence"
        )
    elif fifo_count and broker_count:
        reason = (
            f"all sells are attributed: {broker_count} broker-reported and {fifo_count} "
            "FIFO-derived from complete preceding imported buys"
        )
    elif fifo_count:
        reason = "all sells are FIFO-derived from complete preceding imported buys"
    else:
        reason = "all sells carry broker-reported realized P&L"
    total = sum((entry["amount_base"] for entry in entries), start=Decimal("0"))
    normalized: list[_NormalizedAttributionEntry] = []
    for entry in entries:
        amount = entry["amount_base"]
        normalized.append(
            {
                **entry,
                "share_of_verified_pnl": None if total == 0 else amount / total,
            }
        )
    return (
        {
            "method": "broker_reported_or_fifo_derived_realized_pnl.v1",
            "coverage": grade,
            "verified_pnl_base": total,
            "entries": normalized,
        },
        grade,
        reason,
    )


def _market_ticks(source: PortfolioReviewInput) -> _MarketReplay:
    events: list[_MarketEvent] = []
    ordered = sorted(
        enumerate(source.market_ticks),
        key=lambda pair: (pair[1].at, pair[0]),
    )
    for source_index, item in ordered:
        fx = (
            None
            if item.currency is None
            else _fx(item.currency, item.fx_to_base, source.account.base_currency)
        )
        events.append(
            {
                "source_index": source_index,
                "source_id": item.source_id,
                "content_sha256": item.content_sha256,
                "at": item.at,
                "granularity": item.granularity,
                "market": item.market,
                "symbol": item.symbol,
                "last_price": item.last_price,
                "account_equity_base": item.account_equity_base,
                "currency": item.currency,
                "fx_to_base": fx,
                "last_price_base": (
                    None if item.last_price is None or fx is None else item.last_price * fx
                ),
            }
        )
    granularities = tuple(sorted({item.granularity for item in source.market_ticks}))
    return {
        "timestamp_origin": "imported",
        "granularities": granularities,
        "interpolation": "none",
        "events": events,
    }


def _highlights(entries: list[_AttributionEntry]) -> list[_Highlight]:
    verified_positive: list[_Highlight] = []
    for entry in entries:
        amount = entry["amount_base"]
        if amount <= 0:
            continue
        name = entry["name"]
        fifo_derived = entry["calculation_method"] == "fifo_derived"
        verified_positive.append(
            {
                "kind": "trade_realized_pnl",
                "type": "已实现盈利",
                "at": entry["at"],
                "market": entry["market"],
                "symbol": entry["symbol"],
                "name": name,
                "amount_base": amount,
                "impact_base": amount,
                "impact_pct": None,
                "title": f"{name} 卖出兑现",
                "subtitle": (
                    "按完整导入成交的 FIFO 成本法计算"
                    if fifo_derived
                    else "券商已实现盈亏记录可核验"
                ),
                "evidence": (
                    "客户确认的成交、费用与逐笔汇率；FIFO 为成本法假设"
                    if fifo_derived
                    else "客户确认的成交与券商已实现盈亏"
                ),
                "calculation_method": entry["calculation_method"],
                "evidence_fields": entry["evidence_fields"],
                "verification": "derived_from_imported_records",
                "trade_source_index": entry["source_index"],
                "source_id": entry["source_id"],
                "content_sha256": entry["content_sha256"],
                "start_tick_index": None,
                "end_tick_index": None,
            }
        )
    verified_positive.sort(
        key=lambda item: (
            -item["amount_base"],
            item["at"],
            item["symbol"],
        )
    )
    return verified_positive[:5]


def _replay_frames(
    source: PortfolioReviewInput,
    *,
    trade_events: list[_TradeEvent],
    cash_events: list[_CashEvent],
    performance: _PerformanceData | None,
    market_events: list[_MarketEvent],
    highlights: list[_Highlight],
) -> list[_ReplayFrame]:
    frames: list[_ReplayFrame] = []
    for event in trade_events:
        side = str(event["side"])
        frames.append(
            {
                "at": event["executed_at"],
                "kind": side,
                "title": (
                    f"{'买入' if side == 'buy' else '卖出'} {event['name'] or event['symbol']}"
                ),
                "market": event["market"],
                "symbol": event["symbol"],
                "account_equity_base": None,
                "last_price": event["price"],
                "return_pct": None,
                "caption": (
                    f"{event['quantity']} 股，成交价 {event['price']} {event['currency']}。"
                    + (
                        " 券商文件只提供日期，画面不代表精确成交时刻。"
                        if event["timestamp_precision"] == "date_only"
                        else ""
                    )
                ),
                "evidence": "客户确认的成交记录",
                "source_kind": "trade",
                "source_index": event["source_index"],
                "source_id": event["source_id"],
                "content_sha256": event["content_sha256"],
                "is_highlight": False,
                "battle_cry": None,
            }
        )
    for event in cash_events:
        frames.append(
            {
                "at": event["occurred_at"],
                "kind": event["kind"],
                "title": _cash_flow_title(str(event["kind"])),
                "market": None,
                "symbol": None,
                "account_equity_base": None,
                "last_price": None,
                "return_pct": None,
                "caption": f"{event['amount']} {event['currency']}。",
                "evidence": "客户确认的资金流水",
                "source_kind": "cash_flow",
                "source_index": event["source_index"],
                "source_id": event["source_id"],
                "content_sha256": event["content_sha256"],
                "is_highlight": False,
                "battle_cry": None,
            }
        )
    if performance is not None:
        points = performance["points"]
        zone = ZoneInfo(source.account.timezone)
        for index, point in enumerate(points):
            frames.append(
                {
                    "at": datetime.combine(point["at"], time(16), tzinfo=zone),
                    "kind": "account_equity",
                    "title": "账户日终净值",
                    "market": None,
                    "symbol": None,
                    "account_equity_base": point["equity_base"],
                    "last_price": None,
                    "return_pct": (
                        None
                        if point["period_return"] is None
                        else _as_percent(point["period_return"])
                    ),
                    "caption": "该点来自客户确认的每日账户总资产。",
                    "evidence": "客户确认的每日净值记录",
                    "source_kind": "daily_equity",
                    "source_index": index,
                    "source_id": point["source_id"],
                    "content_sha256": point["content_sha256"],
                    "is_highlight": False,
                    "battle_cry": None,
                }
            )
    for event in market_events:
        frames.append(
            {
                "at": event["at"],
                "kind": "market_observation",
                "title": event["symbol"] or "账户行情点",
                "market": event["market"],
                "symbol": event["symbol"],
                "account_equity_base": event["account_equity_base"],
                "last_price": event["last_price"],
                "return_pct": None,
                "caption": f"客户导入的 {event['granularity']} 粒度记录。",
                "evidence": "客户确认的行情记录",
                "source_kind": "market_tick",
                "source_index": event["source_index"],
                "source_id": event["source_id"],
                "content_sha256": event["content_sha256"],
                "is_highlight": False,
                "battle_cry": None,
            }
        )
    frames.sort(key=lambda item: (item["at"], item["source_kind"], item["source_index"]))

    highlight_by_trade = {item["trade_source_index"]: item for item in highlights}
    for frame_index, frame in enumerate(frames):
        if frame["source_kind"] != "trade" or frame["kind"] != "sell":
            continue
        highlight = highlight_by_trade.get(frame["source_index"])
        if highlight is None:
            continue
        frame["is_highlight"] = True
        frame["battle_cry"] = "兑现判断"
        highlight["start_tick_index"] = frame_index
        highlight["end_tick_index"] = frame_index
    return frames


def _cash_flow_title(kind: str) -> str:
    return {
        "deposit": "账户入金",
        "withdrawal": "账户出金",
        "in_kind_transfer_in": "证券转入",
        "in_kind_transfer_out": "证券转出",
        "dividend": "收到分红",
        "interest": "收到利息",
        "fee": "账户费用",
        "tax": "账户税费",
        "other": "其他账户流水",
    }[kind]


def _as_percent(value: object) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError("percentage source must be Decimal")
    return value * Decimal("100")


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


__all__ = [
    "CashFlowRecord",
    "DailyEquityRecord",
    "HoldingRecord",
    "MarketTickRecord",
    "PortfolioReviewAnalysis",
    "PortfolioReviewInput",
    "PortfolioReviewInputError",
    "ReviewAccount",
    "TradeRecord",
    "analyze_portfolio_review",
    "build_verified_highlight",
    "infer_broker_ledger_import",
]
