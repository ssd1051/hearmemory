"""Replay 4: regression tests for the problems seen in a real run (Codex -> Claude -> Codex, 2026-09-25).

What happened for real: after a fresh `hearmemory init` at cf15004, Codex session A (code mode) added `mul(a, b)` to
src/calc.py plus `test_mul`, ran `python -m pytest -q && git diff ... && git status --short` (7 passed, dirty tree),
then in ONE command ran `hearmemory record --kind claim "新增 mul(a, b) 返回 a * b，并为其添加测试；python -m pytest -q 已通过
7 个测试。" && git add src tests && hearmemory check --staged && git status --short && git commit ...` (the sandbox refused
the .git write), re-ran the add / check / commit escalated -> 578e1b9. (Codex had been started from a Claude Code
terminal: the record was stored as a "claude" CLI record.) A Claude desktop session (worktree; two subagents)
ran pytest (7 passed at 578e1b9, recorded) and recorded the finding "tests/test_calc.py mul coverage insufficient:
... Missing: one negative, both negative, zero, floats ..." (B1 supports, confidence 0.50). Codex session B read
the brief, added the edge tests, ran pytest (11 passed), committed 387134d and `git commit --amend --no-edit`
twice -> 1dcbbe0. What went wrong:
  1. both of A's commit commands mention hearmemory, so neither became an observation: 578e1b9 was never seen and the
     dirty 7-passed run could not be linked to it;
  2. the B1 evidence of that combined run kept only its tail (from "b):" mid-diff): "7 passed" never reached Jev;
  3. A's change claim was insufficient twice (evidence: runs only) and never reached the Claude session's brief
     ("claude:?" record = "maybe this very session") -- Claude learned "tests passed", not "mul was added";
  4. after B addressed the coverage finding (edit + 11 passed + commit), the next brief still listed it first as
     a new [SUPPORTED] finding;
  5. B's run / claim were labelled with the pre-commit HEAD; B's two amends were not followed to 1dcbbe0;
  6. the 0.50-confidence "supports" of the coverage finding was shown as [SUPPORTED].

The fixtures tests/fixtures/replay_4_* are the relevant real rows, sanitised (project path -> {ROOT}; no user
prompt / instructions / skills / reasoning; A's first snippet, unrelated to the task, is left out). The scripted Jev gives the REAL answers, except that A's claim is supported once its own edit
of src/calc.py and its commit are in the evidence.
"""
from __future__ import annotations

import calendar
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from hearmemory import interfaces as I  # noqa: E402
from hearmemory.config import load_config, set_config_value  # noqa: E402
from hearmemory.store import create_store  # noqa: E402

FIX = REPO / "tests" / "fixtures"
SID_A = "01a0d441-cf0b-7a73-b989-4772bd0cf3be"
SID_B = "01a0d44b-73c2-7ee1-b103-0711d876ba62"
SID_CLAUDE = "3a7034e9-c24b-4165-85d2-78fc938786b4"
A_CLAIM_OBS, B_CLAIM_OBS = "o-937776aee3ecac29", "o-1c3f9be9d0b587ca"
CLAUDE_RUN, CLAUDE_CLAIM_OBS, FINDING_OBS = "o-e6eaa036634361eb", "o-e2332bf398307f58", "o-e851502939344f70"
PHASE = {1: {"obs": [A_CLAIM_OBS], "events": ["ev-alias-8d740dcb2804f504"]},
         2: {"obs": [CLAUDE_RUN, "o-0472aebbd1e4fa3d", CLAUDE_CLAIM_OBS, FINDING_OBS],
             "events": ["ev-link-69adf4e37ddfd9bc", "ev-link-ec9d9c16b7635507"]},
         3: {"obs": [B_CLAIM_OBS], "events": ["ev-alias-6c616cce73c78d7d"]}}
ROLLOUT = {"a": f"rollout-2026-09-25T00-31-17-{SID_A}.jsonl", "b": f"rollout-2026-09-25T00-41-49-{SID_B}.jsonl"}


def _ans(choice: str, conf: float, **probs: float) -> Dict[str, Any]:
    return {"type": "choice", "choice": choice, "confidence": conf, "probabilities": probs}


