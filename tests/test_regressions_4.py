"""Regression tests for hardening set 4.
Offline only: fake hook payloads, fake Codex rollouts, sandboxed HOME, real `git commit` in temporary
repositories; nothing is written outside the temporary directories and Jev is never called."""
import contextlib
import io
import json
import os
import random
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tests"))

from test_regressions_3 import _Tmp, _cfg, _env, _git, _set_config  # noqa: E402

import hearmemory.interfaces as I  # noqa: E402
import hearmemory.host.codex as codex  # noqa: E402
import hearmemory.memory.runs as runs_mod  # noqa: E402
from hearmemory import observe, testcmd  # noqa: E402
from hearmemory.host import hooks, install as host_install, manifest as M  # noqa: E402
from hearmemory.memory.build import MemoryBuilder  # noqa: E402
from hearmemory.memory.runs import RunIndex, run_record  # noqa: E402
from hearmemory.store import create_store  # noqa: E402
from hearmemory.testcmd import canonical_target, is_test_command, target_covers  # noqa: E402

FAIL_OUT = ("Exit code 1\nFAILED tests/test_app.py::test_f - assert 0\n"
            "========= 1 failed in 0.05s =========\n")


# ============================================================================================ 4.1
class Hardening4_1OnlyRealTestRunsCountAsPasses(_Tmp):
    # (second command, its output, commit must be blocked?) -- after `pytest tests/test_app.py` failed
    CASES = [
        ("pytest --version", "pytest 8.3.2\n", True),
        ("pytest --collect-only -q", "tests/test_app.py::test_f\n\n1 test collected in 0.01s\n", True),
        ("pytest --co", "<Module test_app.py>\n  <Function test_f>\n", True),
        ("pytest -h", "usage: pytest [options] [file_or_dir] [file_or_dir] [...]\n", True),
        ("pytest --fixtures", "cache -- .../_pytest/cacheprovider.py:560\n", True),
        ("cd tests/unit && pytest", "========= 3 passed in 0.10s =========\n", True),
        ("cd /tmp && pytest", "========= 2 passed in 0.10s =========\n", True),
        ("pytest tests/test_app.py > /tmp/log.txt 2>&1; echo done", "done\n", True),
        ("pytest tests/test_app.py 2>&1 | head -3",
         "========= test session starts =========\nplatform linux\ncollected 1 item\n", True),
        ("pytest tests/test_app.py || echo 'tests failed'", "F\ntests failed\n", True),
        # controls: unchanged
        ("echo nothing", "nothing\n", True),
        ("pytest tests/test_other.py", "========= 1 passed in 0.02s =========\n", True),
        ("pytest tests/test_app.py", "========= 1 passed in 0.02s =========\n", False),
        ("pytest tests/test_app.py 2>&1 | tail -20", "========= 1 passed in 0.02s =========\n", False),
        ("cd tests && pytest", "========= 4 passed in 0.10s =========\n", False),
    ]

    def _record(self, root, event, command, output, n):
        payload = {"session_id": "s-%d" % n, "cwd": str(root), "tool_use_id": "tu-%d" % n, "tool_name": "Bash",
                   "tool_input": {"command": command}}
        if event == "PostToolUseFailure":
            payload["error"] = output
        else:
            payload["tool_response"] = {"stdout": output, "stderr": "", "interrupted": False}
        r = hooks.run_hook("claude", event, json.dumps(payload).encode(), root=str(root))
        self.assertEqual(r.exit_code, 0)

    def test_block_mode_commit_after_fail_then_a_non_run(self):
        for i, (cmd, out, want_block) in enumerate(self.CASES):
            with self.subTest(cmd=cmd):
                root = self.project("p%d" % i)
                (root / "tests" / "unit").mkdir(parents=True)
                self.assertEqual(self.cli(root, "init", "--hosts", "git").returncode, 0)
                _set_config(root, ('git_mode = "warn"', 'git_mode = "block"'))
                self._record(root, "PostToolUseFailure", "pytest tests/test_app.py", FAIL_OUT, 1)
                time.sleep(0.01)
                self._record(root, "PostToolUse", cmd, out, 2)
                (root / "app.py").write_text("x = %d\n" % i, encoding="utf-8")
                (root / "tests" / "test_app.py").write_text("def test_f():\n    assert %d\n" % i, encoding="utf-8")
                _git(root, "add", "-A")
                r = subprocess.run(["git", "commit", "-q", "-m", "c%d" % i], cwd=str(root), env=_env(self.home),
                                   capture_output=True, text=True, timeout=120)
                self.assertEqual(r.returncode != 0, want_block, r.stdout + r.stderr)

    def test_canonical_forms(self):
        root = "/srv/proj"
        cases = {
            "pytest --version": "", "pytest -V": "", "pytest --collect-only -q": "", "pytest --co": "",
            "pytest -h": "", "pytest --help": "", "pytest --fixtures": "", "pytest --markers": "",
            "pytest --setup-plan tests": "", "PYTEST_ADDOPTS=--co pytest": "",
            "export PYTEST_ADDOPTS='--co -q'; pytest": "", "cd ~/x && pytest": "", "cd $D && pytest": "",
            "cd - && pytest": "", "cargo test --no-run": "", "jest --listTests": "",
            "cd tests/unit && pytest": "pytest tests/unit",
            "cd /srv/proj/tests && pytest -q": "pytest tests",
            "cd /tmp && pytest": "pytest /tmp",
            "cd ../otherlib && pytest tests/test_app.py": "pytest /srv/otherlib/tests/test_app.py",
            "cd /srv/proj && pytest": "pytest",
            "pytest --runslow tests": "pytest --runslow tests",
            "cd frontend && npm test": "cd frontend && npm test",
            "cd /tmp && npm test": "cd /tmp && npm test",
            "pytest -q --cov=src --ff tests/test_app.py": "pytest tests/test_app.py",
        }
        for cmd, want in cases.items():
            with self.subTest(cmd=cmd):
                got = testcmd.placed_target(cmd, root)
                self.assertEqual(got, want)
                self.assertEqual(bool(want), is_test_command(cmd) or cmd.startswith("cd ../"))
                if want:
                    self.assertEqual(canonical_target(want, root), want, "idempotent with the root")
                    self.assertEqual(canonical_target(want), want, "idempotent without the root")
        self.assertIsNone(testcmd.placed_target("echo hi", root))
        # a run placed elsewhere / with an unknown option never covers the project's targets
        self.assertFalse(target_covers("pytest tests/unit", "pytest tests/test_app.py"))
        self.assertFalse(target_covers("pytest /tmp", "pytest tests/test_app.py"))
        self.assertFalse(target_covers("pytest --runslow tests", "pytest tests/test_app.py"))
        self.assertFalse(target_covers("cd frontend && npm test", "npm test"))
        self.assertFalse(target_covers("npm test", "cd frontend && npm test"))
        self.assertTrue(target_covers("pytest tests", "pytest tests/unit/test_x.py::test_a"))

    def test_exit_status_belongs_to_the_test_run(self):
        owned = ["pytest x", "cd a && pytest x", "pytest x && echo ok", "pytest x;", "source v; pytest x",
                 "pytest a && pytest b", "timeout 60 pytest x 2>&1"]
        not_owned = ["pytest x; echo done", "pytest x || echo failed", "pytest x | head -3", "pytest x | tail -5",
                     "pytest x &", "pytest a; pytest b", "false || pytest x", "pytest x > log; echo done"]
        for c in owned:
            self.assertIs(testcmd.exit_owned(c), True, c)
        for c in not_owned:
            self.assertIs(testcmd.exit_owned(c), False, c)
        self.assertIsNone(testcmd.exit_owned("ls -la"))
        self.assertIs(testcmd.exit_owned("pytest --version"), False)

    def test_outcome_rules(self):
        def tool(cmd, exit_code=0, status="ok", passed=0, failed=0):
            test = I.RunnerSummary(runner="pytest", passed=passed, failed=failed) if (passed or failed) else None
            return I.ToolInfo(name="Bash", command=cmd, exit_code=exit_code, status=status, test=test)
        self.assertIsNone(testcmd.test_outcome(tool("pytest --version")))
        self.assertIsNone(testcmd.test_outcome(tool("pytest --co -q", passed=0)))
        self.assertIsNone(testcmd.test_outcome(tool("pytest x; echo done")))
        self.assertIsNone(testcmd.test_outcome(tool("pytest x | head -3")))
        self.assertIsNone(testcmd.test_outcome(tool("pytest x || echo failed")))
        self.assertEqual(testcmd.test_outcome(tool("pytest x || echo failed", failed=1)), "fail")
        self.assertEqual(testcmd.test_outcome(tool("pytest x | tail -5", passed=3)), "pass")
        # the command failed because of what came AFTER pytest, pytest itself passed
        self.assertEqual(testcmd.test_outcome(tool("pytest x; false", exit_code=1, status="error", passed=3)), "pass")
        # one summary cannot vouch for two runs whose statuses we cannot see
        self.assertIsNone(testcmd.test_outcome(tool("pytest a; pytest b", passed=3)))
        self.assertEqual(testcmd.test_outcome(tool("pytest x")), "pass")
        self.assertEqual(testcmd.test_outcome(tool("pytest x", exit_code=1, status="error")), "fail")
        self.assertEqual(testcmd.test_outcome(tool("./run_tests.sh", exit_code=0)), "pass")    # unknown runner: as before

    def test_codex_workdir_places_a_bare_pytest(self):
        root = self.project("codex", git=False)
        (root / "tests" / "unit").mkdir(parents=True)
        home = self.base / "codex_home"
        d = home / "sessions" / "2026" / "09" / "24"
        d.mkdir(parents=True)

        def line(ts, typ, payload):
            return json.dumps({"timestamp": ts, "type": typ, "payload": payload})

        def call(ts, cid, args):
            return line(ts, "response_item", {"type": "function_call", "name": "exec_command", "call_id": cid,
                                              "arguments": json.dumps(args)})

        def out(ts, cid, text):
            return line(ts, "response_item", {"type": "function_call_output", "call_id": cid, "output": text})

        rows = [
            line("2026-09-24T11:00:00Z", "session_meta", {"id": "sess-wd", "cwd": str(root)}),
            call("2026-09-24T11:00:01Z", "c1", {"cmd": "pytest tests/test_app.py"}),
            out("2026-09-24T11:00:02Z", "c1", "Process exited with code 1\nOutput:\n" + FAIL_OUT),
            call("2026-09-24T11:00:03Z", "c2", {"cmd": "pytest -q", "workdir": str(root / "tests" / "unit")}),
            out("2026-09-24T11:00:04Z", "c2", "Process exited with code 0\nOutput:\n===== 3 passed in 0.1s =====\n"),
            call("2026-09-24T11:00:05Z", "c3", {"cmd": "pytest", "workdir": "tests/unit"}),
            out("2026-09-24T11:00:06Z", "c3", "Process exited with code 0\nOutput:\n===== 3 passed in 0.1s =====\n"),
        ]
        (d / "rollout-2026-09-24T11-00-00-sess-wd.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")
        store = create_store(root)
        codex.import_rollouts(store, _cfg(), codex_home=str(home))
        idx = RunIndex()
        for _, o in store.iter_observations(0):
            rec = run_record(o, I.actor_key(o.provenance))
            if rec is not None:
                idx.add(rec)
        self.assertEqual(idx.latest("pytest tests/test_app.py")["outcome"], "fail")
        self.assertEqual(idx.latest("pytest tests/unit")["outcome"], "pass")
        self.assertIn("pytest tests/test_app.py", [r["target"] for r in idx.failing()])


