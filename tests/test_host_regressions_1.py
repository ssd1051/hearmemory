"""Regression tests for host-adapter hardening (set 1).
Each test class names the item it pins down; all run offline (fake payloads / fake rollouts,
HOME sandbox for git), no agent and no network."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from test_host_common import FakeStore, make_project  # noqa: E402

import hearmemory.interfaces as I  # noqa: E402
import hearmemory.host.codex as codex  # noqa: E402
from hearmemory.host import _deps, cursor, hooks, install as host_install  # noqa: E402


def _cfg(**precommit):
    cfg = json.loads(json.dumps(I.DEFAULT_CONFIG))
    cfg["precommit"].update(precommit)
    return cfg


class _Tmp(unittest.TestCase):
    git = False

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.root = make_project(self.base, git=self.git)

    def tearDown(self):
        self._tmp.cleanup()


# ----------------------------------------------------------------------------------------- set 1.1
class M1GlobalHooksPathIsNeverWritten(unittest.TestCase):
    """A user-level core.hooksPath (global git config, e.g. ~/.githooks) must not receive the
    dispatcher, and the user's global hook must not be renamed, unless --force-hooks-path."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.home = self.base / "home"
        self.global_hooks = self.home / ".githooks"
        self.global_hooks.mkdir(parents=True)
        self.user_hook = self.global_hooks / "pre-commit"
        self.user_hook.write_text("#!/bin/sh\nexit 0  # user global hook\n", encoding="utf-8")
        self.user_hook.chmod(0o755)
        (self.home / ".gitconfig").write_text(
            f"[core]\n\thooksPath = {self.global_hooks}\n[user]\n\temail = t@example.com\n\tname = t\n",
            encoding="utf-8")
        self._env = mock.patch.dict(os.environ, {"HOME": str(self.home),
                                                 "XDG_CONFIG_HOME": str(self.home / ".config"),
                                                 "GIT_CONFIG_NOSYSTEM": "1"})
        self._env.start()
        self.root = make_project(self.base, git=True)

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _snapshot(self):
        return sorted((p.name, p.read_bytes()) for p in self.global_hooks.iterdir())

    def test_global_hooks_path_left_alone_by_default(self):
        before = self._snapshot()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            manifest = host_install.install(self.root, ["git"], dict(I.DEFAULT_CONFIG), python=sys.executable)
        self.assertEqual(self._snapshot(), before, "hearmemory wrote into the user-level hooks dir")
        self.assertFalse((self.global_hooks / "pre-commit.hearmemory-orig").exists())
        self.assertEqual([r for r in manifest.records if r.host == "git"], [])
        self.assertIn("--force-hooks-path", err.getvalue())
        host_install.uninstall(self.root)
        self.assertEqual(self._snapshot(), before)

    def test_force_hooks_path_opt_in_still_works(self):
        with contextlib.redirect_stderr(io.StringIO()):
            manifest = host_install.install(self.root, ["git"], dict(I.DEFAULT_CONFIG), python=sys.executable,
                                            force_hooks_path=True)
        self.assertTrue((self.global_hooks / "pre-commit.hearmemory-orig").exists())
        self.assertTrue(any(r.action == "chained" for r in manifest.records))
        host_install.uninstall(self.root)
        self.assertEqual(self.user_hook.read_text(encoding="utf-8"), "#!/bin/sh\nexit 0  # user global hook\n")
        self.assertFalse((self.global_hooks / "pre-commit.hearmemory-orig").exists())

    def test_tracked_hooks_path_inside_worktree_also_skipped(self):
        subprocess.run(["git", "config", "core.hooksPath", ".husky"], cwd=str(self.root), check=True)
        with contextlib.redirect_stderr(io.StringIO()):
            manifest = host_install.install(self.root, ["git"], dict(I.DEFAULT_CONFIG), python=sys.executable)
        self.assertEqual([r for r in manifest.records if r.host == "git"], [])
        self.assertFalse((self.root / ".husky").exists())

    def test_hooks_path_inside_repo_git_dir_is_installed(self):
        subprocess.run(["git", "config", "core.hooksPath", ".git/hooks"], cwd=str(self.root), check=True)
        manifest = host_install.install(self.root, ["git"], dict(I.DEFAULT_CONFIG), python=sys.executable)
        self.assertTrue((self.root / ".git" / "hooks" / "pre-commit").exists())
        self.assertTrue(any(r.host == "git" and r.shared for r in manifest.records))
        self.assertEqual(sorted(p.name for p in self.global_hooks.iterdir()), ["pre-commit"])


