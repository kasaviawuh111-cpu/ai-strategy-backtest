"""Bounded candidate glue for agent-style natural-language interpreters.

The orchestration pattern is adapted from HKUDS/Vibe-Trading's
``strategy-generate`` skill at commit
``e90b6c6cd9fea23067a85667e7fbf74f9d73ea48`` (MIT).  Unlike that upstream
workflow, this adapter never accepts or executes generated Python.  An
untrusted model may only return the strict JSON shape below; the existing
StrategyCompiler and Catalog remain the authority for executable semantics.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from math import isfinite
from typing import Annotated, Literal, Protocol, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from ashare_lab.adapters.language.backtest_period import parse_backtest_period
from ashare_lab.domain.catalog import CatalogSnapshot, CoverageCatalogSnapshot
from ashare_lab.domain.events.catalog import (
    DOCUMENT_TEXT_EVENT_CODES,
    EXECUTABLE_EVENT_DEFINITIONS,
)
from ashare_lab.domain.strategy.canonical import canonical_hash
from ashare_lab.domain.strategy.models import JsonScalar
from ashare_lab.ports.candidate_generation import (
    BoundedCandidateBoundary,
    CandidateAst,
    CandidateGenerator,
    CandidateGroundingEvidence,
    CandidateProvenance,
    CompileInput,
    ConditionJoin,
    DocumentTextIntent,
    EventIntent,
    HoldingPeriodIntent,
    IndicatorIntent,
    PositionReturnIntent,
    TrailingDrawdownIntent,
)

_UPSTREAM_COMMIT = "e90b6c6cd9fea23067a85667e7fbf74f9d73ea48"
_DEFAULT_FALLBACK_CODES = frozenset(
    {
        "no_supported_signal_recognized",
        "strategy_rule_incomplete",
        "entry_rule_not_recognized",
        "exit_rule_not_recognized",
        "ambiguous_macd_trigger",
        "ambiguous_boolean_expression",
    }
)
_INCOMPLETE_RULE_CODES = frozenset(
    {
        "strategy_rule_incomplete",
        "entry_rule_not_recognized",
        "exit_rule_not_recognized",
    }
)
_GENERIC_ACTION_PLACEHOLDER_RE = re.compile(r"(?:随便|任意|随机|不知道|不确定|你看着)")
_DEFAULTED_PARAMETER_PATH_RE = re.compile(
    r"^/(?P<side>entry|exit)/(?P<index>\d+)/params/(?P<name>[A-Za-z0-9_]+)$"
)
_RSI_TRANSITION_THRESHOLD_RE = re.compile(
    r"(?:重新)?(?:回到|到)\s*[+-]?(?:\d+(?:\.\d+)?|\.\d+)\s*(?:上方|下方)"
)
_DEFAULT_MIN_CONFIDENCE = 0.75
_LOGGER = logging.getLogger(__name__)


class CandidateTransportError(RuntimeError):
    """Declared, sanitized transport failure that may degrade to unavailable."""


# These are display/search hints for the bounded interpreter, not executable
# parsing rules.  Every event always gets its canonical Chinese Catalog name;
# the short forms below cover only widely used report names.  The server still
# accepts the event only by its exact allowlisted code.
_EVENT_ALIAS_OVERRIDES: Mapping[str, tuple[str, ...]] = {
    "event.financial_results.annual_report": ("年报",),
    "event.financial_results.semiannual_report": ("半年报", "中报"),
    "event.financial_results.quarterly_report": ("季报",),
    "event.financial_results.earnings_forecast_published": ("业绩预告",),
    "event.contracts_orders.major_contract_won": ("最终中标", "重大项目中标"),
    "event.macro_policy_industry.license_approval": ("许可证获批", "业务许可获批"),
}
_INDICATOR_ALIAS_OVERRIDES: Mapping[str, tuple[str, ...]] = {
    "technical.ma": ("均线", "MA"),
    "technical.ema": ("EMA", "指数均线"),
    "technical.ma_cross": ("双均线", "均线交叉"),
    "technical.bbi": ("BBI",),
    "technical.macd": ("MACD", "指数平滑异同移动平均线"),
    "technical.rsi": ("RSI",),
    "technical.cci": ("CCI",),
    "technical.kdj": ("KDJ",),
    "technical.ema_bias": ("EMA乖离", "EMA 乖离"),
    "technical.bollinger": ("布林带", "BOLL"),
    "technical.trend_regime": ("阶段趋势",),
    "technical.obv": ("OBV", "能量潮"),
    "price.return_pct": ("区间涨跌幅",),
    "price.amplitude": ("振幅",),
    "price.rolling_high": ("滚动新高", "新高"),
    "price.consecutive_up": ("连续上涨", "连涨"),
    "market.volume": ("成交量",),
    "market.amount": ("成交额",),
    "amount.average": ("平均成交额", "均成交额"),
    "volume.relative": ("相对成交量", "RVOL"),
    "volume.price_confirmation": ("量价同向", "量价确认"),
    "volume.price_divergence": ("量价背离", "顶背离", "底背离"),
}
_TRIGGER_ALIAS_OVERRIDES: Mapping[str, tuple[str, ...]] = {
    "above": ("高于", "大于", "上方"),
    "below": ("低于", "小于", "下方"),
    "crosses_above": ("上穿", "突破"),
    "crosses_below": ("下穿", "跌破"),
    "crosses_above_zero": ("上穿零轴", "上穿0轴"),
    "crosses_below_zero": ("下穿零轴", "下穿0轴"),
    "golden_cross": ("金叉", "上穿"),
    "death_cross": ("死叉", "下穿"),
    "price_crosses_above": ("股价上穿", "价格上穿", "突破", "站上"),
    "price_crosses_below": ("股价下穿", "价格下穿", "跌破"),
    "price_above": ("股价高于", "价格高于"),
    "price_below": ("股价低于", "价格低于"),
    "price_crosses_above_upper": ("上穿上轨",),
    "price_crosses_below_upper": ("下穿上轨",),
    "price_crosses_above_middle": ("上穿中轨",),
    "price_crosses_below_middle": ("下穿中轨",),
    "price_crosses_above_lower": ("上穿下轨",),
    "price_crosses_below_lower": ("下穿下轨",),
    "price_above_upper": ("高于上轨",),
    "price_below_lower": ("低于下轨",),
    "gte_multiple": ("达到倍数", "放量"),
    "lte_multiple": ("低于倍数", "缩量"),
    "consecutive_gte_multiple": ("持续放量",),
    "new_high": ("新高",),
    "at_least": ("至少", "不少于"),
    "surge_up": ("放量上涨", "放量大涨"),
    "surge_down": ("放量下跌", "放量大跌"),
    "rising": ("上升", "走高", "转强"),
    "falling": ("下降", "走低", "转弱"),
    "bullish": ("底背离", "看涨背离"),
    "bearish": ("顶背离", "看跌背离"),
    "uptrend": ("上涨趋势", "上升趋势", "多头趋势"),
    "downtrend": ("下跌趋势", "下降趋势", "空头趋势"),
    "range": ("震荡", "横盘", "盘整"),
    "fast_above_slow": ("快线高于慢线",),
    "fast_below_slow": ("快线低于慢线",),
    "j_above": ("J值高于",),
    "j_below": ("J值低于",),
    "k_above": ("K值高于",),
    "k_below": ("K值低于",),
    "k_crosses_above_d": ("K线上穿D线", "KDJ金叉"),
    "k_crosses_below_d": ("K线下穿D线", "KDJ死叉"),
    "plus_above_minus": ("正DI高于负DI", "+DI高于-DI"),
    "plus_below_minus": ("正DI低于负DI", "+DI低于-DI"),
    "plus_crosses_above_minus": ("正DI上穿负DI", "+DI上穿-DI"),
    "plus_crosses_below_minus": ("正DI下穿负DI", "+DI下穿-DI"),
}
_ENTRY_ACTION_WORDS = ("买入", "买进", "建仓", "开仓", "上车", "就买", "才买")
_EXIT_ACTION_WORDS = (
    "MACD转弱卖",
    "转弱卖",
    "交易日后卖",
    "卖出",
    "卖掉",
    "退出",
    "平仓",
    "清仓",
    "止盈",
    "止损",
    "离场",
    "就走",
    "就卖",
    "收手",
)
_CHINESE_SMALL_NUMBERS = {
    1: "一",
    2: "二",
    3: "三",
    4: "四",
    5: "五",
    6: "六",
    7: "七",
    8: "八",
    9: "九",
    10: "十",
}
_PARAMETER_ALIAS_OVERRIDES: Mapping[str, tuple[str, ...]] = {
    "fast": ("快线", "快速周期"),
    "slow": ("慢线", "慢速周期"),
    "signal": ("信号线", "信号周期"),
    "fast_period": ("快线周期", "短周期"),
    "slow_period": ("慢线周期", "长周期"),
    "short_period": ("短周期",),
    "long_period": ("长周期",),
    "period": ("周期",),
}
_EVENT_ATTRIBUTE_ALIAS_OVERRIDES: Mapping[str, tuple[str, ...]] = {
    "forecast_type": ("预告类型",),
    "direction": ("业绩方向", "方向"),
    "source": ("来源",),
    "report_type": ("报告类型",),
    "stat_date": ("报告期", "统计日期"),
    "award_stage": ("中标阶段",),
    "contract_type": ("合同类型",),
    "counterparty": ("合同对方", "交易对方"),
    "is_consortium": ("联合体", "是否联合体"),
    "issuer_role": ("公司角色", "发行人角色"),
    "materiality_basis": ("重大性依据",),
    "materiality_status": ("重大性状态",),
    "project_name": ("项目名称",),
    "source_kind": ("来源类型",),
    "approval_status": ("获批状态", "审批状态"),
    "jurisdiction": ("司法辖区", "地区"),
    "license_type": ("许可类型",),
    "product_or_scope": ("产品或范围", "许可范围"),
    "regulator": ("监管机构", "审批机构"),
}
_ALL_JOIN_WORDS = ("且", "并且", "同时", "以及", "和", "与", "、", "AND", "&&")
_ANY_JOIN_WORDS = ("或", "或者", "任一", "OR", "||")


class _StrictCandidateModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


class CandidateParameterCapability(_StrictCandidateModel):
    name: str
    value_type: Literal["integer", "number", "string", "boolean"]
    required: bool
    default: JsonScalar | None = None
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[JsonScalar, ...] = ()


class CandidateParameterRelationCapability(_StrictCandidateModel):
    left: str
    op: Literal["lt", "lte", "gt", "gte"]
    right: str


class CandidateTriggerCapability(_StrictCandidateModel):
    id: str
    aliases_zh: tuple[str, ...] = Field(min_length=1)
    value_requirement: Literal["required", "forbidden"]
    minimum: float | None = None
    maximum: float | None = None
    exclusive_minimum: bool = False
    exclusive_maximum: bool = False


class IndicatorCandidateCapability(_StrictCandidateModel):
    indicator_id: str
    definition_version: str
    aliases_zh: tuple[str, ...] = Field(min_length=1)
    triggers: tuple[CandidateTriggerCapability, ...] = Field(min_length=1)
    parameters: tuple[CandidateParameterCapability, ...] = ()
    parameter_relations: tuple[CandidateParameterRelationCapability, ...] = ()
    timeframe: Literal["1d"] = "1d"
    evaluation_mode: Literal["bar_close_confirmed"] = "bar_close_confirmed"


class EventCandidateCapability(_StrictCandidateModel):
    event_code: str
    definition_version: str
    aliases_zh: tuple[str, ...] = Field(min_length=1)
    allowed_attributes: tuple[str, ...] = ()
    document_text_allowed: bool = False
    trigger: Literal["published"] = "published"


class CandidateCapabilityMatrix(_StrictCandidateModel):
    """The exact Catalog slice an untrusted interpreter may name.

    This is an expression allowlist, not proof that a requested stock/date
    range is present in a pinned snapshot.  Submission performs that separate
    capability/coverage check.
    """

    schema_version: Literal["candidate-capabilities.v1"] = "candidate-capabilities.v1"
    indicator_catalog_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    event_catalog_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    indicators: tuple[IndicatorCandidateCapability, ...] = Field(min_length=1)
    events: tuple[EventCandidateCapability, ...] = Field(min_length=1)
    condition_joins: tuple[Literal["all", "any"], ...] = ("all", "any")
    exit_kinds: tuple[
        Literal[
            "indicator",
            "event",
            "holding_period",
            "position_return",
            "trailing_drawdown",
        ],
        ...,
    ] = (
        "indicator",
        "event",
        "holding_period",
        "position_return",
        "trailing_drawdown",
    )
    holding_period_unit: Literal["subsequent_a_share_trading_sessions"] = (
        "subsequent_a_share_trading_sessions"
    )
    holding_period_min: int = 1
    holding_period_max: int = 10_000

    @model_validator(mode="after")
    def capabilities_are_unique(self) -> CandidateCapabilityMatrix:
        indicator_ids = [item.indicator_id for item in self.indicators]
        event_codes = [item.event_code for item in self.events]
        if len(indicator_ids) != len(set(indicator_ids)):
            raise ValueError("candidate matrix contains duplicate indicator ids")
        if len(event_codes) != len(set(event_codes)):
            raise ValueError("candidate matrix contains duplicate event codes")
        return self

    def resolve_indicator(self, indicator_id: str) -> IndicatorCandidateCapability | None:
        return next(
            (item for item in self.indicators if item.indicator_id == indicator_id),
            None,
        )

    def resolve_event(self, event_code: str) -> EventCandidateCapability | None:
        return next((item for item in self.events if item.event_code == event_code), None)

    @property
    def content_hash(self) -> str:
        return canonical_hash(self)


def build_candidate_capability_matrix(
    catalog: CatalogSnapshot,
    coverage_catalog: CoverageCatalogSnapshot,
) -> CandidateCapabilityMatrix:
    """Build one fail-closed interpreter allowlist from the active Catalogs."""

    stable_indicators = {item.id: item for item in catalog.indicators if item.status == "stable"}
    covered_indicators = {
        item.id: item for item in coverage_catalog.metrics if item.status == "stable"
    }
    if set(stable_indicators) != set(covered_indicators):
        raise ValueError("stable executable and coverage indicator catalogs do not match")

    indicator_capabilities: list[IndicatorCandidateCapability] = []
    for indicator_id in sorted(stable_indicators):
        definition = stable_indicators[indicator_id]
        coverage = covered_indicators[indicator_id]
        if set(coverage.parameters) != {item.name for item in definition.parameters}:
            raise ValueError(f"coverage parameters drifted for {indicator_id}")
        if set(coverage.triggers) != {item.id for item in definition.triggers}:
            raise ValueError(f"coverage triggers drifted for {indicator_id}")
        indicator_capabilities.append(
            IndicatorCandidateCapability(
                indicator_id=definition.id,
                definition_version=definition.version,
                aliases_zh=tuple(
                    dict.fromkeys(
                        (coverage.name_zh, *_INDICATOR_ALIAS_OVERRIDES.get(definition.id, ()))
                    )
                ),
                triggers=tuple(
                    CandidateTriggerCapability(
                        id=item.id,
                        aliases_zh=tuple(
                            dict.fromkeys((item.id, *_TRIGGER_ALIAS_OVERRIDES.get(item.id, ())))
                        ),
                        value_requirement=item.value_requirement,
                        minimum=item.minimum,
                        maximum=item.maximum,
                        exclusive_minimum=item.exclusive_minimum,
                        exclusive_maximum=item.exclusive_maximum,
                    )
                    for item in definition.triggers
                ),
                parameters=tuple(
                    CandidateParameterCapability(
                        name=item.name,
                        value_type=item.value_type,
                        required=item.required,
                        default=item.default,
                        minimum=item.minimum,
                        maximum=item.maximum,
                        choices=item.choices,
                    )
                    for item in definition.parameters
                ),
                parameter_relations=tuple(
                    CandidateParameterRelationCapability(
                        left=item.left,
                        op=item.op,
                        right=item.right,
                    )
                    for item in definition.parameter_relations
                ),
            )
        )

    covered_events = {item.id: item for item in coverage_catalog.events if item.status == "stable"}
    if set(EXECUTABLE_EVENT_DEFINITIONS) != set(covered_events):
        raise ValueError("stable executable and coverage event catalogs do not match")

    event_capabilities: list[EventCandidateCapability] = []
    for event_code in sorted(EXECUTABLE_EVENT_DEFINITIONS):
        definition = EXECUTABLE_EVENT_DEFINITIONS[event_code]
        coverage = covered_events[event_code]
        aliases = tuple(
            dict.fromkeys((coverage.name_zh, *_EVENT_ALIAS_OVERRIDES.get(event_code, ())))
        )
        event_capabilities.append(
            EventCandidateCapability(
                event_code=event_code,
                definition_version=definition.definition_version,
                aliases_zh=aliases,
                allowed_attributes=definition.allowed_attributes,
                document_text_allowed=event_code in DOCUMENT_TEXT_EVENT_CODES,
            )
        )

    return CandidateCapabilityMatrix(
        indicator_catalog_hash=catalog.content_hash,
        event_catalog_hash=coverage_catalog.content_hash,
        indicators=tuple(indicator_capabilities),
        events=tuple(event_capabilities),
    )


class IndicatorCandidate(_StrictCandidateModel):
    kind: Literal["indicator"] = "indicator"
    indicator_id: str = Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
    definition_version: str = Field(
        default="1.0.0",
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
    )
    trigger: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    params: dict[str, JsonScalar] = Field(default_factory=dict, max_length=16)
    value: float | None = None


class DocumentTextCandidate(_StrictCandidateModel):
    term: str = Field(min_length=1, max_length=64)
    match_mode: Literal["ascii_token", "literal"]
    comparator: Literal["gt", "gte"]
    value: int = Field(ge=0, le=1_000_000)
    case_sensitive: bool = False

    @model_validator(mode="after")
    def ascii_mode_requires_ascii_term(self) -> DocumentTextCandidate:
        if self.match_mode == "ascii_token" and (
            not self.term.isascii() or not any(character.isalnum() for character in self.term)
        ):
            raise ValueError("ascii_token match mode requires an ASCII alphanumeric term")
        return self


class EventCandidate(_StrictCandidateModel):
    kind: Literal["event"] = "event"
    event_code: str = Field(pattern=r"^event\.[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
    definition_version: str = Field(
        default="1.0.0",
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
    )
    trigger: Literal["published"] = "published"
    attributes: dict[str, JsonScalar] = Field(default_factory=dict, max_length=8)
    document_text: DocumentTextCandidate | None = None


class HoldingPeriodCandidate(_StrictCandidateModel):
    kind: Literal["holding_period"] = "holding_period"
    sessions: int = Field(ge=1, le=10_000)


class PositionReturnCandidate(_StrictCandidateModel):
    kind: Literal["position_return"] = "position_return"
    trigger: Literal["take_profit", "stop_loss"]
    threshold_pct: float = Field(gt=0, le=10_000)

    @model_validator(mode="after")
    def stop_loss_is_bounded(self) -> PositionReturnCandidate:
        if not isfinite(self.threshold_pct):
            raise ValueError("position-return threshold must be finite")
        if self.trigger == "stop_loss" and self.threshold_pct > 100:
            raise ValueError("stop-loss threshold cannot exceed 100 percent")
        return self


class TrailingDrawdownCandidate(_StrictCandidateModel):
    kind: Literal["trailing_drawdown"] = "trailing_drawdown"
    threshold_pct: float = Field(gt=0, le=100)

    @field_validator("threshold_pct")
    @classmethod
    def threshold_is_finite(cls, value: float) -> float:
        if not isfinite(value):
            raise ValueError("trailing-drawdown threshold must be finite")
        return value


class CandidateSourceSpan(_StrictCandidateModel):
    start: int = Field(ge=0, le=2_000)
    end: int = Field(gt=0, le=2_000)
    text: str = Field(min_length=1, max_length=2_000)

    @model_validator(mode="after")
    def span_is_ordered(self) -> CandidateSourceSpan:
        if self.end <= self.start:
            raise ValueError("candidate source span end must be after start")
        return self


type _SignalCandidate = Annotated[
    IndicatorCandidate | EventCandidate,
    Field(discriminator="kind"),
]
type _ExitCandidate = Annotated[
    IndicatorCandidate
    | EventCandidate
    | HoldingPeriodCandidate
    | PositionReturnCandidate
    | TrailingDrawdownCandidate,
    Field(discriminator="kind"),
]


class BoundedCandidate(_StrictCandidateModel):
    instrument_symbol: str | None = Field(
        default=None,
        pattern=r"^[0-9]{6}\.(SH|SZ|BJ)$",
    )
    entry: tuple[_SignalCandidate, ...] = Field(min_length=1, max_length=8)
    exit: tuple[_ExitCandidate, ...] = Field(min_length=1, max_length=8)
    entry_spans: tuple[CandidateSourceSpan, ...] = Field(min_length=1, max_length=8)
    exit_spans: tuple[CandidateSourceSpan, ...] = Field(min_length=1, max_length=8)
    instrument_span: CandidateSourceSpan | None = None
    backtest_span: CandidateSourceSpan | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    entry_join: ConditionJoin = "all"
    exit_join: ConditionJoin = "any"
    defaulted_fields: tuple[str, ...] = Field(default=(), max_length=32)
    backtest_start: date | None = None
    backtest_end: date | None = None
    backtest_lookback_years: int | None = Field(default=None, ge=1, le=50)

    @model_validator(mode="after")
    def period_is_unambiguous(self) -> BoundedCandidate:
        if self.backtest_lookback_years is not None and (
            self.backtest_start is not None or self.backtest_end is not None
        ):
            raise ValueError("lookback years cannot be combined with explicit dates")
        if len(self.entry_spans) != len(self.entry):
            raise ValueError("entry source spans must match entry leaves one-for-one")
        if len(self.exit_spans) != len(self.exit):
            raise ValueError("exit source spans must match exit leaves one-for-one")
        if len(self.defaulted_fields) != len(set(self.defaulted_fields)):
            raise ValueError("defaulted field paths must be unique")
        return self


class BoundedCandidateBatch(_StrictCandidateModel):
    candidates: tuple[BoundedCandidate, ...] = Field(min_length=1, max_length=3)


@dataclass(frozen=True, slots=True)
class CandidateTransportRequest:
    utterance: str
    instrument_context: str | None
    as_of_date: date
    max_candidates: int
    response_schema: Mapping[str, object]
    capability_matrix: Mapping[str, object]
    capability_projection_version: str
    capability_projection_hash: str
    system_contract: str
    upstream_pattern_commit: str = _UPSTREAM_COMMIT


type CandidateTransportResponse = str | bytes | Mapping[str, object]


class CandidateJsonTransport(Protocol):
    """Transport supplied by an LLM gateway or an offline test double."""

    async def generate_json(
        self,
        request: CandidateTransportRequest,
    ) -> CandidateTransportResponse: ...


@dataclass(frozen=True, slots=True)
class CandidateProviderIdentityView:
    """Non-secret transport identity shared by the transport and provenance layer."""

    provider: str
    model: str
    prompt_version: str
    schema_version: str


class IdentifiedCandidateJsonTransport(CandidateJsonTransport, Protocol):
    @property
    def identity(self) -> CandidateProviderIdentityView: ...


class VibeBoundedCandidateGenerator:
    """Translate untrusted JSON into the project's constrained CandidateAst."""

    def __init__(
        self,
        transport: CandidateJsonTransport,
        *,
        capability_matrix: CandidateCapabilityMatrix | None = None,
        provider_identity: CandidateProviderIdentityView | None = None,
        min_confidence: float = _DEFAULT_MIN_CONFIDENCE,
    ) -> None:
        if not 0.0 <= min_confidence <= 1.0:
            raise ValueError("min_confidence must be between zero and one")
        self._transport = transport
        self._capability_matrix = capability_matrix
        self._provider_identity = provider_identity
        self._min_confidence = min_confidence

    @property
    def boundary(self) -> BoundedCandidateBoundary:
        """Identify the schema-constrained candidate boundary to application code."""

        return "schema_bounded_candidate.v1"

    @property
    def capability_projection_version(self) -> str | None:
        if self._capability_matrix is None:
            return None
        return self._capability_matrix.schema_version

    @property
    def capability_projection_hash(self) -> str | None:
        if self._capability_matrix is None:
            return None
        return self._capability_matrix.content_hash

    async def generate(self, request: CompileInput) -> tuple[CandidateAst, ...]:
        if self._capability_matrix is None:
            return (
                _unsupported(
                    request.instrument_context,
                    "candidate_capability_matrix_unavailable",
                ),
            )
        matrix = self._capability_matrix
        transport_request = CandidateTransportRequest(
            utterance=request.utterance,
            instrument_context=request.instrument_context,
            as_of_date=request.as_of_date,
            max_candidates=3,
            response_schema=_bounded_response_schema(matrix),
            capability_matrix=cast(
                dict[str, object],
                matrix.model_dump(mode="json"),
            ),
            capability_projection_version=matrix.schema_version,
            capability_projection_hash=matrix.content_hash,
            system_contract=(
                "只把用户原话翻译成给定 JSON Schema。只能处理单只 A 股、只做多；"
                "不得生成 Python、SQL、Pine Script 或任何可执行代码；不得发明指标、"
                "事件、参数或成交规则；指标、事件、触发器和参数必须逐项来自 capability_matrix；"
                "只支持日线收盘确认、AND/OR 条件组合和按 A 股交易日计数的固定持有期；"
                "止盈、止损和跟踪回撤只有在原话明确给出类型与百分比时才能使用；"
                "entry_spans/exit_spans 必须逐叶给出用户原话的精确 start/end/text，"
                "每段只证明对应能力、触发器、动作和显式数值；股票与回测区间也必须给精确 span；"
                "未在原话出现的指标参数只有等于 Catalog default 时才可写入，"
                "并须在 defaulted_fields "
                "使用 /entry/{i}/params/{name} 或 /exit/{i}/params/{name} 标记；"
                "缺关键买入或卖出条件时不要补默认策略。"
            ),
        )
        for attempt in range(2):
            try:
                payload = await self._transport.generate_json(transport_request)
                candidates = _translate_transport_payload(
                    payload,
                    request=request,
                    matrix=matrix,
                    provider_identity=self._provider_identity,
                    min_confidence=self._min_confidence,
                )
            except _CandidateRuleIncompleteError as exc:
                return (_unsupported(request.instrument_context, exc.diagnostic_code),)
            except (TypeError, ValueError, ValidationError):
                if attempt == 0:
                    continue
                return (
                    _unsupported(request.instrument_context, "candidate_provider_invalid_output"),
                )
            except CandidateTransportError:
                return (_unsupported(request.instrument_context, "candidate_provider_unavailable"),)
            except Exception as exc:
                # Do not serialize the exception message: an unexpected provider
                # implementation bug may contain request text or credentials.
                _LOGGER.error(
                    "unexpected candidate transport exception type=%s",
                    type(exc).__name__,
                )
                raise
            if (
                attempt == 0
                and candidates
                and all(
                    item.unsupported_code == "candidate_provider_invalid_output"
                    for item in candidates
                )
            ):
                continue
            return candidates
        return (_unsupported(request.instrument_context, "candidate_provider_invalid_output"),)


