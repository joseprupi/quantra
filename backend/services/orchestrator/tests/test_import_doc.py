"""Route + mapping tests for ``POST /v1/import`` (engine-format document import).

Hermetic: the ``app_rw`` / ``app_ro`` engines are recording
:class:`FakeEngine` instances (from ``conftest``) driven by a tiny
in-memory ``_FakeAppDb`` handler, so no Postgres is touched. These
assert the accepted input shapes (full request / bare pricing /
fragment / legacy flat layout), the per-entity mapping (incl.
value-point curves + quote substitution), per-item error isolation,
name-conflict semantics in both ``on_conflict`` modes, dry-run
no-write, unsupported-section reporting, the audit ``change_reason``,
and the 400 / 401 / 503 envelopes.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from http import HTTPStatus
from typing import Any

import pytest
from fastapi.testclient import TestClient

from quantra_common.settings import Environment, LogLevel
from quantra_orchestrator.app import create_app
from quantra_orchestrator.importer import map_document
from quantra_orchestrator.settings import OrchestratorSettings

from .conftest import FakeEngine

# ---------------------------------------------------------------------------
# Fixtures: bypass-auth app over an in-memory app.* store
# ---------------------------------------------------------------------------


def _settings(*, bypass: bool = True) -> OrchestratorSettings:
    return OrchestratorSettings(
        env=Environment.DEV,
        log_level=LogLevel.WARNING,
        build_sha="testsha",
        dev_auth_bypass=bypass,
    )


class _FakeAppDb:
    """In-memory ``app.*`` store: INSERTs land rows, the name precheck reads them."""

    def __init__(self) -> None:
        # table -> name -> stored params
        self.tables: dict[str, dict[str, dict[str, Any]]] = {}
        self.version_inserts: list[dict[str, Any]] = []

    def seed(self, table: str, name: str) -> None:
        self.tables.setdefault(table, {})[name] = {"name": name, "id": str(uuid.uuid4())}

    def handler(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        s = sql.strip()
        if "INSERT INTO app.entity_versions" in s:
            self.version_inserts.append(dict(params))
            return []
        if s.startswith("INSERT INTO app."):
            table = s.split("INSERT INTO app.", 1)[1].split(" ", 1)[0]
            row = {"id": str(uuid.uuid4()), **params}
            self.tables.setdefault(table, {})[str(params.get("name"))] = row
            return [row]
        if "SELECT 1 AS present FROM app." in s:
            table = s.split("FROM app.", 1)[1].split(" ", 1)[0]
            present = str(params.get("name")) in self.tables.get(table, {})
            return [{"present": 1}] if present else []
        return []


@pytest.fixture
def fake_db() -> _FakeAppDb:
    return _FakeAppDb()


@pytest.fixture
def client(
    fake_db: _FakeAppDb,
    fake_rw_engine: FakeEngine,
    fake_ro_engine: FakeEngine,
) -> Iterator[TestClient]:
    fake_rw_engine.set_handler(fake_db.handler)
    fake_ro_engine.set_handler(fake_db.handler)
    app = create_app(
        _settings(),
        app_rw_engine=fake_rw_engine,  # type: ignore[arg-type]
        app_ro_engine=fake_ro_engine,  # type: ignore[arg-type]
    )
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# Document builders (mirror the engine's examples/data shapes)
# ---------------------------------------------------------------------------


def _index_def(index_id: str = "EUR_6M") -> dict[str, Any]:
    return {
        "id": index_id,
        "name": "Euribor",
        "index_type": "Ibor",
        "fixing_days": 2,
        "calendar": "TARGET",
        "business_day_convention": "ModifiedFollowing",
        "day_counter": "Actual360",
        "end_of_month": False,
        "currency": "EUR",
        "tenor": {"n": 6, "unit": "Months"},
    }


def _deposit_point(rate: float | None = 0.03, **inner: Any) -> dict[str, Any]:
    point: dict[str, Any] = {
        "fixing_days": 2,
        "calendar": "TARGET",
        "business_day_convention": "ModifiedFollowing",
        "day_counter": "Actual365Fixed",
        "tenor": {"n": 6, "unit": "Months"},
    }
    if rate is not None:
        point["rate"] = rate
    point.update(inner)
    return {"point_type": "DepositHelper", "point": point}


def _swap_point(index_id: str = "EURIBOR_6M") -> dict[str, Any]:
    return {
        "point_type": "SwapHelper",
        "point": {
            "rate": 0.03,
            "calendar": "TARGET",
            "sw_fixed_leg_frequency": "Annual",
            "sw_fixed_leg_convention": "ModifiedFollowing",
            "sw_fixed_leg_day_counter": "Thirty360",
            "spread": 0.0,
            "fwd_start_days": 0,
            "float_index": {"id": index_id},
            "tenor": {"n": 5, "unit": "Years"},
        },
    }


def _curve(
    curve_id: str = "discount", points: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        "id": curve_id,
        "day_counter": "Actual365Fixed",
        "interpolator": "LogLinear",
        "reference_date": "2025-01-15",
        "bootstrap_trait": "Discount",
        "points": points if points is not None else [_deposit_point(), _swap_point()],
    }


def _zero_point(pillar: str, rate: float) -> dict[str, Any]:
    return {
        "point_type": "ZeroRatePoint",
        "point": {
            "date": pillar,
            "zero_rate": rate,
            "compounding": "Continuous",
            "frequency": "Annual",
            "calendar": "TARGET",
            "business_day_convention": "ModifiedFollowing",
        },
    }


def _value_curve(curve_id: str = "USD_ZERO_LIN") -> dict[str, Any]:
    return {
        "id": curve_id,
        "reference_date": "2025-01-15",
        "day_counter": "Actual365Fixed",
        "interpolator": "Linear",
        "bootstrap_trait": "InterpolatedZero",
        "points": [
            _zero_point("2025-01-15", 0.030),
            _zero_point("2026-01-15", 0.032),
            _zero_point("2028-01-15", 0.034),
        ],
    }


def _vanilla_swap_request() -> dict[str, Any]:
    return {
        "pricing": {
            "as_of_date": "2025-01-15",
            "rates": {"indices": [_index_def()], "curves": [_curve()]},
        },
        "swaps": [{"vanilla_swap": {"swap_type": "Payer"}, "discounting_curve": "discount"}],
    }


def _post(client: TestClient, document: dict[str, Any], **extra: Any) -> Any:
    return client.post("/v1/import", json={"document": document, **extra})


# ---------------------------------------------------------------------------
# Accepted input shapes
# ---------------------------------------------------------------------------


def test_full_vanilla_swap_request_imports_indices_and_curves(
    client: TestClient, fake_db: _FakeAppDb
) -> None:
    response = _post(client, _vanilla_swap_request())
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["ok"] is True
    assert body["dry_run"] is False
    assert [(i["entity_type"], i["name"]) for i in body["imported"]] == [
        ("index", "EUR_6M"),
        ("curve", "discount"),
    ]
    assert all(i["id"] for i in body["imported"])
    assert body["errors"] == []
    # the trade is REPORTED, never silently dropped
    assert body["unsupported"] == [
        {
            "section": "swaps",
            "source_id": "",
            "path": "swaps[0]",
            "reason": "unsupported_in_v1: trades",
        }
    ]
    # rows actually landed in the store
    assert "EUR_6M" in fake_db.tables["indices"]
    assert "discount" in fake_db.tables["curves"]


def test_bare_pricing_object_and_fragment_both_accepted(client: TestClient) -> None:
    # bare pricing object (nested layout)
    r1 = _post(client, {"rates": {"curves": [_curve("c-nested")]}})
    assert r1.status_code == HTTPStatus.OK
    assert [i["name"] for i in r1.json()["imported"]] == ["c-nested"]
    # bare fragment
    r2 = _post(client, {"curves": [_curve("c-fragment")]})
    assert r2.status_code == HTTPStatus.OK
    assert [i["name"] for i in r2.json()["imported"]] == ["c-fragment"]


def test_legacy_flat_pricing_layout_accepted(client: TestClient, fake_db: _FakeAppDb) -> None:
    document = {
        "pricing": {
            "as_of_date": "2025-01-15",
            "indices": [_index_def("FLAT_6M")],
            "curves": [_curve("flat-discount", [_deposit_point(), _swap_point("FLAT_6M")])],
        }
    }
    response = _post(client, document)
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["ok"] is True
    assert [(i["entity_type"], i["name"]) for i in body["imported"]] == [
        ("index", "FLAT_6M"),
        ("curve", "flat-discount"),
    ]
    # scalar/body split: engine id -> name; display name + tenor in the body
    stored = fake_db.tables["indices"]["FLAT_6M"]
    assert stored["kind"] == "IBOR"
    assert stored["currency"] == "EUR"


def test_value_point_curve_imports(client: TestClient, fake_db: _FakeAppDb) -> None:
    response = _post(client, {"curves": [_value_curve()]})
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["ok"] is True
    assert [i["name"] for i in body["imported"]] == ["USD_ZERO_LIN"]
    assert "USD_ZERO_LIN" in fake_db.tables["curves"]


# ---------------------------------------------------------------------------
# Quote substitution
# ---------------------------------------------------------------------------


def test_document_quotes_substitute_into_points(client: TestClient) -> None:
    document: dict[str, Any] = {
        "pricing": {
            "quotes": [{"id": "eur_6m_depo", "kind": "Rate", "value": 0.0275}],
            "rates": {
                "curves": [_curve("q-curve", [_deposit_point(rate=None, quote_id="eur_6m_depo")])]
            },
        }
    }

    response = _post(client, document)
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["ok"] is True
    assert body["warnings"] == []
    # the mapping is deterministic — assert through the pure mapper
    mapped = map_document(document)
    stored_point = mapped.items[0].values["points"][0]["point"]
    assert stored_point["rate"] == 0.0275
    assert "quote_id" not in stored_point


def test_unresolved_quote_id_kept_verbatim_with_warning(client: TestClient) -> None:
    document: dict[str, Any] = {
        "curves": [_curve("md-curve", [_deposit_point(rate=None, quote_id="GBP.RATES.5Y")])]
    }

    response = _post(client, document)
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["ok"] is True
    assert len(body["warnings"]) == 1
    warning = body["warnings"][0]
    assert warning["entity_type"] == "curve"
    assert "GBP.RATES.5Y" in warning["message"]
    assert "market data" in warning["message"]
    mapped = map_document(document)
    assert mapped.items[0].values["points"][0]["point"]["quote_id"] == "GBP.RATES.5Y"


# ---------------------------------------------------------------------------
# Conflict semantics + dry run
# ---------------------------------------------------------------------------


def test_name_conflict_default_mode_reports_error(client: TestClient, fake_db: _FakeAppDb) -> None:
    fake_db.seed("curves", "discount")
    response = _post(client, {"curves": [_curve("discount")]})
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["ok"] is False
    assert body["imported"] == []
    assert len(body["errors"]) == 1
    assert "name_conflict" in body["errors"][0]["reason"]


def test_name_conflict_skip_mode_reports_skipped(client: TestClient, fake_db: _FakeAppDb) -> None:
    fake_db.seed("curves", "discount")
    response = _post(client, {"curves": [_curve("discount")]}, on_conflict="skip")
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["ok"] is True
    assert body["imported"] == []
    assert body["skipped"] == [
        {
            "entity_type": "curve",
            "source_id": "discount",
            "name": "discount",
            "reason": "name_conflict",
        }
    ]


def test_duplicate_id_within_document_conflicts_with_itself(client: TestClient) -> None:
    response = _post(client, {"curves": [_curve("dupe"), _curve("dupe")]}, on_conflict="skip")
    body = response.json()
    assert [i["name"] for i in body["imported"]] == ["dupe"]
    assert [s["name"] for s in body["skipped"]] == ["dupe"]


def test_dry_run_validates_and_writes_nothing(
    client: TestClient, fake_db: _FakeAppDb, fake_rw_engine: FakeEngine
) -> None:
    response = _post(client, _vanilla_swap_request(), dry_run=True)
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["ok"] is True
    assert body["dry_run"] is True
    assert [i["name"] for i in body["imported"]] == ["EUR_6M", "discount"]
    assert all(i["id"] is None for i in body["imported"])
    # nothing written: no rw-mode statements at all, store untouched
    assert [r for r in fake_rw_engine.recordings if r.mode == "write"] == []
    assert fake_db.tables == {}
    assert fake_db.version_inserts == []


# ---------------------------------------------------------------------------
# Audit provenance
# ---------------------------------------------------------------------------


def test_create_records_default_change_reason(client: TestClient, fake_db: _FakeAppDb) -> None:
    _post(client, {"curves": [_curve("audited")]})
    assert len(fake_db.version_inserts) == 1
    assert fake_db.version_inserts[0]["change_reason"] == "imported via /v1/import"
    assert fake_db.version_inserts[0]["change_type"] == "create"


def test_x_change_reason_header_overrides_default(client: TestClient, fake_db: _FakeAppDb) -> None:
    client.post(
        "/v1/import",
        json={"document": {"curves": [_curve("audited-2")]}},
        headers={"X-Change-Reason": "term sheet batch 7"},
    )
    assert fake_db.version_inserts[0]["change_reason"] == "term sheet batch 7"


# ---------------------------------------------------------------------------
# Per-item errors + isolation
# ---------------------------------------------------------------------------


def test_per_item_error_does_not_abort_the_rest(client: TestClient) -> None:
    bad_curve = _curve("bad", [{"point_type": "NoSuchHelper", "point": {}}])
    response = _post(client, {"curves": [bad_curve, _curve("good")]})
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["ok"] is False
    assert [i["name"] for i in body["imported"]] == ["good"]
    assert len(body["errors"]) == 1
    error = body["errors"][0]
    assert error["source_id"] == "bad"
    assert error["path"] == "pricing.rates.curves[0]"
    assert "NoSuchHelper" in error["reason"]


def test_curve_referencing_unknown_index_is_rejected(client: TestClient) -> None:
    document = {"curves": [_curve("orphan", [_deposit_point(), _swap_point("MYSTERY_IDX")])]}
    response = _post(client, document)
    body = response.json()
    assert body["ok"] is False
    assert body["imported"] == []
    assert "MYSTERY_IDX" in body["errors"][0]["reason"]


def test_curve_referencing_document_index_is_accepted(client: TestClient) -> None:
    document = {
        "indices": [_index_def("MY_CUSTOM_6M")],
        "curves": [_curve("custom", [_deposit_point(), _swap_point("MY_CUSTOM_6M")])],
    }
    response = _post(client, document)
    body = response.json()
    assert body["ok"] is True
    assert [i["name"] for i in body["imported"]] == ["MY_CUSTOM_6M", "custom"]


def test_credit_curve_quote_ref_missing_from_document_is_an_error(
    client: TestClient,
) -> None:
    credit = {
        "id": "eur_credit",
        "reference_date": "2025-01-15",
        "recovery_rate": 0.4,
        "quotes": [
            {
                "quote_type": "ParSpread",
                "quote_id": "eur_credit_5y",
                "tenor": {"n": 5, "unit": "Years"},
            }
        ],
    }
    response = _post(client, {"credit_curves": [credit]})
    body = response.json()
    assert body["ok"] is False
    assert "eur_credit_5y" in body["errors"][0]["reason"]


def test_credit_curve_with_document_quote_substitutes_and_imports(
    client: TestClient, fake_db: _FakeAppDb
) -> None:
    document = {
        "pricing": {
            "quotes": [{"id": "eur_credit_5y", "kind": "Rate", "value": 0.012}],
            "credit": {
                "credit_curves": [
                    {
                        "id": "eur_credit",
                        "reference_date": "2025-01-15",
                        "recovery_rate": 0.4,
                        "quotes": [
                            {
                                "quote_type": "ParSpread",
                                "quote_id": "eur_credit_5y",
                                "tenor": {"n": 5, "unit": "Years"},
                            }
                        ],
                    }
                ]
            },
        }
    }
    response = _post(client, document)
    body = response.json()
    assert body["ok"] is True
    assert [i["entity_type"] for i in body["imported"]] == ["credit_curve"]
    mapped = map_document(document)
    quote = mapped.items[0].values["body"]["quotes"][0]
    assert quote["quoted_par_spread"] == 0.012
    assert "quote_id" not in quote
    assert mapped.items[0].values["source"] == "manual"


def test_flat_hazard_credit_curve_maps_source_flat(client: TestClient) -> None:
    credit = {
        "id": "flat_credit",
        "reference_date": "2025-01-15",
        "recovery_rate": 0.4,
        "quotes": [],
        "flat_hazard_rate": 0.02,
    }
    response = _post(client, {"credit_curves": [credit]})
    assert response.json()["ok"] is True
    mapped = map_document({"credit_curves": [credit]})
    assert mapped.items[0].values["source"] == "flat"


def test_vol_surface_and_model_import(client: TestClient, fake_db: _FakeAppDb) -> None:
    surface = {
        "id": "swaption_atm",
        "payload_type": "SwaptionVolSpec",
        "payload": {
            "swap_index_id": "EUR_SWAP_6M",
            "payload_type": "SwaptionVolAtmMatrixSpec",
            "payload": {
                "base": {
                    "reference_date": "2025-01-15",
                    "calendar": "TARGET",
                    "business_day_convention": "ModifiedFollowing",
                    "day_counter": "Actual365Fixed",
                    "volatility_type": "Lognormal",
                    "displacement": 0.0,
                },
                "expiries": [{"n": 1, "unit": "Years"}, {"n": 2, "unit": "Years"}],
                "tenors": [{"n": 5, "unit": "Years"}, {"n": 10, "unit": "Years"}],
                "vols": {"n_rows": 2, "n_cols": 2, "values": [0.2, 0.22, 0.24, 0.25]},
            },
        },
    }
    model = {
        "id": "hw_model",
        "payload_type": "SwaptionModelSpec",
        "payload": {"model_type": "HullWhiteLattice", "hw_a": 0.03, "hw_sigma": 0.01},
    }
    response = _post(client, {"vol_surfaces": [surface], "models": [model]})
    body = response.json()
    assert body["ok"] is True
    assert [(i["entity_type"], i["name"]) for i in body["imported"]] == [
        ("vol_surface", "swaption_atm"),
        ("swaption_model", "hw_model"),
    ]
    assert fake_db.tables["vol_surfaces"]["swaption_atm"]["kind"] == "SwaptionVolSpec"
    assert fake_db.tables["swaption_models"]["hw_model"]["kind"] == "HullWhiteLattice"


def test_non_swaption_model_payload_is_a_per_item_error(client: TestClient) -> None:
    model = {"id": "cds_isda", "payload_type": "CdsModelSpec", "payload": {}}
    response = _post(client, {"models": [model]})
    body = response.json()
    assert body["ok"] is False
    assert "unsupported_kind" in body["errors"][0]["reason"]


def test_unsupported_sections_reported_per_item(client: TestClient) -> None:
    document = {
        "pricing": {
            "rates": {
                "curves": [_curve("kept")],
                "swap_indices": [{"id": "EUR_SWAP_6M"}],
            },
            "inflation": {"inflation_indices": [{"id": "EUHICP"}]},
        },
        "swaps": [{"vanilla_swap": {}}],
        "bonds": [{"fixed_rate_bond": {}}],
    }
    response = _post(client, document)
    body = response.json()
    assert body["ok"] is True
    assert [i["name"] for i in body["imported"]] == ["kept"]
    reasons = {(u["section"], u["reason"]) for u in body["unsupported"]}
    assert reasons == {
        ("swaps", "unsupported_in_v1: trades"),
        ("bonds", "unsupported_in_v1: trades"),
        ("swap_indices", "unsupported_in_v1: swap_indices"),
        ("inflation_indices", "unsupported_in_v1: inflation"),
    }


# ---------------------------------------------------------------------------
# Envelope-level failures
# ---------------------------------------------------------------------------


def test_empty_document_is_400_import_invalid_request(client: TestClient) -> None:
    response = _post(client, {"nothing": "here"})
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json()["code"] == "import_invalid_request"


def test_missing_document_is_422_validation_error(client: TestClient) -> None:
    response = client.post("/v1/import", json={"dry_run": True})
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["code"] == "validation_error"


def test_401_without_credentials() -> None:
    app = create_app(_settings(bypass=False))
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/v1/import", json={"document": {"curves": [_curve()]}})
    assert response.status_code == HTTPStatus.UNAUTHORIZED


def test_503_when_storage_unconfigured() -> None:
    app = create_app(_settings())
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/v1/import", json={"document": {"curves": [_curve()]}})
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json()["code"] == "storage_unavailable"


# ---------------------------------------------------------------------------
# Pure mapper details (no route)
# ---------------------------------------------------------------------------


def test_mapper_curve_body_keeps_construction_fields() -> None:
    mapped = map_document({"curves": [_curve("shape-check")]})
    values = mapped.items[0].values
    assert values["body"]["interpolator"] == "LogLinear"
    assert values["body"]["bootstrap_trait"] == "Discount"
    assert values["body"]["local_id"] == "shape-check"
    assert values["currency"] is None
    assert values["day_counter"] == "Actual365Fixed"
    assert str(values["reference_date"]) == "2025-01-15"


def test_mapper_index_scalar_body_split() -> None:
    mapped = map_document({"indices": [_index_def("SPLIT_6M")]})
    values = mapped.items[0].values
    assert values["name"] == "SPLIT_6M"
    assert values["kind"] == "IBOR"
    assert values["calendar"] == "TARGET"
    assert values["day_counter"] == "Actual360"
    body = values["body"]
    assert body["name"] == "Euribor"  # display name honoured by translate_index
    assert body["tenor"] == {"n": 6, "unit": "Months"}
    assert body["fixing_days"] == 2
    assert body["end_of_month"] is False


def test_mapper_nested_layout_wins_over_flat() -> None:
    document = {
        "pricing": {
            "rates": {"curves": [_curve("nested-wins")]},
            "curves": [_curve("flat-loses")],
        }
    }
    mapped = map_document(document)
    assert [i.source_id for i in mapped.items] == ["nested-wins"]


def test_mapper_missing_id_is_a_per_item_error() -> None:
    curve = _curve("x")
    del curve["id"]
    mapped = map_document({"curves": [curve]})
    assert mapped.items == []
    assert len(mapped.errors) == 1
    assert "``id``" in mapped.errors[0].reason
