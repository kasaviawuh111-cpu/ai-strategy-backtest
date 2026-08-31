from __future__ import annotations

import io
import json
import urllib.error
from copy import deepcopy
from typing import Any
from unittest.mock import Mock

import pytest

from ashare_lab.domain.strategy import canonical_hash
from scripts.live_strict_e2e import (
    ApiClient,
    LiveE2EFailure,
    _contains_event_provenance,
    _run_case,
    _validate_cross_case_identity,
)

RUN_ID = "run:strict-replay"
RESULT_HASH = "sha256:" + "a" * 64
FINGERPRINT = "sha256:" + "b" * 64
SNAPSHOT_ID = "snapshot:" + "c" * 64
SNAPSHOT_CHECKSUM = "sha256:" + "d" * 64
PRODUCER_ID = "composite:" + "e" * 64
CATALOG_HASH = "sha256:" + "f" * 64


def _strategy() -> dict[str, object]:
    return {
        "backtest": {
            "start": "2021-08-06",
            "end": "2026-08-06",
            "initial_cash_cny": 1_000_000,
        },
        "entry": {"type": "indicator_condition"},
    }


STRATEGY_HASH = canonical_hash(_strategy())


class _HttpResponse:
    status = 200

    def __init__(self, payload: object) -> None:
        self.headers = {"X-Request-ID": "strict-live-e2e"}
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> _HttpResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class _FakeClient:
    def __init__(
        self,
        *,
        created: dict[str, object],
        statuses: list[dict[str, object]],
        summary: dict[str, object] | None = None,
        draft_strategy_hash: str = STRATEGY_HASH,
    ) -> None:
        self.created = created
        self.statuses = statuses
        self.summary = _summary() if summary is None else summary
        self.draft_strategy_hash = draft_strategy_hash
        self.calls: list[tuple[str, str]] = []

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
    ) -> Any:
        del payload
        self.calls.append((method, path))
        if (method, path) == ("POST", "/api/v1/strategy-drafts"):
            return {
                "status": "ready",
                "strategy": _strategy(),
                "strategy_hash": self.draft_strategy_hash,
            }
        if (method, path) == ("POST", "/api/v1/backtest-runs"):
            return deepcopy(self.created)
        if (method, path) == ("GET", f"/api/v1/backtest-runs/{RUN_ID}"):
            if not self.statuses:
                raise AssertionError("unexpected extra run-status read")
            return deepcopy(self.statuses.pop(0))
        if (method, path) == ("GET", f"/api/v1/backtest-runs/{RUN_ID}/summary"):
            return deepcopy(self.summary)
        if (method, path) == ("GET", f"/api/v1/backtest-runs/{RUN_ID}/series"):
            return [{"date": "2021-08-06", "equity": 100.0}]
        if (method, path) == ("GET", f"/api/v1/backtest-runs/{RUN_ID}/trades"):
            return [{"id": "fill:1", "kind": "fill"}]
        raise AssertionError(f"unexpected request: {method} {path}")


def _status(*, result_hash: str = RESULT_HASH) -> dict[str, object]:
    return {
        "id": RUN_ID,
        "state": "succeeded",
        "fingerprint": FINGERPRINT,
        "resultAvailable": True,
        "resultHash": result_hash,
    }


def _assumptions() -> dict[str, str]:
    return {
        "benchmark_policy": "funded_buy_and_hold.same_execution_ledger.v1",
        "commission_rate": "0.0003",
        "corporate_action_policy": "corporate-actions.v2",
        "dividend_tax_policy": "gross-research.v1",
        "entry_signal_validity_policy": "entry-validity.v3",
        "fee_schedule_version": "fees.2015-present.v1",
        "market_rule_version": "daily-market-rules.v2",
        "minimum_commission_cny": "5",
        "opening_auction_policy": "opening-auction.v2",
        "price_limit_mode": "wait_for_unlock",
        "rights_issue_policy": "decline-no-external-cash.v1",
    }