# ============================================================================================ 4.2
class Hardening4_2UninstallKeepsAUserHookInHearmemorysMatcherEntry(_Tmp):
    def _add_user_hook(self, root):
        sp = root / ".claude" / "settings.local.json"
        st = json.loads(sp.read_text(encoding="utf-8"))
        entries = st["hooks"]["PreToolUse"]
        mine = [e for e in entries if M.is_hearmemory_entry(e)]
        self.assertTrue(mine, entries)
        mine[0]["hooks"].append({"type": "command", "command": "./scripts/guard.sh"})
        sp.write_text(json.dumps(st, indent=2), encoding="utf-8")
        return sp

    def _user_hooks_left(self, sp):
        st = json.loads(sp.read_text(encoding="utf-8"))
        found = []
        for event, entries in (st.get("hooks") or {}).items():
            for e in entries:
                for h in e.get("hooks") or []:
                    self.assertNotIn("-m hearmemory", h.get("command", ""), "a hearmemory hook was left behind")
                    found.append((event, e.get("matcher"), h["command"]))
        return found

    def test_plain_uninstall(self):
        root = self.project("plain", git=False)
        with contextlib.redirect_stderr(io.StringIO()):
            host_install.install(root, ["claude"], _cfg(), python=sys.executable, claude_persist=True)
        sp = self._add_user_hook(root)
        matcher = [e for e in json.loads(sp.read_text())["hooks"]["PreToolUse"] if M.is_hearmemory_entry(e)][0].get("matcher")
        with contextlib.redirect_stderr(io.StringIO()):
            host_install.uninstall(root)
        self.assertEqual(self._user_hooks_left(sp), [("PreToolUse", matcher, "./scripts/guard.sh")])

    def test_uninstall_purge_via_cli(self):
        root = self.project("purge", git=False)
        r = self.cli(root, "init", "--hosts", "claude", "--claude-persist")
        self.assertEqual(r.returncode, 0, r.stderr)
        sp = self._add_user_hook(root)
        r = self.cli(root, "uninstall", "--purge", "--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((root / ".hearmemory").exists())
        self.assertTrue(sp.exists(), "the user's hook file was deleted")
        self.assertIn(("PreToolUse", "Bash", "./scripts/guard.sh"), self._user_hooks_left(sp))

    def test_without_hearmemory_unit(self):
        hearmemory_h = {"type": "command", "command": "'/usr/bin/python3' -m hearmemory --project '/p' hook claude Stop"}
        user_h = {"type": "command", "command": "./guard.sh"}
        entries = [{"matcher": "Bash", "hooks": [hearmemory_h, user_h]},     # mixed: keep the user's hook
                   {"matcher": "Edit", "hooks": [hearmemory_h]},             # hearmemory only: goes
                   {"matcher": "Read", "hooks": [user_h]},              # user only: untouched
                   {"command": "'/usr/bin/python3' -m hearmemory --project '/p' hook cursor stop"},   # cursor style
                   {"command": "./my-stop.sh"}]
        self.assertEqual(M.without_hearmemory(entries), [{"matcher": "Bash", "hooks": [user_h]},
                                                    {"matcher": "Read", "hooks": [user_h]},
                                                    {"command": "./my-stop.sh"}])
        legacy = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [hearmemory_h, user_h]}]}}
        M.remove_json_keys(legacy, [["hooks"]])
        self.assertEqual(legacy, {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [user_h]}]}})


