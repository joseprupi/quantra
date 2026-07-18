"""Liveness probe."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter

from quantra_market_data.schemas import HealthResponse

router = APIRouter()


@router.get("/health", response_model=HealthResponse, tags=["public"])
def health() -> HealthResponse:
    """Liveness — never touches the database."""

    return HealthResponse(
        status="ok",
        service="market-data-api",
        utc_time=datetime.now(UTC).isoformat(),
    )
