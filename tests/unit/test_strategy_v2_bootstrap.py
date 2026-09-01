from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ashare_lab.adapters.language.openai_compatible import OpenAICompatibleCandidateTransport
from ashare_lab.adapters.language.vibe_candidates import (
    CandidateTransportRequest,
    CandidateTransportResponse,
)
from ashare_lab.adapters.market_data.trusted_snapshots import (
    TrustedTechnicalSnapshotLoader,
    build_trusted_security_master_snapshot,
)
from ashare_lab.api import create_app
from ashare_lab.bootstrap import create_configured_app
from ashare_lab.domain.instruments import (
    Exchange,
    SecurityMasterAssetType,
    SecurityMasterRecord,
    SecurityMasterSnapshot,
)
from ashare_lab.domain.market_data import DataSnapshotRef
from ashare_lab.domain.shared import StrongId
from ashare_lab.ports.market_data import DateRange
from ashare_lab.ports.trusted_snapshots import (
    TrustedSnapshotMetadata,
    TrustedTechnicalSnapshot,
)
from ashare_lab.settings import AppSettings

ROOT = Path(__file__).parents[2]


def _settings(tmp_path: Path, **overrides: object) -> AppSettings:
    data_root = tmp_path / "data"
    data_root.mkdir(parents=True)
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
        "strategy_v2_enabled": True,
    }
    values.update(overrides)
    return AppSettings(**values)


def test_v2_signing_secret_is_excluded_from_settings_output() -> None:
    secret = "v2-signing-secret-that-must-never-leak"
    settings = AppSettings(strategy_v2_receipt_signing_key=secret)

    assert settings.strategy_v2_receipt_signing_key is not None
    assert settings.strategy_v2_receipt_signing_key.get_secret_value() == secret
    assert "strategy_v2_receipt_signing_key" not in settings.model_dump()
    assert secret not in repr(settings)


def test_enabled_v2_without_provider_fails_readiness_with_safe_reason(tmp_path: Path) -> None:
    app = create_configured_app(_settings(tmp_path))

    with TestClient(app) as client:
        ready = client.get("/api/v1/ready")
        v2 = client.post(
            "/api/v2/strategy-drafts",
            json={
                "utterance": "MACD金叉买入，MACD死叉卖出",
                "instrument_context": "300059.SZ",
                "as_of_date": "2026-08-30",
            },
        )

    assert ready.status_code == 503
    assert ready.json()["error"]["details"] == [
        {
            "location": "strategy_v2_runtime",
            "message": "bounded candidate provider is disabled",
            "type": "readiness_check_failed",
        }
    ]
    assert v2.status_code == 503
    assert v2.json()["error"]["code"] == "strategy_v2_service_unavailable"


def test_direct_readiness_can_expose_one_named_reason_without_leaking_config() -> None:
    app = create_app(
        readiness_probe=lambda: {"strategy_v2_runtime": False},
        readiness_reasons_probe=lambda: {
            "strategy_v2_runtime": "configured composite snapshot is missing"
        },
    )

    with TestClient(app) as client:
        response = client.get("/api/v1/ready")

    assert response.status_code == 503
    assert response.json()["error"]["details"][0]["message"] == (
        "configured composite snapshot is missing"
    )


