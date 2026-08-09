"""Core proxy logic with two-level retry: combo member selection + key rotation."""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import AsyncGenerator
from time import perf_counter

import httpx
from fastapi import Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from jsonpath_ng import parse as jsonpath_parse

from .combo_router import ComboRouter
from .config import AppConfig, HealthCheckRule, ProviderConfig
from .key_manager import ProviderKeyManager

logger = logging.getLogger(__name__)

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

RESPONSE_HEADERS_TO_IGNORE = HOP_BY_HOP_HEADERS | {"content-encoding", "content-length"}


def _filter_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in HOP_BY_HOP_HEADERS}


def _filter_response_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in RESPONSE_HEADERS_TO_IGNORE}


class ProxyService:
    """Proxy service with two-level retry: combo member selection and key rotation.

    Handles both streaming (SSE) and non-streaming requests.
    API format (openai/anthropic) is determined by the request path.
    """

    def __init__(
        self,
        config: AppConfig,
        provider_key_managers: dict[str, ProviderKeyManager],
        combo_router: ComboRouter,
        client: httpx.AsyncClient | None = None,
        recorder=None,   # db.Recorder | None — avoids circular import
        report_logger=None,  # report_log.ReportLogger | None
    ) -> None:
        self.config = config
        self.provider_key_managers = provider_key_managers
        self.combo_router = combo_router
        # When a shared client is supplied (hot-reload path) we don't own it
        # and must not close it in aclose().
        self._owns_client = client is None
        self.client = client if client is not None else httpx.AsyncClient(timeout=httpx.Timeout(120.0))
        self._recorder = recorder
        self._report_logger = report_logger
        self._verbose: bool = config.verbose_logging

        self._providers: dict[str, ProviderConfig] = {p.name: p for p in config.providers}

        self._payload_scripts = config.payload_scripts  # list[PayloadScript]

        # Pre-compile jsonpath expressions per provider
        self._provider_rules: dict[str, list[tuple]] = {}
        for provider in config.providers:
            compiled = []
            for rule in provider.health_check_rules:
                try:
                    expr = jsonpath_parse(rule.jsonpath)
                except Exception as e:
                    raise ValueError(
                        f"Invalid jsonpath '{rule.jsonpath}' in provider {provider.name!r}: {e}"
                    ) from e
                compiled.append((expr, rule))
            self._provider_rules[provider.name] = compiled

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    async def handle_openai_request(self, request: Request) -> Response:
        """Handle POST /v1/chat/completions (OpenAI format)."""
        return await self._handle_request(request, api_format="openai")

    async def handle_anthropic_request(self, request: Request) -> Response:
        """Handle POST /v1/messages (Anthropic format)."""
        return await self._handle_request(request, api_format="anthropic")

    async def handle_openai_responses_request(self, request: Request) -> Response:
        """Handle POST /v1/responses (OpenAI Responses API format)."""
        return await self._handle_request(request, api_format="openai-responses")

    async def handle_openai_images_request(self, request: Request) -> Response:
        """Handle POST /v1/images/generations (OpenAI Images format)."""
        return await self._handle_request(request, api_format="openai-images", force_non_stream=True)

    # ------------------------------------------------------------------
    # Core two-level retry loop
    # ------------------------------------------------------------------

    async def _handle_request(self, request: Request, api_format: str, *, force_non_stream: bool = False) -> Response:
        t0 = perf_counter()
        body = await request.body()
        is_stream = False if force_non_stream else self._is_streaming_request(request, body)
        requested_model = self._extract_model(body)

        # Capture client-side request context for verbose logging (zero overhead when off)
        _client_report_ctx: dict | None = None
        if self._verbose and self._report_logger is not None:
            _client_report_ctx = {
                "method": request.method,
                "path": str(request.url.path),
                "headers": dict(request.headers),
                "body": _try_parse_json(body),
            }

        combo = self.combo_router.get_combo(requested_model)
        if combo is None:
            return JSONResponse(
                status_code=400,
                content={"error": f"unknown combo: {requested_model!r}", "type": "proxy_error"},
            )
        if api_format not in combo.api_formats:
            return JSONResponse(
                status_code=400,
                content={
                    "error": (
                        f"combo {requested_model!r} supports formats {combo.api_formats!r}, "
                        f"but request was sent to {api_format!r} endpoint"
                    ),
                    "type": "proxy_error",
                },
            )

        attempted_members: set[tuple[str, str]] = set()

        while True:
            pair = self.combo_router.next_member(requested_model, attempted_members)
            if pair is None:
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": f"all providers exhausted for combo {requested_model!r}",
                        "type": "proxy_error",
                    },
                )

            provider_name, model = pair
            km = self.provider_key_managers[provider_name]
            provider_cfg = self._providers[provider_name]
            target_url = provider_cfg.get_chat_url(api_format)
            # Body after model-name rewrite (pre-payload-rules); used as the
            # baseline each key attempt so payload rules start from the same input.
            _model_rewritten_body = self._rewrite_model(body, model)
            attempted_keys: set[str] = set()
            max_attempts = provider_cfg.max_retries + 1

            for _ in range(max_attempts):
                key = km.get_key(model, attempted_keys)
                if key is None:
                    break

                attempted_keys.add(key)
                headers = self._build_headers(request, key)

                # Run enabled payload scripts in order (chained: output of one is input of next).
                actual_body: bytes = _model_rewritten_body
                matched_payload_parts: list[str] = []
                for ps in self._payload_scripts:
                    if not ps.enabled:
                        continue
                    actual_body, headers, status = _run_payload_script(
                        ps.script,
                        requested_model,
                        str(request.url.path),
                        actual_body,
                        headers,
                    )
                    if status is not None:
                        label = ps.name or f"script@{id(ps)}"
                        matched_payload_parts.append(f"{label}:{status}")
                matched_payload = ", ".join(matched_payload_parts) or None

                # Capture upstream request context for verbose logging.
                _upstream_report_ctx: dict | None = None
                if _client_report_ctx is not None:
                    _upstream_report_ctx = {
                        "url": target_url,
                        "headers": dict(headers),
                        "body": _try_parse_json(actual_body),
                    }

                try:
                    if is_stream:
                        result, matched_rule = await self._proxy_streaming(
                            target_url, headers, actual_body, provider_name, model,
                            combo=requested_model, key=key, t0=t0, api_format=api_format,
                            client_ctx=_client_report_ctx,
                            upstream_ctx=_upstream_report_ctx,
                            matched_payload=matched_payload,
                        )
                    else:
                        result, matched_rule = await self._proxy_non_streaming(
                            target_url, headers, actual_body, provider_name, model,
                        )

                    if matched_rule is not None:
                        km.record_error(key, model, matched_rule.cooldown_seconds)
                        logger.info(
                            "Rotation: provider=%s key=%s model=%s rule=%r cooldown=%ds",
                            provider_name, key[:8], model,
                            matched_rule.description, matched_rule.cooldown_seconds,
                        )
                        self._record(
                            combo=requested_model, provider=provider_name, model=model,
                            key_prefix=key[:8], api_format=api_format, is_stream=is_stream,
                            status_code=None, success=False,
                            matched_rule=matched_rule.description,
                            usage={}, t0=t0,
                            matched_payload=matched_payload,
                        )
                        continue

                    # Non-streaming: record here (streaming records in _forward finally)
                    if not is_stream and result is not None:
                        raw_body = bytes(result.body) if hasattr(result, "body") else b""
                        http_ok = result.status_code < 400
                        usage = _extract_usage(raw_body, api_format) if http_ok else {}
                        if http_ok:
                            km.record_success(key, model)
                        err_text: str | None = None
                        if not http_ok:
                            try:
                                err_text = json.loads(raw_body).get("error", {})
                                if isinstance(err_text, dict):
                                    err_text = err_text.get("message") or str(err_text)
                                err_text = str(err_text)
                            except Exception:
                                err_text = raw_body.decode(errors="replace")[:200]
                        self._record(
                            combo=requested_model, provider=provider_name, model=model,
                            key_prefix=key[:8], api_format=api_format, is_stream=False,
                            status_code=result.status_code, success=http_ok,
                            matched_rule=None, usage=usage, t0=t0,
                            error=err_text,
                            matched_payload=matched_payload,
                        )
                        # Verbose report (non-streaming)
                        if _client_report_ctx is not None and _upstream_report_ctx is not None:
                            resp_headers = dict(result.headers) if hasattr(result, "headers") else {}
                            self._report(
                                combo=requested_model, provider=provider_name, model=model,
                                api_format=api_format, is_stream=False,
                                status_code=result.status_code, success=http_ok,
                                duration_ms=int((perf_counter() - t0) * 1000),
                                client_ctx=_client_report_ctx,
                                upstream_ctx=_upstream_report_ctx,
                                response_ctx={
                                    "status_code": result.status_code,
                                    "headers": resp_headers,
                                    "body": _try_parse_json(raw_body),
                                },
                            )
                    else:
                        km.record_success(key, model)
                    return result  # type: ignore[return-value]

                except httpx.TimeoutException:
                    logger.warning("Upstream timeout: provider=%s key=%s", provider_name, key[:8])
                    self._record(
                        combo=requested_model, provider=provider_name, model=model,
                        key_prefix=key[:8], api_format=api_format, is_stream=is_stream,
                        status_code=504, success=False, matched_rule=None,
                        usage={}, t0=t0, error="upstream timeout",
                        matched_payload=matched_payload,
                    )
                    return JSONResponse(
                        status_code=504,
                        content={"error": "upstream timeout", "type": "proxy_error"},
                    )
                except httpx.ConnectError:
                    logger.error("Connection failed: provider=%s key=%s", provider_name, key[:8])
                    self._record(
                        combo=requested_model, provider=provider_name, model=model,
                        key_prefix=key[:8], api_format=api_format, is_stream=is_stream,
                        status_code=502, success=False, matched_rule=None,
                        usage={}, t0=t0, error="upstream connection failed",
                        matched_payload=matched_payload,
                    )
                    return JSONResponse(
                        status_code=502,
                        content={"error": "upstream connection failed", "type": "proxy_error"},
                    )

            attempted_members.add(pair)
            logger.info(
                "All keys exhausted for provider=%s model=%s, trying next member",
                provider_name, model,
            )

    # ------------------------------------------------------------------
    # Recording helper
    # ------------------------------------------------------------------

    def _record(
        self,
        *,
        combo: str,
        provider: str,
        model: str,
        key_prefix: str,
        api_format: str,
        is_stream: bool,
        status_code: int | None,
        success: bool,
        matched_rule: str | None,
        usage: dict,
        t0: float,
        error: str | None = None,
        matched_payload: str | None = None,
    ) -> None:
        if self._recorder is None:
            return
        self._recorder.record({
            "ts": time.time(),
            "combo": combo,
            "provider": provider,
            "model": model,
            "key_prefix": key_prefix,
            "api_format": api_format,
            "is_stream": 1 if is_stream else 0,
            "status_code": status_code,
            "success": 1 if success else 0,
            "matched_rule": matched_rule,
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "cache_read_tokens": usage.get("cache_read_tokens"),
            "cache_write_tokens": usage.get("cache_write_tokens"),
            "duration_ms": int((perf_counter() - t0) * 1000),
            "error": error,
            "matched_payload": matched_payload,
        })

    def _report(
        self,
        *,
        combo: str,
        provider: str,
        model: str,
        api_format: str,
        is_stream: bool,
        status_code: int | None,
        success: bool,
        duration_ms: int,
        client_ctx: dict,
        upstream_ctx: dict,
        response_ctx: dict,
    ) -> None:
        """Log a full verbose report record.  No-op when verbose logging is off."""
        if not self._verbose or self._report_logger is None:
            return
        self._report_logger.log({
            "ts": time.time(),
            "combo": combo,
            "provider": provider,
            "model": model,
            "api_format": api_format,
            "is_stream": is_stream,
            "status_code": status_code,
            "success": success,
            "duration_ms": duration_ms,
            "request": {
                "client": client_ctx,
                "upstream": upstream_ctx,
            },
            "response": response_ctx,
        })

    # ------------------------------------------------------------------
    # Request helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_model(body: bytes) -> str:
        try:
            return str(json.loads(body).get("model", ""))
        except (json.JSONDecodeError, AttributeError):
            return ""

    @staticmethod
    def _rewrite_model(body: bytes, model: str) -> bytes:
        try:
            data = json.loads(body)
            data["model"] = model
            return json.dumps(data, ensure_ascii=False).encode()
        except (json.JSONDecodeError, TypeError):
            return body

    @staticmethod
    def _is_streaming_request(request: Request, body: bytes = b"") -> bool:
        accept = request.headers.get("accept", "").lower()
        if "text/event-stream" in accept:
            return True
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
        headers = _filter_headers(dict(request.headers))
        headers.pop("authorization", None)
        headers["authorization"] = f"Bearer {api_key}"
        headers.pop("host", None)
        # Remove content-length so httpx recomputes it from the rewritten body.
        headers.pop("content-length", None)
        return headers

    # ------------------------------------------------------------------
    # Rotation-rule matching
    # ------------------------------------------------------------------

    def _match_rotation_rules(
        self, body: bytes, provider_name: str, model: str
    ) -> HealthCheckRule | None:
        """Return the first matching HealthCheckRule, or None."""
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return None

        for expr, rule in self._provider_rules.get(provider_name, []):
            if rule.models and model not in rule.models:
                continue
            for match in expr.find(data):
                if self._value_matches(match.value, rule.match_value, rule.match_type):
                    return rule
        return None

    @staticmethod
    def _value_matches(value: object, match_value: str, match_type: str) -> bool:
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
        target_url: str,
        headers: dict[str, str],
        body: bytes,
        provider_name: str,
        model: str,
    ) -> tuple[Response | None, HealthCheckRule | None]:
        upstream_resp = await self.client.post(target_url, headers=headers, content=body)
        resp_body = await upstream_resp.aread()

        matched_rule = self._match_rotation_rules(resp_body, provider_name, model)
        if matched_rule is not None:
            return None, matched_rule

        resp_headers = _filter_response_headers(dict(upstream_resp.headers))
        return (
            Response(
                content=resp_body,
                status_code=upstream_resp.status_code,
                headers=resp_headers,
                media_type=upstream_resp.headers.get("content-type"),
            ),
            None,
        )

    # ------------------------------------------------------------------
    # Streaming proxy
    # ------------------------------------------------------------------

    async def _proxy_streaming(
        self,
        target_url: str,
        headers: dict[str, str],
        body: bytes,
        provider_name: str,
        model: str,
        *,
        combo: str = "",
        key: str = "",
        t0: float = 0.0,
        api_format: str = "",
        client_ctx: dict | None = None,
        upstream_ctx: dict | None = None,
        matched_payload: str | None = None,
    ) -> tuple[Response | None, HealthCheckRule | None]:
        req = self.client.build_request("POST", target_url, headers=headers, content=body)
        upstream_resp = await self.client.send(req, stream=True)
        resp_headers = _filter_response_headers(dict(upstream_resp.headers))
        media_type = upstream_resp.headers.get("content-type", "text/event-stream")
        status_code = upstream_resp.status_code

        if "text/event-stream" not in media_type.lower():
            resp_body = await upstream_resp.aread()
            await upstream_resp.aclose()
            matched_rule = self._match_rotation_rules(resp_body, provider_name, model)
            if matched_rule is not None:
                return None, matched_rule
            # Non-SSE error response (e.g. 404, 400) — record and pass through
            if status_code >= 400:
                err_text: str | None = None
                try:
                    err_obj = json.loads(resp_body).get("error", {})
                    if isinstance(err_obj, dict):
                        err_text = err_obj.get("message") or str(err_obj)
                    else:
                        err_text = str(err_obj)
                except Exception:
                    err_text = resp_body.decode(errors="replace")[:200]
                self._record(
                    combo=combo, provider=provider_name, model=model,
                    key_prefix=key[:8] if key else "",
                    api_format=api_format, is_stream=True,
                    status_code=status_code, success=False,
                    matched_rule=None, usage={}, t0=t0, error=err_text,
                    matched_payload=matched_payload,
                )
                if client_ctx is not None and upstream_ctx is not None:
                    self._report(
                        combo=combo, provider=provider_name, model=model,
                        api_format=api_format, is_stream=True,
                        status_code=status_code, success=False,
                        duration_ms=int((perf_counter() - t0) * 1000),
                        client_ctx=client_ctx,
                        upstream_ctx=upstream_ctx,
                        response_ctx={
                            "status_code": status_code,
                            "headers": dict(resp_headers),
                            "body": _try_parse_json(resp_body),
                        },
                    )
            return (
                Response(
                    content=resp_body,
                    status_code=status_code,
                    headers=resp_headers,
                    media_type=media_type,
                ),
                None,
            )

        # Buffer until first SSE event boundary
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

        matched_rule = self._check_sse_error(first_chunk, provider_name, model)
        if matched_rule is not None:
            await upstream_resp.aclose()
            return None, matched_rule

        # Capture references for the closure (value snapshots protect against
        # hot-reload replacing self attributes mid-stream).
        recorder = self._recorder
        verbose = self._verbose
        report_logger = self._report_logger
        # Narrow verbose-ctx pair for the closure (both are set together or not at all).
        _verbose_ctx = (client_ctx, upstream_ctx) if (client_ctx is not None and upstream_ctx is not None) else None
        # Usage sniffing state: pending holds cross-chunk partial SSE lines
        usage_holder: dict = {"pending": bytearray(), "usage": {}}

        async def _forward() -> AsyncGenerator[bytes, None]:
            # Accumulate response bytes for verbose logging (only when enabled)
            resp_accumulator: bytearray | None = bytearray() if (verbose and report_logger is not None) else None
            try:
                if first_chunk:
                    _sniff_usage_chunk(first_chunk, api_format, usage_holder)
                    if resp_accumulator is not None:
                        resp_accumulator.extend(first_chunk)
                    yield first_chunk
                async for chunk in upstream_iter:
                    _sniff_usage_chunk(chunk, api_format, usage_holder)
                    if resp_accumulator is not None:
                        resp_accumulator.extend(chunk)
                    yield chunk
            finally:
                await upstream_resp.aclose()
                if recorder is not None:
                    self._record(
                        combo=combo, provider=provider_name, model=model,
                        key_prefix=key[:8] if key else "",
                        api_format=api_format, is_stream=True,
                        status_code=status_code, success=status_code < 400,
                        matched_rule=None,
                        usage=usage_holder["usage"],
                        t0=t0,
                        matched_payload=matched_payload,
                    )
                # Verbose report (streaming)
                if resp_accumulator is not None and _verbose_ctx is not None:
                    _v_client, _v_upstream = _verbose_ctx
                    self._report(
                        combo=combo, provider=provider_name, model=model,
                        api_format=api_format, is_stream=True,
                        status_code=status_code, success=status_code < 400,
                        duration_ms=int((perf_counter() - t0) * 1000),
                        client_ctx=_v_client,
                        upstream_ctx=_v_upstream,
                        response_ctx={
                            "status_code": status_code,
                            "headers": dict(resp_headers),
                            "body": _try_parse_json(bytes(resp_accumulator)),
                        },
                    )

        return (
            StreamingResponse(
                content=_forward(),
                status_code=status_code,
                headers=resp_headers,
                media_type=media_type,
            ),
            None,
        )

    def _check_sse_error(
        self, sse_data: bytes, provider_name: str, model: str
    ) -> HealthCheckRule | None:
        text = sse_data.decode("utf-8", errors="replace")
        for line in text.split("\n"):
            line = line.strip()
            if not line.startswith("data:"):
                continue
            json_str = line[5:].strip()
            if json_str == "[DONE]":
                continue
            try:
                parsed = json.loads(json_str)
            except json.JSONDecodeError:
                continue
            for expr, rule in self._provider_rules.get(provider_name, []):
                if rule.models and model not in rule.models:
                    continue
                for m in expr.find(parsed):
                    if self._value_matches(m.value, rule.match_value, rule.match_type):
                        return rule
        return None


