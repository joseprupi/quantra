"""Shared per-stage trace recording helpers.

Lifted out of the swap_ir pilot's ``api.py`` so every product
instruments the same pipeline stages with a
handful of calls instead of copy-pasting the recorder boilerplate. Each
helper buffers exactly one stage event into the per-request
:class:`~quantra_orchestrator.tracing.recorder.TraceRecorder`; the
product route supplies only the small product-specific payload bits.

The helpers are deliberately thin and side-effect-free beyond the
buffer append — the recorder itself enforces the ``TRACE_CAPTURE`` gate,
the size cap, and the best-effort off-hot-path flush, so a product never
re-implements any of those guarantees.

Stage coverage (one helper each):

* :func:`record_input` — ``input``
* :func:`record_load_entities` — ``load_entities``
* :func:`record_md_resolve` — ``md_resolve``
* :func:`record_engine_request` — ``engine_request``
* :func:`record_engine_response` — ``engine_response`` (success)
* :func:`record_engine_error` — ``engine_response`` (failure, real text)
* :func:`record_history_write` — ``history_write``
* :func:`record_error_stage` — ``error`` (the envelope the client saw)
"""

from __future__ import annotations

import base64
import re
import time
from collections.abc import Callable
from datetime import date, datetime
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, Final, Protocol

from fastapi import HTTPException