# ------------------------------------------------------------------------------------ set 1.2
def _rollout_lines(cwd, *, n_cmds=0, secret=False, flat_patch=False, sid="sess-r"):
    L = [{"type": "session_meta", "timestamp": "2026-09-24T11:00:00.000000Z",
          "payload": {"id": sid, "cwd": cwd, "source": "cli"}},
         {"type": "turn_context", "timestamp": "2026-09-24T11:00:01.000000Z",
          "payload": {"cwd": cwd, "model": "gpt-test"}}]
    for i in range(n_cmds):
        L.append({"type": "response_item", "timestamp": "2026-09-24T11:00:02.000000Z",
                  "payload": {"type": "function_call", "name": "exec_command", "call_id": f"c{i}",
                              "arguments": json.dumps({"cmd": f"echo step {i}"})}})
        L.append({"type": "response_item", "timestamp": "2026-09-24T11:00:03.000000Z",
                  "payload": {"type": "function_call_output", "call_id": f"c{i}",
                              "output": f"Exit code: 0\nstep {i}\n"}})
    if secret:
        L.append({"type": "response_item", "timestamp": "2026-09-24T11:01:00.000000Z",
                  "payload": {"type": "function_call", "name": "exec_command", "call_id": "cenv",
                              "arguments": json.dumps({"cmd": "cat .env"})}})
        L.append({"type": "response_item", "timestamp": "2026-09-24T11:01:01.000000Z",
                  "payload": {"type": "function_call_output", "call_id": "cenv",
                              "output": "Exit code: 0\nSTRIPE_SECRET=sk_live_0123456789abcdefABCDEF\n"}})
        patch = ("*** Begin Patch\n*** Add File: secrets/prod.pem\n+-----BEGIN PRIVATE KEY-----\n"
                 "+MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7\n*** End Patch\n")
        L.append({"type": "response_item", "timestamp": "2026-09-24T11:01:02.000000Z",
                  "payload": {"type": "custom_tool_call", "status": "completed", "call_id": "cp1",
                              "name": "apply_patch", "input": patch}})
        L.append({"type": "response_item", "timestamp": "2026-09-24T11:01:03.000000Z",
                  "payload": {"type": "custom_tool_call_output", "call_id": "cp1",
                              "output": json.dumps({"output": "Success.", "metadata": {"exit_code": 0}})}})
        L.append({"type": "response_item", "timestamp": "2026-09-24T11:01:04.000000Z",
                  "payload": {"type": "function_call", "name": "exec_command", "call_id": "cpriv",
                              "arguments": json.dumps({"cmd": "cat private/notes.txt"})}})
        L.append({"type": "response_item", "timestamp": "2026-09-24T11:01:05.000000Z",
                  "payload": {"type": "function_call_output", "call_id": "cpriv",
                              "output": "Exit code: 0\nmy private notes\n"}})
    patch2 = "*** Begin Patch\n*** Update File: src/app.py\n@@\n-a = 1\n+a = 2\n*** End Patch\n"
    call = {"type": "custom_tool_call", "status": "completed", "call_id": "cp2", "name": "apply_patch",
            "input": patch2}
    outp = {"type": "custom_tool_call_output", "call_id": "cp2",
            "output": json.dumps({"output": "Success.", "metadata": {"exit_code": 0}})}
    if flat_patch:  # legacy/flat shape, still tolerated
        L.append({"type": "custom_tool_call", "timestamp": "2026-09-24T11:02:00.000000Z", "payload": call})
        L.append({"type": "custom_tool_call_output", "timestamp": "2026-09-24T11:02:01.000000Z", "payload": outp})
    else:  # real Codex shape: nested under response_item
        L.append({"type": "response_item", "timestamp": "2026-09-24T11:02:00.000000Z", "payload": call})
        L.append({"type": "response_item", "timestamp": "2026-09-24T11:02:01.000000Z", "payload": outp})
    return L


