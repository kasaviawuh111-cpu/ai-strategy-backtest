from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from ashare_lab.domain.shared import DomainValidationError
from ashare_lab.domain.time import (
    AShareSessionSchedule,
    PointInTimeAvailability,
    earliest_tradable_at,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
SESSIONS = (
    date(2025, 1, 2),
    date(2025, 1, 3),
    date(2025, 1, 6),
)


def moment(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2025, 1, day, hour, minute, tzinfo=SHANGHAI)


def availability(**overrides: object) -> PointInTimeAvailability:
    values: dict[str, object] = {
        "observed_at": moment(2, 14, 0),
        "announced_at": moment(2, 15, 10),
        "first_available_at": moment(2, 15, 11),
        "signal_at": moment(3, 9, 30),
        "execution_at": moment(3, 9, 30),
        "retrieved_at": moment(3, 10, 0),
        "timezone": "Asia/Shanghai",
        "source": "eastmoney",
        "revision_id": "announcement:1",
    }
    values.update(overrides)
    return PointInTimeAvailability(**values)  # type: ignore[arg-type]


def test_visibility_is_gated_only_by_first_available_at() -> None:
    point_in_time = availability(
        first_available_at=moment(2, 15, 11),
        signal_at=None,
        execution_at=None,
        retrieved_at=moment(2, 14, 30),
    )

    assert not point_in_time.is_available_at(moment(2, 15, 10))
    assert point_in_time.is_available_at(moment(2, 15, 11))
    assert point_in_time.value_as_of(Decimal("12.5"), moment(2, 15, 10)) is None
    assert point_in_time.value_as_of(Decimal("12.5"), moment(2, 15, 11)) == Decimal("12.5")


def test_retrieved_at_never_substitutes_for_missing_first_available_at() -> None:
    point_in_time = availability(
        announced_at=None,
        first_available_at=None,
        signal_at=None,
        execution_at=None,
        retrieved_at=moment(3, 10, 0),
    )

    assert not point_in_time.is_available_at(moment(6, 15, 0))
    assert point_in_time.value_as_of(Decimal("8.8"), moment(6, 15, 0)) is None


def test_missing_value_remains_none_after_it_becomes_available() -> None:
    point_in_time = availability(signal_at=None, execution_at=None)

    assert point_in_time.value_as_of(None, moment(3, 10, 0)) is None


@pytest.mark.parametrize(
    "field_name",
    [
        "observed_at",
        "announced_at",
        "first_available_at",
        "signal_at",
        "execution_at",
        "retrieved_at",
    ],
)
def test_every_provided_datetime_must_be_timezone_aware(field_name: str) -> None:
    with pytest.raises(DomainValidationError, match=rf"{field_name} must be timezone-aware"):
        availability(**{field_name: datetime(2025, 1, 2, 10, 0)})


def test_datetime_timezone_must_match_the_declared_timezone() -> None:
    with pytest.raises(DomainValidationError, match="observed_at must use timezone Asia/Shanghai"):
        availability(observed_at=datetime(2025, 1, 2, 6, 0, tzinfo=UTC))


def test_declared_timezone_must_be_an_iana_timezone() -> None:
    with pytest.raises(DomainValidationError, match="timezone must be a valid IANA timezone"):
        availability(timezone="UTC+8")


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {"first_available_at": moment(2, 15, 9)},
            "first_available_at cannot precede announced_at",
        ),
        (
            {"first_available_at": moment(3, 9, 31)},
            "first_available_at cannot follow signal_at",
        ),
        (
            {"signal_at": moment(3, 9, 31), "execution_at": moment(3, 9, 30)},
            "signal_at cannot follow execution_at",
        ),
        (
            {"first_available_at": None},
            "signal_at requires first_available_at",
        ),
        (
            {"signal_at": None},
            "execution_at requires signal_at",
        ),
    ],
)
def test_signal_and_execution_times_are_monotonic(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(DomainValidationError, match=message):
        availability(**overrides)


def test_serialization_is_deterministic_and_round_trips() -> None:
    point_in_time = availability()

    serialized = point_in_time.to_json()

    assert serialized == (
        '{"announced_at":"2025-01-02T15:10:00+08:00",'
        '"execution_at":"2025-01-03T09:30:00+08:00",'
        '"first_available_at":"2025-01-02T15:11:00+08:00",'
        '"observed_at":"2025-01-02T14:00:00+08:00",'
        '"retrieved_at":"2025-01-03T10:00:00+08:00",'
        '"revision_id":"announcement:1",'
        '"signal_at":"2025-01-03T09:30:00+08:00",'
        '"source":"eastmoney",'
        '"timezone":"Asia/Shanghai"}'
    )
    assert PointInTimeAvailability.from_json(serialized) == point_in_time


def test_serialization_rejects_unknown_fields() -> None:
    payload = availability().to_dict()
    payload["retrieved_as_first_available"] = True

    with pytest.raises(DomainValidationError, match="unexpected fields"):
        PointInTimeAvailability.from_dict(payload)


def test_serialization_rejects_non_object_payloads() -> None:
    with pytest.raises(DomainValidationError, match="payload must be an object"):
        PointInTimeAvailability.from_dict(["not", "an", "object"])


def test_a_share_schedule_is_explicit_and_validated() -> None:
    with pytest.raises(DomainValidationError, match="strictly increasing"):
        AShareSessionSchedule(
            session_open=time(9, 30),
            morning_close=time(9, 20),
            afternoon_open=time(13, 0),
            session_close=time(15, 0),
        )


@pytest.mark.parametrize(
    ("available_at", "expected"),
    [
        (moment(2, 8, 0), moment(2, 9, 30)),
        (moment(2, 9, 30), moment(2, 9, 30)),
        (moment(2, 10, 16), moment(2, 10, 16)),
        (moment(2, 11, 30), moment(2, 13, 0)),
        (moment(2, 12, 15), moment(2, 13, 0)),
        (moment(2, 13, 0), moment(2, 13, 0)),
        (moment(2, 14, 59), moment(2, 14, 59)),
        (moment(2, 15, 0), moment(3, 9, 30)),
        (moment(3, 18, 0), moment(6, 9, 30)),
        (moment(4, 10, 0), moment(6, 9, 30)),
    ],
)
def test_earliest_tradable_time_uses_only_injected_sessions(
    available_at: datetime, expected: datetime
) -> None:
    assert earliest_tradable_at(available_at, sessions=SESSIONS) == expected


def test_earliest_tradable_time_rejects_a_timezone_mismatch() -> None:
    with pytest.raises(DomainValidationError, match="available_at must use timezone Asia/Shanghai"):
        earliest_tradable_at(
            datetime(2025, 1, 2, 1, 30, tzinfo=UTC),
            sessions=SESSIONS,
        )


def test_earliest_tradable_time_fails_closed_without_a_future_session() -> None:
    with pytest.raises(DomainValidationError, match="no trading session at or after"):
        earliest_tradable_at(moment(6, 15, 0), sessions=SESSIONS)


def test_earliest_tradable_time_does_not_guess_missing_weekdays() -> None:
    sparse_sessions = (date(2025, 1, 2), date(2025, 1, 6))

    assert earliest_tradable_at(moment(2, 15, 1), sessions=sparse_sessions) == moment(6, 9, 30)


def test_custom_injected_schedule_is_honoured() -> None:
    schedule = AShareSessionSchedule(
        session_open=time(9, 45),
        morning_close=time(11, 15),
        afternoon_open=time(13, 15),
        session_close=time(14, 45),
    )

    assert earliest_tradable_at(moment(2, 12, 0), sessions=SESSIONS, schedule=schedule) == moment(
        2, 13, 15
    )


def test_session_dates_must_be_strictly_increasing_and_real_dates() -> None:
    with pytest.raises(DomainValidationError, match="strictly increasing"):
        earliest_tradable_at(moment(2, 10), sessions=(SESSIONS[0], SESSIONS[0]))
    with pytest.raises(DomainValidationError, match="sessions must contain only dates"):
        earliest_tradable_at(
            moment(2, 10),
            sessions=(date(2025, 1, 2), datetime(2025, 1, 3, tzinfo=SHANGHAI)),
        )


def test_fixed_offset_plus_eight_is_accepted_for_asia_shanghai() -> None:
    fixed_plus_eight = timezone(timedelta(hours=8))
    point_in_time = availability(observed_at=datetime(2025, 1, 2, 14, 0, tzinfo=fixed_plus_eight))

    assert point_in_time.observed_at == datetime(2025, 1, 2, 14, 0, tzinfo=fixed_plus_eight)
