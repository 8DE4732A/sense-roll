"""Configuration loading and validation for sense-roll."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


class ConfigError(Exception):
    """Raised when configuration is invalid."""


@dataclass
class ProxyConfig:
    """Proxy-level settings."""

    target_url: str = "https://token.sensenova.cn/v1/chat/completions"
    max_retries: int = 3


@dataclass
class KeyConfig:
    """A single API key entry."""

    key: str


@dataclass
class RotationRule:
    """A rule that triggers key rotation when a JSONPath expression matches."""

    description: str = ""
    jsonpath: str = "$.error.type"
    match_value: str = "quota_exceeded_error"
    action: str = "rotate"


@dataclass
class AppConfig:
    """Top-level application configuration."""

    proxy: ProxyConfig = field(default_factory=ProxyConfig)
    keys: list[KeyConfig] = field(default_factory=list)
    rotation_rules: list[RotationRule] = field(default_factory=list)


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    """Load and validate the YAML configuration file.

    Args:
        path: Path to the YAML configuration file.

    Returns:
        A validated AppConfig instance.

    Raises:
        ConfigError: If the configuration is invalid or missing required fields.
    """
    path = Path(path)

    if not path.exists():
        raise ConfigError(f"Configuration file not found: {path}")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"Failed to parse YAML configuration: {e}") from e

    if not isinstance(raw, dict):
        raise ConfigError("Configuration file must contain a top-level mapping")

    # --- proxy section ---
    proxy_cfg = ProxyConfig()
    proxy_raw = raw.get("proxy", {})
    if isinstance(proxy_raw, dict):
        if "target_url" in proxy_raw:
            proxy_cfg.target_url = str(proxy_raw["target_url"])
        if "max_retries" in proxy_raw:
            proxy_cfg.max_retries = int(proxy_raw["max_retries"])

    # --- keys section ---
    keys_raw = raw.get("keys", [])
    if not isinstance(keys_raw, list) or not keys_raw:
        raise ConfigError(
            "Configuration must contain at least one key in the 'keys' list"
        )
    keys = []
    for i, entry in enumerate(keys_raw):
        if not isinstance(entry, dict) or "key" not in entry:
            raise ConfigError(f"keys[{i}] is missing the 'key' field")
        keys.append(KeyConfig(key=str(entry["key"])))

    # --- rotation_rules section ---
    rules_raw = raw.get("rotation_rules", [])
    if not isinstance(rules_raw, list) or not rules_raw:
        raise ConfigError(
            "Configuration must contain at least one rule in 'rotation_rules'"
        )
    rules = []
    for i, entry in enumerate(rules_raw):
        if not isinstance(entry, dict):
            raise ConfigError(f"rotation_rules[{i}] must be a mapping")
        rules.append(
            RotationRule(
                description=str(entry.get("description", "")),
                jsonpath=str(entry.get("jsonpath", "$.error.type")),
                match_value=str(entry.get("match_value", "quota_exceeded_error")),
                action=str(entry.get("action", "rotate")),
            )
        )

    return AppConfig(proxy=proxy_cfg, keys=keys, rotation_rules=rules)
