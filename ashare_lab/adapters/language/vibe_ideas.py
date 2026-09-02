"""Bounded viewpoint-to-hypothesis guidance adapted from Vibe-Trading.

The hypothesis-registry and research-autopilot pattern is adapted from
HKUDS/Vibe-Trading at commit ``1ee7df16af6eed8831014fa16ec0a9cb2d35f4e7``
(MIT).  This adapter intentionally does less than that upstream workflow: the
provider may explain a viewpoint and select fixed template ids, but it cannot
write a strategy sentence, choose another security, or generate executable
code.  Server-owned templates are the only source of suggested utterances.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ashare_lab.adapters.language.vibe_candidates import (
    CandidateCapabilityMatrix,
    CandidateJsonTransport,
    CandidateProviderIdentityView,
    CandidateTransportError,
    CandidateTransportRequest,
    CandidateTransportResponse,
    IndicatorCandidateCapability,
)
from ashare_lab.domain.market_data import AshareInstrumentCodeError, normalize_a_share_instrument
from ashare_lab.ports.candidate_generation import CompileInput
from ashare_lab.ports.idea_routing import (
    IdeaAssetMapping,
    IdeaProposal,
    IdeaRoute,
    IdeaRouteProvenance,
)

_UPSTREAM_COMMIT = "1ee7df16af6eed8831014fa16ec0a9cb2d35f4e7"
_PROMPT_VERSION = "idea-route.prompt.v1"
_PROVIDER_SCHEMA_VERSION = "idea-route-provider.v1"
_PROPOSAL_CONFIDENCE = 0.75
_LOGGER = logging.getLogger(__name__)


class _StrictIdeaModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class _ProviderIdeaRoute(_StrictIdeaModel):
    understanding: str = Field(min_length=1, max_length=240)
    hypothesis: str = Field(min_length=1, max_length=320)
    mapping_rationale: str = Field(min_length=1, max_length=240)
    template_ids: tuple[str, ...] = Field(min_length=2, max_length=3)

    @field_validator("template_ids")
    @classmethod
    def template_ids_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("idea template ids must be unique")
        return value


@dataclass(frozen=True, slots=True)
class _IdeaTemplate:
    id: Literal["ma20_trend", "rsi_reversal", "macd_momentum"]
    title: str
    entry_summary: str
    exit_summary: str
    suggested_utterance: str
    capability_ids: tuple[str, ...]


_TEMPLATES: tuple[_IdeaTemplate, ...] = (
    _IdeaTemplate(
        id="ma20_trend",
        title="等趋势确认后参与",
        entry_summary="股价上穿 20 日均线",
        exit_summary="股价跌破 20 日均线",
        suggested_utterance="股价上穿20日均线买入，跌破20日均线卖出，回测近1年",
        capability_ids=("technical.ma",),
    ),
    _IdeaTemplate(
        id="rsi_reversal",
        title="检验超跌后的反转",
        entry_summary="RSI 低于 30",
        exit_summary="RSI 高于 70",
        suggested_utterance="RSI低于30买入，高于70卖出，回测近1年",
        capability_ids=("technical.rsi",),
    ),
    _IdeaTemplate(
        id="macd_momentum",
        title="用动量转强确认",
        entry_summary="MACD 金叉",
        exit_summary="MACD 死叉",
        suggested_utterance="MACD金叉买入，死叉卖出，回测近1年",
        capability_ids=("technical.macd",),
    ),
)


class VibeIdeaRouter:
    """Turn a broad view into non-executable, fixed-template guidance."""

    def __init__(
        self,
        transport: CandidateJsonTransport,
        *,
        capability_matrix: CandidateCapabilityMatrix,
        provider_identity: CandidateProviderIdentityView | None = None,
    ) -> None:
        self._transport = transport
        self._capability_matrix = capability_matrix
        self._provider_identity = provider_identity

    async def route(self, request: CompileInput) -> IdeaRoute | None:
        templates = _available_templates(self._capability_matrix)
        if len(templates) < 2:
            return None

        instrument_symbol: str | None = None
        if request.instrument_context is not None:
            try:
                instrument_symbol = normalize_a_share_instrument(request.instrument_context).value
            except AshareInstrumentCodeError:
                return None

        template_by_id = {item.id: item for item in templates}
        transport_request = CandidateTransportRequest(
            utterance=request.utterance,
            instrument_context=instrument_symbol,
            as_of_date=request.as_of_date,
            max_candidates=3,
            response_schema=_idea_response_schema(tuple(template_by_id)),
            capability_matrix=cast(
                Mapping[str, object],
                self._capability_matrix.model_dump(mode="json"),
            ),
            capability_projection_version=self._capability_matrix.schema_version,
            capability_projection_hash=self._capability_matrix.content_hash,
            upstream_pattern_commit=_UPSTREAM_COMMIT,
            system_contract=_system_contract(templates),
        )

        provider_route: _ProviderIdeaRoute | None = None
        for attempt in range(2):
            try:
                payload = await self._transport.generate_json(transport_request)
                provider_route = _parse_provider_route(
                    payload,
                    allowed_template_ids=frozenset(template_by_id),
                )
            except (TypeError, ValueError, ValidationError):
                if attempt == 0:
                    continue
                return None
            except CandidateTransportError:
                return None
            except Exception as exc:
                _LOGGER.error(
                    "unexpected idea transport exception type=%s",
                    type(exc).__name__,
                )
                raise
            break
        if provider_route is None:
            return None

        asset_mapping = _asset_mapping(instrument_symbol)
        proposals = tuple(
            _to_proposal(
                template_by_id[template_id],
                instrument_symbol=instrument_symbol,
                route_hypothesis=provider_route.hypothesis,
            )
            for template_id in provider_route.template_ids
        )
        return IdeaRoute(
            understanding=provider_route.understanding,
            hypothesis=provider_route.hypothesis,
            asset_mapping=asset_mapping,
            proposals=proposals,
            provenance=_provenance(
                self._provider_identity,
                capability_matrix=self._capability_matrix,
            ),
        )


def _available_templates(matrix: CandidateCapabilityMatrix) -> tuple[_IdeaTemplate, ...]:
    return tuple(template for template in _TEMPLATES if _template_is_available(template, matrix))


def _template_is_available(
    template: _IdeaTemplate,
    matrix: CandidateCapabilityMatrix,
) -> bool:
    if template.id == "ma20_trend":
        capability = matrix.resolve_indicator("technical.ma")
        return _indicator_supports(
            capability,
            triggers=frozenset({"price_crosses_above", "price_crosses_below"}),
        ) and _parameter_accepts(capability, "period", 20)
    if template.id == "rsi_reversal":
        capability = matrix.resolve_indicator("technical.rsi")
        return (
            _indicator_supports(
                capability,
                triggers=frozenset({"below", "above"}),
            )
            and _trigger_accepts(capability, "below", 30)
            and _trigger_accepts(capability, "above", 70)
        )
    capability = matrix.resolve_indicator("technical.macd")
    return _indicator_supports(
        capability,
        triggers=frozenset({"golden_cross", "death_cross"}),
    )


def _indicator_supports(
    capability: IndicatorCandidateCapability | None,
    *,
    triggers: frozenset[str],
) -> bool:
    if capability is None:
        return False
    return triggers.issubset({item.id for item in capability.triggers})


def _parameter_accepts(
    capability: IndicatorCandidateCapability | None,
    name: str,
    value: float,
) -> bool:
    if capability is None:
        return False
    parameter = next((item for item in capability.parameters if item.name == name), None)
    if parameter is None:
        return False
    if parameter.minimum is not None and value < parameter.minimum:
        return False
    return parameter.maximum is None or value <= parameter.maximum


def _trigger_accepts(
    capability: IndicatorCandidateCapability | None,
    trigger_id: str,
    value: float,
) -> bool:
    if capability is None:
        return False
    trigger = next((item for item in capability.triggers if item.id == trigger_id), None)
    if trigger is None or trigger.value_requirement != "required":
        return False
    if trigger.minimum is not None:
        if trigger.exclusive_minimum and value <= trigger.minimum:
            return False
        if not trigger.exclusive_minimum and value < trigger.minimum:
            return False
    if trigger.maximum is not None:
        if trigger.exclusive_maximum and value >= trigger.maximum:
            return False
        if not trigger.exclusive_maximum and value > trigger.maximum:
            return False
    return True


def _idea_response_schema(template_ids: tuple[str, ...]) -> Mapping[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "understanding",
            "hypothesis",
            "mapping_rationale",
            "template_ids",
        ],
        "properties": {
            "understanding": {"type": "string", "minLength": 1, "maxLength": 240},
            "hypothesis": {"type": "string", "minLength": 1, "maxLength": 320},
            "mapping_rationale": {
                "type": "string",
                "minLength": 1,
                "maxLength": 240,
            },
            "template_ids": {
                "type": "array",
                "minItems": 2,
                "maxItems": min(3, len(template_ids)),
                "uniqueItems": True,
                "items": {"type": "string", "enum": list(template_ids)},
            },
        },
    }


def _system_contract(templates: tuple[_IdeaTemplate, ...]) -> str:
    template_lines = "; ".join(
        f"{item.id}={item.entry_summary}买入/{item.exit_summary}卖出" for item in templates
    )
    return (
        "你只做非执行的投资假设引导，返回给定 JSON Schema。"
        "先自然承接并用一句话复述用户观点，再从可能的影响机制写一个"
        "可检验但未被证明的假设。没有联网检索证据时，不得声称最新新闻、"
        "行情、政策结果或因果关系已经核验。"
        "不得生成股票代码、股票名称、自由交易规则、指标参数、事件时间、"
        "Python、SQL、Pine Script 或任何可执行代码。"
        "instrumentContext 如果存在，只是当前页面标的；不得替换它。"
        "instrumentContext 缺失时只做未绑定的方向建议，不得猜股票。"
        "不得宣称观点与任何股票存在因果关系。"
        "template_ids 只能从下列服务端模板中选 2 至 3 个，不得改写模板："
        f"{template_lines}。"
    )


def _parse_provider_route(
    payload: CandidateTransportResponse,
    *,
    allowed_template_ids: frozenset[str],
) -> _ProviderIdeaRoute:
    raw: object = json.loads(payload) if isinstance(payload, bytes | str) else payload
    route = _ProviderIdeaRoute.model_validate(raw)
    if not set(route.template_ids).issubset(allowed_template_ids):
        raise ValueError("provider selected an unavailable idea template")
    return route


def _asset_mapping(instrument_symbol: str | None) -> IdeaAssetMapping:
    if instrument_symbol is None:
        return IdeaAssetMapping(
            instrument_symbol=None,
            relation="unbound",
            rationale="当前只分析观点并给出可回测方向；用户选定方向后仍需补充具体 A 股。",
            evidence_status="instrument_required",
        )
    return IdeaAssetMapping(
        instrument_symbol=instrument_symbol,
        rationale=(
            f"只使用当前股票页的 {instrument_symbol} 检验价格行为；"
            "当前没有资产暴露证据，不声称该观点导致该股涨跌。"
        ),
    )


def _to_proposal(
    template: _IdeaTemplate,
    *,
    instrument_symbol: str | None,
    route_hypothesis: str,
) -> IdeaProposal:
    digest = hashlib.sha256(
        f"{instrument_symbol}|{template.id}|{template.suggested_utterance}".encode()
    ).hexdigest()[:12]
    assumptions = [
        "候选只是价格行为代理，不证明原观点与股价存在因果关系。",
        "候选为日线、只做多、近 1 年；选择后仍需通过现有 DSL 与 Catalog 校验。",
    ]
    assumptions.insert(
        1,
        "仅使用当前 A 股页面标的，模型不能替换股票。"
        if instrument_symbol is not None
        else "尚未绑定证券；选定方向后还需用户补充具体 A 股。",
    )
    return IdeaProposal(
        id=f"idea_{digest}",
        title=template.title,
        hypothesis=(f"{route_hypothesis}；本候选仅用“{template.title}”作为可回测的价格行为代理。"),
        entry_summary=template.entry_summary,
        exit_summary=template.exit_summary,
        suggested_utterance=template.suggested_utterance,
        capability_ids=template.capability_ids,
        assumptions=tuple(assumptions),
        confidence=_PROPOSAL_CONFIDENCE,
    )


def _provenance(
    identity: CandidateProviderIdentityView | None,
    *,
    capability_matrix: CandidateCapabilityMatrix,
) -> IdeaRouteProvenance | None:
    if identity is None:
        return None
    return IdeaRouteProvenance(
        source="bounded_provider",
        provider=identity.provider,
        model=identity.model,
        prompt_version=_PROMPT_VERSION,
        schema_version=_PROVIDER_SCHEMA_VERSION,
        capability_projection_version=capability_matrix.schema_version,
        capability_projection_hash=capability_matrix.content_hash,
        upstream_pattern_commit=_UPSTREAM_COMMIT,
    )