# ---------------------------------------------------------------------------
# Module-level token-extraction utilities
# ---------------------------------------------------------------------------

def _try_parse_json(data: bytes | str) -> object:
    """Try to parse *data* as JSON; return the string on failure."""
    raw = data if isinstance(data, str) else data.decode("utf-8", errors="replace")
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw


def _extract_usage(body: bytes, api_format: str) -> dict:
    """Parse the ``usage`` field from a completed (non-streaming) response body.

    Returns a dict with keys ``prompt_tokens``, ``completion_tokens``,
    ``total_tokens``, ``cache_read_tokens``, ``cache_write_tokens``
    (all may be None if parsing fails or fields are absent).
    """
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return {}
    u = data.get("usage") or {}
    if api_format == "anthropic":
        inp = u.get("input_tokens")
        out = u.get("output_tokens")
        total = (inp + out) if (inp is not None and out is not None) else None
        return {
            "prompt_tokens": inp,
            "completion_tokens": out,
            "total_tokens": total,
            "cache_read_tokens": u.get("cache_read_input_tokens"),
            "cache_write_tokens": u.get("cache_creation_input_tokens"),
        }
    # OpenAI / openai-responses format
    details = u.get("prompt_tokens_details") or {}
    return {
        "prompt_tokens": u.get("prompt_tokens"),
        "completion_tokens": u.get("completion_tokens"),
        "total_tokens": u.get("total_tokens"),
        "cache_read_tokens": details.get("cached_tokens"),
        "cache_write_tokens": None,  # OpenAI does not expose cache write count
    }


