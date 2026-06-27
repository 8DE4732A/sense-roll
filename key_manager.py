"""Key manager with round-robin rotation and cooldown."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class KeyStats:
    """Usage statistics for a single API key."""

    key_prefix: str
    use_count: int = 0
    error_count: int = 0
    last_used_at: float | None = None
    last_error_at: float | None = None


class KeyManager:
    """Manages a list of API keys with round-robin rotation and cooldown.

    Thread-safe: all public methods acquire the internal lock.
    """

    def __init__(self, keys: list[str], cooldown_seconds: int = 60) -> None:
        if not keys:
            raise ValueError("At least one API key is required")

        self._keys = list(keys)
        self._index = 0
        self._cooldown_seconds = cooldown_seconds
        self._lock = threading.Lock()
        self._stats: dict[int, KeyStats] = {}
        for i, key in enumerate(self._keys):
            self._stats[i] = KeyStats(key_prefix=key[: min(8, len(key))])

    # ------------------------------------------------------------------
    # Private helpers (caller must hold _lock)
    # ------------------------------------------------------------------

    def _is_available(self, stat: KeyStats, now: float | None = None) -> bool:
        """Check whether *stat* is out of cooldown at time *now*."""
        if stat.last_error_at is None:
            return True
        return (now or time.time()) - stat.last_error_at >= self._cooldown_seconds

    def _advance_index(self) -> None:
        """Advance index by one (round-robin).  Caller must hold _lock."""
        self._index = (self._index + 1) % len(self._keys)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_current_key(self) -> str:
        """Return the current (active) API key.

        Thread-safe: does NOT mutate state.
        """
        with self._lock:
            return self._keys[self._index]

    def get_current_key_prefix(self) -> str:
        """Return the prefix of the current key (for safe display)."""
        with self._lock:
            return self._stats[self._index].key_prefix

    def rotate(self) -> str:
        """Advance to the next key (round-robin) and return it.

        Thread-safe: acquires the lock.
        """
        with self._lock:
            self._advance_index()
            return self._keys[self._index]

    def record_usage(self, key: str, is_error: bool = False) -> None:
        """Record a usage (and optionally an error) for the given key.

        Thread-safe: acquires the lock.
        """
        with self._lock:
            idx = self._keys.index(key)
            stat = self._stats[idx]
            stat.use_count += 1
            now = time.time()
            stat.last_used_at = now
            if is_error:
                stat.error_count += 1
                stat.last_error_at = now

    def is_key_available(self, key: str) -> bool:
        """Check if the given key is out of cooldown (i.e. usable).

        A key with no recorded error, or whose last error was more than
        ``cooldown_seconds`` ago, is considered available.
        """
        with self._lock:
            idx = self._keys.index(key)
            return self._is_available(self._stats[idx])

    def next_available_key(self) -> str | None:
        """Advance round-robin until an available key is found.

        Returns the key string, or ``None`` if *all* keys are in cooldown.
        The internal index is updated to the returned key; on ``None`` it is
        restored to the original position so the next request retries fairly.
        Thread-safe: acquires the lock.
        """
        with self._lock:
            start = self._index
            now = time.time()
            for _ in range(len(self._keys)):
                self._advance_index()
                if self._is_available(self._stats[self._index], now):
                    return self._keys[self._index]
            # All keys in cooldown – restore original position
            self._index = start
            return None

    def get_stats(self) -> list[dict]:
        """Return a list of per-key usage statistics (safe for API output).

        Only exposes the key prefix (first 8 characters) for security.

        Thread-safe: acquires the lock.
        """
        with self._lock:
            now = time.time()
            return [
                {
                    "key_prefix": stat.key_prefix,
                    "use_count": stat.use_count,
                    "error_count": stat.error_count,
                    "last_used_at": stat.last_used_at,
                    "last_error_at": stat.last_error_at,
                    "available": self._is_available(stat, now),
                }
                for stat in self._stats.values()
            ]

    def reset(self) -> None:
        """Reset the key index to 0 and clear all statistics.

        Thread-safe: acquires the lock.
        """
        with self._lock:
            self._index = 0
            for i, key in enumerate(self._keys):
                self._stats[i] = KeyStats(key_prefix=key[: min(8, len(key))])

    @property
    def total_keys(self) -> int:
        return len(self._keys)
