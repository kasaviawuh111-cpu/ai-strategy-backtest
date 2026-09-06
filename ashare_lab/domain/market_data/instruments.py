"""Canonical syntactic identity rules for mainland listed shares."""

from __future__ import annotations

import re

from ashare_lab.domain.shared import InstrumentId

_A_SHARE_CODE_TO_SUFFIX = (
    (re.compile(r"^(?:600|601|603|605|688|689)\d{3}$"), "SH"),
    (re.compile(r"^(?:000|001|002|003|300|301)\d{3}$"), "SZ"),
    # Exchange-approved replacement code for 中航成飞, verified in its SZSE
    # issuer filings. Do not widen the entire 302xxx range or rewrite old history.
    (re.compile(r"^302132$"), "SZ"),
    (re.compile(r"^(?:[48]\d{5}|920\d{3})$"), "BJ"),
)


class AshareInstrumentCodeError(ValueError):
    """Raised when a symbol cannot be proven to be a syntactic A-share id."""


def normalize_a_share_instrument(value: InstrumentId | str) -> InstrumentId:
    """Return ``<six digits>.<SH|SZ|BJ>`` and reject suffix/code conflicts.

    This is deliberately a strict syntactic gate, not a security master.  A
    data snapshot must still prove that the instrument existed and was
    tradable during the requested point-in-time range.
    """

    raw = str(value).strip().upper()
    parts = raw.split(".")
    if len(parts) == 1:
        code = parts[0]
        suffix = None
    elif len(parts) == 2:
        code, suffix = parts
    else:
        raise AshareInstrumentCodeError(f"invalid instrument code: {raw!r}")
    if len(code) != 6 or not code.isdigit():
        raise AshareInstrumentCodeError(f"instrument code must contain exactly six digits: {raw!r}")

    expected_suffix = next(
        (candidate for pattern, candidate in _A_SHARE_CODE_TO_SUFFIX if pattern.fullmatch(code)),
        None,
    )
    if expected_suffix is None:
        raise AshareInstrumentCodeError(
            f"instrument code is outside the supported mainland share spaces: {code!r}"
        )
    if suffix is not None and suffix not in {"SH", "SZ", "BJ"}:
        raise AshareInstrumentCodeError(f"unsupported exchange suffix: {suffix!r}")
    if suffix is not None and suffix != expected_suffix:
        raise AshareInstrumentCodeError(
            f"instrument suffix {suffix!r} conflicts with code {code!r} "
            f"(expected {expected_suffix!r})"
        )
    return InstrumentId(f"{code}.{expected_suffix}")
