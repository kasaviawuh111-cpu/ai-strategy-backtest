"""Bounded, local-only visibility into an in-flight dialogue request."""

from __future__ import annotations

from dataclasses import dataclass, field
from time import monotonic
from uuid import UUID

from fastapi import FastAPI, HTTPException
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ashare_lab.ports.dialogue_progress import model_reasoning_sink, progress_sink


@dataclass
class _Progress:
    started: float = field(default_factory=monotonic)
    updated: float = field(default_factory=monotonic)
    events: list[dict[str, object]] = field(default_factory=lambda: list[dict[str, object]]())
    finished: bool = False

    def emit_reasoning(self, text: str) -> None:
        if self.finished or not text:
            return
        self.emit("model_reasoning", "已收到模型推理流，仍在生成。")
        event = self.events[-1]
        content = str(event.get("reasoning", "")) + text
        event["reasoning"] = content[-60_000:]
        event["reasoning_truncated"] = (
            bool(event.get("reasoning_truncated")) or len(content) > 60_000
        )

    def emit(self, stage: str, message: str) -> None:
        streaming = stage in {"model_reasoning", "model_output"}
        if self.finished or (
            not streaming and self.events and self.events[-1]["message"] == message
        ):
            return
        self.updated = monotonic()
        event: dict[str, object] = {
            "stage": stage,
            "message": message,
            "elapsed_ms": round((self.updated - self.started) * 1000),
        }
        if (
            streaming
            and self.events
            and self.events[-1]["stage"] == stage
        ):
            # A continuing stream updates its current phase, not an invented
            # new step; keep earlier searches and sources visible.
            for key in ("reasoning", "reasoning_truncated"):
                if key in self.events[-1]:
                    event[key] = self.events[-1][key]
            self.events[-1] = event
        else:
            self.events.append(event)
        # Keep the model's already-completed public explanation on screen while
        # later searches and model calls continue. It is not a reasoning delta.
        directions = [item for item in self.events if item["stage"] == "strategy_direction"]
        if directions:
            direction = directions[-1]
            recent = [item for item in self.events if item["stage"] != "strategy_direction"][-11:]
            self.events[:] = [item for item in self.events if item is direction or item in recent]
        else:
            self.events[:] = self.events[-12:]


class DialogueProgressMiddleware:
    def __init__(self, app: ASGIApp, records: dict[str, _Progress],
                 include_model_reasoning: bool = False) -> None:
        self.app = app
        self.records = records
        self.include_model_reasoning = include_model_reasoning

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = str(scope.get("path", ""))
        is_review = path.startswith("/api/v1/backtest-runs/") and path.endswith("/review")
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or not (path.startswith("/api/v1/strategy-drafts") or is_review)
        ):
            await self.app(scope, receive, send)
            return
        raw_id = Headers(scope=scope).get("x-dialogue-progress-id")
        try:
            progress_id = str(UUID(raw_id)) if raw_id else None
        except ValueError:
            progress_id = None
        if progress_id is None:
            await self.app(scope, receive, send)
            return
        for key in tuple(self.records):
            if self.records[key].finished and monotonic() - self.records[key].updated > 600:
                del self.records[key]
        if progress_id in self.records:
            # A reused id must not merge two users' or two requests' summaries.
            await self.app(scope, receive, send)
            return
        while len(self.records) >= 64:
            del self.records[next(iter(self.records))]
        record = self.records[progress_id] = _Progress()
        token = progress_sink.set(record.emit)
        reasoning_token = model_reasoning_sink.set(
            record.emit_reasoning if self.include_model_reasoning else None
        )
        record.emit("received", "已收到回测结果分析请求。" if is_review
                    else "已收到你的输入，正在处理这轮策略请求。")
        response_status = 500

        async def observe(message: Message) -> None:
            nonlocal response_status
            if message["type"] == "http.response.start":
                response_status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, observe)
        finally:
            record.emit(
                "complete" if response_status < 400 else "failed",
                "本轮处理已结束，请查看回复或待确认事项。"
                if response_status < 400
                else "本轮请求未完成，请查看错误说明。",
            )
            record.finished = True
            progress_sink.reset(token)
            model_reasoning_sink.reset(reasoning_token)


def install_dialogue_progress(app: FastAPI, *, include_model_reasoning: bool = False) -> None:
    records: dict[str, _Progress] = {}
    app.add_middleware(DialogueProgressMiddleware, records=records,
                       include_model_reasoning=include_model_reasoning)

    async def read_progress(progress_id: UUID) -> dict[str, object]:
        record = records.get(str(progress_id))
        if record is None or (record.finished and monotonic() - record.updated > 600):
            raise HTTPException(status_code=404, detail="Progress is not available")
        return {"events": list(record.events), "finished": record.finished}

    app.add_api_route("/api/v1/dialogue-progress/{progress_id}", read_progress, methods=["GET"])