def test_explicit_server_configuration_constructs_v2_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_requests: list[CandidateTransportRequest] = []

    async def provider_response(
        self: OpenAICompatibleCandidateTransport,
        request: CandidateTransportRequest,
    ) -> CandidateTransportResponse:
        del self
        provider_requests.append(request)
        utterance = request.utterance
        entry = "MACD金叉买入"
        exit_rule = "MACD死叉卖出"
        return {
            "candidates": [
                {
                    "instrument_symbol": None,
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
                        {
                            "start": utterance.index(entry),
                            "end": utterance.index(entry) + len(entry),
                            "text": entry,
                        }
                    ],
                    "exit_spans": [
                        {
                            "start": utterance.index(exit_rule),
                            "end": utterance.index(exit_rule) + len(exit_rule),
                            "text": exit_rule,
                        }
                    ],
                    "instrument_span": None,
                    "backtest_span": None,
                    "confidence": 0.99,
                    "entry_join": "all",
                    "exit_join": "any",
                    "defaulted_fields": [
                        "/entry/0/params/fast",
                        "/entry/0/params/slow",
                        "/entry/0/params/signal",
                        "/exit/0/params/fast",
                        "/exit/0/params/slow",
                        "/exit/0/params/signal",
                    ],
                    "backtest_start": None,
                    "backtest_end": None,
                    "backtest_lookback_years": None,
                }
            ]
        }

    monkeypatch.setattr(
        OpenAICompatibleCandidateTransport,
        "generate_json",
        provider_response,
    )
    master_root = tmp_path / "master"
    built = build_trusted_security_master_snapshot(
        snapshot=SecurityMasterSnapshot(
            snapshot_id="security-master:logical-source",
            records=(
                SecurityMasterRecord(
                    symbol="300059.SZ",
                    name="东方财富",
                    exchange=Exchange.SZ,
                    asset_type=SecurityMasterAssetType.STOCK,
                    listing_date=date(2010, 3, 19),
                    tradable=True,
                    data_source="server-master",
                ),
            ),
        ),
        provider="fixture-master",
        coverage=DateRange(date(2010, 3, 19), date(2026, 8, 31)),
        generated_at=datetime.now(UTC),
        output_root=master_root,
    )
    producer_id = "composite:" + "2" * 64
    producer_root = tmp_path / "composite" / ("2" * 64)
    producer_root.mkdir(parents=True)
    (producer_root / "snapshot_manifest.json").write_text("{}", encoding="utf-8")
    settings = _settings(
        tmp_path,
        candidate_provider_mode="openai_compatible",
        candidate_provider_endpoint="https://gateway.example.test/v1/chat/completions",
        candidate_provider_name="fixture-provider",
        candidate_provider_model="fixture-model",
        candidate_provider_api_key="provider-secret",
        strategy_v2_security_master_root=master_root,
        strategy_v2_security_master_snapshot_id=built.metadata.snapshot_id,
        strategy_v2_composite_root=tmp_path / "composite",
        strategy_v2_producer_snapshot_id=producer_id,
        strategy_v2_receipt_signing_key="r" * 32,
        code_revision="a" * 40,
    )
    monkeypatch.setattr(
        TrustedTechnicalSnapshotLoader,
        "load_market_metadata",
        lambda self, producer_snapshot_id: TrustedSnapshotMetadata(
            snapshot_id="market_data:" + "3" * 64,
            provider="Choice Quant API",
            schema_version="local-parquet.market-data.v3",
            content_hash="sha256:" + "3" * 64,
            coverage=DateRange(date(2021, 8, 31), date(2026, 8, 31)),
            generated_at=datetime.now(UTC),
            producer_snapshot_id=str(producer_snapshot_id),
        ),
    )

    def load_snapshot(self: object, **kwargs: object) -> TrustedTechnicalSnapshot:
        del self
        period = kwargs["period"]
        assert isinstance(period, DateRange)
        producer = TrustedSnapshotMetadata(
            snapshot_id=producer_id,
            provider="fixture composite",
            schema_version="ashare-lab.composite-research-snapshot.v2",
            content_hash="sha256:" + "2" * 64,
            coverage=period,
            generated_at=datetime.now(UTC),
        )
        market = TrustedSnapshotMetadata(
            snapshot_id="market_data:" + "3" * 64,
            provider="Choice Quant API",
            schema_version="local-parquet.market-data.v3",
            content_hash="sha256:" + "3" * 64,
            coverage=period,
            generated_at=datetime.now(UTC),
            producer_snapshot_id=producer_id,
        )
        calendar = TrustedSnapshotMetadata(
            snapshot_id="trading_calendar:" + "4" * 64,
            provider="BaoStock Python API",
            schema_version="ashare-lab.instrument-session-snapshot.v1",
            content_hash="sha256:" + "4" * 64,
            coverage=period,
            generated_at=datetime.now(UTC),
            producer_snapshot_id=producer_id,
        )
        return TrustedTechnicalSnapshot(
            producer_snapshot_id=producer_id,
            snapshot_ref=DataSnapshotRef(
                snapshot_id=StrongId("snapshot:" + "5" * 64),
                checksum="sha256:" + "5" * 64,
                schema_version="local-parquet.market-data.v3",
                created_at=datetime.now(UTC),
                producer_schema_version=producer.schema_version,
                producer_snapshot_id=producer_id,
            ),
            market_data_metadata=market,
            calendar_metadata=calendar,
            execution_bars=(),
            signal_bars=(),
            sessions=(),
            corporate_actions=(),
            producer_metadata=producer,
        )

    monkeypatch.setattr(TrustedTechnicalSnapshotLoader, "load", load_snapshot)

    app = create_configured_app(settings)
    with TestClient(app) as client:
        response = client.get("/api/v1/ready")
        draft = client.post(
            "/api/v2/strategy-drafts",
            json={
                "utterance": "MACD金叉买入，MACD死叉卖出",
                "instrument_context": "300059.SZ",
                "as_of_date": "2099-12-31",
            },
        )

    assert response.status_code == 200
    assert response.json()["checks"]["strategy_v2_runtime"] == "ok"
    assert app.state.runtime.strategy_v2_service is not None
    assert draft.status_code == 201
    assert draft.json()["status"] == "ready"
    assert draft.json()["strategy"]["backtest"]["end"] == "2026-08-31"
    assert provider_requests[0].as_of_date == date(2026, 8, 31)
