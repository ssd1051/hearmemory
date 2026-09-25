"""Codex adapter tests: rollout import filtering/incrementality/idempotency/mapping,
and AGENTS.md install/uninstall."""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from test_host_common import FakeStore, load_fixture, make_project  # noqa: E402

import hearmemory.interfaces as I  # noqa: E402
from hearmemory.host import codex, install as host_install, snippets as S  # noqa: E402


def _write_rollout(root: Path, home: Path, cwd: str, name: str = "rollout-1.jsonl") -> Path:
    content = load_fixture("codex_rollout_template.jsonl").replace("{CWD}", cwd)
    sess_dir = home / "sessions" / "2026" / "09" / "24"
    sess_dir.mkdir(parents=True, exist_ok=True)
    path = sess_dir / name
    path.write_text(content, encoding="utf-8")
    return path


class ImportRollouts(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.root = make_project(base)
        self.home = base / "codex_home"
        self.store = FakeStore(self.root)
        self.cfg = dict(I.DEFAULT_CONFIG)

    def tearDown(self):
        self._tmp.cleanup()

    def test_filters_by_cwd_and_maps_events(self):
        _write_rollout(self.root, self.home, str(self.root))
        n = codex.import_rollouts(self.store, self.cfg, codex_home=str(self.home))
        self.assertGreater(n, 0)
        kinds = [o.kind for o in self.store._obs]
        self.assertIn("command", kinds)
        self.assertIn("file_edit", kinds)
        self.assertIn("assistant_message", kinds)
        self.assertIn("user_prompt", kinds)

        cmd_obs = [o for o in self.store._obs if o.kind == "command"][0]
        self.assertEqual(cmd_obs.tool.name, "exec_command")
        self.assertEqual(cmd_obs.tool.exit_code, 1)
        self.assertEqual(cmd_obs.tool.test.failed, 1)
        self.assertEqual(cmd_obs.provenance.host, "codex")
        self.assertEqual(cmd_obs.provenance.session_id, "sess-abc")
        self.assertEqual(cmd_obs.provenance.source, "import:codex_rollout")

        edit_obs = [o for o in self.store._obs if o.kind == "file_edit"][0]
        self.assertEqual(edit_obs.tool.paths, ["src/x.py"])

        # hearmemory MCP record + hearmemory CLI record must NOT create command/other observations, only links.
        link_targets = {e.target for e in self.store._events if e.kind == "provenance_link"}
        self.assertIn("o-1122334455667788", link_targets)
        self.assertIn("o-99aabbccddeeff00", link_targets)
        self.assertEqual(len(self.store._obs), 4)  # command, file_edit, user_prompt, ONE assistant_message

        # task_complete must be deduped against the agent_message that already fired.
        assistant_msgs = [o for o in self.store._obs if o.kind == "assistant_message"]
        self.assertEqual(len(assistant_msgs), 1)

    def test_session_outside_project_is_skipped(self):
        other = self.home.parent / "elsewhere"
        other.mkdir()
        _write_rollout(self.root, self.home, str(other))
        n = codex.import_rollouts(self.store, self.cfg, codex_home=str(self.home))
        self.assertEqual(n, 0)
        self.assertEqual(self.store._obs, [])

    def test_incremental_and_idempotent_reimport(self):
        _write_rollout(self.root, self.home, str(self.root))
        n1 = codex.import_rollouts(self.store, self.cfg, codex_home=str(self.home))
        n2 = codex.import_rollouts(self.store, self.cfg, codex_home=str(self.home))
        self.assertGreater(n1, 0)
        self.assertEqual(n2, 0)  # cursor advanced past everything; nothing new to import
        ids = [o.id for o in self.store._obs]
        self.assertEqual(len(ids), len(set(ids)))  # no duplicate ids even before store-side dedupe

    def test_max_lines_partial_batch_does_not_lose_lines(self):
        _write_rollout(self.root, self.home, str(self.root))
        # 20 single-line batches for a 13-line rollout: many lines (session_meta, turn_context,
        # function_call, ...) produce zero observations on their own, so "0 appended" is NOT a
        # valid stopping signal -- only exhausting the file's lines is. Run a fixed, generous
        # number of tiny batches instead of stopping early on n == 0.
        for _ in range(20):
            codex.import_rollouts(self.store, self.cfg, codex_home=str(self.home), max_lines=1)
        full_store = FakeStore(self.root)
        codex.import_rollouts(full_store, self.cfg, codex_home=str(self.home))
        self.assertEqual(len(self.store._obs), len(full_store._obs))
        self.assertEqual({o.id for o in self.store._obs}, {o.id for o in full_store._obs})

    def test_old_rollout_beyond_max_age_is_ignored(self):
        import os
        import time
        path = _write_rollout(self.root, self.home, str(self.root))
        old_ts = time.time() - 30 * 86400
        os.utime(path, (old_ts, old_ts))
        cfg = dict(self.cfg)
        cfg["import"] = dict(cfg["import"], codex_max_age_days=14)
        n = codex.import_rollouts(self.store, cfg, codex_home=str(self.home))
        self.assertEqual(n, 0)


class AgentsMdInstall(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_project(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_creates_agents_md_with_marker_block(self):
        records = codex.install(self.root, "/usr/bin/python3", dict(I.DEFAULT_CONFIG))
        text = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn(S.BEGIN_MARKER, text)
        self.assertIn(S.END_MARKER, text)
        rec = next(r for r in records if r.path == "AGENTS.md")
        self.assertTrue(rec.created_file)

    def test_preserves_existing_agents_md_content(self):
        (self.root / "AGENTS.md").write_text("# My project\n\nSome instructions.\n", encoding="utf-8")
        codex.install(self.root, "/usr/bin/python3", dict(I.DEFAULT_CONFIG))
        text = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("# My project", text)
        self.assertIn(S.BEGIN_MARKER, text)

    def test_reinstall_does_not_duplicate_block(self):
        codex.install(self.root, "/usr/bin/python3", dict(I.DEFAULT_CONFIG))
        codex.install(self.root, "/usr/bin/python3", dict(I.DEFAULT_CONFIG))
        text = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertEqual(text.count(S.BEGIN_MARKER), 1)

    def test_uninstall_removes_block_and_new_file(self):
        manifest = host_install.install(self.root, ["codex"], dict(I.DEFAULT_CONFIG), python="/usr/bin/python3")
        self.assertTrue((self.root / "AGENTS.md").exists())
        host_install.uninstall(self.root)
        self.assertFalse((self.root / "AGENTS.md").exists())

    def test_uninstall_keeps_user_authored_agents_md(self):
        (self.root / "AGENTS.md").write_text("# My project\n", encoding="utf-8")
        host_install.install(self.root, ["codex"], dict(I.DEFAULT_CONFIG), python="/usr/bin/python3")
        host_install.uninstall(self.root)
        text = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("# My project", text)
        self.assertNotIn(S.BEGIN_MARKER, text)


if __name__ == "__main__":
    unittest.main()
