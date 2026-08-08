from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from sense_roll.config import AppConfig, ConfigError, build_config, dump_config, load_config

# New format: providers use `api` list; combos use `api_format` list or single string.
MINIMAL = """
providers:
  - name: sn
    api:
      - api_format: openai
        base_url: "https://upstream.test/v1"
    keys:
      - key: sk-1
    health_check_rules:
      - jsonpath: "$.error.type"
        match_value: "quota_exceeded_error"
        match_type: equals
        action: rotate
        cooldown_seconds: 60
        models: []
combos:
  - name: my-combo
    api_format: openai
    strategy: fill-first
    members:
      - provider: sn
        model: gpt-4o
"""

MINIMAL_DUAL = """
providers:
  - name: sn
    api:
      - api_format: openai
        base_url: "https://upstream.test/v1"
      - api_format: anthropic
        base_url: "https://upstream.test/v1"
    keys:
      - key: sk-1
    health_check_rules: []
combos:
  - name: dual-combo
    api_format:
      - openai
      - anthropic
    strategy: fill-first
    members:
      - provider: sn
        model: my-model
"""


class ConfigTests(unittest.TestCase):
    def load_from_text(self, text: str):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.yaml"
            path.write_text(text, encoding="utf-8")
            return load_config(path)

    def test_loads_minimal_valid_config(self) -> None:
        config = self.load_from_text(MINIMAL)
        self.assertEqual(len(config.providers), 1)
        self.assertEqual(config.providers[0].name, "sn")
        self.assertEqual(config.providers[0].api_endpoints[0].api_format, "openai")
        self.assertEqual(len(config.combos), 1)
        self.assertEqual(config.combos[0].name, "my-combo")

    def test_provider_chat_url_openai(self) -> None:
        config = self.load_from_text(MINIMAL)
        self.assertEqual(
            config.providers[0].get_chat_url("openai"),
            "https://upstream.test/v1/chat/completions",
        )

    def test_provider_chat_url_anthropic(self) -> None:
        config = self.load_from_text("""
providers:
  - name: sn-ant
    api:
      - api_format: anthropic
        base_url: "https://upstream.test/v1"
    keys:
      - key: sk-1
    health_check_rules: []
combos:
  - name: ant-combo
    api_format: anthropic
    members:
      - provider: sn-ant
        model: claude-3
""")
        self.assertEqual(
            config.providers[0].get_chat_url("anthropic"),
            "https://upstream.test/v1/messages",
        )

    def test_provider_dual_format(self) -> None:
        config = self.load_from_text(MINIMAL_DUAL)
        p = config.providers[0]
        self.assertTrue(p.supports_format("openai"))
        self.assertTrue(p.supports_format("anthropic"))
        self.assertEqual(p.get_chat_url("openai"), "https://upstream.test/v1/chat/completions")
        self.assertEqual(p.get_chat_url("anthropic"), "https://upstream.test/v1/messages")

    def test_combo_dual_api_formats(self) -> None:
        config = self.load_from_text(MINIMAL_DUAL)
        self.assertEqual(config.combos[0].api_formats, ["openai", "anthropic"])

    def test_combo_single_api_format_as_string(self) -> None:
        config = self.load_from_text(MINIMAL)
        self.assertEqual(config.combos[0].api_formats, ["openai"])

    def test_rejects_unknown_api_format_in_provider(self) -> None:
        with self.assertRaises(ConfigError):
            self.load_from_text("""
providers:
  - name: sn
    api:
      - api_format: grpc
        base_url: "https://upstream.test/v1"
    keys:
      - key: sk-1
    health_check_rules: []
combos:
  - name: c
    api_format: openai
    members:
      - provider: sn
        model: m
""")

    def test_rejects_combo_format_unsupported_by_provider(self) -> None:
        with self.assertRaises(ConfigError):
            self.load_from_text("""
providers:
  - name: sn-openai
    api:
      - api_format: openai
        base_url: "https://upstream.test/v1"
    keys:
      - key: sk-1
    health_check_rules: []
combos:
  - name: c
    api_format: anthropic
    members:
      - provider: sn-openai
        model: m
""")

    def test_rejects_undefined_provider_in_combo(self) -> None:
        with self.assertRaises(ConfigError):
            self.load_from_text("""
providers:
  - name: real-provider
    api:
      - api_format: openai
        base_url: "https://upstream.test/v1"
    keys:
      - key: sk-1
    health_check_rules: []
combos:
  - name: c
    api_format: openai
    members:
      - provider: ghost-provider
        model: m
""")

    def test_rejects_unknown_match_type(self) -> None:
        with self.assertRaises(ConfigError):
            self.load_from_text("""
providers:
  - name: sn
    api:
      - api_format: openai
        base_url: "https://upstream.test/v1"
    keys:
      - key: sk-1
    health_check_rules:
      - jsonpath: "$.error.type"
        match_value: "x"
        match_type: fuzzy
        action: rotate
        cooldown_seconds: 60
        models: []
combos:
  - name: c
    api_format: openai
    members:
      - provider: sn
        model: m
""")

    def test_rejects_invalid_regex(self) -> None:
        with self.assertRaises(ConfigError):
            self.load_from_text("""
providers:
  - name: sn
    api:
      - api_format: openai
        base_url: "https://upstream.test/v1"
    keys:
      - key: sk-1
    health_check_rules:
      - jsonpath: "$.error.type"
        match_value: "["
        match_type: regex
        action: rotate
        cooldown_seconds: 60
        models: []
combos:
  - name: c
    api_format: openai
    members:
      - provider: sn
        model: m
""")

    def test_rejects_duplicate_provider_name(self) -> None:
        with self.assertRaises(ConfigError):
            self.load_from_text("""
providers:
  - name: dup
    api:
      - api_format: openai
        base_url: "https://upstream.test/v1"
    keys:
      - key: sk-1
    health_check_rules: []
  - name: dup
    api:
      - api_format: openai
        base_url: "https://upstream2.test/v1"
    keys:
      - key: sk-2
    health_check_rules: []
combos:
  - name: c
    api_format: openai
    members:
      - provider: dup
        model: m
""")

    def test_multiple_providers_and_combos(self) -> None:
        config = self.load_from_text("""
providers:
  - name: sn-openai
    api:
      - api_format: openai
        base_url: "https://sn.test/v1"
    keys:
      - key: sk-sn-1
      - key: sk-sn-2
    health_check_rules:
      - jsonpath: "$.error.type"
        match_value: "quota_exceeded_error"
        match_type: equals
        action: rotate
        cooldown_seconds: 18000
        models: ["deepseek-v4-flash"]
  - name: deepseek
    api:
      - api_format: openai
        base_url: "https://ds.test/v1"
    key_strategy: round-robin
    keys:
      - key: sk-ds-1
    health_check_rules: []
combos:
  - name: fast
    api_format: openai
    strategy: fill-first
    members:
      - provider: sn-openai
        model: deepseek-v4-flash
      - provider: deepseek
        model: deepseek-chat
""")
        self.assertEqual(len(config.providers), 2)
        self.assertEqual(len(config.combos[0].members), 2)
        rule = config.providers[0].health_check_rules[0]
        self.assertEqual(rule.cooldown_seconds, 18000)
        self.assertEqual(rule.models, ["deepseek-v4-flash"])