def _sniff_usage_chunk(chunk: bytes, api_format: str, holder: dict) -> None:
    """Sniff token usage from an SSE chunk, updating *holder* in place.

    *holder* must have keys ``"pending"`` (bytearray) and ``"usage"`` (dict).
    Cross-chunk SSE line boundaries are handled via the ``pending`` buffer.

    For OpenAI streams:  the final data chunk with ``usage`` present
    (requires ``stream_options.include_usage=true`` in the request) is parsed.
    For Anthropic streams: ``message_start`` input_tokens +
    ``message_delta`` output_tokens are accumulated.
    """
    pending: bytearray = holder["pending"]
    pending.extend(chunk)
    # Process complete lines only (split on \n, keep trailing partial)
    text = pending.decode("utf-8", errors="replace")
    lines = text.split("\n")
    holder["pending"] = bytearray(lines[-1].encode("utf-8", errors="replace"))

    for raw_line in lines[:-1]:
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        json_str = line[5:].strip()
        if json_str == "[DONE]":
            continue
        try:
            event = json.loads(json_str)
        except json.JSONDecodeError:
            continue

        if api_format == "anthropic":
            # message_start: {"type":"message_start","message":{"usage":{"input_tokens":N}}}
            if event.get("type") == "message_start":
                u = (event.get("message") or {}).get("usage") or {}
                if u.get("input_tokens") is not None:
                    holder["usage"]["prompt_tokens"] = u["input_tokens"]
                if u.get("cache_read_input_tokens") is not None:
                    holder["usage"]["cache_read_tokens"] = u["cache_read_input_tokens"]
                if u.get("cache_creation_input_tokens") is not None:
                    holder["usage"]["cache_write_tokens"] = u["cache_creation_input_tokens"]
            # message_delta: {"type":"message_delta","usage":{"output_tokens":N}}
            elif event.get("type") == "message_delta":
                u = event.get("usage") or {}
                if u.get("output_tokens") is not None:
                    holder["usage"]["completion_tokens"] = u["output_tokens"]
                    inp = holder["usage"].get("prompt_tokens")
                    out = u["output_tokens"]
                    holder["usage"]["total_tokens"] = (
                        (inp + out) if inp is not None else out
                    )
        else:
            # OpenAI: final chunk with usage field (stream_options.include_usage=true)
            u = event.get("usage") or {}
            if u.get("total_tokens") is not None:
                holder["usage"]["prompt_tokens"] = u.get("prompt_tokens")
                holder["usage"]["completion_tokens"] = u.get("completion_tokens")
                holder["usage"]["total_tokens"] = u.get("total_tokens")
                details = u.get("prompt_tokens_details") or {}
                cached = details.get("cached_tokens")
                if cached is not None:
                    holder["usage"]["cache_read_tokens"] = cached


