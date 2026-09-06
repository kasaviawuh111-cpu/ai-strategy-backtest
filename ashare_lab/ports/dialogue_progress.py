"""Request-local status, plus separately opt-in upstream DeepSeek reasoning."""

from collections.abc import Callable
from contextvars import ContextVar

progress_sink: ContextVar[Callable[[str, str], None] | None] = ContextVar(
    "dialogue_progress_sink", default=None
)
model_reasoning_sink: ContextVar[Callable[[str], None] | None] = ContextVar(
    "dialogue_model_reasoning_sink", default=None
)


def emit_model_reasoning(text: str) -> None:
    """Forward the provider's returned text only to an explicitly enabled UI sink."""
    sink = model_reasoning_sink.get()
    if sink is not None:
        sink(text)


def emit_progress(stage: str, message: str) -> None:
    sink = progress_sink.get()
    if sink is not None:
        sink(stage, message)
