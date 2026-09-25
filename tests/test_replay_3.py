"""Replay 3: regression tests for the problems seen in a real run (Codex -> Claude -> Codex, 2026-09-24).

What happened for real: after `hearmemory init` on a fresh store in hello-hearmemory, Codex session A added `div(a, b)` to
src/calc.py plus tests (apply_patch heredoc), ran `python -m pytest -q` (3 passed) BEFORE committing, committed
8761b49 and recorded "新增 `div(a, b)`：… `b == 0` 时抛出 `ValueError`；`python -m pytest -q` 通过（3 passed），并已按
指定信息提交。"; a Claude desktop session (git worktree; main agent + 2 subagents) ran pytest (3 passed) and recorded
"pytest run: 3 passed, 0 failed (in 0.00s), no errors. Test suite is green." (subagent A) and a coverage-gap
finding (subagent B); Codex session B, in the new "code mode" (one custom tool `exec` running a JS snippet that
calls `tools.exec_command` / `tools.apply_patch` / `tools.mcp__hearmemory__*`), added 3 edge tests, ran pytest (6
passed) and committed cf15004. What went wrong:
  1. none of B's 9 code-mode calls became an observation: its edit, its 6-passed run and its commit were invisible;
  2. A's compound claim got B1 "insufficient" twice (evidence: its test run only, never its edits / commit) and
     never reached the Claude session's brief -- which learned "tests passed", not "div was added";
  3. A's run on the dirty tree was attributed to the pre-commit HEAD 9929756 ("3 passed（对应提交 9929756）");
  4. the English test-result claim was class 'other' (never judged) and showed as [UNVERIFIED] next to the very
     run line it restates;
  5. the auto-import of the fresh store pulled Codex sessions that ENDED before `hearmemory init`; an old echo claim
     became [SUPPORTED] and was shown to Codex A.

The fixtures tests/fixtures/replay_3_* are the relevant real rows, sanitised (project path -> {ROOT}, no user
prompt / instructions / reasoning). The scripted Jev answers B1 "supports" when the
author's own edit of src/calc.py AND its git commit (or a Claude run of 3 passed) are in the evidence, else the
REAL answer (insufficient, p=0.92).
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
SID_A = "01a0d40a-1570-7c31-be5d-6a91ebd0d372"
SID_B = "01a0d411-d38e-7c00-8d51-3fc009e2ad6e"
SID_GUARD = "01a0d411-d467-72a2-b0c6-191c6085d928"
SID_OLD = "01a0d3c3-ef90-7920-bf38-2edeb5fa4094"
SID_CLAUDE = "11f0b146-e843-45a2-811d-393f710a3594"
A_CLAIM_OBS, CLAUDE_RUN, CLAUDE_CLAIM_OBS = "o-8d8833b18608a3fa", "o-0f19edec91e56523", "o-bc23d25b5a6519d6"
FINDING_OBS = "o-a3f3314033d876b6"
PHASE = {1: {"obs": [A_CLAIM_OBS], "events": ["ev-alias-a8265dcbd66b4c3c", "ev-link-fcb9a7daad1b180d"]},
         2: {"obs": [CLAUDE_RUN, CLAUDE_CLAIM_OBS, "o-3e2780fa7c518eaf", FINDING_OBS],
             "events": ["ev-link-378f8867e747bef3", "ev-link-18826a1e1634d914"]},
         3: {"obs": ["o-3ce3884abfbf2b62"], "events": ["ev-alias-f2d40186b4c2a8e3", "ev-link-c09bce594a2cfe5f"]}}
ROLLOUT = {"a": f"rollout-2026-09-24T23-30-25-{SID_A}.jsonl", "b": f"rollout-2026-09-24T23-38-53-{SID_B}.jsonl",
           "guardian": f"rollout-2026-09-24T23-38-53-{SID_GUARD}.jsonl",
           "pre_init": f"rollout-2026-09-24T22-14-00-{SID_OLD}.jsonl"}
REAL_B1_ANSWER = {"type": "choice", "choice": "insufficient", "confidence": 0.89,
                  "probabilities": {"both": 0.0, "refutes": 0.07, "insufficient": 0.92, "supports": 0.01}}
SUPPORTS = {"type": "choice", "choice": "supports", "confidence": 0.9,
            "probabilities": {"supports": 0.9, "refutes": 0.02, "both": 0.0, "insufficient": 0.08}}


def epoch(ts: str) -> float:
    return float(calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%S")))


def _git(root: Path, *args: str, env: Dict[str, str] = None) -> None:
    subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True, text=True,
                   env=dict(os.environ, **(env or {})))


def make_calc_project(root: Path) -> Path:
    """hello-hearmemory as it was before Codex A: add() only, committed before the scenario (like 9929756)."""
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (root / "tests" / "test_calc.py").write_text(
        "from src.calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "add", "-A")
    when = "2026-09-24T15:00:00Z"
    _git(root, "commit", "-q", "-m", "init", env={"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when})
    return root.resolve()


def fixture_text(name: str, root: Path) -> str:
    return (FIX / name).read_text(encoding="utf-8").replace("{ROOT}", str(root))


def write_rollout(home: Path, which: str, root: Path, mtime: float = None) -> Path:
    day = home / "sessions" / "2026" / "09" / "24"
    day.mkdir(parents=True, exist_ok=True)
    path = day / ROLLOUT[which]
    path.write_text(fixture_text(f"replay_3_{'codex_' if which in ('a', 'b') else ''}{which}.jsonl", root),
                    encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def append_phase(root: Path, phase: int) -> None:
    """The rows hearmemory wrote live in that phase (MCP records, Claude hooks, alias / link events)."""
    for fname, ids, key in (("replay_3_obs.jsonl", PHASE[phase]["obs"], "observations.jsonl"),
                            ("replay_3_events.jsonl", PHASE[phase]["events"], "events.jsonl")):
        rows = [ln for ln in fixture_text(fname, root).splitlines() if json.loads(ln)["id"] in ids]
        with open(root / ".hearmemory" / key, "a", encoding="utf-8") as fh:
            fh.write("\n".join(rows) + "\n")


class ScriptedJev:
    """B1: supports when the author's own src/calc.py edit AND its commit (or, for a "3 passed" claim, a Claude run
    of 3 passed) are among the evidence, else `fallback` (the real answer); A1/A2/A3: "not the same"."""

    def __init__(self, fallback: Dict[str, Any] = REAL_B1_ANSWER, never_support: bool = False) -> None:
        self.states: List[Dict[str, Any]] = []
        self.fallback, self.never_support = fallback, never_support

    def system_one(self, state, questions, timeout=None):
        q = questions[I.QUESTION_KEY]
        labels = list((q.get("criteria") if isinstance(q, dict) else getattr(q, "criteria", None)) or {})
        self.states.append(json.loads(json.dumps(state)))
        if "supports" in labels:
            ev = state.get("evidence") or []
            edit = any(e["text"].startswith("edit src/calc.py") and "div" in e["text"] for e in ev)
            commit = any("git commit" in e["text"] for e in ev)
            claude_run = "3 passed" in state["target_claim"] and any(
                "claude" in e["source"] and "3 passed" in e["text"] for e in ev)
            ok = not self.never_support and ((edit and commit) or claude_run)
            ans = dict(SUPPORTS if ok else self.fallback)
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
        cls.cfg = load_config(cls.root)
        cls.client = ScriptedJev(**cls.jev_kwargs)
        judge = JevJudge(cls.cfg, store=cls.store, client=cls.client, environ={I.JEV_API_KEY_ENV: "test-key"})
        judge.capable, judge.reason = True, None
        cls.states, cls.briefs, cls.imported = {}, {}, {}

        def phase(n: int, clock: str, rollouts=()):
            from hearmemory.host.codex import import_rollouts
            for r in rollouts:
                write_rollout(cls.home, r, cls.root)
            append_phase(cls.root, n)
            cls.imported[n] = import_rollouts(cls.store, cls.cfg)
            run_pipeline(cls.store, cls.cfg, 30.0, True, "once", jev_judge=judge, clock=lambda: epoch(clock))
            cls.states[n] = load_or_rebuild(cls.store, cls.cfg, allow_rebuild=True, now=clock + ".000000Z")

        # problem 5: a Codex session that ended (file last written) at 14:14, before this store existed
        write_rollout(cls.home, "pre_init", cls.root, mtime=epoch("2026-09-24T14:14:43"))
        phase(1, "2026-09-24T15:33:00", ("a",))
        cls.briefs["claude"] = brief_for(cls.states[1], cls.store, cls.cfg, "claude", SID_CLAUDE,
                                         "2026-09-24T15:34:26.000000Z")
        if 2 in cls.phases:
            phase(2, "2026-09-24T15:37:00")
            cls.briefs["codex_b"] = brief_for(cls.states[2], cls.store, cls.cfg, "codex", SID_B,
                                              "2026-09-24T15:39:13.000000Z")
        if 3 in cls.phases:
            phase(3, "2026-09-24T15:41:00", ("b", "guardian"))
            cls.briefs["next"] = brief_for(cls.states[3], cls.store, cls.cfg, "claude", "next-session",
                                           "2026-09-24T15:42:00.000000Z")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def obs(self) -> Dict[str, I.Observation]:
        return {o.id: o for _, o in self.store.iter_observations()}

    def claim_of(self, state, obs_id: str):
        views = [v for v in state.claims.values() if v.claim.obs_id == obs_id]
        self.assertEqual(len(views), 1, [v.claim.text for v in views])
        return views[0]

    def b1_of(self, claim_id: str):
        return [c for c in self.store.iter_candidates() if c.template_id == "B1" and c.meta.get("claim_id") == claim_id]


class RealReplay(_Scenario):
    # ---- problem 1 -------------------------------------------------------------------------------------
    def test_code_mode_session_B_is_imported(self):
        b = [o for o in self.obs().values() if o.provenance.session_id == SID_B]
        runs = [o for o in b if o.kind == "command" and o.tool.test is not None]
        self.assertEqual(len(runs), 1, [o.text[:60] for o in b])
        self.assertEqual((runs[0].tool.test.passed, runs[0].tool.test.failed), (6, 0))
        self.assertEqual((runs[0].tool.exit_code, runs[0].tool.status), (0, "ok"))   # from the CommandExecution item
        self.assertTrue(runs[0].text.startswith("$ python -m pytest -q\n"))
        self.assertNotIn("Script completed", runs[0].text)
        edits = [o for o in b if o.kind == "file_edit"]
        self.assertEqual([o.paths for o in edits], [["tests/test_calc.py"]])        # absolute path made relative
        self.assertIn("+def test_div_non_exact_values():", edits[0].text)
        self.assertIn("-", "".join(ln[:1] for ln in edits[0].text.splitlines()) + "-")
        commits = [o for o in b if o.kind == "command" and "git commit" in (o.tool.command or "")]
        self.assertEqual(len(commits), 1)
        self.assertEqual(commits[0].tool.status, "ok")
        self.assertIn("[main cf15004]", commits[0].text)
        # hearmemory's own recall output (the brief it was shown) is never imported; the pure-JS tool listing is ignored
        self.assertFalse([o for o in b if I.HEARMEMORY_OUTPUT_MARKER in (o.text or "") or "tool list" in (o.text or "")])
        # the record inside the snippet is linked to session B
        links = [e for e in self.store.iter_events() if e.kind == "provenance_link" and e.target == "o-3ce3884abfbf2b62"]
        self.assertTrue(links and all(e.provenance.session_id == SID_B for e in links))

    def test_guardian_rollout_is_not_an_agent(self):
        self.assertFalse([o for o in self.obs().values() if o.provenance.session_id == SID_GUARD])
        self.assertFalse([o for o in self.obs().values() if "git status --short -- tests" in (o.text or "")])

    def test_latest_run_is_Bs_six_passed(self):
        last = self.states[3].stats["runs"]["pytest"]["last"]
        self.assertEqual((last["summary"], last["session"]), ("6 passed", SID_B))
        self.assertIn("test `pytest` passed (6 passed)", self.briefs["next"].text)
        self.assertNotIn("(3 passed)", self.briefs["next"].text)

    # ---- problem 2 -------------------------------------------------------------------------------------
    def test_compound_claim_evidence_has_the_authors_edit_and_commit(self):
        a = self.claim_of(self.states[1], A_CLAIM_OBS)
        self.assertEqual(a.claim.claim_class, "status")
        cand = self.b1_of(a.claim.claim_id)[0]
        obs = self.obs()
        ev = [obs[i] for i in cand.meta["evidence_obs_ids"]]
        self.assertLessEqual(len(ev), int(self.cfg["extract"]["b1_max_evidence"]))       # the budget holds
        kinds = [(o.kind, o.paths if o.kind == "file_edit" else (o.tool.command or "")[:20]) for o in ev]
        self.assertIn(("file_edit", ["src/calc.py"]), kinds)
        self.assertTrue([o for o in ev if o.kind == "command" and "git commit" in o.tool.command])
        self.assertTrue([o for o in ev if o.kind == "command" and o.tool.test is not None])
        texts = [e["text"] for e in cand.state["evidence"]]
        self.assertTrue(any(t.startswith("edit src/calc.py") and "+     if b == 0:" in t for t in texts), texts)
        self.assertTrue(cand.meta["implementing_edit_ids"])
        self.assertIn(a.status, ("supported", "same_source_only"))

    def test_claude_session_learns_div_was_added(self):
        text = self.briefs["claude"].text
        self.assertIn("新增 `div(a, b)`", text)
        line = [ln for ln in text.splitlines() if "新增 `div(a, b)`" in ln][0]
        self.assertIn("(the agent's own report)", line)                  # own evidence only: not independent
        self.assertIn("codex · session 01a0d40a", line)

    # ---- problem 3 -------------------------------------------------------------------------------------
    def test_dirty_run_is_labelled_and_linked_to_the_commit_after_it(self):
        last = self.states[1].stats["runs"]["pytest"]["last"]
        self.assertEqual(last["session"], SID_A)
        self.assertTrue(last["dirty"])
        self.assertEqual(last["commit_to"], "8761b49")
        run_obs = self.obs()[last["obs_id"]]
        self.assertTrue(run_obs.provenance.git_dirty)
        self.assertEqual(run_obs.meta["dirty_inferred"], ["src/calc.py", "tests/test_calc.py"])
        head = run_obs.provenance.git_commit[:7]
        line = [ln for ln in self.briefs["claude"].text.splitlines() if ln.startswith("- test `pytest`")][0]
        self.assertIn(f"{head}+dirty → 8761b49", line)
        # Jev is told too
        a = self.claim_of(self.states[1], A_CLAIM_OBS)
        srcs = [e["source"] for e in self.b1_of(a.claim.claim_id)[0].state["evidence"]]
        self.assertTrue(any("plus uncommitted changes" in s and "command run" in s for s in srcs), srcs)
        # the commit itself (and every command after it) ran on a clean tree
        commit = [o for o in self.obs().values() if o.provenance.session_id == SID_A and o.kind == "command"
                  and "git commit" in o.tool.command][0]
        self.assertIsNot(commit.provenance.git_dirty, True)

    # ---- problem 4 -------------------------------------------------------------------------------------
    def test_english_test_result_claim_is_a_status_claim_and_judged(self):
        c = self.claim_of(self.states[2], CLAUDE_CLAIM_OBS)
        self.assertEqual(c.claim.claim_class, "status")
        cand = self.b1_of(c.claim.claim_id)
        self.assertTrue(cand)
        self.assertIn(CLAUDE_RUN, cand[-1].meta["direct_support_ids"])
        self.assertNotEqual(c.status, "unjudged")

    def test_claim_restating_a_shown_run_collapses_into_it(self):
        text = self.briefs["codex_b"].text
        self.assertIn("test `pytest` passed (3 passed) — claude", text)
        self.assertNotIn("pytest run: 3 passed", text)
        self.assertIn("Reviewed tests/test_calc.py", text)                 # the finding keeps its slot

    # ---- problem 5 -------------------------------------------------------------------------------------
    def test_session_before_init_is_not_imported(self):
        self.assertFalse([o for o in self.obs().values() if o.provenance.session_id == SID_OLD])
        self.assertFalse([v for v in self.states[3].claims.values() if "共享记忆显示" in v.claim.text])
        self.assertTrue((self.root / ".hearmemory" / "state" / "init.json").is_file())


class RealReplayJevInsufficient(_Scenario):
    """Problem 2, brief side: even when Jev never supports it, A's end-of-session claim reaches the next agent."""
    jev_kwargs = {"never_support": True}
    phases = (1,)

    def test_unsettled_own_report_is_listed_not_dropped(self):
        a = self.claim_of(self.states[1], A_CLAIM_OBS)
        self.assertEqual(a.status, "insufficient")
        line = [ln for ln in self.briefs["claude"].text.splitlines() if "新增 `div(a, b)`" in ln]
        self.assertEqual(len(line), 1, self.briefs["claude"].text)
        self.assertTrue(line[0].startswith("- [UNVERIFIED] "))
        self.assertIn("(the agent's own report)", line[0])

    def test_report_never_outranks_another_fact(self):
        from hearmemory.memory.brief import build_brief
        cfg = json.loads(json.dumps(self.cfg))
        cfg["brief"]["p3_max"] = 1
        req = I.BriefRequest(context=I.AgentContext(host="claude", session_id="other-1"), purpose="session_start",
                             max_tokens=600, lang="en")
        b = build_brief(self.states[1], self.store, req, cfg, now="2026-09-24T15:34:26.000000Z")
        self.assertEqual([it.kind for it in b.items], ["fact"])
        self.assertTrue(b.items[0].item_key.startswith("run:"))