REAL = {"新增 mul": _ans("insufficient", 0.62, supports=0.25, refutes=0.02, insufficient=0.71, both=0.02),
        "python -m pytest -q at HEAD": _ans("supports", 0.84, refutes=0.0, insufficient=0.12, supports=0.88, both=0.0),
        "mul coverage insufficient": _ans("supports", 0.5, supports=0.63, insufficient=0.35, refutes=0.0, both=0.02),
        "补充 mul": _ans("supports", 0.9, both=0.01, insufficient=0.06, supports=0.92, refutes=0.01)}
SUPPORTS = _ans("supports", 0.9, supports=0.9, refutes=0.02, both=0.0, insufficient=0.08)

CALC = {0: "def add(a, b):\n    return a + b\n\n\ndef div(a, b):\n    if b == 0:\n        raise ValueError(\"cannot divide by "
           "zero\")\n    return a / b\n"}
CALC[1] = CALC[0].replace("\n\n\ndef div", "\n\n\ndef mul(a, b):\n    return a * b\n\n\ndef div")
TESTS = {0: "import pytest\n\nfrom src.calc import add, div\n\n\ndef test_add():\n    assert add(2, 3) == 5\n\n\n"
            "def test_add_with_zero_and_negative_number():\n    assert add(0, -4) == -4\n\n\ndef test_div():\n"
            "    assert div(6, 3) == 2\n\n\ndef test_div_non_exact_values():\n    assert div(5, 2) == 2.5\n\n\n"
            "def test_div_with_negative_values():\n    assert div(-6, 3) == -2\n\n\ndef test_div_by_zero():\n"
            "    with pytest.raises(ValueError):\n        div(1, 0)\n"}
TESTS[1] = TESTS[0].replace("import add, div\n", "import add, div, mul\n").replace(
    "\n\n\ndef test_div():", "\n\n\ndef test_mul():\n    assert mul(2, 3) == 6\n\n\ndef test_div():")
TESTS[2] = TESTS[1].replace("== 6\n\n\n", "== 6\n\n\n@pytest.mark.parametrize(\n    (\"left\", \"right\", \"expected\"),\n"
                            "    [(-2, 3, -6), (-2, -3, 6), (0, 7, 0), (2.5, 4, 10.0)],\n)\n"
                            "def test_mul_with_additional_values(left, right, expected):\n"
                            "    assert mul(left, right) == expected\n\n\n")


def epoch(ts: str) -> float:
    return float(calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%S")))


def _git(root: Path, *args: str, env: Dict[str, str] = None) -> None:
    subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True, text=True,
                   env=dict(os.environ, **(env or {})))


def write_project(root: Path, version: int) -> None:
    (root / "src" / "calc.py").write_text(CALC[min(version, 1)], encoding="utf-8")
    (root / "tests" / "test_calc.py").write_text(TESTS[version], encoding="utf-8")


