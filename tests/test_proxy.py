from __future__ import annotations

import json
import unittest

import httpx
from starlette.requests import Request

from sense_roll.combo_router import ComboRouter
from sense_roll.config import (
    AppConfig,
    ApiEndpoint,
    ComboConfig,
    ComboMember,
    HealthCheckRule,
    KeyConfig,
    ProviderConfig,
)
from sense_roll.key_manager import ProviderKeyManager
from sense_roll.proxy import ProxyService


def make_request(
    body: bytes,
    headers: dict[str, str] | None = None,
    path: str = "/v1/chat/completions",
) -> Request:
    sent = False

    async def receive() -> dict:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    raw_headers = [
        (name.lower().encode("latin-1"), value.encode("latin-1"))
        for name, value in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": raw_headers,
        "query_string": b"",
    }
    return Request(scope, receive)


def make_service(
    keys_by_provider: dict[str, list[str]] | None = None,
    rules: list[HealthCheckRule] | None = None,
    combo_members: list[tuple[str, str]] | None = None,
    combo_strategy: str = "fill-first",
    key_strategy: str = "fill-first",
    max_retries: int = 3,
) -> ProxyService:
    """Build a minimal ProxyService for testing."""
    if keys_by_provider is None:
        keys_by_provider = {"sn": ["key-1", "key-2"]}
    if rules is None:
        rules = [
            HealthCheckRule(
                jsonpath="$.error.type",
                match_value="quota_exceeded_error",
                match_type="equals",
                cooldown_seconds=60,
                models=[],
            )
        ]
    if combo_members is None:
        combo_members = [(list(keys_by_provider.keys())[0], "test-model")]

    providers = [
        ProviderConfig(
            name=name,
            api_endpoints=[ApiEndpoint(api_format="openai", base_url="https://upstream.test/v1")],
            max_retries=max_retries,
            key_strategy=key_strategy,
            keys=[KeyConfig(k) for k in keys],
            health_check_rules=rules,
        )
        for name, keys in keys_by_provider.items()
    ]
    combos = [
        ComboConfig(
            name="my-combo",
            api_formats=["openai"],
            strategy=combo_strategy,
            members=[ComboMember(provider=p, model=m) for p, m in combo_members],
        )
    ]
    config = AppConfig(providers=providers, combos=combos)
    kms = {
        p.name: ProviderKeyManager(p.name, [k.key for k in p.keys], strategy=p.key_strategy)
        for p in providers
    }
    router = ComboRouter(combos)
    return ProxyService(config, kms, router)


def combo_body(model: str = "my-combo", stream: bool = False) -> bytes:
    return json.dumps({"model": model, "stream": stream}).encode()


class ProxyServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        svc = getattr(self, "svc", None)
        if svc is not None:
            await svc.aclose()

    # ------------------------------------------------------------------
    # Basic detection helpers
    # ------------------------------------------------------------------

    async def test_stream_true_body_marks_request_as_streaming(self) -> None:
        body = json.dumps({"model": "my-combo", "stream": True}).encode()
        request = make_request(body, {"content-type": "application/json"})
        self.assertTrue(ProxyService._is_streaming_request(request, body))

    # ------------------------------------------------------------------
    # Unknown / mismatched combo
    # ------------------------------------------------------------------

    async def test_unknown_combo_returns_400(self) -> None:
        self.svc = make_service()
        self.svc.client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"ok": True})
        ))
        request = make_request(combo_body("ghost-combo"), {"content-type": "application/json"})
        response = await self.svc.handle_openai_request(request)
        self.assertEqual(response.status_code, 400)

    async def test_combo_format_mismatch_returns_400(self) -> None:
        self.svc = make_service()
        self.svc.client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"ok": True})
        ))
        # combo is openai, but we call handle_anthropic_request
        request = make_request(combo_body("my-combo"), {"content-type": "application/json"})
        response = await self.svc.handle_anthropic_request(request)
        self.assertEqual(response.status_code, 400)

    # ------------------------------------------------------------------
    # Non-streaming key rotation
    # ------------------------------------------------------------------

    async def test_non_streaming_rotates_key_on_quota_error(self) -> None:
        self.svc = make_service()
        calls: list[str] = []

        def handler(r: httpx.Request) -> httpx.Response:
            calls.append(r.headers["authorization"])
            if r.headers["authorization"] == "Bearer key-1":
                return httpx.Response(429, json={"error": {"type": "quota_exceeded_error"}})
            return httpx.Response(200, json={"ok": True})

        self.svc.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        request = make_request(combo_body(), {"content-type": "application/json"})
        response = await self.svc.handle_openai_request(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, ["Bearer key-1", "Bearer key-2"])

    async def test_model_is_rewritten_to_provider_model(self) -> None:
        self.svc = make_service()
        received_models: list[str] = []

        def handler(r: httpx.Request) -> httpx.Response:
            received_models.append(json.loads(r.content)["model"])
            return httpx.Response(200, json={"ok": True})

        self.svc.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        request = make_request(combo_body("my-combo"), {"content-type": "application/json"})
        await self.svc.handle_openai_request(request)

        self.assertEqual(received_models, ["test-model"])

    async def test_all_keys_exhausted_returns_503(self) -> None:
        self.svc = make_service(keys_by_provider={"sn": ["key-1"]})
        calls = 0

        def handler(r: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(429, json={"error": {"type": "quota_exceeded_error"}})

        self.svc.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        request = make_request(combo_body(), {"content-type": "application/json"})
        response = await self.svc.handle_openai_request(request)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(calls, 1)

    # ------------------------------------------------------------------
    # Two-level fallback: provider member switch
    # ------------------------------------------------------------------

    async def test_falls_back_to_second_member_when_first_exhausted(self) -> None:
        self.svc = make_service(
            keys_by_provider={"sn": ["key-sn"], "ds": ["key-ds"]},
            combo_members=[("sn", "flash"), ("ds", "chat")],
        )
        calls: list[str] = []

        def handler(r: httpx.Request) -> httpx.Response:
            calls.append(r.headers["authorization"])
            if r.headers["authorization"] == "Bearer key-sn":
                return httpx.Response(429, json={"error": {"type": "quota_exceeded_error"}})
            return httpx.Response(200, json={"ok": True})

        self.svc.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        request = make_request(combo_body(), {"content-type": "application/json"})
        response = await self.svc.handle_openai_request(request)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Bearer key-sn", calls)
        self.assertIn("Bearer key-ds", calls)

    # ------------------------------------------------------------------
    # Cooldown is model-specific
    # ------------------------------------------------------------------

    async def test_quota_rule_with_model_filter_does_not_affect_other_model(self) -> None:
        rules = [
            HealthCheckRule(
                jsonpath="$.error.type",
                match_value="quota_exceeded_error",
                match_type="equals",
                cooldown_seconds=3600,
                models=["flash"],  # only affects "flash"
            )
        ]
        # Two combos share the same provider
        providers = [
            ProviderConfig(
                name="sn",
                api_endpoints=[ApiEndpoint(api_format="openai", base_url="https://upstream.test/v1")],
                max_retries=1,
                key_strategy="fill-first",
                keys=[KeyConfig("key-1")],
                health_check_rules=rules,
            )
        ]
        combos = [
            ComboConfig(
                name="flash-combo",
                api_formats=["openai"],
                strategy="fill-first",
                members=[ComboMember(provider="sn", model="flash")],
            ),
            ComboConfig(
                name="other-combo",
                api_formats=["openai"],
                strategy="fill-first",
                members=[ComboMember(provider="sn", model="other")],
            ),
        ]
        config = AppConfig(providers=providers, combos=combos)
        kms = {"sn": ProviderKeyManager("sn", ["key-1"], strategy="fill-first")}
        router = ComboRouter(combos)
        self.svc = ProxyService(config, kms, router)

        call_count = 0

        def handler(r: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            body = json.loads(r.content)
            if body["model"] == "flash":
                return httpx.Response(429, json={"error": {"type": "quota_exceeded_error"}})
            return httpx.Response(200, json={"ok": True})

        self.svc.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        # Flash combo should fail (quota error on flash → key in cooldown for flash)
        req_flash = make_request(
            json.dumps({"model": "flash-combo"}).encode(),
            {"content-type": "application/json"},
        )
        resp_flash = await self.svc.handle_openai_request(req_flash)
        self.assertEqual(resp_flash.status_code, 503)

        # Other combo should succeed (key not in cooldown for "other")
        req_other = make_request(
            json.dumps({"model": "other-combo"}).encode(),
            {"content-type": "application/json"},
        )
        resp_other = await self.svc.handle_openai_request(req_other)
        self.assertEqual(resp_other.status_code, 200)

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    async def test_streaming_rotates_on_json_error_response(self) -> None:
        self.svc = make_service()
        calls: list[str] = []

        def handler(r: httpx.Request) -> httpx.Response:
            calls.append(r.headers["authorization"])
            if r.headers["authorization"] == "Bearer key-1":
                return httpx.Response(
                    429,
                    headers={"content-type": "application/json"},
                    json={"error": {"type": "quota_exceeded_error"}},
                )
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=b'data: {"ok": true}\n\n',
            )

        self.svc.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        body = json.dumps({"model": "my-combo", "stream": True}).encode()
        request = make_request(body, {"content-type": "application/json"})
        response = await self.svc.handle_openai_request(request)
        chunks = [chunk async for chunk in response.body_iterator]

        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(chunks), b'data: {"ok": true}\n\n')
        self.assertIn("Bearer key-1", calls)
        self.assertIn("Bearer key-2", calls)

    # ------------------------------------------------------------------
    # Response header filtering
    # ------------------------------------------------------------------

    async def test_response_filters_compression_headers(self) -> None:
        import gzip
        self.svc = make_service()

        def handler(r: httpx.Request) -> httpx.Response:
            compressed = gzip.compress(b'{"ok": true}')
            return httpx.Response(
                200,
                headers={
                    "content-type": "application/json",
                    "content-encoding": "gzip",
                    "content-length": str(len(compressed)),
                    "x-custom-header": "test-value",
                },
                content=compressed,
            )

        self.svc.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        request = make_request(combo_body(), {"content-type": "application/json"})
        response = await self.svc.handle_openai_request(request)

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("content-encoding", response.headers)
        self.assertEqual(response.headers.get("x-custom-header"), "test-value")
