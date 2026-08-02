"""Per-provider key manager with (key, model) granularity cooldown."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class ModelCooldown:
    """Cooldown state for a specific (key, model) combination."""

    last_error_at: float
    cooldown_seconds: int


@dataclass
class KeyStats:
    """Usage statistics for a single API key."""

    key_prefix: str
    use_count: int = 0
    error_count: int = 0
    last_used_at: float | None = None


class ProviderKeyManager:
    """Manages a list of API keys for one provider.

    Tracks cooldown at (key_index, model) granularity so that a quota
    error on model A does not affect model B for the same key.

    Thread-safe: all public methods acquire the internal lock.
    """

    def __init__(
        self,
        provider_name: str,
        keys: list[str],
        strategy: str = "fill-first",
    ) -> None:
        if not keys:
            raise ValueError(f"Provider {provider_name!r} must have at least one key")
        self.provider_name = provider_name
        self._keys = list(keys)
        self._strategy = strategy
        self._rr_index = 0
        self._lock = threading.Lock()
        self._stats: dict[int, KeyStats] = {
            i: KeyStats(key_prefix=k[: min(8, len(k))])
            for i, k in enumerate(self._keys)
        }
        self._cooldowns: dict[tuple[int, str], ModelCooldown] = {}

    # ------------------------------------------------------------------
    # Private helpers (caller must hold _lock)
    # ------------------------------------------------------------------

    def _is_available(self, key_idx: int, model: str, now: float) -> bool:
        entry = self._cooldowns.get((key_idx, model))
        if entry is None:
            return True
        return now - entry.last_error_at >= entry.cooldown_seconds

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_key(self, model: str, attempted_keys: set[str] | None = None) -> str | None:
        """Return the next usable key for *model*, or None if all are cooling down."""
        if attempted_keys is None:
            attempted_keys = set()

        with self._lock:
            now = time.time()
            if self._strategy == "fill-first":
                for i, key in enumerate(self._keys):
                    if key not in attempted_keys and self._is_available(i, model, now):
                        return key
                return None
            else:
                # Round-robin: scan forward from current index, wrap once
                for _ in range(len(self._keys)):
                    self._rr_index = (self._rr_index + 1) % len(self._keys)
                    key = self._keys[self._rr_index]
                    if key not in attempted_keys and self._is_available(self._rr_index, model, now):
                        return key
                return None

    def merge_stats_from(self, old: "ProviderKeyManager") -> None:
        """Migrate per-key usage stats and unexpired cooldowns from *old* into self.

        Matching is done by key string (not index) so that re-ordering or adding/
        removing keys does not corrupt data.  Keys absent from *old* start fresh;
        keys removed from *old* are silently discarded.

        Thread-safe: acquires both locks in a consistent order (old then self).
        Callers must ensure *old* is no longer being written to after this call.
        """
        with old._lock:  # noqa: SLF001
            old_key_index: dict[str, int] = {k: i for i, k in enumerate(old._keys)}  # noqa: SLF001
            now = time.time()
            with self._lock:
                for i, key in enumerate(self._keys):
                    old_idx = old_key_index.get(key)
                    if old_idx is None:
                        continue
                    old_stat = old._stats[old_idx]  # noqa: SLF001
                    self._stats[i].use_count = old_stat.use_count
                    self._stats[i].error_count = old_stat.error_count
                    self._stats[i].last_used_at = old_stat.last_used_at
                    # Migrate unexpired (key, model) cooldowns
                    for (kidx, cd_model), cd in old._cooldowns.items():  # noqa: SLF001
                        if kidx != old_idx:
                            continue
                        if now - cd.last_error_at < cd.cooldown_seconds:
                            self._cooldowns[(i, cd_model)] = ModelCooldown(
                                last_error_at=cd.last_error_at,
                                cooldown_seconds=cd.cooldown_seconds,
                            )

    def record_error(self, key: str, model: str, cooldown_seconds: int) -> None:
        """Mark *key* as cooling down for *model* for *cooldown_seconds*."""
        with self._lock:
            idx = self._keys.index(key)
            self._stats[idx].error_count += 1
            self._stats[idx].last_used_at = time.time()
            self._cooldowns[(idx, model)] = ModelCooldown(
                last_error_at=time.time(),
                cooldown_seconds=cooldown_seconds,
            )

    def record_success(self, key: str, model: str) -> None:
        """Record a successful use of *key* for *model*."""
        with self._lock:
            idx = self._keys.index(key)
            self._stats[idx].use_count += 1
            self._stats[idx].last_used_at = time.time()

    def get_stats(self) -> dict:
        """Return provider stats including per-key model cooldown details."""
        with self._lock:
            now = time.time()
            keys_info = []
            for i, key in enumerate(self._keys):
                stat = self._stats[i]
                model_cooldowns: dict[str, dict] = {}
                for (kidx, model), cd in self._cooldowns.items():
                    if kidx != i:
                        continue
                    elapsed = now - cd.last_error_at
                    remaining = cd.cooldown_seconds - elapsed
                    if remaining > 0:
                        model_cooldowns[model] = {
                            "available": False,
                            "seconds_remaining": round(remaining, 1),
                        }
                    else:
                        model_cooldowns[model] = {"available": True}
                keys_info.append({
                    "key_prefix": stat.key_prefix,
                    "use_count": stat.use_count,
                    "error_count": stat.error_count,
                    "last_used_at": stat.last_used_at,
                    "model_cooldowns": model_cooldowns,
                })
            return {
                "provider": self.provider_name,
                "strategy": self._strategy,
                "keys": keys_info,
            }

    @property
    def total_keys(self) -> int:
        return len(self._keys)
