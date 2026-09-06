# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false
"""Small execution-setting plumbing fixtures, separate from real-model acceptance."""

from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ashare_lab.adapters.language import RuleBasedCandidateGenerator
from ashare_lab.api import create_app
from ashare_lab.application.compile_strategy import CompileOutcome, CompileStatus, StrategyCompiler
from ashare_lab.domain.catalog import load_catalog_directory
from ashare_lab.domain.strategy import IndicatorCondition, canonical_hash
from ashare_lab.ports.candidate_generation import CandidateAst, CandidateProvenance, CompileInput
from ashare_lab.ports.execution_settings import ExecutionSettingsPatch
from ashare_lab.ports.idea_routing import (
    IdeaAssetMapping,
    IdeaProposal,
    IdeaRoute,
    IdeaRouter,
    UnboundIdeaStrategy,
)
from ashare_lab.ports.strategy_editing import StrategyEditRequest, StrategyEditResult

ROOT = Path(__file__).parents[3]
INITIAL = {
    "utterance": "创20日新高买入，下穿20日均线卖出，回测近1年",
    "instrument_context": "300059.SZ", "as_of_date": "2026-09-05",
}


class _CandidateSettings:
    def __init__(self, settings: ExecutionSettingsPatch) -> None:
        self.settings = settings

    async def generate(self, request: CompileInput) -> tuple[CandidateAst, ...]:
        candidates = await RuleBasedCandidateGenerator().generate(request)
        return tuple(replace(item, execution_settings=self.settings, initial_cash_cny=250_000)
                     for item in candidates)


class _SettingsEditor:
    def __init__(self, patch: ExecutionSettingsPatch) -> None:
        self.patch = patch
        self.requests: list[StrategyEditRequest] = []

    async def edit(self, request: StrategyEditRequest) -> StrategyEditResult:
        self.requests.append(request)
        clarify = request.answer == "费用还不确定"
        return StrategyEditResult(
            disposition="clarify" if clarify else "apply",
            message="请确认费用数值。" if clarify else "只修改指定费用，其他保持不变。",
            strategy=None if clarify else request.strategy,
            provenance=CandidateProvenance(
                source="bounded_provider", provider="test", model="contract-fixture",
                prompt_version="test.v1", schema_version="test.v1",
                capability_projection_version="test",
                capability_projection_hash="sha256:" + "0" * 64,
                upstream_pattern_commit="0" * 40, candidate_rank=1,
            ),
            execution_settings=ExecutionSettingsPatch() if clarify else self.patch,
        )


def _compiler(
    settings: ExecutionSettingsPatch | None = None, editor: _SettingsEditor | None = None,
    idea_router: IdeaRouter | None = None,
) -> StrategyCompiler:
    return StrategyCompiler(
        generator=_CandidateSettings(settings or ExecutionSettingsPatch()),
        catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals", release_version="2026.09.01", strategy_editor=editor,
        backtest_anchor_date=date(2026, 9, 5), idea_router=idea_router,
    )


def _client(
    settings: ExecutionSettingsPatch | None = None, editor: _SettingsEditor | None = None,
) -> TestClient:
    return TestClient(create_app(compiler=_compiler(settings, editor)))


def test_candidate_execution_settings_reach_created_draft_with_defaults_resolved() -> None:
    settings = ExecutionSettingsPatch(
        slippage_bps=Decimal("0"), commission_rate=Decimal("0.0003"),
        minimum_commission_cny=Decimal("0"),
    )
    with _client(settings) as client:
        response = client.post("/api/v1/strategy-drafts", json=INITIAL)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "ready"
    assert body["strategy"]["backtest"]["initial_cash_cny"] == 250_000
    saved = ExecutionSettingsPatch.model_validate(body["execution_settings"])
    assert saved.slippage_bps == 0 and saved.minimum_commission_cny == 0
    assert saved.commission_rate == Decimal("0.0003")
    assert len(saved.model_dump(exclude_none=True)) == 12


