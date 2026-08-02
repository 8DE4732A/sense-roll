"""SQLite-backed request recorder using stdlib only.

Architecture
------------
* A single background writer thread drains a `queue.Queue` and does all
  INSERTs.  The async `record()` call only does a non-blocking `put_nowait`,
  so the event loop is never blocked.
* Read queries (`query_stats`, `query_list`) open a separate, read-only
  connection and are meant to be called via `asyncio.to_thread` from async
  code.  WAL journal mode allows concurrent readers while the writer is
  active.
* The DB file lives next to config.yaml (same directory).  The caller passes
  the config path at construction time.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS requests (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  ts               REAL    NOT NULL,
  combo            TEXT,
  provider         TEXT,
  model            TEXT,
  key_prefix       TEXT,
  api_format       TEXT,
  is_stream        INTEGER NOT NULL DEFAULT 0,
  status_code      INTEGER,
  success          INTEGER NOT NULL DEFAULT 0,
  matched_rule     TEXT,
  prompt_tokens    INTEGER,
  completion_tokens INTEGER,
  total_tokens     INTEGER,
  cache_read_tokens  INTEGER,
  cache_write_tokens INTEGER,
  duration_ms      INTEGER,
  error            TEXT
);
CREATE INDEX IF NOT EXISTS idx_requests_ts       ON requests(ts);
CREATE INDEX IF NOT EXISTS idx_requests_combo    ON requests(combo);
CREATE INDEX IF NOT EXISTS idx_requests_prov_mdl ON requests(provider, model);
"""

_MIGRATIONS = [
    "ALTER TABLE requests ADD COLUMN cache_read_tokens  INTEGER",
    "ALTER TABLE requests ADD COLUMN cache_write_tokens INTEGER",
]

_INSERT = """
INSERT INTO requests
  (ts, combo, provider, model, key_prefix, api_format, is_stream,
   status_code, success, matched_rule,
   prompt_tokens, completion_tokens, total_tokens,
   cache_read_tokens, cache_write_tokens,
   duration_ms, error)
VALUES
  (:ts, :combo, :provider, :model, :key_prefix, :api_format, :is_stream,
   :status_code, :success, :matched_rule,
   :prompt_tokens, :completion_tokens, :total_tokens,
   :cache_read_tokens, :cache_write_tokens,
   :duration_ms, :error)
"""


