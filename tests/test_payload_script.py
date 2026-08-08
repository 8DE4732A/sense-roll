"""Tests for proxy._run_payload_script — body rewrite, header ops, edge cases."""

from __future__ import annotations

import json
import unittest

from sense_roll.proxy import _run_payload_script


def _body(**kwargs) -> bytes:
    return json.dumps(kwargs, ensure_ascii=False).encode()


class RunPayloadScriptEmptyTests(unittest.TestCase):
    def test_empty_script_returns_original(self) -> None:
        body = _body(model="fast")
        headers = {"content-type": "application/json"}
        new_body, new_headers, status = _run_payload_script("", "fast", "/v1/chat/completions", body, headers)
        self.assertEqual(new_body, body)
        self.assertEqual(new_headers, headers)
        self.assertIsNone(status)

    def test_whitespace_only_script_is_noop(self) -> None:
        body = _body(model="x")
        _, _, status = _run_payload_script("   \n  ", "x", "/v1/messages", body, {})
        self.assertIsNone(status)


class RunPayloadScriptBodyTests(unittest.TestCase):
    def test_modify_existing_field(self) -> None:
        script = "request.body['thinking']['budget_tokens'] = 1024"
        body = _body(thinking={"type": "enabled", "budget_tokens": 8000})
        new_body, _, status = _run_payload_script(script, "fast", "/v1/chat/completions", body, {})
        self.assertEqual(json.loads(new_body)["thinking"]["budget_tokens"], 1024)
        self.assertEqual(status, "ok")

    def test_add_new_field(self) -> None:
        script = "request.body.setdefault('metadata', {})['injected'] = True"
        body = _body(model="fast")
        new_body, _, _ = _run_payload_script(script, "fast", "/v1/chat/completions", body, {})
        self.assertTrue(json.loads(new_body)["metadata"]["injected"])

    def test_delete_field(self) -> None:
        script = "request.body.pop('user_agent', None)"
        body = _body(model="x", user_agent="MyClient/1.0")
        new_body, _, _ = _run_payload_script(script, "x", "/v1/chat/completions", body, {})
        self.assertNotIn("user_agent", json.loads(new_body))

    def test_conditional_on_combo_name(self) -> None:
        script = """
if request.combo == 'fast' and 'thinking' in request.body:
    request.body['thinking']['budget_tokens'] = 1024
"""
        body = _body(thinking={"budget_tokens": 8000})
        new_fast, _, _ = _run_payload_script(script, "fast", "/v1/chat/completions", body, {})
        new_slow, _, _ = _run_payload_script(script, "slow", "/v1/chat/completions", body, {})
        self.assertEqual(json.loads(new_fast)["thinking"]["budget_tokens"], 1024)
        self.assertEqual(json.loads(new_slow)["thinking"]["budget_tokens"], 8000)


class RunPayloadScriptHeaderTests(unittest.TestCase):
    def test_delete_header(self) -> None:
        script = "request.headers.pop('user-agent', None)"
        body = _body()
        _, headers, _ = _run_payload_script(script, "x", "/v1/chat/completions", body,
                                             {"user-agent": "MyClient", "content-type": "application/json"})
        self.assertNotIn("user-agent", headers)
        self.assertIn("content-type", headers)

    def test_set_header(self) -> None:
        script = "request.headers['x-custom'] = 'injected'"
        _, headers, _ = _run_payload_script(script, "x", "/v1/chat/completions", _body(), {})
        self.assertEqual(headers["x-custom"], "injected")

    def test_case_sensitive_header_deletion(self) -> None:
        script = "request.headers.pop('User-Agent', None)"
        _, headers, _ = _run_payload_script(script, "x", "/v1/chat/completions", _body(),
                                             {"User-Agent": "MyClient"})
        self.assertNotIn("User-Agent", headers)


