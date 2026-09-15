"""Bounded price-plan parameters shared by language, UI and execution."""

from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from .defaults import DEFAULT_INITIAL_CASH_CNY, DEFAULT_SCHEDULED_BUDGET_CNY

D = Decimal


def validate_grid_buy_quantities(params: GridParameters, minimum: int, increment: int) -> None:
    """Validate planned buys, never existing holdings or actual partial fills."""
    quantities = [('初始建仓', params.initial_shares)]
    if params.sizing_mode == 'shares':
        quantities.append(('每格买入', params.order_shares))
    for label, quantity in quantities:
        if quantity and (quantity < minimum or (quantity - minimum) % increment):
            raise GridSpecificationError('invalid grid buy quantity', code='grid_buy_quantity_invalid',
                safe_message=f'{label}{quantity}股不符合该股票申报规则：买入至少{minimum}股，之后按{increment}股递增。请修改数量，原策略已保留。')


class GridSpecificationError(ValueError):
    """A grid request or its history cannot be executed as stated."""

    def __init__(self, message: str, *, code: str = "grid_parameters_invalid",
                 safe_message: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        # Only explicitly application-authored diagnostics can cross a public
        # or model boundary; legacy call sites may contain non-parameter faults.
        self.safe_message = safe_message or "交易计划尚未通过执行校验，原规则已保留，请检查参数或稍后重试。"


class GridParameters(BaseModel):
    """Persisted choices. Percent uses percentage points: 1 means 1%."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    # Missing fields preserve the existing saved/manual request semantics.
    # New language-generated plans explicitly choose previous_close; saved
    # manual/first_open choices are not migrated by changing this schema.
    anchor_mode: Literal["manual", "first_open", "latest_price", "previous_close"] = "manual"
    observation: Literal["daily_close", "minute_bar"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    anchor_price: Decimal | None = Field(default=None, gt=0, le=1_000_000)
    # Server-populated quote provenance. The supplier's table time label is
    # not asserted to be an exchange tick timestamp.
    anchor_quote_source: str | None = Field(default=None, exclude_if=lambda value: value is None)
    anchor_quote_retrieved_at: str | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    anchor_quote_time_label: str | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    anchor_quote_response_sha256: str | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    anchor_update: Literal["fixed", "last_fill", "last_trigger"] = "fixed"
    startup_mode: Literal["catch_up", "wait_for_crossing"] = "catch_up"
    spacing_mode: Literal["cny", "anchor_percent", "percent"] = "cny"
    spacing: Decimal = Field(default=D(1), gt=0, le=1_000_000)
    # Older saved grids retain their shared spacing. Explicit directional values
    # override only that side, including its unit; never collapse 3%/1% to 1%.
    buy_spacing: Decimal | None = Field(
        default=None, gt=0, le=1_000_000, exclude_if=lambda value: value is None
    )
    sell_spacing: Decimal | None = Field(
        default=None, gt=0, le=1_000_000, exclude_if=lambda value: value is None
    )
    buy_spacing_mode: Literal["cny", "anchor_percent", "percent"] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    sell_spacing_mode: Literal["cny", "anchor_percent", "percent"] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    lower_price: Decimal = Field(gt=0, le=1_000_000)
    upper_price: Decimal = Field(gt=0, le=1_000_000)
    # Optional executable bounds relative to the pinned initial anchor.
    # range_percent means the explicit per-side range, not the per-cell gap.
    range_percent: Decimal | None = Field(default=None, gt=0, lt=100)
    levels_below: int | None = Field(default=None, ge=1, le=10000, strict=True)
    levels_above: int | None = Field(default=None, ge=1, le=10000, strict=True)
    sizing_mode: Literal["shares", "amount"] = "shares"
    order_shares: int = Field(default=100, ge=1, le=1_000_000, strict=True)
    order_amount_cny: Decimal = Field(default=D(10000), gt=0, le=1_000_000_000)
    initial_shares: int = Field(default=0, ge=0, le=100_000_000, strict=True)
    opening_shares: int = Field(default=0, ge=0, le=100_000_000, strict=True)
    initial_capital_scope: Literal["total_equity", "cash_plus_opening_holdings"] = "total_equity"
    min_shares: int = Field(default=0, ge=0, le=100_000_000, strict=True)
    max_shares: int = Field(default=10000, ge=1, le=100_000_000, strict=True)
    max_position_cny: Decimal | None = Field(default=None, gt=0, le=1_000_000_000)
    price_mode: Literal["next_open", "grid_limit", "fixed_limit"] = "next_open"
    buy_limit: Decimal | None = Field(default=None, gt=0, le=1_000_000)
    sell_limit: Decimal | None = Field(default=None, gt=0, le=1_000_000)
    limit_offset_cny: Decimal = Field(default=D(0), ge=0, le=10000)
    initial_cash_cny: Decimal = Field(default=D(DEFAULT_INITIAL_CASH_CNY), gt=0, le=1_000_000_000)
    commission_rate: Decimal = Field(default=D("0.00025"), ge=0, le=D("0.05"))
    minimum_commission_cny: Decimal = Field(default=D(5), ge=0, le=1000)
    stamp_tax_rate: Decimal = Field(default=D("0.0005"), ge=0, le=D("0.05"))
    transfer_fee_rate: Decimal = Field(default=D("0.00001"), ge=0, le=D("0.05"))
    slippage_bps: Decimal = Field(default=D(5), ge=0, le=1000)
    slippage_cny: Decimal = Field(default=D(0), ge=0, exclude_if=lambda value: not value)

    @model_validator(mode="after")
    def consistent(self) -> GridParameters:
        if not self.lower_price < self.upper_price:
            raise ValueError("网格上界必须高于下界")
        if self.anchor_mode == "manual":
            if self.anchor_price is None:
                raise ValueError("手动基准模式须填写基准价")
            if not self.lower_price <= self.anchor_price <= self.upper_price:
                raise ValueError("基准价须在网格范围内")
        if (
            max(self.initial_shares, self.opening_shares) > self.max_shares
            or self.min_shares > self.max_shares
        ):
            raise ValueError("初始建仓和最小底仓均不得超过最大持仓")
        if self.initial_shares and self.opening_shares:
            raise ValueError("首日新建仓与期初已有持仓不能同时设置")
        for side in ("buy", "sell"):
            mode, spacing = self.spacing_for(side)
            if mode != "cny" and spacing >= 100:
                raise ValueError("百分比格距须小于100%，1表示1%")
        if self.price_mode == "fixed_limit" and (self.buy_limit is None or self.sell_limit is None):
            raise ValueError("固定限价须同时填写买入限价与卖出限价")
        if any(
            value is not None and value % D("0.01")
            for value in (
                self.anchor_price,
                self.lower_price,
                self.upper_price,
                self.buy_limit,
                self.sell_limit,
                self.limit_offset_cny,
            )
        ):
            raise ValueError("A股价格请精确到分")
        return self

    @property
    def asymmetric(self) -> bool:
        return self.spacing_for("buy") != self.spacing_for("sell")

    def resolve_geometry(self) -> GridParameters:
        """Compile relative bounds once, without changing either side's spacing."""
        if self.range_percent is None and self.levels_below is None and self.levels_above is None:
            return self
        anchor = self.resolved_anchor
        lower, upper = self.lower_price, self.upper_price
        if self.range_percent is not None:
            lower = max(lower, anchor * (1 - self.range_percent / 100))
            upper = min(upper, anchor * (1 + self.range_percent / 100))
        for side, count in (("buy", self.levels_below), ("sell", self.levels_above)):
            if count is None:
                continue
            mode, spacing = self.spacing_for(side)
            if mode == "percent":
                ratio = (1 + spacing / 100) ** count
                edge = anchor / ratio if side == "buy" else anchor * ratio
            else:
                step = spacing if mode == "cny" else anchor * spacing / 100
                edge = anchor - step * count if side == "buy" else anchor + step * count
            if edge <= 0:
                message = "网格层数与间距导致价格不为正，请调整层数或间距"
                raise GridSpecificationError(message, code="grid_nonpositive_level", safe_message=message)
            if not lower <= edge <= upper:
                executable_edge = edge.quantize(
                    D("0.01"), rounding=ROUND_FLOOR if side == "buy" else ROUND_CEILING,
                )
                raise GridSpecificationError(
                    "网格层数、间距与价格范围不一致，不能省略层数或改写间距",
                    code="grid_geometry_conflict",
                    safe_message=(f"网格参数有冲突：基准价{anchor:g}元，"
                                  f"{'下方' if side == 'buy' else '上方'}{count}格的边界为"
                                  f"{executable_edge:.2f}元（理论值{edge:.6f}元，"
                                  f"{'向下' if side == 'buy' else '向上'}取到分），"
                                  f"超出设定的{lower:.2f}–{upper:.2f}元范围。"
                                  "请调整价格区间或格数；原规则已保留，本次未启动回测。"),
                )
            if side == "buy":
                lower = edge
            else:
                upper = edge
        return self.model_copy(
            update={
                "lower_price": max(D("0.01"), lower.quantize(D("0.01"), rounding=ROUND_FLOOR)),
                "upper_price": upper.quantize(D("0.01"), rounding=ROUND_CEILING),
            }
        )

    def spacing_for(self, side: str) -> tuple[str, Decimal]:
        return (
            getattr(self, f"{side}_spacing_mode") or self.spacing_mode,
            getattr(self, f"{side}_spacing") or self.spacing,
        )

    def for_side(self, side: str) -> GridParameters:
        mode, spacing = self.spacing_for(side)
        return self.model_copy(
            update={
                "spacing_mode": mode,
                "spacing": spacing,
                "buy_spacing": None,
                "sell_spacing": None,
                "buy_spacing_mode": None,
                "sell_spacing_mode": None,
            }
        )

    @property
    def resolved_anchor(self) -> Decimal:
        if self.anchor_price is None:
            raise GridSpecificationError("自动基准价须先从回测起始行情确定")
        return self.anchor_price

    def distance(self, price: Decimal) -> Decimal:
        mode, spacing = self.spacing_for("buy")
        bounded = max(self.lower_price, min(self.upper_price, price))
        if mode != "percent":
            step = spacing if mode == "cny" else (self.resolved_anchor * spacing / 100)
            return (self.resolved_anchor - bounded) / step
        ratio = D(1) + spacing / 100
        value = (self.resolved_anchor / bounded).ln() / ratio.ln()
        integer = value.to_integral_value()
        return integer if abs(value - integer) < D("1e-20") else value

    def line(self, buy_distance: Decimal) -> Decimal:
        mode, spacing = self.spacing_for("buy")
        if mode != "percent":
            step = spacing if mode == "cny" else (self.resolved_anchor * spacing / 100)
            return self.resolved_anchor - buy_distance * step
        return self.resolved_anchor / ((D(1) + spacing / 100) ** buy_distance)


class ConditionRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    kind: Literal[
        "price",
        "rebound",
        "pullback",
        "take_profit",
        "stop_loss",
        "holding_period",
        "relative_price",
    ]
    side: Literal["buy", "sell"]
    direction: Literal["up", "down"] = "up"
    price_comparison: Literal["inclusive", "strict"] = "inclusive"
    target_price: Decimal | None = Field(default=None, gt=0, le=1_000_000)
    gap: Decimal | None = Field(default=None, gt=0, le=1_000_000)
    gap_unit: Literal["cny", "percent"] = "percent"
    quantity: int = Field(default=100, ge=1, le=1_000_000, strict=True)
    limit_price: Decimal | None = Field(default=None, gt=0, le=1_000_000)
    # Adjacent rules sharing a group race; the first trigger locks the stage.
    # Other stages stay sequential. This also expresses staged profit-taking.
    group: str | None = Field(default=None, min_length=1, max_length=32)
    sessions: int | None = Field(default=None, ge=1, le=10_000, strict=True)
    sizing_mode: Literal["shares", "amount", "all_position"] = "shares"
    amount_cny: Decimal | None = Field(default=None, gt=0, le=1_000_000_000)
    activation_price: Decimal | None = Field(default=None, gt=0, le=1_000_000)
    # Relative stages normally follow the previous actual fill.  A sell-first
    # T cycle has no previous fill, so its first stage may explicitly anchor to
    # the first completed observation in the replay instead of inventing one.
    reference_mode: Literal["previous_fill", "first_observation"] = "previous_fill"

    @model_validator(mode="before")
    @classmethod
    def percentage_alias(cls, value: object) -> object:
        # Legacy indicator DSL uses threshold_pct. This alias has a precise
        # percentage unit; accept it without ever treating it as a CNY gap.
        if not isinstance(value, dict) or "threshold_pct" not in value:
            return value
        result = dict(value)
        threshold = result.pop("threshold_pct")
        if result.get("kind") not in {
            "take_profit",
            "stop_loss",
            "rebound",
            "pullback",
            "relative_price",
        }:
            raise ValueError("该条件不能使用百分比幅度")
        if result.get("gap_unit", "percent") != "percent":
            raise ValueError("百分比字段与元价差单位冲突")
        if result.get("gap") is not None and D(str(result["gap"])) != D(str(threshold)):
            raise ValueError("重复的百分比幅度不一致")
        result.update(gap=threshold, gap_unit="percent")
        return result

    @model_validator(mode="after")
    def usable(self) -> ConditionRule:
        if self.price_comparison == "strict" and self.kind != "price":
            raise ValueError("严格价格比较仅适用于到价条件")
        if self.sizing_mode == "all_position" and self.side != "sell":
            raise ValueError("全部持仓数量模式仅用于卖出")
        if self.kind == "price" and self.target_price is None:
            raise ValueError("到价条件须填写触发价")
        if self.kind not in {"price", "holding_period"} and self.gap is None:
            raise ValueError("反弹或回落条件须填写幅度及单位")
        if (self.kind == "rebound" and self.side != "buy") or (
            self.kind == "pullback" and self.side != "sell"
        ):
            raise ValueError("反弹条件用于买入，回落条件用于卖出")
        if self.kind in {"take_profit", "stop_loss", "holding_period"} and self.side != "sell":
            raise ValueError("止盈、止损和持有期限条件用于卖出")
        if self.kind == "holding_period" and self.sessions is None:
            raise ValueError("期限卖出须填写持有交易日数")
        if self.sizing_mode == "amount" and self.amount_cny is None:
            raise ValueError("按金额委托须填写委托金额（不含费用）")
        if self.sizing_mode == "amount" and self.kind == "holding_period":
            raise ValueError("到期开盘卖出须按股数委托，不能事后用开盘价反推数量")
        if self.activation_price is not None and self.kind not in {"rebound", "pullback"}:
            raise ValueError("启动观察价仅用于反弹买入或回落卖出")
        if self.reference_mode != "previous_fill" and self.kind != "relative_price":
            raise ValueError("首次观察价基准仅用于相对成交价条件")
        if (
            self.gap_unit == "percent"
            and self.gap is not None
            and self.gap >= 100
            and (
                self.kind in {"pullback", "stop_loss"}
                or (self.kind == "relative_price" and self.direction == "down")
            )
        ):
            raise ValueError("百分比幅度须小于100%，1表示1%")
        if any(
            v is not None and v % D("0.01")
            for v in (
                self.target_price,
                self.limit_price,
                self.activation_price,
                self.gap if self.gap_unit == "cny" else None,
            )
        ):
            raise ValueError("价格和元价差请精确到分")
        return self


class ConditionParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    observation: Literal["daily_close", "minute_bar"] = "daily_close"
    # Once armed, each rule remains pending until filled or until the range ends.
    # Multiple rules execute in submitted order, never simultaneously.
    rules: list[ConditionRule] = Field(min_length=1, max_length=8)
    repeat_cycles: int = Field(default=1, ge=1, le=1000, strict=True)
    initial_cash_cny: Decimal = Field(default=D(DEFAULT_INITIAL_CASH_CNY), gt=0, le=1_000_000_000)
    initial_shares: int = Field(default=0, ge=0, le=100_000_000, strict=True)
    opening_shares: int = Field(default=0, ge=0, le=100_000_000, strict=True)
    initial_capital_scope: Literal["total_equity", "cash_plus_opening_holdings"] = "total_equity"
    min_shares: int = Field(default=0, ge=0, le=100_000_000, strict=True)
    max_shares: int = Field(default=10000, ge=1, le=100_000_000, strict=True)
    max_position_cny: Decimal | None = Field(default=None, gt=0, le=1_000_000_000)
    commission_rate: Decimal = Field(default=D("0.00025"), ge=0, le=D("0.05"))
    minimum_commission_cny: Decimal = Field(default=D(5), ge=0, le=1000)
    stamp_tax_rate: Decimal = Field(default=D("0.0005"), ge=0, le=D("0.05"))
    transfer_fee_rate: Decimal = Field(default=D("0.00001"), ge=0, le=D("0.05"))
    slippage_bps: Decimal = Field(default=D(5), ge=0, le=1000)
    slippage_cny: Decimal = Field(default=D(0), ge=0, exclude_if=lambda value: not value)

    @model_validator(mode="after")
    def holdings(self) -> ConditionParameters:
        if (
            max(self.initial_shares, self.opening_shares) > self.max_shares
            or self.min_shares > self.max_shares
        ):
            raise ValueError("初始建仓和最小底仓均不得超过最大持仓")
        if self.initial_shares and self.opening_shares:
            raise ValueError("首日新建仓与期初已有持仓不能同时设置")
        groups: set[str] = set()
        previous: str | None = None
        for rule in self.rules:
            if rule.group is not None and rule.group != previous:
                if rule.group in groups:
                    raise ValueError("互斥组中的条件须相邻，不能跨阶段复用组名")
                groups.add(rule.group)
            previous = rule.group
        return self


class GridPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["grid"] = "grid"
    parameters: GridParameters


class ConditionalPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["conditional"] = "conditional"
    parameters: ConditionParameters


class ScheduledParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    exit_rules: tuple[ConditionRule, ...] = Field(default=(), max_length=8)
    buy_on_start: bool = False
    frequency: Literal["once", "weekly", "monthly"] = "weekly"
    day: int = Field(default=1, ge=1, le=31, strict=True)
    at: Literal["open", "close"] = "open"
    side: Literal["buy", "sell"] = "buy"
    sizing_mode: Literal["amount", "shares"] = "amount"
    quantity: int = Field(default=100, ge=1, le=1_000_000, strict=True)
    budget_cny: Decimal = Field(default=D(1000), gt=0, le=1_000_000_000)
    limit_price: Decimal | None = Field(default=None, gt=0, le=1_000_000)
    initial_cash_cny: Decimal = Field(default=D(DEFAULT_INITIAL_CASH_CNY), gt=0, le=1_000_000_000)
    initial_shares: int = Field(default=0, ge=0, le=100_000_000, strict=True)
    min_shares: int = Field(default=0, ge=0, le=100_000_000, strict=True)
    max_shares: int = Field(default=10000, ge=1, le=100_000_000, strict=True)
    max_position_cny: Decimal | None = Field(default=None, gt=0, le=1_000_000_000)
    commission_rate: Decimal = Field(default=D("0.00025"), ge=0, le=D("0.05"))
    minimum_commission_cny: Decimal = Field(default=D(5), ge=0, le=1000)
    slippage_bps: Decimal = Field(default=D(5), ge=0, le=1000)
    slippage_cny: Decimal = Field(default=D(0), ge=0)

    @model_validator(mode="after")
    def consistent(self):
        if self.exit_rules and (self.side != "buy" or any(rule.side != "sell" for rule in self.exit_rules)):
            raise ValueError("定投组合条件只允许周期买入配合卖出规则")
        if self.buy_on_start and (self.side != "buy" or self.initial_shares):
            raise ValueError("首日定投只能用于买入，不能同时重复设置初始建仓股数")
        if self.frequency == "weekly" and self.day > 7:
            raise ValueError("周定投日期须为1至7，1表示周一")
        if self.side == "sell" and self.sizing_mode == "amount":
            raise ValueError("定时卖出须按股数计划，不能用未来成交价反推数量")
        if self.initial_shares > self.max_shares or self.min_shares > self.max_shares:
            raise ValueError("初始建仓和最小底仓均不得超过最大持仓")
        if self.limit_price is not None and self.limit_price % D(".01"):
            raise ValueError("委托限价请精确到分")
        return self


class ScheduledPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["scheduled"] = "scheduled"
    parameters: ScheduledParameters


type PricePlan = Annotated[GridPlan | ConditionalPlan | ScheduledPlan, Field(discriminator="kind")]


def with_new_strategy_defaults(plan: PricePlan) -> PricePlan:
    """Apply defaults promised only by the new-strategy language flow.

    Persisted grids deliberately keep ``observation=None`` for backward
    compatibility.  A newly generated phase-one grid, however, is minute
    observed unless the user explicitly asks for daily-close observation.
    Keeping that distinction here prevents a model omission from silently
    turning a new grid into a server-selected execution interval.
    """
    # Keep old persisted/default-only payloads readable at their original 1000
    # yuan. Only new language plans with no specified budget get the suggestion.
    if (isinstance(plan, ScheduledPlan) and plan.parameters.sizing_mode == "amount"
            and "budget_cny" not in plan.parameters.model_fields_set):
        return plan.model_copy(update={"parameters": plan.parameters.model_copy(update={
            "budget_cny": D(DEFAULT_SCHEDULED_BUDGET_CNY),
        })})
    if isinstance(plan, GridPlan) and plan.parameters.observation is None:
        return plan.model_copy(update={
            "parameters": plan.parameters.model_copy(update={"observation": "minute_bar"}),
        })
    if isinstance(plan, ConditionalPlan):
        rules = list(plan.parameters.rules)
        changed = False
        for index in range(len(rules) - 1):
            left, right = rules[index:index + 2]
            if (
                {left.kind, right.kind} == {"take_profit", "stop_loss"}
                and left.side == right.side == "sell"
                and left.group is None
                and right.group is None
            ):
                group = f"position_exit_{index + 1}"
                rules[index] = left.model_copy(update={"group": group})
                rules[index + 1] = right.model_copy(update={"group": group})
                changed = True
        if changed:
            return plan.model_copy(update={
                "parameters": plan.parameters.model_copy(update={"rules": tuple(rules)}),
            })
    return plan
