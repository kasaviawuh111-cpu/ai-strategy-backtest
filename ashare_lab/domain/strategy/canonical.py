"""Deterministic JSON serialization and content hashing for versioned inputs."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from math import isfinite
from typing import Any, cast

from pydantic import BaseModel


def canonical_json(value: Any) -> str:
    """Return compact, key-sorted UTF-8 JSON with normalized scalar values."""

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", by_alias=True, exclude_none=False)
    normalized = _normalize(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def canonical_hash(value: Any) -> str:
    payload = canonical_json(value).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _normalize(value: Any) -> Any:
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError("canonical JSON does not permit non-finite numbers")
        return int(value) if value.is_integer() else value
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return {
            unicodedata.normalize("NFC", str(key)): _normalize(item)
            for key, item in mapping.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        sequence = cast(Sequence[object], value)
        return [_normalize(item) for item in sequence]
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")