class BuildConfigTests(unittest.TestCase):
    """build_config() accepts raw dict directly (used by admin hot-reload endpoint)."""

    def _raw(self):
        return yaml.safe_load(MINIMAL)

    def test_build_config_accepts_dict(self) -> None:
        cfg = build_config(self._raw())
        self.assertIsInstance(cfg, AppConfig)
        self.assertEqual(cfg.providers[0].name, "sn")

    def test_build_config_rejects_non_dict(self) -> None:
        with self.assertRaises(ConfigError):
            build_config("not a dict")  # type: ignore[arg-type]

    def test_build_config_rejects_bad_api_format(self) -> None:
        raw = self._raw()
        raw["providers"][0]["api"][0]["api_format"] = "grpc"
        with self.assertRaises(ConfigError):
            build_config(raw)


class DumpConfigTests(unittest.TestCase):
    """dump_config() round-trips through build_config without data loss."""

    def _cfg(self):
        return build_config(yaml.safe_load(MINIMAL))

    def test_dump_produces_dict(self) -> None:
        d = dump_config(self._cfg())
        self.assertIsInstance(d, dict)
        self.assertIn("providers", d)
        self.assertIn("combos", d)

    def test_dump_roundtrip(self) -> None:
        cfg = self._cfg()
        dumped = yaml.safe_dump(dump_config(cfg))
        cfg2 = build_config(yaml.safe_load(dumped))
        self.assertEqual(cfg.providers[0].name, cfg2.providers[0].name)
        ep1 = cfg.providers[0].api_endpoints[0]
        ep2 = cfg2.providers[0].api_endpoints[0]
        self.assertEqual(ep1.base_url, ep2.base_url)
        self.assertEqual(cfg.providers[0].keys[0].key, cfg2.providers[0].keys[0].key)
        self.assertEqual(cfg.combos[0].name, cfg2.combos[0].name)
        self.assertEqual(cfg.combos[0].members[0].model, cfg2.combos[0].members[0].model)

    def test_dump_preserves_health_check_rule_fields(self) -> None:
        raw = yaml.safe_load(MINIMAL)
        raw["providers"][0]["health_check_rules"][0]["cooldown_seconds"] = 9999
        raw["providers"][0]["health_check_rules"][0]["models"] = ["m1", "m2"]
        cfg = build_config(raw)
        d = dump_config(cfg)
        rule = d["providers"][0]["health_check_rules"][0]
        self.assertEqual(rule["cooldown_seconds"], 9999)
        self.assertEqual(rule["models"], ["m1", "m2"])

    def test_dump_preserves_keys(self) -> None:
        cfg = self._cfg()
        d = dump_config(cfg)
        self.assertEqual(d["providers"][0]["keys"][0]["key"], "sk-1")

    def test_dump_dual_format_roundtrip(self) -> None:
        cfg = build_config(yaml.safe_load(MINIMAL_DUAL))
        dumped = yaml.safe_dump(dump_config(cfg))
        cfg2 = build_config(yaml.safe_load(dumped))
        self.assertEqual(cfg2.combos[0].api_formats, ["openai", "anthropic"])
        self.assertTrue(cfg2.providers[0].supports_format("openai"))
        self.assertTrue(cfg2.providers[0].supports_format("anthropic"))


