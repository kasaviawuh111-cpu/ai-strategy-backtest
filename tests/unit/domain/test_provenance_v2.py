from dataclasses import FrozenInstanceError, replace
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from ashare_lab.domain.provenance import (
    DataEnvelope,
    SignalProducer,
    SignalRecord,
    SourceKind,
    SourceRef,
)
from ashare_lab.domain.shared import DomainValidationError
from ashare_lab.domain.time import PointInTimeAvailability

SHANGHAI = ZoneInfo("Asia/Shanghai")
HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64
PLAN_A = "sha256:" + "d" * 64
GIT_A = "1" * 40
GIT_B = "2" * 40


def moment(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2025, 1, day, hour, minute, tzinfo=SHANGHAI)


def source(**overrides: object) -> SourceRef:
    values: dict[str, object] = {
        "provider": "choice",
        "source_id": "choice:daily:600519.SH:2025-01-02",
        "snapshot_id": "composite:" + "d" * 64,
        "schema_version": "choice.daily.v1",
        "content_sha256": HASH_A,
        "source_kind": SourceKind.PROVIDER_RECORD,
    }
    values.update(overrides)
    return SourceRef(**values)  # type: ignore[arg-type]


def availability(**overrides: object) -> PointInTimeAvailability:
    values: dict[str, object] = {
        "observed_at": moment(2, 15, 0),
        "announced_at": None,
        "first_available_at": moment(2, 15, 0),
        "signal_at": moment(2, 15, 0),
        "execution_at": moment(3, 9, 30),
        "retrieved_at": moment(3, 10, 0),
        "timezone": "Asia/Shanghai",
        "source": "choice",
        "revision_id": "choice:daily:rev-1",
    }
    values.update(overrides)
    return PointInTimeAvailability(**values)  # type: ignore[arg-type]


def envelope(**overrides: object) -> DataEnvelope:
    values: dict[str, object] = {
        "data_id": "close",
        "value": Decimal("13.82"),
        "unit": "CNY",
        "availability": availability(),
        "source_refs": (source(),),
    }
    values.update(overrides)
    return DataEnvelope(**values)  # type: ignore[arg-type]


def signal(**overrides: object) -> SignalRecord:
    first_input = envelope()
    values: dict[str, object] = {
        "instrument_id": "600519.SH",
        "condition_ref": "$.entry",
        "triggered": True,
        "signal_at": moment(2, 15, 0),
        "plan_id": PLAN_A,
        "strategy_hash": HASH_B,
        "dsl_schema_version": "strategy.v2",
        "code_revision": GIT_A,
        "input_envelopes": (first_input,),
        "source_refs": first_input.source_refs,
        "producer": SignalProducer.DETERMINISTIC_RUNTIME,
    }
    values.update(overrides)
    return SignalRecord(**values)  # type: ignore[arg-type]


