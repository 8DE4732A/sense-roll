"""Tests for report_log.ReportLogger — write, rotation, gzip archives, and read-back."""

from __future__ import annotations

import gzip
import tempfile
import time
import unittest
from pathlib import Path

from sense_roll.report_log import ReportLogger, report_log_dir


def _make_logger(tmp_dir: str, max_bytes: int = 1024 * 1024, backup_count: int = 10) -> ReportLogger:
    return ReportLogger(Path(tmp_dir), max_bytes=max_bytes, backup_count=backup_count)


def _flush(rl: ReportLogger, timeout: float = 2.0) -> None:
    """Wait until the write queue is empty."""
    import time as _time
    deadline = _time.monotonic() + timeout
    while not rl._q.empty():  # noqa: SLF001
        if _time.monotonic() > deadline:
            raise TimeoutError("queue did not drain in time")
        _time.sleep(0.01)
    _time.sleep(0.05)


class ReportLoggerWriteTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.rl = _make_logger(self._tmp.name)

    def tearDown(self):
        self.rl.close()
        self._tmp.cleanup()

    def test_log_and_read_back(self) -> None:
        record = {
            "ts": time.time(), "combo": "fast", "provider": "sn", "model": "flash",
            "api_format": "openai", "is_stream": False, "status_code": 200,
            "success": True, "duration_ms": 100,
            "request": {"client": {"method": "POST"}, "upstream": {"url": "https://x/v1"}},
            "response": {"status_code": 200, "headers": {}, "body": {"id": "chatcmpl-1"}},
        }
        self.rl.log(record)
        _flush(self.rl)

        result = self.rl.read(limit=10)
        self.assertEqual(len(result["items"]), 1)
        self.assertFalse(result["has_more"])
        self.assertEqual(result["items"][0]["combo"], "fast")
        self.assertEqual(result["items"][0]["duration_ms"], 100)

    def test_multiple_records_newest_first(self) -> None:
        for i in range(3):
            self.rl.log({"ts": float(i), "idx": i})
        _flush(self.rl)

        result = self.rl.read(limit=10)
        items = result["items"]
        self.assertEqual(len(items), 3)
        # Newest first: ts=2, ts=1, ts=0
        self.assertEqual(items[0]["idx"], 2)
        self.assertEqual(items[1]["idx"], 1)
        self.assertEqual(items[2]["idx"], 0)

    def test_pagination_limit_offset(self) -> None:
        for i in range(5):
            self.rl.log({"ts": float(i), "idx": i})
        _flush(self.rl)

        page1 = self.rl.read(limit=2, offset=0)
        self.assertEqual(len(page1["items"]), 2)
        self.assertTrue(page1["has_more"])
        self.assertEqual(page1["items"][0]["idx"], 4)
        self.assertEqual(page1["items"][1]["idx"], 3)

        page2 = self.rl.read(limit=2, offset=2)
        self.assertEqual(len(page2["items"]), 2)
        self.assertTrue(page2["has_more"])

        page3 = self.rl.read(limit=2, offset=4)
        self.assertEqual(len(page3["items"]), 1)
        self.assertFalse(page3["has_more"])

    def test_success_filter(self) -> None:
        self.rl.log({"ts": 1.0, "success": True, "idx": 0})
        self.rl.log({"ts": 2.0, "success": False, "idx": 1})
        self.rl.log({"ts": 3.0, "success": True, "idx": 2})
        _flush(self.rl)

        ok = self.rl.read(limit=10, success=True)
        fail = self.rl.read(limit=10, success=False)
        self.assertEqual(len(ok["items"]), 2)
        self.assertEqual(len(fail["items"]), 1)

    def test_dropped_counter_when_queue_full(self) -> None:
        import queue as _q
        old_q = self.rl._q  # noqa: SLF001
        small_q: _q.Queue = _q.Queue(maxsize=1)
        small_q.put({"ts": time.time()})
        self.rl._q = small_q  # noqa: SLF001
        self.rl.log({"ts": time.time()})  # should be dropped
        self.assertGreater(self.rl.dropped_count, 0)
        self.rl._q = old_q  # noqa: SLF001


class ReportLoggerRotationTests(unittest.TestCase):
    """Tests that trigger rotation by using a tiny max_bytes threshold."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._log_dir = Path(self._tmp.name)
        # Each JSON line will be ~50-100 bytes; set threshold low to force rotation.
        self.rl = _make_logger(self._tmp.name, max_bytes=200, backup_count=3)

    def tearDown(self):
        self.rl.close()
        self._tmp.cleanup()

    def _flush(self) -> None:
        _flush(self.rl)

    def test_rotation_creates_gz_archive(self) -> None:
        # Write enough to trigger at least one rotation.
        for i in range(10):
            self.rl.log({"ts": float(i), "data": "x" * 30, "idx": i})
        self._flush()

        archives = sorted(self._log_dir.glob("requests.jsonl.*.gz"))
        self.assertGreater(len(archives), 0, "expected at least one .gz archive")

    def test_rotation_respects_backup_count(self) -> None:
        # Write many records to trigger multiple rotations.
        for i in range(50):
            self.rl.log({"ts": float(i), "data": "x" * 30, "idx": i})
        self._flush()

        archives = sorted(self._log_dir.glob("requests.jsonl.*.gz"))
        self.assertLessEqual(
            len(archives), 3,
            f"backup_count=3 but found {len(archives)} archives: {archives}",
        )

    def test_gz_archives_are_readable(self) -> None:
        for i in range(20):
            self.rl.log({"ts": float(i), "data": "x" * 30, "idx": i})
        self._flush()

        archives = sorted(self._log_dir.glob("requests.jsonl.*.gz"))
        self.assertGreater(len(archives), 0)
        # Each archive must be a valid gzip file containing valid JSONL.
        for arch in archives:
            with gzip.open(arch, "rt", encoding="utf-8") as fh:
                lines = [l for l in fh.read().splitlines() if l.strip()]
            self.assertGreater(len(lines), 0, f"archive {arch} is empty")
            import json
            for line in lines:
                json.loads(line)  # must not raise

    def test_read_spans_active_and_archives(self) -> None:
        """All written records should be recoverable via read() across rotations."""
        written_indices = set()
        for i in range(40):
            self.rl.log({"ts": float(i), "idx": i})
            written_indices.add(i)
        self._flush()

        # Read all (use large limit + multiple pages)
        found_indices = set()
        offset = 0
        limit = 20
        while True:
            result = self.rl.read(limit=limit, offset=offset)
            for rec in result["items"]:
                found_indices.add(rec["idx"])
            if not result["has_more"]:
                break
            offset += limit

        # With backup_count=3 and tiny max_bytes, some old records may be lost
        # (oldest archives rotated off).  But we must find at least some records.
        self.assertGreater(len(found_indices), 0)
        # All found indices must be valid.
        self.assertTrue(found_indices.issubset(written_indices))


class ReportLogDirTests(unittest.TestCase):
    def test_report_log_dir_is_cwd_logs(self) -> None:
        import os
        expected = Path(os.getcwd()) / "logs"
        self.assertEqual(report_log_dir(), expected)