def test_fee_only_model_edit_is_ready_without_changing_rules_dates_or_cash() -> None:
    editor = _SettingsEditor(ExecutionSettingsPatch(slippage_bps=Decimal("0")))
    with _client(editor=editor) as client:
        original = client.post("/api/v1/strategy-drafts", json=INITIAL).json()
        response = client.post("/api/v1/strategy-drafts", headers={
            "X-Conversation-Parent-Draft-ID": original["draft_id"],
        }, json={"utterance": "滑点改为0，先不回测", "as_of_date": "2026-09-05",
                 "edit_current_strategy": True})
    assert response.status_code == 201, response.text
    edited = response.json()
    assert edited["status"] == "ready"
    assert edited["strategy"] == original["strategy"]
    assert edited["strategy_hash"] == original["strategy_hash"]
    assert edited["is_strategy_edit"] is True
    assert not edited.get("run_requested")
    assert Decimal(edited["execution_settings"]["slippage_bps"]) == 0
    assert editor.requests[0].execution_settings.slippage_bps == 5


def test_revision_saves_settings_and_parent_request_supplies_current_overrides() -> None:
    editor = _SettingsEditor(ExecutionSettingsPatch(slippage_bps=Decimal("0")))
    with _client(editor=editor) as client:
        original = client.post("/api/v1/strategy-drafts", json=INITIAL).json()
        revised = client.post(
            f"/api/v1/strategy-drafts/{original['draft_id']}/revisions", json={
                "strategy": original["strategy"], "execution_settings": {
                    "slippage_bps": "11", "commission_rate": "0.0008",
                    "minimum_commission_cny": "9",
                },
            },
        )
        assert revised.status_code == 201, revised.text
        response = client.post("/api/v1/strategy-drafts", headers={
            "X-Conversation-Parent-Draft-ID": revised.json()["draft_id"],
        }, json={"utterance": "只把滑点改为0", "as_of_date": "2026-09-05",
                 "edit_current_strategy": True,
                 "execution_settings": {"minimum_commission_cny": "0"}})
    assert response.status_code == 201, response.text
    edited = response.json()
    assert edited["status"] == "ready" and edited["strategy"] == original["strategy"]
    current = editor.requests[0].execution_settings
    assert current.slippage_bps == 11 and current.minimum_commission_cny == 0
    assert current.commission_rate == Decimal("0.0008")
    saved = ExecutionSettingsPatch.model_validate(edited["execution_settings"])
    assert saved.slippage_bps == 0 and saved.minimum_commission_cny == 0
    assert saved.commission_rate == Decimal("0.0008")


def test_fee_clarification_keeps_original_settings_and_accepts_current_request_context() -> None:
    editor = _SettingsEditor(ExecutionSettingsPatch(slippage_bps=Decimal("0")))
    initial_settings = ExecutionSettingsPatch(commission_rate=Decimal("0.0008"))
    with _client(initial_settings, editor) as client:
        original = client.post("/api/v1/strategy-drafts", json=INITIAL).json()
        pending = client.post("/api/v1/strategy-drafts", headers={
            "X-Conversation-Parent-Draft-ID": original["draft_id"],
        }, json={"utterance": "费用还不确定", "as_of_date": "2026-09-05",
                 "edit_current_strategy": True})
        assert pending.status_code == 201, pending.text
        draft = pending.json()
        assert draft["status"] == "needs_clarification"
        response = client.post(
            f"/api/v1/strategy-drafts/{draft['draft_id']}/revisions/"
            f"{draft['revision']}/clarification-answers",
            json={"answer": "只把滑点设成0", "execution_settings": {
                "minimum_commission_cny": "9",
            }},
        )
    assert response.status_code == 200, response.text
    edited = response.json()["draft"]
    assert edited["status"] == "ready" and edited["strategy"] == original["strategy"]
    current = editor.requests[-1].execution_settings
    assert current.commission_rate == Decimal("0.0008") and current.minimum_commission_cny == 9
    saved = ExecutionSettingsPatch.model_validate(edited["execution_settings"])
    assert saved.slippage_bps == 0 and saved.minimum_commission_cny == 9
    assert saved.commission_rate == Decimal("0.0008")