def test_source_ref_is_frozen_and_has_strict_snapshot_identity() -> None:
    item = source()

    assert item.to_dict() == {
        "content_sha256": HASH_A,
        "provider": "choice",
        "schema_version": "choice.daily.v1",
        "snapshot_id": "composite:" + "d" * 64,
        "source_id": "choice:daily:600519.SH:2025-01-02",
        "source_kind": "provider_record",
    }
    with pytest.raises(FrozenInstanceError):
        item.provider = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"provider": " "}, "provider must be a non-empty string"),
        ({"source_id": ""}, "source_id must be a non-empty string"),
        ({"snapshot_id": ""}, "snapshot_id must be a non-empty string"),
        ({"schema_version": ""}, "schema_version must be a non-empty string"),
        ({"content_sha256": "a" * 64}, "content_sha256 must be a sha256 content hash"),
        ({"source_kind": "model_generated"}, "source_kind is invalid"),
    ],
)
def test_source_ref_rejects_incomplete_or_model_created_provenance(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(DomainValidationError, match=message):
        source(**overrides)


def test_source_ref_parser_rejects_secret_or_unknown_fields() -> None:
    payload = source().to_dict()
    payload["api_key"] = "must-not-enter-domain-records"

    with pytest.raises(DomainValidationError, match="unexpected fields: api_key"):
        SourceRef.from_dict(payload)


def test_data_envelope_is_frozen_and_exposes_value_only_when_available() -> None:
    item = envelope(
        availability=availability(
            first_available_at=moment(2, 15, 1),
            signal_at=None,
            execution_at=None,
        )
    )

    assert item.value_as_of(moment(2, 15, 0)) is None
    assert item.value_as_of(moment(2, 15, 1)) == Decimal("13.82")
    with pytest.raises(FrozenInstanceError):
        item.value = Decimal("0")  # type: ignore[misc]


def test_missing_value_remains_none_after_first_availability() -> None:
    item = envelope(value=None)

    assert item.value is None
    assert item.value_as_of(moment(3, 10, 0)) is None
    assert item.to_dict()["value"] is None


def test_data_envelope_requires_a_real_source_reference() -> None:
    with pytest.raises(DomainValidationError, match="source_refs must be non-empty"):
        envelope(source_refs=())


def test_data_envelope_source_must_match_point_in_time_source() -> None:
    with pytest.raises(
        DomainValidationError,
        match="availability source must match a source_refs provider",
    ):
        envelope(availability=availability(source="eastmoney"))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), b"secret", {"close": 1}])
def test_data_envelope_rejects_non_deterministic_or_unbounded_values(value: object) -> None:
    with pytest.raises(DomainValidationError, match="value type is not supported"):
        envelope(value=value)


def test_data_envelope_rejects_non_finite_decimals() -> None:
    with pytest.raises(DomainValidationError, match="value must be finite"):
        envelope(value=Decimal("NaN"))


def test_data_envelope_serialization_is_strict_and_deterministic() -> None:
    item = envelope()

    serialized = item.to_json()

    assert DataEnvelope.from_json(serialized) == item
    assert DataEnvelope.from_json(serialized).fingerprint == item.fingerprint
    payload = item.to_dict()
    payload["authorization"] = "Bearer secret"
    with pytest.raises(DomainValidationError, match="unexpected fields: authorization"):
        DataEnvelope.from_dict(payload)


def test_signal_record_requires_source_refs_and_inputs() -> None:
    with pytest.raises(DomainValidationError, match="input_envelopes must be non-empty"):
        signal(input_envelopes=(), source_refs=())

    with pytest.raises(DomainValidationError, match="source_refs must be non-empty"):
        signal(source_refs=())


def test_signal_source_refs_must_exactly_cover_input_sources() -> None:
    extra = source(
        source_id="eastmoney:event:1",
        snapshot_id="events:" + "e" * 64,
        schema_version="events.v2",
        content_sha256=HASH_C,
        provider="eastmoney",
    )

    with pytest.raises(DomainValidationError, match="source_refs must exactly match input sources"):
        signal(source_refs=(source(), extra))


def test_signal_rejects_data_not_yet_available_at_signal_time() -> None:
    late = envelope(
        availability=availability(
            first_available_at=moment(3, 9, 30),
            signal_at=None,
            execution_at=None,
        )
    )

    with pytest.raises(
        DomainValidationError,
        match="input first_available_at cannot follow signal_at",
    ):
        signal(input_envelopes=(late,), source_refs=late.source_refs)


def test_signal_rejects_missing_historical_availability() -> None:
    unavailable = envelope(
        availability=availability(
            first_available_at=None,
            signal_at=None,
            execution_at=None,
        )
    )

    with pytest.raises(DomainValidationError, match="input requires first_available_at"):
        signal(input_envelopes=(unavailable,), source_refs=unavailable.source_refs)