class HybridCandidateGenerator:
    """Use deterministic rules first and a bounded provider only for allowlisted misses."""

    def __init__(
        self,
        *,
        deterministic: CandidateGenerator,
        bounded_fallback: CandidateGenerator,
        fallback_codes: frozenset[str] = _DEFAULT_FALLBACK_CODES,
    ) -> None:
        self._deterministic = deterministic
        self._bounded_fallback = bounded_fallback
        self._fallback_codes = fallback_codes

    async def generate(self, request: CompileInput) -> tuple[CandidateAst, ...]:
        primary = await self._deterministic.generate(request)
        if not primary:
            return await self._bounded_fallback.generate(request)
        first = primary[0]
        if not _should_use_bounded_fallback(
            first,
            utterance=request.utterance,
            fallback_codes=self._fallback_codes,
        ):
            return primary
        return await self._bounded_fallback.generate(request)


def _should_use_bounded_fallback(
    candidate: CandidateAst,
    *,
    utterance: str,
    fallback_codes: frozenset[str],
) -> bool:
    """Route only a genuine parser miss or a source-complete partial parse.

    Explicitly unsupported semantics never enter this path.  For an incomplete
    local parse, the user must still have supplied the action on the missing
    side; the bounded provider may translate that action, but may not invent a
    default entry or exit rule.  Generic placeholders remain a clarification.
    """

    code = candidate.unsupported_code
    if code not in fallback_codes:
        return False
    text = re.sub(r"\s+", "", utterance).casefold()
    if _GENERIC_ACTION_PLACEHOLDER_RE.search(text) is not None:
        return False
    if code == "no_supported_signal_recognized":
        return True
    has_entry_action = any(word.casefold() in text for word in _ENTRY_ACTION_WORDS)
    has_exit_action = any(word.casefold() in text for word in _EXIT_ACTION_WORDS)
    if not (has_entry_action and has_exit_action):
        return False
    if code == "ambiguous_macd_trigger":
        return "macd" in text and "上穿" in text and "下穿" in text
    if code == "ambiguous_boolean_expression":
        return (
            "、" in text
            and "新高" in text
            and "成交量" in text
            and "macd" in text
            and "转弱" in text
        )
    return code in _INCOMPLETE_RULE_CODES


