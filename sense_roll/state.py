"""GatewayState: holds the live, atomically-replaceable service bundle.

Hot-reload design
-----------------
All proxy requests read `state.service` once at the top of `_handle_request`
and keep that local reference for the entire request (including streaming
`_forward`).  `reload()` only replaces `self._service` (a single attribute
assignment, which is atomic under the GIL), so in-flight requests continue
using the old snapshot while new requests pick up the new one.

The shared `httpx.AsyncClient` is owned by GatewayState (not ProxyService)
and is reused across reloads to avoid tearing down keep-alive connections.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import httpx
import yaml

from .combo_router import ComboRouter
from .config import AppConfig, dump_config
from .key_manager import ProviderKeyManager
from .proxy import ProxyService


class GatewayState:
    """Owns the shared httpx client and the hot-swappable ProxyService snapshot."""

    def __init__(
        self,
        config: AppConfig,
        config_path: str | Path,
        recorder=None,  # db.Recorder | None — optional to avoid circular import at type-check
    ) -> None:
        self._lock = threading.Lock()           # guards _service replacement only
        self._config_path = Path(config_path)
        self._recorder = recorder
        # The httpx client is shared across reloads so existing keep-alive
        # connections are not dropped on every config save.
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(120.0))
        self._service = self._build_service(config)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_service(
        self,
        config: AppConfig,
        prev_kms: dict[str, ProviderKeyManager] | None = None,
    ) -> ProxyService:
        kms: dict[str, ProviderKeyManager] = {}
        for p in config.providers:
            km = ProviderKeyManager(
                p.name, [k.key for k in p.keys], strategy=p.key_strategy
            )
            if prev_kms and p.name in prev_kms:
                km.merge_stats_from(prev_kms[p.name])
            kms[p.name] = km
        return ProxyService(
            config,
            kms,
            ComboRouter(config.combos),
            client=self._client,
            recorder=self._recorder,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def service(self) -> ProxyService:
        """Return the current ProxyService snapshot (take once per request)."""
        return self._service

    def reload(self, new_config: AppConfig) -> None:
        """Atomically replace the ProxyService with one built from *new_config*.

        In-flight requests keep the old snapshot; new requests get the new one.
        The shared httpx client is NOT replaced.
        """
        with self._lock:
            prev_kms = self._service.provider_key_managers
            new_svc = self._build_service(new_config, prev_kms=prev_kms)
            self._service = new_svc  # atomic under GIL

    def save_and_reload(self, new_config: AppConfig) -> None:
        """Validate, atomically write YAML, then hot-reload.

        Steps:
        1. dump_config → yaml text
        2. Write to a temp file in the same directory, then os.replace (atomic)
        3. reload()

        If reload raises (e.g. jsonpath compile error), the YAML has already
        been written but the in-memory service is unchanged — a subsequent
        restart will pick up the new file.  This is the safer trade-off.
        """
        yaml_text = yaml.safe_dump(
            dump_config(new_config),
            allow_unicode=True,
            sort_keys=False,
        )
        tmp_path = self._config_path.with_suffix(".yaml.tmp")
        tmp_path.write_text(yaml_text, encoding="utf-8")
        os.replace(tmp_path, self._config_path)  # atomic on POSIX + Windows
        self.reload(new_config)

    async def aclose(self) -> None:
        """Close the shared httpx client (call once on lifespan shutdown)."""
        await self._client.aclose()