class VerboseLoggingConfigTests(unittest.TestCase):
    """Tests for verbose_logging field in AppConfig."""

    def _minimal_raw(self) -> dict:
        return {
            "providers": [{
                "name": "sn",
                "api": [{"api_format": "openai", "base_url": "https://x/v1"}],
                "keys": [{"key": "sk-1"}],
            }],
            "combos": [{"name": "fast", "api_format": "openai", "strategy": "fill-first",
                        "members": [{"provider": "sn", "model": "m"}]}],
        }

    def test_default_is_false(self) -> None:
        cfg = build_config(self._minimal_raw())
        self.assertFalse(cfg.verbose_logging)

    def test_explicit_true(self) -> None:
        raw = self._minimal_raw()
        raw["verbose_logging"] = True
        cfg = build_config(raw)
        self.assertTrue(cfg.verbose_logging)

    def test_dump_includes_verbose_logging(self) -> None:
        raw = self._minimal_raw()
        raw["verbose_logging"] = True
        cfg = build_config(raw)
        d = dump_config(cfg)
        self.assertIn("verbose_logging", d)
        self.assertTrue(d["verbose_logging"])

    def test_roundtrip_preserves_verbose_logging(self) -> None:
        import yaml as _yaml
        raw = self._minimal_raw()
        raw["verbose_logging"] = True
        cfg = build_config(raw)
        dumped = _yaml.safe_dump(dump_config(cfg))
        cfg2 = build_config(_yaml.safe_load(dumped))
        self.assertTrue(cfg2.verbose_logging)


class PayloadScriptConfigTests(unittest.TestCase):
    """Tests for payload_scripts list in AppConfig."""

    _MINIMAL = {
        "providers": [{"name": "sn", "api": [{"api_format": "openai", "base_url": "https://x/v1"}], "keys": [{"key": "sk-1"}]}],
        "combos": [{"name": "fast", "api_format": "openai", "strategy": "fill-first", "members": [{"provider": "sn", "model": "m"}]}],
    }

    def test_default_is_empty_list(self) -> None:
        from sense_roll.config import build_config
        cfg = build_config(dict(self._MINIMAL))
        self.assertEqual(cfg.payload_scripts, [])

    def test_parses_script_list(self) -> None:
        from sense_roll.config import build_config
        raw = {**self._MINIMAL, "payload_scripts": [
            {"name": "hide-ua", "enabled": True, "script": "request.headers.pop('user-agent', None)"},
            {"name": "limit-thinking", "enabled": False, "script": "request.body['x'] = 1"},
        ]}
        cfg = build_config(raw)
        self.assertEqual(len(cfg.payload_scripts), 2)
        self.assertEqual(cfg.payload_scripts[0].name, "hide-ua")
        self.assertTrue(cfg.payload_scripts[0].enabled)
        self.assertIn("user-agent", cfg.payload_scripts[0].script)
        self.assertEqual(cfg.payload_scripts[1].name, "limit-thinking")
        self.assertFalse(cfg.payload_scripts[1].enabled)

    def test_rejects_non_list(self) -> None:
        from sense_roll.config import build_config, ConfigError
        raw = {**self._MINIMAL, "payload_scripts": "not a list"}
        with self.assertRaises(ConfigError):
            build_config(raw)

    def test_rejects_non_dict_entry(self) -> None:
        from sense_roll.config import build_config, ConfigError
        raw = {**self._MINIMAL, "payload_scripts": ["not a dict"]}
        with self.assertRaises(ConfigError):
            build_config(raw)

    def test_roundtrip_preserves_scripts(self) -> None:
        import yaml as _yaml
        from sense_roll.config import build_config, dump_config
        raw = {**self._MINIMAL, "payload_scripts": [
            {"name": "s1", "enabled": True, "script": "request.body['k'] = 1"},
            {"name": "s2", "enabled": False, "script": "pass"},
        ]}
        cfg = build_config(raw)
        dumped = _yaml.safe_dump(dump_config(cfg))
        cfg2 = build_config(_yaml.safe_load(dumped))
        self.assertEqual(len(cfg2.payload_scripts), 2)
        self.assertEqual(cfg2.payload_scripts[0].name, "s1")
        self.assertTrue(cfg2.payload_scripts[0].enabled)
        self.assertFalse(cfg2.payload_scripts[1].enabled)

    def test_dump_includes_payload_scripts(self) -> None:
        from sense_roll.config import build_config, dump_config
        raw = {**self._MINIMAL, "payload_scripts": [{"name": "test", "enabled": True, "script": "pass"}]}
        cfg = build_config(raw)
        d = dump_config(cfg)
        self.assertIn("payload_scripts", d)
        self.assertEqual(d["payload_scripts"][0]["name"], "test")
