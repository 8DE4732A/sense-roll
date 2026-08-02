"""sense-roll: Multi-provider API gateway with combo routing and key rotation."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .config import ConfigError, load_config
from .state import GatewayState

logger = logging.getLogger(__name__)


class _SPAStaticFiles(StaticFiles):
    """StaticFiles subclass that returns index.html for unknown paths (SPA fallback)."""

    async def get_response(self, path: str, scope):  # type: ignore[override]
        try:
            return await super().get_response(path, scope)
        except Exception:
            return await super().get_response("index.html", scope)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config_path = os.environ.get("SENSE_ROLL_CONFIG", "config.yaml")
    try:
        config = load_config(config_path)
    except ConfigError as e:
        logger.fatal("Configuration error: %s", e)
        raise SystemExit(1) from e

    logger.info(
        "Loaded config: providers=%d, combos=%d",
        len(config.providers),
        len(config.combos),
    )
    for p in config.providers:
        formats = [ep.api_format for ep in p.api_endpoints]
        logger.info(
            "  Provider %r: formats=%s, keys=%d, rules=%d",
            p.name, formats, len(p.keys), len(p.health_check_rules),
        )

    from .db import Recorder, db_path_for_config  # noqa: PLC0415

    db_path = db_path_for_config(config_path)
    recorder = Recorder(db_path)
    logger.info("SQLite recorder initialised at %s", db_path)

    gateway = GatewayState(config, config_path, recorder=recorder)
    app.state.gateway = gateway

    yield

    await gateway.aclose()
    recorder.close()
    logger.info("sense-roll shut down")


app = FastAPI(
    title="sense-roll",
    description="Multi-provider API gateway with combo routing and key rotation",
    version="0.2.0",
    lifespan=lifespan,
)

from .admin_router import admin_router  # noqa: E402
from .router import router  # noqa: E402

# Admin API first — explicit paths beat the SPA static-file mount below
app.include_router(admin_router)
app.include_router(router)

# Mount the admin SPA once the web/dist directory exists.
# Admin API routes (registered in a future admin_router) must be included
# BEFORE this static mount so that explicit paths take priority.
_web_dist = Path(__file__).parent / "web" / "dist"
if _web_dist.exists():
    app.mount("/admin", _SPAStaticFiles(directory=str(_web_dist), html=True), name="admin")


def main() -> None:
    """Entry point for the ``sense-roll`` CLI."""
    import argparse

    parser = argparse.ArgumentParser(description="Multi-provider API gateway with combo routing")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("-p", "--port", type=int, default=8000, help="Bind port (default: 8000)")
    parser.add_argument("-c", "--config", default="config.yaml", help="Path to config yaml")
    args = parser.parse_args()

    os.environ["SENSE_ROLL_CONFIG"] = args.config

    uvicorn.run("sense_roll.main:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
