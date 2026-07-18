# quantra-common

Shared Python library used by every service in `quantra-backend`. This is the
first dependency every workspace member picks up; everything cross-cutting
lives here.

## Modules

| Module | Responsibility |
|---|---|
| `settings/` | `pydantic-settings`-backed `Settings` class. Loads env + `.env`; explicit `require_*` accessors for fields the orchestrator demands at startup. |
| `logging/` | `structlog` config (JSON in prod/staging, pretty in dev) and a pure-ASGI `RequestIdMiddleware` that threads `X-Request-Id` into `structlog.contextvars`. |
| `db/` | Async SQLAlchemy 2.x engine factories (`make_app_engine`, `make_md_engine`), async session context manager, Alembic discovery (single env, two version tables per D6). |
| `auth/` | `verify_firebase_id_token`, `verify_api_key` (against an injected `ApiKeyLookup`), `AuthContext`, FastAPI `require_auth()` dependency. |
| `md_client/` | Async `httpx` client for the read-only MD service. Retries on 5xx + transport errors, structured exceptions, pluggable `QuoteCache` (in-process LRU default). |
| `engine_client/` | Async interface over the pricing engine's gRPC surface. Ships with a `StubEngineClient` that raises `NotImplementedError` — the real grpc-aio implementation lands in `engine_client_real`. |
| `types/` | Pydantic v2 market-data entities and enums shared across services: `Quote`, `Snapshot`, `ResolvedQuote`, `ResolutionMode`, `QuoteKind`, etc. Curve models live per-product in the orchestrator (`pricing/<product>/models.py`). |

## Stubs

`engine_client.StubEngineClient` is the default ship — concrete grpc/aio
wiring is intentionally out of scope for the shared-infra plan so this
library doesn't take `grpcio` and the engine's generated FlatBuffer
bindings as runtime dependencies. Every call raises `NotImplementedError`
with a message pointing at the future `engine_client_real` subplan.

## Tests

```
uv run pytest packages/common/tests
```

Each module has at least one happy-path test and one error-path test, per
the plan's acceptance criteria.
