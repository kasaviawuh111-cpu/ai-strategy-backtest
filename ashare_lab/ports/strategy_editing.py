"""Untrusted model edits to a server-owned, previously validated strategy."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Literal, Protocol

from ashare_lab.domain.strategy import StrategySpec
from ashare_lab.ports.candidate_generation import CandidateProvenance
from ashare_lab.ports.clarification_dialogue import ClarificationDialogueTurn
from ashare_lab.ports.execution_settings import ExecutionSettingsPatch
from ashare_lab.ports.instrument_resolution import InstrumentNameCandidate


@dataclass(frozen=True, slots=True)
class StrategyEditRequest:
    answer: str
    prior_utterance: str
    strategy: StrategySpec
    as_of_date: date
    recent_turns: tuple[ClarificationDialogueTurn, ...] = ()
    backtest_results: tuple[Mapping[str, object], ...] = ()
    pending_clarification: str | None = None
    pending_run_requested: bool = False
    pending_refresh_data: bool = False
    instrument_candidates: tuple[InstrumentNameCandidate, ...] = ()
    execution_settings: ExecutionSettingsPatch = field(default_factory=ExecutionSettingsPatch)
    pending_execution_settings: ExecutionSettingsPatch = field(
        default_factory=ExecutionSettingsPatch,
    )


@dataclass(frozen=True, slots=True)
class StrategyEditResult:
    disposition: Literal[
        "apply", "change_instrument", "request_optimization", "clarify", "discuss", "not_edit",
        "conversation",
    ]
    message: str
    strategy: StrategySpec | None
    provenance: CandidateProvenance
    run_requested: bool = False
    refresh_data: bool = False
    instrument_refs: tuple[str, ...] = ()
    execution_settings: ExecutionSettingsPatch = field(default_factory=ExecutionSettingsPatch)


class StrategyEditor(Protocol):
    async def edit(self, request: StrategyEditRequest) -> StrategyEditResult | None: ...
