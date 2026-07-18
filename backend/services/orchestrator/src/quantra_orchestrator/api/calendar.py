"""Calendar utility routes — thin forwarders over the engine's
calendar RPCs.

Three owner-agnostic ``/v1/calendar/*`` endpoints that let the portal's
Calendar feature (business-day counting, holiday listing, date advancing)
run through the orchestrator instead of the retired legacy cloud API. Each
is a near pass-through: parse the JSON body into the engine's FlatBuffers
request, call the RPC, decode the response into JSON. No market data,
curve, or quote resolution is involved (these are pure calendar-math RPCs),
so — unlike the pricing routes — there is no MD walker and no ``Pricing``
graph.

* ``POST /v1/calendar/business-days`` → engine ``CalendarBusinessDays``
  — the business days in ``[start_date, end_date]`` for a calendar.
* ``POST /v1/calendar/holidays``      → engine ``CalendarHolidays``
  — the holidays in ``[start_date, end_date]`` for a calendar.
* ``POST /v1/calendar/advance``       → engine ``CalendarAdvance``
  — advance one date by a tenor under a business-day convention.

Auth: the standard ``get_auth_context`` dependency (dev-bypass-aware).
Engine failures map through the shared error envelope
(:func:`map_engine_client_error`). Enum-valued fields (``calendar`` /
``tenor_unit`` / ``convention``) accept the QuantLib member name (e.g.
``"TARGET"``, ``"Days"``, ``"Following"``) or the raw integer; an unknown
name is a clean 422, never an opaque engine crash.
"""

from __future__ import annotations

from typing import Any, Protocol

import flatbuffers
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from quantra_common.auth.context import AuthContext
from quantra_common.engine_client import EngineClient, EngineClientError, EngineRpc
from quantra_common.engine_client._generated.quantra.CalendarAdvanceRequest import (
    CalendarAdvanceRequestT,
)
from quantra_common.engine_client._generated.quantra.CalendarAdvanceResponse import (
    CalendarAdvanceResponse,
    CalendarAdvanceResponseT,
)
from quantra_common.engine_client._generated.quantra.CalendarBusinessDaysRequest import (
    CalendarBusinessDaysRequestT,
)
from quantra_common.engine_client._generated.quantra.CalendarBusinessDaysResponse import (
    CalendarBusinessDaysResponse,
    CalendarBusinessDaysResponseT,
)
from quantra_common.engine_client._generated.quantra.CalendarHolidaysRequest import (
    CalendarHolidaysRequestT,
)
from quantra_common.engine_client._generated.quantra.CalendarHolidaysResponse import (
    CalendarHolidaysResponse,
    CalendarHolidaysResponseT,
)
from quantra_common.engine_client._generated.quantra.enums.BusinessDayConvention import (
    BusinessDayConvention,
)
from quantra_common.engine_client._generated.quantra.enums.Calendar import Calendar
from quantra_common.engine_client._generated.quantra.enums.TimeUnit import TimeUnit
from quantra_orchestrator.auth.dependencies import get_auth_context
from quantra_orchestrator.engine import get_engine_client, map_engine_client_error

router = APIRouter(prefix="/v1/calendar", tags=["calendar"])


class _Packable(Protocol):
    """A generated FlatBuffers ``*T`` object able to pack itself into a builder."""

    def Pack(self, builder: flatbuffers.Builder) -> int: ...


def _enum_value(enum_cls: type, raw: object, *, field: str) -> int:
    """Resolve a QuantLib enum member name **or** raw int to its value.

    Accepts the member name (``"TARGET"``) or an already-numeric value; an
    unknown name / out-of-range int surfaces as an actionable 422 rather than
    reaching the engine as a bad enum.
    """

    if isinstance(raw, bool):  # bool is an int subclass — reject explicitly
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{field}: expected an enum name or integer, got a boolean.",
        )
    if isinstance(raw, int):
        if raw in {int(m) for m in enum_cls.__dict__.values() if isinstance(m, int)}:
            return raw
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{field}: {raw} is not a valid {enum_cls.__name__} value.",
        )
    if isinstance(raw, str):
        member = getattr(enum_cls, raw, None)
        if isinstance(member, int):
            return int(member)
    valid = sorted(name for name, val in vars(enum_cls).items() if isinstance(val, int))
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=f"{field}: unknown {enum_cls.__name__} {raw!r}. Valid: {valid[:20]}",
    )


def _decode_str(value: object) -> object:
    return value.decode() if isinstance(value, (bytes, bytearray)) else value


class CalendarRangeRequest(BaseModel):
    """Shared shape for the business-days / holidays range queries."""

    calendar: str | int = Field(
        default="TARGET",
        description="QuantLib calendar member name (e.g. 'TARGET', 'UnitedStates') or int.",
    )
    start_date: str = Field(description="Inclusive range start, YYYY-MM-DD.")
    end_date: str = Field(description="Inclusive range end, YYYY-MM-DD.")