# ============================================================================================ 4.3
SECRETS = ["hf_AbCdEfGhIjKlMnOpQrStUvWxYz012345", "q8w7e6r5t4y3u2i1o0p9",
           "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-AbCdEfGh", "Hunter2Secret",
           "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789", "Hunter3Patch"]


class Hardening4_3PendingCodexCallsAreRedactedOnDisk(_Tmp):
    def _rollout(self, root, rows):
        home = self.base / "codex_home"
        d = home / "sessions" / "2026" / "09" / "24"
        d.mkdir(parents=True, exist_ok=True)
        (d / "rollout-2026-09-24T11-00-00-sess-sec.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")
        return home

    def _all_hearmemory_text(self, root):
        out = []
        for p in (root / ".hearmemory").rglob("*"):
            if p.is_file():
                out.append(p.read_bytes().decode("utf-8", "replace"))
        return "\n".join(out)

    def test_running_command_and_output_never_reach_the_cursor_file_raw(self):
        root = self.project("sec", git=False)

        def line(ts, typ, payload):
            return json.dumps({"timestamp": ts, "type": typ, "payload": payload})

        rows = [
            line("2026-09-24T11:00:00Z", "session_meta", {"id": "sess-sec", "cwd": str(root)}),
            line("2026-09-24T11:00:01Z", "response_item", {"type": "function_call", "name": "exec_command",
                 "call_id": "c1", "arguments": json.dumps({"cmd": "HF_TOKEN=%s python -m pytest tests/test_app.py "
                                                                  "--api-key %s" % (SECRETS[0], SECRETS[1])})}),
            line("2026-09-24T11:00:11Z", "response_item", {"type": "function_call_output", "call_id": "c1",
                 "output": "Chunk ID: ab12\nWall time: 10.0 seconds\nProcess running with session ID 4867\n"
                           "Original token count: 20\nOutput:\nANTHROPIC_API_KEY=%s\npassword=%s\n"
                           % (SECRETS[2], SECRETS[3])}),
            line("2026-09-24T11:00:20Z", "response_item", {"type": "function_call", "name": "write_stdin",
                 "call_id": "c2", "arguments": json.dumps({"session_id": 4867, "chars": ""})}),
            line("2026-09-24T11:00:31Z", "response_item", {"type": "function_call_output", "call_id": "c2",
                 "output": "Chunk ID: cd34\nWall time: 11.0 seconds\nProcess running with session ID 4867\n"
                           "Output:\nGITHUB_TOKEN=%s\n" % SECRETS[4]}),
            # an exec call and an apply_patch call whose outputs are not in the file yet
            line("2026-09-24T11:00:40Z", "response_item", {"type": "function_call", "name": "exec_command",
                 "call_id": "c3", "arguments": json.dumps({"cmd": "curl -H 'Authorization: Bearer %s' x" % SECRETS[4]})}),
            line("2026-09-24T11:00:41Z", "response_item", {"type": "custom_tool_call", "name": "apply_patch",
                 "call_id": "c4", "input": "*** Begin Patch\n*** Add File: conf.py\n+PASSWORD = 'x'\n"
                                           "+password=%s\n*** End Patch\n" % SECRETS[5]}),
        ]
        home = self._rollout(root, rows)
        store = create_store(root)
        codex.import_rollouts(store, _cfg(), codex_home=str(home))
        cursors = (root / ".hearmemory" / "state" / "cursors.json").read_text(encoding="utf-8")
        self.assertIn("proc:4867", cursors, "the running command must still be pending")
        self.assertIn("pytest tests/test_app.py", cursors)
        blob = self._all_hearmemory_text(root)
        for s in SECRETS:
            self.assertNotIn(s, blob, s)

    def test_pending_calls_from_an_older_version_are_scrubbed(self):
        root = self.project("legacy", git=False)
        home = self._rollout(root, [json.dumps({"timestamp": "2026-09-24T11:00:00Z", "type": "session_meta",
                                                "payload": {"id": "sess-sec", "cwd": str(root)}})])
        store = create_store(root)
        codex.import_rollouts(store, _cfg(), codex_home=str(home))
        cur = store.read_state("cursors")
        (entry,) = cur["import"]["codex"].values()
        entry["_pending_call"] = {
            "proc:1": {"kind": "proc", "cmd": "pytest --api-key %s" % SECRETS[1], "call_id": "c1", "ts": None,
                       "out": "password=%s" % SECRETS[3]},
            "c9": {"kind": "exec", "cmd": "HF_TOKEN=%s pytest" % SECRETS[0], "ts": None}}
        entry["offset"] = 0              # the file is read again
        store.write_state("cursors", cur)
        codex.import_rollouts(store, _cfg(), codex_home=str(home))
        blob = self._all_hearmemory_text(root)
        for s in (SECRETS[0], SECRETS[1], SECRETS[3]):
            self.assertNotIn(s, blob, s)

    def test_redaction_unavailable_fails_closed(self):
        with mock.patch.object(codex._deps, "redact", None):
            self.assertNotIn(SECRETS[1], codex._redacted("pytest --api-key " + SECRETS[1], {}))


