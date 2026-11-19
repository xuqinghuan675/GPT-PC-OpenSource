"""Regression tests for the one-command Chrome / virtual desktop router.

Tests mock BrowserSkill; no Chrome tabs, user data, or desktop switches.
"""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import aiwork


class InputValidation(unittest.TestCase):
    @staticmethod
    def fake_browser(*args, **kwargs):
        if args[0]=="cooperate":
            return {"context_id":"ctx-A","observation_id":"obs-123", "state":{"mode":"ready","editing":False}}
        return {"done":True}

    def test_agent_ids(self):
        for s in ["muse_1", "GPT-2026", "A"*48]:
            self.assertEqual(aiwork.validate_agent(s), s)
        for s in ["", "../muse", "hello world", "A"*49, "x;dir", None]:
            with self.subTest(s=s), self.assertRaises(aiwork.AIWorkError):
                aiwork.validate_agent(s)

    def test_urls(self):
        self.assertEqual(aiwork.valid_url("https://example.com"), "https://example.com")
        for url in ["file:///etc/passwd", "javascript:alert(1)", "data:text/html,a",
                    "http://user:pass@example.com", "example.com", "ftp://example.com"]:
            with self.subTest(url=url), self.assertRaises(aiwork.AIWorkError):
                aiwork.valid_url(url)

    def test_click_requires_observation_for_snapshot_ref(self):
        with patch.object(aiwork, "session_for", return_value="sessA"), \
             patch.object(aiwork, "bsk", side_effect=self.fake_browser):
            with self.assertRaisesRegex(aiwork.AIWorkError, "observation"):
                aiwork.browser_interact("click", "abc", target="@e1")

    def test_fill_is_routed_to_owned_browser_session(self):
        with patch.object(aiwork, "session_for", return_value="sessB"), \
             patch.object(aiwork, "bsk", side_effect=self.fake_browser) as fake:
            result = aiwork.browser_interact(
                "fill", "agentB", target="#q", value="a short test"
            )
        self.assertEqual(result["agent"], "agentB")
        args = fake.call_args.args
        self.assertEqual(args[0], "fill")
        self.assertIn("--selector", args)
        self.assertIn("#q", args)
        self.assertIn("--value", args)
        self.assertIn("sessB", args)

    def test_press_key_is_isolated(self):
        with patch.object(aiwork, "session_for", return_value="sessC"), \
             patch.object(aiwork, "bsk", side_effect=self.fake_browser) as fake:
            result = aiwork.browser_interact("press", "agentC", key="Enter")
        self.assertEqual(result["session_id"], "sessC")
        self.assertEqual(fake.call_args.args[:2], ("press", "Enter"))

    def test_ref_with_observation(self):
        with patch.object(aiwork, "session_for", return_value="s"), \
             patch.object(aiwork, "bsk", side_effect=self.fake_browser) as fake:
            aiwork.browser_interact("click", "abc", target="@e3", observation="obs-123")
        self.assertIn("--ref", fake.call_args.args)
        self.assertIn("--observation", fake.call_args.args)


class CooperationPolicy(unittest.TestCase):
    def test_human_hold_blocks_all_input(self):
        with patch.object(aiwork, "session_for", return_value="session1"), \
             patch.object(aiwork, "bsk", return_value={
                 "state": {"mode":"held", "editing":True}, "context_id":None
             }) as fake:
            with self.assertRaisesRegex(aiwork.AIWorkError, "human has control"):
                aiwork.browser_interact("click","agentA",target="#doit")
        self.assertEqual(fake.call_count, 1)
        self.assertEqual(fake.call_args.args[:2], ("cooperate","sync"))

    def test_untargeted_press_uses_fresh_sync_observation(self):
        def fake(*args, **kwargs):
            if args[0]=="cooperate":
                return {"state":{"mode":"ready","editing":False},
                        "context_id":"ctx-only","observation_id":"obs-latest"}
            return {"pressed":True}
        with patch.object(aiwork, "session_for", return_value="sessA"), \
             patch.object(aiwork, "bsk", side_effect=fake) as browser:
            out=aiwork.browser_interact("press","agentA",key="Escape")
        self.assertEqual(out["operation"], {"pressed":True})
        self.assertIn("--context",browser.call_args.args)
        self.assertIn("ctx-only",browser.call_args.args)
        self.assertIn("obs-latest",browser.call_args.args)


