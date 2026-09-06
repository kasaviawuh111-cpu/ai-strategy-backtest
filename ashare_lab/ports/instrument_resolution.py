"""Verified identity choices from a security-name lookup, not stock advice."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class InstrumentNameCandidate:
    symbol: str
    name: str
    source: str
    retrieved_at: datetime


class InstrumentNameAmbiguous(LookupError):
    """No unique exact name; expose verified matches for user confirmation."""

    def __init__(self, candidates: tuple[InstrumentNameCandidate, ...]) -> None:
        super().__init__("instrument name requires confirmation")
        self.candidates = candidates