class CodeModeShapes(unittest.TestCase):
    """Problem 1, parser side."""

    def _import(self, rows: List[Dict[str, Any]]):
        from hearmemory.host.codex import import_rollouts
        with tempfile.TemporaryDirectory() as tmp:
            root = make_calc_project(Path(tmp) / "p")
            store = create_store(root)
            home = Path(tmp) / "h" / "sessions"
            home.mkdir(parents=True)
            t = "2026-09-24T15:39:00.000Z"
            meta = {"timestamp": t, "type": "session_meta", "payload": {"id": "s-code", "cwd": str(root), "source": "exec"}}
            with open(home / "rollout-s-code.jsonl", "w", encoding="utf-8") as fh:
                for r in [meta] + rows:
                    fh.write(json.dumps(dict({"timestamp": t}, **r)) + "\n")
            import_rollouts(store, load_config(root), codex_home=str(home.parent))
            return [o for _, o in store.iter_observations()], list(store.iter_events())

    @staticmethod
    def _call(cid: str, js: str) -> Dict[str, Any]:
        return {"type": "response_item", "payload": {"type": "custom_tool_call", "call_id": cid, "name": "exec", "input": js}}

    @staticmethod
    def _out(cid: str, *texts: str, head: str = "Script completed\nWall time 0.2 seconds\nOutput:\n", stringify=False):
        parts = [{"type": "input_text", "text": head}] + [{"type": "input_text", "text": t} for t in texts]
        return {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": cid,
                                                     "output": str(parts) if stringify else parts}}

    def test_several_calls_in_one_snippet(self):
        js = ('const a = await tools.exec_command({cmd: "python -m pytest -q", workdir: "."});\n'
              "const b = await tools.exec_command({cmd:'git status --short'});\ntext(a.output); text(b.output);\n")
        obs, _ = self._import([self._call("c1", js),
                               self._out("c1", "..F\n1 failed, 2 passed in 0.03s\n", " M src/calc.py\n", stringify=True)])
        cmds = [o for o in obs if o.kind == "command"]
        self.assertEqual([o.tool.command for o in cmds], ["python -m pytest -q", "git status --short"])
        run, last = cmds
        self.assertEqual((run.tool.test.failed, run.tool.test.passed), (1, 2))    # still parsed as a test result
        self.assertEqual(run.meta["test_summary_from"], "combined_output")
        self.assertTrue(last.meta["combined_output"])
        self.assertIn("1 failed, 2 passed", last.text)
        self.assertNotIn("1 failed", run.text)
        from hearmemory.testcmd import test_outcome
        self.assertEqual(test_outcome(run.tool), "fail")

    def test_patch_from_a_variable_and_an_unknown_tool(self):
        js = ('const p = "*** Begin Patch\\n*** Update File: src/calc.py\\n@@\\n def add(a, b):\\n-    return a + b\\n'
              '+    return b + a\\n*** End Patch";\nawait tools.apply_patch(p);\nawait tools.view_image({path: "x.png"});\n')
        obs, _ = self._import([self._call("c2", js), self._out("c2", "{}"),
                               {"type": "response_item", "payload": {"type": "custom_tool_call", "call_id": "c3",
                                                                     "name": "web_search", "input": "{}"}},
                               self._out("c3", "results", head="")])
        edits = [o for o in obs if o.kind == "file_edit"]
        self.assertEqual([o.paths for o in edits], [["src/calc.py"]])
        self.assertIn("+    return b + a", edits[0].text)
        self.assertIn("-    return a + b", edits[0].text)
        generic = sorted(o.tool.name for o in obs if o.kind == "search")
        self.assertEqual(generic, ["view_image", "web_search"])

    def test_garbage_never_crashes(self):
        obs, _ = self._import([self._call("c4", "await tools.exec_command({cmd: `unterminated"),
                               self._out("c4", "x"), self._call("c5", "text(1)"), self._out("c5", "1"),
                               self._out("nope", "orphan output")])
        self.assertEqual([o for o in obs if o.kind in ("command", "file_edit")], [])