class _CandidateRuleIncompleteError(ValueError):
    def __init__(self, diagnostic_code: str) -> None:
        self.diagnostic_code = diagnostic_code
        super().__init__(diagnostic_code)


class _CandidateSemanticRejection(ValueError):
    def __init__(self, diagnostic_code: str) -> None:
        self.diagnostic_code = diagnostic_code
        super().__init__(diagnostic_code)


def _validate_transport_payload(payload: CandidateTransportResponse) -> BoundedCandidateBatch:
    raw: object = json.loads(payload) if isinstance(payload, bytes | str) else payload
    gap = _raw_critical_rule_gap(raw)
    if gap is not None:
        raise _CandidateRuleIncompleteError(gap)
    return BoundedCandidateBatch.model_validate(raw)


def _translate_transport_payload(
    payload: CandidateTransportResponse,
    *,
    request: CompileInput,
    matrix: CandidateCapabilityMatrix,
    provider_identity: CandidateProviderIdentityView | None,
    min_confidence: float,
) -> tuple[CandidateAst, ...]:
    batch = _normalize_transport_batch(
        _validate_transport_payload(payload),
        request=request,
        matrix=matrix,
    )
    ranked = tuple(
        sorted(
            batch.candidates,
            key=lambda item: item.confidence,
            reverse=True,
        )
    )
    candidates: list[CandidateAst] = []
    for rank, item in enumerate(ranked, start=1):
        provenance = _candidate_provenance(
            identity=provider_identity,
            matrix=matrix,
            candidate_rank=rank,
        )
        if item.confidence < min_confidence:
            candidates.append(
                _unsupported(
                    request.instrument_context,
                    "candidate_provider_low_confidence",
                    provenance=provenance,
                )
            )
            continue
        try:
            _validate_candidate_against_matrix(item, matrix)
            _validate_candidate_grounding(item, matrix, request)
            candidates.append(
                _to_candidate_ast(
                    item,
                    request,
                    provenance=provenance,
                )
            )
        except _CandidateSemanticRejection as exc:
            candidates.append(
                _unsupported(
                    request.instrument_context,
                    exc.diagnostic_code,
                    provenance=provenance,
                )
            )
        except (TypeError, ValueError, ValidationError):
            candidates.append(
                _unsupported(
                    request.instrument_context,
                    "candidate_provider_invalid_output",
                    provenance=provenance,
                )
            )
    return tuple(candidates)


