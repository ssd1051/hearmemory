"""Offline end-to-end tests that wire ALL modules together through the REAL
CLI / MCP server / git hooks / host hook adapters (no fakes, no mocks) in a throwaway git project.

Most other tests/test_<area>_*.py files exercise one area against fakes for its siblings. This file is the one
place that never fakes anything - it is exactly the check those per-area fakes cannot do, and it
is how several real cross-module bugs were found while wiring the modules together:
`hearmemory init` not actually creating `.hearmemory`, every host-generated command putting `--project` after
the subcommand instead of before it, Claude-hook observations never carrying git_commit (so the B1
"unchanged scope" rule could never fire on real Claude Code output), and `hearmemory status`/`doctor`
crashing or staying silent on real (non-fake) Candidate/Store data.

No network, no Jev key needed: everything here runs with TYPESAFE_API_KEY unset (`--no-jev` /
default no-key degradation).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
PYTHON = sys.executable

sys.path.insert(0, str(SRC))

import hearmemory.interfaces as I  # noqa: E402
from hearmemory.host.hooks import run_hook  # noqa: E402


# ---------------------------------------------------------------------------
# Small helpers: a throwaway git project + real subprocess CLI calls.
# ---------------------------------------------------------------------------
def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True, text=True)


def make_git_project(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "e2e@example.com")
    _git(root, "config", "user.name", "e2e")
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "init")
    return root


def hearmemory(root: Path, *args: str, env: Optional[Dict[str, str]] = None,
         input_text: Optional[str] = None) -> subprocess.CompletedProcess:
    """A real `python -m hearmemory --project <root> <args>` subprocess call (the ONLY correct place
    for --project: it is a top-level option, parsed before the subcommand - see cli.build_parser)."""
    full_env = dict(os.environ)
    full_env["PYTHONPATH"] = str(SRC)
    full_env.pop("TYPESAFE_API_KEY", None)  # offline: never touch the network from this file
    if env:
        full_env.update(env)
    return subprocess.run([PYTHON, "-m", "hearmemory", "--project", str(root), *args], cwd=str(root),
                          env=full_env, capture_output=True, text=True, timeout=30, input=input_text)


def hook(host: str, event: str, payload: Dict[str, Any], root: Path) -> "I.HookResult":
    payload = dict(payload)
    payload.setdefault("cwd", str(root))
    return run_hook(host, event, json.dumps(payload).encode("utf-8"), root=str(root))


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


class E2ECase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_git_project(Path(self._tmp.name) / "proj")
        self.addCleanup(self._tmp.cleanup)
        # Hooks spawn the real background worker; stop it BEFORE the temp dir is removed (cleanups
        # run last-in-first-out), or it can still be writing .hearmemory/ while rmtree runs.
        self.addCleanup(self._stop_worker)

    def _stop_worker(self) -> None:
        try:
            from hearmemory.judge.worker import stop_worker
            if (self.root / ".hearmemory").exists():
                stop_worker(self.root, 5.0)
        except Exception:
            pass

    def init(self, hosts: str = "claude,codex,git") -> None:
        r = hearmemory(self.root, "init", "--hosts", hosts)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


# ---------------------------------------------------------------------------
# `hearmemory init` really creates a working store (regression: open_store(create=True) used to return
# a bare, still-uninitialised Store; hearmemory init never actually created .hearmemory/VERSION).
# ---------------------------------------------------------------------------
class InitReallyWorks(E2ECase):
    def test_init_record_status_roundtrip(self) -> None:
        self.init(hosts="git")
        self.assertTrue((self.root / ".hearmemory" / "VERSION").is_file())
        self.assertTrue((self.root / ".hearmemory" / "config.toml").is_file())

        r = hearmemory(self.root, "record", "a smoke-test note")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout.strip(), r"^hearmemory: recorded o-[0-9a-f]{16}$")

        r = hearmemory(self.root, "status", "--verbose")
        self.assertEqual(r.returncode, 0, r.stderr)

        r = hearmemory(self.root, "--json", "status")
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(r.stdout)
        self.assertEqual(doc["counts"]["observations"], 1)
        self.assertEqual(doc["jev"]["reason"], "no_key")


# ---------------------------------------------------------------------------
# (a) Parallel Claude subagents get a SHARED memory: concurrent hook writes from 3 distinct
# subagents (real threads hitting the real file lock), then one recall sees all of them.
# ---------------------------------------------------------------------------
class ParallelSubagents(E2ECase):
    def test_concurrent_subagent_writes_are_all_recorded_and_visible(self) -> None:
        self.init(hosts="claude")
        agents = ["agent-A", "agent-B", "agent-C"]
        n_per_agent = 5
        results: List[int] = []
        lock = threading.Lock()

        def write(agent_id: str, i: int) -> None:
            payload = {
                "session_id": "s-parallel", "agent_id": agent_id, "cwd": str(self.root),
                "hook_event_name": "PostToolUse", "tool_name": "Bash",
                "tool_input": {"command": f"grep -rn TODO module_{agent_id}.py"},
                "tool_response": {"stdout": f"finding by {agent_id} #{i}: possible bug\n",
                                  "stderr": "", "exit_code": 0},
                "tool_use_id": f"tu-{agent_id}-{i}",
            }
            r = hook("claude", "PostToolUse", payload, self.root)
            with lock:
                results.append(r.exit_code)

        threads = [threading.Thread(target=write, args=(a, i)) for a in agents for i in range(n_per_agent)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertTrue(all(rc == 0 for rc in results))
        self.assertEqual(len(results), len(agents) * n_per_agent)

        obs = read_jsonl(self.root / ".hearmemory" / "observations.jsonl")
        self.assertEqual(len(obs), len(agents) * n_per_agent, "no line lost/corrupted under concurrency")
        self.assertEqual(len({o["id"] for o in obs}), len(obs), "every id unique despite concurrent appends")
        seen_agents = {o["provenance"]["subagent_id"] for o in obs}
        self.assertEqual(seen_agents, set(agents))

        r = hearmemory(self.root, "worker", "--once", "--no-jev")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = hearmemory(self.root, "recall", "bug", "--limit", "20")
        self.assertEqual(r.returncode, 0, r.stderr)
        for a in agents:
            self.assertIn(a, r.stdout, f"{a}'s finding must be visible to (any) other subagent")


# ---------------------------------------------------------------------------
# (b) Codex works first (fake rollout import), Claude opens next and gets Codex's findings, with
# provenance, in its SessionStart brief.
# ---------------------------------------------------------------------------
class CodexThenClaude(E2ECase):
    def test_session_start_brief_carries_codex_findings_with_provenance(self) -> None:
        self.init(hosts="claude,codex")
        codex_home = Path(self._tmp.name) / "fake_codex_home"
        sessions_dir = codex_home / "sessions" / "2026" / "09" / "24"
        sessions_dir.mkdir(parents=True)
        template = (REPO / "tests" / "fixtures" / "codex_rollout_template.jsonl").read_text(encoding="utf-8")
        (sessions_dir / "rollout-2026-09-24T11-00-00-sess-abc.jsonl").write_text(
            template.replace("{CWD}", str(self.root)), encoding="utf-8")

        r = hearmemory(self.root, "import", "codex", "--codex-home", str(codex_home))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("imported", r.stdout)

        obs = read_jsonl(self.root / ".hearmemory" / "observations.jsonl")
        self.assertTrue(obs, "codex import produced no observations")
        self.assertTrue(all(o["provenance"]["host"] == "codex" for o in obs))
        self.assertTrue(any(o["provenance"]["session_id"] == "sess-abc" for o in obs))
        # the CLI/MCP `hearmemory record` calls inside the rollout must become provenance links, not
        # their own observations - only exec_command / apply_patch / agent turns remain
        self.assertFalse(any("hearmemory record" in o["text"] for o in obs))

        r = hearmemory(self.root, "worker", "--once", "--no-jev")
        self.assertEqual(r.returncode, 0, r.stderr)

        result = hook("claude", "SessionStart",
                      {"session_id": "claude-s1", "hook_event_name": "SessionStart", "source": "startup"},
                      self.root)
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(result.stdout, "SessionStart must push a brief via additionalContext")
        payload = json.loads(result.stdout)
        brief_text = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIn("codex", brief_text)
        self.assertIn("sess-abc", brief_text)

        # a Claude subagent starting now gets the ~300-token subagent brief too,
        # so parallel subagents share Codex's finding through the real memory stack.
        result = hook("claude", "SubagentStart",
                      {"session_id": "claude-s1", "hook_event_name": "SubagentStart",
                       "agent_id": "sub-1", "agent_type": "general-purpose"}, self.root)
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(result.stdout, "SubagentStart must push the subagent brief")
        sub = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(sub["hookEventName"], "SubagentStart")
        self.assertIn("sess-abc", sub["additionalContext"])

        # the brief is token-budgeted and may prioritise the failing-test item over the
        # claim; the full recall (no budget) must still carry Codex's actual finding, verbatim.
        r = hearmemory(self.root, "recall", "stale cache")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("stale cache", r.stdout)
        self.assertIn("sess-abc", r.stdout)


# ---------------------------------------------------------------------------
# (c) A planted refuted claim is caught by `hearmemory check` (warn) and blocked by the real git
# pre-commit hook (block mode) - the whole point of the tool.
# ---------------------------------------------------------------------------
class RefutedClaimBlocked(E2ECase):
    def _plant_refuted_claim(self) -> None:
        (self.root / "tests").mkdir(exist_ok=True)
        (self.root / "tests" / "test_sync.py").write_text("def test_sync():\n    assert True\n",
                                                           encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "add test_sync")
        self.init(hosts="git")

        # 1) a passing run, from the same actor as the claim that follows it
        ok = hook("claude", "PostToolUse", {
            "session_id": "s1", "hook_event_name": "PostToolUse", "tool_name": "Bash",
            "tool_input": {"command": "pytest tests/test_sync.py"},
            "tool_response": {"stdout": "1 passed in 0.05s\n", "stderr": "", "exit_code": 0},
            "tool_use_id": "tu-1"}, self.root)
        self.assertEqual(ok.exit_code, 0)

        # 2) the (false) claim that it passes
        r = hearmemory(self.root, "record",
                  "`pytest tests/test_sync.py` now passes after the fix.", "--kind", "claim",
                  "--session", "s1")
        self.assertEqual(r.returncode, 0, r.stderr)

        # 3) a LATER run, same commit, no edits in between, different actor: it actually fails
        fail = hook("claude", "PostToolUseFailure", {
            "session_id": "s2", "hook_event_name": "PostToolUseFailure", "tool_name": "Bash",
            "tool_input": {"command": "pytest tests/test_sync.py"}, "tool_use_id": "tu-2",
            "error": "Exit code 1\nFAILED tests/test_sync.py::test_sync\n1 failed, 0 passed in 0.10s\n",
        }, self.root)
        self.assertEqual(fail.exit_code, 0)

        r = hearmemory(self.root, "worker", "--once", "--no-jev")
        self.assertEqual(r.returncode, 0, r.stderr)
        judgments = read_jsonl(self.root / ".hearmemory" / "judgments.jsonl")
        self.assertTrue(any(j["label"] == "refutes" and j["rule_id"] == "B1_test_status"
                            for j in judgments), judgments)

    def test_check_warns_by_default(self) -> None:
        self._plant_refuted_claim()
        r = hearmemory(self.root, "check", "--text",
                  "Since `pytest tests/test_sync.py` now passes after the fix, shipping this.")
        self.assertEqual(r.returncode, 0, r.stderr)  # warn mode: never blocks
        self.assertIn("REFUTED", r.stdout)
        self.assertIn("Warning only", r.stdout)

    def test_git_hook_blocks_in_block_mode(self) -> None:
        self._plant_refuted_claim()
        cfg_path = self.root / ".hearmemory" / "config.toml"
        cfg_path.write_text(cfg_path.read_text(encoding="utf-8").replace(
            'git_mode = "warn"', 'git_mode = "block"'), encoding="utf-8")

        (self.root / "sync.py").write_text("# relies on `pytest tests/test_sync.py` passing\n",
                                           encoding="utf-8")
        _git(self.root, "add", "-A")
        before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(self.root),
                                capture_output=True, text=True).stdout.strip()
        commit = subprocess.run(["git", "commit", "-m", "ship it"], cwd=str(self.root),
                                capture_output=True, text=True,
                                env={**os.environ, "GIT_EDITOR": "true"})
        self.assertNotEqual(commit.returncode, 0, commit.stdout + commit.stderr)
        after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(self.root),
                               capture_output=True, text=True).stdout.strip()
        self.assertEqual(before, after, "a blocked commit must not land")
        self.assertIn("REFUTED", commit.stdout + commit.stderr)


# ---------------------------------------------------------------------------
# (d) MCP server: real stdio JSON-RPC subprocess exchange (initialize, tools/list, two tools/call,
# an unknown method).
# ---------------------------------------------------------------------------
class McpServerExchange(E2ECase):
    def test_full_protocol_roundtrip(self) -> None:
        self.init(hosts="claude")
        hearmemory(self.root, "record", "sync.py has a race condition", "--kind", "claim")

        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC)
        env.pop("TYPESAFE_API_KEY", None)
        proc = subprocess.Popen([PYTHON, "-m", "hearmemory", "--project", str(self.root), "mcp", "--host", "claude"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, env=env)
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                        "clientInfo": {"name": "test", "version": "0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "hearmemory_recall", "arguments": {"query": "sync.py"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"name": "hearmemory_record", "arguments": {"text": "mcp e2e note", "kind": "note"}}},
            {"jsonrpc": "2.0", "id": 5, "method": "not/a/real/method"},
        ]
        data = "".join(json.dumps(r) + "\n" for r in requests)
        try:
            out, err = proc.communicate(data, timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise

        lines = [json.loads(ln) for ln in out.splitlines() if ln.strip()]
        by_id = {m["id"]: m for m in lines if "id" in m}
        self.assertEqual(by_id[1]["result"]["protocolVersion"], "2024-11-05")
        tool_names = {t["name"] for t in by_id[2]["result"]["tools"]}
        self.assertEqual(tool_names, {"hearmemory_recall", "hearmemory_record", "hearmemory_check", "hearmemory_issues",
                                      "hearmemory_status"})
        self.assertIn("sync.py", by_id[3]["result"]["content"][0]["text"])
        self.assertRegex(by_id[4]["result"]["content"][0]["text"], r"hearmemory: recorded o-[0-9a-f]{16}")
        self.assertEqual(by_id[5]["error"]["code"], -32601)
        # stdout carries protocol messages only.
        for ln in out.splitlines():
            if ln.strip():
                json.loads(ln)


# ---------------------------------------------------------------------------
# (e) Robustness: no key (default here), deleted .hearmemory mid-session, corrupted raw line.
# ---------------------------------------------------------------------------
class Robustness(E2ECase):
    def test_no_key_degrades_to_rules(self) -> None:
        self.init(hosts="git")
        r = hearmemory(self.root, "--json", "status")
        doc = json.loads(r.stdout)
        self.assertFalse(doc["jev"]["capable"])
        self.assertEqual(doc["jev"]["reason"], "no_key")

    def test_sandboxed_no_network_degrades_even_with_a_key_present(self) -> None:
        """Item 2 of the revision: a key alone is not enough - a process that Codex launched
        inside a no-network sandbox (`CODEX_SANDBOX_NETWORK_DISABLED=1`) must degrade to rules too,
        and this must stay a PROCESS-LOCAL fact (never written to the shared jev_health.json, never
        able to turn Jev off project-wide for a different, networked process)."""
        self.init(hosts="git")
        r = hearmemory(self.root, "--json", "status",
                 env={"TYPESAFE_API_KEY": "fake-key-never-used", "CODEX_SANDBOX_NETWORK_DISABLED": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(r.stdout)
        self.assertFalse(doc["jev"]["capable"])
        self.assertEqual(doc["jev"]["reason"], "sandbox_no_network")
        self.assertFalse((self.root / ".hearmemory" / "state" / "jev_health.json").exists(),
                         "a process-local no-network reason must never be written to shared state")
        # worker must still complete a full pipeline pass without ever attempting a network call
        r = hearmemory(self.root, "worker", "--once",
                 env={"TYPESAFE_API_KEY": "fake-key-never-used", "CODEX_SANDBOX_NETWORK_DISABLED": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_hook_silent_and_no_recreate_after_hearmemory_deleted(self) -> None:
        self.init(hosts="claude")
        import shutil
        shutil.rmtree(self.root / ".hearmemory")
        result = hook("claude", "PostToolUseFailure", {
            "session_id": "s1", "hook_event_name": "PostToolUseFailure", "tool_name": "Bash",
            "tool_input": {"command": "pytest"}, "tool_use_id": "tu-x", "error": "boom"}, self.root)
        self.assertEqual(result.exit_code, 0)
        self.assertFalse((self.root / ".hearmemory").exists(), "a hook must NEVER recreate .hearmemory")
        r = hearmemory(self.root, "record", "should fail cleanly")
        self.assertEqual(r.returncode, I.EXIT_NOT_INITIALISED)

    def test_corrupted_line_does_not_crash_and_is_reported(self) -> None:
        self.init(hosts="git")
        hearmemory(self.root, "record", "a real note before corruption")
        obs_path = self.root / ".hearmemory" / "observations.jsonl"
        with obs_path.open("a", encoding="utf-8") as f:
            f.write("{not valid json\n")

        r = hearmemory(self.root, "status")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = hearmemory(self.root, "recall", "note")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = hearmemory(self.root, "doctor")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("corrupt", r.stdout)


if __name__ == "__main__":
    unittest.main()
