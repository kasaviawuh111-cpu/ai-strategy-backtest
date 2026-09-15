"""Storage roundtrips and concurrency only; no model/data acceptance is claimed."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Barrier, Lock
from uuid import uuid4

import pytest
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import Engine, select, update

from alembic import command
from ashare_lab.adapters.persistence.backtest_runs import create_backtest_run_engine
from ashare_lab.api import persistent_store
from ashare_lab.api.persistent_store import DIALOGUE_METADATA, SQLAlchemyDraftStore
from ashare_lab.api.schemas import BacktestReviewResponse
from ashare_lab.api.store import (
    DraftNotFoundError,
    DraftRevisionStaleError,
    IdempotencyConflictError,
    InMemoryDraftStore,
    StoreResult,
)
from ashare_lab.application.compile_strategy import CompileOutcome, CompileStatus
from ashare_lab.application.dialogue_state import VerifiedInstrumentMemory
from ashare_lab.domain.strategy import StrategySpec, canonical_hash
from ashare_lab.ports.candidate_generation import CompileInput
from ashare_lab.ports.execution_settings import ExecutionSettingsPatch
from ashare_lab.ports.idea_routing import (
    IdeaAssetMapping,
    IdeaProposal,
    IdeaRoute,
    UnboundIdeaStrategy,
)

ROOT = Path(__file__).parents[3]
NOW = datetime(2026, 9, 6, 7, 0, tzinfo=UTC)
INPUT = CompileInput("保留策略，滑点0，佣金万三", date(2026, 9, 5), "300059.SZ")
MEMORY = VerifiedInstrumentMemory("300059.SZ", "eastmoney_mx_finance_data", NOW, "东方财富")


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent", [False, True])
async def test_completed_http_response_is_immutable_and_hash_checked(
    engine: Engine, persistent: bool,
) -> None:
    store = (SQLAlchemyDraftStore(engine, initialize_schema=False)
             if persistent else InMemoryDraftStore())
    args = {"scope": "http:create-draft:v1", "key": "mobile-retry", "request_hash": "hash-one"}
    assert await store.get_http_response(**args) is None
    payload = '{"message":"原回复","data":{"as_of":"2026-09-06"}}'
    assert await store.remember_http_response(**args, payload_json=payload) == payload
    if persistent:
        store = SQLAlchemyDraftStore(engine, initialize_schema=False)
    assert await store.get_http_response(**args) == payload
    repeated = await store.remember_http_response(**args, payload_json='{"message":"不同回复"}')
    assert repeated == payload
    with pytest.raises(IdempotencyConflictError):
        await store.get_http_response(**{**args, "request_hash": "other"})
    with pytest.raises(IdempotencyConflictError):
        await store.remember_http_response(
            **{**args, "request_hash": "other"}, payload_json=payload,
        )


@pytest.fixture
def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    engine = create_backtest_run_engine(f"sqlite+pysqlite:///{tmp_path / 'dialogue.db'}")
    monkeypatch.setenv("DATABASE_URL", str(engine.url))
    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
    yield engine
    engine.dispose()


def test_missing_schema_fails_at_construction_when_initialization_is_disabled(
    tmp_path: Path,
) -> None:
    fresh_engine = create_backtest_run_engine(f"sqlite+pysqlite:///{tmp_path / 'uninitialized.db'}")
    try:
        with pytest.raises(
            ValueError, match="draft persistence schema is missing tables:",
        ) as error:
            SQLAlchemyDraftStore(fresh_engine, initialize_schema=False)
        for table_name in (
            "dialogue_drafts", "dialogue_draft_revisions", "dialogue_idempotency",
            "dialogue_backtest_reviews",
        ):
            assert table_name in str(error.value)
    finally:
        fresh_engine.dispose()


def _outcome() -> CompileOutcome:
    strategy = StrategySpec.model_validate_json(
        (ROOT / "contracts/examples/strategy.macd-volume.daily.v1.json").read_text(),
    )
    template = UnboundIdeaStrategy.model_validate(
        strategy.model_dump(mode="json", exclude={"schema_version", "instrument"}),
    )
    proposals = tuple(IdeaProposal(
        id=f"idea-{index}", title=f"方向{index}", hypothesis="存储恢复测试候选",
        entry_summary="MACD金叉买入", exit_summary="MACD死叉卖出",
        suggested_utterance="MACD金叉买入，MACD死叉卖出", capability_ids=(),
        assumptions=(), confidence=0.9, strategy_template=template,
    ) for index in (1, 2))
    return CompileOutcome(
        status=CompileStatus.NEEDS_CLARIFICATION,
        clarification="你想用哪只股票？", diagnostic_code="instrument_required",
        idea_route=IdeaRoute(
            understanding="保留已选策略。", hypothesis="存储恢复测试候选",
            asset_mapping=IdeaAssetMapping(instrument_symbol=None), proposals=proposals,
        ),
        selected_idea_proposal=proposals[0], suggested_strategy=strategy,
        suggested_strategy_hash=canonical_hash(strategy), suggested_strategy_choice_id="idea-1",
        revision_base_strategy=strategy, is_strategy_edit=True,
        pending_edit_run_requested=True, pending_edit_refresh_data=True,
        execution_settings=ExecutionSettingsPatch(
            slippage_bps=Decimal("0"), commission_rate=Decimal("0.0003"),
            minimum_commission_cny=Decimal("0"), run_robustness=False,
        ),
        pending_execution_settings=ExecutionSettingsPatch(slippage_bps=Decimal("8")),
    )


async def _create(store: SQLAlchemyDraftStore, key: str | None = None) -> StoreResult:
    return await store.create(
        outcome=_outcome(), compile_input=INPUT, request_hash="first-request", idempotency_key=key,
        pending_instrument_reuse=MEMORY,
    )


@pytest.mark.asyncio
async def test_new_engine_restores_exact_pending_draft_and_revises_it(engine: Engine) -> None:
    first = SQLAlchemyDraftStore(engine)
    created = (await _create(first)).value
    restarted_engine = create_backtest_run_engine(engine.url)
    try:
        restarted = SQLAlchemyDraftStore(restarted_engine)
        recovered = await restarted.latest_for_answer(draft_id=created.draft_id, revision=1)
        assert recovered == created
        assert recovered.outcome.selected_idea_proposal is not None
        assert isinstance(recovered.outcome.selected_idea_proposal.strategy_template,
                          UnboundIdeaStrategy)
        assert recovered.outcome.status is CompileStatus.NEEDS_CLARIFICATION
        assert isinstance(recovered.outcome.execution_settings.commission_rate, Decimal)
        assert recovered.outcome.execution_settings.slippage_bps == 0
        assert recovered.outcome.execution_settings.run_robustness is False
        assert recovered.pending_instrument_reuse == MEMORY
        changed = replace(recovered.outcome, clarification="先不运行，确认股票即可。")
        revised = await restarted.revise(
            draft_id=created.draft_id, expected_revision=1, outcome=changed,
            request_hash="revision", idempotency_key="revision", pending_instrument_reuse=MEMORY,
        )
        assert revised.value.revision == 2 and revised.value.compile_input == INPUT
        state = await first.load_latest_dialogue_state(draft_id=created.draft_id)
        assert state.outcome == changed
        with pytest.raises(DraftRevisionStaleError):
            await first.latest_for_answer(draft_id=created.draft_id, revision=1)
        table = DIALOGUE_METADATA.tables["dialogue_draft_revisions"]
        with engine.connect() as connection:
            assert connection.execute(select(table.c.revision).order_by(table.c.revision))\
                .scalars().all() == [1, 2]
    finally:
        restarted_engine.dispose()


@pytest.mark.asyncio
async def test_twenty_turn_memory_is_durable_inherited_and_independent(engine: Engine) -> None:
    first = SQLAlchemyDraftStore(engine)
    parent = (await _create(first)).value
    for index in range(23):
        await first.record_dialogue_turn(
            draft_id=parent.draft_id, user_text=f"问题{index}", assistant_text=f"答复{index}",
            intent="supplement", revision=1, verified_instrument=MEMORY, require_latest=True,
        )
    restarted = SQLAlchemyDraftStore(engine)
    state = await restarted.load_dialogue_state(draft_id=parent.draft_id, revision=1)
    assert len(state.recent_turns) == 20
    assert [turn.user_text for turn in state.recent_turns] == [f"问题{i}" for i in range(3, 23)]
    assert state.last_verified_instrument == MEMORY
    child = (await restarted.create(
        outcome=_outcome(), compile_input=INPUT, request_hash="child", idempotency_key=None,
        parent_draft_id=parent.draft_id, expected_parent_revision=1,
    )).value
    assert child.parent_draft_id == parent.draft_id
    assert await restarted.dialogue_history(draft_id=child.draft_id) == \
        await first.dialogue_history(draft_id=parent.draft_id)
    await restarted.record_dialogue_turn(
        draft_id=child.draft_id, user_text="只修改子会话", assistant_text="收到", intent="edit",
        revision=1,
    )
    assert (await first.dialogue_history(draft_id=parent.draft_id))[-1].user_text == "问题22"
    assert (await first.dialogue_history(draft_id=child.draft_id, limit=1))[0]\
        .user_text == "只修改子会话"
    with pytest.raises(ValueError):
        await restarted.dialogue_history(draft_id=child.draft_id, limit=21)


@pytest.mark.asyncio
async def test_idempotent_create_and_revision_survive_restart_and_conflicts(engine: Engine) -> None:
    first = SQLAlchemyDraftStore(engine)
    created = await _create(first, "create-key")
    restarted = SQLAlchemyDraftStore(engine)
    replay = await _create(restarted, "create-key")
    assert replay.replayed and replay.value == created.value
    with pytest.raises(IdempotencyConflictError):
        await restarted.create(outcome=_outcome(), compile_input=INPUT, request_hash="different",
                               idempotency_key="create-key")
    revised = await first.revise(
        draft_id=created.value.draft_id, outcome=_outcome(), request_hash="revision-request",
        idempotency_key="revision-key",
    )
    repeated = await restarted.revise(
        draft_id=created.value.draft_id, outcome=_outcome(), request_hash="revision-request",
        idempotency_key="revision-key",
    )
    assert repeated.replayed and repeated.value == revised.value
    with pytest.raises(IdempotencyConflictError):
        await restarted.revise(
            draft_id=created.value.draft_id, outcome=_outcome(), request_hash="different",
            idempotency_key="revision-key",
        )


@pytest.mark.asyncio
async def test_preserved_parent_response_projection_is_replayed_without_overwrite(
    engine: Engine,
) -> None:
    first = SQLAlchemyDraftStore(engine)
    parent = (await _create(first)).value
    projection = replace(parent.outcome, clarification="这轮只讨论，不修改已保存的策略。")
    arguments = dict(
        outcome=projection, compile_input=replace(INPUT, utterance="解释一下"),
        request_hash="discuss", idempotency_key="discuss", parent_draft_id=parent.draft_id,
        expected_parent_revision=1, preserve_parent_revision=True,
    )
    response = await first.create(**arguments)
    restarted = SQLAlchemyDraftStore(engine)
    replay = await restarted.create(**arguments)
    assert response.value.draft_id == parent.draft_id and response.value.revision == 1
    assert response.value.outcome == projection
    assert replay.replayed and replay.value == response.value
    assert await restarted.latest_for_answer(draft_id=parent.draft_id, revision=1) == parent


def _review(response_character: str) -> BacktestReviewResponse:
    strategy = _outcome().revision_base_strategy
    assert strategy is not None
    return BacktestReviewResponse.model_validate({
        "runId": "run:stored-review", "sourceResultHash": "sha256:" + "a" * 64,
        "generatedAt": NOW, "evidenceGrade": "limited", "evidenceReasons": ["样本仍较少"],
        "analysis": "这份报告用于检验已保存的分析结果。", "conclusion": "尚不能确认改善。",
        "optimizationCandidates": [{
            "id": f"model-opt-{index}", "title": f"调整方向{index}", "diagnosis": "比较已有结果",
            "changeDimension": "entry", "expectedEffect": "减少噪声", "tradeoff": "交易减少",
            "suggestedUtterance": "保留原标的，将买入条件改为MACD金叉，卖出条件保持不变",
            "strategy": strategy, "strategyHash": canonical_hash(strategy),
        } for index in (1, 2)],
        "modelProvenance": {"provider": "test", "model": "store-fixture",
                            "promptVersion": "test.v1", "schemaVersion": "test.v1",
                            "responseHash": "sha256:" + response_character * 64},
    })


@pytest.mark.asyncio
async def test_review_versions_restore_by_run_and_response_hash(engine: Engine) -> None:
    store = SQLAlchemyDraftStore(engine)
    first, second = _review("b"), _review("c")
    await store.remember_review(first)
    await store.remember_review(second)
    restarted = SQLAlchemyDraftStore(engine)
    await restarted.remember_review(first)
    await restarted.remember_review(first.model_copy(update={
        "generated_at": NOW + timedelta(seconds=1),
    }))
    assert await restarted.get_review(first.run_id, first.model_provenance.response_hash) == first
    recovered = await restarted.get_review(second.run_id, second.model_provenance.response_hash)
    assert recovered == second
    assert await restarted.get_review(first.run_id, "sha256:" + "d" * 64) is None
    with pytest.raises(ValueError, match="different content"):
        await restarted.remember_review(first.model_copy(update={
            "analysis": "同一键不应替换内容。",
        }))


@pytest.mark.asyncio
async def test_concurrent_revision_compare_and_swap_has_one_winner(
    engine: Engine, monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = SQLAlchemyDraftStore(engine)
    created = (await _create(first)).value
    second_engine = create_backtest_run_engine(engine.url)
    barrier, lock = Barrier(2), Lock()
    original = persistent_store._compare_and_swap
    entered = 0

    def synchronized(*args: object, **kwargs: object) -> None:
        nonlocal entered
        with lock:
            entered += 1
            should_wait = entered <= 2
        if should_wait:
            barrier.wait(timeout=5)
        original(*args, **kwargs)

    monkeypatch.setattr(persistent_store, "_compare_and_swap", synchronized)
    try:
        results = await asyncio.gather(*(
            store.revise(
                draft_id=created.draft_id, expected_revision=1, outcome=_outcome(),
                request_hash=f"edit-{index}", idempotency_key=f"edit-{index}",
            ) for index, store in enumerate((first, SQLAlchemyDraftStore(second_engine)))
        ), return_exceptions=True)
        assert sum(isinstance(item, StoreResult) for item in results) == 1
        assert sum(isinstance(item, DraftRevisionStaleError) for item in results) == 1
        assert (await first.load_latest_dialogue_state(draft_id=created.draft_id)).revision == 2
    finally:
        second_engine.dispose()


@pytest.mark.asyncio
async def test_stale_parent_and_turn_guards_remain_enforced(engine: Engine) -> None:
    memory_engine = create_backtest_run_engine("sqlite+pysqlite:///:memory:")
    try:
        with pytest.raises(ValueError, match="durable"):
            SQLAlchemyDraftStore(memory_engine)
    finally:
        memory_engine.dispose()
    store = SQLAlchemyDraftStore(engine)
    parent = (await _create(store)).value
    await store.revise(draft_id=parent.draft_id, expected_revision=1, outcome=_outcome(),
                       request_hash="edit", idempotency_key=None)
    with pytest.raises(DraftRevisionStaleError):
        await store.create(
            outcome=_outcome(), compile_input=INPUT, request_hash="stale", idempotency_key=None,
            parent_draft_id=parent.draft_id, expected_parent_revision=1,
        )
    for revision in (1, 3):
        with pytest.raises(DraftRevisionStaleError):
            await store.record_dialogue_turn(
                draft_id=parent.draft_id, user_text="旧回答", assistant_text="", intent="edit",
                revision=revision, require_latest=True,
            )
    with pytest.raises(DraftNotFoundError):
        await store.load_latest_dialogue_state(draft_id=uuid4())


@pytest.mark.asyncio
async def test_corrupt_persisted_json_is_rejected_not_reinterpreted(engine: Engine) -> None:
    store = SQLAlchemyDraftStore(engine)
    created = (await _create(store)).value
    revisions = DIALOGUE_METADATA.tables["dialogue_draft_revisions"]
    heads = DIALOGUE_METADATA.tables["dialogue_drafts"]
    with engine.begin() as connection:
        # Simulate malformed persisted input without bypassing the migration's
        # append-only guard or rewriting a valid historical revision.
        connection.execute(revisions.insert().values(
            draft_id=str(created.draft_id), revision=2,
            payload_json='{"revision":"broken"}',
        ))
        connection.execute(update(heads).where(
            heads.c.draft_id == str(created.draft_id),
        ).values(latest_revision=2))
    with pytest.raises(ValidationError):
        await SQLAlchemyDraftStore(engine).load_latest_dialogue_state(draft_id=created.draft_id)
