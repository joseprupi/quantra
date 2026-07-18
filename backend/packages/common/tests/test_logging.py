"""Tests for `quantra_common.logging`."""

from __future__ import annotations

import io
import json
import logging
from typing import Any

import pytest
import structlog
from starlette.types import Message, Receive, Scope, Send

from quantra_common.logging import RequestIdMiddleware, configure_logging, get_logger
from quantra_common.settings.base import Environment, LogLevel, Settings


@pytest.fixture(autouse=True)
def _reset_structlog() -> None:
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {"env": Environment.PROD, "log_level": LogLevel.INFO}
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


def test_configure_logging_emits_json_in_prod(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(_settings(env=Environment.PROD))
    log = get_logger("test")

    log.info("hello", user="alice")

    err = capsys.readouterr().err.strip()
    assert err, "expected a log line on stderr"
    payload = json.loads(err.splitlines()[-1])
    assert payload["event"] == "hello"
    assert payload["user"] == "alice"
    assert payload["env"] == "prod"
    assert payload["level"] == "info"
    assert "timestamp" in payload


def test_configure_logging_pretty_in_dev(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(_settings(env=Environment.DEV))
    log = get_logger("test")

    log.info("dev-mode", thing=1)

    err = capsys.readouterr().err
    assert "dev-mode" in err
    with pytest.raises(json.JSONDecodeError):
        json.loads(err.splitlines()[-1])


def test_log_level_filter_drops_below_threshold(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(_settings(log_level=LogLevel.WARNING))
    log = get_logger("test")

    log.info("dropped")
    log.warning("kept")

    err = capsys.readouterr().err
    assert "dropped" not in err
    assert "kept" in err


def test_invalid_log_level_in_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "LOUD")
    with pytest.raises(ValueError, match="log_level"):
        Settings(_env_file=None)  # type: ignore[call-arg]


# ----- middleware -----------------------------------------------------------


async def _identity_app(scope: Scope, receive: Receive, send: Send) -> None:
    structlog.get_logger("app").info("inside")
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain")],
        }
    )
    await send({"type": "http.response.body", "body": b"ok"})


def _make_scope(headers: list[tuple[bytes, bytes]]) -> Scope:
    return {
        "type": "http",
        "method": "GET",
        "path": "/x",
        "headers": headers,
    }


@pytest.mark.asyncio
async def test_middleware_propagates_inbound_request_id() -> None:
    captured: list[Message] = []

    middleware = RequestIdMiddleware(
        _identity_app,
        settings=_settings(),
    )

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        captured.append(message)

    scope = _make_scope([(b"x-request-id", b"abc-123")])

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
    )
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)

    await middleware(scope, receive, send)

    start = next(m for m in captured if m["type"] == "http.response.start")
    assert (b"x-request-id", b"abc-123") in start["headers"]

    payload = json.loads(buf.getvalue().strip().splitlines()[-1])
    assert payload["request_id"] == "abc-123"
    assert payload["event"] == "inside"


@pytest.mark.asyncio
async def test_middleware_generates_id_when_missing() -> None:
    captured: list[Message] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = RequestIdMiddleware(app, header_name="X-Request-Id")

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        captured.append(message)

    await middleware(_make_scope([]), receive, send)

    start = next(m for m in captured if m["type"] == "http.response.start")
    headers = dict(start["headers"])
    generated = headers[b"x-request-id"].decode("latin-1")
    assert len(generated) == 32  # uuid4().hex


@pytest.mark.asyncio
async def test_middleware_passes_through_non_http_scope() -> None:
    seen: list[Scope] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope)

    middleware = RequestIdMiddleware(app)

    async def receive() -> Message:
        return {"type": "lifespan.startup"}

    async def send(_msg: Message) -> None:
        return None

    await middleware({"type": "lifespan"}, receive, send)
    assert seen == [{"type": "lifespan"}]