def test_signal_rejects_naive_time_invalid_hash_and_non_git_revision() -> None:
    with pytest.raises(DomainValidationError, match="signal_at must be timezone-aware"):
        signal(signal_at=datetime(2025, 1, 2, 15, 0))
    with pytest.raises(DomainValidationError, match="strategy_hash must be a sha256 content hash"):
        signal(strategy_hash="b" * 64)
    with pytest.raises(DomainValidationError, match="plan_id must be a sha256 content hash"):
        signal(plan_id="plan:unvalidated")
    with pytest.raises(DomainValidationError, match="code_revision must be a 40-character Git SHA"):
        signal(code_revision="local-demo")


def test_model_cannot_be_declared_as_signal_producer() -> None:
    with pytest.raises(DomainValidationError, match="producer must be deterministic_runtime"):
        signal(producer="model")


def test_signal_fingerprint_is_order_independent_and_reproducible() -> None:
    price_source = source()
    event_source = source(
        provider="eastmoney",
        source_id="eastmoney:announcement:1",
        snapshot_id="events:" + "e" * 64,
        schema_version="events.v2",
        content_sha256=HASH_C,
    )
    price = envelope(source_refs=(price_source,))
    event = envelope(
        data_id="annual_report.event",
        value=True,
        unit=None,
        availability=availability(source="eastmoney"),
        source_refs=(event_source,),
    )

    first = signal(
        input_envelopes=(price, event),
        source_refs=(price_source, event_source),
    )
    reordered = signal(
        input_envelopes=(event, price),
        source_refs=(event_source, price_source),
    )

    assert first.fingerprint == reordered.fingerprint
    assert first.to_json() == reordered.to_json()
    assert SignalRecord.from_json(first.to_json()).fingerprint == first.fingerprint


@pytest.mark.parametrize(
    "changed",
    [
        {"strategy_hash": HASH_C},
        {"plan_id": HASH_C},
        {"code_revision": GIT_B},
        {"condition_ref": "$.exit"},
        {"triggered": False},
    ],
)
def test_signal_fingerprint_changes_when_semantic_inputs_change(
    changed: dict[str, object],
) -> None:
    original = signal()

    assert signal(**changed).fingerprint != original.fingerprint


def test_signal_fingerprint_changes_with_an_aligned_signal_time() -> None:
    original = signal()
    later_input = envelope(
        availability=availability(
            signal_at=moment(3, 9, 30),
            execution_at=moment(3, 9, 30),
        )
    )

    later = signal(
        signal_at=moment(3, 9, 30),
        input_envelopes=(later_input,),
        source_refs=later_input.source_refs,
    )

    assert later.fingerprint != original.fingerprint


def test_signal_fingerprint_changes_when_snapshot_or_input_value_changes() -> None:
    original = signal()
    changed_source = source(snapshot_id="composite:" + "e" * 64)
    changed_snapshot_input = envelope(source_refs=(changed_source,))
    changed_value_input = replace(original.input_envelopes[0], value=Decimal("13.83"))

    assert (
        signal(
            input_envelopes=(changed_snapshot_input,),
            source_refs=changed_snapshot_input.source_refs,
        ).fingerprint
        != original.fingerprint
    )
    assert (
        signal(
            input_envelopes=(changed_value_input,),
            source_refs=changed_value_input.source_refs,
        ).fingerprint
        != original.fingerprint
    )


def test_signal_parser_rejects_embedded_secret_fields() -> None:
    payload = signal().to_dict()
    payload["provider_api_key"] = "secret"

    with pytest.raises(DomainValidationError, match="unexpected fields: provider_api_key"):
        SignalRecord.from_dict(payload)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"instrument_id": "600519"}, "instrument_id must be a canonical A-share symbol"),
        ({"condition_ref": "entry.conditions[0]"}, "condition_ref must be a Strategy v2 path"),
        ({"dsl_schema_version": "strategy.v1"}, "dsl_schema_version must be strategy.v2"),
    ],
)
def test_signal_identity_is_bound_to_strategy_v2_contract(
    overrides: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(DomainValidationError, match=message):
        signal(**overrides)


def test_identical_plan_dsl_snapshot_code_and_inputs_produce_identical_signal() -> None:
    assert signal().fingerprint == signal().fingerprint