def _write_rollout(home: Path, lines, name="rollout-2026-09-24T11-00-00-x.jsonl") -> Path:
    d = home / "sessions" / "2026" / "09" / "24"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    return p


class M2CodexImportAppliesPrivacyConfig(_Tmp):
    def test_env_dump_secret_patch_and_user_globs_are_excluded(self):
        home = self.base / "codex_home"
        _write_rollout(home, _rollout_lines(str(self.root), secret=True))
        cfg = _cfg()
        cfg["privacy"]["exclude_globs"] = list(cfg["privacy"]["exclude_globs"]) + ["private/**"]
        store = FakeStore(self.root)
        codex.import_rollouts(store, cfg, codex_home=str(home))
        by_cmd = {(o.tool.command if o.tool else None): o for o in store._obs if o.kind == "command"}
        env_obs = by_cmd["cat .env"]
        self.assertTrue(env_obs.excluded)
        self.assertNotIn("sk_live_0123456789abcdefABCDEF", env_obs.text)
        self.assertTrue(by_cmd["cat private/notes.txt"].excluded, "user's own exclude glob ignored")
        self.assertNotIn("my private notes", by_cmd["cat private/notes.txt"].text)
        pem = [o for o in store._obs if o.kind == "file_edit" and o.tool.paths == ["secrets/prod.pem"]]
        self.assertEqual(len(pem), 1)
        self.assertTrue(pem[0].excluded)
        self.assertNotIn("MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7", pem[0].text)
        blob = json.dumps([o.to_dict() for o in store._obs])
        self.assertNotIn("sk_live_0123456789abcdefABCDEF", blob)


class M3NestedApplyPatchIsImported(_Tmp):
    def _import(self, **kw):
        home = self.base / "codex_home"
        _write_rollout(home, _rollout_lines(str(self.root), **kw))
        store = FakeStore(self.root)
        codex.import_rollouts(store, _cfg(), codex_home=str(home))
        return [o for o in store._obs if o.kind == "file_edit"]

    def test_real_nested_response_item_shape(self):
        edits = self._import()
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0].tool.name, "apply_patch")
        self.assertEqual(edits[0].tool.paths, ["src/app.py"])
        self.assertIn("+a = 2", edits[0].text)
        self.assertNotIn("End Patch", edits[0].text)

    def test_legacy_flat_shape_still_tolerated(self):
        self.assertEqual(len(self._import(flat_patch=True)), 1)

    def test_absolute_patch_path_inside_project_is_made_relative(self):
        home = self.base / "codex_home"
        lines = _rollout_lines(str(self.root))
        for rec in lines:
            if rec["payload"].get("name") == "apply_patch":
                rec["payload"]["input"] = rec["payload"]["input"].replace(
                    "Update File: src/app.py", f"Update File: {self.root}/src/app.py")
        _write_rollout(home, lines)
        store = FakeStore(self.root)
        codex.import_rollouts(store, _cfg(), codex_home=str(home))
        self.assertEqual([o.tool.paths for o in store._obs if o.kind == "file_edit"], [["src/app.py"]])


