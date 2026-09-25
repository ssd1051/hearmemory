"""tests for `hearmemory.commands.COMMANDS`.

Every `hearmemory.memory.*` / `hearmemory.judge.*` / `hearmemory.host.*` / core dependency is
faked (see `tests/test_iface_support.py`) so this file exercises only the CLI/MCP layer's
own code: argument handling, exit codes, `--json` vs. plain-text output, and
delegation to the shared `do_*` functions that `hearmemory.mcp_server` also calls.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import unittest
from pathlib import Path
from typing import Any, Dict

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tests"))

import hearmemory.interfaces as I  # noqa: E402
from hearmemory import commands  # noqa: E402
from test_iface_support import FakeRegistry, FakeStore, InstalledFakeModules, default_config  # noqa: E402


def ns(**kw) -> argparse.Namespace:
    return argparse.Namespace(**kw)


class CommandTestCase(unittest.TestCase):
    """Base class: a fresh temp project root, a fresh fake registry, and a
    fresh set of fake cross-module modules for every test."""

    def setUp(self) -> None:
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.registry = FakeRegistry()
        self.fakes = InstalledFakeModules(self.registry)
        self.fakes.__enter__()
        self.addCleanup(self.fakes.__exit__)
        self.addCleanup(self._tmp.cleanup)

    def make_store(self) -> FakeStore:
        store = self.registry.open_store(self.root, create=True)
        assert store is not None
        return store

    def ctx(self, store, json_mode: bool = False, quiet: bool = False) -> Dict[str, Any]:
        return {"root": self.root, "store": store, "config": default_config(), "json": json_mode, "quiet": quiet}

    def run_cmd(self, name: str, args: argparse.Namespace, ctx: Dict[str, Any]):
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            code = commands.COMMANDS[name](args, ctx)
        finally:
            sys.stdout = old
        return code, buf.getvalue()


class NotInitialisedTests(CommandTestCase):
    """EXIT_NOT_INITIALISED=3 for every non-hook command when `.hearmemory` is missing."""

    def test_every_command_but_init_and_mcp_requires_store(self):
        ctx = self.ctx(store=None)
        for name in ("status", "record", "recall", "check", "issues", "import", "worker", "doctor", "rebuild"):
            with self.subTest(command=name):
                code, out = self.run_cmd(name, ns(text="x", query="", issues_action="list"), ctx)
                self.assertEqual(code, I.EXIT_NOT_INITIALISED)

    def test_not_initialised_json_shape(self):
        ctx = self.ctx(store=None, json_mode=True)
        code, out = self.run_cmd("status", ns(verbose=False), ctx)
        self.assertEqual(code, I.EXIT_NOT_INITIALISED)
        self.assertEqual(json.loads(out.strip())["error"], "not_initialised")


class InitTests(CommandTestCase):
    def test_init_creates_store_and_installs(self):
        ctx = self.ctx(store=None)
        code, out = self.run_cmd("init", ns(hosts="claude,git", no_git_hook=False, claude_persist=False,
                                             force_hooks_path=False, python=None, force=False), ctx)
        self.assertEqual(code, I.EXIT_OK)
        self.assertIsNotNone(ctx["store"])
        install_calls = sys.modules["hearmemory.host.install"].calls
        self.assertEqual(install_calls[0][0], "install")
        self.assertEqual(sorted(install_calls[0][1]), ["claude", "git"])
        self.assertIn("initialised", out)

    def test_init_no_git_hook_drops_git(self):
        ctx = self.ctx(store=None)
        self.run_cmd("init", ns(hosts="claude,git", no_git_hook=True, claude_persist=False,
                                 force_hooks_path=False, python=None, force=False), ctx)
        hosts_installed = sys.modules["hearmemory.host.install"].calls[0][1]
        self.assertNotIn("git", hosts_installed)

    def test_init_json(self):
        ctx = self.ctx(store=None, json_mode=True)
        code, out = self.run_cmd("init", ns(hosts="claude", no_git_hook=False, claude_persist=False,
                                             force_hooks_path=False, python=None, force=False), ctx)
        self.assertEqual(code, I.EXIT_OK)
        doc = json.loads(out.strip())
        self.assertIn("records", doc)


class QuietVsJsonTests(CommandTestCase):
    def test_quiet_suppresses_text_but_not_json(self):
        store = self.make_store()
        ctx_text = self.ctx(store, json_mode=False, quiet=True)
        code, out = self.run_cmd("status", ns(verbose=False), ctx_text)
        self.assertEqual(out, "")

        ctx_json = self.ctx(store, json_mode=True, quiet=True)
        code, out = self.run_cmd("status", ns(verbose=False), ctx_json)
        self.assertTrue(out.strip())
        json.loads(out.strip())


class StatusTests(CommandTestCase):
    def test_status_normal_and_json(self):
        store = self.make_store()
        ctx = self.ctx(store)
        code, out = self.run_cmd("status", ns(verbose=False), ctx)
        self.assertEqual(code, I.EXIT_OK)
        self.assertIn("hearmemory status", out)
        self.assertIn("observations: 0", out)

        ctx_json = self.ctx(store, json_mode=True)
        code, out = self.run_cmd("status", ns(verbose=False), ctx_json)
        self.assertEqual(code, I.EXIT_OK)
        doc = json.loads(out.strip())
        self.assertEqual(doc["counts"]["observations"], 0)
        self.assertIn("jev", doc)


class RecordTests(CommandTestCase):
    def test_record_echo_prefix_exact(self):
        store = self.make_store()
        ctx = self.ctx(store)
        code, out = self.run_cmd("record", ns(text="the bug is in sync.py", kind="note", paths=None, refs=None,
                                               session=None, agent_label=None, key=None), ctx)
        self.assertEqual(code, I.EXIT_OK)
        self.assertTrue(out.strip().startswith(I.RECORD_ECHO_PREFIX))
        obs_id = out.strip()[len(I.RECORD_ECHO_PREFIX):]
        self.assertRegex(obs_id, I.OBS_ID_RE)
        self.assertEqual(len(list(store.iter_observations())), 1)

    def test_record_json_shape(self):
        store = self.make_store()
        ctx = self.ctx(store, json_mode=True)
        code, out = self.run_cmd("record", ns(text="claim text here", kind="claim", paths=["a.py"], refs=None,
                                               session=None, agent_label=None, key=None), ctx)
        self.assertEqual(code, I.EXIT_OK)
        doc = json.loads(out.strip())
        self.assertRegex(doc["obs_id"], I.OBS_ID_RE)
        self.assertIsNone(doc["issue_id"])

    def test_record_issue_kind_opens_issue_event(self):
        store = self.make_store()
        ctx = self.ctx(store, json_mode=True)
        code, out = self.run_cmd("record", ns(text="sync.py silently drops rows", kind="issue", paths=None,
                                               refs=None, session=None, agent_label=None, key=None), ctx)
        self.assertEqual(code, I.EXIT_OK)
        doc = json.loads(out.strip())
        self.assertIsNotNone(doc["issue_id"])
        events = list(store.iter_events())
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "issue_open")
        self.assertEqual(events[0].target, doc["issue_id"])
        # the underlying observation is kind=note, never kind=issue
        self.assertEqual(list(store.iter_observations())[0][1].kind, "note")

    def test_record_key_makes_event_key_idempotent_shaped(self):
        store = self.make_store()
        ctx = self.ctx(store)
        self.run_cmd("record", ns(text="x", kind="note", paths=None, refs=None, session=None, agent_label=None,
                                   key="my-key-1"), ctx)
        obs = list(store.iter_observations())[0][1]
        self.assertEqual(obs.event_key, "cli:my-key-1")

    def test_record_spawns_worker(self):
        store = self.make_store()
        ctx = self.ctx(store)
        self.run_cmd("record", ns(text="x", kind="note", paths=None, refs=None, session=None, agent_label=None,
                                   key=None), ctx)
        calls = sys.modules["hearmemory.judge.worker"].calls
        self.assertTrue(any(c[0] == "spawn_background" for c in calls))


class RecallTests(CommandTestCase):
    def test_recall_normal_and_json(self):
        store = self.make_store()
        ctx = self.ctx(store)
        code, out = self.run_cmd("recall", ns(query="sync.py", brief=False, limit=8, include_archive=False,
                                               paths=None, session=None, wait=None, restore=None), ctx)
        self.assertEqual(code, I.EXIT_OK)
        self.assertIn("sync.py", out)

        ctx_json = self.ctx(store, json_mode=True)
        code, out = self.run_cmd("recall", ns(query="sync.py", brief=False, limit=8, include_archive=False,
                                               paths=None, session=None, wait=None, restore=None), ctx_json)
        doc = json.loads(out.strip())
        self.assertEqual(doc["query"], "sync.py")

    def test_recall_brief(self):
        store = self.make_store()
        ctx = self.ctx(store, json_mode=True)
        code, out = self.run_cmd("recall", ns(query="", brief=True, limit=8, include_archive=False, paths=None,
                                               session=None, wait=None, restore=None), ctx)
        self.assertEqual(code, I.EXIT_OK)
        doc = json.loads(out.strip())
        self.assertIn("memory_as_of", doc)

    def test_recall_restore_appends_event(self):
        store = self.make_store()
        ctx = self.ctx(store)
        code, out = self.run_cmd("recall", ns(query=None, brief=False, limit=8, include_archive=False, paths=None,
                                               session=None, wait=None, restore="o-deadbeefdeadbeef"), ctx)
        self.assertEqual(code, I.EXIT_OK)
        events = list(store.iter_events())
        self.assertEqual(events[0].kind, "archive_restore")
        self.assertEqual(events[0].target, "o-deadbeefdeadbeef")


class CheckTests(CommandTestCase):
    def test_check_allow_exit_ok(self):
        store = self.make_store()
        ctx = self.ctx(store)
        code, out = self.run_cmd("check", ns(staged=False, text="looks fine", message=None, paths=None, mode=None,
                                              session=None, wait=None, ack=None), ctx)
        self.assertEqual(code, I.EXIT_OK)
        self.assertIn("allow", out)

    def test_check_block_exit_blocked(self):
        store = self.make_store()
        ctx = self.ctx(store)
        code, out = self.run_cmd("check", ns(staged=False, text="TRIGGER_BLOCK this", message=None, paths=None,
                                              mode=None, session=None, wait=None, ack=None), ctx)
        self.assertEqual(code, I.EXIT_BLOCKED)

    def test_check_hold_exit_blocked(self):
        store = self.make_store()
        ctx = self.ctx(store)
        code, out = self.run_cmd("check", ns(staged=False, text="TRIGGER_HOLD this", message=None, paths=None,
                                              mode=None, session=None, wait=None, ack=None), ctx)
        self.assertEqual(code, I.EXIT_BLOCKED)

    def test_check_json(self):
        store = self.make_store()
        ctx = self.ctx(store, json_mode=True)
        code, out = self.run_cmd("check", ns(staged=False, text="fine", message=None, paths=None, mode=None,
                                              session=None, wait=None, ack=None), ctx)
        doc = json.loads(out.strip())
        self.assertEqual(doc["decision"], "allow")


class IssuesTests(CommandTestCase):
    def test_issues_list_empty(self):
        store = self.make_store()
        ctx = self.ctx(store)
        code, out = self.run_cmd("issues", ns(issues_action="list", id=None, title=None, reason=None, all=False,
                                               paths=None, session=None), ctx)
        self.assertEqual(code, I.EXIT_OK)
        self.assertIn("no open issues", out)

    def test_issues_open_close_reopen(self):
        store = self.make_store()
        ctx = self.ctx(store, json_mode=True)
        code, out = self.run_cmd("issues", ns(issues_action="open", id=None, title="rows silently dropped",
                                               reason=None, all=False, paths=["a.py"], session=None), ctx)
        self.assertEqual(code, I.EXIT_OK)
        doc = json.loads(out.strip())
        issue_id = doc["issue_id"]
        self.assertIsNotNone(issue_id)

        ctx2 = self.ctx(store)
        code, out = self.run_cmd("issues", ns(issues_action="close", id=issue_id, title=None, reason="fixed",
                                               all=False, paths=None, session=None), ctx2)
        self.assertEqual(code, I.EXIT_OK)
        code, out = self.run_cmd("issues", ns(issues_action="reopen", id=issue_id, title=None, reason=None,
                                               all=False, paths=None, session=None), ctx2)
        self.assertEqual(code, I.EXIT_OK)
        kinds = [e.kind for e in store.iter_events()]
        self.assertIn("issue_open", kinds)
        self.assertIn("issue_close", kinds)
        self.assertIn("issue_reopen", kinds)

    def test_issues_show_not_found(self):
        store = self.make_store()
        ctx = self.ctx(store)
        code, out = self.run_cmd("issues", ns(issues_action="show", id="i-doesnotexist", title=None, reason=None,
                                               all=False, paths=None, session=None), ctx)
        self.assertEqual(code, I.EXIT_USAGE)


class ImportTests(CommandTestCase):
    def test_import_codex(self):
        store = self.make_store()
        ctx = self.ctx(store, json_mode=True)
        code, out = self.run_cmd("import", ns(source="codex", since=None, session=None, codex_home=None,
                                               dry_run=False), ctx)
        self.assertEqual(code, I.EXIT_OK)
        self.assertEqual(json.loads(out.strip())["imported"], 0)

    def test_import_unknown_source(self):
        store = self.make_store()
        ctx = self.ctx(store)
        code, out = self.run_cmd("import", ns(source="bogus", since=None, session=None, codex_home=None,
                                               dry_run=False), ctx)
        self.assertEqual(code, I.EXIT_USAGE)


class WorkerTests(CommandTestCase):
    def test_worker_spawn(self):
        store = self.make_store()
        ctx = self.ctx(store, json_mode=True)
        code, out = self.run_cmd("worker", ns(once=False, daemon=False, spawn=True, stop=False, timeout=None,
                                               no_jev=False, wait_lock=None, launched_by="test", retry_failed=False,
                                               status=False), ctx)
        self.assertEqual(code, I.EXIT_OK)
        self.assertTrue(json.loads(out.strip())["spawned"])

    def test_worker_stop(self):
        store = self.make_store()
        ctx = self.ctx(store, json_mode=True)
        code, out = self.run_cmd("worker", ns(once=False, daemon=False, spawn=False, stop=True, timeout=2.0,
                                               no_jev=False, wait_lock=None, launched_by=None, retry_failed=False,
                                               status=False), ctx)
        self.assertEqual(code, I.EXIT_OK)
        self.assertFalse(json.loads(out.strip())["stopped"])
        self.assertIn(("stop_worker", 2.0), sys.modules["hearmemory.judge.worker"].calls)

    def test_worker_wait_lock_retries_until_free(self):
        store = self.make_store()
        wmod = sys.modules["hearmemory.judge.worker"]
        state = {"n": 0}
        real = wmod.run_pipeline

        def flaky(store, cfg, deadline_s, use_jev, mode):
            state["n"] += 1
            if state["n"] < 3:
                return {"skipped": "busy"}
            return real(store, cfg, deadline_s, use_jev, mode)

        wmod.run_pipeline = flaky
        ctx = self.ctx(store, json_mode=True)
        code, out = self.run_cmd("worker", ns(once=True, daemon=False, spawn=False, stop=False, timeout=1,
                                               no_jev=False, wait_lock=2, launched_by=None, retry_failed=False,
                                               status=False), ctx)
        self.assertEqual(code, I.EXIT_OK)
        self.assertEqual(state["n"], 3)
        self.assertTrue(json.loads(out.strip())["ran"])

    def test_worker_status_none(self):
        store = self.make_store()
        ctx = self.ctx(store)
        code, out = self.run_cmd("worker", ns(once=False, daemon=False, spawn=False, stop=False, timeout=None,
                                               no_jev=False, wait_lock=None, launched_by=None, retry_failed=False,
                                               status=True), ctx)
        self.assertEqual(code, I.EXIT_OK)
        self.assertIn("not running", out)


class DoctorTests(CommandTestCase):
    def test_doctor_reports_ok(self):
        store = self.make_store()
        ctx = self.ctx(store)
        code, out = self.run_cmd("doctor", ns(repair=False), ctx)
        self.assertEqual(code, I.EXIT_OK)
        self.assertIn("no problems found", out)

    def test_doctor_repair_merges_spool(self):
        store = self.make_store()
        store.spool.extend([object(), object()])
        ctx = self.ctx(store, json_mode=True)
        code, out = self.run_cmd("doctor", ns(repair=True), ctx)
        self.assertEqual(code, I.EXIT_OK)
        doc = json.loads(out.strip())
        self.assertTrue(any("merged 2" in r for r in doc["repairs"]))
        self.assertEqual(store.spool, [])


class UninstallTests(CommandTestCase):
    def test_uninstall_purge_requires_yes(self):
        # non-interactive (quiet): no `--yes` must refuse rather than block on stdin
        store = self.make_store()
        ctx = self.ctx(store, quiet=True)
        code, out = self.run_cmd("uninstall", ns(purge=True, yes=False), ctx)
        self.assertEqual(code, I.EXIT_USAGE)

    def test_uninstall_purge_with_yes(self):
        store = self.make_store()
        ctx = self.ctx(store)
        code, out = self.run_cmd("uninstall", ns(purge=True, yes=True), ctx)
        self.assertEqual(code, I.EXIT_OK)
        self.assertIsNone(ctx["store"])
        self.assertIsNone(self.registry.open_store(self.root, create=False))

    def test_uninstall_without_purge(self):
        store = self.make_store()
        ctx = self.ctx(store)
        code, out = self.run_cmd("uninstall", ns(purge=False, yes=False), ctx)
        self.assertEqual(code, I.EXIT_OK)
        self.assertIsNotNone(self.registry.open_store(self.root, create=False))


class RebuildTests(CommandTestCase):
    def test_rebuild(self):
        store = self.make_store()
        ctx = self.ctx(store, json_mode=True)
        code, out = self.run_cmd("rebuild", ns(reextract=False), ctx)
        self.assertEqual(code, I.EXIT_OK)
        doc = json.loads(out.strip())
        self.assertIn("built_ts", doc)

    def test_rebuild_reextract_runs_pipeline(self):
        store = self.make_store()
        ctx = self.ctx(store)
        self.run_cmd("rebuild", ns(reextract=True), ctx)
        calls = sys.modules["hearmemory.judge.worker"].calls
        self.assertTrue(any(c[0] == "run_pipeline" for c in calls))


class HostTests(CommandTestCase):
    def test_host_claude_cmd_missing_launch_script(self):
        store = self.make_store()
        ctx = self.ctx(store)
        code, out = self.run_cmd("host", ns(host_action="claude-cmd"), ctx)
        self.assertEqual(code, I.EXIT_USAGE)

    def test_host_claude_cmd_reads_launch_script(self):
        store = self.make_store()
        launch = self.root / ".hearmemory" / "host" / "claude"
        launch.mkdir(parents=True)
        (launch / "launch.sh").write_text("#!/bin/sh\nexec claude --mcp-config x --settings y \"$@\"\n")
        ctx = self.ctx(store)
        code, out = self.run_cmd("host", ns(host_action="claude-cmd"), ctx)
        self.assertEqual(code, I.EXIT_OK)
        # one complete pasteable command, never launch.sh's `exec` line
        self.assertTrue(out.strip().startswith("claude --mcp-config "), out)
        self.assertNotIn("exec", out)

    def test_host_cursor_status(self):
        store = self.make_store()
        ctx = self.ctx(store, json_mode=True)
        code, out = self.run_cmd("host", ns(host_action="cursor-status"), ctx)
        self.assertEqual(code, I.EXIT_OK)
        doc = json.loads(out.strip())
        self.assertFalse(doc["mcp_json"])

    def test_host_unknown_action(self):
        store = self.make_store()
        ctx = self.ctx(store)
        code, out = self.run_cmd("host", ns(host_action="bogus"), ctx)
        self.assertEqual(code, I.EXIT_USAGE)


class GuessHostTests(unittest.TestCase):
    def test_guess_claude(self):
        self.assertEqual(commands.guess_cli_host({"CLAUDECODE": "1"}), "claude")

    def test_guess_codex_by_session_prefix(self):
        self.assertEqual(commands.guess_cli_host({"HEARMEMORY_SESSION_ID": "codex-123-456"}), "codex")

    def test_guess_codex_by_sandbox_env(self):
        self.assertEqual(commands.guess_cli_host({"CODEX_SANDBOX_NETWORK_DISABLED": "1"}), "codex")

    def test_guess_cursor(self):
        self.assertEqual(commands.guess_cli_host({"CURSOR_AGENT": "1"}), "cursor")

    def test_guess_default_cli(self):
        self.assertEqual(commands.guess_cli_host({}), "cli")

    def test_session_id_prefers_explicit(self):
        self.assertEqual(commands.cli_session_id("explicit", {"HEARMEMORY_SESSION_ID": "env"}), "explicit")

    def test_session_id_falls_back_to_env(self):
        self.assertEqual(commands.cli_session_id(None, {"HEARMEMORY_SESSION_ID": "env"}), "env")

    def test_session_id_generates_uuid_prefixed(self):
        sid = commands.cli_session_id(None, {})
        self.assertTrue(sid.startswith("cli-"))


if __name__ == "__main__":
    unittest.main()