# ============================================================================================ 4.4
def _rec(i, target, outcome, ts):
    return {"obs_id": "o%05d" % i, "ts": ts, "target": target, "outcome": outcome, "actor": "a%d" % (i % 3),
            "host": "claude", "session": "s", "subagent": None, "subagent_type": None, "commit": None,
            "paths": [], "failed_ids": [], "summary": "", "command": target}


def _targets(n):
    out = ["pytest", "pytest tests", "pytest -k slow tests"]
    i = 0
    while len(out) < n:
        d = "tests/pkg%d" % (i % 7)
        out += ["pytest %s" % d, "pytest %s/test_m%d.py" % (d, i), "pytest %s/test_m%d.py::test_f%d" % (d, i, i),
                "pytest %s/test_m%d.py::TestC::test_g" % (d, i), "npm test -- m%d" % i]
        i += 1
    return out[:n]


class Hardening4_4RunIndexCostIsNotRecordsTimesTargets(_Tmp):
    def _fill(self, n_runs, n_targets, seed=1):
        rnd = random.Random(seed)
        tg = _targets(n_targets)
        idx = RunIndex()
        for i in range(n_runs):
            idx.add(_rec(i, rnd.choice(tg), "pass" if rnd.random() < 0.7 else "fail",
                         "2026-09-24T10:%02d:%02d.%06dZ" % (i // 3600 % 60, i // 60 % 60, i)))
        return idx

    def test_covered_by_matches_the_pairwise_definition(self):
        idx = self._fill(1500, 80, seed=7)
        data = idx.to_dict()
        for n, cur in data.items():
            best = None
            for w, c in data.items():
                lp = c.get("last_pass")
                if w != n and lp and target_covers(w, n) and (best is None or (lp["ts"], lp["obs_id"]) >
                                                                (best["ts"], best["obs_id"])):
                    best = lp
            self.assertEqual(cur.get("covered_by"), best, n)
        # incremental: adding to a copy (the overlay path) gives the same as building at once
        more = [_rec(90000 + i, t, "pass", "2026-09-25T00:00:%02dZ" % i) for i, t in
                enumerate(["pytest tests/pkg1", "pytest tests/pkg2/test_m2.py"])]
        a = RunIndex(data).copy()
        for r in more:
            a.add(r)
        b = self._fill(1500, 80, seed=7)
        for r in more:
            b.add(r)
        self.assertEqual(a.to_dict(), b.to_dict())

    def test_3000_runs_over_120_targets_is_fast(self):
        calls = [0]
        real = runs_mod.target_covers

        def counting(w, n):
            calls[0] += 1
            return real(w, n)
        with mock.patch.object(runs_mod, "target_covers", counting):
            t0 = time.monotonic()
            idx = self._fill(3000, 120)
            idx.failing()
            idx.to_dict()
            dt = time.monotonic() - t0
        self.assertLess(calls[0], 120 * 40, "coverage must not compare every record with every target")
        self.assertLess(dt, 3.0)

    def test_rebuild_with_3000_runs_writes_memory_in_time(self):
        root = self.project("perf", git=False)
        store = create_store(root)
        cfg = _cfg()
        rnd = random.Random(3)
        tg = [t for t in _targets(120)]
        obs = []
        for i in range(3000):
            tgt = rnd.choice(tg)
            ok = rnd.random() < 0.7
            out = "===== 1 passed in 0.1s =====" if ok else "FAILED x::y\n===== 1 failed in 0.1s ====="
            prov = I.Provenance(host="claude", session_id="s%d" % (i % 5), cwd=str(root), source="hook:PostToolUse")
            tool = I.ToolInfo(name="Bash", command=tgt, exit_code=0 if ok else 1, status="ok" if ok else "error",
                              test=observe.parse_test_summary(tgt, out))
            obs.append(observe.make_observation(root, cfg, "command", "$ %s\n%s" % (tgt, out), prov, tool=tool,
                                                event_key="perf:%d" % i, historical=True,
                                                event_ts="2026-09-24T%02d:%02d:%02dZ" % (i // 3600, i // 60 % 60, i % 60)))
        store.append_observations(obs)
        t0 = time.monotonic()
        state = MemoryBuilder().build(store, cfg, deadline_s=30)
        dt = time.monotonic() - t0
        self.assertIsNotNone(state)
        self.assertGreater(len((state.stats or {}).get("runs") or {}), 50)
        self.assertLess(dt, 15.0)


if __name__ == "__main__":
    unittest.main()