def test_revision_without_execution_settings_preserves_previous_values() -> None:
    settings = ExecutionSettingsPatch(
        slippage_bps=Decimal("0"), commission_rate=Decimal("0.0008"),
        minimum_commission_cny=Decimal("9"),
    )
    with _client(settings) as client:
        original = client.post("/api/v1/strategy-drafts", json=INITIAL).json()
        response = client.post(
            f"/api/v1/strategy-drafts/{original['draft_id']}/revisions",
            json={"strategy": original["strategy"]},
        )
    assert response.status_code == 201, response.text
    assert response.json()["execution_settings"] == original["execution_settings"]


def test_initial_missing_stock_retains_model_extracted_execution_settings() -> None:
    settings = ExecutionSettingsPatch(slippage_bps=Decimal("0"))
    with _client(settings) as client:
        response = client.post("/api/v1/strategy-drafts", json={
            "utterance": INITIAL["utterance"] + "，滑点0", "as_of_date": INITIAL["as_of_date"],
        })
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "needs_clarification"
    assert body["diagnostic_code"] == "instrument_required"
    assert ExecutionSettingsPatch.model_validate(body["execution_settings"]).slippage_bps == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", [
    "select_bound_proposal", "select_unbound_proposal", "bound_guidance", "unbound_guidance",
])
async def test_guidance_and_proposal_selection_preserve_execution_settings(stage: str) -> None:
    settings = ExecutionSettingsPatch(
        slippage_bps=Decimal("0"), commission_rate=Decimal("0.0008"),
    )
    compiler = _compiler(settings)
    original_input = CompileInput(
        utterance=INITIAL["utterance"], instrument_context="300059.SZ", as_of_date=date(2026, 9, 5),
    )
    original = await compiler.compile(original_input)
    strategy = original.strategy
    assert strategy is not None
    entry = strategy.entry
    assert isinstance(entry, IndicatorCondition)
    bound = stage in {"select_bound_proposal", "bound_guidance"}
    proposals: list[IdeaProposal] = []
    for period in (20, 40):
        variant = strategy.model_copy(update={
            "entry": entry.model_copy(update={"params": {**entry.params, "period": period}}),
        })
        template = UnboundIdeaStrategy.model_validate(
            variant.model_dump(mode="json", exclude={"schema_version", "instrument"}),
        )
        proposals.append(IdeaProposal(
            id=f"idea-{period}", title=f"{period}日新高", hypothesis="测试候选配置接续",
            entry_summary=f"创{period}日新高买入", exit_summary="下穿20日均线卖出",
            suggested_utterance=f"创{period}日新高买入，下穿20日均线卖出",
            capability_ids=(), assumptions=(), confidence=0.9, strategy_template=template,
            instrument_symbol="300059.SZ" if bound else None,
            strategy=variant if bound else None,
            strategy_hash=canonical_hash(variant) if bound else None,
        ))
    route = IdeaRoute(
        understanding="选择一个方向。", hypothesis="测试候选配置接续",
        asset_mapping=IdeaAssetMapping(instrument_symbol="300059.SZ" if bound else None),
        proposals=tuple(proposals),
    )
    request = replace(original_input, instrument_context="300059.SZ" if bound else None)
    if stage.startswith("select_"):
        pending = CompileOutcome(
            status=CompileStatus.NEEDS_CLARIFICATION, clarification="选择一个方向。",
            diagnostic_code="idea_guidance_required", idea_route=route,
            execution_settings=settings,
        )
        turn = await compiler.answer_clarification(
            original_input=request, prior_outcome=pending, answer=proposals[0].id,
        )
        outcome = turn.outcome
        assert outcome.status == (
            CompileStatus.READY if bound else CompileStatus.NEEDS_CLARIFICATION
        )
    else:
        class Router:
            async def route(self, _request: CompileInput) -> IdeaRoute:
                return route

        guidance_compiler = _compiler(settings, idea_router=Router())
        known = CandidateAst(
            instrument_symbol=request.instrument_context, entry=(), exit=(), confidence=0.9,
            execution_settings=settings,
        )
        outcome = await guidance_compiler._compile_idea_guidance(  # pyright: ignore[reportPrivateUsage]
            request, known_candidate=known,
        )
        assert outcome is not None and outcome.diagnostic_code == "idea_guidance_required"
    assert outcome.execution_settings == settings