def _normalize_transport_batch(
    batch: BoundedCandidateBatch,
    *,
    request: CompileInput,
    matrix: CandidateCapabilityMatrix,
) -> BoundedCandidateBatch:
    """Repair only mechanically provable JSON-object transport noise.

    A JSON-object provider can choose the correct existing DSL but miscount
    Chinese character offsets or mark an explicitly written parameter as a
    Catalog default.  Both repairs below are derived from the exact utterance;
    no indicator, trigger, value, condition, or missing span is invented.
    Everything else continues through the existing fail-closed validators.
    """

    return batch.model_copy(
        update={
            "candidates": tuple(
                _normalize_transport_candidate(item, request=request, matrix=matrix)
                for item in batch.candidates
            )
        }
    )


def _normalize_transport_candidate(
    candidate: BoundedCandidate,
    *,
    request: CompileInput,
    matrix: CandidateCapabilityMatrix,
) -> BoundedCandidate:
    entry_spans = tuple(
        _normalize_exact_unique_span(span, request.utterance) for span in candidate.entry_spans
    )
    exit_spans = tuple(
        _normalize_exact_unique_span(span, request.utterance) for span in candidate.exit_spans
    )
    normalized = candidate.model_copy(
        update={
            "entry_spans": entry_spans,
            "exit_spans": exit_spans,
            "instrument_span": (
                None
                if candidate.instrument_span is None
                else _normalize_exact_unique_span(candidate.instrument_span, request.utterance)
            ),
            "backtest_span": (
                None
                if candidate.backtest_span is None
                else _normalize_exact_unique_span(candidate.backtest_span, request.utterance)
            ),
        }
    )
    context = request.instrument_context.strip().upper() if request.instrument_context else None
    if context is not None and normalized.instrument_symbol == context:
        # The host page already supplies the authoritative A-share identity.
        # A provider often repeats that context with a company-name span, but
        # the name is not proof of the six-digit security code.  Discard only
        # this redundant pair; a conflicting symbol remains fail-closed.
        normalized = normalized.model_copy(
            update={"instrument_symbol": None, "instrument_span": None}
        )
    retained_defaults: list[str] = []
    for path in normalized.defaulted_fields:
        match = _DEFAULTED_PARAMETER_PATH_RE.fullmatch(path)
        if match is None:
            retained_defaults.append(path)
            continue
        side = cast(Literal["entry", "exit"], match.group("side"))
        index = int(match.group("index"))
        leaves = normalized.entry if side == "entry" else normalized.exit
        spans = normalized.entry_spans if side == "entry" else normalized.exit_spans
        if index >= len(leaves) or index >= len(spans):
            retained_defaults.append(path)
            continue
        leaf = leaves[index]
        if not isinstance(leaf, IndicatorCandidate):
            retained_defaults.append(path)
            continue
        capability = matrix.resolve_indicator(leaf.indicator_id)
        name = match.group("name")
        value = leaf.params.get(name)
        explicit = capability is not None and (
            name in _explicit_parameter_names(spans[index].text, capability)
            or (
                value is not None
                and _special_parameter_evidence(leaf, spans[index].text, name, value)
            )
        )
        if not explicit:
            retained_defaults.append(path)
    return normalized.model_copy(update={"defaulted_fields": tuple(retained_defaults)})


def _normalize_exact_unique_span(
    span: CandidateSourceSpan,
    utterance: str,
) -> CandidateSourceSpan:
    if span.end <= len(utterance) and utterance[span.start : span.end] == span.text:
        return span
    starts = tuple(match.start() for match in re.finditer(re.escape(span.text), utterance))
    if len(starts) != 1:
        return span
    start = starts[0]
    return span.model_copy(update={"start": start, "end": start + len(span.text)})


def _raw_critical_rule_gap(raw: object) -> str | None:
    if not isinstance(raw, Mapping):
        return None
    raw_mapping = cast(Mapping[object, object], raw)
    candidates = raw_mapping.get("candidates")
    if not isinstance(candidates, list | tuple) or not candidates:
        return None
    candidate_items = cast(list[object] | tuple[object, ...], candidates)
    gaps: list[frozenset[str]] = []
    for raw_candidate in candidate_items:
        if not isinstance(raw_candidate, Mapping):
            return None
        candidate = cast(Mapping[object, object], raw_candidate)
        gap = frozenset(
            name
            for name in ("entry", "exit")
            if name not in candidate
            or (isinstance(candidate.get(name), list | tuple) and not candidate.get(name))
        )
        if not gap:
            return None
        gaps.append(gap)
    if not gaps or any(item != gaps[0] for item in gaps):
        return None
    missing = gaps[0]
    if missing == frozenset({"entry", "exit"}):
        return "strategy_rule_incomplete"
    if missing == frozenset({"entry"}):
        return "entry_rule_not_recognized"
    if missing == frozenset({"exit"}):
        return "exit_rule_not_recognized"
    return None


def _bounded_response_schema(matrix: CandidateCapabilityMatrix) -> dict[str, object]:
    schema = BoundedCandidateBatch.model_json_schema()
    definitions = _schema_object(schema, "$defs")
    indicator = _schema_object(definitions, "IndicatorCandidate")
    indicator_properties = _schema_object(indicator, "properties")
    indicator_id = _schema_object(indicator_properties, "indicator_id")
    indicator_id.pop("pattern", None)
    indicator_id["enum"] = [item.indicator_id for item in matrix.indicators]
    indicator_trigger = _schema_object(indicator_properties, "trigger")
    indicator_trigger.pop("pattern", None)
    indicator_trigger["enum"] = sorted(
        {trigger.id for item in matrix.indicators for trigger in item.triggers}
    )
    indicator_version = _schema_object(indicator_properties, "definition_version")
    indicator_version.pop("pattern", None)
    indicator_version["enum"] = sorted({item.definition_version for item in matrix.indicators})

    event = _schema_object(definitions, "EventCandidate")
    event_properties = _schema_object(event, "properties")
    event_code = _schema_object(event_properties, "event_code")
    event_code.pop("pattern", None)
    event_code["enum"] = [item.event_code for item in matrix.events]
    event_version = _schema_object(event_properties, "definition_version")
    event_version.pop("pattern", None)
    event_version["enum"] = sorted({item.definition_version for item in matrix.events})
    return cast(dict[str, object], schema)


def _schema_object(parent: Mapping[str, object], key: str) -> dict[str, object]:
    value = parent.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"candidate response schema is missing {key!r}")
    return cast(dict[str, object], value)


def _validate_candidate_against_matrix(
    candidate: BoundedCandidate,
    matrix: CandidateCapabilityMatrix,
) -> None:
    for signal in (*candidate.entry, *candidate.exit):
        if isinstance(signal, IndicatorCandidate):
            _validate_indicator_candidate(signal, matrix)
        elif isinstance(signal, EventCandidate):
            _validate_event_candidate(signal, matrix)


def _validate_indicator_candidate(
    candidate: IndicatorCandidate,
    matrix: CandidateCapabilityMatrix,
) -> None:
    capability = matrix.resolve_indicator(candidate.indicator_id)
    if capability is None:
        raise ValueError("candidate named an indicator outside the Catalog projection")
    if candidate.definition_version != capability.definition_version:
        raise ValueError("candidate named an unsupported indicator definition version")

    trigger = next((item for item in capability.triggers if item.id == candidate.trigger), None)
    if trigger is None:
        raise ValueError("candidate named a trigger outside the indicator definition")
    _validate_trigger_value(candidate.value, trigger)

    definitions = {item.name: item for item in capability.parameters}
    if set(candidate.params) - set(definitions):
        raise ValueError("candidate named an unknown indicator parameter")
    if {item.name for item in capability.parameters if item.required} - set(candidate.params):
        raise ValueError("candidate omitted a required indicator parameter")
    for name, value in candidate.params.items():
        _validate_parameter_value(value, definitions[name])
    for relation in capability.parameter_relations:
        if relation.left not in candidate.params or relation.right not in candidate.params:
            continue
        left = candidate.params[relation.left]
        right = candidate.params[relation.right]
        if isinstance(left, bool) or isinstance(right, bool):
            raise ValueError("numeric parameter relation received a boolean")
        if not isinstance(left, int | float) or not isinstance(right, int | float):
            raise ValueError("numeric parameter relation received a non-number")
        if not {
            "lt": left < right,
            "lte": left <= right,
            "gt": left > right,
            "gte": left >= right,
        }[relation.op]:
            raise ValueError("candidate violated an indicator parameter relation")


