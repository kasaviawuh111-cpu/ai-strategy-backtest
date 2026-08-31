"""Typed handling for HTTP idempotency metadata."""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import Header, Response

from .errors import ApiProblem

IdempotencyKey = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    ),
]

_SAFE_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def validate_idempotency_key(value: str | None) -> None:
    if value is not None and _SAFE_IDEMPOTENCY_KEY.fullmatch(value) is None:
        raise ApiProblem(
            status_code=422,
            code="invalid_idempotency_key",
            message="Idempotency-Key contains unsupported characters",
        )


def set_idempotency_replayed(response: Response, *, replayed: bool) -> None:
    response.headers["Idempotency-Replayed"] = "true" if replayed else "false"
