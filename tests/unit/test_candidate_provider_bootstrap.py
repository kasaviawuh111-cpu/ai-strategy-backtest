from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from ashare_lab.adapters.language.openai_compatible import (
    OpenAICompatibleCandidateTransport,
)
from ashare_lab.adapters.language.rule_based import RuleBasedCandidateGenerator
from ashare_lab.adapters.language.vibe_candidates import CandidateTransportRequest
from ashare_lab.api.app import create_app
from ashare_lab.bootstrap import create_configured_app
from ashare_lab.ports.candidate_generation import CandidateAst, CompileInput, IndicatorIntent
from ashare_lab.settings import AppSettings

ROOT = Path(__file__).parents[2]
MODEL_ENTRY_TEXT = "MACD 12 26 9 快线向上穿越时买入"
MODEL_EXIT_TEXT = "MACD 12 26 9 快线向下穿越时卖出"
MODEL_UTTERANCE = f"{MODEL_ENTRY_TEXT}，{MODEL_EXIT_TEXT}"


def _settings(tmp_path: Path, **overrides: object) -> AppSettings:
    data_root = tmp_path / "data"
    data_root.mkdir()
    (data_root / "daily_ohlcv.parquet").touch()
    values: dict[str, object] = {
        "app_env": "test",
        "database_url": f"sqlite+pysqlite:///{tmp_path / 'runs.db'}",
        "data_root": data_root,
        "catalog_root": ROOT / "catalogs",
        "queue_backend": "thread",
        "market_data_profile": "generic_parquet",
        "session_reference_mode": "research_300059",
        "event_data_required": False,
    }
    values.update(overrides)
    return AppSettings(**values)


def _draft(utterance: str) -> dict[str, str]:
    return {
        "utterance": utterance,
        "instrument_context": "300059.SZ",
        "as_of_date": "2026-08-30",
    }


def _macd_batch() -> dict[str, object]:
    entry_text = MODEL_ENTRY_TEXT
    exit_text = MODEL_EXIT_TEXT
    return {
        "candidates": [
            {
                "instrument_symbol": "300059.SZ",
                "entry": [
                    {
                        "kind": "indicator",
                        "indicator_id": "technical.macd",
                        "definition_version": "1.0.0",
                        "trigger": "golden_cross",
                        "params": {"fast": 12, "slow": 26, "signal": 9},
                    }
                ],
                "exit": [
                    {
                        "kind": "indicator",
                        "indicator_id": "technical.macd",
                        "definition_version": "1.0.0",
                        "trigger": "death_cross",
                        "params": {"fast": 12, "slow": 26, "signal": 9},
                    }
                ],
                "entry_spans": [
                    {"start": 0, "end": len(entry_text), "text": entry_text},
                ],
                "exit_spans": [
                    {
                        "start": len(entry_text) + 1,
                        "end": len(entry_text) + 1 + len(exit_text),
                        "text": exit_text,
                    },
                ],
                "confidence": 0.91,
            }
        ]
    }


def test_direct_http_factory_keeps_deterministic_default() -> None:
    with TestClient(create_app()) as client:
        known = cast(
            Response,
            client.post(
                "/api/v1/strategy-drafts",
                json=_draft("MACD 金叉买入，死叉卖出"),
            ),
        )

    assert known.status_code == 201
    assert known.json()["status"] == "ready"
    assert known.json()["candidate_provenance"] is None


