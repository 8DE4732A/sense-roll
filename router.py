"""Route definitions for sense-roll."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

router = APIRouter()


def _get_services():
    """Lazy-import to avoid circular dependency at module load time."""
    from main import key_manager, proxy_service  # noqa: PLC0415

    return proxy_service, key_manager


@router.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
    """Proxy ``/v1/chat/completions`` to the upstream with key rotation."""
    proxy_service, _ = _get_services()
    return await proxy_service.handle_proxy_request(request)


@router.get("/health")
async def health_check() -> JSONResponse:
    """Simple health-check endpoint."""
    return JSONResponse(
        content={"status": "ok", "timestamp": datetime.now().isoformat()}
    )


@router.get("/keys/status")
async def keys_status() -> JSONResponse:
    """Return the current key status and per-key usage statistics."""
    _, km = _get_services()
    stats = km.get_stats()
    return JSONResponse(
        content={
            "current_key": km.get_current_key_prefix(),
            "total_keys": km.total_keys,
            "keys": stats,
        }
    )