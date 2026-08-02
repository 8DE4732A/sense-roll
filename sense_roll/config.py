"""Configuration loading and validation for sense-roll."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

VALID_API_FORMATS = {"openai", "anthropic", "openai-responses", "openai-images"}
VALID_KEY_STRATEGIES = {"fill-first", "round-robin"}
VALID_MATCH_TYPES = {"equals", "contains", "regex"}


class ConfigError(Exception):
    """Raised when configuration is invalid."""


@dataclass
class KeyConfig:
    """A single API key entry."""

    key: str


@dataclass
class HealthCheckRule:
    """A rule that triggers key rotation when a JSONPath expression matches."""

    description: str = ""
    jsonpath: str = "$.error.type"
    match_value: str = "quota_exceeded_error"
    match_type: str = "equals"
    action: str = "rotate"
    cooldown_seconds: int = 60
    models: list[str] = field(default_factory=list)


@dataclass
class ApiEndpoint:
    """One (api_format, base_url) pair inside a provider's api list."""

    api_format: str
    base_url: str

    @property
    def chat_url(self) -> str:
        base = self.base_url.rstrip("/")
        if self.api_format == "anthropic":
            return f"{base}/messages"
        if self.api_format == "openai-responses":
            return f"{base}/responses"
        if self.api_format == "openai-images":
            return f"{base}/images/generations"
        return f"{base}/chat/completions"


@dataclass
class ProviderConfig:
    """Configuration for a single upstream provider.

    A provider may expose multiple API formats (openai + anthropic) through
    separate base_url entries in ``api_endpoints``, all sharing the same key
    pool and health-check rules.
    """

    name: str
    api_endpoints: list[ApiEndpoint]
    max_retries: int = 3
    key_strategy: str = "fill-first"
    keys: list[KeyConfig] = field(default_factory=list)
    health_check_rules: list[HealthCheckRule] = field(default_factory=list)

    def get_chat_url(self, api_format: str) -> str:
        for ep in self.api_endpoints:
            if ep.api_format == api_format:
                return ep.chat_url
        raise ValueError(
            f"Provider {self.name!r} has no endpoint for api_format={api_format!r}"
        )

    def supports_format(self, api_format: str) -> bool:
        return any(ep.api_format == api_format for ep in self.api_endpoints)


@dataclass
class ComboMember:
    """A (provider, model) pair inside a combo."""

    provider: str
    model: str


@dataclass
class ComboConfig:
    """A named virtual model that routes requests to (provider, model) members.

    ``api_formats`` lists all API formats this combo accepts (e.g. both
    ``openai`` and ``anthropic``).  A single combo entry therefore serves
    both /v1/chat/completions and /v1/messages without duplication.
    """

    name: str
    api_formats: list[str]
    strategy: str = "fill-first"
    members: list[ComboMember] = field(default_factory=list)


@dataclass
class AppConfig:
    """Top-level application configuration."""

    providers: list[ProviderConfig] = field(default_factory=list)
    combos: list[ComboConfig] = field(default_factory=list)


def _parse_health_check_rule(entry: dict, idx: int, context: str) -> HealthCheckRule:
    if not isinstance(entry, dict):
        raise ConfigError(f"{context}[{idx}] must be a mapping")
    action = str(entry.get("action", "rotate"))
    if action != "rotate":
        raise ConfigError(f"{context}[{idx}].action must be 'rotate', got {action!r}")
    match_type = str(entry.get("match_type", "equals"))
    if match_type not in VALID_MATCH_TYPES:
        raise ConfigError(
            f"{context}[{idx}].match_type must be one of: {', '.join(sorted(VALID_MATCH_TYPES))}"
        )
    match_value = str(entry.get("match_value", "quota_exceeded_error"))
    if match_type == "regex":
        try:
            re.compile(match_value)
        except re.error as e:
            raise ConfigError(
                f"{context}[{idx}].match_value is not a valid regex: {e}"
            ) from e
    try:
        cooldown_seconds = int(entry.get("cooldown_seconds", 60))
    except (TypeError, ValueError) as e:
        raise ConfigError(f"{context}[{idx}].cooldown_seconds must be an integer") from e
    if cooldown_seconds < 0:
        raise ConfigError(f"{context}[{idx}].cooldown_seconds must be >= 0")

    models_raw = entry.get("models", [])
    if not isinstance(models_raw, list):
        raise ConfigError(f"{context}[{idx}].models must be a list")
    models = [str(m) for m in models_raw]

    return HealthCheckRule(
        description=str(entry.get("description", "")),
        jsonpath=str(entry.get("jsonpath", "$.error.type")),
        match_value=match_value,
        match_type=match_type,
        action=action,
        cooldown_seconds=cooldown_seconds,
        models=models,
    )


