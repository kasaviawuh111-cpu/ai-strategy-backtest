"""Uniform, non-leaky API error handling."""

from __future__ import annotations

import logging
from datetime import date
from collections.abc import Sequence

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ashare_lab.adapters.language.vibe_candidates import CandidateTransportError

from .schemas import ErrorBody, ErrorDetail, ErrorEnvelope

LOGGER = logging.getLogger(__name__)


class ApiProblem(Exception):
    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        details: Sequence[ErrorDetail] = (),
        available_start: date | None = None,
        available_end: date | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = tuple(details)
        # Internal typed bounds; never infer machine actions from display prose.
        self.available_start = available_start
        self.available_end = available_end


def request_id(request: Request) -> str:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) and value else "unavailable"


def error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    request_id_value: str,
    details: Sequence[ErrorDetail] = (),
) -> JSONResponse:
    payload = ErrorEnvelope(
        error=ErrorBody(code=code, message=message, details=tuple(details)),
        request_id=request_id_value,
    )
    return JSONResponse(
        status_code=status_code,
        content=payload.model_dump(mode="json"),
        headers={"X-Request-ID": request_id_value},
    )


def install_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiProblem, _handle_api_problem)
    app.add_exception_handler(CandidateTransportError, _handle_candidate_provider_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_error)
    app.add_exception_handler(Exception, _handle_unexpected_error)


async def _handle_api_problem(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, ApiProblem):
        raise TypeError("ApiProblem handler received an incompatible exception")
    return error_response(
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        details=exc.details,
        request_id_value=request_id(request),
    )


async def _handle_candidate_provider_error(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, CandidateTransportError):
        raise TypeError("candidate handler received an incompatible exception")
    if not exc.is_classified:
        return await _handle_unexpected_error(request, exc)
    LOGGER.warning(
        "model_provider_failed reason=%s http_status=%s",
        exc.failure_kind, exc.http_status,
    )
    return await _handle_api_problem(request, ApiProblem(
        status_code=exc.api_status_code, code=exc.public_code, message=exc.public_message,
        details=() if exc.http_status is None else (ErrorDetail(
            location="model_provider.http_status", message=str(exc.http_status),
            type="upstream_http_status",
        ),),
    ))


async def _handle_validation_error(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    if not isinstance(exc, RequestValidationError):
        raise TypeError("validation handler received an incompatible exception")
    details = tuple(
        ErrorDetail(
            location=".".join(str(part) for part in item.get("loc", ())),
            message=str(item.get("msg", "Invalid value")),
            type=str(item.get("type", "validation_error")),
        )
        for item in exc.errors()
    )
    return error_response(
        status_code=422,
        code="request_validation_failed",
        message="Request validation failed",
        details=details,
        request_id_value=request_id(request),
    )


async def _handle_http_error(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    if not isinstance(exc, StarletteHTTPException):
        raise TypeError("HTTP handler received an incompatible exception")
    return error_response(
        status_code=exc.status_code,
        code=f"http_{exc.status_code}",
        message=exc.detail,
        request_id_value=request_id(request),
    )


async def _handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    LOGGER.exception("Unhandled API error", exc_info=exc)
    return error_response(
        status_code=500,
        code="internal_error",
        message="Internal server error",
        request_id_value=request_id(request),
    )
