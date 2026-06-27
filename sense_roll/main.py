"""sense-roll: API Key Rotation Proxy for token.sensenova.cn.

Usage:
    uvicorn main:app --reload --port 8000
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from .config import ConfigError, load_config
from .key_manager import KeyManager
from .proxy import ProxyService

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level singletons (initialised during lifespan)
# ---------------------------------------------------------------------------
key_manager: KeyManager | None = None
proxy_service: ProxyService | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: load config, initialise services.  Shutdown: close client."""
    global key_manager, proxy_service

    logging.basicConfig(
        level=getattr(logging, "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config_path = os.environ.get("SENSE_ROLL_CONFIG", "config.yaml")
    try:
        config = load_config(config_path)
    except ConfigError as e:
        logger.fatal("Configuration error: %s", e)
        raise SystemExit(1) from e

    logger.info(
        "Loaded config: target=%s, keys=%d, rules=%d, max_retries=%d",
        config.proxy.target_url,
        len(config.keys),
        len(config.rotation_rules),
        config.proxy.max_retries,
    )

    key_manager = KeyManager(
        [k.key for k in config.keys],
        cooldown_seconds=config.proxy.key_cooldown_seconds,
        strategy=config.routing.strategy,
    )
    proxy_service = ProxyService(config, key_manager)

    yield

    # Shutdown
    if proxy_service is not None:
        await proxy_service.aclose()
    logger.info("sense-roll shut down")


app = FastAPI(
    title="sense-roll",
    description="API Key Rotation Proxy for token.sensenova.cn",
    version="0.1.0",
    lifespan=lifespan,
)

# Import router after app creation to break the circular dependency:
#   main -> router -> main (lazy import in router._get_services)
from .router import router  # noqa: E402

app.include_router(router)


def main() -> None:
    """Entry point for ``sense-roll`` CLI."""
    import argparse
    parser = argparse.ArgumentParser(description="API Key Rotation Proxy for token.sensenova.cn")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("-p", "--port", type=int, default=8000, help="Bind port (default: 8000)")
    parser.add_argument("-c", "--config", default="config.yaml", help="Path to config yaml file (default: config.yaml)")
    args = parser.parse_args()

    os.environ["SENSE_ROLL_CONFIG"] = args.config

    uvicorn.run(
        "sense_roll.main:app",
        host=args.host,
        port=args.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