class DurableSessions(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory(prefix="aiwork-tests-")
        self.tmp = Path(self.t.name)
        self.patches = [
            patch.object(aiwork, "DATA", self.tmp),
            patch.object(aiwork, "LOCK", self.tmp/"lock"),
            patch.object(aiwork, "SESSIONS", self.tmp/"state.json"),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        self.t.cleanup()

    def test_start_reuses_session(self):
        live = {"sessions": [{"session_id": "abc1"}], "browsers": [{"browser_name": "chrome", "instance_id": "chrome1"}]}
        (self.tmp/"state.json").write_text('{"agentA":"abc1"}', encoding="utf-8")
        with patch.object(aiwork, "bsk", return_value=live) as fake:
            out = aiwork.start("agentA")
        self.assertFalse(out["created"])
        self.assertEqual(out["session_id"], "abc1")
        self.assertEqual(fake.call_count, 1)

    def test_start_creates_only_one_agent(self):
        def fake(*args, **kwargs):
            if args[0] == "status":
                return {"sessions": [], "browsers": [{"browser_name": "chrome", "instance_id": "chrome1"}]}
            if args[:2] == ("session", "start"):
                return {"session_id": "new12"}
            raise AssertionError(args)
        with patch.object(aiwork, "bsk", side_effect=fake):
            out = aiwork.start("agentA")
        self.assertTrue(out["created"])
        self.assertEqual(aiwork.load(), {"agentA": "new12"})

    def test_navigation_error_is_not_success(self):
        def fake(*args, **kwargs):
            if args[0] == "status":
                return {"sessions": [], "browsers": [{"browser_name": "chrome", "instance_id": "chrome1"}]}
            if args[:2] == ("session", "start"):
                return {"session_id": "new12"}
            if args[0] == "navigate":
                raise aiwork.AIWorkError("network down")
            raise AssertionError(args)
        with patch.object(aiwork, "bsk", side_effect=fake):
            with self.assertRaisesRegex(aiwork.AIWorkError, "navigation failed"):
                aiwork.start("agentA", "https://example.com")
        self.assertEqual(aiwork.load(), {"agentA": "new12"})

    def test_registry_write_failure_cleans_up_new_window(self):
        calls = []
        def fake(*args, **kwargs):
            calls.append(args[:2])
            if args[0] == "status":
                return {"sessions": [], "browsers": [{"browser_name": "chrome", "instance_id": "chrome1"}]}
            if args[:2] == ("session", "start"):
                return {"session_id": "new12"}
            if args[:2] == ("session", "stop"):
                return {"stopped": ["new12"]}
            raise AssertionError(args)
        with patch.object(aiwork, "bsk", side_effect=fake), \
             patch.object(aiwork, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                aiwork.start("agentA")
        self.assertIn(("session", "stop"), calls)

    def test_stop_one_agent_preserves_other(self):
        (self.tmp/"state.json").write_text('{"a":"idA","b":"idB"}', encoding="utf-8")
        def fake(*args, **kwargs):
            if args[0] == "status":
                return {"sessions": [{"session_id":"idA"},{"session_id":"idB"}]}
            if args[:2] == ("session", "stop"):
                return {"stopped": ["idA"]}
            raise AssertionError(args)
        with patch.object(aiwork, "bsk", side_effect=fake):
            out = aiwork.stop("a")
        self.assertTrue(out["stopped"])
        self.assertEqual(aiwork.load(), {"b": "idB"})

    def test_health_reports_desktop_bridge_failure(self):
        with patch.object(aiwork, "bsk", return_value={
            "pid": 123, "sessions": [], "browsers": [{"browser_name":"chrome","instance_id":"id","unresponsive":False}]
        }), patch.object(aiwork, "desk", return_value={
            "ok": True, "broker": {"ok": False, "error": "broker missing"}
        }):
            out = aiwork.status()
        self.assertFalse(out["healthy"])
        self.assertFalse(out["desktop_ready"])
        self.assertTrue(out["browser"]["ready"])


if __name__ == "__main__":
    unittest.main()
