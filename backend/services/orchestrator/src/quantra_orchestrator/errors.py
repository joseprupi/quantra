"""Global exception handlers + structured error contract.

The orchestrator's external surface returns the same JSON envelope for
every error path: ``{error, code, request_id}`` (with an optional
``details`` field). This module centralises both the envelope and the
three handlers wired into the FastAPI app by ``register_exception_handlers``:

1. ``RequestValidationError`` (FastAPI / pydantic input validation)
2. ``StarletteHTTPException`` (anything raised via ``HTTPException``)
3. ``Exception`` (catch-all -- defends against an HTML traceback leak)

The catch-all handler is the one acceptance criterion in
 that demands a JSON response on
unhandled errors.

Request-ID resolution preference (highest first):

1. The inbound header (configured by ``Settings.request_id_header``).
2. ``structlog.contextvars`` -- where ``RequestIdMiddleware`` binds it.

We try the header first because the catch-all path can be invoked from a
context where the contextvars binding has already been torn down (the
``finally`` in the ASGI middleware unbinds before the surrounding
ServerErrorMiddleware reaches our handler). Reading the header keeps the
echo guarantee while structlog still owns the binding for in-request
log lines.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from http import HTTPStatus
from typing import Any, cast

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from quantra_common.settings import Settings
from quantra_orchestrator.schemas import ErrorResponse

_log = structlog.get_logger(__name__)

_HandlerSig = Callable[[Request, Exception], Awaitable[JSONResponse]]


def _current_request_id_from_contextvars() -> str | None:
    ctx = structlog.contextvars.get_contextvars()
    value = ctx.get("request_id")
    return value if isinstance(value, str) else None


def _resolve_request_id(request: Request, header_name: str) -> str | None:
    inbound = request.headers.get(header_name)
    if inbound:
        return inbound
    return _current_request_id_from_contextvars()


def _envelope(
    *,
    error: str,
    code: str,
    request_id: str | None,
    details: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload = ErrorResponse(
        error=error,
        code=code,
        request_id=request_id,
        details=details,
    )
    return payload.model_dump(exclude_none=True)


def _sanitise_validation_error(err: dict[str, Any]) -> dict[str, Any]:
    """Strip un-JSON-serialisable values from one pydantic v2 error entry.

    pydantic v2 may attach a raw ``ValueError`` to ``err["ctx"]["error"]``
    when an ``@model_validator`` raises. The stdlib JSON encoder downstream
    chokes on that, so we replace any non-stdlib value under ``ctx`` with
    its ``str(...)`` form. Everything else (``loc``, ``msg``, ``type``,
    ``input``) is already JSON-friendly because pydantic gates it.
    """

    out = dict(err)
    raw_ctx = out.get("ctx")
    if isinstance(raw_ctx, dict):
        out["ctx"] = {
            key: (value if isinstance(value, (str, int, float, bool, type(None))) else str(value))
            for key, value in raw_ctx.items()
        }
    return out


def _make_validation_handler(header_name: str) -> _HandlerSig:
    async def _handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, RequestValidationError), exc  # nosec B101
        details = [_sanitise_validation_error(err) for err in exc.errors()]
        return JSONResponse(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            content=_envelope(
                error="Request validation failed.",
                code="validation_error",
                request_id=_resolve_request_id(request, header_name),
                details=details,
            ),
        )

    return _handle_validation_error


def _coerce_details(value: Any) -> list[dict[str, Any]] | None:  # noqa: ANN401 -- runtime branch
    """Validate an optional ``details`` attribute on an HTTPException.

    Accepted shapes:

    * ``None`` / missing → omit ``details`` from the envelope.
    * ``list[dict[str, Any]]`` → forward verbatim.

    Anything else is dropped silently — the envelope contract treats
    ``details`` as best-effort context, not a structural promise.
    """

    if value is None:
        return None
    if isinstance(value, list) and all(isinstance(item, dict) for item in value):
        return [cast(dict[str, Any], item) for item in value]
    return None


def _make_http_handler(header_name: str) -> _HandlerSig:
    async def _handle_http_exception(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, StarletteHTTPException), exc  # nosec B101
        status = exc.status_code
        detail = exc.detail
        message = detail if isinstance(detail, str) and detail else HTTPStatus(status).phrase
        headers = exc.headers or None
        # ``HTTPException`` subclasses may pin a stable ``code`` token
        # via a class attribute (see e.g. ``UnauthenticatedError`` in
        # ``quantra_orchestrator.auth.dependencies``). Fall back to the
        # generic ``http_<status>`` token when none is provided so legacy
        # call sites keep working unchanged.
        custom_code = getattr(exc, "code", None)
        code = custom_code if isinstance(custom_code, str) and custom_code else f"http_{status}"
        # ``details`` is symmetric with ``code``: per-product routes
        # attach structured context (e.g. the
        # assembled engine request) so an operator inspecting the structured-error
        # envelope can see which stage the request reached before
        # failing. Routes set it as an instance attribute on the
        # raised exception; the contract on the envelope side stays
        # ``list[dict[str, Any]]`` so the OpenAPI schema doesn't have
        # to widen.
        details = _coerce_details(getattr(exc, "details", None))
        return JSONResponse(
            status_code=status,
            content=_envelope(
                error=message,
                code=code,
                request_id=_resolve_request_id(request, header_name),
                details=details,
            ),
            headers=headers,
        )

    return _handle_http_exception


def _make_unexpected_handler(header_name: str) -> _HandlerSig:
    async def _handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        request_id = _resolve_request_id(request, header_name)
        # Re-bind so the log line is tagged even if the surrounding
        # middleware has already torn down its own contextvars binding.
        structlog.contextvars.bind_contextvars(request_id=request_id)
        _log.exception(
            "orchestrator.unhandled_exception",
            method=request.method,
            path=request.url.path,
            exc_type=type(exc).__name__,
        )
        return JSONResponse(
            status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
            content=_envelope(
                error="Internal server error.",
                code="internal_error",
                request_id=request_id,
            ),
        )

    return _handle_unexpected_error


def register_exception_handlers(app: FastAPI, *, settings: Settings) -> None:
    """Install the three structured-envelope handlers on ``app``.

    ``settings`` is captured so the handlers can read the configured
    request-id header name without re-importing the settings model on
    every request.
    """

    header_name = settings.request_id_header
    app.add_exception_handler(RequestValidationError, _make_validation_handler(header_name))
    app.add_exception_handler(StarletteHTTPException, _make_http_handler(header_name))
    app.add_exception_handler(Exception, _make_unexpected_handler(header_name))


__all__ = ["register_exception_handlers"]
