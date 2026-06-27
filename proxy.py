"""Core proxy logic with key rotation and retry for sense-roll."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator

import httpx
from fastapi import Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from jsonpath_ng import parse as jsonpath_parse

from config import AppConfig
from key_manager import KeyManager

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
            self._rules.append((expr, rule.match_value, rule.action))

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
        is_stream = self._is_streaming_request(request)
        body = await request.body()
        key = self.key_manager.get_current_key()

        last_response: httpx.Response | None = None
        last_body = b""

        for attempt in range(self.config.proxy.max_retries + 1):
            headers = self._build_headers(request, key)

            try:
                if is_stream:
                    result = await self._proxy_streaming(headers, body, key)
                    if result is None:
                        # Error detected → key was rotated; retry
                        key = self.key_manager.get_current_key()
                        continue
                    return result
                else:
                    result = await self._proxy_non_streaming(headers, body, key)
                    if result is None:
                        key = self.key_manager.get_current_key()
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
                status_code=last_response.status_code if last_response else 502,
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
    def _is_streaming_request(request: Request) -> bool:
        """Determine whether the client expects a streaming response."""
        accept = request.headers.get("accept", "")
        if "text/event-stream" in accept:
            return True
        # Also check content-type if the client sends it
        content_type = request.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            return True
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

        for expr, match_value, _action in self._rules:
            for match in expr.find(data):
                if match.value == match_value:
                    return True
        return False

    # ------------------------------------------------------------------
    # Non-streaming proxy
    # ------------------------------------------------------------------

    async def _proxy_non_streaming(
        self,
        headers: dict[str, str],
        body: bytes,
        key: str,
    ) -> Response | None:
        """POST to upstream and check the full response for rotation triggers.

        Returns a ``Response`` on success, or ``None`` when a rotation rule
        matched (caller should retry with the next key).
        """
        upstream_resp = await self.client.post(
            self.config.proxy.target_url,
            headers=headers,
            content=body,
        )
        resp_body = await upstream_resp.aread()

        self.key_manager.record_usage(key, is_error=False)

        if self._match_rotation_rules(resp_body):
            logger.info(
                "Rotation triggered by error (key=%s → next)", key[:8]
            )
            self.key_manager.record_usage(key, is_error=True)
            self.key_manager.rotate()
            return None

        resp_headers = _filter_headers(dict(upstream_resp.headers))
        return Response(
            content=resp_body,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            media_type=upstream_resp.headers.get("content-type"),
        )

    # ------------------------------------------------------------------
    # Streaming proxy
    # ------------------------------------------------------------------

    async def _proxy_streaming(
        self,
        headers: dict[str, str],
        body: bytes,
        key: str,
    ) -> Response | None:
        """Proxy a streaming (SSE) request with error detection.

        Buffers the first SSE event (delimited by ``\\n\\n``).
        If an error is found the upstream connection is discarded and
        ``None`` is returned so the caller retries with the next key.

        If no error is found the buffered data plus the remaining stream
        is forwarded as a ``StreamingResponse``.
        """
        buffer = bytearray()
        first_event_done = False
        abort = False

        async def _stream_or_abort() -> AsyncGenerator[bytes, None]:
            """State-machine generator that reads from ``aiter_bytes`` exactly once.

            COLLECTING: buffer chunks until ``\\n\\n`` is found, then check error.
            FORWARDING: once the first event is clean, stream remaining chunks.
            """
            nonlocal first_event_done, abort

            forwarding = False

            async with self.client.stream(
                "POST",
                self.config.proxy.target_url,
                headers=headers,
                content=body,
            ) as upstream_resp:
                async for chunk in upstream_resp.aiter_bytes():
                    if forwarding:
                        yield chunk
                        continue

                    buffer.extend(chunk)

                    if not first_event_done and b"\n\n" in buffer:
                        first_event_done = True
                        if self._check_sse_error(bytes(buffer)):
                            abort = True
                            return  # upstream error – discard everything
                        # First event is clean – yield buffered data and switch.
                        forwarding = True
                        yield bytes(buffer)
                        buffer.clear()

                # Stream ended before the first event completed.
                if not abort and buffer:
                    yield bytes(buffer)

        gen = _stream_or_abort()
        first_chunk: bytes | None = None

        try:
            first_chunk = await gen.__anext__()
        except StopAsyncIteration:
            pass

        if abort or first_chunk is None:
            # Error detected in the first SSE event
            logger.info(
                "Streaming rotation triggered by error event (key=%s → next)",
                key[:8],
            )
            self.key_manager.record_usage(key, is_error=True)
            self.key_manager.rotate()
            return None

        self.key_manager.record_usage(key, is_error=False)

        async def _forward() -> AsyncGenerator[bytes, None]:
            yield first_chunk  # type: ignore[union-attr]
            async for chunk in gen:
                yield chunk

        return StreamingResponse(
            content=_forward(),
            media_type="text/event-stream",
        )

    # ------------------------------------------------------------------
    # SSE error detection
    # ------------------------------------------------------------------

    def _check_sse_error(self, sse_data: bytes) -> bool:
        """Parse SSE text and check each ``data:`` JSON against rotation rules."""
        text = sse_data.decode("utf-8", errors="replace")
        for line in text.split("\n"):
            line = line.strip()
            if line.startswith("data: "):
                json_str = line[6:].strip()
                if json_str == "[DONE]":
                    continue
                try:
                    parsed = json.loads(json_str)
                except json.JSONDecodeError:
                    continue
                for expr, match_value, _action in self._rules:
                    for m in expr.find(parsed):
                        if m.value == match_value:
                            return True
        return False