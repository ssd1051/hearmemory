"""Regression tests for hardening set 3 (quoting, test targets, secrets, subagent results, uninstall, commands,
running commands) and a worker-cleanup race. Offline only: fake payloads, fake rollouts, fake
Jev client, sandboxed HOME; nothing is written outside the temporary directories."""
import contextlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(REPO / "tests"))

from test_host_common import FakeStore  # noqa: E402

import hearmemory.interfaces as I  # noqa: E402
import hearmemory.host.codex as codex  # noqa: E402
from hearmemory import observe, privacy  # noqa: E402
from hearmemory.host import _deps, claude, hooks, install as host_install, manifest as M, snippets as S  # noqa: E402
from hearmemory.memory.runs import RunIndex, run_record  # noqa: E402
from hearmemory.store import create_store  # noqa: E402

DASH = shutil.which("dash")
BASH = shutil.which("bash")
SHELLS = [s for s in (DASH, BASH) if s]
HOOK_SUFFIX = " 2>/dev/null || true"
NASTY = "we ird's \"q\" $HOME `x` dir"          # space ' " $ backtick, all at once
SINGLE_CHAR_NAMES = ["sp ace", "bob's proj", 'dq"uote', "dol$lar", "back`tick"]


def _cfg(**over):
    cfg = json.loads(json.dumps(I.DEFAULT_CONFIG))
    for sect, kv in over.items():
        cfg[sect] = dict(cfg[sect], **kv)
    return cfg


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True, check=True)


def _env(home):
    env = dict(os.environ)
    env.update({"PYTHONPATH": str(SRC), "HOME": str(home), "XDG_CONFIG_HOME": str(Path(home) / ".config"),
                "GIT_CONFIG_NOSYSTEM": "1"})
    env.pop("TYPESAFE_API_KEY", None)
    return env


def _set_config(root, *pairs):
    p = Path(root) / ".hearmemory" / "config.toml"
    text = p.read_text(encoding="utf-8")
    for old, new in pairs:
        assert old in text, old
        text = text.replace(old, new)
    p.write_text(text, encoding="utf-8")


class _Tmp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="hearmemory-r3-")
        self.base = Path(self._tmp)
        self.home = self.base / "home"
        self.home.mkdir()
        self._envp = mock.patch.dict(os.environ, {"HOME": str(self.home),
                                                  "XDG_CONFIG_HOME": str(self.home / ".config"),
                                                  "GIT_CONFIG_NOSYSTEM": "1"})
        self._envp.start()
        # in-process hooks must never start a real background worker from these tests
        self._spawn = mock.patch.object(_deps, "spawn_worker", lambda *a, **k: False)
        self._spawn.start()

    def tearDown(self):
        self._spawn.stop()
        self._envp.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def project(self, name="proj", git=True):
        root = self.base / name / "app"
        root.mkdir(parents=True)
        if git:
            _git(root, "init", "-q")
            _git(root, "config", "user.email", "t@example.com")
            _git(root, "config", "user.name", "t")
            (root / "README.md").write_text("hello\n", encoding="utf-8")
            _git(root, "add", "README.md")
            _git(root, "commit", "-q", "-m", "init")
        store = create_store(root)
        self.assertTrue(store.is_initialised())
        _set_config(root, ("spawn_from_hooks = true", "spawn_from_hooks = false"))
        return root

    def cli(self, root, *args):
        return subprocess.run([sys.executable, "-m", "hearmemory", "--project", str(root), *args], cwd=str(root),
                              env=_env(self.home), capture_output=True, text=True, timeout=60)