# ---------------------------------------------------------------------------
# Payload script execution
# ---------------------------------------------------------------------------

class _Req:
    """Mutable request context passed to the user's payload script.

    Attributes the script may read and modify:
        combo   – the client-facing combo/model name (str, read-only by convention)
        path    – request path, e.g. "/v1/chat/completions" (str, read-only)
        method  – HTTP method, always "POST" (str, read-only)
        body    – parsed JSON body (dict); modify fields in-place or replace entirely
        headers – request headers dict; modify in-place
        raw_body – original body bytes, set only when body is not valid JSON
    """

    __slots__ = ("combo", "path", "method", "body", "headers", "raw_body")

    def __init__(
        self,
        combo: str,
        path: str,
        body: dict,
        headers: dict[str, str],
        raw_body: bytes = b"",
    ) -> None:
        self.combo = combo
        self.path = path
        self.method = "POST"
        self.body = body
        self.headers = headers
        self.raw_body = raw_body


def _run_payload_script(
    script: str,
    combo_name: str,
    path: str,
    body: bytes,
    headers: dict[str, str],
) -> tuple[bytes, dict[str, str], str | None]:
    """Execute *script* against a ``request`` context and return the rewritten body,
    headers, and an execution status string (``None`` = not executed / no-op).

    The script runs with ``exec`` in a fresh namespace containing only
    ``request`` (a ``_Req`` instance) and the standard builtins.

    On any exception the original body and headers are returned unchanged;
    a warning is logged and the exception summary is returned as the status.
    Empty script is a no-op (returns originals immediately).
    """
    if not script or not script.strip():
        return body, headers, None

    # Parse body; set raw_body when not valid JSON so the script can still inspect it.
    try:
        body_dict: dict = json.loads(body)
        raw_body = b""
    except (json.JSONDecodeError, ValueError):
        body_dict = {}
        raw_body = body

    req = _Req(
        combo=combo_name,
        path=path,
        body=body_dict,
        headers=dict(headers),  # copy so the script works on a fresh dict
        raw_body=raw_body,
    )

    try:
        exec(script, {"request": req, "__builtins__": __builtins__})  # noqa: S102
    except Exception as exc:
        logger.warning("payload script raised %s: %s", type(exc).__name__, exc)
        return body, headers, f"error: {type(exc).__name__}: {exc}"

    # Re-encode body only when it was valid JSON (script may have mutated body_dict).
    if raw_body:
        new_body = body  # not JSON; return as-is
    else:
        try:
            new_body = json.dumps(req.body, ensure_ascii=False).encode()
        except Exception as exc:
            logger.warning("payload script: failed to re-encode body: %s", exc)
            new_body = body

    return new_body, req.headers, "ok"
