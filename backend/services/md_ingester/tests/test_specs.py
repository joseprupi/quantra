"""Spec-loader tests.

Each spec file under ``quantra_md_ingester/configs`` is part of the
worker's deployed contract (the FRED config id list dictates which
canonical IDs the dev ingest will populate). The asserts below catch
the cases where a future config change silently invalidates the
loader (e.g. a renamed JSON field or a non-string ``maturity_years``).
"""

from __future__ import annotations

import pytest

from quantra_md_ingester.specs import (
    default_boe_config_path,
    default_boe_govt_curve_config_path,
    default_boe_ois_curve_config_path,
    default_ecb_config_paths,
    default_fred_config_paths,
    default_treasury_config_paths,
    load_boe_govt_curve_series,
    load_boe_ois_curve_series,
    load_boe_series,
    load_ecb_series,
    load_fred_series,
    load_treasury_series,
)


def test_default_fred_config_loads_and_has_dgs10() -> None:
    specs = load_fred_series(default_fred_config_paths()[0])
    assert len(specs) > 0
    series_ids = {s.series_id for s in specs}
    assert "DGS10" in series_ids
    dgs10 = next(s for s in specs if s.series_id == "DGS10")
    assert dgs10.canonical_id == "USD.RATES.UST.DGS.10Y.YIELD"
    assert dgs10.tenor == "10Y"


def test_default_ecb_configs_load_with_explicit_flow() -> None:
    paths = default_ecb_config_paths()
    seen = 0
    for path in paths:
        if not path.exists():
            continue
        specs = load_ecb_series(path)
        assert len(specs) > 0
        for spec in specs:
            assert spec.flow
            assert spec.series_key
            assert spec.canonical_id
        seen += 1
    assert seen > 0


def test_default_boe_configs_load() -> None:
    if default_boe_config_path().exists():
        boe_specs = load_boe_series(default_boe_config_path())
        assert len(boe_specs) > 0

    if default_boe_govt_curve_config_path().exists():
        curve_specs = load_boe_govt_curve_series(default_boe_govt_curve_config_path())
        assert len(curve_specs) > 0
        # The maturity column must parse as a real number so the curve
        # connector can compare it against the workbook tenor row.
        for spec in curve_specs:
            assert isinstance(spec.maturity_years, float)

    if default_boe_ois_curve_config_path().exists():
        ois_specs = load_boe_ois_curve_series(default_boe_ois_curve_config_path())
        assert len(ois_specs) > 0
        for ois_spec in ois_specs:
            assert isinstance(ois_spec.maturity_years, float)
            assert ois_spec.canonical_id.startswith("GBP.RATES.BOE.OIS.")


def test_default_treasury_configs_load() -> None:
    paths = default_treasury_config_paths()
    seen = 0
    for path in paths:
        if not path.exists():
            continue
        specs = load_treasury_series(path)
        assert len(specs) > 0
        seen += 1
    assert seen > 0


def test_spec_loader_rejects_non_object_root(tmp_path) -> None:  # type: ignore[no-untyped-def]
    bad = tmp_path / "bad.json"
    bad.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(TypeError, match="JSON object at the root"):
        load_fred_series(bad)


def test_spec_loader_rejects_non_list_series(tmp_path) -> None:  # type: ignore[no-untyped-def]
    bad = tmp_path / "bad.json"
    bad.write_text('{"series": "oops"}', encoding="utf-8")
    with pytest.raises(TypeError, match="'series' must be a list"):
        load_fred_series(bad)