# ============================================================================================ 3.1
class Hardening3_1QuotedPathsInGeneratedShellAndToml(_Tmp):
    def _python_wrapper(self, parent: Path) -> str:
        """An interpreter whose own path is nasty too: `<parent>/py bin/python` -> the real python."""
        d = parent / "py bin"
        d.mkdir(parents=True)
        w = d / "python"
        w.write_text("#!/bin/sh\nexec %s \"$@\"\n" % shlex.quote(sys.executable), encoding="utf-8")
        w.chmod(0o755)
        return str(w)

    def _install_all(self, root, python):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            host_install.install(root, ["claude", "codex", "cursor", "git"], _cfg(), python=python,
                                 claude_persist=True, with_codex_hooks=True)
        return err.getvalue()

    def _hook_commands(self, root):
        cmds = []
        settings = json.loads((root / ".hearmemory/host/claude/settings.json").read_text(encoding="utf-8"))
        for event, entries in settings["hooks"].items():
            for e in entries:
                for h in e["hooks"]:
                    cmds.append(("claude", event, h["command"]))
        for rel, host in ((".cursor/hooks.json", "cursor"), (".hearmemory/host/codex/hooks.json", "codex")):
            data = json.loads((root / rel).read_text(encoding="utf-8"))
            for event, entries in data["hooks"].items():
                for e in entries:
                    cmds.append((host, event, e["command"]))
        return cmds

    @unittest.skipUnless(SHELLS, "needs dash or bash")
    def test_every_generated_command_parses_for_each_special_character(self):
        for name in SINGLE_CHAR_NAMES + [NASTY]:
            with self.subTest(path=name):
                root = self.project(name)
                self._install_all(root, self._python_wrapper(self.base / name))
                scripts = [root / ".hearmemory/host/claude/launch.sh", root / ".hearmemory/host/codex/launch.sh",
                           root / ".hearmemory/host/git/pre-commit"]
                for sh in SHELLS:
                    for host, event, cmd in self._hook_commands(root):
                        r = subprocess.run([sh, "-n", "-c", cmd], capture_output=True, text=True)
                        self.assertEqual(r.returncode, 0, f"{sh} {host} {event}: {r.stderr} :: {cmd}")
                        self.assertIn(str(root), shlex.split(cmd.replace(HOOK_SUFFIX, "")))
                    for script in scripts:
                        r = subprocess.run([sh, "-n", str(script)], capture_output=True, text=True)
                        self.assertEqual(r.returncode, 0, f"{sh} {script.name}: {r.stderr}")

    @unittest.skipUnless(SHELLS, "needs dash or bash")
    def test_generated_commands_really_run_under_dash_and_bash(self):
        root = self.project(NASTY)
        wrapper = self._python_wrapper(self.base / NASTY)
        self._install_all(root, wrapper)
        env = _env(self.home)
        by_event = {(h, e): c for h, e, c in self._hook_commands(root)}
        payloads = {
            ("claude", "UserPromptSubmit"): {"session_id": "s-q", "cwd": str(root), "hook_event_name": "UserPromptSubmit",
                                             "prompt": "quoting works"},
            ("claude", "PreToolUse"): {"session_id": "s-q", "cwd": str(root), "tool_name": "Bash",
                                       "tool_input": {"command": "ls"}},
            ("cursor", "stop"): {"conversation_id": "c-q", "status": "completed"},
            ("codex", "SessionStart"): {"session_id": "x-q", "cwd": str(root)},
        }
        for sh in SHELLS:
            for key, payload in payloads.items():
                cmd = by_event[key]
                for c in (cmd, cmd[: -len(HOOK_SUFFIX)]):       # also WITHOUT `|| true`: must really succeed
                    r = subprocess.run([sh, "-c", c], input=json.dumps(payload), env=env, capture_output=True,
                                       text=True, timeout=60)
                    self.assertEqual(r.returncode, 0, f"{sh} {key}: rc={r.returncode} {r.stderr[-500:]}")
            # the pre-commit script of this project (git runs it through sh)
            r = subprocess.run([sh, str(root / ".hearmemory/host/git/pre-commit")], cwd=str(root), env=env,
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, f"{sh} pre-commit: {r.stderr[-500:]}")
        obs = (root / ".hearmemory" / "observations.jsonl").read_text(encoding="utf-8")
        self.assertIn("quoting works", obs, "the UserPromptSubmit hook command never reached hearmemory")

    @unittest.skipUnless(SHELLS, "needs dash or bash")
    def test_launch_scripts_pass_the_exact_paths(self):
        root = self.project(NASTY)
        wrapper = self._python_wrapper(self.base / NASTY)
        self._install_all(root, wrapper)
        fake_bin = self.base / "fakebin"
        fake_bin.mkdir()
        for tool in ("codex", "claude"):
            f = fake_bin / tool
            f.write_text("#!%s\nimport json, os, sys\nopen(os.environ['FAKE_OUT'], 'w').write(json.dumps(sys.argv[1:]))\n"
                         % sys.executable, encoding="utf-8")
            f.chmod(0o755)
        for sh in SHELLS:
            env = _env(self.home)
            env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
            out = self.base / "argv.json"
            env["FAKE_OUT"] = str(out)
            r = subprocess.run([sh, str(root / ".hearmemory/host/codex/launch.sh"), "--extra"], env=env,
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            argv = json.loads(out.read_text())
            self.assertEqual(argv[-1], "--extra")
            values = {}
            for i, a in enumerate(argv):
                if a == "-c":
                    k, _, v = argv[i + 1].partition("=")
                    values[k] = tomllib.loads("v = " + v)["v"]
            self.assertEqual(values["mcp_servers.hearmemory.command"], wrapper)
            self.assertEqual(values["mcp_servers.hearmemory.args"],
                             ["-m", "hearmemory", "--project", str(root), "mcp", "--host", "codex"])
            self.assertTrue(values["mcp_servers.hearmemory.env"]["HEARMEMORY_SESSION_ID"].startswith("codex-"))
            r = subprocess.run([sh, str(root / ".hearmemory/host/claude/launch.sh")], env=env,
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(json.loads(out.read_text()),
                             ["--mcp-config", f"{root}/.hearmemory/host/claude/mcp.json",
                              "--settings", f"{root}/.hearmemory/host/claude/settings.json"])

    def test_toml_values_survive_quotes_and_backslashes(self):
        for py, root in (('/o"dd/py', "/p/a\\b"), ("/x/py", 'we"ird\\\\ $HOME `x`')):
            argv = S.codex_cmd_argv(py, root)
            vals = {a.partition("=")[0]: tomllib.loads("v = " + a.partition("=")[2])["v"]
                    for a in argv if a.startswith("mcp_servers.")}
            self.assertEqual(vals["mcp_servers.hearmemory.command"], py)
            self.assertEqual(vals["mcp_servers.hearmemory.args"][3], root)

    def test_control_characters_in_the_path_are_refused_by_init(self):
        root = self.base / "new\nline"
        root.mkdir()
        r = self.cli(root, "init", "--hosts", "claude")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("control characters", r.stderr)
        self.assertFalse((root / ".hearmemory").exists(), "init must refuse before creating anything")
        with self.assertRaises(S.UnsafePathError):
            host_install.install(self.project("ok"), ["claude"], _cfg(), python="/usr/bin/py\tthon")


# ============================================================================================ 3.2
class Hardening3_2OneCanonicalTestTarget(_Tmp):
    EQUIVALENT = [
        ("cd {root} && pytest tests/test_app.py", "pytest tests/test_app.py"),
        ("python -m pytest tests/test_app.py", "pytest tests/test_app.py"),
        ("pytest tests/test_app.py", ".venv/bin/pytest tests/test_app.py"),
        ("timeout 60 pytest tests/test_app.py", "pytest tests/test_app.py"),
        ("pytest tests/test_app.py 2>&1", "pytest tests/test_app.py"),
        ("pytest -q tests/test_app.py", "python3.11 -m pytest -x -v tests/test_app.py 2>&1 | tail -20"),
        ("FOO=1 uv run pytest ./tests/test_app.py", "bash -lc 'cd {root} && pytest tests/test_app.py'"),
    ]

    def test_equivalent_spellings_share_one_target(self):
        from hearmemory.testcmd import canonical_target, is_test_command, target_covers
        root = "/srv/proj"
        for a, b in self.EQUIVALENT:
            a, b = a.format(root=root), b.format(root=root)
            with self.subTest(a=a, b=b):
                ca, cb = canonical_target(a, root), canonical_target(b, root)
                self.assertEqual(ca, cb)
                self.assertEqual(canonical_target(ca), ca, "canonical form must be idempotent")
                self.assertTrue(is_test_command(a) and is_test_command(b))
        self.assertEqual(canonical_target("pytest /srv/proj/tests/test_app.py", root), "pytest tests/test_app.py")
        self.assertEqual(canonical_target("cd tests && pytest test_app.py"), "pytest tests/test_app.py")
        self.assertNotEqual(canonical_target("pytest tests/test_app.py"), canonical_target("pytest tests/test_other.py"))
        self.assertNotEqual(canonical_target("pytest -k slow tests"), canonical_target("pytest tests"))

    def test_codex_legacy_bash_lc_argv_is_unwrapped(self):
        from hearmemory.testcmd import canonical_target, is_test_command, target_covers
        self.assertEqual(codex._cmd_text({"command": ["bash", "-lc", "cd /srv/proj && pytest -q tests/test_app.py"]}),
                         "cd /srv/proj && pytest -q tests/test_app.py")
        self.assertEqual(canonical_target("bash -lc cd /srv/proj && pytest tests/test_app.py", "/srv/proj"),
                         "pytest tests/test_app.py")

    def test_wider_target_covers_narrower(self):
        from hearmemory.testcmd import canonical_target, is_test_command, target_covers
        self.assertTrue(target_covers("pytest tests/test_app.py", "pytest tests/test_app.py::test_f"))
        self.assertTrue(target_covers("pytest tests", "pytest tests/test_app.py::test_f"))
        self.assertTrue(target_covers("pytest", "pytest tests/test_app.py"))
        self.assertTrue(target_covers("pytest tests/test_app.py", "pytest -k slow tests/test_app.py"))
        self.assertFalse(target_covers("pytest tests/test_app.py::test_f", "pytest tests/test_app.py"))
        self.assertFalse(target_covers("pytest tests/test_other.py", "pytest tests/test_app.py"))
        self.assertFalse(target_covers("pytest -k slow tests", "pytest tests/test_app.py"))
        self.assertFalse(target_covers("npm test", "npm test -- foo"))

    def _record(self, root, event, command, output, n):
        payload = {"session_id": "s-%d" % n, "cwd": str(root), "tool_use_id": "tu-%d" % n, "tool_name": "Bash",
                   "tool_input": {"command": command}}
        if event == "PostToolUseFailure":
            payload["error"] = output
        else:
            payload["tool_response"] = {"stdout": output, "stderr": "", "interrupted": False}
        r = hooks.run_hook("claude", event, json.dumps(payload).encode(), root=str(root))
        self.assertEqual(r.exit_code, 0)

    def _commit_blocked(self, root, n):
        (root / "app.py").write_text("x = %d\n" % n, encoding="utf-8")
        (root / "tests" / "test_app.py").write_text("def test_f():\n    assert %d\n" % n, encoding="utf-8")
        _git(root, "add", "-A")
        r = subprocess.run(["git", "commit", "-q", "-m", "c%d" % n], cwd=str(root), env=_env(self.home),
                           capture_output=True, text=True, timeout=120)
        return r.returncode != 0, r.stdout + r.stderr

    def test_block_mode_commit_after_fail_then_pass_with_another_spelling(self):
        fail_out = ("Exit code 1\nFAILED tests/test_app.py::test_f - assert 0\n"
                    "========= 1 failed in 0.05s =========\n")
        pass_out = "========= 1 passed in 0.02s =========\n"
        cases = [(a, b, False) for a, b in self.EQUIVALENT[:6]] + [
            ("pytest tests/test_app.py::test_f", "pytest tests/test_app.py", False),
            ("pytest tests/test_app.py::test_f", "pytest", False),
            ("pytest tests/test_app.py", "pytest tests/test_other.py", True),   # really different: still blocked
        ]
        for i, (fail_cmd, pass_cmd, want_block) in enumerate(cases):
            with self.subTest(fail=fail_cmd, passed=pass_cmd):
                root = self.project("p%d" % i)
                (root / "tests").mkdir()
                self.assertEqual(self.cli(root, "init", "--hosts", "git").returncode, 0)
                _set_config(root, ('git_mode = "warn"', 'git_mode = "block"'))
                self._record(root, "PostToolUseFailure", fail_cmd.format(root=root), fail_out, 1)
                time.sleep(0.01)
                self._record(root, "PostToolUse", pass_cmd.format(root=root), pass_out, 2)
                blocked, out = self._commit_blocked(root, i)
                self.assertEqual(blocked, want_block, out)


# ============================================================================================ 3.3
SECRET_LINES = {
    "hf_AbCdEfGhIjKlMnOpQrStUvWxYz012345": "huggingface-cli login --token hf_AbCdEfGhIjKlMnOpQrStUvWxYz012345",
    "q8w7e6r5t4y3u2i1o0p9": "python train.py --api-key q8w7e6r5t4y3u2i1o0p9 --epochs 3",
    "Zx9TopSecretValue": "python serve.py --token=Zx9TopSecretValue",
    "Hunter2Docker": "docker login -u me -p Hunter2Docker registry.example.com",
    "dckr_pat_AbCdEfGhIjKlMnOpQrStUvWx": "docker login -u me -p dckr_pat_AbCdEfGhIjKlMnOpQrStUvWx",
    "Hunter2Sshpass": "sshpass -p Hunter2Sshpass ssh user@host",
    "Hunter2Mysql": "mysql -u root -pHunter2Mysql mydb",
    "dXNlcjpwYXNzd29yZA==": "curl -H 'Authorization: Basic dXNlcjpwYXNzd29yZA==' https://x",
    "horse battery staple": 'password="correct horse battery staple"',
    "glpat-AbCdEfGhIjKlMnOpQrSt": "export T=1; echo glpat-AbCdEfGhIjKlMnOpQrSt",
    "npm_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789": "echo npm_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
    "gsk_AbCdEfGhIjKlMnOpQrStUv": "echo gsk_AbCdEfGhIjKlMnOpQrStUv",
    "dckr_pat_ZyXwVuTsRqPoNmLkJiHgFeDc": "the token dckr_pat_ZyXwVuTsRqPoNmLkJiHgFeDc was pasted",
}


class Hardening3_3CliFlagSecretsAndBarePrefixes(_Tmp):
    def test_each_form_is_redacted(self):
        for secret, line in SECRET_LINES.items():
            with self.subTest(line=line):
                out, n = privacy.redact(line, environ={})
                self.assertNotIn(secret, out)
                self.assertGreaterEqual(n, 1)
        # values that only look like flags stay readable
        for keep in ("python train.py --max-tokens 1000", "mysql -u root -p mydb", "pytest --pass-through x",
                     "git commit -m 'pass the tokens'"):
            self.assertEqual(privacy.redact(keep, environ={})[0], keep)

    def test_store_path_redacts_text_and_command(self):
        root = self.project("store", git=False)
        cmd = "huggingface-cli login --token hf_AbCdEfGhIjKlMnOpQrStUvWxYz012345 && sshpass -p Hunter2Sshpass ssh h"
        payload = {"session_id": "s1", "cwd": str(root), "tool_use_id": "tu-1", "tool_name": "Bash",
                   "tool_input": {"command": cmd}, "tool_response": {"stdout": "Login successful\n", "stderr": ""}}
        hooks.run_hook("claude", "PostToolUse", json.dumps(payload).encode(), root=str(root))
        r = self.cli(root, "record", "--kind", "note", "hf token hf_BBBBBBBBBBBBBBBBBBBBBBBBBBBB and --api-key q8w7e6r5t4y3u2i1o0p9")
        self.assertEqual(r.returncode, 0, r.stderr)
        raw = (root / ".hearmemory" / "observations.jsonl").read_text(encoding="utf-8")
        for secret in ("hf_AbCdEfGhIjKlMnOpQrStUvWxYz012345", "Hunter2Sshpass", "hf_BBBBBBBBBBBBBBBBBBBBBBBBBBBB",
                       "q8w7e6r5t4y3u2i1o0p9"):
            self.assertNotIn(secret, raw)
        self.assertIn("--token [REDACTED", raw)

    def test_jev_send_path_redacts(self):
        from test_judge_jev import ENV, FakeBudget, FakeClient, answer
        from test_judge_support import Clock, FakeStore as JFakeStore, T0, default_cfg
        from hearmemory.judge import jev as J
        evidence = "\n".join(SECRET_LINES.values())
        state = {"target_claim": "login works", "target_scope": {"project": "p", "branch": "main", "commit": "abc1234",
                                                                  "paths": []},
                 "evidence": [{"text": evidence, "source": "command run by codex session at 2026-09-24T10:00Z"}]}
        ver = I.TEMPLATE_VERSIONS["B1"]
        h = I.input_hash("B1", ver, state)
        cand = I.Candidate(candidate_id=I.candidate_id_for("B1", ver, "claim:x", None, h), template_id="B1",
                           template_version=ver, subject_key="claim:x", state=state, input_hash=h, basis_obs_ids=[],
                           created_ts="2026-09-24T10:00:00.000000Z", meta={})
        (self.base / "jev").mkdir()
        store = JFakeStore.init(str(self.base / "jev"))
        client = FakeClient(lambda st, q: answer("supports"))
        j = J.JevJudge(default_cfg(), store=store, budget=FakeBudget(), client=client, environ=ENV, clock=Clock(T0))
        j.judge([cand], 10)
        self.assertEqual(len(client.calls), 1)
        sent = json.dumps(client.calls[0]["state"], ensure_ascii=False)
        for secret in SECRET_LINES:
            self.assertNotIn(secret, sent)


# ============================================================================================ 3.4
class Hardening3_4TaskToolResponseObject(_Tmp):
    def test_only_the_subagents_text_is_recorded(self):
        root = self.project("task", git=False)
        payload = json.loads((REPO / "tests/fixtures/claude_post_task_object.json").read_text(encoding="utf-8"))
        payload["cwd"] = str(root)
        r = hooks.run_hook("claude", "PostToolUse", json.dumps(payload).encode(), root=str(root))
        self.assertEqual(r.exit_code, 0)
        rows = [json.loads(ln) for ln in (root / ".hearmemory/observations.jsonl").read_text().splitlines() if ln.strip()]
        [obs] = [o for o in rows if o["kind"] == "subagent_result"]
        self.assertTrue(obs["text"].startswith("Root cause: app/cache.py sets TTL=0"))
        self.assertIn("\n", obs["text"], "real newlines, not a repr's literal \\n")
        for leaked in ("Hypothesis", "'status'", "totalTokens", "usage", "agentId", "toolStats"):
            self.assertNotIn(leaked, obs["text"])
        # the extractor sees the subagent's conclusion, never the parent's hypothesis
        self.assertEqual(self.cli(root, "worker", "--once", "--no-jev").returncode, 0)
        claims = [json.loads(ln)["text"] for ln in (root / ".hearmemory/claims.jsonl").read_text().splitlines()
                  if ln.strip()] if (root / ".hearmemory/claims.jsonl").exists() else []
        self.assertFalse(any("Hypothesis" in c or c.startswith("TTL is 0 and db.connect") for c in claims), claims)


# ============================================================================================ 3.5
class Hardening3_5UninstallKeepsUserAdditions(_Tmp):
    def _quiet(self, fn, *a, **k):
        with contextlib.redirect_stderr(io.StringIO()):
            return fn(*a, **k)

    def _archived(self, root, rel):
        return list((root / ".hearmemory" / "archive").glob("uninstall-*/" + rel))

    def test_cursor_files_hearmemory_created_keep_the_users_entries(self):
        root = self.project("cursor", git=False)
        self._quiet(host_install.install, root, ["cursor"], _cfg(), python=sys.executable)
        mcp_p, hooks_p = root / ".cursor/mcp.json", root / ".cursor/hooks.json"
        mcp = json.loads(mcp_p.read_text())
        mcp["mcpServers"]["github"] = {"command": "gh-mcp", "args": ["serve"]}
        mcp_p.write_text(json.dumps(mcp))
        hk = json.loads(hooks_p.read_text())
        hk["hooks"]["beforeReadFile"] = [{"command": "./my-audit.sh"}]
        hk["hooks"]["stop"].append({"command": "./my-stop.sh"})
        hooks_p.write_text(json.dumps(hk))

        self._quiet(host_install.uninstall, root)
        mcp = json.loads(mcp_p.read_text())
        self.assertEqual(mcp, {"mcpServers": {"github": {"command": "gh-mcp", "args": ["serve"]}}})
        hk = json.loads(hooks_p.read_text())
        self.assertEqual(hk["hooks"], {"beforeReadFile": [{"command": "./my-audit.sh"}],
                                       "stop": [{"command": "./my-stop.sh"}]})
        self.assertEqual(hk.get("version"), 1, "the user's hooks still need the version key")
        self.assertFalse((root / ".cursor/rules/hearmemory.mdc").exists())
        self.assertTrue(self._archived(root, ".cursor/mcp.json") and self._archived(root, ".cursor/hooks.json"))

    def test_claude_persist_files_keep_the_users_entries(self):
        root = self.project("claude", git=False)
        (root / ".claude").mkdir()
        (root / ".claude/settings.local.json").write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]}}))
        self._quiet(host_install.install, root, ["claude"], _cfg(), python=sys.executable, claude_persist=True)
        mp, sp = root / ".mcp.json", root / ".claude/settings.local.json"
        mcp = json.loads(mp.read_text())
        mcp["mcpServers"]["playwright"] = {"command": "npx", "args": ["@playwright/mcp"]}
        mp.write_text(json.dumps(mcp))
        st = json.loads(sp.read_text())
        st["hooks"]["Notification"] = [{"hooks": [{"type": "command", "command": "notify-send hi"}]}]
        sp.write_text(json.dumps(st))

        self._quiet(host_install.uninstall, root)
        self.assertEqual(json.loads(mp.read_text()),
                         {"mcpServers": {"playwright": {"command": "npx", "args": ["@playwright/mcp"]}}})
        st = json.loads(sp.read_text())
        self.assertEqual(st, {"permissions": {"allow": ["Bash(ls)"]},
                              "hooks": {"Notification": [{"hooks": [{"type": "command",
                                                                      "command": "notify-send hi"}]}]}})
        self.assertTrue(self._archived(root, ".mcp.json"))

    def test_untouched_created_files_are_removed_as_empty_skeletons(self):
        root = self.project("clean", git=False)
        self._quiet(host_install.install, root, ["cursor", "claude"], _cfg(), python=sys.executable, claude_persist=True)
        man = M.read_manifest(root)
        keys = {r.path: r.json_keys for r in man.records if r.action == "json_merged"}
        self.assertIn(["mcpServers", "hearmemory"], keys[".cursor/mcp.json"])
        self.assertIn(["hooks", "stop"], keys[".cursor/hooks.json"])
        self.assertNotIn(["hooks"], keys[".cursor/hooks.json"])
        self._quiet(host_install.uninstall, root)
        for rel in (".cursor/mcp.json", ".cursor/hooks.json", ".mcp.json", ".claude/settings.local.json"):
            self.assertFalse((root / rel).exists(), rel)
        self.assertFalse((root / ".cursor").exists())

    def test_legacy_manifest_with_top_level_keys_is_undone_safely(self):
        root = self.project("legacy", git=False)
        self._quiet(host_install.install, root, ["cursor"], _cfg(), python=sys.executable)
        man = M.read_manifest(root)
        for r in man.records:                          # what round-2 builds recorded
            if r.path == ".cursor/mcp.json":
                r.json_keys = [["mcpServers"]]
            elif r.path == ".cursor/hooks.json":
                r.json_keys = [["version"], ["hooks"]]
        M.write_manifest(root, man)
        mcp_p, hooks_p = root / ".cursor/mcp.json", root / ".cursor/hooks.json"
        mcp = json.loads(mcp_p.read_text())
        mcp["mcpServers"]["github"] = {"command": "gh-mcp"}
        mcp_p.write_text(json.dumps(mcp))
        hk = json.loads(hooks_p.read_text())
        hk["hooks"]["beforeReadFile"] = [{"command": "./my-audit.sh"}]
        hooks_p.write_text(json.dumps(hk))
        self._quiet(host_install.uninstall, root)
        self.assertEqual(json.loads(mcp_p.read_text()), {"mcpServers": {"github": {"command": "gh-mcp"}}})
        self.assertEqual(json.loads(hooks_p.read_text())["hooks"], {"beforeReadFile": [{"command": "./my-audit.sh"}]})