from quantra_common.engine_client import CapturingEngineClient, EngineWireRecord
from quantra_orchestrator.tracing.recorder import (
    STAGE_ENGINE_REQUEST,
    STAGE_ENGINE_RESPONSE,
    STAGE_ERROR,
    STAGE_HISTORY_WRITE,
    STAGE_INPUT,
    STAGE_LOAD_ENTITIES,
    STAGE_MD_RESOLVE,
    TraceRecorder,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence


class ResolvedQuoteLike(Protocol):
    """Structural shape of one resolved quote, shared across products.

    Every product's ``ResolvedQuoteValue`` (swap_ir / swaption / bonds /
    cds / equity_options / swaps_inflation) carries exactly these
    fields, so :func:`record_md_resolve` can build the timeline payload
    without importing any product model.
    """

    canonical_id: str
    value: float
    source: str | None
    from_snapshot: bool
    as_of: date | datetime


def elapsed_ms(start: float) -> float:
    """Milliseconds elapsed since a ``time.monotonic()`` mark."""

    return (time.monotonic() - start) * 1000.0


# ---------------------------------------------------------------------------
# Human-readable per-stage summaries (summaries)
# Each stage carries a short plain-English sentence that interprets the
# stage's ACTUAL content — especially empty / zero / failure states so a
# "correct but empty" stage stops looking broken on the investigate
# screen. Summaries are generated here, in the SHARED helpers, from the
# data each stage already has, so EVERY product inherits them at once.
# Generation is DEFENSIVE by construction: tracing is best-effort and must
# never throw, so every builder is wrapped and falls back to a generic
# sentence when a field is missing or unexpected.
# ---------------------------------------------------------------------------


def _coerce_float(value: object) -> float | None:
    """Best-effort numeric coercion for a summary field; ``None`` on failure."""

    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _fmt_amount(value: object) -> str:
    """Render a notional/amount with thousands separators, no decimals if whole."""

    num = _coerce_float(value)
    if num is None:
        return str(value)
    if num == int(num):
        return f"{int(num):,}"
    return f"{num:,.2f}"


def _fmt_num(value: object) -> str:
    """Render a money figure with thousands separators and up to 2 decimals."""

    num = _coerce_float(value)
    if num is None:
        return str(value)
    if num == int(num):
        return f"{int(num):,}"
    return f"{num:,.2f}"


def _fmt_rate(value: object) -> str:
    """Render a coupon/strike rate as a percent (decimal fraction in → ``2.5%``)."""

    num = _coerce_float(value)
    if num is None:
        return str(value)
    # Rates flow through the engine as decimal fractions (0.025 == 2.5%).
    # A magnitude >= 1 is already expressed in percent points, so leave it.
    pct = num * 100.0 if abs(num) < 1.0 else num
    return f"{pct:g}%"


def _first(params: dict[str, Any], *keys: str) -> Any:  # noqa: ANN401 - loose payloads
    """First non-``None`` value among ``keys`` in ``params`` (else ``None``)."""

    for key in keys:
        val = params.get(key)
        if val is not None:
            return val
    return None


def _summarize_input(product: str, params: dict[str, Any]) -> str:
    """One-liner for the ``input`` stage from the product tag + trade params."""

    label = product or "trade"
    try:
        bits: list[str] = []
        notional = _coerce_float(_first(params, "notional", "nominal", "face_amount"))
        side = _first(params, "swap_type", "side", "direction")
        rate = _coerce_float(_first(params, "fixed_rate", "rate", "strike", "coupon"))
        eff = _first(params, "effective_date", "start_date", "issue_date")
        term = _first(params, "termination_date", "maturity_date", "end_date", "maturity")
        as_of = params.get("as_of")

        ticket: str | None = None
        if notional is not None:
            ticket = _fmt_amount(notional)
            if side:
                ticket = f"{ticket} {side}"
        elif side:
            ticket = str(side)
        if rate is not None:
            rate_str = f"@ {_fmt_rate(rate)}"
            ticket = f"{ticket} {rate_str}" if ticket else rate_str
        if ticket:
            bits.append(ticket)
        if eff and term:
            bits.append(f"{eff} → {term}")

        # Fall back to ref-vs-inline shape when no trade economics are present.
        if not bits:
            ref_id = _first(
                params,
                "swap_id",
                "bond_id",
                "cds_id",
                "equity_option_id",
                "swaption_id",
            )
            inline = any(bool(v) for k, v in params.items() if k.startswith("inline_"))
            if ref_id:
                bits.append(f"saved {ref_id}")
            elif inline:
                bits.append("inline trade")

        head = f"Priced {label}"
        if bits:
            head += ": " + ", ".join(bits)
        if as_of:
            head += f", as_of {as_of}" if bits else f"; as_of {as_of}"
        return head + "."
    except Exception:  # tracing is best-effort — never raise
        return f"Priced {label}."


def _summarize_load_entities(entities: dict[str, Any]) -> str:
    """One-liner for ``load_entities`` describing what was loaded from ``app.*``."""

    try:
        descs: list[str] = []
        # Style A: role-prefixed singular curve ids (bonds / cds / equity /
        # inflation), e.g. ``discount_curve_id`` / ``credit_curve_id``.
        role_curves = [
            key[: -len("_curve_id")].replace("_", " ")
            for key, val in entities.items()
            if key.endswith("_curve_id") and val
        ]
        # Style B: list of curve names / ids (swap_ir / swaption).
        names = entities.get("curve_names")
        ids = entities.get("curve_ids")
        list_count = (
            len(names)
            if isinstance(names, list) and names
            else (len(ids) if isinstance(ids, list) and ids else 0)
        )

        if role_curves:
            for role in role_curves:
                descs.append(f"{role} curve")
        elif list_count:
            label = f"{list_count} curve" + ("s" if list_count != 1 else "")
            if isinstance(names, list) and names and all(names):
                label += f" ({', '.join(str(n) for n in names)})"
            descs.append(label)

        snap = entities.get("snapshot")
        if isinstance(snap, dict) and snap:
            descs.append(f"snapshot '{snap.get('name') or snap.get('id')}'")
        index = entities.get("index")
        if index:
            descs.append(f"index {index}")

        if not descs:
            return "Loaded pricing inputs."
        return "Loaded " + ", ".join(descs) + " from inputs."
    except Exception:  # tracing is best-effort — never raise
        return "Loaded pricing inputs."


def _summarize_md_resolve(
    *, requested: list[str], resolved: int, live: int, snapshot: int, misses: int
) -> str:
    """One-liner for ``md_resolve`` — explains the empty/inline case explicitly."""

    try:
        total = len(requested)
        if total == 0:
            return (
                "No market-data quotes resolved — curve uses inline rates, "
                "nothing pulled from the market-data service."
            )
        miss_word = "miss" if misses == 1 else "misses"
        quote_word = "quote" if total == 1 else "quotes"
        return (
            f"Resolved {resolved} of {total} {quote_word} "
            f"({live} live, {snapshot} from snapshot); {misses} {miss_word}."
        )
    except Exception:  # tracing is best-effort — never raise
        return "Resolved market-data quotes."


def _summarize_engine_request(payload: dict[str, Any]) -> str:
    """One-liner for ``engine_request`` from the wire view (rpc / bytes / index)."""

    try:
        wire = payload.get("engine_wire")
        if not isinstance(wire, dict) or not wire.get("sent"):
            return "Assembled the engine request (not sent — pre-send failure)."
        rpc = wire.get("rpc") or "request"
        head = f"Sent {rpc} to the engine"
        nbytes = wire.get("request_bytes_len")
        if isinstance(nbytes, int):
            head += f" ({nbytes} bytes)"
        decoded = wire.get("decoded")
        tail: list[str] = []
        if isinstance(decoded, dict):
            index_ids: list[str] = []
            try:
                indices = decoded["pricing"]["rates"]["indices"]
                index_ids = [
                    str(idx["id"]) for idx in indices if isinstance(idx, dict) and idx.get("id")
                ]
            except (KeyError, TypeError):
                index_ids = []
            if index_ids:
                tail.append("curve registers index " + ", ".join(index_ids))
            include_flows = decoded.get("include_flows")
            if isinstance(include_flows, bool):
                tail.append(f"include_flows={'true' if include_flows else 'false'}")
        if tail:
            head += "; " + "; ".join(tail)
        return head + "."
    except Exception:  # tracing is best-effort — never raise
        return "Sent the assembled request to the engine."


def _leg_breakdown(legs: object) -> str:
    """Render ``[{role, npv}, ...]`` as ``fixed -1,160,090 / floating 1,176,847``."""

    if not isinstance(legs, list):
        return ""
    parts: list[str] = []
    for leg in legs:
        if not isinstance(leg, dict):
            continue
        role = leg.get("role") or leg.get("type") or leg.get("name")
        npv = leg.get("npv")
        if role and npv is not None:
            parts.append(f"{role} {_fmt_num(npv)}")
    return " / ".join(parts)


def _summarize_engine_response(payload: dict[str, Any]) -> str:
    """One-liner for the ``engine_response`` success stage (NPV + legs + flows)."""

    try:
        npv = payload.get("npv")
        head = (
            f"Engine returned NPV {_fmt_num(npv)}"
            if npv is not None
            else "Engine returned a result"
        )
        legs = _leg_breakdown(payload.get("leg_npvs"))
        if legs:
            head += f" ({legs})"
        flow_parts: list[str] = []
        total_flows = 0
        for key, word in (
            ("fixed_leg_flows", "fixed"),
            ("floating_leg_flows", "floating"),
            ("flows", "cash"),
            ("cashflows", "cash"),
        ):
            val = payload.get(key)
            if isinstance(val, list):
                flow_parts.append(f"{len(val)} {word}")
                total_flows += len(val)
        if flow_parts:
            cf_word = "cashflow" if total_flows == 1 else "cashflows"
            head += "; " + " + ".join(flow_parts) + f" {cf_word}"
        return head + "."
    except Exception:  # tracing is best-effort — never raise
        return "Engine returned a result."


def _summarize_engine_error(exc: BaseException) -> str:
    """One-liner for the ``engine_response`` failure stage — the engine's real text."""

    try:
        text = str(exc).strip() or type(exc).__name__
        return f"Engine error: {text}"
    except Exception:  # tracing is best-effort — never raise
        return "Engine error."


def _summarize_history_write(history_id: object | None, *, outcome: str) -> str:
    """One-liner for ``history_write`` — names the skip reason when not recorded."""

    try:
        if history_id is not None:
            return f"Recorded to pricing_history (id={history_id})."
        return (
            "Audit-log write skipped — the pricing_history row was not "
            "persisted (commonly the principal is absent from app.users, "
            "which the foreign key requires; dev-bypass users are not "
            "provisioned there)."
        )
    except Exception:  # tracing is best-effort — never raise
        return "Audit-log write outcome unavailable."


def _summarize_error(envelope: dict[str, Any]) -> str:
    """One-liner for the ``error`` stage — the code + message the client saw."""

    try:
        inner = envelope.get("error")
        if isinstance(inner, dict):
            code = inner.get("code") or "error"
            message = inner.get("error") or ""
            return f"Failed: {code} — {message}".rstrip(" —")
        return "Failed."
    except Exception:  # tracing is best-effort — never raise
        return "Failed."


def record_input(trace: TraceRecorder, *, product: str, params: dict[str, Any]) -> None:
    """Emit the ``input`` stage: product tag + key trade params.

    ``product`` is always threaded first so the timeline carries the
    product identifier inline (in addition to the dedicated
    ``app.pricing_traces.product`` column) — a future recent-calls list
    can show the product without parsing the rest of the payload.
    """

    trace.stage(
        STAGE_INPUT,
        {"product": product, **params},
        summary=_summarize_input(product, params),
    )


def record_load_entities(
    trace: TraceRecorder,
    entities: dict[str, Any],
    *,
    duration_ms: float | None = None,
) -> None:
    """Emit the ``load_entities`` stage: which ids loaded from ``app.*``."""

    trace.stage(
        STAGE_LOAD_ENTITIES,
        dict(entities),
        duration_ms=duration_ms,
        summary=_summarize_load_entities(entities),
    )


def record_md_resolve(
    trace: TraceRecorder,
    *,
    requested_canonical_ids: Iterable[str],
    resolved_quotes: Sequence[ResolvedQuoteLike],
    duration_ms: float | None = None,
) -> None:
    """Emit the ``md_resolve`` stage: requested ids, resolved values, misses.

    Captures the canonical IDs the walker asked for, every resolved
    value with its snapshot-vs-live provenance, and any canonical IDs
    that produced no value — the "what market data was pulled and
    resolved" view, identical in shape across every product.
    """

    requested = list(requested_canonical_ids)
    resolved_payload = [
        {
            "canonical_id": q.canonical_id,
            "value": q.value,
            "source": q.source,
            "from_snapshot": q.from_snapshot,
            "as_of": q.as_of.isoformat(),
        }
        for q in resolved_quotes
    ]
    resolved_ids = {q.canonical_id for q in resolved_quotes}
    snapshot_count = sum(1 for q in resolved_quotes if q.from_snapshot)
    live_count = len(resolved_payload) - snapshot_count
    misses = [cid for cid in requested if cid not in resolved_ids]
    trace.stage(
        STAGE_MD_RESOLVE,
        {
            "requested_canonical_ids": requested,
            "resolved": resolved_payload,
            "snapshot_count": snapshot_count,
            "live_count": live_count,
            "misses": misses,
        },
        duration_ms=duration_ms,
        summary=_summarize_md_resolve(
            requested=requested,
            resolved=len(resolved_payload),
            live=live_count,
            snapshot=snapshot_count,
            misses=len(misses),
        ),
    )


def record_engine_request(trace: TraceRecorder, engine_request_json: dict[str, Any]) -> None:
    """Emit the ``engine_request`` stage.

    The payload is built by :func:`build_engine_request_payload`: it carries
    both the orchestrator's assembled-inputs superset *and* the exact bytes
    transmitted to the engine over gRPC (base64 + a JSON view decoded straight
    from those bytes). See that function for the two-view rationale.
    """

    trace.stage(
        STAGE_ENGINE_REQUEST,
        engine_request_json,
        summary=_summarize_engine_request(engine_request_json),
    )


def record_engine_request_wire(
    trace: TraceRecorder,
    *,
    assembled_request: dict[str, Any],
    capturing_engine: CapturingEngineClient,
    decode: Callable[[bytes], dict[str, Any]],
) -> None:
    """Emit the ``engine_request`` stage with the wire view — shared.

    The single, product-agnostic capture point every product route calls after
    the engine round-trip: builds the two-view payload
    (:func:`build_engine_request_payload`) from the EXACT bytes recorded on a
    :class:`~quantra_common.engine_client.CapturingEngineClient` plus the
    product's ``decode`` reader, then records the stage. Each product supplies
    only its wire decoder (one line against its FlatBuffers request root type);
    the gate, the two-view assembly, and the ``sent: false`` pre-send handling
    live here once.

    Gated on ``trace.enabled`` so the (cheap) payload build + base64 is skipped
    entirely when capture is off. ``capturing_engine.last_request`` is ``None``
    when the request never reached the send boundary (a pre-send failure),
    which :func:`build_engine_request_payload` records as ``sent: false``.
    """

    if not trace.enabled:
        return
    record_engine_request(
        trace,
        build_engine_request_payload(
            assembled_request=assembled_request,
            wire=capturing_engine.last_request,
            decode=decode,
        ),
    )


_CAMEL_BOUNDARY: Final = re.compile(r"(?<!^)(?=[A-Z])")
_MAX_FB_DEPTH: Final = 64


def _camel_to_snake(name: str) -> str:
    """Normalise a FlatBuffers object-API field name to snake_case.

    ``includeFlows`` -> ``include_flows``, ``indexType`` -> ``index_type`` so
    the decoded view reads naturally in the timeline.
    """

    return _CAMEL_BOUNDARY.sub("_", name).lower()


def _bytes_to_jsonsafe(value: bytes | bytearray) -> str:
    """Render a FlatBuffers ``bytes`` field (string fields decode to bytes) as str."""

    try:
        return bytes(value).decode("utf-8")
    except UnicodeDecodeError:
        return bytes(value).hex()


def _walk_fb(value: object, depth: int) -> object:  # noqa: PLR0911 - one return per JSON kind
    """Recursively convert one FlatBuffers object-API value to a JSON-safe form."""

    if depth > _MAX_FB_DEPTH:
        return "__max_depth__"
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return _bytes_to_jsonsafe(value)
    if isinstance(value, dict):
        return {str(k): _walk_fb(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_walk_fb(v, depth + 1) for v in value]
    attrs = getattr(value, "__dict__", None)
    if isinstance(attrs, dict) and attrs:
        return {
            _camel_to_snake(k): _walk_fb(v, depth + 1)
            for k, v in attrs.items()
            if not k.startswith("_")
        }
    return str(value)


def serialize_fb_object(obj: object) -> dict[str, Any]:
    """Serialise a FlatBuffers object-API (``*T``) value into a JSON-safe dict.

    Recursively walks the object's instance attributes (each FB ``*T`` field is
    stored as a plain attribute), converting nested ``*T`` -> dict, lists ->
    lists, ``bytes`` (FB string fields decode to bytes) -> utf-8 str, and
    enum/scalars -> as-is. Keys are normalised camelCase -> snake_case. This is
    the shared serializer every product's ``engine_request`` decode reuses.

    Best-effort by construction (tracing must never disturb a price): a walk
    that hits anything unexpected degrades to a marker rather than raising.
    """

    try:
        walked = _walk_fb(obj, 0)
    except Exception as exc:  # tracing is best-effort — never raise
        return {"__unserializable__": True, "exc_type": type(exc).__name__}
    if isinstance(walked, dict):
        return walked
    return {"value": walked}


def build_engine_request_payload(
    *,
    assembled_request: dict[str, Any],
    wire: EngineWireRecord | None,
    decode: Callable[[bytes], dict[str, Any]],
) -> dict[str, Any]:
    """Build the ``engine_request`` stage payload — both views, labelled.

    Two clearly-separated views answer two different questions:

    * ``engine_wire`` — *what did we actually send to quantraserver?* The exact
      transmitted FlatBuffers buffer, base64-encoded, plus ``decoded``: a
      readable JSON parsed FROM those same bytes with the generated reader (via
      ``decode``). This is the indisputable wire payload — it contains the
      engine-native ``pricing`` / ``swaps`` / ``include_flows`` shape (incl. the
      rates.indices registry where ESTR lives) and none of the orchestrator-only
      fields. ``decoded`` round-trips: re-decoding ``request_bytes_b64`` with the
      same reader reproduces it.
    * ``assembled_request`` — the orchestrator's internal assembled-inputs
      superset (snapshot/curve-set ids, resolved quotes, per-curve metadata).
      Retained alongside the wire view so an investigator can correlate the two,
      but explicitly NOT the answer to "what reached the engine".

    Best-effort: ``decode`` is expected to be defensive; ``wire is None`` means
    the request never reached the send boundary (a pre-send failure), recorded
    as ``sent: false`` so the timeline still shows the orchestrator's inputs.
    """

    payload: dict[str, Any] = {
        "_note": (
            "engine_request: `engine_wire` is the EXACT bytes sent to the engine "
            "(base64 + decoded-from-those-bytes); `assembled_request` is the "
            "orchestrator's internal inputs superset, NOT what reached the engine."
        ),
        "assembled_request": assembled_request,
    }
    if wire is None:
        payload["engine_wire"] = {
            "sent": False,
            "_note": "request did not reach the engine send boundary (pre-send failure)",
        }
        return payload
    payload["engine_wire"] = {
        "sent": True,
        "_note": (
            "exact FlatBuffers bytes transmitted to quantraserver over gRPC; "
            "`decoded` is parsed FROM these bytes with the generated reader"
        ),
        "rpc": wire.rpc,
        "request_bytes_len": len(wire.request_bytes),
        "request_bytes_b64": base64.b64encode(wire.request_bytes).decode("ascii"),
        "decoded": decode(wire.request_bytes),
    }
    return payload


def record_engine_response(
    trace: TraceRecorder,
    payload: dict[str, Any],
    *,
    duration_ms: float | None = None,
) -> None:
    """Emit the ``engine_response`` stage on success: the engine's result view."""

    trace.stage(
        STAGE_ENGINE_RESPONSE,
        payload,
        duration_ms=duration_ms,
        summary=_summarize_engine_response(payload),
    )


def record_engine_error(
    trace: TraceRecorder, exc: BaseException, *, duration_ms: float | None = None
) -> None:
    """Emit the ``engine_response`` stage with the engine's REAL error text.

    Captures ``str(exc)`` (e.g. an ESTR negative-time bootstrap failure)
    rather than the mapped error token, so the investigator sees what the
    engine actually said.
    """

    trace.stage(
        STAGE_ENGINE_RESPONSE,
        {"error": str(exc), "exc_type": type(exc).__name__},
        level="error",
        duration_ms=duration_ms,
        summary=_summarize_engine_error(exc),
    )


def record_history_write(trace: TraceRecorder, history_id: object | None, *, outcome: str) -> None:
    """Emit the ``history_write`` stage: persistence success or failure reason."""

    trace.stage(
        STAGE_HISTORY_WRITE,
        {
            "recorded": history_id is not None,
            "pricing_history_id": (str(history_id) if history_id is not None else None),
            "outcome": outcome,
        },
        level="info" if history_id is not None else "warn",
        summary=_summarize_history_write(history_id, outcome=outcome),
    )


def record_error_stage(trace: TraceRecorder, exc: HTTPException) -> None:
    """Emit the ``error`` stage: the envelope the caller will see.

    Closes the timeline on the same failure the client got, covering
    every non-engine surface too (404s, 422s, MD errors — each already
    an :class:`HTTPException` by the time it reaches the route boundary).
    """

    envelope = failure_envelope(exc)
    trace.stage(
        STAGE_ERROR,
        envelope,
        level="error",
        summary=_summarize_error(envelope),
    )


def failure_envelope(exc: HTTPException) -> dict[str, Any]:
    """Render the envelope shape for a pricing failure row.

    Mirrors the global handler in :mod:`quantra_orchestrator.errors` so
    the persisted ``response`` / trace ``error`` stage matches what the
    client actually saw on the wire. The wrapper key is ``error`` so a
    success row and a failure row are distinguishable at read time.
    """

    status_code = exc.status_code
    detail = exc.detail
    message = detail if isinstance(detail, str) and detail else HTTPStatus(status_code).phrase
    custom_code = getattr(exc, "code", None)
    code = custom_code if isinstance(custom_code, str) and custom_code else f"http_{status_code}"
    envelope: dict[str, Any] = {
        "status_code": status_code,
        "error": message,
        "code": code,
    }
    details = getattr(exc, "details", None)
    if isinstance(details, list):
        envelope["details"] = details
    return {"error": envelope}


__all__ = [
    "ResolvedQuoteLike",
    "build_engine_request_payload",
    "elapsed_ms",
    "failure_envelope",
    "record_engine_error",
    "record_engine_request",
    "record_engine_request_wire",
    "record_engine_response",
    "record_error_stage",
    "record_history_write",
    "record_input",
    "record_load_entities",
    "record_md_resolve",
    "serialize_fb_object",
]
