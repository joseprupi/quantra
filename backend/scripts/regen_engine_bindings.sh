#!/usr/bin/env bash
# Regenerate the vendored engine FlatBuffers Python bindings.
#
# Run it whenever the engine repo's `flatbuffers/fbs/*.fbs` schema
# changes; the diff it produces is the orchestrator-side delta of
# the upstream schema bump.
#
# ============================= WARNING ======================================
# THIS SCRIPT CLOBBERS TWO HAND-PATCHED FILES:
#
#   _generated/quantra/CDS.py
#   _generated/quantra/CreditCurveSpec.py
#
# Both carry presence-based emission patches (`CDS.upfront` is only written
# for a genuine upfront leg; `CreditCurveSpec.flat_hazard_rate` is left
# absent on the bootstrap path). Regenerating from bindings that emit these
# fields unconditionally REINTRODUCES a CDS NPV sign-flip and a silently
# ignored credit-curve bootstrap. After a regen you MUST either:
#   * re-apply the two patches, or
#   * regenerate from engine >= 0.2.0 bindings, which are natively
#     presence-based for these fields,
# and in both cases re-run the orchestrator engine-io wire tests
# (services/orchestrator/tests/test_*_engine_io.py — the golden-hex byte
# snapshots will fail loudly on a regression).
# ============================================================================
#
# Inputs:
#
#   QUANTRA_ENGINE_REPO  — path to a checkout of the engine repo
#                          (clone github.com/joseprupi/quantraserver).
#                          Required; the script errors if unset.
#
# Steps:
#
# 1. Ensure the engine repo has up-to-date generated bindings.
#    The engine's own `scripts/generate_schemas.sh` runs `flatc
#    --python` over `flatbuffers/fbs/*.fbs` and writes the output
#    to `flatbuffers/python/quantra/`. This script does NOT
#    invoke `flatc` itself — it consumes whatever the engine repo
#    currently ships, which is the contract: the engine team owns
#    schema codegen, the orchestrator team owns vendoring.
#
# 2. Wipe the orchestrator's local copy and re-copy.
#
# 3. Rewrite the bindings' internal cross-imports from
#    `from quantra.X import Y` to
#    `from quantra_common.engine_client._generated.quantra.X import Y`
#    so the modules live as first-class members of the
#    `quantra_common.engine_client._generated.quantra` package
#    (no top-level `quantra` namespace pollution).
#
# 4. Smoke-import the bindings, then run the orchestrator wire tests
#    (`uv run pytest services/orchestrator/tests/ -k engine_io`) to
#    confirm the golden-hex byte snapshots still pass.
#
# Usage:
#
#   QUANTRA_ENGINE_REPO=/path/to/quantraserver bash scripts/regen_engine_bindings.sh
#
# This script is idempotent: a second run with no schema changes
# leaves the working tree clean.

set -euo pipefail

if [ -z "${QUANTRA_ENGINE_REPO:-}" ]; then
  echo "error: QUANTRA_ENGINE_REPO is unset." >&2
  echo "       Clone github.com/joseprupi/quantraserver and point" >&2
  echo "       QUANTRA_ENGINE_REPO at the checkout root." >&2
  exit 2
fi

ENGINE_REPO="${QUANTRA_ENGINE_REPO}"
SOURCE_DIR="${ENGINE_REPO}/flatbuffers/python/quantra"
TARGET_PARENT="$(cd "$(dirname "$0")/.." && pwd)/packages/common/src/quantra_common/engine_client/_generated"
TARGET_DIR="${TARGET_PARENT}/quantra"
NEW_PKG_PREFIX="quantra_common.engine_client._generated.quantra"

if [ ! -d "${SOURCE_DIR}" ]; then
  echo "error: engine bindings source dir not found at ${SOURCE_DIR}" >&2
  echo "       set QUANTRA_ENGINE_REPO to point at the engine repo root" >&2
  exit 2
fi

echo "==> Regenerating engine bindings"
echo "    source: ${SOURCE_DIR}"
echo "    target: ${TARGET_DIR}"
echo "    pkg:    ${NEW_PKG_PREFIX}"
echo ""
echo "!!! Reminder: this clobbers the hand-patched CDS.py and"
echo "!!! CreditCurveSpec.py (see the WARNING in this script's header)."

mkdir -p "${TARGET_PARENT}"
rm -rf "${TARGET_DIR}"
cp -r "${SOURCE_DIR}" "${TARGET_DIR}"

