from __future__ import annotations

import pytest

from scripts.smoke import SmokeFailure, validate_completed_evidence

_RESULT_HASH = "sha256:" + "1" * 64
_CODE_REVISION = "2" * 40


def _completed() -> dict[str, object]:
    return {
        "state": "succeeded",
        "resultAvailable": True,
        "resultHash": _RESULT_HASH,
    }


def _summary(*, trade_count: int = 1) -> dict[str, object]:
    return {
        "tradeCount": trade_count,
        "runEvidence": {"codeRevision": _CODE_REVISION},
    }


def _financial_activities() -> list[dict[str, object]]:
    return [
        {
            "kind": "signal",
            "side": "buy",
            "reason": "valuation.pe=31.81871055 lt threshold=35 => true",
            "evidence": [{"type": "financial_fact"}],
        },
        {"kind": "fill", "side": "buy", "status": "filled"},
        {"kind": "fill", "side": "sell", "status": "filled"},
    ]


def test_financial_smoke_requires_a_complete_provenance_bearing_trade() -> None:
    assert validate_completed_evidence(
        strategy_mode="financial",
        completed=_completed(),
        summary=_summary(),
        activities=_financial_activities(),
    ) == (_RESULT_HASH, _CODE_REVISION)


def test_smoke_rejects_zero_complete_trades() -> None:
    with pytest.raises(SmokeFailure, match="no complete trades"):
        validate_completed_evidence(
            strategy_mode="financial",
            completed=_completed(),
            summary=_summary(trade_count=0),
            activities=_financial_activities(),
        )


def test_smoke_rejects_one_sided_fills() -> None:
    activities = _financial_activities()[:-1]
    with pytest.raises(SmokeFailure, match="both buy and sell fills"):
        validate_completed_evidence(
            strategy_mode="financial",
            completed=_completed(),
            summary=_summary(),
            activities=activities,
        )


def test_financial_smoke_rejects_missing_financial_provenance() -> None:
    activities = _financial_activities()
    activities[0] = {**activities[0], "evidence": []}
    with pytest.raises(SmokeFailure, match=r"valuation\.pe source provenance"):
        validate_completed_evidence(
            strategy_mode="financial",
            completed=_completed(),
            summary=_summary(),
            activities=activities,
        )
