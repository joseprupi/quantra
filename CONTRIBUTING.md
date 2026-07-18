# Contributing to Quantra

Thanks for your interest in contributing. This document covers the practical
workflow and the contributor license agreement.

## Development workflow

The repository is a monorepo with two independent subtrees, each with its own
toolchain and test gate. A change must keep its subtree's gate green.

### `backend/` (Python — orchestrator, market_data, md_ingester)

```bash
cd backend
uv sync --all-packages --group dev
uv run ruff check . && uv run ruff format --check .
uv run mypy packages services
uv run pytest
```

### `frontend/` (React/TypeScript portal)

```bash
cd frontend
npm install
npm run lint
npx tsc --noEmit
npx vitest run
npm run build
```

CI (`.github/workflows/ci.yml`) runs both gates automatically, path-filtered
so a PR touching only one subtree runs only that subtree's gate.

For pricing-affecting changes, please also verify against a live stack
(`docker compose up -d` from the repo root) — the hermetic tests use engine
stubs and cannot catch wire-contract or numerical regressions.

## Contributor License Agreement (CLA)

By submitting a pull request or otherwise contributing code, documentation,
or other material to this project, you agree that:

1. You are the author of your contribution (or otherwise have the right to
   submit it), and you license it to the project under the project's license
   (AGPL-3.0).
2. In addition, you grant the project maintainer (**Josep Rubió Piqué**) a
   **perpetual, irrevocable, worldwide, royalty-free copyright license** to
   use, reproduce, modify, distribute, and sublicense your contribution —
   **including the right to relicense it under other license terms**.

Point 2 is what keeps the project sustainable: it preserves the maintainer's
ability to offer the codebase under licenses other than the AGPL (for
example, commercial licenses) without having to track down every past
contributor. Your contribution always remains available under the AGPL in
this repository; the grant is additional, not a replacement.

Opening a pull request constitutes agreement to these terms. (This is a
lightweight click-through CLA; an automated CLA check may be added later.)

## Code of conduct

All participation in this project is governed by the
[Code of Conduct](CODE_OF_CONDUCT.md).