def _summary() -> dict[str, object]:
    return {
        "runId": RUN_ID,
        "initialCashCny": 1_000_000,
        "runEvidence": {
            "strategyHash": STRATEGY_HASH,
            "catalogHash": CATALOG_HASH,
            "dataSnapshotId": SNAPSHOT_ID,
            "dataSnapshotChecksum": SNAPSHOT_CHECKSUM,
            "dataSchemaVersion": "local-parquet.market-data.v3",
            "producerSnapshotId": PRODUCER_ID,
            "producerSnapshotSchemaVersion": "ashare-lab.composite-research-snapshot.v2",
            "codeRevision": "0123456789abcdef0123456789abcdef01234567",
            "engineVersion": "2.0.0a0",
            "executionAssumptions": _assumptions(),
        },
    }


def _created_completed_replay() -> dict[str, object]:
    return {**_status(), "replayed": True}


def test_completed_idempotent_replay_is_fully_revalidated() -> None:
    client = _FakeClient(
        created=_created_completed_replay(),
        statuses=[_status(), _status()],
    )

    result = _run_case(
        client,  # type: ignore[arg-type]
        name="technical",
        utterance="MACD 金叉买入，死叉卖出",
        wait_seconds=1,
    )

    assert result["resultHash"] == RESULT_HASH
    assert client.calls.count(("GET", f"/api/v1/backtest-runs/{RUN_ID}")) == 2
    assert ("GET", f"/api/v1/backtest-runs/{RUN_ID}/summary") in client.calls
    assert ("GET", f"/api/v1/backtest-runs/{RUN_ID}/series") in client.calls
    assert ("GET", f"/api/v1/backtest-runs/{RUN_ID}/trades") in client.calls


def test_draft_is_rejected_when_strategy_hash_does_not_match_strategy() -> None:
    client = _FakeClient(
        created=_created_completed_replay(),
        statuses=[_status(), _status()],
        draft_strategy_hash="sha256:" + "2" * 64,
    )

    with pytest.raises(LiveE2EFailure, match="draft strategy_hash does not match"):
        _run_case(
            client,  # type: ignore[arg-type]
            name="technical",
            utterance="MACD 金叉买入，死叉卖出",
            wait_seconds=1,
        )

    assert ("POST", "/api/v1/backtest-runs") not in client.calls


def test_result_is_rejected_when_strategy_hash_does_not_match_draft() -> None:
    summary = _summary()
    evidence = summary["runEvidence"]
    assert isinstance(evidence, dict)
    evidence["strategyHash"] = "sha256:" + "3" * 64
    client = _FakeClient(
        created=_created_completed_replay(),
        statuses=[_status()],
        summary=summary,
    )

    with pytest.raises(LiveE2EFailure, match="result strategyHash does not match"):
        _run_case(
            client,  # type: ignore[arg-type]
            name="technical",
            utterance="MACD 金叉买入，死叉卖出",
            wait_seconds=1,
        )


def test_completed_replay_is_rejected_when_result_hash_changes_during_reads() -> None:
    changed_hash = "sha256:" + "9" * 64
    client = _FakeClient(
        created=_created_completed_replay(),
        statuses=[_status(), _status(result_hash=changed_hash)],
    )

    with pytest.raises(LiveE2EFailure, match="resultHash changed"):
        _run_case(
            client,  # type: ignore[arg-type]
            name="technical",
            utterance="MACD 金叉买入，死叉卖出",
            wait_seconds=1,
        )


def test_result_available_on_non_replay_is_rejected_before_result_reads() -> None:
    created = {**_status(), "replayed": False}
    client = _FakeClient(created=created, statuses=[])

    with pytest.raises(LiveE2EFailure, match="outside a succeeded idempotent replay"):
        _run_case(
            client,  # type: ignore[arg-type]
            name="technical",
            utterance="MACD 金叉买入，死叉卖出",
            wait_seconds=1,
        )

    assert all(not path.endswith(("/summary", "/series", "/trades")) for _, path in client.calls)


def test_completed_replay_requires_fee_and_policy_manifest_evidence() -> None:
    summary = _summary()
    evidence = summary["runEvidence"]
    assert isinstance(evidence, dict)
    assumptions = evidence["executionAssumptions"]
    assert isinstance(assumptions, dict)
    assumptions.pop("fee_schedule_version")
    client = _FakeClient(
        created=_created_completed_replay(),
        statuses=[_status()],
        summary=summary,
    )

    with pytest.raises(LiveE2EFailure, match="fee_schedule_version"):
        _run_case(
            client,  # type: ignore[arg-type]
            name="technical",
            utterance="MACD 金叉买入，死叉卖出",
            wait_seconds=1,
        )