class M4BoundedIncrementalImport(_Tmp):
    def setUp(self):
        super().setUp()
        self.home = self.base / "codex_home"

    def _cursor(self, store, path):
        return (store.read_state("cursors") or {})["import"]["codex"][str(path)]

    def test_deadline_bounds_each_call_and_resumes_without_loss(self):
        n = 400
        path = _write_rollout(self.home, _rollout_lines(str(self.root), n_cmds=n))
        store = FakeStore(self.root)
        t0 = time.monotonic()
        codex.import_rollouts(store, _cfg(), codex_home=str(self.home), deadline_s=0.05)
        self.assertLess(time.monotonic() - t0, 0.6)
        size = path.stat().st_size
        first = self._cursor(store, path)["offset"]
        self.assertLess(first, size, "a 50 ms budget should not finish 800+ lines")
        for _ in range(400):
            if self._cursor(store, path)["offset"] >= size:
                break
            codex.import_rollouts(store, _cfg(), codex_home=str(self.home), deadline_s=0.05)
        self.assertEqual(self._cursor(store, path)["offset"], size)
        cmds = [o for o in store._obs if o.kind == "command"]
        self.assertEqual(len({o.id for o in cmds}), n)
        self.assertEqual(len([o for o in store._obs if o.kind == "file_edit"]), 1)

    def test_reads_from_saved_offset_not_whole_file(self):
        path = _write_rollout(self.home, _rollout_lines(str(self.root), n_cmds=3))
        store = FakeStore(self.root)
        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("whole-file read")):
            codex.import_rollouts(store, _cfg(), codex_home=str(self.home))
            with path.open("a", encoding="utf-8") as fh:
                for rec in _rollout_lines(str(self.root), n_cmds=0)[2:]:
                    fh.write(json.dumps(rec) + "\n")
            calls = []
            real = codex._handle_rollout_line
            with mock.patch.object(codex, "_handle_rollout_line",
                                   side_effect=lambda *a, **k: calls.append(1) or real(*a, **k)):
                codex.import_rollouts(store, _cfg(), codex_home=str(self.home))
        self.assertEqual(len(calls), 2, "only the 2 appended lines should be parsed on re-import")

    def test_other_project_rollout_skipped_after_session_meta(self):
        other = self.base / "elsewhere"
        other.mkdir()
        path = _write_rollout(self.home, _rollout_lines(str(other), n_cmds=300))
        store = FakeStore(self.root)
        calls = []
        real = codex._handle_rollout_line
        with mock.patch.object(codex, "_handle_rollout_line",
                               side_effect=lambda *a, **k: calls.append(1) or real(*a, **k)):
            codex.import_rollouts(store, _cfg(), codex_home=str(self.home))
        self.assertEqual(len(calls), 1, "only session_meta should be parsed for a foreign session")
        self.assertEqual(self._cursor(store, path)["offset"], path.stat().st_size)
        self.assertEqual(store._obs, [])

    def test_cursor_saved_after_each_file(self):
        p1 = _write_rollout(self.home, _rollout_lines(str(self.root), n_cmds=2, sid="s1"), name="rollout-a.jsonl")
        _write_rollout(self.home, _rollout_lines(str(self.root), n_cmds=2, sid="s2"), name="rollout-b.jsonl")
        store = FakeStore(self.root)
        real = codex._import_one_file
        seen = []

        def flaky(*a, **k):
            seen.append(1)
            if len(seen) == 2:
                raise KeyboardInterrupt("hook killed mid-import")
            return real(*a, **k)
        with mock.patch.object(codex, "_import_one_file", side_effect=flaky):
            with self.assertRaises(KeyboardInterrupt):
                codex.import_rollouts(store, _cfg(), codex_home=str(self.home))
        self.assertEqual(self._cursor(store, p1)["offset"], p1.stat().st_size)

    def _slow_import_hook(self, host, event, payload):
        import hearmemory.host.codex as codex_mod
        got = {}

        def slow_import(store, cfg, since=None, session=None, *, deadline_s=None, **k):
            got["deadline_s"] = deadline_s
            time.sleep(3.0)
            return 0
        old = (_deps.open_store, _deps.load_memory, _deps.build_brief, _deps.spawn_worker,
               codex_mod.import_rollouts)
        _deps.open_store = lambda root: FakeStore(self.root)
        _deps.load_memory = lambda *a, **k: object()
        _deps.build_brief = lambda state, store, req, cfg: I.Brief(text="brief after bounded import")
        _deps.spawn_worker = lambda root, launched_by=None: True
        codex_mod.import_rollouts = slow_import
        try:
            t0 = time.monotonic()
            result = hooks.run_hook(host, event, json.dumps(payload).encode(), root=str(self.root))
            return result, time.monotonic() - t0, got
        finally:
            (_deps.open_store, _deps.load_memory, _deps.build_brief, _deps.spawn_worker,
             codex_mod.import_rollouts) = old

    def test_session_start_hook_respects_import_slice(self):
        for host in ("claude", "codex"):
            result, elapsed, got = self._slow_import_hook(host, "SessionStart",
                                                          {"session_id": "s1", "cwd": str(self.root)})
            self.assertLess(elapsed, 1.5, f"{host} SessionStart waited {elapsed:.2f}s on the import")
            self.assertIn("brief after bounded import", result.stdout)
            self.assertIsNotNone(got.get("deadline_s"))
            self.assertLessEqual(got["deadline_s"], 0.7 + 1e-6)

    def test_codex_stop_hook_respects_import_slice(self):
        result, elapsed, got = self._slow_import_hook("codex", "Stop", {"session_id": "s1", "cwd": str(self.root)})
        self.assertEqual(result.exit_code, 0)
        self.assertLess(elapsed, 1.5)
        self.assertLessEqual(got["deadline_s"], 0.9 + 1e-6)


