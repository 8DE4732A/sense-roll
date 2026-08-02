"""Tests for db.Recorder — write thread, queries, and lifecycle."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from sense_roll.db import Recorder, db_path_for_config


def _make_recorder(tmp_dir: str) -> Recorder:
    return Recorder(Path(tmp_dir) / "test.db")


class RecorderWriteTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.rec = _make_recorder(self._tmp.name)

    def tearDown(self):
        self.rec.close()
        self._tmp.cleanup()

    def _flush(self, timeout: float = 2.0) -> None:
        """Wait until the write queue is empty."""
        deadline = time.monotonic() + timeout
        while not self.rec._q.empty():  # noqa: SLF001
            if time.monotonic() > deadline:
                raise TimeoutError("queue did not drain in time")
            time.sleep(0.01)
        time.sleep(0.05)  # give the writer thread one more cycle

    def test_record_and_query_list(self) -> None:
        self.rec.record({
            "ts": time.time(),
            "combo": "fast", "provider": "sn", "model": "flash",
            "key_prefix": "sk-sn-xx",
            "api_format": "openai", "is_stream": 0,
            "status_code": 200, "success": 1,
            "prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30,
            "duration_ms": 150,
        })
        self._flush()
        result = self.rec.query_list(limit=10)
        self.assertEqual(result["total"], 1)
        row = result["items"][0]
        self.assertEqual(row["combo"], "fast")
        self.assertEqual(row["total_tokens"], 30)

    def test_query_stats_grouped_by_combo(self) -> None:
        for success in (1, 1, 0):
            self.rec.record({
                "ts": time.time(), "combo": "fast", "provider": "sn",
                "model": "flash", "success": success,
                "total_tokens": 100 if success else 0,
            })
        self._flush()
        stats = self.rec.query_stats(group_by="combo")
        self.assertEqual(len(stats), 1)
        s = stats[0]
        self.assertEqual(s["group_key"], "fast")
        self.assertEqual(s["total"], 3)
        self.assertEqual(s["success_count"], 2)
        self.assertEqual(s["error_count"], 1)
        self.assertEqual(s["total_tokens"], 200)

    def test_query_trend(self) -> None:
        now = time.time()
        self.rec.record({"ts": now, "success": 1, "total_tokens": 50})
        self._flush()
        trend = self.rec.query_trend(bucket="hour")
        self.assertEqual(len(trend), 1)
        self.assertEqual(trend[0]["success_count"], 1)

    def test_query_list_filters(self) -> None:
        now = time.time()
        self.rec.record({"ts": now, "combo": "a", "success": 1})
        self.rec.record({"ts": now, "combo": "b", "success": 0})
        self._flush()
        result = self.rec.query_list(combo="a")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["combo"], "a")

    def test_query_list_success_filter(self) -> None:
        now = time.time()
        self.rec.record({"ts": now, "success": 1})
        self.rec.record({"ts": now, "success": 0})
        self._flush()
        ok = self.rec.query_list(success=True)
        fail = self.rec.query_list(success=False)
        self.assertEqual(ok["total"], 1)
        self.assertEqual(fail["total"], 1)

    def test_dropped_counter_increments_when_queue_full(self) -> None:
        # Temporarily shrink the queue to 1 item and fill it, then force a drop.
        import queue as _q
        old_q = self.rec._q  # noqa: SLF001
        small_q: _q.Queue = _q.Queue(maxsize=1)
        small_q.put({"ts": time.time()})  # fill it
        self.rec._q = small_q  # noqa: SLF001
        self.rec.record({"ts": time.time()})  # should be dropped
        self.assertGreater(self.rec.dropped_count, 0)
        self.rec._q = old_q  # noqa: SLF001 restore


class DbPathTests(unittest.TestCase):
    def test_db_path_sibling_to_config(self) -> None:
        path = db_path_for_config("/some/dir/config.yaml")
        self.assertEqual(path, Path("/some/dir/sense-roll.db"))
