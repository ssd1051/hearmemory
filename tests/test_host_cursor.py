"""Cursor adapter tests (opt-in)."""
import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from test_host_common import make_project  # noqa: E402

import hearmemory.interfaces as I  # noqa: E402
from hearmemory.host import cursor  # noqa: E402


class Normalize(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _payload(self, **kw):
        base = {"conversation_id": "conv-1", "generation_id": "gen-1", "hook_event_name": "x",
               "workspace_roots": [str(self.root)], "_root": self.root, "_cfg": dict(I.DEFAULT_CONFIG)}
        base.update(kw)
        return base

    def test_generation_id_never_becomes_part_of_actor_identity(self):
        p1 = self._payload(generation_id="gen-1", command="pytest", output="1 passed")
        p2 = self._payload(generation_id="gen-2", command="pytest", output="1 passed")
        obs1 = cursor.normalize("afterShellExecution", p1)[0]
        obs2 = cursor.normalize("afterShellExecution", p2)[0]
        # Same conversation -> same actor, REGARDLESS of generation_id.
        self.assertEqual(I.actor_key(obs1.provenance), I.actor_key(obs2.provenance))
        self.assertEqual(obs1.provenance.session_id, "conv-1")
        self.assertEqual(obs1.meta["generation_id"], "gen-1")
        self.assertEqual(obs2.meta["generation_id"], "gen-2")

    def test_user_email_and_similar_fields_are_discarded(self):
        p = self._payload(command="ls", output="", user_email="me@example.com")
        obs = cursor.normalize("afterShellExecution", p)[0]
        self.assertNotIn("user_email", obs.meta)
        self.assertNotIn("me@example.com", json.dumps(obs.to_dict()))

    def test_after_shell_execution_unknown_exit_code(self):
        p = self._payload(command="ls", output="a.py\n")
        obs = cursor.normalize("afterShellExecution", p)[0]
        self.assertEqual(obs.tool.status, "unknown")
        self.assertIsNone(obs.tool.exit_code)

    def test_after_file_edit_diff(self):
        p = self._payload(file_path="a.py", edits=[{"old_string": "x=1", "new_string": "x=2"}])
        obs = cursor.normalize("afterFileEdit", p)[0]
        self.assertEqual(obs.kind, "file_edit")
        self.assertIn("x=2", obs.text)
        self.assertEqual(obs.tool.paths, ["a.py"])

    def test_after_agent_response(self):
        p = self._payload(text="done, root cause was X")
        obs = cursor.normalize("afterAgentResponse", p)[0]
        self.assertEqual(obs.kind, "assistant_message")

    def test_before_shell_execution_produces_no_observation(self):
        p = self._payload(command="git commit -m x")
        self.assertEqual(cursor.normalize("beforeShellExecution", p), [])

    def test_after_mcp_execution_hearmemory_record_links_only(self):
        p = self._payload(tool_name="hearmemory_record", result_json=json.dumps({"obs_id": "o-1122334455667788"}))
        items = cursor.normalize("afterMCPExecution", p)
        self.assertEqual(len(items), 1)
        self.assertIsInstance(items[0], I.ControlEvent)
        self.assertEqual(items[0].target, "o-1122334455667788")

    def test_after_mcp_execution_other_tool_ignored(self):
        p = self._payload(tool_name="hearmemory_recall", result_json="{}")
        self.assertEqual(cursor.normalize("afterMCPExecution", p), [])


class BeforeShellExecutionCheck(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_project(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def _payload(self, command, cfg):
        return {"conversation_id": "conv-1", "command": command, "_root": self.root, "_cfg": cfg}

    def test_non_commit_returns_empty_permission_object(self):
        result = cursor.handle_hook("beforeShellExecution", self._payload("ls", dict(I.DEFAULT_CONFIG)))
        self.assertEqual(json.loads(result.stdout), {})

    def test_warn_mode_never_denies(self):
        import hearmemory.host._deps as deps
        cfg = dict(I.DEFAULT_CONFIG)
        cfg["precommit"] = dict(cfg["precommit"], cursor_mode="warn")
        old = (deps.open_store, deps.load_memory, deps.mem_check)
        deps.open_store = lambda root: object()
        deps.load_memory = lambda *a, **k: object()
        deps.mem_check = lambda *a, **k: I.CheckResult(decision="block", text="refuted claim", mode="warn")
        try:
            result = cursor.handle_hook("beforeShellExecution", self._payload("git commit -m x", cfg))
            data = json.loads(result.stdout)
            self.assertEqual(data, {})
        finally:
            deps.open_store, deps.load_memory, deps.mem_check = old

    def test_block_mode_denies_without_allow(self):
        import hearmemory.host._deps as deps
        cfg = dict(I.DEFAULT_CONFIG)
        cfg["precommit"] = dict(cfg["precommit"], cursor_mode="block")
        old = (deps.open_store, deps.load_memory, deps.mem_check)
        deps.open_store = lambda root: object()
        deps.load_memory = lambda *a, **k: object()
        deps.mem_check = lambda *a, **k: I.CheckResult(decision="block", text="refuted claim", mode="block")
        try:
            result = cursor.handle_hook("beforeShellExecution", self._payload("git commit -m x", cfg))
            data = json.loads(result.stdout)
            self.assertEqual(data["permission"], "deny")
            self.assertNotIn("allow", json.dumps(data))
        finally:
            deps.open_store, deps.load_memory, deps.mem_check = old


class Install(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_project(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_merges_into_existing_cursor_mcp_json(self):
        cursor_dir = self.root / ".cursor"
        cursor_dir.mkdir()
        (cursor_dir / "mcp.json").write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}),
                                             encoding="utf-8")
        records = cursor.install(self.root, "/usr/bin/python3", dict(I.DEFAULT_CONFIG))
        merged = json.loads((cursor_dir / "mcp.json").read_text())
        self.assertIn("other", merged["mcpServers"])
        self.assertIn("hearmemory", merged["mcpServers"])

    def test_writes_rules_file(self):
        cursor.install(self.root, "/usr/bin/python3", dict(I.DEFAULT_CONFIG))
        rules = (self.root / ".cursor" / "rules" / "hearmemory.mdc").read_text(encoding="utf-8")
        self.assertIn("alwaysApply: true", rules)


if __name__ == "__main__":
    unittest.main()
