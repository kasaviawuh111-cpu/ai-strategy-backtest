from __future__ import annotations

import asyncio
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from ashare_lab.api.dialogue_progress import _Progress, install_dialogue_progress
from ashare_lab.ports.dialogue_progress import (
    emit_model_reasoning,
    emit_progress,
    model_reasoning_sink,
    progress_sink,
)


def test_public_direction_survives_later_search_rounds_without_leaking_between_requests() -> None:
    record = _Progress()
    record.emit("strategy_direction", "先看看趋势跟随的方向。")
    for index in range(20):
        record.emit("stock_data_enrichment", f"补查记录 {index}")
    assert len(record.events) == 12
    assert record.events[0]["message"] == "先看看趋势跟随的方向。"
    assert record.events[-1]["message"] == "补查记录 19"
    record.emit("strategy_direction", "更新后的模型方向。")
    assert sum(item["stage"] == "strategy_direction" for item in record.events) == 1
    assert record.events[-1]["message"] == "更新后的模型方向。"
    assert not _Progress().events


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/strategy-drafts",
        "/api/v1/backtest-runs/run:review/review",
    ],
)
async def test_progress_exposes_only_emitted_work_status_and_finishes(path: str) -> None:
    app = FastAPI()
    entered, release = asyncio.Event(), asyncio.Event()

    async def draft() -> dict[str, str]:
        emit_progress("web_search", "正在查找公开来源。")
        emit_model_reasoning("unit-test-only-default-hidden")
        entered.set()
        await release.wait()
        emit_progress("model_reasoning", "已收到模型推理流，仍在生成。")
        emit_progress("model_reasoning", "已收到模型推理流，仍在生成。")
        emit_progress("model_output", "模型开始返回结果。")
        return {"status": "needs_clarification"}

    app.add_api_route(path, draft, methods=["POST"])
    install_dialogue_progress(app)
    progress_id = str(uuid4())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://local"
    ) as client:
        running = asyncio.create_task(
            client.post(
                path,
                headers={"X-Dialogue-Progress-ID": progress_id},
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            status = await client.get(f"/api/v1/dialogue-progress/{progress_id}")
            assert status.json()["events"][-1]["message"] == "正在查找公开来源。"
            assert status.json()["finished"] is False
            assert set(status.json()["events"][-1]) == {"stage", "message", "elapsed_ms"}
            assert (await client.get(f"/api/v1/dialogue-progress/{uuid4()}")).status_code == 404
        finally:
            release.set()
            await running
        done = await client.get(f"/api/v1/dialogue-progress/{progress_id}")
        assert done.json()["finished"] is True
        assert done.json()["events"][-1]["stage"] == "complete"
        assert [event["stage"] for event in done.json()["events"]] == [
            "received",
            "web_search",
            "model_reasoning",
            "model_output",
            "complete",
        ]
        assert progress_sink.get() is None
        assert model_reasoning_sink.get() is None
        assert all("reasoning" not in event for event in done.json()["events"])


@pytest.mark.asyncio
async def test_opt_in_reasoning_is_incremental_bounded_separate_and_context_local() -> None:
    app = FastAPI()
    entered = [asyncio.Event() for _ in range(3)]
    release = [asyncio.Event() for _ in range(3)]
    first, second = "unit-only-delta-A\n", "unit-only-delta-B"
    large_delta = "x" * 60_000 + "unit-only-tail"
    next_model = "unit-only-next-model"

    async def draft() -> dict[str, str]:
        emit_progress("model", "正在请求第一个模型。")
        emit_model_reasoning(first)
        entered[0].set()
        await release[0].wait()
        emit_model_reasoning(second)
        # A repeated transport status must not clear the accumulated stream.
        emit_progress("model_reasoning", "已收到模型推理流，仍在生成。")
        entered[1].set()
        await release[1].wait()
        emit_model_reasoning(large_delta)
        emit_progress("model_output", "模型开始返回结果。")
        emit_progress("model", "正在请求第二个模型。")
        emit_model_reasoning(next_model)
        entered[2].set()
        await release[2].wait()
        return {"status": "ready"}

    app.add_api_route("/api/v1/strategy-drafts", draft, methods=["POST"])
    install_dialogue_progress(app, include_model_reasoning=True)
    progress_id = str(uuid4())
    outer_reasoning: list[str] = []
    outer_progress: list[tuple[str, str]] = []
    reasoning_callback = outer_reasoning.append

    def progress_callback(stage: str, message: str) -> None:
        outer_progress.append((stage, message))

    reasoning_token = model_reasoning_sink.set(reasoning_callback)
    progress_token = progress_sink.set(progress_callback)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://local",
        ) as client:

            async def post_and_check_reset() -> httpx.Response:
                response = await client.post(
                    "/api/v1/strategy-drafts",
                    headers={"X-Dialogue-Progress-ID": progress_id},
                )
                # Check the same task that entered the middleware, not only
                # the polling task's separately inherited ContextVars.
                assert model_reasoning_sink.get() is reasoning_callback
                assert progress_sink.get() is progress_callback
                return response

            running = asyncio.create_task(post_and_check_reset())
            try:
                await asyncio.wait_for(entered[0].wait(), timeout=2)
                partial = (await client.get(f"/api/v1/dialogue-progress/{progress_id}")).json()
                assert partial["finished"] is False
                assert partial["events"][-1]["reasoning"] == first
                assert partial["events"][-1]["reasoning_truncated"] is False
                release[0].set()
                await asyncio.wait_for(entered[1].wait(), timeout=2)
                merged = (await client.get(f"/api/v1/dialogue-progress/{progress_id}")).json()
                assert merged["finished"] is False
                assert merged["events"][-1]["reasoning"] == first + second
                release[1].set()
                await asyncio.wait_for(entered[2].wait(), timeout=2)
                bounded = (await client.get(f"/api/v1/dialogue-progress/{progress_id}")).json()
                streams = [event for event in bounded["events"] if "reasoning" in event]
                assert len(streams) == 2
                assert streams[0]["reasoning"] == (first + second + large_delta)[-60_000:]
                assert streams[0]["reasoning_truncated"] is True
                assert streams[1]["reasoning"] == next_model
                assert streams[1]["reasoning_truncated"] is False
                assert bounded["finished"] is False
            finally:
                for gate in release:
                    gate.set()
                response = await running
            assert response.json() == {"status": "ready"}
            done = (await client.get(f"/api/v1/dialogue-progress/{progress_id}")).json()
            assert done["finished"] is True
            assert done["events"][-1]["stage"] == "complete"
        assert outer_reasoning == []
        assert outer_progress == []
    finally:
        progress_sink.reset(progress_token)
        model_reasoning_sink.reset(reasoning_token)
    assert model_reasoning_sink.get() is None
    assert progress_sink.get() is None