class Recorder:
    """Async-safe SQLite recorder with background write thread."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._q: queue.Queue[dict | None] = queue.Queue(maxsize=10_000)
        self._dropped = 0
        self._lock = threading.Lock()  # protects _dropped counter

        # Write connection — used only by the background thread.
        self._write_conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._write_conn.execute("PRAGMA journal_mode=WAL;")
        self._write_conn.executescript(_DDL)
        for migration in _MIGRATIONS:
            try:
                self._write_conn.execute(migration)
            except sqlite3.OperationalError:
                pass  # column already exists
        self._write_conn.commit()

        self._thread = threading.Thread(target=self._run, daemon=True, name="recorder-writer")
        self._thread.start()

    # ------------------------------------------------------------------
    # Async-safe write path (called from the event loop)
    # ------------------------------------------------------------------

    def record(self, row: dict) -> None:
        """Enqueue *row* for writing.  Non-blocking; drops silently if full."""
        try:
            self._q.put_nowait(row)
        except queue.Full:
            with self._lock:
                self._dropped += 1

    # ------------------------------------------------------------------
    # Background writer
    # ------------------------------------------------------------------

    def _run(self) -> None:
        while True:
            item = self._q.get()
            if item is None:  # shutdown sentinel
                break
            try:
                self._write_conn.execute(_INSERT, _fill_defaults(item))
                self._write_conn.commit()
            except Exception:
                logger.exception("Failed to write request record")

    # ------------------------------------------------------------------
    # Synchronous read helpers (call via asyncio.to_thread)
    # ------------------------------------------------------------------

    def _read_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True,
                               check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def query_stats(
        self,
        group_by: str = "combo",
        since: float | None = None,
        until: float | None = None,
    ) -> list[dict]:
        """Return per-group aggregated stats from the DB.

        group_by: one of "combo", "model", "provider", "key_prefix"
        """
        valid_groups = {"combo", "model", "provider", "key_prefix"}
        if group_by not in valid_groups:
            group_by = "combo"

        where, params = _build_time_filter(since, until)
        sql = f"""
            SELECT
                {group_by}                       AS group_key,
                COUNT(*)                         AS total,
                SUM(success)                     AS success_count,
                COUNT(*) - SUM(success)          AS error_count,
                SUM(COALESCE(total_tokens, 0))   AS total_tokens,
                SUM(COALESCE(prompt_tokens, 0))  AS prompt_tokens,
                SUM(COALESCE(completion_tokens,0)) AS completion_tokens,
                SUM(COALESCE(cache_read_tokens, 0))  AS cache_read_tokens,
                SUM(COALESCE(cache_write_tokens, 0)) AS cache_write_tokens,
                AVG(NULLIF(duration_ms, 0))      AS avg_duration_ms
            FROM requests
            {where}
            GROUP BY {group_by}
            ORDER BY total DESC
        """
        with self._read_conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def query_trend(
        self,
        bucket: str = "hour",
        since: float | None = None,
        until: float | None = None,
    ) -> list[dict]:
        """Return time-bucketed request counts and token sums."""
        bucket_seconds = {"hour": 3600, "day": 86400, "minute": 60}.get(bucket, 3600)
        where, params = _build_time_filter(since, until)
        sql = f"""
            SELECT
                CAST(ts / {bucket_seconds} AS INTEGER) * {bucket_seconds} AS bucket_ts,
                COUNT(*)                          AS total,
                SUM(success)                      AS success_count,
                SUM(COALESCE(total_tokens, 0))    AS total_tokens
            FROM requests
            {where}
            GROUP BY bucket_ts
            ORDER BY bucket_ts
        """
        with self._read_conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def query_list(
        self,
        limit: int = 50,
        offset: int = 0,
        combo: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        success: bool | None = None,
        since: float | None = None,
        until: float | None = None,
    ) -> dict[str, Any]:
        """Return a paginated list of raw request records plus total count."""
        filters: list[str] = []
        params: list[Any] = []

        if since is not None:
            filters.append("ts >= ?"); params.append(since)
        if until is not None:
            filters.append("ts <= ?"); params.append(until)
        if combo is not None:
            filters.append("combo = ?"); params.append(combo)
        if provider is not None:
            filters.append("provider = ?"); params.append(provider)
        if model is not None:
            filters.append("model = ?"); params.append(model)
        if success is not None:
            filters.append("success = ?"); params.append(1 if success else 0)

        where = ("WHERE " + " AND ".join(filters)) if filters else ""

        count_sql = f"SELECT COUNT(*) FROM requests {where}"
        list_sql = f"""
            SELECT * FROM requests {where}
            ORDER BY ts DESC
            LIMIT ? OFFSET ?
        """

        with self._read_conn() as conn:
            total = conn.execute(count_sql, params).fetchone()[0]
            rows = conn.execute(list_sql, params + [limit, offset]).fetchall()

        return {"total": total, "items": [dict(r) for r in rows]}

    @property
    def dropped_count(self) -> int:
        with self._lock:
            return self._dropped

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Flush the queue and shut down the writer thread."""
        self._q.put(None)
        self._thread.join(timeout=5)
        self._write_conn.close()

    async def aclose(self) -> None:
        await asyncio.to_thread(self.close)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _fill_defaults(row: dict) -> dict:
    defaults: dict[str, Any] = {
        "ts": time.time(),
        "combo": None, "provider": None, "model": None, "key_prefix": None,
        "api_format": None, "is_stream": 0, "status_code": None,
        "success": 0, "matched_rule": None,
        "prompt_tokens": None, "completion_tokens": None, "total_tokens": None,
        "cache_read_tokens": None, "cache_write_tokens": None,
        "duration_ms": None, "error": None,
    }
    defaults.update(row)
    return defaults


def _build_time_filter(
    since: float | None, until: float | None
) -> tuple[str, list[float]]:
    filters: list[str] = []
    params: list[float] = []
    if since is not None:
        filters.append("ts >= ?"); params.append(since)
    if until is not None:
        filters.append("ts <= ?"); params.append(until)
    where = ("WHERE " + " AND ".join(filters)) if filters else ""
    return where, params


def db_path_for_config(config_path: str | Path) -> Path:
    """Return the DB file path sibling to the given config file."""
    return Path(config_path).parent / "sense-roll.db"