def make_calc_project(root: Path) -> Path:
    """hello-hearmemory at cf15004 (add, div + tests), committed before the scenario."""
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    write_project(root, 0)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "add", "-A")
    when = "2026-09-24T16:00:00Z"
    _git(root, "commit", "-q", "-m", "init", env={"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when})
    return root.resolve()


def fixture_text(name: str, root: Path) -> str:
    return (FIX / name).read_text(encoding="utf-8").replace("{ROOT}", str(root))


def write_rollout(home: Path, which: str, root: Path) -> Path:
    day = home / "sessions" / "2026" / "09" / "25"
    day.mkdir(parents=True, exist_ok=True)
    path = day / ROLLOUT[which]
    path.write_text(fixture_text(f"replay_4_codex_{which}.jsonl", root), encoding="utf-8")
    return path


def append_phase(root: Path, phase: int) -> None:
    """The rows hearmemory wrote live in that phase (CLI / MCP records, Claude hooks, alias / link events)."""
    for fname, ids, key in (("replay_4_obs.jsonl", PHASE[phase]["obs"], "observations.jsonl"),
                            ("replay_4_events.jsonl", PHASE[phase]["events"], "events.jsonl")):
        rows = [ln for ln in fixture_text(fname, root).splitlines() if json.loads(ln)["id"] in ids]
        with open(root / ".hearmemory" / key, "a", encoding="utf-8") as fh:
            fh.write("\n".join(rows) + "\n")


class ScriptedJev:
    """B1: the REAL answer per claim, except that A's claim is supported when its own src/calc.py edit adding `mul`
    AND a `git commit` are in the evidence (unless never_support); A1/A2/A3: "not the same"."""

    def __init__(self, never_support: bool = False) -> None:
        self.states: List[Dict[str, Any]] = []
        self.never_support = never_support

    def system_one(self, state, questions, timeout=None):
        q = questions[I.QUESTION_KEY]
        labels = list((q.get("criteria") if isinstance(q, dict) else getattr(q, "criteria", None)) or {})
        self.states.append(json.loads(json.dumps(state)))
        if "supports" in labels:
            key = [k for k in REAL if k in state["target_claim"]]
            ans = dict(REAL[key[0]]) if key else dict(REAL["新增 mul"])
            if key == ["新增 mul"] and not self.never_support:
                ev = state.get("evidence") or []
                edit = any(e["text"].startswith("edit src/calc.py") and "mul" in e["text"] for e in ev)
                commit = any("git commit" in e["text"] for e in ev)
                if edit and commit:
                    ans = dict(SUPPORTS)
        else:
            ans = {"type": "choice", "confidence": 0.5,
                   "choice": [x for x in labels if x in ("not_contained", "different", "different_events")][0]}
        return {"answers": {I.QUESTION_KEY: ans}, "model": I.JEV_MODEL_DEFAULT,
                "usage": {"input_tokens": 700, "output_tokens": 50}}


def brief_for(state, store, cfg, host: str, session: str, now: str):
    from hearmemory.memory.brief import build_brief
    req = I.BriefRequest(context=I.AgentContext(host=host, session_id=session), purpose="session_start",
                         max_tokens=600, lang="en")
    return build_brief(state, store, req, cfg, now=now)


class _Scenario(unittest.TestCase):
    jev_kwargs: Dict[str, Any] = {}
    phases = (1, 2, 3)
    config: Dict[str, Any] = {}

    @classmethod
    def setUpClass(cls) -> None:
        from hearmemory.judge.jev import JevJudge
        from hearmemory.judge.worker import run_pipeline
        from hearmemory.memory.build import load_or_rebuild
        cls._tmp = tempfile.TemporaryDirectory()
        base = Path(cls._tmp.name)
        cls.root = make_calc_project(base / "hello-hearmemory")
        cls.home = base / "codex-home"
        cls.store = create_store(cls.root)
        set_config_value(cls.root, "import", "codex_home", str(cls.home))
        set_config_value(cls.root, "worker", "spawn_from_hooks", False)
        for (sec, key), val in cls.config.items():
            set_config_value(cls.root, sec, key, val)
        cls.cfg = load_config(cls.root)
        cls.client = ScriptedJev(**cls.jev_kwargs)
        judge = JevJudge(cls.cfg, store=cls.store, client=cls.client, environ={I.JEV_API_KEY_ENV: "test-key"})
        judge.capable, judge.reason = True, None
        cls.states, cls.briefs = {}, {}

        def phase(n: int, clock: str, rollouts=()):
            from hearmemory.host.codex import import_rollouts
            for r in rollouts:
                write_rollout(cls.home, r, cls.root)
            append_phase(cls.root, n)
            import_rollouts(cls.store, cls.cfg)
            run_pipeline(cls.store, cls.cfg, 30.0, True, "once", jev_judge=judge, clock=lambda: epoch(clock))
            cls.states[n] = load_or_rebuild(cls.store, cls.cfg, allow_rebuild=True, now=clock + ".000000Z")

        phase(1, "2026-09-24T16:33:00", ("a",))
        cls.briefs["claude"] = brief_for(cls.states[1], cls.store, cls.cfg, "claude", SID_CLAUDE,
                                         "2026-09-24T16:38:35.000000Z")
        if 2 in cls.phases:
            write_project(cls.root, 1)
            phase(2, "2026-09-24T16:40:00")
            cls.briefs["codex_b"] = brief_for(cls.states[2], cls.store, cls.cfg, "codex", SID_B,
                                              "2026-09-24T16:42:07.000000Z")
        if 3 in cls.phases:
            write_project(cls.root, 2)
            phase(3, "2026-09-24T16:45:00", ("b",))
            cls.briefs["next"] = brief_for(cls.states[3], cls.store, cls.cfg, "claude", "next-session",
                                           "2026-09-24T16:45:13.000000Z")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def obs(self) -> Dict[str, I.Observation]:
        return {o.id: o for _, o in self.store.iter_observations()}

    def session_obs(self, sid: str, kind: str = "command") -> List[I.Observation]:
        return sorted((o for o in self.obs().values() if o.provenance.session_id == sid and o.kind == kind),
                      key=lambda o: (o.ts, o.id))

    def claim_of(self, state, obs_id: str):
        views = [v for v in state.claims.values() if v.claim.obs_id == obs_id]
        self.assertEqual(len(views), 1, [v.claim.text for v in views])
        return views[0]

    def b1_of(self, claim_id: str):
        return [c for c in self.store.iter_candidates() if c.template_id == "B1" and c.meta.get("claim_id") == claim_id]

    def line(self, brief: str, needle: str) -> str:
        lines = [ln for ln in self.briefs[brief].text.splitlines() if needle in ln]
        self.assertEqual(len(lines), 1, self.briefs[brief].text)
        return lines[0]


class RealReplay(_Scenario):
    # ---- problem 1 -------------------------------------------------------------------------------------
    def test_commands_containing_hearmemory_are_observed(self):
        cmds = [o for o in self.session_obs(SID_A) if "git commit" in (o.tool.command or "")]
        self.assertEqual(len(cmds), 2, [o.text[:80] for o in self.session_obs(SID_A)])
        refused, done = cmds
        self.assertTrue(refused.tool.command.startswith("hearmemory record --kind claim"))
        self.assertIn("fatal: Unable to create", refused.text)
        self.assertNotEqual(refused.tool.status, "ok")
        self.assertEqual((done.tool.exit_code, done.tool.status), (0, "ok"))
        self.assertIn("[main 578e1b9] add mul (by codex session A)", done.text)
        for o in cmds:                                   # hearmemory's own output is never part of it
            self.assertNotIn("hearmemory: recorded", o.text)
            self.assertNotIn("hearmemory check: allow", o.text)
        # the record is linked to session A (not recorded a second time)
        self.assertEqual([o.id for o in self.obs().values() if o.kind == "claim" and "新增 mul" in o.text], [A_CLAIM_OBS])
        links = [e for e in self.store.iter_events() if e.kind == "provenance_link" and e.target == A_CLAIM_OBS]
        self.assertTrue(links and all(e.provenance.session_id == SID_A for e in links))

    def test_dirty_run_is_linked_to_the_commit(self):
        last = self.states[1].stats["runs"]["pytest"]["last"]
        self.assertEqual((last["session"], last["summary"], last["dirty"]), (SID_A, "7 passed", True))
        self.assertEqual(last["commit_to"], "578e1b9")

    # ---- problem 2 -------------------------------------------------------------------------------------
    def test_combined_run_evidence_keeps_the_test_summary(self):
        a = self.claim_of(self.states[1], A_CLAIM_OBS)
        ev = self.b1_of(a.claim.claim_id)[0].state["evidence"]
        run = [e["text"] for e in ev if e["text"].startswith("$ python -m pytest -q && git diff")]
        self.assertEqual(len(run), 1, ev)
        self.assertIn("[hearmemory: parsed pytest result: 7 passed, 0 failed]", run[0])
        self.assertIn("7 passed in 0.00s", run[0])
        self.assertLessEqual(len(run[0]), int(self.cfg["extract"]["b1_evidence_chars"]))

    # ---- problem 3 -------------------------------------------------------------------------------------
    def test_change_claim_evidence_has_the_authors_edit_and_commit(self):
        a = self.claim_of(self.states[1], A_CLAIM_OBS)
        cand = self.b1_of(a.claim.claim_id)[0]
        self.assertEqual(cand.meta["actor"], "codex:" + SID_A)
        obs = self.obs()
        ev = [obs[i] for i in cand.meta["evidence_obs_ids"]]
        self.assertEqual([o.paths for o in ev if o.kind == "file_edit"], [["src/calc.py"]])   # the one defining mul
        self.assertTrue([o for o in ev if o.kind == "command" and "[main 578e1b9]" in o.text])
        self.assertIn(a.status, ("supported", "same_source_only"))

    def test_claude_session_learns_mul_was_added(self):
        line = self.line("claude", "新增 mul(a, b)")
        self.assertIn("(the agent's own report)", line)
        self.assertIn("codex · session 01a0d441", line)

    # ---- problem 4 -------------------------------------------------------------------------------------
    def test_open_finding_is_shown_first_to_codex_b(self):
        p3 = self.briefs["codex_b"].text.split("New from other agents:\n", 1)[1].splitlines()
        self.assertIn("mul coverage insufficient", p3[0])

    def test_addressed_finding_is_demoted(self):
        f = self.claim_of(self.states[3], FINDING_OBS)
        self.assertEqual(f.addressed["session"], SID_B)
        line = self.line("next", "mul coverage insufficient")
        self.assertTrue(line.startswith("- [ADDRESSED?] "), line)
        self.assertIn("→ codex session 01a0d44b edited tests/test_calc.py, 11 passed, 1dcbbe0", line)
        p3 = self.briefs["next"].text.split("New from other agents:\n", 1)[1].splitlines()
        self.assertNotIn("mul coverage insufficient", p3[0])
        self.assertEqual(p3.index(line), max(i for i, ln in enumerate(p3) if ln.startswith("- ")))

    # ---- problem 5 -------------------------------------------------------------------------------------
    def test_amended_commit_is_followed(self):
        last = self.states[3].stats["runs"]["pytest"]["last"]
        self.assertEqual((last["session"], last["summary"]), (SID_B, "11 passed"))
        self.assertEqual(last["commit_to"], "1dcbbe0")
        entry = self.states[3].stats["obs_index"][B_CLAIM_OBS]
        self.assertEqual((entry["dirty"], entry["commit_to"]), (True, "1dcbbe0"))
        self.assertIn("578e1b9+dirty → 1dcbbe0", self.line("next", "补充 mul"))
        self.assertIn("cf15004+dirty → 578e1b9", self.line("next", "新增 mul"))       # A's claim, before its commit

    # ---- problem 6 -------------------------------------------------------------------------------------
    def test_low_confidence_support_is_weak(self):
        f = self.claim_of(self.states[2], FINDING_OBS)
        self.assertEqual(f.status, "weak_support")
        line = self.line("codex_b", "mul coverage insufficient")
        self.assertTrue(line.startswith("- [WEAK SUPPORT] "), line)
        self.assertNotIn("[SUPPORTED]", line)
        c = self.claim_of(self.states[2], CLAUDE_CLAIM_OBS)            # 0.84 + its own run: not weak
        self.assertNotEqual(c.status, "weak_support")


class RealReplayJevInsufficient(_Scenario):
    """Problem 3, brief side: even when Jev never supports it, A's change claim reaches the Claude session."""
    jev_kwargs = {"never_support": True}
    phases = (1,)

    def test_unsettled_change_claim_is_listed(self):
        a = self.claim_of(self.states[1], A_CLAIM_OBS)
        self.assertEqual(a.status, "insufficient")
        line = self.line("claude", "新增 mul(a, b)")
        self.assertTrue(line.startswith("- [UNVERIFIED] "), line)
        self.assertIn("(the agent's own report)", line)


class RealReplayLowFloor(_Scenario):
    """Problem 6: the floor is configurable."""
    config = {("judge", "b1_min_support_confidence"): 0.4}
    phases = (1, 2)

    def test_floor_from_config(self):
        self.assertEqual(self.claim_of(self.states[2], FINDING_OBS).status, "supported")


class Units(unittest.TestCase):
    def test_hearmemory_only_and_strip(self):
        from hearmemory.textutil import hearmemory_only_command, strip_hearmemory_output
        self.assertTrue(hearmemory_only_command('hearmemory record --kind claim "a && b"'))
        self.assertTrue(hearmemory_only_command("cd /x && hearmemory check --staged"))
        self.assertTrue(hearmemory_only_command("python -m hearmemory recall --brief"))
        self.assertFalse(hearmemory_only_command("hearmemory record x && git commit -m y"))
        self.assertFalse(hearmemory_only_command("python -m pytest -q && hearmemory record --kind claim ok"))
        out = ("hearmemory: recorded o-937776aee3ecac29\nhearmemory check: allow\n[hearmemory] Shared project memory — 1 items.\n"
               "New from other agents:\n- [SUPPORTED] \"x\" — codex\nDetails: hearmemory_recall (MCP) or ...\n"
               "M  src/calc.py\n[main 578e1b9] add mul\n")
        self.assertEqual(strip_hearmemory_output(out), "M  src/calc.py\n[main 578e1b9] add mul\n")

    def test_function_call_command_with_hearmemory_is_observed(self):
        from hearmemory.host.codex import import_rollouts
        with tempfile.TemporaryDirectory() as tmp:
            root = make_calc_project(Path(tmp) / "p")
            store = create_store(root)
            home = Path(tmp) / "h" / "sessions"
            home.mkdir(parents=True)
            t = "2026-09-25T10:00:00.000Z"

            def call(cid, cmd):
                return {"type": "response_item", "payload": {"type": "function_call", "name": "exec_command",
                                                             "call_id": cid, "arguments": json.dumps({"cmd": cmd})}}

            def out(cid, text):
                return {"type": "response_item", "payload": {"type": "function_call_output", "call_id": cid,
                                                             "output": text}}
            rows = [{"type": "session_meta", "payload": {"id": "s-fc", "cwd": str(root), "source": "exec"}},
                    call("c1", "hearmemory recall --brief"), out("c1", "[hearmemory] Shared project memory — 1 items.\n- x\n"),
                    call("c2", 'hearmemory record --kind claim "added mul" && git commit -m y'),
                    out("c2", "hearmemory: recorded o-0123456789abcdef\n[main abc1234] y\n 1 file changed\n"
                              "Process exited with code 0\n")]
            with open(home / "rollout-s-fc.jsonl", "w", encoding="utf-8") as fh:
                for r in rows:
                    fh.write(json.dumps(dict({"timestamp": t}, **r)) + "\n")
            import_rollouts(store, load_config(root), codex_home=str(home.parent))
            obs = [o for _, o in store.iter_observations()]
            events = list(store.iter_events())
        self.assertEqual([o.tool.command for o in obs], ['hearmemory record --kind claim "added mul" && git commit -m y'])
        self.assertEqual((obs[0].tool.exit_code, obs[0].tool.status), (0, "ok"))
        self.assertIn("[main abc1234] y", obs[0].text)
        self.assertNotIn("hearmemory: recorded", obs[0].text)
        self.assertEqual([e.target for e in events if e.kind == "provenance_link"], ["o-0123456789abcdef"])

    def test_cli_host_guess_prefers_codex_launch_id(self):
        from hearmemory.commands import guess_cli_host
        self.assertEqual(guess_cli_host({"CLAUDECODE": "1", "HEARMEMORY_SESSION_ID": "codex-1790267473-3284"}), "codex")
        self.assertEqual(guess_cli_host({"CLAUDECODE": "1"}), "claude")

    def test_claude_bash_with_hearmemory_is_observed(self):
        from hearmemory.host import claude
        with tempfile.TemporaryDirectory() as tmp:
            root = make_calc_project(Path(tmp) / "p")
            payload = {"session_id": "s1", "cwd": str(root), "hook_event_name": "PostToolUse", "tool_name": "Bash",
                       "tool_use_id": "toolu_1", "_root": root, "_cfg": dict(I.DEFAULT_CONFIG),
                       "tool_input": {"command": "python -m pytest -q && hearmemory record --kind claim 'tests pass'"},
                       "tool_response": {"stdout": "..\n2 passed in 0.01s\nhearmemory: recorded o-0123456789abcdef\n",
                                         "stderr": "", "interrupted": False}}
            out = claude._normalize_tool_event("PostToolUse", payload, dict(I.DEFAULT_CONFIG), root)
        links = [x for x in out if isinstance(x, I.ControlEvent)]
        cmds = [x for x in out if isinstance(x, I.Observation)]
        self.assertEqual([e.target for e in links], ["o-0123456789abcdef"])
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0].tool.test.passed, 2)
        self.assertNotIn("hearmemory: recorded", cmds[0].text)

    def test_test_run_window(self):
        from hearmemory.judge.extract import _test_run_window
        test = I.RunnerSummary(runner="pytest", passed=7, failed=0)
        body = ".......    [100%]\n7 passed in 0.00s\n" + "".join(f"+line {i} of a long diff\n" for i in range(200))
        w = _test_run_window("$ pytest -q && git diff\n", body, test, 800, None)
        self.assertLessEqual(len(w), 800)
        self.assertTrue(w.startswith("$ pytest -q && git diff\n[hearmemory: parsed pytest result: 7 passed, 0 failed]\n"
                                     "7 passed in 0.00s\n"))
        self.assertIn("chars omitted]", w)
        self.assertIn("+line 199 of a long diff", w)
        self.assertIn("+line 0 of a long diff", w)

    def test_amend_after_an_edit_is_not_followed(self):
        from hearmemory.memory.runs import link_dirty_runs

        def o(oid, ts, kind, cmd=None, text="", paths=()):
            tool = I.ToolInfo(name="exec_command", command=cmd, exit_code=0, status="ok") if cmd else \
                I.ToolInfo(name="apply_patch", paths=list(paths), status="ok")
            return I.Observation(id=oid, ts=ts, kind=kind, event_key=oid, text=text, tool=tool,
                                 provenance=I.Provenance(host="codex", session_id="s"))
        seq = [o("o-run", "2026-09-24T16:42:28Z", "command", "pytest -q"),
               o("o-c1", "2026-09-24T16:43:11Z", "command", "git commit -m x", "[main 387134d] x\n"),
               o("o-a1", "2026-09-24T16:43:43Z", "command", "git commit --amend --no-edit", "[main dfd325c] x\n"),
               o("o-ed", "2026-09-24T16:43:50Z", "file_edit", paths=["tests/test_calc.py"]),
               o("o-a2", "2026-09-24T16:44:19Z", "command", "git commit --amend --no-edit", "[main 1dcbbe0] x\n")]
        recs = {"o-run": {"obs_id": "o-run", "ts": seq[0].ts, "actor": "codex:s", "dirty": True}}
        link_dirty_runs(seq, {x.id: "codex:s" for x in seq}, recs)
        self.assertEqual(recs["o-run"]["commit_to"], "dfd325c")

    def test_weak_support_decision(self):
        from hearmemory.memory.ops import consume_b1, decision_from_judgment
        claim = I.Claim(claim_id="c-1", obs_id="o-c", span=[0, 5], text="x gap", claim_class="other", explicit=True)
        cand = I.Candidate(candidate_id="k-1", template_id="B1", template_version="v", subject_key="claim:c-1",
                           state={}, basis_obs_ids=["o-c", "o-e"], input_hash="h", created_ts="2026-09-24T16:39:17Z",
                           meta={"claim_id": "c-1", "evidence_obs_ids": ["o-e"]})
        j = I.Judgment(judgment_id="j-1", candidate_id="k-1", template_id="B1", template_version="v", input_hash="h",
                       provider="jev", outcome="valid", ts="2026-09-24T16:39:19Z", label="supports", confidence=0.5,
                       probabilities={"supports": 0.63, "insufficient": 0.35, "refutes": 0.0, "both": 0.02})
        status = lambda d: consume_b1(d, claim)[0].target["status"]           # noqa: E731
        self.assertEqual(status(decision_from_judgment(cand, j)), "weak_support")
        self.assertEqual(status(decision_from_judgment(cand, j, min_support_confidence=0.4)), "supported")
        cand.meta["direct_support_ids"] = ["o-e"]                              # program-checked run: not weakened
        self.assertEqual(status(decision_from_judgment(cand, j)), "supported")


if __name__ == "__main__":
    unittest.main()
