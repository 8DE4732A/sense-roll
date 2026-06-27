"""Core proxy logic with key rotation and retry for sense-roll."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncGenerator

import httpx
from fastapi import Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from jsonpath_ng import parse as jsonpath_parse

from .config import AppConfig
from .key_manager import KeyManager

logger = logging.getLogger(__name__)

# Headers that MUST NOT be forwarded (RFC 9113 and common practice)
HOP_BY_HOP_HEADERS = frozenset({
    "connection",
    "keep-alive",
    "transfer-encoding",
    "te",
    "trailer",
    "upgrade",
    "proxy-authorization",
    "proxy-authenticate",
})


def _filter_headers(headers: dict[str, str]) -> dict[str, str]:
    """Remove hop-by-hop headers before forwarding."""
    return {k: v for k, v in headers.items() if k.lower() not in HOP_BY_HOP_HEADERS}


RESPONSE_HEADERS_TO_IGNORE = HOP_BY_HOP_HEADERS | {"content-encoding", "content-length"}


def _filter_response_headers(headers: dict[str, str]) -> dict[str, str]:
    """Remove hop-by-hop and content-length/encoding headers from upstream response."""
    return {k: v for k, v in headers.items() if k.lower() not in RESPONSE_HEADERS_TO_IGNORE}


class ProxyService:
    """FastAPI proxy service that forwards /v1/chat/completions with key rotation.

    Handles both streaming (SSE) and non-streaming requests.
    Detects specified error patterns in upstream responses and
    automatically rotates to the next API key on retry.
    """

    def __init__(self, config: AppConfig, key_manager: KeyManager) -> None:
        self.config = config
        self.key_manager = key_manager
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(120.0))

        # Pre-compile jsonpath rules for performance
        self._rules: list[tuple] = []
        for rule in config.rotation_rules:
            try:
                expr = jsonpath_parse(rule.jsonpath)
            except Exception as e:
                raise ValueError(
                    f"Invalid jsonpath expression '{rule.jsonpath}': {e}"
                ) from e
            self._rules.append((expr, rule.match_value, rule.match_type, rule.action))

    async def aclose(self) -> None:
        """Close the underlying httpx client."""
        await self.client.aclose()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def handle_proxy_request(self, request: Request) -> Response:
        """Main entry point for the ``/v1/chat/completions`` proxy.

        Orchestrates retries with key rotation when configured error
        patterns are detected in the upstream response.
        """
        body = await request.body()
        is_stream = self._is_streaming_request(request, body)
        attempted_keys: set[str] = set()
        attempts = 0
        max_attempts = min(
            self.key_manager.total_keys,
            max(1, self.config.proxy.max_retries + 1),
        )

        key = self.key_manager.get_key(attempted_keys)
        if key is None:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "all keys are cooling down",
                    "type": "proxy_error",
                },
            )

        last_body = b""
        last_status: int | None = None

        while attempts < max_attempts:
            attempted_keys.add(key)
            attempts += 1
            headers = self._build_headers(request, key)

            try:
                if is_stream:
                    result, err_body, err_status = await self._proxy_streaming(
                        headers, body, key
                    )
                else:
                    result, err_body, err_status = await self._proxy_non_streaming(
                        headers, body, key
                    )

                if result is None:
                    last_body = err_body or b""
                    last_status = err_status
                    next_key = self.key_manager.get_key(attempted_keys)
                    if next_key is None:
                        break  # all keys in cooldown
                    key = next_key
                    continue

                return result

            except httpx.TimeoutException:
                logger.warning("Upstream timed out (key=%s)", key[:8])
                return JSONResponse(
                    status_code=504,
                    content={"error": "upstream timeout", "type": "proxy_error"},
                )
            except httpx.ConnectError:
                logger.error("Upstream connection failed (key=%s)", key[:8])
                return JSONResponse(
                    status_code=502,
                    content={
                        "error": "upstream connection failed",
                        "type": "proxy_error",
                    },
                )

        # All retries exhausted — return the last error response
        logger.warning(
            "All %d keys exhausted, returning last error",
            self.key_manager.total_keys,
        )
        if last_body:
            return Response(
                content=last_body,
                status_code=last_status or 502,
                media_type="application/json",
            )
        return JSONResponse(
            status_code=502,
            content={"error": "all keys exhausted with errors", "type": "proxy_error"},
        )

    # ------------------------------------------------------------------
    # Request inspection helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_streaming_request(request: Request, body: bytes = b"") -> bool:
        """Determine whether the client expects a streaming response."""
        accept = request.headers.get("accept", "").lower()
        if "text/event-stream" in accept:
            return True
        # Also check content-type if the client sends it
        content_type = request.headers.get("content-type", "").lower()
        if "text/event-stream" in content_type:
            return True
        if "application/json" in content_type and body:
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                return False
            return payload.get("stream") is True
        return False

    @staticmethod
    def _build_headers(request: Request, api_key: str) -> dict[str, str]:
        """Copy incoming request headers, replace Authorization."""
        headers = dict(request.headers)
        headers = _filter_headers(headers)
        headers.pop("authorization", None)
        headers["authorization"] = f"Bearer {api_key}"
        headers.pop("host", None)
        return headers

    # ------------------------------------------------------------------
    # Rotation-rule matching
    # ------------------------------------------------------------------

    def _match_rotation_rules(self, body: bytes) -> bool:
        """Check *body* (raw JSON bytes) against all compiled rotation rules.

        Returns ``True`` if at least one rule matches.
        """
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return False

        for expr, match_value, match_type, _action in self._rules:
            for match in expr.find(data):
                if self._value_matches(match.value, match_value, match_type):
                    return True
        return False

    @staticmethod
    def _value_matches(value: object, match_value: str, match_type: str) -> bool:
        """Match a JSONPath value using the configured rule mode."""
        value_text = str(value)
        if match_type == "contains":
            return match_value in value_text
        if match_type == "regex":
            return re.search(match_value, value_text) is not None
        return value_text == match_value



    # ------------------------------------------------------------------
    # Non-streaming proxy
    # ------------------------------------------------------------------

    async def _proxy_non_streaming(
        self,
        headers: dict[str, str],
        body: bytes,
        key: str,
    ) -> tuple[Response | None, bytes | None, int | None]:
        """POST to upstream and check the full response for rotation triggers.

        Returns ``(Response, None, None)`` on success, or
        ``(None, error_body_bytes, status_code)`` when a rotation rule
        matched (caller should retry with the next key).
        """
        upstream_resp = await self.client.post(
            self.config.proxy.target_url,
            headers=headers,
            content=body,
        )
        resp_body = await upstream_resp.aread()

        should_rotate = self._match_rotation_rules(resp_body)
        self.key_manager.record_usage(key, is_error=should_rotate)

        if should_rotate:
            logger.info(
                "Rotation triggered by error (key=%s → next)", key[:8]
            )
            return None, resp_body, upstream_resp.status_code

        resp_headers = _filter_response_headers(dict(upstream_resp.headers))
        return (
            Response(
                content=resp_body,
                status_code=upstream_resp.status_code,
                headers=resp_headers,
                media_type=upstream_resp.headers.get("content-type"),
            ),
            None,
            None,
        )

    # ------------------------------------------------------------------
    # Streaming proxy
    # ------------------------------------------------------------------

    async def _proxy_streaming(
        self,
        headers: dict[str, str],
        body: bytes,
        key: str,
    ) -> tuple[Response | None, bytes | None, int | None]:
        """Proxy a streaming (SSE) request with error detection.

        Buffers the first SSE event (delimited by ``\\n\\n``).
        If an error is found ``(None, None, None)`` is returned so the caller
        retries with the next key (status/body are not meaningful for SSE).

        If no error is found ``(StreamingResponse, None, None)`` is returned.
        """
        request = self.client.build_request(
            "POST",
            self.config.proxy.target_url,
            headers=headers,
            content=body,
        )
        upstream_resp = await self.client.send(request, stream=True)
        resp_headers = _filter_response_headers(dict(upstream_resp.headers))
        media_type = upstream_resp.headers.get("content-type", "text/event-stream")

        if "text/event-stream" not in media_type.lower():
            resp_body = await upstream_resp.aread()
            await upstream_resp.aclose()
            should_rotate = self._match_rotation_rules(resp_body)
            self.key_manager.record_usage(key, is_error=should_rotate)
            if should_rotate:
                logger.info(
                    "Streaming request received JSON rotation error (key=%s -> next)",
                    key[:8],
                )
                return None, resp_body, upstream_resp.status_code
            return (
                Response(
                    content=resp_body,
                    status_code=upstream_resp.status_code,
                    headers=resp_headers,
                    media_type=media_type,
                ),
                None,
                None,
            )

        buffer = bytearray()
        upstream_iter = upstream_resp.aiter_bytes()
        while True:
            try:
                chunk = await upstream_iter.__anext__()
            except StopAsyncIteration:
                break
            buffer.extend(chunk)
            if b"\n\n" in buffer or b"\r\n\r\n" in buffer:
                break

        first_chunk = bytes(buffer)

        if first_chunk and self._check_sse_error(first_chunk):
            logger.info(
                "Streaming rotation triggered by error event (key=%s → next)",
                key[:8],
            )
            self.key_manager.record_usage(key, is_error=True)
            await upstream_resp.aclose()
            return None, first_chunk, upstream_resp.status_code

        self.key_manager.record_usage(key, is_error=False)

        async def _forward() -> AsyncGenerator[bytes, None]:
            try:
                if first_chunk:
                    yield first_chunk
                async for chunk in upstream_iter:
                    yield chunk
            finally:
                await upstream_resp.aclose()

        return (
            StreamingResponse(
                content=_forward(),
                status_code=upstream_resp.status_code,
                headers=resp_headers,
                media_type=media_type,
            ),
            None,
            None,
        )

    # ------------------------------------------------------------------
    # SSE error detection
    # ------------------------------------------------------------------

    def _check_sse_error(self, sse_data: bytes) -> bool:
        """Parse SSE text and check each ``data:`` JSON against rotation rules."""
        text = sse_data.decode("utf-8", errors="replace")
        for line in text.split("\n"):
            line = line.strip()
            if line.startswith("data:"):
                json_str = line[5:].strip()
                if json_str == "[DONE]":
                    continue
                try:
                    parsed = json.loads(json_str)
                except json.JSONDecodeError:
                    continue
                for expr, match_value, match_type, _action in self._rules:
                    for m in expr.find(parsed):
                        if self._value_matches(m.value, match_value, match_type):
                            return True
        return False
