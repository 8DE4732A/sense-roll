from __future__ import annotations

import json
import unittest

import httpx
from starlette.requests import Request

from sense_roll.config import AppConfig, KeyConfig, ProxyConfig, RotationRule
from sense_roll.key_manager import KeyManager
from sense_roll.proxy import ProxyService


def make_request(body: bytes, headers: dict[str, str] | None = None) -> Request:
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
        "path": "/v1/chat/completions",
        "headers": raw_headers,
        "query_string": b"",
    }
    return Request(scope, receive)


def app_config(max_retries: int = 3, cooldown_seconds: int = 60) -> AppConfig:
    return AppConfig(
        proxy=ProxyConfig(
            target_url="https://upstream.test/v1/chat/completions",
            max_retries=max_retries,
            key_cooldown_seconds=cooldown_seconds,
        ),
        keys=[KeyConfig("key-1"), KeyConfig("key-2")],
        rotation_rules=[
            RotationRule(
                jsonpath="$.error.type",
                match_value="quota_exceeded_error",
                match_type="equals",
            )
        ],
    )


class ProxyServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        service = getattr(self, "service", None)
        if service is not None:
            await service.aclose()

    async def test_stream_true_body_marks_request_as_streaming(self) -> None:
        body = json.dumps({"stream": True}).encode()
        request = make_request(body, {"content-type": "application/json"})

        self.assertTrue(ProxyService._is_streaming_request(request, body))

    async def test_non_streaming_rotation_counts_failed_key_once(self) -> None:
        km = KeyManager(["key-1", "key-2"], cooldown_seconds=60)
        self.service = ProxyService(app_config(), km)
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.headers["authorization"])
            if request.headers["authorization"] == "Bearer key-1":
                return httpx.Response(
                    429,
                    json={"error": {"type": "quota_exceeded_error"}},
                )
            return httpx.Response(200, json={"ok": True})

        self.service.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        request = make_request(b"{}", {"content-type": "application/json"})

        response = await self.service.handle_proxy_request(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, ["Bearer key-1", "Bearer key-2"])
        stats = km.get_stats()
        self.assertEqual(stats[0]["use_count"], 1)
        self.assertEqual(stats[0]["error_count"], 1)

    async def test_single_key_rotation_error_does_not_retry_forever(self) -> None:
        config = app_config(max_retries=10, cooldown_seconds=0)
        config.keys = [KeyConfig("key-1")]
        km = KeyManager(["key-1"], cooldown_seconds=0)
        self.service = ProxyService(config, km)
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(429, json={"error": {"type": "quota_exceeded_error"}})

        self.service.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        request = make_request(b"{}", {"content-type": "application/json"})

        response = await self.service.handle_proxy_request(request)

        self.assertEqual(response.status_code, 429)
        self.assertEqual(calls, 1)

    async def test_all_keys_in_cooldown_returns_503_without_upstream_call(self) -> None:
        config = app_config(max_retries=10, cooldown_seconds=60)
        config.keys = [KeyConfig("key-1")]
        km = KeyManager(["key-1"], cooldown_seconds=60)
        km.record_usage("key-1", is_error=True)
        self.service = ProxyService(config, km)
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={"ok": True})

        self.service.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        request = make_request(b"{}", {"content-type": "application/json"})

        response = await self.service.handle_proxy_request(request)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(calls, 0)

    async def test_streaming_request_rotates_on_json_error_response(self) -> None:
        km = KeyManager(["key-1", "key-2"], cooldown_seconds=60)
        self.service = ProxyService(app_config(), km)
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.headers["authorization"])
            if request.headers["authorization"] == "Bearer key-1":
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

        self.service.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        body = json.dumps({"stream": True}).encode()
        request = make_request(body, {"content-type": "application/json"})

        response = await self.service.handle_proxy_request(request)
        chunks = [chunk async for chunk in response.body_iterator]

        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(chunks), b'data: {"ok": true}\n\n')
        self.assertEqual(calls, ["Bearer key-1", "Bearer key-2"])

    async def test_response_filters_compression_headers(self) -> None:
        import gzip
        km = KeyManager(["key-1"], cooldown_seconds=60)
        self.service = ProxyService(app_config(), km)

        def handler(request: httpx.Request) -> httpx.Response:
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

        self.service.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        request = make_request(b"{}", {"content-type": "application/json"})

        response = await self.service.handle_proxy_request(request)

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("content-encoding", response.headers)
        # Content-length should be recalculated by Starlette to 12 (decompressed size) instead of gzip size
        self.assertEqual(response.headers.get("content-length"), "12")
        self.assertEqual(response.headers.get("x-custom-header"), "test-value")

    async def test_routing_strategy_fill_first(self) -> None:
        config = app_config()
        config.routing.strategy = "fill-first"
        km = KeyManager(["key-1", "key-2"], cooldown_seconds=60, strategy="fill-first")
        self.service = ProxyService(config, km)
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.headers["authorization"])
            return httpx.Response(200, json={"ok": True})

        self.service.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        
        request1 = make_request(b"{}", {"content-type": "application/json"})
        await self.service.handle_proxy_request(request1)

        request2 = make_request(b"{}", {"content-type": "application/json"})
        await self.service.handle_proxy_request(request2)

        self.assertEqual(calls, ["Bearer key-1", "Bearer key-1"])

    async def test_routing_strategy_round_robin(self) -> None:
        config = app_config()
        config.routing.strategy = "round-robin"
        km = KeyManager(["key-1", "key-2"], cooldown_seconds=60, strategy="round-robin")
        self.service = ProxyService(config, km)
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.headers["authorization"])
            return httpx.Response(200, json={"ok": True})

        self.service.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        
        request1 = make_request(b"{}", {"content-type": "application/json"})
        await self.service.handle_proxy_request(request1)

        request2 = make_request(b"{}", {"content-type": "application/json"})
        await self.service.handle_proxy_request(request2)

        self.assertEqual(set(calls), {"Bearer key-1", "Bearer key-2"})