def build_config(raw: dict) -> AppConfig:
    """Validate a raw dict and return an AppConfig.

    Raises ConfigError on invalid input.  Used by both load_config (file-based
    startup) and the admin PUT endpoint (request-body hot-reload) so that
    validation logic lives in exactly one place.
    """
    if not isinstance(raw, dict):
        raise ConfigError("Configuration must be a top-level mapping")

    # --- providers ---
    providers_raw = raw.get("providers", [])
    if not isinstance(providers_raw, list) or not providers_raw:
        raise ConfigError("Configuration must contain at least one entry in 'providers'")

    providers: list[ProviderConfig] = []
    provider_names: set[str] = set()

    for i, p in enumerate(providers_raw):
        if not isinstance(p, dict):
            raise ConfigError(f"providers[{i}] must be a mapping")

        name = str(p.get("name", "")).strip()
        if not name:
            raise ConfigError(f"providers[{i}].name must not be empty")
        if name in provider_names:
            raise ConfigError(f"Duplicate provider name: {name!r}")
        provider_names.add(name)

        # --- api endpoints ---
        api_raw = p.get("api", [])
        if not isinstance(api_raw, list) or not api_raw:
            raise ConfigError(
                f"providers[{i}].api must be a non-empty list of {{api_format, base_url}} entries"
            )
        api_endpoints: list[ApiEndpoint] = []
        seen_formats: set[str] = set()
        for k, ep in enumerate(api_raw):
            if not isinstance(ep, dict):
                raise ConfigError(f"providers[{i}].api[{k}] must be a mapping")
            fmt = str(ep.get("api_format", "")).strip().lower()
            if fmt not in VALID_API_FORMATS:
                raise ConfigError(
                    f"providers[{i}].api[{k}].api_format must be one of: "
                    f"{', '.join(sorted(VALID_API_FORMATS))}"
                )
            if fmt in seen_formats:
                raise ConfigError(
                    f"providers[{i}].api: duplicate api_format {fmt!r}"
                )
            seen_formats.add(fmt)
            base_url = str(ep.get("base_url", "")).strip()
            if not base_url:
                raise ConfigError(f"providers[{i}].api[{k}].base_url must not be empty")
            api_endpoints.append(ApiEndpoint(api_format=fmt, base_url=base_url))

        try:
            max_retries = int(p.get("max_retries", 3))
        except (TypeError, ValueError) as e:
            raise ConfigError(f"providers[{i}].max_retries must be an integer") from e
        if max_retries < 0:
            raise ConfigError(f"providers[{i}].max_retries must be >= 0")

        key_strategy = str(p.get("key_strategy", "fill-first")).strip().lower()
        if key_strategy not in VALID_KEY_STRATEGIES:
            raise ConfigError(
                f"providers[{i}].key_strategy must be one of: {', '.join(sorted(VALID_KEY_STRATEGIES))}"
            )

        keys_raw = p.get("keys", [])
        if not isinstance(keys_raw, list) or not keys_raw:
            raise ConfigError(f"providers[{i}] must contain at least one key in 'keys'")
        keys: list[KeyConfig] = []
        for j, entry in enumerate(keys_raw):
            if not isinstance(entry, dict) or "key" not in entry:
                raise ConfigError(f"providers[{i}].keys[{j}] is missing the 'key' field")
            key = str(entry["key"]).strip()
            if not key:
                raise ConfigError(f"providers[{i}].keys[{j}].key must not be empty")
            keys.append(KeyConfig(key=key))

        rules_raw = p.get("health_check_rules", [])
        if not isinstance(rules_raw, list):
            raise ConfigError(f"providers[{i}].health_check_rules must be a list")
        rules = [
            _parse_health_check_rule(r, j, f"providers[{i}].health_check_rules")
            for j, r in enumerate(rules_raw)
        ]

        providers.append(ProviderConfig(
            name=name,
            api_endpoints=api_endpoints,
            max_retries=max_retries,
            key_strategy=key_strategy,
            keys=keys,
            health_check_rules=rules,
        ))

    # --- combos ---
    combos_raw = raw.get("combos", [])
    if not isinstance(combos_raw, list) or not combos_raw:
        raise ConfigError("Configuration must contain at least one entry in 'combos'")

    provider_map: dict[str, ProviderConfig] = {p.name: p for p in providers}
    combos: list[ComboConfig] = []
    combo_names: set[str] = set()

    for i, c in enumerate(combos_raw):
        if not isinstance(c, dict):
            raise ConfigError(f"combos[{i}] must be a mapping")

        name = str(c.get("name", "")).strip()
        if not name:
            raise ConfigError(f"combos[{i}].name must not be empty")
        if name in combo_names:
            raise ConfigError(f"Duplicate combo name: {name!r}")
        combo_names.add(name)

        # api_format may be a single string or a list
        fmt_raw = c.get("api_format", [])
        if isinstance(fmt_raw, str):
            fmt_raw = [fmt_raw]
        if not isinstance(fmt_raw, list) or not fmt_raw:
            raise ConfigError(
                f"combos[{i}].api_format must be a non-empty string or list"
            )
        api_formats: list[str] = []
        for fmt in fmt_raw:
            fmt = str(fmt).strip().lower()
            if fmt not in VALID_API_FORMATS:
                raise ConfigError(
                    f"combos[{i}].api_format contains unknown value {fmt!r}; "
                    f"must be one of: {', '.join(sorted(VALID_API_FORMATS))}"
                )
            api_formats.append(fmt)

        strategy = str(c.get("strategy", "fill-first")).strip().lower()
        if strategy not in VALID_KEY_STRATEGIES:
            raise ConfigError(
                f"combos[{i}].strategy must be one of: {', '.join(sorted(VALID_KEY_STRATEGIES))}"
            )

        members_raw = c.get("members", [])
        if not isinstance(members_raw, list) or not members_raw:
            raise ConfigError(f"combos[{i}] must contain at least one entry in 'members'")

        members: list[ComboMember] = []
        for j, m in enumerate(members_raw):
            if not isinstance(m, dict):
                raise ConfigError(f"combos[{i}].members[{j}] must be a mapping")
            provider_name = str(m.get("provider", "")).strip()
            if not provider_name:
                raise ConfigError(f"combos[{i}].members[{j}].provider must not be empty")
            if provider_name not in provider_map:
                raise ConfigError(
                    f"combos[{i}].members[{j}].provider {provider_name!r} is not defined in providers"
                )
            prov = provider_map[provider_name]
            for fmt in api_formats:
                if not prov.supports_format(fmt):
                    raise ConfigError(
                        f"combos[{i}].members[{j}].provider {provider_name!r} "
                        f"does not have an api endpoint for format {fmt!r}"
                    )
            model = str(m.get("model", "")).strip()
            if not model:
                raise ConfigError(f"combos[{i}].members[{j}].model must not be empty")
            members.append(ComboMember(provider=provider_name, model=model))

        combos.append(ComboConfig(
            name=name,
            api_formats=api_formats,
            strategy=strategy,
            members=members,
        ))

    return AppConfig(providers=providers, combos=combos)


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    """Load and validate the YAML configuration file."""
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"Configuration file not found: {path}")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"Failed to parse YAML configuration: {e}") from e

    return build_config(raw)


def dump_config(cfg: AppConfig) -> dict:
    """Serialize an AppConfig back to a plain dict suitable for yaml.safe_dump."""
    return {
        "providers": [
            {
                "name": p.name,
                "api": [
                    {"api_format": ep.api_format, "base_url": ep.base_url}
                    for ep in p.api_endpoints
                ],
                "max_retries": p.max_retries,
                "key_strategy": p.key_strategy,
                "keys": [{"key": k.key} for k in p.keys],
                "health_check_rules": [
                    {
                        "description": r.description,
                        "jsonpath": r.jsonpath,
                        "match_value": r.match_value,
                        "match_type": r.match_type,
                        "action": r.action,
                        "cooldown_seconds": r.cooldown_seconds,
                        "models": list(r.models),
                    }
                    for r in p.health_check_rules
                ],
            }
            for p in cfg.providers
        ],
        "combos": [
            {
                "name": c.name,
                "api_format": c.api_formats,
                "strategy": c.strategy,
                "members": [
                    {"provider": m.provider, "model": m.model}
                    for m in c.members
                ],
            }
            for c in cfg.combos
        ],
    }