class EnglishTestResultClaims(unittest.TestCase):
    """Problem 4, classifier side (English and Chinese)."""

    def test_classify(self):
        from hearmemory.judge.claims import claimed_outcome, classify, command_targets, restates_run_only
        real = "pytest run: 3 passed, 0 failed (in 0.00s), no errors. Test suite is green."
        self.assertEqual(claimed_outcome(real), "pass")                  # "0 failed" / "no errors" are not failures
        self.assertEqual(command_targets(real), ["pytest"])             # not "pytest run:"
        self.assertEqual(classify(real, []), "status")
        for t, want in (("All 6 tests passed.", "pass"), ("The test suite is red.", "fail"),
                        ("2 tests failed in tests/test_calc.py", "fail"), ("测试全部通过（6 passed）。", "pass")):
            self.assertEqual((classify(t, []), claimed_outcome(t)), ("status", want), t)
        self.assertEqual(classify("The parser reads the whole file.", []), "other")
        self.assertTrue(restates_run_only(real))
        self.assertTrue(restates_run_only("pytest 结果:3 passed, 0 failed(耗时 0.00s),测试全部通过。"))
        self.assertFalse(restates_run_only("Added tests for zero/negative addition; python -m pytest -q passes all 6 tests."))
        self.assertFalse(restates_run_only("新增 `div(a, b)`；`python -m pytest -q` 通过（3 passed）。"))


