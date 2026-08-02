from __future__ import annotations

import time
import unittest

from sense_roll.key_manager import ProviderKeyManager


class ProviderKeyManagerTests(unittest.TestCase):
    def test_fill_first_always_returns_first_available(self) -> None:
        km = ProviderKeyManager("sn", ["key-1", "key-2"], strategy="fill-first")
        self.assertEqual(km.get_key("model-a"), "key-1")
        self.assertEqual(km.get_key("model-a"), "key-1")

    def test_round_robin_rotates_across_requests(self) -> None:
        km = ProviderKeyManager("sn", ["key-1", "key-2"], strategy="round-robin")
        first = km.get_key("model-a")
        second = km.get_key("model-a")
        self.assertNotEqual(first, second)

    def test_attempted_keys_are_skipped(self) -> None:
        km = ProviderKeyManager("sn", ["key-1", "key-2"], strategy="fill-first")
        result = km.get_key("model-a", attempted_keys={"key-1"})
        self.assertEqual(result, "key-2")

    def test_record_error_puts_key_in_cooldown_for_model(self) -> None:
        km = ProviderKeyManager("sn", ["key-1", "key-2"], strategy="fill-first")
        km.record_error("key-1", "flash", cooldown_seconds=3600)
        # key-1 is in cooldown for flash, so fill-first should skip to key-2
        result = km.get_key("flash")
        self.assertEqual(result, "key-2")

    def test_cooldown_is_model_specific(self) -> None:
        km = ProviderKeyManager("sn", ["key-1"], strategy="fill-first")
        km.record_error("key-1", "flash", cooldown_seconds=3600)
        # key-1 is in cooldown for flash, but NOT for another model
        self.assertIsNone(km.get_key("flash"))
        self.assertEqual(km.get_key("other-model"), "key-1")

    def test_returns_none_when_all_keys_cooling_down(self) -> None:
        km = ProviderKeyManager("sn", ["key-1", "key-2"], strategy="fill-first")
        km.record_error("key-1", "flash", cooldown_seconds=3600)
        km.record_error("key-2", "flash", cooldown_seconds=3600)
        self.assertIsNone(km.get_key("flash"))

    def test_cooldown_expires(self) -> None:
        km = ProviderKeyManager("sn", ["key-1"], strategy="fill-first")
        km.record_error("key-1", "flash", cooldown_seconds=0)
        # cooldown_seconds=0 means it expires immediately
        result = km.get_key("flash")
        self.assertEqual(result, "key-1")

    def test_record_success_increments_use_count(self) -> None:
        km = ProviderKeyManager("sn", ["key-1"], strategy="fill-first")
        km.record_success("key-1", "flash")
        km.record_success("key-1", "flash")
        stats = km.get_stats()
        self.assertEqual(stats["keys"][0]["use_count"], 2)
        self.assertEqual(stats["keys"][0]["error_count"], 0)

    def test_get_stats_shows_model_cooldown(self) -> None:
        km = ProviderKeyManager("sn", ["key-1"], strategy="fill-first")
        km.record_error("key-1", "flash", cooldown_seconds=3600)
        stats = km.get_stats()
        cooldowns = stats["keys"][0]["model_cooldowns"]
        self.assertIn("flash", cooldowns)
        self.assertFalse(cooldowns["flash"]["available"])
        self.assertGreater(cooldowns["flash"]["seconds_remaining"], 3500)

    def test_get_stats_shows_available_after_expiry(self) -> None:
        km = ProviderKeyManager("sn", ["key-1"], strategy="fill-first")
        km.record_error("key-1", "flash", cooldown_seconds=0)
        stats = km.get_stats()
        cooldowns = stats["keys"][0]["model_cooldowns"]
        self.assertIn("flash", cooldowns)
        self.assertTrue(cooldowns["flash"]["available"])


class MergeStatsTests(unittest.TestCase):
    """ProviderKeyManager.merge_stats_from() — hot-reload stat preservation."""

    def test_merges_use_and_error_counts(self) -> None:
        old = ProviderKeyManager("sn", ["key-1", "key-2"], strategy="fill-first")
        old.record_success("key-1", "flash")
        old.record_success("key-1", "flash")
        old.record_error("key-2", "flash", cooldown_seconds=3600)

        new = ProviderKeyManager("sn", ["key-1", "key-2"], strategy="fill-first")
        new.merge_stats_from(old)

        stats = new.get_stats()
        self.assertEqual(stats["keys"][0]["use_count"], 2)
        self.assertEqual(stats["keys"][1]["error_count"], 1)

    def test_unexpired_cooldown_is_preserved(self) -> None:
        old = ProviderKeyManager("sn", ["key-1"], strategy="fill-first")
        old.record_error("key-1", "flash", cooldown_seconds=3600)

        new = ProviderKeyManager("sn", ["key-1"], strategy="fill-first")
        new.merge_stats_from(old)

        # key-1 should still be in cooldown for "flash"
        self.assertIsNone(new.get_key("flash"))

    def test_expired_cooldown_is_not_migrated(self) -> None:
        old = ProviderKeyManager("sn", ["key-1"], strategy="fill-first")
        old.record_error("key-1", "flash", cooldown_seconds=0)  # already expired

        new = ProviderKeyManager("sn", ["key-1"], strategy="fill-first")
        new.merge_stats_from(old)

        # cooldown expired → key should be available
        self.assertEqual(new.get_key("flash"), "key-1")

    def test_removed_key_is_ignored(self) -> None:
        old = ProviderKeyManager("sn", ["key-1", "key-gone"], strategy="fill-first")
        old.record_success("key-gone", "flash")

        new = ProviderKeyManager("sn", ["key-1"], strategy="fill-first")
        new.merge_stats_from(old)  # should not raise

        stats = new.get_stats()
        self.assertEqual(len(stats["keys"]), 1)

    def test_new_key_starts_fresh(self) -> None:
        old = ProviderKeyManager("sn", ["key-1"], strategy="fill-first")
        new = ProviderKeyManager("sn", ["key-1", "key-new"], strategy="fill-first")
        new.merge_stats_from(old)

        stats = new.get_stats()
        new_key_stat = stats["keys"][1]
        self.assertEqual(new_key_stat["use_count"], 0)
        self.assertEqual(new_key_stat["error_count"], 0)
