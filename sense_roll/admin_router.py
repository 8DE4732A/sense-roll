"""Admin API router — no authentication (local use only).

Mounted at /admin/api by main.py BEFORE the /admin static-file mount so that
explicit API paths take priority over the SPA fallback.

Endpoints
---------
GET  /admin/api/config              Return current config (includes key plaintext — local only)
PUT  /admin/api/config              Validate, write YAML, hot-reload
GET  /admin/api/stats/keys          Real-time key pool status (same as /keys/status)
GET  /admin/api/stats/summary       Aggregated DB stats
GET  /admin/api/stats/trend         Time-bucketed request counts / tokens
GET  /admin/api/requests            Paginated request log
GET  /admin/api/health              Process / DB health
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import sys
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from .config import ConfigError, build_config, dump_config

admin_router = APIRouter(prefix="/admin/api")

try:
    _VERSION = importlib.metadata.version("sense-roll")
except importlib.metadata.PackageNotFoundError:
    _VERSION = "dev"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _gateway(request: Request):
    gw = getattr(request.app.state, "gateway", None)
    if gw is None:
        raise HTTPException(status_code=503, detail="gateway not initialised")
    return gw


def _recorder(request: Request):
    gw = _gateway(request)
    rec = getattr(gw, "_recorder", None)
    if rec is None:
        raise HTTPException(status_code=503, detail="recorder not initialised")
    return rec


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@admin_router.get("/config")
async def get_config(request: Request) -> JSONResponse:
    """Return the current in-memory config as a plain dict (includes key values)."""
    svc = _gateway(request).service
    return JSONResponse(content=dump_config(svc.config))


@admin_router.put("/config")
async def put_config(request: Request) -> JSONResponse:
    """Validate the request body, atomically write to config.yaml, then hot-reload."""
    body: Any = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be a JSON object")

    try:
        new_config = build_config(body)
    except ConfigError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})

    gw = _gateway(request)
    try:
        await asyncio.to_thread(gw.save_and_reload, new_config)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"reload failed: {e}"})

    return JSONResponse(content=dump_config(new_config))


# ---------------------------------------------------------------------------
# Stats — real-time key status
# ---------------------------------------------------------------------------

@admin_router.get("/stats/keys")
async def stats_keys(request: Request) -> JSONResponse:
    """Return current combo list and per-provider real-time key stats."""
    svc = _gateway(request).service
    return JSONResponse(content={
        "combos": svc.combo_router.list_combos(),
        "providers": [km.get_stats() for km in svc.provider_key_managers.values()],
    })


# ---------------------------------------------------------------------------
# Stats — aggregated DB queries
# ---------------------------------------------------------------------------

@admin_router.get("/stats/summary")
async def stats_summary(
    request: Request,
    group_by: str = "combo",
    since: float | None = None,
    until: float | None = None,
) -> JSONResponse:
    """Return per-group aggregated stats from the request log."""
    rec = _recorder(request)
    rows = await asyncio.to_thread(rec.query_stats, group_by, since, until)
    return JSONResponse(content={"data": rows, "group_by": group_by})


@admin_router.get("/stats/trend")
async def stats_trend(
    request: Request,
    bucket: str = "hour",
    since: float | None = None,
    until: float | None = None,
) -> JSONResponse:
    """Return time-bucketed request counts and token sums."""
    rec = _recorder(request)
    rows = await asyncio.to_thread(rec.query_trend, bucket, since, until)
    return JSONResponse(content={"data": rows, "bucket": bucket})


# ---------------------------------------------------------------------------
# Request log
# ---------------------------------------------------------------------------

@admin_router.get("/requests")
async def list_requests(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    combo: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    success: bool | None = None,
    since: float | None = None,
    until: float | None = None,
) -> JSONResponse:
    """Return a paginated list of recorded requests."""
    rec = _recorder(request)
    result = await asyncio.to_thread(
        rec.query_list,
        limit, offset, combo, provider, model, success, since, until,
    )
    return JSONResponse(content=result)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@admin_router.get("/info")
async def admin_info(request: Request) -> JSONResponse:
    """Return app version and available model/combo information."""
    svc = _gateway(request).service
    combos = []
    for c in svc.config.combos:
        combos.append({
            "name": c.name,
            "aliases": c.aliases,
            "api_formats": c.api_formats,
            "strategy": c.strategy,
            "members": [
                {"provider": m.provider, "model": m.model}
                for m in c.members
            ],
        })

    providers = []
    for p in svc.config.providers:
        providers.append({
            "name": p.name,
            "api_formats": [ep.api_format for ep in p.api_endpoints],
            "key_count": len(p.keys),
            "strategy": p.key_strategy,
        })

    return JSONResponse(content={
        "version": _VERSION,
        "python": sys.version.split()[0],
        "combos": combos,
        "providers": providers,
    })


@admin_router.get("/health")
async def admin_health(request: Request) -> JSONResponse:
    """Return a basic health snapshot."""
    gw = _gateway(request)
    svc = gw.service

    rec = getattr(gw, "_recorder", None)
    db_info: dict[str, Any] = {}
    if rec is not None:
        db_info = {
            "queue_size": rec._q.qsize(),  # noqa: SLF001
            "dropped_count": rec.dropped_count,
            "db_path": rec._db_path,        # noqa: SLF001
        }

    return JSONResponse(content={
        "status": "ok",
        "python": sys.version,
        "config": {
            "providers": len(svc.config.providers),
            "combos": len(svc.config.combos),
        },
        "db": db_info,
    })