class BusinessDaysRequest(CalendarRangeRequest):
    include_start: bool = True
    include_end: bool = True


class HolidaysRequest(CalendarRangeRequest):
    include_weekends: bool = False


class CalendarAdvanceRequest(BaseModel):
    date: str = Field(description="The date to advance, YYYY-MM-DD.")
    tenor_number: int = Field(description="Number of ``tenor_unit`` periods to advance.")
    tenor_unit: str | int = Field(
        default="Days",
        description="QuantLib TimeUnit member name (e.g. 'Days', 'Months', 'Years') or int.",
    )
    convention: str | int = Field(
        default="Following",
        description="Business-day convention member name (e.g. 'Following') or int.",
    )


_ENGINE_ERR_RESPONSES: dict[int | str, dict[str, str]] = {
    401: {"description": "Missing or invalid credentials."},
    422: {"description": "Invalid calendar / tenor-unit / convention or dates."},
    502: {"description": "Engine failure."},
    503: {"description": "Engine unavailable."},
}


@router.post(
    "/business-days",
    summary="Count / list the business days in a date range for a calendar",
    responses=_ENGINE_ERR_RESPONSES,
)
async def calendar_business_days(
    payload: BusinessDaysRequest,
    _ctx: AuthContext = Depends(get_auth_context),
    engine_client: EngineClient = Depends(get_engine_client),
) -> dict[str, Any]:
    request = CalendarBusinessDaysRequestT()
    request.calendar = _enum_value(Calendar, payload.calendar, field="calendar")
    request.startDate = payload.start_date
    request.endDate = payload.end_date
    request.includeStart = payload.include_start
    request.includeEnd = payload.include_end

    response_bytes = await _call(engine_client, EngineRpc.CALENDAR_BUSINESS_DAYS, request)
    decoded = CalendarBusinessDaysResponseT.InitFromObj(
        CalendarBusinessDaysResponse.GetRootAs(bytearray(response_bytes), 0)
    )
    return {
        "dates": [_decode_str(d) for d in (decoded.dates or [])],
        "count": int(decoded.count),
    }


@router.post(
    "/holidays",
    summary="List the holidays in a date range for a calendar",
    responses=_ENGINE_ERR_RESPONSES,
)
async def calendar_holidays(
    payload: HolidaysRequest,
    _ctx: AuthContext = Depends(get_auth_context),
    engine_client: EngineClient = Depends(get_engine_client),
) -> dict[str, Any]:
    request = CalendarHolidaysRequestT()
    request.calendar = _enum_value(Calendar, payload.calendar, field="calendar")
    request.startDate = payload.start_date
    request.endDate = payload.end_date
    request.includeWeekends = payload.include_weekends

    response_bytes = await _call(engine_client, EngineRpc.CALENDAR_HOLIDAYS, request)
    decoded = CalendarHolidaysResponseT.InitFromObj(
        CalendarHolidaysResponse.GetRootAs(bytearray(response_bytes), 0)
    )
    return {
        "dates": [_decode_str(d) for d in (decoded.dates or [])],
        "count": int(decoded.count),
    }


@router.post(
    "/advance",
    summary="Advance a date by a tenor under a business-day convention",
    responses=_ENGINE_ERR_RESPONSES,
)
async def calendar_advance(
    payload: CalendarAdvanceRequest,
    _ctx: AuthContext = Depends(get_auth_context),
    engine_client: EngineClient = Depends(get_engine_client),
) -> dict[str, Any]:
    request = CalendarAdvanceRequestT()
    request.date = payload.date
    request.tenorNumber = payload.tenor_number
    request.tenorUnit = _enum_value(TimeUnit, payload.tenor_unit, field="tenor_unit")
    request.convention = _enum_value(BusinessDayConvention, payload.convention, field="convention")

    response_bytes = await _call(engine_client, EngineRpc.CALENDAR_ADVANCE, request)
    decoded = CalendarAdvanceResponseT.InitFromObj(
        CalendarAdvanceResponse.GetRootAs(bytearray(response_bytes), 0)
    )
    return {
        "input_date": _decode_str(decoded.inputDate),
        "advanced_date": _decode_str(decoded.advancedDate),
    }


async def _call(engine_client: EngineClient, rpc: EngineRpc, request: _Packable) -> bytes:
    """Pack ``request`` and invoke ``rpc``, mapping engine failures to the error envelope."""

    builder = flatbuffers.Builder(512)
    # ForceDefaults: the engine requires every set field explicit on the wire;
    # zero-default enums/scalars (e.g. Frequency.Annual == 0) must not be omitted.
    builder.ForceDefaults(True)
    builder.Finish(request.Pack(builder))
    try:
        return await engine_client.call(rpc, bytes(builder.Output()))
    except EngineClientError as exc:
        raise map_engine_client_error(exc) from exc


__all__ = ["router"]