def _validate_parameter_value(
    value: JsonScalar,
    definition: CandidateParameterCapability,
) -> None:
    valid_type = {
        "boolean": isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, int | float) and not isinstance(value, bool),
        "string": isinstance(value, str),
    }[definition.value_type]
    if not valid_type:
        raise ValueError("candidate indicator parameter has the wrong type")
    if isinstance(value, float) and not isfinite(value):
        raise ValueError("candidate indicator parameter must be finite")
    if definition.choices and value not in definition.choices:
        raise ValueError("candidate indicator parameter is outside its choices")
    if isinstance(value, int | float) and not isinstance(value, bool):
        if definition.minimum is not None and value < definition.minimum:
            raise ValueError("candidate indicator parameter is below its minimum")
        if definition.maximum is not None and value > definition.maximum:
            raise ValueError("candidate indicator parameter is above its maximum")


def _validate_trigger_value(
    value: float | None,
    trigger: CandidateTriggerCapability,
) -> None:
    if trigger.value_requirement == "required" and value is None:
        raise ValueError("candidate omitted a required trigger value")
    if trigger.value_requirement == "forbidden" and value is not None:
        raise ValueError("candidate supplied a forbidden trigger value")
    if value is None:
        return
    if not isfinite(value):
        raise ValueError("candidate trigger value must be finite")
    if trigger.minimum is not None:
        invalid = value <= trigger.minimum if trigger.exclusive_minimum else value < trigger.minimum
        if invalid:
            raise ValueError("candidate trigger value is below its minimum")
    if trigger.maximum is not None:
        invalid = value >= trigger.maximum if trigger.exclusive_maximum else value > trigger.maximum
        if invalid:
            raise ValueError("candidate trigger value is above its maximum")


def _validate_event_candidate(
    candidate: EventCandidate,
    matrix: CandidateCapabilityMatrix,
) -> None:
    capability = matrix.resolve_event(candidate.event_code)
    if capability is None:
        raise ValueError("candidate named an event outside the executable Catalog projection")
    if candidate.definition_version != capability.definition_version:
        raise ValueError("candidate named an unsupported event definition version")
    if set(candidate.attributes) - set(capability.allowed_attributes):
        raise ValueError("candidate named an event attribute outside the executable definition")
    for value in candidate.attributes.values():
        if isinstance(value, float) and not isfinite(value):
            raise ValueError("candidate event attribute must be finite")
    if candidate.document_text is not None and not capability.document_text_allowed:
        raise ValueError("candidate requested unavailable full-document semantics")


def _validate_candidate_grounding(
    candidate: BoundedCandidate,
    matrix: CandidateCapabilityMatrix,
    request: CompileInput,
) -> None:
    defaults = set(candidate.defaulted_fields)
    consumed_defaults: set[str] = set()
    for index, (leaf, span) in enumerate(zip(candidate.entry, candidate.entry_spans, strict=True)):
        _validate_leaf_grounding(
            leaf,
            span,
            candidate=candidate,
            side="entry",
            index=index,
            utterance=request.utterance,
            matrix=matrix,
            defaults=defaults,
            consumed_defaults=consumed_defaults,
        )
    for index, (leaf, span) in enumerate(zip(candidate.exit, candidate.exit_spans, strict=True)):
        _validate_leaf_grounding(
            leaf,
            span,
            candidate=candidate,
            side="exit",
            index=index,
            utterance=request.utterance,
            matrix=matrix,
            defaults=defaults,
            consumed_defaults=consumed_defaults,
        )
    _validate_explicit_leaf_coverage(
        candidate.entry,
        side="entry",
        utterance=request.utterance,
        matrix=matrix,
    )
    _validate_explicit_leaf_coverage(
        candidate.exit,
        side="exit",
        utterance=request.utterance,
        matrix=matrix,
    )
    _validate_document_predicate_coverage(
        candidate.entry,
        candidate.entry_spans,
        request.utterance,
    )
    _validate_document_predicate_coverage(
        candidate.exit,
        candidate.exit_spans,
        request.utterance,
    )
    if defaults != consumed_defaults:
        raise ValueError("candidate claimed an unknown or unconsumed defaulted field")
    _validate_join_grounding(
        candidate.entry_join,
        candidate.entry_spans,
        leaf_count=len(candidate.entry),
        utterance=request.utterance,
    )
    _validate_join_grounding(
        candidate.exit_join,
        candidate.exit_spans,
        leaf_count=len(candidate.exit),
        utterance=request.utterance,
    )
    _validate_instrument_grounding(candidate, request)
    _validate_period_grounding(candidate, request)


def _validate_leaf_grounding(
    leaf: _SignalCandidate | _ExitCandidate,
    span: CandidateSourceSpan,
    *,
    candidate: BoundedCandidate,
    side: Literal["entry", "exit"],
    index: int,
    utterance: str,
    matrix: CandidateCapabilityMatrix,
    defaults: set[str],
    consumed_defaults: set[str],
) -> None:
    _validate_exact_span(span, utterance)
    action_words = _ENTRY_ACTION_WORDS if side == "entry" else _EXIT_ACTION_WORDS
    opposite_words = _EXIT_ACTION_WORDS if side == "entry" else _ENTRY_ACTION_WORDS
    action_text = re.sub(r"\s+", "", span.text).casefold()
    if not any(word.casefold() in action_text for word in action_words):
        raise ValueError("candidate source span does not contain the matching action")
    if any(word.casefold() in action_text for word in opposite_words):
        raise ValueError("candidate source span mixes entry and exit actions")

    if isinstance(leaf, HoldingPeriodCandidate):
        if side != "exit":
            raise ValueError("holding period cannot ground an entry leaf")
        has_holding_action = "持有" in span.text or "交易日后卖" in span.text
        if not has_holding_action or not _numeric_evidence(span.text, leaf.sessions):
            raise ValueError("holding-period exit lacks lexical evidence")
        return
    if isinstance(leaf, PositionReturnCandidate):
        if side != "exit":
            raise ValueError("position return cannot ground an entry leaf")
        required_word = "止盈" if leaf.trigger == "take_profit" else "止损"
        if not _labeled_numeric_evidence(span.text, (required_word,), leaf.threshold_pct):
            raise ValueError("position-return exit lacks lexical evidence")
        return
    if isinstance(leaf, TrailingDrawdownCandidate):
        if side != "exit":
            raise ValueError("trailing drawdown cannot ground an entry leaf")
        if not _labeled_numeric_evidence(
            span.text,
            ("回撤", "移动止损", "跟踪止损"),
            leaf.threshold_pct,
        ):
            raise ValueError("trailing-drawdown exit lacks lexical evidence")
        return
    if isinstance(leaf, IndicatorCandidate):
        capability = matrix.resolve_indicator(leaf.indicator_id)
        assert capability is not None
        competing_aliases = tuple(
            (item.indicator_id, item.aliases_zh) for item in matrix.indicators
        )
        selected_alias_is_grounded = _selected_alias_is_grounded(
            span.text,
            selected_id=capability.indicator_id,
            selected_aliases=capability.aliases_zh,
            competing_aliases=competing_aliases,
        )
        contextual_reference_is_grounded = _contextual_indicator_reference_is_grounded(
            leaf,
            span,
            side=side,
            candidate=candidate,
            selected_aliases=capability.aliases_zh,
            competing_aliases=competing_aliases,
        )
        if not (
            selected_alias_is_grounded
            or contextual_reference_is_grounded
            or _special_indicator_evidence(leaf, span.text)
        ):
            raise ValueError("candidate source span does not name the selected capability")
        trigger = next(item for item in capability.triggers if item.id == leaf.trigger)
        if (
            leaf.indicator_id == "technical.rsi"
            and leaf.trigger in {"above", "below"}
            and _RSI_TRANSITION_THRESHOLD_RE.search(span.text)
        ):
            raise ValueError("RSI transition wording cannot ground a static threshold trigger")
        if not (
            _has_alias(span.text, trigger.aliases_zh) or _special_trigger_evidence(leaf, span.text)
        ):
            raise ValueError("candidate source span does not name the selected capability")
        explicit_parameters = _explicit_parameter_names(span.text, capability)
        for parameter in capability.parameters:
            value = leaf.params.get(parameter.name)
            if value is None:
                continue
            path = f"/{side}/{index}/params/{parameter.name}"
            if path in defaults:
                if value != parameter.default:
                    raise ValueError("defaulted indicator parameter differs from the Catalog")
                if parameter.name in explicit_parameters:
                    raise ValueError("explicit indicator parameter cannot be replaced by a default")
                consumed_defaults.add(path)
            elif not (
                _parameter_evidence(span.text, capability, parameter.name, value)
                or _special_parameter_evidence(leaf, span.text, parameter.name, value)
                or (
                    contextual_reference_is_grounded
                    and _paired_indicator_parameter_is_grounded(
                        leaf,
                        side=side,
                        candidate=candidate,
                        parameter_name=parameter.name,
                        value=value,
                    )
                )
            ):
                raise ValueError("explicit indicator parameter lacks lexical evidence")
        if leaf.value is not None and not _numeric_evidence(span.text, leaf.value):
            raise ValueError("explicit trigger value lacks lexical evidence")
        return

    capability = matrix.resolve_event(leaf.event_code)
    assert capability is not None
    _require_selected_alias(
        span.text,
        selected_id=capability.event_code,
        selected_aliases=capability.aliases_zh,
        competing_aliases=tuple((item.event_code, item.aliases_zh) for item in matrix.events),
    )
    for name, value in leaf.attributes.items():
        if not _event_attribute_evidence(span.text, name, value):
            raise ValueError("event attribute lacks lexical evidence")
    if leaf.document_text is not None:
        document_text = leaf.document_text
        if _document_predicate_key(document_text) not in _parse_document_predicates(span.text):
            raise ValueError("document predicate lacks clause-local lexical evidence")


