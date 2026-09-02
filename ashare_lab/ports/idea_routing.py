"""Non-executable boundary for turning a broad view into strategy guidance.

An :class:`IdeaRoute` is deliberately not a strategy AST.  Provider-authored
understanding and hypotheses may be shown to a user, but only a subsequent
user-confirmed sentence may enter the existing strategy compiler.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from ashare_lab.ports.candidate_generation import CompileInput


@dataclass(frozen=True, slots=True)
class IdeaRouteProvenance:
    """Public identity of the bounded provider contract used for guidance."""

    source: Literal["bounded_provider"]
    provider: str
    model: str
    prompt_version: str
    schema_version: str
    capability_projection_version: str
    capability_projection_hash: str
    upstream_pattern_commit: str


@dataclass(frozen=True, slots=True)
class IdeaAssetMapping:
    """The only asset mapping the first slice is allowed to make."""

    instrument_symbol: str | None
    relation: Literal["current_page_proxy", "unbound"] = "current_page_proxy"
    rationale: str = ""
    evidence_status: Literal["host_context_only", "instrument_required"] = "host_context_only"


@dataclass(frozen=True, slots=True)
class IdeaProposal:
    """A server-authored, non-executable sentence the user may choose."""

    id: str
    title: str
    hypothesis: str
    entry_summary: str
    exit_summary: str
    suggested_utterance: str
    capability_ids: tuple[str, ...]
    assumptions: tuple[str, ...]
    confidence: float


@dataclass(frozen=True, slots=True)
class IdeaRoute:
    """One bounded guidance response; it never contains a StrategySpec."""

    understanding: str
    hypothesis: str
    asset_mapping: IdeaAssetMapping
    proposals: tuple[IdeaProposal, ...]
    provenance: IdeaRouteProvenance | None = None
    schema_version: Literal["idea-route.v1"] = "idea-route.v1"


class IdeaRouter(Protocol):
    """Interpret a non-strategy utterance without making it executable."""

    async def route(self, request: CompileInput) -> IdeaRoute | None: ...