class ImportFloor(unittest.TestCase):
    """Problem 5: only sessions after `hearmemory init`, unless asked to backfill."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = make_calc_project(Path(self._tmp.name) / "p")
        self.home = Path(self._tmp.name) / "h"
        self.store = create_store(self.root)
        self.cfg = load_config(self.root)
        write_rollout(self.home, "pre_init", self.root, mtime=time.time() - 3600)

    def _n(self, **kw) -> int:
        from hearmemory.host.codex import import_rollouts
        return import_rollouts(self.store, self.cfg, codex_home=str(self.home), **kw)

    def test_default_skips_then_backfill_imports(self):
        self.assertIn("init_ts", self.store.read_state("init"))
        self.assertEqual(self._n(), 0)
        self.assertEqual(self._n(since=time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - 60))), 0)
        self.assertEqual(self._n(since=time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - 7200))), 1)

    def test_all_history_and_cli(self):
        from hearmemory import commands
        from hearmemory.cli import main
        self.assertEqual(self._n(all_history=True), 1)
        self.assertEqual(main(["--project", str(self.root), "import", "codex", "--codex-home", str(self.home),
                               "--since", "not-a-date"]), commands.EXIT_USAGE)

    def test_store_without_init_record_keeps_old_behaviour(self):
        (self.root / ".hearmemory" / "state" / "init.json").unlink()
        self.assertEqual(self._n(), 1)

    def test_session_running_across_init_is_imported(self):
        path = write_rollout(self.home, "pre_init", self.root)          # still being written after init
        self.assertGreater(path.stat().st_mtime, time.time() - 60)
        self.assertEqual(self._n(), 1)


if __name__ == "__main__":
    unittest.main()