echo "==> Rewriting cross-imports"
TARGET_DIR="${TARGET_DIR}" NEW_PKG_PREFIX="${NEW_PKG_PREFIX}" python3 <<'PY'
import os
import re
from pathlib import Path

base = Path(os.environ["TARGET_DIR"])
new_pkg = os.environ["NEW_PKG_PREFIX"]
count = 0
for py in base.rglob("*.py"):
    src = py.read_text()
    out = re.sub(r"\bfrom quantra\.(\w)", rf"from {new_pkg}.\1", src)
    out = re.sub(r"\bimport quantra\.(\w)", rf"import {new_pkg}.\1", out)
    if out != src:
        py.write_text(out)
        count += 1
print(f"   rewrote {count} files")
PY

echo "==> Injecting missing object-API cross-module imports"
# Upstream flatc's python object API references sibling ``<Name>T`` classes
# (``XT.InitFromObj`` in ``_UnPack``, ``XT.InitFromBuf`` in union
# ``*Creator`` functions) WITHOUT importing them — the codegen assumes a
# flat single-module layout. Inject a deferred ``from <pkg>.<Name> import
# <Name>T`` at the top of each function body that references one (deferred,
# so recursive schema cycles cannot deadlock module import). Idempotent:
# functions that already import the name are left untouched.
TARGET_DIR="${TARGET_DIR}" NEW_PKG_PREFIX="${NEW_PKG_PREFIX}" python3 <<'PY'
import os
import re
from pathlib import Path

base = Path(os.environ["TARGET_DIR"])
new_pkg = os.environ["NEW_PKG_PREFIX"]
sibling_stems = {p.stem for p in base.glob("*.py") if p.stem != "__init__"}
touched = 0

for py in sorted(base.glob("*.py")):
    lines = py.read_text().splitlines(keepends=True)
    # Locate function defs and their body extents by indentation.
    defs = []  # (def_line_idx, body_indent)
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)def \w+\(", line)
        if m:
            defs.append((i, m.group(1) + "    "))
    if not defs:
        continue
    out = []
    changed = False
    for d, (start, indent) in enumerate(defs):
        end = defs[d + 1][0] if d + 1 < len(defs) else len(lines)
        body = lines[start + 1 : end]
        body_text = "".join(body)
        t_refs = sorted(
            {
                name
                for name in re.findall(r"\b(\w+)T\.(?:InitFromObj|InitFromBuf)\(", body_text)
                if name in sibling_stems and name != py.stem
            }
        )
        creator_refs = sorted(
            {
                name
                for name in re.findall(r"\b(\w+)Creator\(", body_text)
                if name in sibling_stems and name != py.stem
            }
        )
        # Reader-class constructor references (``obj = SwapLegFlow()``):
        # upstream flatc only emits the deferred import in the FIRST accessor
        # that references a sibling reader; later accessors in the same file
        # reference the bare name with no import in their own scope.
        reader_refs = sorted(
            {
                name
                for name in re.findall(r"\b([A-Z]\w*)\(\)", body_text)
                if name in sibling_stems and name != py.stem
            }
        )
        imports = [
            f"{indent}from {new_pkg}.{n} import {n}T\n"
            for n in t_refs
            if f"import {n}T" not in body_text
        ] + [
            f"{indent}from {new_pkg}.{n} import {n}Creator\n"
            for n in creator_refs
            if f"import {n}Creator" not in body_text
        ] + [
            f"{indent}from {new_pkg}.{n} import {n}\n"
            for n in reader_refs
            if not re.search(rf"import {n}\b(?!T)", body_text)
        ]
        if imports:
            changed = True
            out.append((start + 1, imports))
    if changed:
        for insert_at, imports in reversed(out):
            lines[insert_at:insert_at] = imports
        py.write_text("".join(lines))
        touched += 1
print(f"   injected deferred imports into {touched} files")
PY

echo "==> Smoke-importing the bindings (uv run python)"
cd "$(dirname "$0")/.."
uv run python -c "
from quantra_common.engine_client._generated.quantra.PriceVanillaSwapRequest import PriceVanillaSwapRequestT
from quantra_common.engine_client._generated.quantra.VanillaSwapResponse import VanillaSwapResponse
from quantra_common.engine_client._generated.quantra.enums.SwapType import SwapType
assert SwapType.Payer == 0, SwapType.Payer
print('OK')
"

echo "==> Done. Now:"
echo "    1. git diff --stat packages/common/src/quantra_common/engine_client/_generated"
echo "    2. Re-apply (or verify natively present) the CDS.py / CreditCurveSpec.py patches"
echo "    3. uv run pytest services/orchestrator/tests/ -k engine_io"