def test_unconfigured_provider_keeps_fast_path_and_marks_long_tail_unavailable(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    app = create_configured_app(settings)

    with TestClient(app) as client:
        known = cast(
            Response,
            client.post(
                "/api/v1/strategy-drafts",
                json=_draft("MACD 金叉买入，死叉卖出"),
            ),
        )
        long_tail = cast(
            Response,
            client.post(
                "/api/v1/strategy-drafts",
                json=_draft("把我脑海里的那套条件自动拿来交易"),
            ),
        )

    assert known.status_code == 201
    assert known.json()["status"] == "ready"
    assert known.json()["candidate_provenance"] is None
    assert long_tail.status_code == 201
    assert long_tail.json()["status"] == "unsupported"
    assert long_tail.json()["diagnostic_code"] == "candidate_provider_unavailable"
    assert app.state.candidate_provider_identity == {
        "provider": "disabled",
        "model": "unconfigured",
        "prompt_version": "ashare-lab.bounded-candidate.prompt.v1",
        "schema_version": "ashare-lab.bounded-candidate.schema.v1",
    }
    assert app.state.candidate_provider_response_mode == "disabled"
    assert app.state.candidate_capability_projection["version"] == ("candidate-capabilities.v1")
    assert app.state.candidate_capability_projection["hash"].startswith("sha256:")


def test_configured_provider_is_used_only_after_allowlisted_rule_miss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[CandidateTransportRequest] = []

    async def generate_json(
        _self: OpenAICompatibleCandidateTransport,
        request: CandidateTransportRequest,
    ) -> dict[str, object]:
        calls.append(request)
        return _macd_batch()

    async def deterministic_generate(
        _self: RuleBasedCandidateGenerator,
        request: CompileInput,
    ) -> tuple[CandidateAst, ...]:
        if request.utterance == "规则快路测试":
            params = (("fast", 12), ("signal", 9), ("slow", 26))
            return (
                CandidateAst(
                    instrument_symbol=request.instrument_context,
                    entry=(
                        IndicatorIntent(
                            indicator_id="technical.macd",
                            definition_version="1.0.0",
                            trigger="golden_cross",
                            params=params,
                        ),
                    ),
                    exit=(
                        IndicatorIntent(
                            indicator_id="technical.macd",
                            definition_version="1.0.0",
                            trigger="death_cross",
                            params=params,
                        ),
                    ),
                    confidence=1.0,
                ),
            )
        return (
            CandidateAst(
                instrument_symbol=request.instrument_context,
                entry=(),
                exit=(),
                confidence=0.0,
                unsupported_code="no_supported_signal_recognized",
            ),
        )

    monkeypatch.setattr(OpenAICompatibleCandidateTransport, "generate_json", generate_json)
    monkeypatch.setattr(RuleBasedCandidateGenerator, "generate", deterministic_generate)
    secret = "provider-secret-must-not-leak"
    settings = _settings(
        tmp_path,
        candidate_provider_mode="openai_compatible",
        candidate_provider_endpoint="https://gateway.example.test/v1/chat/completions",
        candidate_provider_name="fixture-gateway",
        candidate_provider_model="fixture-model",
        candidate_provider_api_key=secret,
        candidate_provider_timeout_seconds=1.0,
    )
    app = create_configured_app(settings)

    with TestClient(app) as client:
        known = cast(
            Response,
            client.post(
                "/api/v1/strategy-drafts",
                json=_draft("规则快路测试"),
            ),
        )
        long_tail = cast(
            Response,
            client.post(
                "/api/v1/strategy-drafts",
                json=_draft(MODEL_UTTERANCE),
            ),
        )

    assert known.status_code == 201
    assert known.json()["status"] == "ready"
    assert long_tail.status_code == 201
    assert long_tail.json()["status"] == "ready"
    assert long_tail.json()["candidate_provenance"] == {
        "source": "bounded_provider",
        "provider": "fixture-gateway",
        "model": "fixture-model",
        "prompt_version": "ashare-lab.bounded-candidate.prompt.v1",
        "schema_version": "ashare-lab.bounded-candidate.schema.v1",
        "capability_projection_version": "candidate-capabilities.v1",
        "capability_projection_hash": calls[0].capability_projection_hash,
        "upstream_pattern_commit": "e90b6c6cd9fea23067a85667e7fbf74f9d73ea48",
        "candidate_rank": 1,
    }
    assert [item["path"] for item in long_tail.json()["candidate_grounding"]["spans"]] == [
        "/entry/0",
        "/exit/0",
    ]
    assert len(calls) == 1
    assert calls[0].max_candidates == 3
    assert calls[0].capability_projection_version == "candidate-capabilities.v1"
    indicator_ids = {
        item["indicator_id"]  # type: ignore[index]
        for item in calls[0].capability_matrix["indicators"]  # type: ignore[union-attr]
    }
    event_codes = {
        item["event_code"]  # type: ignore[index]
        for item in calls[0].capability_matrix["events"]  # type: ignore[union-attr]
    }
    assert "technical.macd" in indicator_ids
    assert "event.financial_results.annual_report" in event_codes
    assert app.state.candidate_provider_identity == {
        "provider": "fixture-gateway",
        "model": "fixture-model",
        "prompt_version": "ashare-lab.bounded-candidate.prompt.v1",
        "schema_version": "ashare-lab.bounded-candidate.schema.v1",
    }
    assert app.state.candidate_provider_response_mode == "json_schema"
    assert secret not in str(app.state.candidate_provider_identity)
    assert secret not in long_tail.text
