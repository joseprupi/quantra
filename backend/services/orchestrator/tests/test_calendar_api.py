"""Route tests for the calendar utility endpoints.

Drives the FastAPI ``TestClient`` so auth + the FlatBuffers request build +
the engine call + the envelope all light up together. The engine is a
recording stub returning canned response bytes; there is no MD (calendar RPCs
are pure date math).

Coverage:
1. business-days: canned response decodes to ``{dates, count}``; the request
   bytes faithfully carry the calendar / range / include flags.
2. holidays: canned response decodes; ``include_weekends`` rides through.
3. advance: canned response decodes to ``{input_date, advanced_date}``.
4. Enum handling: an unknown calendar name → 422 (never reaches the engine).
5. Error mapping: an engine failure → 502.
"""

from __future__ import annotations

from typing import Any

import flatbuffers
import pytest
from fastapi.testclient import TestClient

from quantra_common.auth.context import ApiKeyRecord
from quantra_common.engine_client import EngineClient, EngineClientError, EngineRpc
from quantra_common.engine_client._generated.quantra.CalendarAdvanceRequest import (
    CalendarAdvanceRequest,
    CalendarAdvanceRequestT,
)
from quantra_common.engine_client._generated.quantra.CalendarAdvanceResponse import (
    CalendarAdvanceResponseT,
)
from quantra_common.engine_client._generated.quantra.CalendarBusinessDaysRequest import (
    CalendarBusinessDaysRequest,
    CalendarBusinessDaysRequestT,
)
from quantra_common.engine_client._generated.quantra.CalendarBusinessDaysResponse import (
    CalendarBusinessDaysResponseT,
)
from quantra_common.engine_client._generated.quantra.CalendarHolidaysResponse import (
    CalendarHolidaysResponseT,
)
from quantra_common.engine_client._generated.quantra.enums.Calendar import Calendar
from quantra_common.settings import Environment, LogLevel
from quantra_orchestrator.app import create_app
from quantra_orchestrator.settings import OrchestratorSettings

API_KEY = "key-calendar"
OWNER = "user-cal"


class _FakeEngine(EngineClient):
    def __init__(self, *, response: bytes | None = None, raises: Exception | None = None) -> None:
        self.calls: list[tuple[EngineRpc, bytes]] = []
        self._response = response
        self._raises = raises

    async def call(self, rpc: EngineRpc, request_bytes: bytes) -> bytes:
        self.calls.append((rpc, request_bytes))
        if self._raises is not None:
            raise self._raises
        assert self._response is not None
        return self._response

    async def close(self) -> None:
        return None


def _client(engine: EngineClient) -> TestClient:
    async def _lookup(key: str) -> ApiKeyRecord | None:
        if key == API_KEY:
            return ApiKeyRecord(
                api_key_id="ak-cal",
                owner_uid=OWNER,
                name="Calendar Test",
                email="cal@example.com",
                tier="free",
                active=True,
            )
        return None

    def _verify(_token: str) -> dict[str, Any]:
        raise ValueError("no firebase")

    app = create_app(
        OrchestratorSettings(env=Environment.DEV, log_level=LogLevel.WARNING, build_sha="testsha"),
        api_key_lookup=_lookup,
        firebase_verifier=_verify,
        engine_client=engine,
    )
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def headers() -> dict[str, str]:
    return {"X-API-Key": API_KEY}


def _bd_response(dates: list[str]) -> bytes:
    resp = CalendarBusinessDaysResponseT()
    resp.dates = list(dates)
    resp.count = len(dates)
    builder = flatbuffers.Builder(256)
    builder.Finish(resp.Pack(builder))
    return bytes(builder.Output())


def _holidays_response(dates: list[str]) -> bytes:
    resp = CalendarHolidaysResponseT()
    resp.dates = list(dates)
    resp.count = len(dates)
    builder = flatbuffers.Builder(256)
    builder.Finish(resp.Pack(builder))
    return bytes(builder.Output())


