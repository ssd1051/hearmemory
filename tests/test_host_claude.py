"""Claude Code adapter tests. Stdlib unittest; runs under pytest too."""
import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from test_host_common import FakeStore, load_fixture_json, make_project  # noqa: E402

import hearmemory.interfaces as I  # noqa: E402
from hearmemory.host import claude, hooks, snippets as S  # noqa: E402


def _norm(fixture, root):
    payload = load_fixture_json(fixture)
    payload["_root"] = root
    payload["_cfg"] = dict(I.DEFAULT_CONFIG)
    return claude.normalize(payload["hook_event_name"], payload)


class NormalizeFixtures(unittest.TestCase):
    """The PostToolUse/PostToolUseFailure fixtures."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_post_bash_ok(self):
        items = _norm("claude_post_bash_ok.json", self.root)
        self.assertEqual(len(items), 1)
        obs = items[0]
        self.assertIsInstance(obs, I.Observation)
        self.assertEqual(obs.kind, "command")
        self.assertEqual(obs.tool.status, "ok")
        self.assertEqual(obs.tool.exit_code, 0)
        self.assertIsNotNone(obs.tool.test)
        self.assertEqual(obs.tool.test.passed, 3)
        self.assertEqual(obs.event_key, "claude:s1:tu-1")

    def test_post_bash_fail_pytest(self):
        items = _norm("claude_post_bash_fail_pytest.json", self.root)
        self.assertEqual(len(items), 1)
        obs = items[0]
        self.assertEqual(obs.kind, "command")
        self.assertEqual(obs.tool.status, "error")
        self.assertEqual(obs.tool.exit_code, 1)
        self.assertIsNotNone(obs.tool.test)
        self.assertEqual(obs.tool.test.failed, 1)
        self.assertIn("tests/test_sync.py::test_x", obs.tool.test.failed_ids)
        # PostToolUse and PostToolUseFailure share ONE event_key space.
        self.assertEqual(obs.event_key, "claude:s1:tu-2")

    def test_post_bash_fail_interrupt(self):
        items = _norm("claude_post_bash_fail_interrupt.json", self.root)
        self.assertEqual(len(items), 1)
        obs = items[0]
        self.assertEqual(obs.tool.exit_code, None)
        self.assertEqual(obs.tool.status, "error")

    def test_post_edit_fail_builds_no_observation(self):
        items = _norm("claude_post_edit_fail.json", self.root)
        self.assertEqual(items, [])

    def test_post_bash_hearmemory_record_links_and_builds_no_command_obs(self):
        items = _norm("claude_post_bash_hearmemory_record.json", self.root)
        self.assertEqual(len(items), 1)
        ev = items[0]
        self.assertIsInstance(ev, I.ControlEvent)
        self.assertEqual(ev.kind, "provenance_link")
        self.assertEqual(ev.target, "o-1234567890abcdef")
        self.assertFalse(any(isinstance(x, I.Observation) for x in items))

    def test_post_mcp_hearmemory_record_links_and_builds_no_observation(self):
        items = _norm("claude_post_mcp_hearmemory_record.json", self.root)
        self.assertEqual(len(items), 1)
        ev = items[0]
        self.assertIsInstance(ev, I.ControlEvent)
        self.assertEqual(ev.kind, "provenance_link")
        self.assertEqual(ev.target, "o-abcdef0123456789")
        self.assertEqual(ev.provenance.subagent_id, "agent-1")


class NormalizeOtherEvents(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _payload(self, **kw):
        base = {"session_id": "s1", "cwd": ".", "_root": self.root, "_cfg": dict(I.DEFAULT_CONFIG)}
        base.update(kw)
        return base

    def test_edit_produces_diff(self):
        p = self._payload(tool_name="Edit",
                          tool_input={"file_path": "a.py", "old_string": "x = 1", "new_string": "x = 2"},
                          tool_response={}, tool_use_id="e1")
        items = claude.normalize("PostToolUse", p)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, "file_edit")
        self.assertIn("x = 2", items[0].text)
        self.assertEqual(items[0].tool.paths, ["a.py"])

    def test_read_default_does_not_store_content(self):
        p = self._payload(tool_name="Read", tool_input={"file_path": "a.py"},
                          tool_response="print('secret')", tool_use_id="r1")
        items = claude.normalize("PostToolUse", p)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, "file_read")
        self.assertEqual(items[0].text, "")

    def test_task_subagent_result(self):
        # the real Task/Agent tool_response is an object (status, prompt, content[], usage...);
        # the fixture now has that shape (it used to be a plain string).
        items = _norm("claude_post_task_object.json", self.root)
        self.assertEqual(items[0].kind, "subagent_result")
        self.assertEqual(items[0].meta["subagent_type"], "general-purpose")
        self.assertTrue(items[0].text.startswith("Root cause: app/cache.py sets TTL=0"))
        self.assertNotIn("Hypothesis", items[0].text)

    def test_task_subagent_result_plain_string_still_accepted(self):
        p = self._payload(tool_name="Task", tool_input={"subagent_type": "explorer", "prompt": "look around"},
                          tool_response="found nothing interesting", tool_use_id="t1")
        items = claude.normalize("PostToolUse", p)
        self.assertEqual(items[0].kind, "subagent_result")
        self.assertEqual(items[0].text, "found nothing interesting")

    def test_other_mcp_tool_ignored(self):
        p = self._payload(tool_name="mcp__hearmemory__hearmemory_recall", tool_input={}, tool_response={}, tool_use_id="m1")
        self.assertEqual(claude.normalize("PostToolUse", p), [])

    def test_unknown_tool_ignored_by_default(self):
        p = self._payload(tool_name="SomeFutureTool", tool_input={}, tool_response="x", tool_use_id="u1")
        self.assertEqual(claude.normalize("PostToolUse", p), [])

    def test_unknown_tool_recorded_when_configured(self):
        cfg = dict(I.DEFAULT_CONFIG)
        cfg["capture"] = dict(cfg["capture"], unknown_tools=True)
        p = self._payload(tool_name="SomeFutureTool", tool_input={}, tool_response="x", tool_use_id="u2", _cfg=cfg)
        items = claude.normalize("PostToolUse", p)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, "search")


class SettingsJson(unittest.TestCase):
    def test_post_tool_use_and_failure_share_one_matcher(self):
        settings = S.claude_settings_json("/usr/bin/python3", "/proj")
        pu = settings["hooks"]["PostToolUse"][0]
        puf = settings["hooks"]["PostToolUseFailure"][0]
        self.assertEqual(pu["matcher"], puf["matcher"])
        self.assertEqual(pu["matcher"], I.CLAUDE_TOOL_MATCHER)
        for event in I.CLAUDE_CAPTURE_EVENTS:
            self.assertIn(event, settings["hooks"])

    def test_session_start_matcher_and_command(self):
        settings = S.claude_settings_json("/usr/bin/python3", "/proj")
        entry = settings["hooks"]["SessionStart"][0]
        self.assertEqual(entry["matcher"], "startup|resume|clear|compact")
        # --project is a top-level `hearmemory` option: it must precede the `hook` subcommand.
        self.assertIn("hearmemory --project /proj hook claude SessionStart", entry["hooks"][0]["command"])

    def test_pretooluse_only_bash(self):
        settings = S.claude_settings_json("/usr/bin/python3", "/proj")
        self.assertEqual(settings["hooks"]["PreToolUse"][0]["matcher"], "Bash")


class Install(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_project(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_writes_only_inside_hearmemory_dir(self):
        records = claude.install(self.root, "/usr/bin/python3", dict(I.DEFAULT_CONFIG))
        for r in records:
            self.assertTrue(r.path.startswith(".hearmemory/host/claude/"), r.path)
        mcp = json.loads((self.root / ".hearmemory/host/claude/mcp.json").read_text())
        self.assertIn(str(self.root), mcp["mcpServers"]["hearmemory"]["args"])
        launch = (self.root / ".hearmemory/host/claude/launch.sh").read_text()
        self.assertIn("--mcp-config", launch)
        self.assertIn("--settings", launch)

    def test_persist_merges_without_clobbering_existing_content(self):
        mcp_path = self.root / ".mcp.json"
        mcp_path.write_text(json.dumps({"mcpServers": {"other": {"command": "foo"}}}), encoding="utf-8")
        records = claude.install(self.root, "/usr/bin/python3", dict(I.DEFAULT_CONFIG), persist=True)
        merged = json.loads(mcp_path.read_text())
        self.assertIn("other", merged["mcpServers"])
        self.assertIn("hearmemory", merged["mcpServers"])
        persist_records = [r for r in records if r.path == ".mcp.json"]
        self.assertEqual(len(persist_records), 1)
        self.assertEqual(persist_records[0].json_keys, [["mcpServers", "hearmemory"]])


class HookRobustness(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_project(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_hearmemory_is_silent_and_fast(self):
        import time
        other = self.root.parent / "no-hearmemory"
        other.mkdir()
        t0 = time.monotonic()
        result = hooks.run_hook("claude", "SessionStart", b"{}", root=str(other))
        elapsed_ms = (time.monotonic() - t0) * 1000
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout, "")
        self.assertLess(elapsed_ms, 50)

    def test_hearmemory_disable_env_short_circuits(self):
        import os
        os.environ["HEARMEMORY_DISABLE"] = "1"
        try:
            result = hooks.run_hook("claude", "PostToolUse", b'{"tool_name": "Bash"}', root=str(self.root))
        finally:
            del os.environ["HEARMEMORY_DISABLE"]
        self.assertEqual(result.exit_code, 0)

    def test_event_outside_project_is_ignored(self):
        sibling = self.root.parent / "sibling"
        sibling.mkdir()
        payload = json.dumps({"session_id": "s1", "cwd": str(sibling), "tool_name": "Bash",
                              "tool_input": {"command": "echo hi"}, "tool_response": {}, "tool_use_id": "x"})
        result = hooks.run_hook("claude", "PostToolUse", payload.encode(), root=str(self.root))
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.observations, [])

    def test_malformed_stdin_does_not_raise(self):
        result = hooks.run_hook("claude", "PostToolUse", b"not json{{{", root=str(self.root))
        self.assertEqual(result.exit_code, 0)

    def test_unknown_event_is_noop(self):
        result = hooks.run_hook("claude", "NotARealEvent", b"{}", root=str(self.root))
        self.assertEqual(result.exit_code, 0)

    def test_record_hook_appends_and_never_emits_allow_permission(self):
        store = FakeStore(self.root)
        claude_mod = claude
        import hearmemory.host._deps as deps
        old_open_store = deps.open_store
        deps.open_store = lambda root: store
        try:
            payload = json.dumps({"session_id": "s1", "cwd": str(self.root), "tool_name": "Bash",
                                  "tool_input": {"command": "pytest -q"},
                                  "tool_response": {"stdout": "1 passed\n", "stderr": "", "exit_code": 0},
                                  "tool_use_id": "z1"})
            result = hooks.run_hook("claude", "PostToolUse", payload.encode(), root=str(self.root))
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(len(store._obs), 1)
            self.assertNotIn("permissionDecision", result.stdout)
        finally:
            deps.open_store = old_open_store


class PreToolUseCheck(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_project(Path(self._tmp.name))
        self.store = FakeStore(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def _patched(self, decision, text, mode):
        import hearmemory.host._deps as deps
        cfg = dict(I.DEFAULT_CONFIG)
        cfg["precommit"] = dict(cfg["precommit"], claude_mode=mode)
        old = (deps.open_store, deps.load_memory, deps.mem_check)
        deps.open_store = lambda root: self.store
        deps.load_memory = lambda store, cfg, allow_rebuild=False: object()
        deps.mem_check = lambda state, store, req, cfg: I.CheckResult(decision=decision, text=text, mode=mode)
        return cfg, old

    def _restore(self, old):
        import hearmemory.host._deps as deps
        deps.open_store, deps.load_memory, deps.mem_check = old

    def _run(self, command, cfg):
        payload = json.dumps({"session_id": "s1", "cwd": str(self.root), "tool_name": "Bash",
                              "tool_input": {"command": command}})
        return hooks.run_hook("claude", "PreToolUse", payload.encode(), root=str(self.root))

    def test_non_commit_command_ignored(self):
        cfg, old = self._patched("block", "should not appear", "block")
        import hearmemory.host._deps as deps
        deps.load_config = lambda root: cfg
        try:
            result = self._run("echo hello", cfg)
            self.assertEqual(result.stdout, "")
        finally:
            self._restore(old)
            deps.load_config = None

    def test_warn_mode_never_denies(self):
        cfg, old = self._patched("block", "relies on a refuted claim", "warn")
        import hearmemory.host._deps as deps
        deps.load_config = lambda root: cfg
        try:
            result = self._run("git commit -m x", cfg)
            self.assertEqual(result.exit_code, 0)
            data = json.loads(result.stdout)
            self.assertNotIn("permissionDecision", data.get("hookSpecificOutput", {}))
            self.assertIn("relies on a refuted claim", data["hookSpecificOutput"]["additionalContext"])
        finally:
            self._restore(old)
            deps.load_config = None

    def test_block_mode_denies_without_allow(self):
        cfg, old = self._patched("block", "relies on a refuted claim", "block")
        import hearmemory.host._deps as deps
        deps.load_config = lambda root: cfg
        try:
            result = self._run("git commit -m x", cfg)
            self.assertEqual(result.exit_code, 0)  # Claude expresses this via JSON, not exit code
            data = json.loads(result.stdout)
            self.assertEqual(data["hookSpecificOutput"]["permissionDecision"], "deny")
        finally:
            self._restore(old)
            deps.load_config = None


if __name__ == "__main__":
    unittest.main()
