"""structured-error HTTPExceptions for engine-client failure paths.

Five orchestrator-side error families translate the
:class:`quantra_common.engine_client.EngineClientError` subclasses (and
the stub's ``NotImplementedError``) into stable envelope tokens:

* ``EngineUnavailableError`` — 502 with ``code="engine_unavailable"``
  for the stub backend (``StubEngineClient.call`` raises
  ``NotImplementedError`` by design). The mapping converts the stub's
  signal that "the real engine is not wired yet" into a envelope
  rather than letting it surface as an opaque 500.
* ``EngineUnreachableError`` — 502 with ``code="engine_unreachable"``
  for transport / connection failures (``OSError``,
  ``ConnectionError``) that occur before the gRPC layer can wrap them
  in an ``AioRpcError``. Distinct from ``engine_timeout`` so clients
  can branch on "the channel never came up" vs "the engine answered
  slowly".
* ``EngineTimeoutMappedError`` — 504 with ``code="engine_timeout"`` for
  gRPC ``UNAVAILABLE`` / ``DEADLINE_EXCEEDED``. The wire-layer
  translator surfaces these as
  :class:`quantra_common.engine_client.EngineRetryableError` /
  :class:`quantra_common.engine_client.EngineTimeoutError` so the
  retry decorator can treat them as transient.
* ``EngineInvalidRequestError`` — 400 with
  ``code="engine_invalid_request"`` for gRPC ``INVALID_ARGUMENT``. The
  caller built a request the engine rejected outright; retrying would
  not help and would mask the bug.
* ``EngineUpstreamError`` — 502 with ``code="engine_upstream_error"``
  for any other engine-side error (gRPC ``INTERNAL``,
  ``UNAUTHENTICATED``, etc.). The engine answered with a non-success
  status that isn't classified above.

Two engine aborts are additionally detected by MESSAGE TEXT (any gRPC
status) and translated into actionable 422s instead of an opaque 502:
a QuantLib ``Missing <index> fixing for <date>`` →
``missing_required_fixing`` (:class:`EngineMissingFixingError`), and a
QuantLib ``negative time`` abort → ``pricing_as_of_before_curve_date``
(:class:`EngineDateCoherenceError`, the cross-source As-Of/date
mismatch).

The shared HTTPException handler in ``quantra_orchestrator.errors``
inspects each subclass's ``code`` class attribute, so raising one of
these directly emits the envelope without per-route wiring.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date
from typing import Any, Final, Protocol
from uuid import UUID

from fastapi import HTTPException, status

from quantra_common.engine_client import (
    EngineClientError,
    EngineRetryableError,
    EngineRpcError,
    EngineTimeoutError,
)

ENGINE_UNAVAILABLE_CODE: Final[str] = "engine_unavailable"
ENGINE_UNREACHABLE_CODE: Final[str] = "engine_unreachable"
ENGINE_TIMEOUT_CODE: Final[str] = "engine_timeout"
ENGINE_INVALID_REQUEST_CODE: Final[str] = "engine_invalid_request"
ENGINE_UPSTREAM_ERROR_CODE: Final[str] = "engine_upstream_error"
MISSING_REQUIRED_FIXING_CODE: Final[str] = "missing_required_fixing"
PRICING_AS_OF_BEFORE_CURVE_DATE_CODE: Final[str] = "pricing_as_of_before_curve_date"

_DATE_COHERENCE_GUIDANCE: Final[str] = (
    "Price at the curve's reference date, or re-select an As-Of date on/after "
    "every curve's reference date."
)


class EngineUnavailableError(HTTPException):
    """502 raised when the stub backend signals "real engine not wired yet".

    ``StubEngineClient`` raises ``NotImplementedError`` for every RPC
    by design: the shared library ships without a runtime
    ``grpcio`` dependency, and the real client lives in a follow-up
    plan. Mapping that signal to a stable ``engine_unavailable``
    envelope means the orchestrator's contract stays predictable on
    every deploy that hasn't enabled the gRPC backend (the default
    today).
    """

    code: Final[str] = ENGINE_UNAVAILABLE_CODE

    def __init__(self, *, reason: str | None = None) -> None:
        super().__init__(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=reason
            or (
                "Pricing engine client is the stub (real gRPC backend not "
                "configured). Set ENGINE_GRPC_TARGET to point at the "
                "engine and redeploy."
            ),
        )


class EngineUnreachableError(HTTPException):
    """502 raised when the gRPC channel can't be reached at the OS layer.

    Reserved for transport-level failures that surface *before* gRPC
    can wrap them in an ``AioRpcError`` (DNS resolution, TCP refused
    on a side-channel, ``OSError``). gRPC ``UNAVAILABLE`` is a
    distinct case mapped to ``engine_timeout`` so retries are still
    in scope; this surface is the "the channel never came up" signal.
    """

    code: Final[str] = ENGINE_UNREACHABLE_CODE

    def __init__(self, *, reason: str = "Pricing engine unreachable.") -> None:
        super().__init__(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=reason,
        )


class EngineTimeoutMappedError(HTTPException):
    """504 raised on gRPC ``UNAVAILABLE`` / ``DEADLINE_EXCEEDED``.

    Both codes mean "the engine couldn't answer within the configured
    budget" (the engine is up but not responding, or the call was
    cut off at the deadline). Surfaced as a 504 so the caller knows
    the retry decorator already exhausted its budget — the
    appropriate next action is to back off, not to retry harder.
    """

    code: Final[str] = ENGINE_TIMEOUT_CODE

    def __init__(self, *, reason: str = "Pricing engine timed out.") -> None:
        super().__init__(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=reason,
        )


class EngineInvalidRequestError(HTTPException):
    """400 raised when the engine rejects the request as malformed.

    Mirrors gRPC ``INVALID_ARGUMENT``. Distinct from the orchestrator's
    own ``validation_error`` (422): a 400 here means the orchestrator
    itself accepted the request shape but the engine's stricter
    validation rejected it (e.g. an unknown calendar, an unsupported
    day-count). Surfacing this as 4xx (not 5xx) tells the caller the
    fix is on their side, not the engine's.
    """

    code: Final[str] = ENGINE_INVALID_REQUEST_CODE

    def __init__(self, *, reason: str = "Pricing engine rejected the request.") -> None:
        super().__init__(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=reason,
        )


class EngineUpstreamError(HTTPException):
    """502 raised on any other engine-side error.

    Catch-all for gRPC ``INTERNAL`` / ``UNKNOWN`` / etc. — the engine
    answered, but with a non-success status the orchestrator can't
    classify into one of the more specific buckets. The 502 contract
    matches MD's ``md_upstream_error``: "upstream is alive but
    returning an error".
    """

    code: Final[str] = ENGINE_UPSTREAM_ERROR_CODE

    def __init__(self, *, reason: str = "Pricing engine returned an error.") -> None:
        super().__init__(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=reason,
        )


class EngineMissingFixingError(HTTPException):
    """422 raised when the engine aborts because a required past fixing is absent.

    Pricing a floating-rate instrument whose first (or any) coupon
    period has already started needs the HISTORICAL index fixing for
    that period. When it is missing the engine aborts inside QuantLib
    with a ``Missing <index> fixing for <date>`` message — an opaque
    signal that, left un-translated, surfaces as a bare
    ``engine_upstream_error`` (502) telling the caller nothing
    actionable (operator error-quality mandate / north-star invariant
    #9: errors must be actionable at every layer).

    This maps that failure onto a stable, product-general error code —
    ``missing_required_fixing`` — carrying ``details`` that name WHICH
    fixing is missing (the index and the fixing date) plus the raw
    ``engine_detail`` for an investigator. It is a 422 (not a 5xx): the
    request is well-formed, but a required market-data input (the past
    fixing) is absent — the caller's next action is to supply the
    fixing (or pin a snapshot that carries it), not to retry.

    Product-general because a missing fixing can arise for any
    floating-rate product (IR swaps, floating-rate bonds, swaptions);
    detection lives at the engine-boundary mapper so every product
    inherits the clean surface without per-route wiring.
    """

    code: Final[str] = MISSING_REQUIRED_FIXING_CODE

    def __init__(
        self,
        *,
        index: str | None,
        fixing_date: str | None,
        engine_detail: str,
    ) -> None:
        if index and fixing_date:
            human = (
                f"Missing required historical fixing for index '{index}' "
                f"on {fixing_date}. Supply this fixing (or pin a snapshot "
                f"that carries it) so the instrument can price."
            )
        else:
            human = (
                "Missing a required historical fixing needed to price this "
                "instrument. Supply the fixing (or pin a snapshot that "
                "carries it) so the instrument can price."
            )
        super().__init__(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=human,
        )
        entry: dict[str, Any] = {
            "index": index,
            "fixing_date": fixing_date,
            "engine_detail": engine_detail,
        }
        self.details: list[dict[str, Any]] = [entry]


class _DatedCurveLike(Protocol):
    """The minimal read surface :meth:`EngineDateCoherenceError.add_date_context` needs."""

    @property
    def id(self) -> UUID | None: ...

    @property
    def name(self) -> str: ...

    @property
    def reference_date(self) -> date | None: ...


class EngineDateCoherenceError(HTTPException):
    """422 raised when the engine aborts with QuantLib's ``negative time`` failure.

    The cross-source As-Of mismatch (error quality, translation only — the
    engine stays the decider): the app's default As-Of can come from one
    public feed (BoE, T-1) while a curve auto-rolls its ``reference_date`` to
    another feed's later date (US Treasury, T+0). Pricing shapes whose math
    runs at the ``as_of`` (e.g. a bond's settlement / accrual / yield) then
    abort deep inside QuantLib with ``negative time (-0.0027...) given``,
    which used to surface as an opaque 502 ``engine_upstream_error`` telling
    the user nothing about dates.

    IMPORTANT: this is deliberately NOT a pre-flight rejection. Many flows
    legitimately price/sample with ``as_of`` earlier than a curve's reference
    date (every cashflow sits beyond the gap — swaptions, vol sampling, T-1
    GBP swaps against a T+0 curve), and the engine accepts them; a blanket
    ``as_of < reference_date`` 422 was live-proven to block green journeys.
    So the engine decides, and this class translates the failure it actually
    raised into the stable, actionable 422 ``pricing_as_of_before_curve_date``.

    A 422 (not 5xx): the request is well-formed but its *date inputs* are
    incoherent — the caller's next action is to re-select the As-Of (or price
    at the curve's reference date), not to retry. The boundary mapper builds
    the generic form (it has no request context); a route that has the
    resolved curves on hand upgrades the message + details via
    :meth:`add_date_context` so the envelope names the as-of and each curve's
    reference date.
    """

    code: Final[str] = PRICING_AS_OF_BEFORE_CURVE_DATE_CODE

    def __init__(self, *, engine_detail: str) -> None:
        super().__init__(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "Pricing engine aborted with a date-coherence error: "
                f"{engine_detail}. This usually means the pricing as-of date "
                "predates a curve's reference date (e.g. a default As-Of from "
                "one market-data source vs a curve auto-rolled to another "
                f"source's later date). {_DATE_COHERENCE_GUIDANCE}"
            ),
        )
        self.engine_detail = engine_detail
        self.details: list[dict[str, Any]] = [
            {"engine_detail": engine_detail, "guidance": _DATE_COHERENCE_GUIDANCE}
        ]

    def add_date_context(self, *, as_of: str, curves: Iterable[_DatedCurveLike]) -> None:
        """Name the request's ``as_of`` + curve reference dates in message + details.

        Called by a route that has the resolved curves on hand (bonds today).
        Curves whose ``reference_date`` is AFTER ``as_of`` are the likely
        culprits and lead the human message; every dated curve lands in
        ``details`` (prepended, so clients read the date context first and
        the raw ``engine_detail`` entry stays last). A curve without a
        reference date is skipped; no curves ⇒ no-op (generic form stands).
        """

        try:
            as_of_date: date | None = date.fromisoformat(str(as_of)[:10])
        except ValueError:
            as_of_date = None
        dated = [(c.name, c.id, c.reference_date) for c in curves if c.reference_date is not None]
        if not dated:
            return
        offending = [entry for entry in dated if as_of_date is not None and as_of_date < entry[2]]
        lead = offending or dated
        first_name, _first_id, first_ref = lead[0]
        if offending:
            human = (
                f"Pricing as-of date {as_of} predates the reference date of "
                f"curve '{first_name}' ({first_ref.isoformat()})"
                + (f" and {len(offending) - 1} more curve(s)" if len(offending) > 1 else "")
                + f". {_DATE_COHERENCE_GUIDANCE} (Engine detail: {self.engine_detail})"
            )
        else:
            human = (
                f"{self.detail} (Request as-of: {as_of}; curve reference date(s): "
                + ", ".join(f"'{name}' {ref.isoformat()}" for name, _cid, ref in dated)
                + ".)"
            )
        self.detail = human
        date_entries: list[dict[str, Any]] = [
            {
                "as_of": as_of,
                "curve": name,
                "curve_id": str(curve_id) if curve_id is not None else None,
                "reference_date": ref.isoformat(),
                "guidance": _DATE_COHERENCE_GUIDANCE,
            }
            for name, curve_id, ref in lead
        ]
        self.details = [*date_entries, *self.details]


# gRPC status codes the wire-layer translator surfaces in
# ``EngineRpcError.code`` — kept as plain strings to avoid importing
# ``grpc`` at module import time. Source of truth:
# ``grpc.StatusCode`` (https://grpc.github.io/grpc/python/grpc.html).
_GRPC_TIMEOUT_CODES: Final[frozenset[str]] = frozenset({"UNAVAILABLE", "DEADLINE_EXCEEDED"})
_GRPC_INVALID_REQUEST_CODES: Final[frozenset[str]] = frozenset({"INVALID_ARGUMENT"})

# QuantLib's ``InterestRateIndex::fixing`` throws
# ``Missing <index name> fixing for <date>`` when a required past fixing is
# absent; the engine surfaces that text in the gRPC error details. The index
# name can contain spaces / slashes (e.g. ``Euribor6M Actual/360``) so the
# index group is non-greedy up to the `` fixing for `` anchor, and the date
# runs to the end of the detail / a closing paren (the wire layer may wrap the
# message as ``... (Missing ... fixing for ...)``). Case-insensitive so a
# re-cased engine message still matches.
_MISSING_FIXING_RE: Final[re.Pattern[str]] = re.compile(
    r"missing\s+(?P<index>.+?)\s+fixing\s+for\s+(?P<date>[^)]+?)\s*[).]?\s*$",
    re.IGNORECASE,
)

# QuantLib aborts with ``negative time (-0.0027...) given`` when asked to
# discount / compound to a date BEFORE a term structure's reference date —
# i.e. the pricing as-of predates a curve's reference date (the cross-source
# As-Of/date-mismatch failure). Referenced/loaded curves are caught up front
# by the orchestrator's typed 422 pre-flight
# (``pricing_as_of_before_curve_date``); this boundary detection is the
# backstop for anything that still reaches the engine (e.g. an inline curve
# path) so the 502 at least tells the caller it is a date-coherence problem,
# not an opaque engine internal. Case-insensitive; the engine may wrap the
# text as ``... ABORTED (negative time (-0.0027) given)``.
_NEGATIVE_TIME_RE: Final[re.Pattern[str]] = re.compile(r"negative\s+time", re.IGNORECASE)


def parse_missing_fixing(text: str) -> tuple[str | None, str | None] | None:
    """Detect the engine's "missing fixing" signal; extract ``(index, date)``.

    Returns ``None`` when ``text`` is not a missing-fixing abort, so the
    caller falls through to the generic engine-error mapping. When it
    matches, the index name and fixing date are returned as strings (or
    ``None`` for a group that didn't capture) so the ``details`` can
    name exactly which fixing is missing. Never raises — a
    non-conforming message simply doesn't match.
    """

    match = _MISSING_FIXING_RE.search(text)
    if match is None:
        return None
    index = (match.group("index") or "").strip() or None
    fixing_date = (match.group("date") or "").strip() or None
    return index, fixing_date


def _map_engine_rpc_error(exc: EngineRpcError) -> HTTPException:
    """Sub-table for :class:`EngineRpcError` keyed by gRPC status name."""

    # Missing-fixing is detected by the QuantLib message text, not the gRPC
    # status code (the engine may surface it as INTERNAL / UNKNOWN / etc.), so
    # this check runs BEFORE the status-code table — a well-formed request that
    # merely lacks a required past fixing must not be mis-reported as a generic
    # upstream error or an invalid-request.
    probe = exc.details or str(exc)
    parsed = parse_missing_fixing(probe)
    if parsed is not None:
        index, fixing_date = parsed
        return EngineMissingFixingError(index=index, fixing_date=fixing_date, engine_detail=probe)

    # QuantLib's ``negative time`` abort = a date-coherence failure (the
    # pricing as-of predates a curve's reference date). Translate it into the
    # typed 422 ``pricing_as_of_before_curve_date`` in its generic form; a
    # route holding the resolved curves upgrades message + details via
    # ``add_date_context`` so the envelope names the exact dates.
    if _NEGATIVE_TIME_RE.search(probe):
        return EngineDateCoherenceError(engine_detail=str(exc) or probe)

    code = exc.code or ""
    if code in _GRPC_INVALID_REQUEST_CODES:
        return EngineInvalidRequestError(reason=str(exc) or "Pricing engine rejected the request.")
    if code in _GRPC_TIMEOUT_CODES:
        return EngineTimeoutMappedError(reason=str(exc) or "Pricing engine timed out.")
    return EngineUpstreamError(reason=str(exc) or "Pricing engine returned an error.")


def map_engine_client_error(exc: BaseException) -> HTTPException:
    """Translate an engine-client exception into the envelope.

    Keeps the route handler free of error-mapping branches. Returns a
    ready-to-raise ``HTTPException`` so the route can ``raise
    map_engine_client_error(...)`` in one line.

    Mapping table (most specific first):

    * ``NotImplementedError`` → ``engine_unavailable`` (502).
    * :class:`EngineTimeoutError` /
      :class:`EngineRetryableError` → ``engine_timeout`` (504). The
      retry decorator already exhausted its budget by the time we
      see one of these here.
    * :class:`EngineRpcError` (sub-dispatched in
      :func:`_map_engine_rpc_error`): a ``Missing <index> fixing for
      <date>`` message (detected by text, any gRPC code) →
      ``missing_required_fixing`` (422) naming the index + date;
      a QuantLib ``negative time`` abort (detected by text) →
      ``pricing_as_of_before_curve_date`` (422) with an actionable
      date-coherence message instead of the raw abort;
      ``INVALID_ARGUMENT`` → ``engine_invalid_request`` (400);
      ``UNAVAILABLE`` / ``DEADLINE_EXCEEDED`` → ``engine_timeout``
      (504); everything else → ``engine_upstream_error`` (502).
    * :class:`EngineClientError` (any other subclass) →
      ``engine_upstream_error`` (502).
    * ``ConnectionError`` / ``OSError`` → ``engine_unreachable``
      (502). Reserved for transport-level failures that escape gRPC's
      own wrapping.
    * Everything else → ``engine_upstream_error`` (502); the catch-
      all 500 handler is bypassed because the engine code path has
      already failed and we want a stable client surface.
    """

    if isinstance(exc, NotImplementedError):
        return EngineUnavailableError(reason=str(exc) or None)
    if isinstance(exc, EngineTimeoutError | EngineRetryableError):
        return EngineTimeoutMappedError(reason=str(exc) or "Pricing engine timed out.")
    if isinstance(exc, EngineRpcError):
        return _map_engine_rpc_error(exc)
    if isinstance(exc, EngineClientError):
        return EngineUpstreamError(reason=str(exc) or "Pricing engine returned an error.")
    if isinstance(exc, ConnectionError | OSError):
        return EngineUnreachableError(reason=str(exc) or "Pricing engine unreachable.")
    return EngineUpstreamError(reason=str(exc) or "Pricing engine returned an error.")


__all__ = [
    "ENGINE_INVALID_REQUEST_CODE",
    "ENGINE_TIMEOUT_CODE",
    "ENGINE_UNAVAILABLE_CODE",
    "ENGINE_UNREACHABLE_CODE",
    "ENGINE_UPSTREAM_ERROR_CODE",
    "MISSING_REQUIRED_FIXING_CODE",
    "PRICING_AS_OF_BEFORE_CURVE_DATE_CODE",
    "EngineDateCoherenceError",
    "EngineInvalidRequestError",
    "EngineMissingFixingError",
    "EngineTimeoutMappedError",
    "EngineUnavailableError",
    "EngineUnreachableError",
    "EngineUpstreamError",
    "map_engine_client_error",
    "parse_missing_fixing",
]
