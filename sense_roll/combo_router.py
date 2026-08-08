"""Combo router: selects the next (provider, model) member from a named combo."""

from __future__ import annotations

import threading

from .config import ComboConfig


class ComboRouter:
    """Routes requests to (provider, model) pairs based on combo configuration.

    Thread-safe: uses a lock for round-robin index mutation.
    """

    def __init__(self, combos: list[ComboConfig]) -> None:
        self._combos: dict[str, ComboConfig] = {c.name: c for c in combos}
        # alias → canonical combo name mapping
        self._aliases: dict[str, str] = {
            alias: c.name
            for c in combos
            for alias in c.aliases
        }
        self._rr_indices: dict[str, int] = {c.name: 0 for c in combos}
        self._lock = threading.Lock()

    def _resolve(self, name: str) -> str:
        """Resolve alias to canonical combo name."""
        return self._aliases.get(name, name)

    def is_combo(self, name: str) -> bool:
        return self._resolve(name) in self._combos

    def get_combo(self, name: str) -> ComboConfig | None:
        return self._combos.get(self._resolve(name))

    def next_member(
        self,
        combo_name: str,
        attempted: set[tuple[str, str]],
    ) -> tuple[str, str] | None:
        """Return the next (provider, model) pair not yet in *attempted*.

        Returns None when all members have been attempted.
        """
        canonical = self._resolve(combo_name)
        combo = self._combos.get(canonical)
        if combo is None:
            return None

        members = combo.members
        if not members:
            return None

        with self._lock:
            if combo.strategy == "fill-first":
                for m in members:
                    pair = (m.provider, m.model)
                    if pair not in attempted:
                        return pair
                return None
            else:
                # Round-robin: advance from current index, scan all once
                idx = self._rr_indices[canonical]
                for _ in range(len(members)):
                    idx = (idx + 1) % len(members)
                    m = members[idx]
                    pair = (m.provider, m.model)
                    if pair not in attempted:
                        self._rr_indices[canonical] = idx
                        return pair
                return None

    def list_combos(self) -> list[str]:
        # Returns only canonical names, not aliases — aliases are transparent
        # routing shortcuts for clients, not separately advertised model IDs.
        return list(self._combos.keys())