# ----------------------------------------------------------------------------------------- set 1.5
class M5WarnNeverDenies(_Tmp):
    def _patch(self, decision, text="open issue: flaky test (non-blocking)"):
        old = (_deps.open_store, _deps.load_memory, _deps.mem_check, _deps.load_config)
        _deps.open_store = lambda root: FakeStore(self.root)
        _deps.load_memory = lambda *a, **k: object()
        _deps.mem_check = lambda *a, **k: I.CheckResult(decision=decision, text=text, mode="x")
        return old

    def _restore(self, old):
        _deps.open_store, _deps.load_memory, _deps.mem_check, _deps.load_config = old

    def _claude(self, mode, decision):
        old = self._patch(decision)
        cfg = _cfg(claude_mode=mode)
        _deps.load_config = lambda root: cfg
        try:
            payload = {"session_id": "s1", "cwd": str(self.root), "tool_name": "Bash",
                       "tool_input": {"command": "git commit -m x"}}
            return hooks.run_hook("claude", "PreToolUse", json.dumps(payload).encode(), root=str(self.root))
        finally:
            self._restore(old)

    def _cursor(self, mode, decision):
        old = self._patch(decision)
        try:
            payload = {"conversation_id": "c1", "command": "git commit -m x", "_root": self.root,
                       "_cfg": _cfg(cursor_mode=mode)}
            return cursor.handle_hook("beforeShellExecution", payload)
        finally:
            self._restore(old)

    def test_claude_warn_decision_in_hold_once_and_block_modes_is_not_denied(self):
        for mode in ("hold_once", "block"):
            for _ in range(3):
                data = json.loads(self._claude(mode, "warn").stdout)
                self.assertNotIn("permissionDecision", data["hookSpecificOutput"], mode)
                self.assertIn("non-blocking", data["hookSpecificOutput"]["additionalContext"])

    def test_claude_hold_and_block_decisions_still_deny(self):
        self.assertEqual(json.loads(self._claude("hold_once", "hold").stdout)
                         ["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(json.loads(self._claude("block", "block").stdout)
                         ["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_cursor_warn_decision_is_not_denied(self):
        for mode in ("hold_once", "block"):
            self.assertEqual(json.loads(self._cursor(mode, "warn").stdout), {}, mode)
        self.assertEqual(json.loads(self._cursor("hold_once", "hold").stdout)["permission"], "deny")
        self.assertEqual(json.loads(self._cursor("block", "block").stdout)["permission"], "deny")


# ------------------------------------------------------------------------------------- set 1.6
class M6SubagentStartBrief(_Tmp):
    def test_subagent_start_pushes_300_token_brief(self):
        reqs = []
        old = (_deps.open_store, _deps.load_memory, _deps.build_brief, _deps.spawn_worker)
        _deps.open_store = lambda root: FakeStore(self.root)
        _deps.load_memory = lambda *a, **k: object()
        _deps.build_brief = lambda state, store, req, cfg: reqs.append(req) or I.Brief(text="shared memory: X is fixed")
        _deps.spawn_worker = lambda root, launched_by=None: True
        try:
            payload = {"session_id": "s1", "agent_id": "a-7", "agent_type": "Explore", "cwd": str(self.root)}
            result = hooks.run_hook("claude", "SubagentStart", json.dumps(payload).encode(), root=str(self.root))
        finally:
            _deps.open_store, _deps.load_memory, _deps.build_brief, _deps.spawn_worker = old
        data = json.loads(result.stdout)
        self.assertEqual(data["hookSpecificOutput"]["hookEventName"], "SubagentStart")
        self.assertEqual(data["hookSpecificOutput"]["additionalContext"], "shared memory: X is fixed")
        self.assertEqual(len(reqs), 1)
        self.assertEqual(reqs[0].purpose, "subagent_start")
        self.assertEqual(reqs[0].max_tokens, 300)
        self.assertEqual(reqs[0].context.subagent_id, "a-7")


class M7CursorSessionStartSnakeCase(_Tmp):
    def test_cursor_session_start_uses_additional_context(self):
        old = (_deps.open_store, _deps.load_memory, _deps.build_brief, _deps.spawn_worker)
        _deps.open_store = lambda root: FakeStore(self.root)
        _deps.load_memory = lambda *a, **k: object()
        _deps.build_brief = lambda state, store, req, cfg: I.Brief(text="brief for cursor")
        _deps.spawn_worker = lambda root, launched_by=None: True
        try:
            result = hooks.run_hook("cursor", "sessionStart", json.dumps({"conversation_id": "c1"}).encode(),
                                    root=str(self.root))
        finally:
            _deps.open_store, _deps.load_memory, _deps.build_brief, _deps.spawn_worker = old
        self.assertEqual(json.loads(result.stdout), {"additional_context": "brief for cursor"})


# ------------------------------------------------------------------------------------- set 1.8
JSONC = '{\n  // my servers\n  "mcpServers": {\n    "mine": {"command": "mine-server"},\n  },\n}\n'


class M8UnparsableUserJsonIsNeverOverwritten(_Tmp):
    def _check_untouched(self, path: Path, hosts, **kw):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(JSONC, encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            manifest = host_install.install(self.root, hosts, dict(I.DEFAULT_CONFIG), python=sys.executable, **kw)
        self.assertEqual(path.read_text(encoding="utf-8"), JSONC)
        rel = path.relative_to(self.root).as_posix()
        self.assertIn(rel, err.getvalue())
        self.assertFalse(any(r.path == rel for r in manifest.records))
        with contextlib.redirect_stderr(io.StringIO()):
            host_install.uninstall(self.root)
        self.assertEqual(path.read_text(encoding="utf-8"), JSONC)

    def test_cursor_mcp_json_with_comment_and_trailing_comma(self):
        self._check_untouched(self.root / ".cursor" / "mcp.json", ["cursor"])
        self.assertFalse((self.root / ".cursor" / "hooks.json").exists())  # the parseable one was undone

    def test_cursor_hooks_json(self):
        self._check_untouched(self.root / ".cursor" / "hooks.json", ["cursor"])

    def test_claude_persist_files(self):
        self._check_untouched(self.root / ".mcp.json", ["claude"], claude_persist=True)
        self._check_untouched(self.root / ".claude" / "settings.local.json", ["claude"], claude_persist=True)

    def test_uninstall_leaves_file_that_became_unparsable(self):
        host_install.install(self.root, ["cursor"], dict(I.DEFAULT_CONFIG), python=sys.executable)
        mcp = self.root / ".cursor" / "mcp.json"
        broken = mcp.read_text(encoding="utf-8").rstrip()[:-1] + ",}\n"  # user adds a trailing comma
        mcp.write_text(broken, encoding="utf-8")
        host_install.uninstall(self.root)
        self.assertEqual(mcp.read_text(encoding="utf-8"), broken)


class M9UninstallLeavesNoTrace(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()
        self._env = mock.patch.dict(os.environ, {"HOME": str(self.home), "GIT_CONFIG_NOSYSTEM": "1",
                                                 "XDG_CONFIG_HOME": str(self.home / ".config")})
        self._env.start()
        self.root = make_project(Path(self._tmp.name), git=True)

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _tree(self):
        return sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*")
                      if ".git" not in p.relative_to(self.root).parts)

    def test_all_hosts_with_persist_then_purge(self):
        before = [p for p in self._tree() if not p.startswith(".hearmemory")]
        exclude = (self.root / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        host_install.install(self.root, list(I.INSTALLABLE_HOSTS), dict(I.DEFAULT_CONFIG),
                             python=sys.executable, claude_persist=True)
        for p in (".mcp.json", ".claude/settings.local.json", ".cursor/mcp.json", ".cursor/hooks.json"):
            self.assertTrue((self.root / p).exists(), p)
        # a second init (idempotent re-install) must not lose the bookkeeping either
        host_install.install(self.root, list(I.INSTALLABLE_HOSTS), dict(I.DEFAULT_CONFIG),
                             python=sys.executable, claude_persist=True)
        host_install.uninstall(self.root, purge=True)
        self.assertEqual(self._tree(), before)
        self.assertFalse((self.root / ".git" / "hooks" / "pre-commit").exists())
        self.assertEqual((self.root / ".git" / "info" / "exclude").read_text(encoding="utf-8"), exclude)

    def test_preexisting_user_dirs_and_keys_are_kept(self):
        (self.root / ".claude").mkdir()
        (self.root / ".claude" / "commands.md").write_text("mine\n", encoding="utf-8")
        (self.root / ".mcp.json").write_text(json.dumps({"mcpServers": {"mine": {"command": "m"}}}),
                                            encoding="utf-8")
        host_install.install(self.root, ["claude"], dict(I.DEFAULT_CONFIG), python=sys.executable,
                             claude_persist=True)
        host_install.uninstall(self.root, purge=True)
        self.assertEqual(json.loads((self.root / ".mcp.json").read_text()), {"mcpServers": {"mine": {"command": "m"}}})
        self.assertTrue((self.root / ".claude" / "commands.md").exists())
        self.assertFalse((self.root / ".claude" / "settings.local.json").exists())


# ----------------------------------------------------------------------------------------- M10
class M10HostSubcommand(unittest.TestCase):
    def test_parser_dest_matches_handler(self):
        from hearmemory.cli import build_parser
        args = build_parser().parse_args(["host", "codex-cmd"])
        self.assertEqual(args.host_action, "codex-cmd")

    def test_cli_end_to_end(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t) / "proj"
            root.mkdir()
            env = dict(os.environ, PYTHONPATH=str(REPO / "src"), HOME=str(Path(t) / "home"))
            run = lambda *a: subprocess.run([sys.executable, "-m", "hearmemory", "--project", str(root), *a],
                                            capture_output=True, text=True, env=env, timeout=60)
            self.assertEqual(run("init", "--hosts", "claude,codex,cursor").returncode, 0)
            for action, needle in (("codex-cmd", "codex"), ("claude-cmd", "claude"), ("cursor-status", "")):
                r = run("host", action)
                self.assertEqual(r.returncode, 0, f"{action}: {r.stderr}")
                self.assertNotIn("unknown host action", r.stderr)
                self.assertIn(needle, r.stdout)
            run("uninstall", "--purge", "--yes")


if __name__ == "__main__":
    unittest.main()