# ============================================================================================ 3.6
class Hardening3_6HostCmdPrintsOneCompleteCommand(_Tmp):
    def test_codex_and_claude_cmd(self):
        root = os.path.realpath(self.project("it's here"))   # macOS: /var is a symlink to /private/var
        self.assertEqual(self.cli(root, "init", "--hosts", "claude,codex").returncode, 0)
        r = self.cli(root, "host", "codex-cmd")
        self.assertEqual(r.returncode, 0, r.stderr)
        line = r.stdout.strip()
        self.assertEqual(len(line.splitlines()), 1, line)
        self.assertFalse(line.startswith("exec"), line)
        self.assertFalse(line.endswith("\\"), line)
        argv = shlex.split(line)
        self.assertEqual(argv, S.codex_cmd_argv(sys.executable, str(root)))
        vals = {a.partition("=")[0]: tomllib.loads("v = " + a.partition("=")[2])["v"]
                for a in argv if a.startswith("mcp_servers.")}
        self.assertEqual(vals["mcp_servers.hearmemory.args"], ["-m", "hearmemory", "--project", str(root), "mcp", "--host", "codex"])

        r = self.cli(root, "host", "claude-cmd")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(shlex.split(r.stdout.strip()), S.claude_cmd_argv(str(root)))

    @unittest.skipUnless(shutil.which("codex"), "codex CLI not installed")
    def test_real_codex_parses_the_printed_command(self):
        root = self.project("it's codex")
        self.assertEqual(self.cli(root, "init", "--hosts", "codex").returncode, 0)
        argv = shlex.split(self.cli(root, "host", "codex-cmd").stdout.strip())
        codex_home = self.base / "codex_home"
        codex_home.mkdir()
        env = _env(self.home)
        env["CODEX_HOME"] = str(codex_home)
        r = subprocess.run([argv[0], "mcp", "get", "hearmemory", "--json", *argv[1:]], env=env, capture_output=True,
                           text=True, timeout=60, stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(r.stdout)
        flat = json.dumps(doc)
        self.assertIn(json.dumps(str(root))[1:-1], flat)
        self.assertIn('"mcp"', flat)


# ============================================================================================ 3.7
class Hardening3_7RunningIsNeverPass(_Tmp):
    def _runs(self, observations):
        idx = RunIndex()
        for o in observations:
            rec = run_record(o, I.actor_key(o.provenance))
            if rec is not None:
                idx.add(rec)
        return idx

    def test_codex_process_running_then_write_stdin_exit(self):
        root = self.project("codex", git=False)
        home = self.base / "codex_home"
        d = home / "sessions" / "2026" / "09" / "24"
        d.mkdir(parents=True)

        def line(ts, typ, payload):
            return json.dumps({"timestamp": ts, "type": typ, "payload": payload})

        rows = [
            line("2026-09-24T11:00:00Z", "session_meta", {"id": "sess-run", "cwd": str(root)}),
            line("2026-09-24T11:00:01Z", "response_item", {"type": "function_call", "name": "exec_command",
                 "call_id": "c1", "arguments": json.dumps({"cmd": "python -m pytest -q tests/test_app.py"})}),
            line("2026-09-24T11:00:11Z", "response_item", {"type": "function_call_output", "call_id": "c1",
                 "output": "Chunk ID: ab12\nWall time: 10.0 seconds\nProcess running with session ID 4867\n"
                           "Original token count: 20\nOutput:\n============ test session starts ============\n"
                           "collected 1 item\n"}),
            line("2026-09-24T11:00:20Z", "response_item", {"type": "function_call", "name": "write_stdin",
                 "call_id": "c2", "arguments": json.dumps({"session_id": 4867, "chars": "", "yield_time_ms": 30000})}),
            line("2026-09-24T11:00:31Z", "response_item", {"type": "function_call_output", "call_id": "c2",
                 "output": "Chunk ID: cd34\nWall time: 11.0 seconds\nProcess exited with code 1\n"
                           "Original token count: 30\nOutput:\nFAILED tests/test_app.py::test_f - assert 0\n"
                           "============ 1 failed in 12.3s ============\n"}),
            # a second long run that never reports back
            line("2026-09-24T11:01:00Z", "response_item", {"type": "function_call", "name": "exec_command",
                 "call_id": "c3", "arguments": json.dumps({"cmd": "pytest tests/test_other.py"})}),
            line("2026-09-24T11:01:10Z", "response_item", {"type": "function_call_output", "call_id": "c3",
                 "output": "Chunk ID: ef56\nWall time: 10.0 seconds\nProcess running with session ID 99\nOutput:\n"}),
        ]
        (d / "rollout-2026-09-24T11-00-00-sess-run.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")
        store = FakeStore(root)
        codex.import_rollouts(store, _cfg(), codex_home=str(home))
        obs = [o for _, o in store.iter_observations(0)]
        idx = self._runs(obs)
        latest = idx.latest("pytest tests/test_app.py")
        self.assertIsNotNone(latest)
        self.assertEqual(latest["outcome"], "fail")
        self.assertIsNone(idx.latest("pytest tests/test_other.py"), "a run that never finished is not a result")
        running = [o for o in obs if o.tool is not None and o.tool.status == "running"]
        self.assertEqual(len(running), 2)
        self.assertTrue(all(o.tool.exit_code is None for o in running))

    def _claude(self, root, n, event, command, **extra):
        payload = {"session_id": "s1", "cwd": str(root), "tool_use_id": "tu-%d" % n, "tool_name": "Bash",
                   "tool_input": {"command": command, **extra.pop("tool_input_extra", {})}, **extra}
        payload["_root"] = root
        payload["_cfg"] = _cfg()
        return claude.normalize(event, payload)

    def test_claude_background_and_empty_output_never_pass(self):
        root = self.project("claude", git=False)
        obs = []
        obs += self._claude(root, 1, "PostToolUseFailure", "pytest tests/test_app.py",
                            error="Exit code 1\n1 failed in 0.1s")
        time.sleep(0.002)
        obs += self._claude(root, 2, "PostToolUse", "pytest tests/test_app.py",
                            tool_input_extra={"run_in_background": True},
                            tool_response={"stdout": "Command running in background with ID: bash_1", "stderr": ""})
        time.sleep(0.002)
        obs += self._claude(root, 3, "PostToolUse", "pytest tests/test_app.py",
                            tool_response={"stdout": "", "stderr": "", "backgroundTaskId": "bash_2"})
        time.sleep(0.002)
        obs += self._claude(root, 4, "PostToolUse", "pytest tests/test_app.py",
                            tool_response={"stdout": "", "stderr": "", "interrupted": False})
        self.assertEqual([o.tool.status for o in obs], ["error", "running", "running", "unknown"])
        idx = self._runs(obs)
        self.assertEqual(idx.latest("pytest tests/test_app.py")["outcome"], "fail")
        # a foreground PostToolUse with output is a success (non-zero exits arrive as PostToolUseFailure)
        time.sleep(0.002)
        ok = self._claude(root, 5, "PostToolUse", "pytest tests/test_app.py",
                          tool_response={"stdout": "all good\n", "stderr": ""})
        self.assertEqual(ok[0].tool.exit_code, 0)
        idx.add(run_record(ok[0], "claude:s1:main"))
        self.assertEqual(idx.latest("pytest tests/test_app.py")["outcome"], "pass")


# ============================================================================= worker cleanup race
class WorkerSpawnedButNotYetLockedIsStopped(_Tmp):
    def test_stop_worker_finds_a_just_spawned_worker(self):
        from hearmemory.judge import worker as W
        root = self.project("race", git=False)
        real = os.path.realpath(str(root))
        _set_config(root, ("spawn_from_hooks = false", "spawn_from_hooks = true"))
        procs = []

        def fake_popen(argv):
            # a stand-in worker that has not taken the worker lock yet (still starting up)
            p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "-m", "hearmemory", "--project",
                                  real, "worker", "--daemon"], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            procs.append(p)
            return p

        try:
            self.assertTrue(W.spawn_background(root, launched_by="test", popen=fake_popen, environ={}))
            self.assertEqual(len(procs), 1)
            self.assertIsNone(procs[0].poll())
            self.assertTrue(W.stop_worker(root, 3.0))
            procs[0].wait(timeout=5)
            self.assertIsNotNone(procs[0].returncode, "stop_worker left the just-spawned worker running")
        finally:
            for p in procs:
                if p.poll() is None:
                    p.kill()
                    p.wait()


if __name__ == "__main__":
    unittest.main()
