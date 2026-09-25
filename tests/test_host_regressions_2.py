"""Regression tests for host-adapter hardening (set 2). Offline only: fake payloads, fake rollouts, sandboxed HOME."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
sys.path.insert(0, str(SRC))

from test_host_common import FakeStore, make_project  # noqa: E402

import hearmemory.interfaces as I  # noqa: E402
import hearmemory.host.codex as codex  # noqa: E402
from hearmemory import observe, privacy  # noqa: E402
from hearmemory.host import _deps, cursor, install as host_install  # noqa: E402
from hearmemory.memory.runs import RunIndex, run_record  # noqa: E402
from hearmemory.store import create_store  # noqa: E402
from hearmemory.textutil import normalize_ts, now_ts  # noqa: E402


def _cfg():
    return json.loads(json.dumps(I.DEFAULT_CONFIG))


def _snapshot(d: Path):
    return sorted((str(p.relative_to(d)), p.read_bytes() if p.is_file() else b"<dir>")
                  for p in d.rglob("*"))


class _Sandbox(unittest.TestCase):
    """A project plus an OUTSIDE directory standing in for user-level config (~/.cursor etc.),
    with HOME pointed at a throwaway dir so nothing can reach the real one."""
    git = False

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.outside = self.base / "outside"
        self.outside.mkdir()
        self._env = mock.patch.dict(os.environ, {"HOME": str(self.home),
                                                 "XDG_CONFIG_HOME": str(self.home / ".config"),
                                                 "GIT_CONFIG_NOSYSTEM": "1"})
        self._env.start()
        self.root = make_project(self.base, git=self.git)

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def cli(self, *args):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC)
        env.pop("TYPESAFE_API_KEY", None)
        return subprocess.run([sys.executable, "-m", "hearmemory", "--project", str(self.root), *args],
                              cwd=str(self.root), env=env, capture_output=True, text=True, timeout=60)


# ----------------------------------------------------------------------------------------- 2.1
class Hardening2_1SymlinkedTargetsAreNeverWrittenThrough(_Sandbox):
    def _quiet_install(self, hosts, **kw):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            m = host_install.install(self.root, hosts, _cfg(), python=sys.executable, **kw)
        return m, err.getvalue()

    def test_symlinked_agents_md_is_skipped(self):
        global_agents = self.outside / "AGENTS.md"
        global_agents.write_text("# my global agent rules\n", encoding="utf-8")
        (self.root / "AGENTS.md").symlink_to(global_agents)
        before = _snapshot(self.outside)
        manifest, err = self._quiet_install(["codex"])
        self.assertEqual(_snapshot(self.outside), before)
        self.assertIn("AGENTS.md", err)
        self.assertFalse(any(r.path == "AGENTS.md" for r in manifest.records))
        self.assertTrue((self.root / ".hearmemory" / "host" / "codex" / "launch.sh").exists())

    def test_symlinked_cursor_dir_is_skipped_without_crash(self):
        (self.root / ".cursor").symlink_to(self.outside, target_is_directory=True)
        manifest, err = self._quiet_install(["claude", "cursor"])
        self.assertEqual(list(self.outside.iterdir()), [], "wrote into the user-level .cursor")
        self.assertIn("outside", err)
        self.assertFalse(any(r.host == "cursor" for r in manifest.records))
        self.assertTrue(host_install.M.manifest_path(self.root).exists())

    def test_symlinked_claude_dir_with_persist_is_skipped(self):
        (self.root / ".claude").symlink_to(self.outside, target_is_directory=True)
        manifest, _ = self._quiet_install(["claude"], claude_persist=True)
        self.assertFalse((self.outside / "settings.local.json").exists())
        self.assertTrue((self.root / ".mcp.json").exists(), "in-project .mcp.json still merged")
        self.assertFalse(any(r.path.endswith("settings.local.json") for r in manifest.records))

    def test_dangling_symlink_to_outside_is_skipped(self):
        (self.root / ".cursor").mkdir()
        (self.root / ".cursor" / "hooks.json").symlink_to(self.outside / "hooks.json")
        self._quiet_install(["cursor"])
        self.assertFalse((self.outside / "hooks.json").exists(), "created global Cursor hooks")

    def test_symlink_that_stays_inside_the_project_is_fine(self):
        (self.root / "config" / "cursor").mkdir(parents=True)
        (self.root / ".cursor").symlink_to(self.root / "config" / "cursor", target_is_directory=True)
        manifest, _ = self._quiet_install(["cursor"])
        self.assertTrue((self.root / "config" / "cursor" / "mcp.json").exists())
        self.assertTrue(any(r.host == "cursor" for r in manifest.records))

    def test_manifest_is_written_even_when_a_host_install_crashes(self):
        def boom(root, py, cfg, *, records=None):
            raise RuntimeError("simulated crash half-way")
        with mock.patch.object(host_install._cursor, "install", side_effect=boom):
            with self.assertRaises(RuntimeError):
                self._quiet_install(["claude", "cursor"])
        m = host_install.M.read_manifest(self.root)
        self.assertIsNotNone(m, "no install_manifest.json after a crash")
        self.assertTrue(any(r.host == "claude" for r in m.records))
        notes = host_install.uninstall(self.root)
        self.assertFalse((self.root / ".hearmemory" / "host" / "claude" / "settings.json").exists(), notes)

    def test_uninstall_never_follows_a_symlink_added_after_init(self):
        self._quiet_install(["cursor"])
        import shutil
        shutil.rmtree(self.root / ".cursor")
        user_mcp = self.outside / "mcp.json"
        user_mcp.write_text(json.dumps({"mcpServers": {"hearmemory": {"command": "x"}, "mine": {}}}),
                            encoding="utf-8")
        (self.root / ".cursor").symlink_to(self.outside, target_is_directory=True)
        before = _snapshot(self.outside)
        host_install.uninstall(self.root)
        self.assertEqual(_snapshot(self.outside), before)

    def test_cli_init_and_uninstall_with_every_symlink(self):
        """The full scenario end to end: AGENTS.md, .cursor and .claude all symlinked to
        user-level locations. init exits 0, writes nothing outside, and uninstall undoes it."""
        (self.outside / "AGENTS.md").write_text("global\n", encoding="utf-8")
        (self.outside / "cursor").mkdir()
        (self.outside / "claude").mkdir()
        (self.root / "AGENTS.md").symlink_to(self.outside / "AGENTS.md")
        (self.root / ".cursor").symlink_to(self.outside / "cursor", target_is_directory=True)
        (self.root / ".claude").symlink_to(self.outside / "claude", target_is_directory=True)
        before = _snapshot(self.outside)
        r = self.cli("init", "--hosts", "claude,codex,cursor", "--claude-persist")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(_snapshot(self.outside), before)
        self.assertTrue((self.root / ".hearmemory" / "install_manifest.json").exists())
        r = self.cli("uninstall")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("nothing to undo", r.stdout + r.stderr)
        self.assertFalse((self.root / ".hearmemory" / "host" / "claude" / "settings.json").exists())
        self.assertFalse((self.root / ".mcp.json").exists())
        self.assertEqual(_snapshot(self.outside), before)

    def test_cli_init_refuses_a_hearmemory_symlink_outside_the_project(self):
        import shutil
        shutil.rmtree(self.root / ".hearmemory")
        (self.root / ".hearmemory").symlink_to(self.outside, target_is_directory=True)
        r = self.cli("init", "--hosts", "claude")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(list(self.outside.iterdir()), [])


class Hardening2_1GitTargets(_Sandbox):
    git = True

    def test_symlinked_pre_commit_hook_is_left_alone(self):
        user_hook = self.outside / "pre-commit"
        user_hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        user_hook.chmod(0o755)
        backup = self.root / ".git" / "hooks" / "pre-commit.hearmemory-orig"
        dispatcher = self.root / ".git" / "hooks" / "pre-commit"
        dispatcher.symlink_to(user_hook)
        (self.outside / "orig").write_text("#!/bin/sh\nexit 0  # other\n", encoding="utf-8")
        backup.symlink_to(self.outside / "orig")  # worst case: backup name also a link
        before = _snapshot(self.outside)
        with contextlib.redirect_stderr(io.StringIO()):
            host_install.install(self.root, ["git"], _cfg(), python=sys.executable)
        self.assertEqual(_snapshot(self.outside), before)

    def test_symlinked_info_exclude_is_left_alone(self):
        target = self.outside / "exclude"
        target.write_text("# global excludes\n", encoding="utf-8")
        ex = self.root / ".git" / "info" / "exclude"
        ex.parent.mkdir(exist_ok=True)
        if ex.exists():
            ex.unlink()
        ex.symlink_to(target)
        with contextlib.redirect_stderr(io.StringIO()):
            host_install.install(self.root, ["git"], _cfg(), python=sys.executable)
        self.assertEqual(target.read_text(encoding="utf-8"), "# global excludes\n")
        self.assertTrue((self.root / ".git" / "hooks" / "pre-commit").exists())


# ----------------------------------------------------------------------------------------- 2.2
class Hardening2_2HearmemoryDirIsAlwaysGitIgnored(_Sandbox):
    git = True

    def setUp(self):
        super().setUp()
        import shutil
        shutil.rmtree(self.root / ".hearmemory")  # let the real `hearmemory init` create the store

    def _staged(self):
        subprocess.run(["git", "add", "-A"], cwd=str(self.root), check=True, capture_output=True)
        out = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=str(self.root),
                             capture_output=True, text=True, check=True).stdout
        return [l for l in out.splitlines() if l.startswith(".hearmemory")]

    def test_create_store_writes_gitignore(self):
        store = create_store(self.base / "fresh")
        gi = store.hearmemory_dir / ".gitignore"
        self.assertEqual([l for l in gi.read_text().splitlines() if not l.startswith("#")], ["*"])

    def test_existing_store_without_gitignore_gets_one(self):
        store = create_store(self.root)
        (store.hearmemory_dir / ".gitignore").unlink()
        create_store(self.root)
        self.assertTrue((store.hearmemory_dir / ".gitignore").exists())

    def _check_not_staged(self, *init_args):
        r = self.cli("init", *init_args)
        self.assertEqual(r.returncode, 0, r.stderr)
        store = create_store(self.root)
        store.append_observations([observe.make_observation(
            self.root, _cfg(), "note", "hello", I.Provenance(host="cli", source="cli"),
            event_key="cli:r22")])
        self.assertTrue((self.root / ".hearmemory" / "observations.jsonl").exists())
        self.assertEqual(self._staged(), [])

    def test_husky_hooks_path(self):
        (self.root / ".husky").mkdir()
        subprocess.run(["git", "config", "core.hooksPath", ".husky"], cwd=str(self.root), check=True)
        self._check_not_staged()

    def test_global_hooks_path(self):
        gh = self.home / ".githooks"
        gh.mkdir()
        (self.home / ".gitconfig").write_text(f"[core]\n\thooksPath = {gh}\n", encoding="utf-8")
        self._check_not_staged()

    def test_hosts_without_git(self):
        for hosts in ("claude", "cursor", "codex"):
            with self.subTest(hosts=hosts):
                self._check_not_staged("--hosts", hosts)

    def test_no_git_hook_flag(self):
        self._check_not_staged("--hosts", "claude,git", "--no-git-hook")


# ----------------------------------------------------------------------------------------- 2.3
class Hardening2_3CursorRepeatedEventsInOneTurn(unittest.TestCase):
    def _obs(self, event, **kw):
        base = {"conversation_id": "conv-1", "generation_id": "gen-1", "hook_event_name": event,
                "_cfg": _cfg(), "_root": None}
        base.update(kw)
        return cursor.normalize(event, base)[0]

    def test_fail_then_pass_rerun_gets_two_observations(self):
        fail = self._obs("afterShellExecution", command="pytest tests/test_a.py", exit_code=1,
                         output="FAILED tests/test_a.py::t\n==== 1 failed in 0.1s ====", duration=812)
        ok = self._obs("afterShellExecution", command="pytest tests/test_a.py", exit_code=0,
                       output="==== 1 passed in 0.1s ====", duration=640)
        self.assertNotEqual(fail.id, ok.id)
        idx = RunIndex()
        for o, ts in ((fail, "2026-09-24T10:00:00.000000Z"), (ok, "2026-09-24T10:00:05.000000Z")):
            import dataclasses
            rec = run_record(dataclasses.replace(o, ts=ts), "cursor:conv-1")
            idx.add(rec)
        self.assertEqual(idx.latest(rec["target"])["outcome"], "pass")

    def test_same_event_delivered_twice_is_still_idempotent(self):
        a = self._obs("afterShellExecution", command="ls", exit_code=0, output="x", duration=5)
        b = self._obs("afterShellExecution", command="ls", exit_code=0, output="x", duration=5)
        self.assertEqual(a.id, b.id)

    def test_repeated_edits_to_one_path(self):
        e1 = self._obs("afterFileEdit", file_path="src/a.py",
                       edits=[{"old_string": "a = 1", "new_string": "a = 2"}])
        e2 = self._obs("afterFileEdit", file_path="src/a.py",
                       edits=[{"old_string": "a = 2", "new_string": "a = 3"}])
        self.assertNotEqual(e1.id, e2.id)


# ----------------------------------------------------------------------------------------- 2.4
def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"  # Codex style: milliseconds


class Hardening2_4ImportedCodexEventsKeepTheirOwnTime(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.root = make_project(self.base, git=True)
        (self.root / "README.md").write_text("dirty now\n", encoding="utf-8")  # dirty worktree
        self.home = self.base / "codex_home"
        self.old = datetime.now(timezone.utc) - timedelta(days=5, hours=1)
        d = self.home / "sessions" / "2026" / "09" / "19"
        d.mkdir(parents=True)
        t = self.old
        lines = [
            {"type": "session_meta", "timestamp": _iso(t), "payload": {"id": "cx", "cwd": str(self.root)}},
            {"type": "response_item", "timestamp": _iso(t + timedelta(seconds=1)),
             "payload": {"type": "function_call", "name": "exec_command", "call_id": "c1",
                         "arguments": json.dumps({"cmd": "pytest tests/test_a.py"})}},
            {"type": "response_item", "timestamp": _iso(t + timedelta(seconds=2)),
             "payload": {"type": "function_call_output", "call_id": "c1",
                         "output": "Exit code: 1\nFAILED tests/test_a.py::t\n==== 1 failed in 0.1s ===="}},
        ]
        (d / "rollout-x.jsonl").write_text("".join(json.dumps(l) + "\n" for l in lines), encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_observation_ts_is_the_rollout_time_and_no_current_dirty_state(self):
        store = FakeStore(self.root)
        codex.import_rollouts(store, _cfg(), codex_home=str(self.home))
        cmd = [o for o in store._obs if o.kind == "command"][0]
        self.assertEqual(cmd.ts, normalize_ts(_iso(self.old + timedelta(seconds=2))))
        self.assertNotIn("dirty_state", cmd.meta)
        self.assertIn("recorded_ts", cmd.meta)
        from hearmemory.memory import render
        self.assertIn("5d", render.ago(cmd.ts, now_ts(), "en").replace(" ", ""))

    def test_fresh_claude_pass_outranks_old_codex_failure(self):
        store = FakeStore(self.root)
        codex.import_rollouts(store, _cfg(), codex_home=str(self.home))
        old_fail = [o for o in store._obs if o.kind == "command"][0]
        tool = I.ToolInfo(name="Bash", command="pytest tests/test_a.py", exit_code=0, status="ok",
                          test=observe.parse_test_summary("pytest tests/test_a.py", "==== 1 passed in 0.1s ===="))
        fresh_pass = observe.make_observation(self.root, _cfg(), "command", "1 passed",
                                              I.Provenance(host="claude", session_id="s", source="hook"),
                                              tool=tool, event_key="claude:s:t1")
        idx = RunIndex()
        # the import happens AFTER the Claude run (this order): append order must not matter
        for o, actor in ((fresh_pass, "claude:s"), (old_fail, "codex:cx")):
            rec = run_record(o, actor)
            self.assertIsNotNone(rec)
            idx.add(rec)
        self.assertEqual(idx.latest(rec["target"])["outcome"], "pass")
        self.assertIn("dirty_state", fresh_pass.meta, "live events still record dirty_state")

    def test_normalize_ts(self):
        self.assertEqual(normalize_ts("2026-09-19T08:01:02.345Z"), "2026-09-19T08:01:02.345000Z")
        self.assertEqual(normalize_ts("2026-09-19T10:01:02+02:00"), "2026-09-19T08:01:02.000000Z")
        self.assertEqual(normalize_ts("2026-09-19T08:01:02.123456789Z"), "2026-09-19T08:01:02.123456Z")
        self.assertIsNone(normalize_ts("yesterday"))
        self.assertIsNone(normalize_ts(None))
        future = _iso(datetime.now(timezone.utc) + timedelta(days=3))
        self.assertLessEqual(normalize_ts(future), now_ts())


# ----------------------------------------------------------------------------------------- 2.5
class Hardening2_5LargeOutputsStayFast(unittest.TestCase):
    def test_redaction_only_sees_a_bounded_prefix_and_suffix(self):
        seen = []
        real = privacy.redact

        def spy(text, **kw):
            seen.append(len(text))
            return real(text, **kw)
        big = ("sk_live_" + "A1b2C3d4E5f6G7h8I9j0K1") + " head\n" + ("x" * 100 + "\n") * 200_000 + \
              "tail token=sk_live_ZZ1b2C3d4E5f6G7h8I9j0 end\n==== 1 failed in 3s ===="
        with mock.patch.object(observe, "redact", side_effect=spy):
            t0 = time.monotonic()
            o = observe.make_observation(None, _cfg(), "command", big,
                                         I.Provenance(host="cli", source="t"),
                                         event_key="t:big")
            dt = time.monotonic() - t0
        self.assertLess(max(seen), 12_000, "redaction ran over the whole 20 MB text")
        self.assertLess(dt, 2.0)
        self.assertTrue(o.truncated)
        self.assertLessEqual(len(o.text), 4200)
        self.assertNotIn("A1b2C3d4E5f6G7h8I9j0K1", o.text)
        self.assertNotIn("ZZ1b2C3d4E5f6G7h8I9j0", o.text)
        self.assertIn("1 failed", o.text)
        self.assertEqual(o.meta.get("original_chars"), len(big))
        omitted = int(o.text.split("[hearmemory: ")[1].split(" chars")[0])
        self.assertGreater(omitted, len(big) - 5000)

    def test_private_key_straddling_the_kept_head_is_redacted(self):
        key = "-----BEGIN PRIVATE KEY-----\n" + ("M" * 64 + "\n") * 50 + "-----END PRIVATE KEY-----\n"
        text = "a" * 2000 + key + "b" * 5_000_000
        o = observe.make_observation(None, _cfg(), "note", text, I.Provenance(host="cli", source="t"),
                                     event_key="t:pem")
        self.assertNotIn("MMMMMMMM", o.text)

    def test_private_key_cut_by_the_precut_is_still_removed(self):
        key = "-----BEGIN RSA PRIVATE KEY-----\n" + ("K" * 64 + "\n") * 60 + "-----END RSA PRIVATE KEY-----\n"
        for text in ("a" * 2400 + key + "b" * 3_000_000,          # opens in the head, ends later
                     "b" * 3_000_000 + key + "c" * 1000):          # ends in the tail, opened earlier
            o = observe.make_observation(None, _cfg(), "note", text, I.Provenance(host="cli", source="t"),
                                         event_key="t:pem2")
            self.assertNotIn("KKKKKKKK", o.text)
            self.assertGreaterEqual(o.redactions, 1)

    def test_text_within_a_large_max_text_chars_is_kept_whole(self):
        cfg = _cfg()
        cfg["capture"]["max_text_chars"] = 100_000
        text = "z" * 50_000
        o = observe.make_observation(None, cfg, "note", text, I.Provenance(host="cli", source="t"),
                                     event_key="t:keep")
        self.assertEqual(o.text, text)
        self.assertFalse(o.truncated)

    def test_small_text_unchanged(self):
        o = observe.make_observation(None, _cfg(), "note", "hello", I.Provenance(host="cli", source="t"),
                                     event_key="t:small")
        self.assertEqual(o.text, "hello")
        self.assertFalse(o.truncated)
        self.assertNotIn("original_chars", o.meta)

    def test_test_summary_on_huge_single_line_output_is_fast(self):
        out = "=" * 5_000_000 + " no newline " + "\n==== 2 failed, 3 passed in 1.0s ===="
        t0 = time.monotonic()
        s = observe.parse_test_summary("pytest", out)
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertEqual((s.failed, s.passed), (2, 3))


class Hardening2_5OversizedRolloutLines(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.root = make_project(self.base)
        self.home = self.base / "codex_home"
        d = self.home / "sessions" / "2026" / "09" / "24"
        d.mkdir(parents=True)
        self.path = d / "rollout-big.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, output_body_chars):
        out = "Exit code: 1\nWall time: 3.0 seconds\nOutput:\n" + ("line of noise\n" * (output_body_chars // 14)) + \
              "FAILED tests/test_big.py::t\n==== 1 failed, 4 passed in 2.0s ===="
        lines = [
            {"timestamp": "2026-09-24T11:00:00.000Z", "type": "session_meta",
             "payload": {"id": "big", "cwd": str(self.root)}},
            {"timestamp": "2026-09-24T11:00:01.000Z", "type": "response_item",
             "payload": {"type": "function_call", "name": "exec_command", "call_id": "cb",
                         "arguments": json.dumps({"cmd": "pytest tests/test_big.py"})}},
            {"timestamp": "2026-09-24T11:00:02.000Z", "type": "response_item",
             "payload": {"type": "function_call_output", "call_id": "cb", "output": out}},
            {"timestamp": "2026-09-24T11:00:03.000Z", "type": "event_msg",
             "payload": {"type": "agent_message", "message": "the big test fails"}},
        ]
        self.path.write_text("".join(json.dumps(l) + "\n" for l in lines), encoding="utf-8")

    def _import(self, cfg=None, deadline_s=None):
        store = FakeStore(self.root)
        t0 = time.monotonic()
        codex.import_rollouts(store, cfg or _cfg(), codex_home=str(self.home), deadline_s=deadline_s)
        return store, time.monotonic() - t0

    def _check(self, store):
        cur = store.read_state("cursors")["import"]["codex"][str(self.path)]
        self.assertEqual(cur["offset"], self.path.stat().st_size, "cursor not advanced past the line")
        cmd = [o for o in store._obs if o.kind == "command"]
        self.assertEqual(len(cmd), 1)
        self.assertEqual(cmd[0].tool.exit_code, 1)
        self.assertEqual((cmd[0].tool.test.failed, cmd[0].tool.test.passed), (1, 4))
        self.assertLessEqual(len(cmd[0].text), 4200)
        self.assertTrue(any(o.kind == "assistant_message" for o in store._obs))

    def test_line_over_the_cap_is_not_parsed_whole(self):
        self._write(2_000_000)
        cfg = _cfg()
        cfg["import"]["codex_max_line_bytes"] = 200_000
        real_loads = json.loads
        sizes = []
        with mock.patch.object(codex.json, "loads",
                               side_effect=lambda s, *a, **k: sizes.append(len(s)) or real_loads(s, *a, **k)):
            store, _ = self._import(cfg)
        self.assertLess(max(sizes), 300_000, "the oversized line was json-parsed whole")
        self._check(store)

    def test_60mb_style_line_imports_fast_with_default_cap(self):
        self._write(30_000_000)
        store, dt = self._import(deadline_s=5.0)
        self.assertLess(dt, 5.0)
        self._check(store)

    def test_line_just_under_the_cap_is_parsed_normally(self):
        self._write(1_500_000)  # like the real 1.4-1.9 MB lines on the server
        store, dt = self._import()
        self.assertLess(dt, 5.0)
        self._check(store)


if __name__ == "__main__":
    unittest.main()