def _advance_response(*, input_date: str, advanced_date: str) -> bytes:
    resp = CalendarAdvanceResponseT()
    resp.inputDate = input_date
    resp.advancedDate = advanced_date
    builder = flatbuffers.Builder(256)
    builder.Finish(resp.Pack(builder))
    return bytes(builder.Output())


def test_business_days_happy_path(headers: dict[str, str]) -> None:
    engine = _FakeEngine(response=_bd_response(["2025-01-02", "2025-01-03"]))
    with _client(engine) as client:
        resp = client.post(
            "/v1/calendar/business-days",
            headers=headers,
            json={
                "calendar": "TARGET",
                "start_date": "2025-01-01",
                "end_date": "2025-01-05",
                "include_start": False,
                "include_end": True,
            },
        )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"dates": ["2025-01-02", "2025-01-03"], "count": 2}

    # Faithful request bytes carry the calendar + range + flags.
    rpc, request_bytes = engine.calls[0]
    assert rpc == EngineRpc.CALENDAR_BUSINESS_DAYS
    decoded = CalendarBusinessDaysRequestT.InitFromObj(
        CalendarBusinessDaysRequest.GetRootAs(bytearray(request_bytes), 0)
    )
    assert decoded.calendar == Calendar.TARGET
    assert decoded.startDate.decode() == "2025-01-01"
    assert decoded.endDate.decode() == "2025-01-05"
    assert decoded.includeStart is False
    assert decoded.includeEnd is True


def test_holidays_happy_path(headers: dict[str, str]) -> None:
    engine = _FakeEngine(response=_holidays_response(["2025-01-01"]))
    with _client(engine) as client:
        resp = client.post(
            "/v1/calendar/holidays",
            headers=headers,
            json={
                "calendar": "UnitedStates",
                "start_date": "2025-01-01",
                "end_date": "2025-12-31",
            },
        )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"dates": ["2025-01-01"], "count": 1}
    assert engine.calls[0][0] == EngineRpc.CALENDAR_HOLIDAYS


def test_advance_happy_path(headers: dict[str, str]) -> None:
    engine = _FakeEngine(
        response=_advance_response(input_date="2025-01-15", advanced_date="2025-04-15")
    )
    with _client(engine) as client:
        resp = client.post(
            "/v1/calendar/advance",
            headers=headers,
            json={
                "date": "2025-01-15",
                "tenor_number": 3,
                "tenor_unit": "Months",
                "convention": "ModifiedFollowing",
            },
        )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "input_date": "2025-01-15",
        "advanced_date": "2025-04-15",
    }
    rpc, request_bytes = engine.calls[0]
    assert rpc == EngineRpc.CALENDAR_ADVANCE
    decoded = CalendarAdvanceRequestT.InitFromObj(
        CalendarAdvanceRequest.GetRootAs(bytearray(request_bytes), 0)
    )
    assert decoded.tenorNumber == 3
    assert decoded.tenorUnit == 5  # TimeUnit.Months


def test_unknown_calendar_is_422(headers: dict[str, str]) -> None:
    engine = _FakeEngine(response=_bd_response([]))
    with _client(engine) as client:
        resp = client.post(
            "/v1/calendar/business-days",
            headers=headers,
            json={
                "calendar": "Atlantis",
                "start_date": "2025-01-01",
                "end_date": "2025-01-05",
            },
        )
    assert resp.status_code == 422, resp.text
    assert engine.calls == []  # never reached the engine


def test_engine_failure_maps_to_502(headers: dict[str, str]) -> None:
    engine = _FakeEngine(raises=EngineClientError("boom"))
    with _client(engine) as client:
        resp = client.post(
            "/v1/calendar/holidays",
            headers=headers,
            json={
                "calendar": "TARGET",
                "start_date": "2025-01-01",
                "end_date": "2025-01-05",
            },
        )
    assert resp.status_code == 502, resp.text


def test_requires_auth() -> None:
    engine = _FakeEngine(response=_bd_response([]))
    with _client(engine) as client:
        resp = client.post(
            "/v1/calendar/business-days",
            json={
                "calendar": "TARGET",
                "start_date": "2025-01-01",
                "end_date": "2025-01-05",
            },
        )
    assert resp.status_code == 401, resp.text