def test_technical_and_event_cases_must_share_execution_identity() -> None:
    technical = _summary()
    event = _summary()
    event_evidence = event["runEvidence"]
    assert isinstance(event_evidence, dict)
    event_assumptions = event_evidence["executionAssumptions"]
    assert isinstance(event_assumptions, dict)
    event_assumptions["commission_rate"] = "0.001"

    with pytest.raises(LiveE2EFailure, match="fee, market-rule, and execution policies"):
        _validate_cross_case_identity(
            {
                "technical": {"summary": technical},
                "event": {"summary": event},
            }
        )


def test_event_provenance_requires_explicit_second_precision() -> None:
    evidence = {
        "sourceEventId": "AN202403141626765931",
        "provider": "eastmoney",
        "rawResponseSha256": "a" * 64,
        "availableAt": "2024-03-14T20:57:33+08:00",
        "timestampPrecision": "second",
        "timeQuality": "vendor_observed",
        "validationStatus": "validated",
    }
    activities = [{"kind": "signal", "evidence": [evidence]}]

    assert _contains_event_provenance(activities)

    for field, unsupported in (
        ("timestampPrecision", "minute"),
        ("timeQuality", "date_only_conservative"),
        ("validationStatus", "unverified"),
    ):
        changed = deepcopy(activities)
        changed[0]["evidence"][0][field] = unsupported
        assert not _contains_event_provenance(changed)


def test_new_queued_run_still_uses_the_same_completed_result_checks() -> None:
    created = {
        "id": RUN_ID,
        "state": "queued",
        "fingerprint": FINGERPRINT,
        "resultAvailable": False,
        "resultHash": None,
        "replayed": False,
    }
    client = _FakeClient(created=created, statuses=[_status(), _status()])

    result = _run_case(
        client,  # type: ignore[arg-type]
        name="technical",
        utterance="MACD 金叉买入，死叉卖出",
        wait_seconds=1,
    )

    assert result["resultHash"] == RESULT_HASH


def test_api_client_retries_transport_failure_with_one_stable_post_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[object] = []

    def urlopen(request: object, *, timeout: float) -> _HttpResponse:
        assert timeout == 1
        requests.append(request)
        if len(requests) == 1:
            raise urllib.error.URLError("temporary TLS disconnect")
        return _HttpResponse({"status": "ready"})

    sleep = Mock()
    monkeypatch.setattr("scripts.live_strict_e2e.urllib.request.urlopen", urlopen)
    monkeypatch.setattr("scripts.live_strict_e2e.time.sleep", sleep)
    client = ApiClient("https://api.example.cn", 1, retry_delay_seconds=0.01)

    assert client.request("POST", "/api/v1/strategy-drafts", {"utterance": "MACD"}) == {
        "status": "ready"
    }
    assert len(requests) == 2
    keys = [request.get_header("Idempotency-key") for request in requests]  # type: ignore[attr-defined]
    assert keys[0]
    assert len(set(keys)) == 1
    sleep.assert_called_once_with(0.01)


def test_api_client_does_not_retry_http_application_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    urlopen = Mock(
        side_effect=urllib.error.HTTPError(
            "https://api.example.cn/api/v1/ready",
            503,
            "Unavailable",
            {},
            io.BytesIO(b'{"detail":"not ready"}'),
        )
    )
    monkeypatch.setattr("scripts.live_strict_e2e.urllib.request.urlopen", urlopen)
    client = ApiClient("https://api.example.cn", 1)

    with pytest.raises(LiveE2EFailure, match="returned HTTP 503"):
        client.request("GET", "/api/v1/ready")

    assert urlopen.call_count == 1


def test_api_client_stops_after_bounded_transport_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    urlopen = Mock(side_effect=TimeoutError("timed out"))
    monkeypatch.setattr("scripts.live_strict_e2e.urllib.request.urlopen", urlopen)
    monkeypatch.setattr("scripts.live_strict_e2e.time.sleep", Mock())
    client = ApiClient("https://api.example.cn", 1, transport_attempts=3)

    with pytest.raises(LiveE2EFailure, match="after 3 transport attempts"):
        client.request("GET", "/api/v1/ready")

    assert urlopen.call_count == 3
