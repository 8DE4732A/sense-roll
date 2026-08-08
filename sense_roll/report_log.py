"""File-based detailed request logger with rotating gzip archives.

Architecture
------------
* A single background writer thread drains a `queue.Queue` and does all file
  writes.  The async/sync `log()` call only does a non-blocking `put_nowait`,
  so the event loop is never blocked.
* Each record is serialised as one JSON line (JSONL) in ``logs/requests.jsonl``
  inside the process's current working directory.
* When the active file reaches *max_bytes* the background thread compresses it
  to ``requests.jsonl.1.gz``, shifts older archives up by one, and removes any
  that would exceed *backup_count*.
* Reads (`read()`) walk files newest-first (active file reversed, then .1.gz,
  .2.gz, …) and short-circuit as soon as enough records are collected.

Security note
-------------
Records include the full HTTP headers sent to upstream providers, which contain
plaintext API keys.  The ``logs/`` directory MUST be in ``.gitignore`` and
access should be restricted to the local user.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import queue
import shutil
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_MAX_BYTES = 20 * 1024 * 1024  # 20 MB
_DEFAULT_BACKUP_COUNT = 10
_LOG_FILENAME = "requests.jsonl"


def _iter_lines_reversed(text: str):
    """Yield parsed JSON records from *text* in reverse line order, skipping unparseable lines."""
    for line in reversed(text.splitlines()):
        if not line.strip():
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


class ReportLogger:
    """Async-safe JSONL request logger with size-based rotation and gzip archives."""

    def __init__(
        self,
        log_dir: Path,
        max_bytes: int = _DEFAULT_MAX_BYTES,
        backup_count: int = _DEFAULT_BACKUP_COUNT,
    ) -> None:
        self._log_dir = Path(log_dir)
        self._max_bytes = max_bytes
        self._backup_count = backup_count
        self._active = self._log_dir / _LOG_FILENAME

        self._log_dir.mkdir(parents=True, exist_ok=True)

        # Open in append mode; creates the file if it doesn't exist.
        self._fh = self._active.open("a", encoding="utf-8")

        self._q: queue.Queue[dict | None] = queue.Queue(maxsize=10_000)
        self._dropped = 0
        self._lock = threading.Lock()  # protects _dropped counter

        self._thread = threading.Thread(
            target=self._run, daemon=True, name="report-logger-writer"
        )
        self._thread.start()

    # ------------------------------------------------------------------
    # Async-safe write path (called from the event loop or sync code)
    # ------------------------------------------------------------------

    def log(self, record: dict) -> None:
        """Enqueue *record* for writing.  Non-blocking; drops silently if full."""
        try:
            self._q.put_nowait(record)
        except queue.Full:
            with self._lock:
                self._dropped += 1

    @property
    def dropped_count(self) -> int:
        with self._lock:
            return self._dropped

    # ------------------------------------------------------------------
    # Background writer
    # ------------------------------------------------------------------

    def _run(self) -> None:
        while True:
            item = self._q.get()
            if item is None:  # shutdown sentinel
                break
            try:
                line = json.dumps(item, ensure_ascii=False) + "\n"
                self._fh.write(line)
                self._fh.flush()
                # Check size after each write; rotate if needed.
                if self._active.stat().st_size >= self._max_bytes:
                    self._rotate()
            except Exception:
                logger.exception("Failed to write report log record")

    def _rotate(self) -> None:
        """Compress the active log and shift archives, respecting backup_count."""
        try:
            # Close active file handle before manipulating it.
            self._fh.close()

            # Remove the oldest archive if at capacity.
            oldest = self._log_dir / f"{_LOG_FILENAME}.{self._backup_count}.gz"
            if oldest.exists():
                oldest.unlink()

            # Shift existing archives: .i.gz → .(i+1).gz, from highest downward.
            for i in range(self._backup_count - 1, 0, -1):
                src = self._log_dir / f"{_LOG_FILENAME}.{i}.gz"
                dst = self._log_dir / f"{_LOG_FILENAME}.{i + 1}.gz"
                if src.exists():
                    src.rename(dst)

            # Compress active file to .1.gz.
            archive = self._log_dir / f"{_LOG_FILENAME}.1.gz"
            with self._active.open("rb") as fin, gzip.open(archive, "wb") as fout:
                shutil.copyfileobj(fin, fout)

            # Truncate and reopen the active file.
            self._fh = self._active.open("w", encoding="utf-8")
        except Exception:
            logger.exception("Failed to rotate report log")
            # Reopen in append mode to recover; may result in a file that was
            # already partially read being appended to.
            try:
                self._fh = self._active.open("a", encoding="utf-8")
            except Exception:
                logger.exception("Failed to reopen report log after rotation error")

    # ------------------------------------------------------------------
    # Synchronous read helper (call via asyncio.to_thread from async code)
    # ------------------------------------------------------------------

    def read(
        self,
        limit: int = 20,
        offset: int = 0,
        success: bool | None = None,
    ) -> dict[str, Any]:
        """Return *limit* records starting at *offset*, newest first.

        Walks files in newest-to-oldest order:
          1. active ``requests.jsonl`` (reversed in memory for newest-first)
          2. ``requests.jsonl.1.gz``, ``requests.jsonl.2.gz``, …

        Returns ``{"items": [...], "has_more": bool}``.
        The total count is intentionally omitted — rolling logs are unbounded.
        """
        need = limit + 1  # fetch one extra to determine has_more
        collected: list[dict] = []
        skipped = 0

        for rec in self._iter_files():
            if success is not None and bool(rec.get("success")) != success:
                continue
            if skipped < offset:
                skipped += 1
                continue
            collected.append(rec)
            if len(collected) >= need:
                break

        has_more = len(collected) > limit
        return {"items": collected[:limit], "has_more": has_more}

    def _iter_files(self):
        """Yield parsed records from each log file, newest-to-oldest one record at a time."""
        if self._active.exists():
            try:
                text = self._active.read_text(encoding="utf-8", errors="replace")
                yield from _iter_lines_reversed(text)
            except Exception:
                logger.exception("Failed to read active report log")

        for i in range(1, self._backup_count + 1):
            archive = self._log_dir / f"{_LOG_FILENAME}.{i}.gz"
            if not archive.exists():
                break
            try:
                with gzip.open(archive, "rt", encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
                yield from _iter_lines_reversed(text)
            except Exception:
                logger.exception("Failed to read report log archive %s", archive)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Flush the queue, shut down the writer thread, and close the file."""
        self._q.put(None)  # shutdown sentinel
        self._thread.join(timeout=5)
        try:
            self._fh.close()
        except Exception:
            pass

    async def aclose(self) -> None:
        await asyncio.to_thread(self.close)


def report_log_dir() -> Path:
    """Return the default log directory: ``cwd/logs``."""
    return Path.cwd() / "logs"
