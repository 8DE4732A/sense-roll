"""Route definitions for sense-roll."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

router = APIRouter()


def _get_proxy(request: Request):
    """Return the current ProxyService snapshot from app.state.gateway."""
    gateway = getattr(request.app.state, "gateway", None)
    if gateway is None:
        raise HTTPException(status_code=503, detail="service is not initialised")
    return gateway.service


@router.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
    """Proxy OpenAI-format requests with combo routing and key rotation."""
    return await _get_proxy(request).handle_openai_request(request)


@router.post("/v1/messages")
async def messages(request: Request) -> Response:
    """Proxy Anthropic-format requests with combo routing and key rotation."""
    return await _get_proxy(request).handle_anthropic_request(request)


@router.post("/v1/responses")
async def responses(request: Request) -> Response:
    """Proxy OpenAI Responses API requests with combo routing and key rotation."""
    return await _get_proxy(request).handle_openai_responses_request(request)


@router.post("/v1/images/generations")
async def images_generations(request: Request) -> Response:
    """Proxy OpenAI Images generations requests with combo routing and key rotation."""
    return await _get_proxy(request).handle_openai_images_request(request)


@router.get("/health")
async def health_check() -> JSONResponse:
    """Simple health-check endpoint."""
    return JSONResponse(content={"status": "ok", "timestamp": datetime.now().isoformat()})


@router.get("/keys/status")
async def keys_status(request: Request) -> JSONResponse:
    """Return combo list and per-provider key statistics."""
    svc = _get_proxy(request)
    return JSONResponse(content={
        "combos": svc.combo_router.list_combos(),
        "providers": [
            km.get_stats()
            for km in svc.provider_key_managers.values()
        ],
    })