def _validate_exact_span(span: CandidateSourceSpan, utterance: str) -> None:
    if span.end > len(utterance) or utterance[span.start : span.end] != span.text:
        raise ValueError("candidate source span does not match the user utterance")


def _has_alias(text: str, aliases: tuple[str, ...]) -> bool:
    return any(_alias_occurrences(text, alias) for alias in aliases)


@dataclass(frozen=True, slots=True)
class _AliasOccurrence:
    start: int
    end: int
    alias: str


def _alias_occurrences(text: str, alias: str) -> tuple[_AliasOccurrence, ...]:
    prefix = r"(?<![A-Za-z0-9_])" if alias[:1].isascii() and alias[:1].isalnum() else ""
    suffix = r"(?![A-Za-z0-9_])" if alias[-1:].isascii() and alias[-1:].isalnum() else ""
    pattern = re.compile(f"{prefix}{re.escape(alias)}{suffix}", re.IGNORECASE)
    return tuple(
        _AliasOccurrence(start=match.start(), end=match.end(), alias=alias)
        for match in pattern.finditer(text)
    )


def _require_selected_alias(
    text: str,
    *,
    selected_id: str,
    selected_aliases: tuple[str, ...],
    competing_aliases: tuple[tuple[str, tuple[str, ...]], ...],
) -> None:
    if _selected_alias_is_grounded(
        text,
        selected_id=selected_id,
        selected_aliases=selected_aliases,
        competing_aliases=competing_aliases,
    ):
        return
    raise ValueError("candidate capability alias is missing or shadowed by a different entity")


def _selected_alias_is_grounded(
    text: str,
    *,
    selected_id: str,
    selected_aliases: tuple[str, ...],
    competing_aliases: tuple[tuple[str, tuple[str, ...]], ...],
) -> bool:
    selected = tuple(
        occurrence for alias in selected_aliases for occurrence in _alias_occurrences(text, alias)
    )
    if not selected:
        return False
    competitors = tuple(
        (owner, occurrence)
        for owner, aliases in competing_aliases
        if owner != selected_id
        for alias in aliases
        for occurrence in _alias_occurrences(text, alias)
    )
    for occurrence in selected:
        is_shadowed = any(
            other.start <= occurrence.start
            and other.end >= occurrence.end
            and (
                other.start < occurrence.start or other.end > occurrence.end or owner != selected_id
            )
            for owner, other in competitors
        )
        if not is_shadowed:
            return True
    return False


def _contextual_indicator_reference_is_grounded(
    leaf: IndicatorCandidate,
    span: CandidateSourceSpan,
    *,
    side: Literal["entry", "exit"],
    candidate: BoundedCandidate,
    selected_aliases: tuple[str, ...],
    competing_aliases: tuple[tuple[str, tuple[str, ...]], ...],
) -> bool:
    """Allow a bounded opposite-side reference to the same named indicator.

    A pronoun or an omitted repeated indicator is never a global alias.  It is
    accepted only on the exit side when the entry leaf names the same Catalog
    indicator explicitly and carries the exact same parameter dictionary.
    """

    if side != "exit":
        return False
    phrases = {
        "technical.ma": ("跌回这条线下",),
        "technical.macd": ("往下穿回去",),
        "technical.rsi": ("到70上方",),
    }.get(leaf.indicator_id, ())
    if not any(phrase.casefold() in span.text.casefold() for phrase in phrases):
        return False
    return any(
        isinstance(other, IndicatorCandidate)
        and other.indicator_id == leaf.indicator_id
        and other.definition_version == leaf.definition_version
        and other.params == leaf.params
        and _selected_alias_is_grounded(
            other_span.text,
            selected_id=leaf.indicator_id,
            selected_aliases=selected_aliases,
            competing_aliases=competing_aliases,
        )
        for other, other_span in zip(candidate.entry, candidate.entry_spans, strict=True)
    )


def _paired_indicator_parameter_is_grounded(
    leaf: IndicatorCandidate,
    *,
    side: Literal["entry", "exit"],
    candidate: BoundedCandidate,
    parameter_name: str,
    value: JsonScalar,
) -> bool:
    if side != "exit":
        return False
    return any(
        isinstance(other, IndicatorCandidate)
        and other.indicator_id == leaf.indicator_id
        and other.definition_version == leaf.definition_version
        and other.params.get(parameter_name) == value
        for other in candidate.entry
    )


def _special_indicator_evidence(leaf: IndicatorCandidate, text: str) -> bool:
    if leaf.indicator_id != "volume.relative":
        return False
    return re.search(r"成交量是过去\d{1,3}日平均的", text) is not None


def _special_trigger_evidence(leaf: IndicatorCandidate, text: str) -> bool:
    compact = re.sub(r"\s+", "", text).casefold()
    if leaf.indicator_id == "technical.ma" and leaf.trigger == "price_crosses_below":
        return "跌回这条线下" in compact
    if leaf.indicator_id == "technical.rsi" and leaf.trigger == "crosses_above":
        return ("重新回到" in compact and "上方" in compact) or re.search(
            r"到\d+(?:\.\d+)?上方", compact
        ) is not None
    if leaf.indicator_id == "volume.relative" and leaf.trigger == "gte_multiple":
        return re.search(r"成交量是过去\d{1,3}日平均的", compact) is not None
    if leaf.indicator_id == "technical.macd" and leaf.trigger == "death_cross":
        return "macd" in compact and "转弱" in compact
    return False


def _special_parameter_evidence(
    leaf: IndicatorCandidate,
    text: str,
    parameter_name: str,
    value: JsonScalar,
) -> bool:
    compact = re.sub(r"\s+", "", text).casefold()
    if leaf.indicator_id == "technical.ma" and parameter_name == "period":
        return f"{value}日均线" in compact
    if leaf.indicator_id == "price.rolling_high":
        if parameter_name == "period":
            return f"{value}日新高" in compact
        if parameter_name == "price_field" and value == "close":
            return "收盘" in compact
    if leaf.indicator_id == "volume.relative" and parameter_name == "baseline_period":
        return f"过去{value}日平均" in compact
    return False


def _validate_join_grounding(
    join: ConditionJoin,
    spans: tuple[CandidateSourceSpan, ...],
    *,
    leaf_count: int,
    utterance: str,
) -> None:
    if leaf_count <= 1:
        return
    start = min(item.start for item in spans)
    end = max(item.end for item in spans)
    clause = utterance[start:end]
    all_count = len(_non_overlapping_alias_occurrences(clause, _ALL_JOIN_WORDS))
    any_count = len(_non_overlapping_alias_occurrences(clause, _ANY_JOIN_WORDS))
    expected_count = leaf_count - 1
    if join == "all" and (all_count != expected_count or any_count != 0):
        raise ValueError("candidate all-join lacks one connector per source condition")
    if join == "any" and (any_count != expected_count or all_count != 0):
        raise ValueError("candidate any-join lacks one connector per source condition")


def _validate_explicit_leaf_coverage(
    leaves: tuple[_SignalCandidate | _ExitCandidate, ...],
    *,
    side: Literal["entry", "exit"],
    utterance: str,
    matrix: CandidateCapabilityMatrix,
) -> None:
    """Reject candidates that omit an explicitly named source condition.

    Per-leaf grounding proves that every emitted leaf exists in the source, but
    it does not prove the reverse.  Without this coverage check a provider can
    return only MACD for ``MACD且RSI`` and still give that one leaf a span over
    the whole clause.  Count named capabilities and position-aware exits in
    action-local source fragments, then require the candidate to cover each of
    them.
    """

    source = Counter[str]()
    for action_fragment in _source_action_fragments(
        utterance,
        side=side,
        matrix=matrix,
    ):
        for condition_fragment in _split_condition_fragments(action_fragment):
            source.update(_named_capability_keys(condition_fragment, matrix))
            if side == "exit":
                source.update(_named_position_exit_keys(condition_fragment))

    candidate = Counter(_candidate_leaf_key(item) for item in leaves)
    missing = source - candidate
    if missing:
        raise ValueError("candidate leaves do not cover every explicit source condition")


def _source_action_fragments(
    utterance: str,
    *,
    side: Literal["entry", "exit"],
    matrix: CandidateCapabilityMatrix,
) -> tuple[str, ...]:
    target_words = _ENTRY_ACTION_WORDS if side == "entry" else _EXIT_ACTION_WORDS
    all_actions = tuple(dict.fromkeys((*_ENTRY_ACTION_WORDS, *_EXIT_ACTION_WORDS)))
    fragments: list[str] = []
    for segment in re.split(r"[，,；;。！？!?]", utterance):
        occurrences = sorted(
            (
                (occurrence.start, occurrence.end, word)
                for word in all_actions
                for occurrence in _alias_occurrences(segment, word)
            ),
            key=lambda item: (item[0], -(item[1] - item[0]), item[2]),
        )
        selected: list[tuple[int, int, str]] = []
        consumed_until = -1
        for occurrence in occurrences:
            if occurrence[0] < consumed_until:
                continue
            selected.append(occurrence)
            consumed_until = occurrence[1]
        claimed_following_intervals: set[int] = set()
        for index, (start, end, word) in enumerate(selected):
            preceding_start = selected[index - 1][1] if index else 0
            preceding = segment[preceding_start:end].strip()
            following_end = selected[index + 1][0] if index + 1 < len(selected) else len(segment)
            following = segment[start:following_end].strip()
            action_side: Literal["entry", "exit"] = (
                "entry" if word in _ENTRY_ACTION_WORDS else "exit"
            )
            preceding_was_claimed = index > 0 and index - 1 in claimed_following_intervals
            preceding_has_condition = _has_named_condition(
                preceding,
                side=action_side,
                matrix=matrix,
            )
            following_has_condition = _has_named_condition(
                following,
                side=action_side,
                matrix=matrix,
            )

            # Resolve the action's orientation for every action, not only the
            # requested side.  This lets an earlier prefix action claim the
            # interval after it, so the next action cannot misread the same
            # condition as its own suffix.
            if not preceding_was_claimed and preceding_has_condition:
                fragment = preceding
            elif following_has_condition:
                fragment = following
                claimed_following_intervals.add(index)
            else:
                fragment = segment[start:end].strip()

            if word in target_words and fragment:
                fragments.append(fragment)
    return tuple(fragments)


