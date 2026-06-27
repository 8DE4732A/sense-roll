from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from config import ConfigError, load_config


class ConfigTests(unittest.TestCase):
    def load_from_text(self, text: str):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.yaml"
            path.write_text(text, encoding="utf-8")
            return load_config(path)

    def test_loads_valid_match_type(self) -> None:
        config = self.load_from_text(
            """
proxy:
  target_url: https://upstream.test
  max_retries: 2
  key_cooldown_seconds: 0
keys:
  - key: sk-test
rotation_rules:
  - jsonpath: $.error.message
    match_value: exhausted
    match_type: contains
    action: rotate
"""
        )

        self.assertEqual(config.proxy.max_retries, 2)
        self.assertEqual(config.rotation_rules[0].match_type, "contains")

    def test_rejects_invalid_proxy_section(self) -> None:
        with self.assertRaises(ConfigError):
            self.load_from_text(
                """
proxy: []
keys:
  - key: sk-test
rotation_rules:
  - jsonpath: $.error.type
    match_value: quota_exceeded_error
"""
            )

    def test_rejects_unknown_match_type(self) -> None:
        with self.assertRaises(ConfigError):
            self.load_from_text(
                """
keys:
  - key: sk-test
rotation_rules:
  - jsonpath: $.error.message
    match_value: exhausted
    match_type: fuzzy
"""
            )

    def test_rejects_invalid_regex_match_value(self) -> None:
        with self.assertRaises(ConfigError):
            self.load_from_text(
                """
keys:
  - key: sk-test
rotation_rules:
  - jsonpath: $.error.message
    match_value: "["
    match_type: regex
"""
            )