class RunPayloadScriptEdgeCaseTests(unittest.TestCase):
    def test_syntax_error_returns_original(self) -> None:
        script = "this is not valid python [[["
        body = _body(model="x")
        headers = {"content-type": "application/json"}
        new_body, new_headers, status = _run_payload_script(script, "x", "/v1/chat/completions", body, headers)
        self.assertEqual(new_body, body)
        self.assertEqual(new_headers, headers)
        self.assertIsNotNone(status)
        self.assertIn("error", status)

    def test_runtime_error_returns_original(self) -> None:
        script = "raise ValueError('deliberate error')"
        body = _body(model="x")
        new_body, _, status = _run_payload_script(script, "x", "/v1/chat/completions", body, {})
        self.assertEqual(new_body, body)
        self.assertIn("ValueError", status)

    def test_non_json_body_preserved(self) -> None:
        script = "request.headers['x-ran'] = 'yes'"
        raw_body = b"not json at all"
        new_body, headers, status = _run_payload_script(script, "x", "/v1/chat/completions", raw_body, {})
        # body unchanged (not JSON)
        self.assertEqual(new_body, raw_body)
        # header mutation still applied
        self.assertEqual(headers["x-ran"], "yes")
        self.assertEqual(status, "ok")

    def test_script_can_access_combo_name(self) -> None:
        script = "request.body['combo_seen'] = request.combo"
        new_body, _, _ = _run_payload_script(script, "my-combo", "/v1/chat/completions", _body(), {})
        self.assertEqual(json.loads(new_body)["combo_seen"], "my-combo")

    def test_script_can_access_path(self) -> None:
        script = "request.body['path_seen'] = request.path"
        new_body, _, _ = _run_payload_script(script, "x", "/v1/messages", _body(), {})
        self.assertEqual(json.loads(new_body)["path_seen"], "/v1/messages")


class PayloadScriptsChainTests(unittest.TestCase):
    """Tests for chained execution via ProxyService._payload_scripts logic.

    We test the chaining behavior directly using _run_payload_script in a loop,
    mirroring what ProxyService does.
    """

    def _chain(self, scripts: list[tuple[str, bool]], body: bytes, headers: dict) -> tuple[bytes, dict, list[str]]:
        """Execute a list of (script, enabled) pairs in chain order."""
        matched = []
        for name, enabled, script in scripts:
            if not enabled:
                continue
            body, headers, status = _run_payload_script(script, "fast", "/v1/chat/completions", body, headers)
            if status is not None:
                matched.append(f"{name}:{status}")
        return body, headers, matched

    def _scripts(self, entries):
        return [(e[0], e[1], e[2]) for e in entries]

    def test_two_scripts_chain(self) -> None:
        scripts = self._scripts([
            ("s1", True, "request.body['a'] = 1"),
            ("s2", True, "request.body['b'] = 2"),
        ])
        new_body, _, matched = self._chain(scripts, _body(), {})
        data = json.loads(new_body)
        self.assertEqual(data["a"], 1)
        self.assertEqual(data["b"], 2)
        self.assertEqual(len(matched), 2)
        self.assertIn("s1:ok", matched)
        self.assertIn("s2:ok", matched)

    def test_disabled_script_skipped(self) -> None:
        scripts = self._scripts([
            ("s1", True,  "request.body['ran'] = True"),
            ("s2", False, "request.body['skipped'] = True"),
        ])
        new_body, _, matched = self._chain(scripts, _body(), {})
        data = json.loads(new_body)
        self.assertTrue(data.get("ran"))
        self.assertNotIn("skipped", data)
        # Only s1 appears in matched
        self.assertEqual(len(matched), 1)
        self.assertIn("s1", matched[0])

    def test_later_script_sees_earlier_mutation(self) -> None:
        scripts = self._scripts([
            ("s1", True, "request.body['x'] = 10"),
            ("s2", True, "request.body['x'] = request.body['x'] * 3"),
        ])
        new_body, _, _ = self._chain(scripts, _body(x=0), {})
        self.assertEqual(json.loads(new_body)["x"], 30)

    def test_error_in_one_script_continues_chain(self) -> None:
        scripts = self._scripts([
            ("s1", True, "raise ValueError('deliberate')"),
            ("s2", True, "request.body['after_error'] = True"),
        ])
        new_body, _, matched = self._chain(scripts, _body(), {})
        # s2 still ran on the (unchanged) body from s1's failure
        self.assertTrue(json.loads(new_body).get("after_error"))
        self.assertIn("ValueError", matched[0])
        self.assertIn("s2:ok", matched[1])

    def test_all_disabled_returns_original(self) -> None:
        scripts = self._scripts([
            ("s1", False, "request.body['ran'] = True"),
            ("s2", False, "request.body['also'] = True"),
        ])
        original = _body(model="fast")
        new_body, headers, matched = self._chain(scripts, original, {})
        self.assertEqual(new_body, original)
        self.assertEqual(matched, [])