def _has_named_condition(
    text: str,
    *,
    side: Literal["entry", "exit"],
    matrix: CandidateCapabilityMatrix,
) -> bool:
    if _named_capability_keys(text, matrix):
        return True
    return side == "exit" and bool(_named_position_exit_keys(text))


def _split_condition_fragments(text: str) -> tuple[str, ...]:
    aliases = sorted(
        {*_ALL_JOIN_WORDS, *_ANY_JOIN_WORDS},
        key=lambda item: (-len(item), item.casefold()),
    )
    pattern = "|".join(re.escape(item) for item in aliases)
    return tuple(
        item.strip() for item in re.split(pattern, text, flags=re.IGNORECASE) if item.strip()
    )


def _named_capability_keys(
    text: str,
    matrix: CandidateCapabilityMatrix,
) -> tuple[str, ...]:
    occurrences: list[tuple[str, _AliasOccurrence]] = []
    for capability in matrix.indicators:
        key = f"indicator:{capability.indicator_id}"
        occurrences.extend(
            (key, occurrence)
            for alias in capability.aliases_zh
            for occurrence in _alias_occurrences(text, alias)
        )
    for capability in matrix.events:
        key = f"event:{capability.event_code}"
        occurrences.extend(
            (key, occurrence)
            for alias in capability.aliases_zh
            for occurrence in _alias_occurrences(text, alias)
        )

    selected: set[str] = set()
    for key, occurrence in occurrences:
        shadowed = any(
            other.start <= occurrence.start
            and other.end >= occurrence.end
            and (other.start < occurrence.start or other.end > occurrence.end)
            for _other_key, other in occurrences
        )
        if not shadowed:
            selected.add(key)
    if re.search(r"成交量是过去\d{1,3}日平均的", text) is not None:
        selected.discard("indicator:market.volume")
        selected.add("indicator:volume.relative")
    return tuple(sorted(selected))


def _named_position_exit_keys(text: str) -> tuple[str, ...]:
    keys: set[str] = set()
    if re.search(r"(?:持有|成交后|买入后)[^，。；;]{0,16}\d{1,4}(?:个)?(?:交易日|交易天)", text):
        keys.add("exit:holding_period")
    if "止盈" in text:
        keys.add("exit:take_profit")
    has_trailing_stop = any(word in text for word in ("移动止损", "跟踪止损"))
    if "止损" in text and not has_trailing_stop:
        keys.add("exit:stop_loss")
    if "回撤" in text or has_trailing_stop:
        keys.add("exit:trailing_drawdown")
    return tuple(sorted(keys))


def _candidate_leaf_key(leaf: _SignalCandidate | _ExitCandidate) -> str:
    if isinstance(leaf, IndicatorCandidate):
        return f"indicator:{leaf.indicator_id}"
    if isinstance(leaf, EventCandidate):
        return f"event:{leaf.event_code}"
    if isinstance(leaf, HoldingPeriodCandidate):
        return "exit:holding_period"
    if isinstance(leaf, PositionReturnCandidate):
        return f"exit:{leaf.trigger}"
    return "exit:trailing_drawdown"


def _non_overlapping_alias_occurrences(
    text: str,
    aliases: tuple[str, ...],
) -> tuple[_AliasOccurrence, ...]:
    candidates = sorted(
        (occurrence for alias in aliases for occurrence in _alias_occurrences(text, alias)),
        key=lambda item: (item.start, -(item.end - item.start), item.alias.casefold()),
    )
    selected: list[_AliasOccurrence] = []
    consumed_until = -1
    for occurrence in candidates:
        if occurrence.start < consumed_until:
            continue
        selected.append(occurrence)
        consumed_until = occurrence.end
    return tuple(selected)


def _parameter_evidence(
    text: str,
    capability: IndicatorCandidateCapability,
    name: str,
    value: JsonScalar,
) -> bool:
    aliases = (name, *_PARAMETER_ALIAS_OVERRIDES.get(name, ()))
    if _labeled_scalar_evidence(text, aliases, value):
        return True
    parameter_index = next(
        (index for index, item in enumerate(capability.parameters) if item.name == name),
        None,
    )
    if parameter_index is None:
        return False
    for alias in capability.aliases_zh:
        for occurrence in _alias_occurrences(text, alias):
            remainder = text[occurrence.end :]
            match = re.match(r"\s*[（(]([^）)]*)[）)]", remainder)
            token_sets: list[tuple[str, ...]] = []
            if match is not None:
                token_sets.append(
                    tuple(item.strip() for item in re.split(r"[,，、/]", match.group(1)))
                )
            numeric = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)"
            separator = r"(?:\s*[,，、/]\s*|\s+)"
            count = len(capability.parameters)
            bare = re.match(
                rf"\s+(?P<values>{numeric}(?:{separator}{numeric}){{{count - 1}}})",
                remainder,
            )
            if bare is not None:
                token_sets.append(tuple(re.findall(numeric, bare.group("values"))))
            for tokens in token_sets:
                if len(tokens) == count and _token_matches_scalar(tokens[parameter_index], value):
                    return True
    return False


def _explicit_parameter_names(
    text: str,
    capability: IndicatorCandidateCapability,
) -> frozenset[str]:
    """Return parameters for which the source supplies an explicit value.

    A provider may use a Catalog default only when the user omitted that
    parameter.  Positional calls such as ``MACD(8,21,5)`` therefore mark all
    three slots explicit even when the provider tries to return the default
    ``12,26,9`` tuple.
    """

    explicit: set[str] = set()
    numeric = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)")
    for parameter in capability.parameters:
        labels = (parameter.name, *_PARAMETER_ALIAS_OVERRIDES.get(parameter.name, ()))
        for label in labels:
            for occurrence in _alias_occurrences(text, label):
                for value_match in numeric.finditer(text):
                    between = (
                        text[occurrence.end : value_match.start()]
                        if occurrence.end <= value_match.start()
                        else text[value_match.end() : occurrence.start]
                        if value_match.end() <= occurrence.start
                        else None
                    )
                    if (
                        between is not None
                        and len(between) <= 6
                        and re.fullmatch(r"[\s:=：为是()（）%％]*", between)
                    ):
                        explicit.add(parameter.name)
                        break
                if parameter.name in explicit:
                    break

    parameter_names = tuple(item.name for item in capability.parameters)
    expected_count = len(parameter_names)
    if expected_count:
        for alias in capability.aliases_zh:
            for occurrence in _alias_occurrences(text, alias):
                remainder = text[occurrence.end :]
                parenthesized = re.match(r"\s*[（(]([^）)]*)[）)]", remainder)
                token_sets: list[tuple[str, ...]] = []
                if parenthesized is not None:
                    token_sets.append(
                        tuple(
                            item.strip() for item in re.split(r"[,，、/]", parenthesized.group(1))
                        )
                    )
                scalar = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)"
                separator = r"(?:\s*[,，、/]\s*|\s+)"
                bare = re.match(
                    rf"\s+(?P<values>{scalar}(?:{separator}{scalar}){{{expected_count - 1}}})",
                    remainder,
                )
                if bare is not None:
                    token_sets.append(tuple(re.findall(scalar, bare.group("values"))))
                if any(len(tokens) == expected_count for tokens in token_sets):
                    explicit.update(parameter_names)
    return frozenset(explicit)


def _labeled_scalar_evidence(
    text: str,
    labels: tuple[str, ...],
    value: JsonScalar,
) -> bool:
    value_spans = _scalar_occurrences(text, value)
    for label in labels:
        for occurrence in _alias_occurrences(text, label):
            for start, end in value_spans:
                between = (
                    text[occurrence.end : start]
                    if occurrence.end <= start
                    else text[end : occurrence.start]
                    if end <= occurrence.start
                    else None
                )
                if (
                    between is not None
                    and len(between) <= 6
                    and re.fullmatch(
                        r"[\s:=：为是()（）%％]*",
                        between,
                    )
                ):
                    return True
    return False


def _labeled_numeric_evidence(
    text: str,
    labels: tuple[str, ...],
    value: int | float,
) -> bool:
    return _labeled_scalar_evidence(text, labels, value)


def _scalar_occurrences(text: str, value: JsonScalar) -> tuple[tuple[int, int], ...]:
    if isinstance(value, bool):
        variants = ("true", "是") if value else ("false", "否")
    elif isinstance(value, int | float):
        variants = tuple(_numeric_variants(value))
    else:
        variants = (value,)
    return tuple(
        (occurrence.start, occurrence.end)
        for variant in variants
        for occurrence in _alias_occurrences(text, str(variant))
    )


def _numeric_variants(value: int | float) -> set[str]:
    if isinstance(value, float) and not isfinite(value):
        return set()
    variants = {str(value)}
    if float(value).is_integer():
        integer = int(value)
        variants.add(str(integer))
        chinese = _CHINESE_SMALL_NUMBERS.get(integer)
        if chinese is not None:
            variants.add(chinese)
    return variants


def _token_matches_scalar(token: str, value: JsonScalar) -> bool:
    normalized = token.strip().strip("'\"")
    if isinstance(value, bool):
        return normalized.casefold() == str(value).casefold()
    if isinstance(value, int | float):
        try:
            return float(normalized) == float(value)
        except ValueError:
            return False
    return normalized.casefold() == value.casefold()


