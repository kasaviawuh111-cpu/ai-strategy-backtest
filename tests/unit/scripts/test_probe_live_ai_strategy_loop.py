from __future__ import annotations

import pytest

from scripts.probe_live_ai_strategy_loop import _require_live_model


@pytest.mark.parametrize(
    "provenance",
    [
        None,
        {},
        {"provider": "disabled", "model": "unconfigured"},
        {"provider": "local", "model": "local-template"},
        {"provider": "rule_based", "model": "rule-based"},
        {"provider": "fixture", "model": "deepseek-v4-pro"},
    ],
)
def test_live_probe_rejects_missing_or_local_model_provenance(provenance: object) -> None:
    with pytest.raises(RuntimeError, match=r"instead of a live model|model provenance"):
        _require_live_model(provenance, stage="strategy generation")


def test_live_probe_accepts_nonlocal_model_provenance() -> None:
    provenance = {
        "provider": "deepseek",
        "model": "deepseek-v4-pro",
        "prompt_version": "idea-route.prompt.v1",
    }

    assert _require_live_model(provenance, stage="strategy generation") is provenance