def _event_attribute_evidence(text: str, name: str, value: JsonScalar) -> bool:
    aliases = (name, *_EVENT_ATTRIBUTE_ALIAS_OVERRIDES.get(name, ()))
    if name == "is_consortium" and isinstance(value, bool):
        negative = any(word in text for word in ("非联合体", "不是联合体", "单独中标", "独立中标"))
        positive = "联合体" in text and not negative
        return positive if value else negative
    if isinstance(value, bool):
        return _labeled_scalar_evidence(text, aliases, value)
    return _labeled_scalar_evidence(text, aliases, value)


def _document_predicate_key(
    predicate: DocumentTextCandidate,
) -> tuple[str, Literal["gt", "gte"], int]:
    return (predicate.term.casefold(), predicate.comparator, predicate.value)


def _parse_document_predicates(
    text: str,
) -> frozenset[tuple[str, Literal["gt", "gte"], int]]:
    predicates: set[tuple[str, Literal["gt", "gte"], int]] = set()
    fragments = re.split(r"(?:或者|或|并且|且|同时|以及|[；，,])", text)
    comparator = r"(?P<comparator>不少于|不低于|至少|超过|大于|>=|≥|>)"
    suffix = rf"\s*(?:出现|提到|词频|次数)*\s*{comparator}\s*(?P<value>\d+)\s*次"
    patterns = (
        re.compile(rf"(?P<term>[A-Za-z][A-Za-z0-9_-]{{0,63}}){suffix}", re.IGNORECASE),
        re.compile(rf"(?P<term>[\u4e00-\u9fff]{{1,16}}){suffix}"),
    )
    for fragment in fragments:
        for pattern in patterns:
            for match in pattern.finditer(fragment):
                raw_comparator = match.group("comparator")
                normalized: Literal["gt", "gte"] = (
                    "gte" if raw_comparator in {"不少于", "不低于", "至少", ">=", "≥"} else "gt"
                )
                predicates.add(
                    (
                        match.group("term").casefold(),
                        normalized,
                        int(match.group("value")),
                    )
                )
    return frozenset(predicates)


def _validate_document_predicate_coverage(
    leaves: tuple[_SignalCandidate | _ExitCandidate, ...],
    spans: tuple[CandidateSourceSpan, ...],
    utterance: str,
) -> None:
    candidate_predicates = {
        _document_predicate_key(leaf.document_text)
        for leaf in leaves
        if isinstance(leaf, EventCandidate) and leaf.document_text is not None
    }
    start = min(item.start for item in spans)
    end = max(item.end for item in spans)
    source_predicates = set(_parse_document_predicates(utterance[start:end]))
    if candidate_predicates != source_predicates:
        raise ValueError("candidate document predicates do not cover the source clause")


def _numeric_evidence(text: str, value: int | float) -> bool:
    if isinstance(value, float) and not isfinite(value):
        return False
    variants = {str(value)}
    if float(value).is_integer():
        integer = int(value)
        variants.add(str(integer))
        chinese = _CHINESE_SMALL_NUMBERS.get(integer)
        if chinese is not None:
            variants.add(chinese)
    return any(
        re.search(rf"(?<![0-9.]){re.escape(variant)}(?![0-9.])", text) is not None
        for variant in variants
    )


def _validate_instrument_grounding(candidate: BoundedCandidate, request: CompileInput) -> None:
    span = candidate.instrument_span
    if span is not None:
        _validate_exact_span(span, request.utterance)
    if request.instrument_context is not None:
        context = request.instrument_context.strip().upper()
        if candidate.instrument_symbol is not None and (candidate.instrument_symbol != context):
            raise ValueError("candidate cannot replace the host instrument context")
        if span is not None:
            if candidate.instrument_symbol is None:
                raise ValueError("candidate supplied unused instrument evidence")
            if context.split(".", 1)[0] not in span.text:
                raise ValueError("instrument source span does not contain the host code")
        return
    if candidate.instrument_symbol is None:
        if span is not None:
            raise ValueError("instrument evidence was supplied without an instrument")
        return
    if span is None:
        raise ValueError("provider-extracted instrument requires exact source evidence")
    digits = candidate.instrument_symbol.split(".", 1)[0]
    if digits not in span.text:
        raise ValueError("instrument source span does not contain the selected code")


def _validate_period_grounding(candidate: BoundedCandidate, request: CompileInput) -> None:
    requested = parse_backtest_period(request.utterance)
    if requested.diagnostic_code is not None:
        raise _CandidateSemanticRejection(requested.diagnostic_code)
    requested_period = (
        requested.start,
        requested.end,
        requested.lookback_years,
    )
    candidate_period = (
        candidate.backtest_start,
        candidate.backtest_end,
        candidate.backtest_lookback_years,
    )
    if any(item is not None for item in requested_period) and candidate_period != requested_period:
        raise ValueError("candidate omitted or changed the explicit backtest period")

    span = candidate.backtest_span
    has_period = any(item is not None for item in candidate_period)
    if not has_period:
        if span is not None:
            raise ValueError("backtest evidence was supplied without a requested period")
        return
    if span is None:
        raise ValueError("provider-extracted backtest period requires exact source evidence")
    _validate_exact_span(span, request.utterance)
    if candidate.backtest_lookback_years is not None and not (
        _numeric_evidence(span.text, candidate.backtest_lookback_years)
        and ("年" in span.text or "year" in span.text.casefold())
    ):
        raise ValueError("lookback period lacks lexical evidence")
    for value in (candidate.backtest_start, candidate.backtest_end):
        if value is not None and not _date_evidence(span.text, value):
            raise ValueError("explicit backtest date lacks lexical evidence")
    if candidate.backtest_end is not None and candidate.backtest_end > request.as_of_date:
        raise _CandidateSemanticRejection("backtest_end_after_as_of_date")


def _date_evidence(text: str, value: date) -> bool:
    variants = (
        value.isoformat(),
        value.strftime("%Y/%m/%d"),
        f"{value.year}年{value.month}月{value.day}日",
    )
    return any(item in text for item in variants)


def _to_candidate_ast(
    item: BoundedCandidate,
    request: CompileInput,
    *,
    provenance: CandidateProvenance | None,
) -> CandidateAst:
    symbol = item.instrument_symbol
    if request.instrument_context is not None:
        context = request.instrument_context.strip().upper()
        if symbol is not None and symbol != context:
            raise ValueError("candidate cannot replace the host instrument context")
        symbol = context
    grounding = [
        CandidateGroundingEvidence(
            path=f"/entry/{index}",
            start=span.start,
            end=span.end,
            text=span.text,
        )
        for index, span in enumerate(item.entry_spans)
    ]
    grounding.extend(
        CandidateGroundingEvidence(
            path=f"/exit/{index}",
            start=span.start,
            end=span.end,
            text=span.text,
        )
        for index, span in enumerate(item.exit_spans)
    )
    if item.instrument_span is not None:
        grounding.append(
            CandidateGroundingEvidence(
                path="/instrument/symbol",
                start=item.instrument_span.start,
                end=item.instrument_span.end,
                text=item.instrument_span.text,
            )
        )
    if item.backtest_span is not None:
        grounding.append(
            CandidateGroundingEvidence(
                path="/backtest",
                start=item.backtest_span.start,
                end=item.backtest_span.end,
                text=item.backtest_span.text,
            )
        )
    return CandidateAst(
        instrument_symbol=symbol,
        entry=tuple(_to_signal(value) for value in item.entry),
        exit=tuple(_to_exit(value) for value in item.exit),
        confidence=item.confidence,
        entry_join=item.entry_join,
        exit_join=item.exit_join,
        defaulted_fields=tuple(sorted(set(item.defaulted_fields))),
        backtest_start=item.backtest_start,
        backtest_end=item.backtest_end,
        backtest_lookback_years=item.backtest_lookback_years,
        provenance=provenance,
        grounding_evidence=tuple(grounding),
    )


def _to_signal(value: _SignalCandidate) -> IndicatorIntent | EventIntent:
    if isinstance(value, IndicatorCandidate):
        return IndicatorIntent(
            indicator_id=value.indicator_id,
            definition_version=value.definition_version,
            trigger=value.trigger,
            params=tuple(sorted(value.params.items())),
            value=value.value,
        )
    return EventIntent(
        event_code=value.event_code,
        definition_version=value.definition_version,
        trigger=value.trigger,
        attributes=tuple(sorted(value.attributes.items())),
        document_text=(
            None
            if value.document_text is None
            else DocumentTextIntent(
                term=value.document_text.term,
                match_mode=value.document_text.match_mode,
                comparator=value.document_text.comparator,
                value=value.document_text.value,
                case_sensitive=value.document_text.case_sensitive,
            )
        ),
    )


def _to_exit(
    value: _ExitCandidate,
) -> (
    IndicatorIntent
    | EventIntent
    | HoldingPeriodIntent
    | PositionReturnIntent
    | TrailingDrawdownIntent
):
    if isinstance(value, HoldingPeriodCandidate):
        return HoldingPeriodIntent(sessions=value.sessions)
    if isinstance(value, PositionReturnCandidate):
        return PositionReturnIntent(
            trigger=value.trigger,
            threshold_pct=value.threshold_pct,
        )
    if isinstance(value, TrailingDrawdownCandidate):
        return TrailingDrawdownIntent(threshold_pct=value.threshold_pct)
    return _to_signal(value)


def _candidate_provenance(
    *,
    identity: CandidateProviderIdentityView | None,
    matrix: CandidateCapabilityMatrix,
    candidate_rank: int,
) -> CandidateProvenance | None:
    if identity is None:
        return None
    return CandidateProvenance(
        source="bounded_provider",
        provider=identity.provider,
        model=identity.model,
        prompt_version=identity.prompt_version,
        schema_version=identity.schema_version,
        capability_projection_version=matrix.schema_version,
        capability_projection_hash=matrix.content_hash,
        upstream_pattern_commit=_UPSTREAM_COMMIT,
        candidate_rank=candidate_rank,
    )


def _unsupported(
    symbol: str | None,
    code: str,
    *,
    provenance: CandidateProvenance | None = None,
) -> CandidateAst:
    return CandidateAst(
        instrument_symbol=symbol,
        entry=(),
        exit=(),
        confidence=0.0,
        unsupported_code=code,
        provenance=provenance,
    )
