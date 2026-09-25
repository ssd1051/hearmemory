"""Replay 2: regression tests for the problems seen in a real run (Claude Code desktop, 2026-09-24).

What happened for real: a Claude Code desktop session in the test project hello-hearmemory ran inside a git worktree
(<root>/.claude/worktrees/zealous-williams-e40025, same repository, same HEAD); the main agent started two parallel
subagents. A ran `python -m pytest -q` (1 passed) and recorded "子 agent A：... 1 passed"; B reviewed the tests and
recorded "... 缺少负数、零、浮点数等边界用例，测试覆盖不充分". Before that, three Codex sessions had fixed and restated
the add() fix. What went wrong:
  1. A's run was stored with the target `pytest .claude/worktrees/zealous-williams-e40025`, so it matched no claim;
     Jev only saw an older Codex run (another commit) and A's claim became [INSUFFICIENT];
  2. agents quoted hearmemory's own output ("- `[SUPPORTED]` <claim> — codex ...", recall results) and restated memory
     they had just been shown; the extractor took those as new claims from new agents (memory echo);
  3. B's review finding (an explicitly recorded 'other' claim, unjudged) never reached the next agent's brief;
  4. `hearmemory status` in a shell without the key printed "jev: unavailable (no_key)" while the live worker had Jev
     and made 26 real judgments.

`tests/fixtures/replay_2_claude_{obs,events}.jsonl` are the relevant real rows, sanitised (project path ->
{ROOT}, no user prompt); the memory_shown events are what the fixed brief would have recorded, taken from the real
state/sessions/<key>.json "shown" files. The fake Jev answers B1 "supports" when the claim's own same-commit run is
in the evidence and gives the REAL answer (insufficient, p=0.92) otherwise.
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
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from hearmemory import interfaces as I  # noqa: E402
from hearmemory import commands  # noqa: E402
from hearmemory.config import load_config, set_config_value  # noqa: E402
from hearmemory.store import create_store  # noqa: E402

FIX = REPO / "tests" / "fixtures"
WT = ".claude/worktrees/zealous-williams-e40025"
SID = "13222d3f-f602-4e67-a821-490b0b9e84aa"
AGENT_A, AGENT_B = "ad34eb9d79ba7b8e0", "ae2e3491f9cb6c19a"
A_RUN, A_CLAIM_OBS, B_CLAIM_OBS = "o-abbcabfc5c86619d", "o-a5f1283193ef1829", "o-84718629af3dcab3"
ECHO_OBS = ("o-fcbb626f1dad24a9", "o-95ceb06c7b8ba334")
CODEX_CLAIM = "c-d9364538930c0c6b"            # session 2's own record (the original)
CODEX_ECHO = "c-59e716efb0091837"             # session 3 "共享记忆显示：上一会话把 ..." (shown c-d936 first)
CODEX_RECHECK = "c-c2664ecd9c2ad88f"          # session 3's record, restating c-d936 it had been shown
MAIN_ECHO = "c-dee97d0e0ee11fc5"              # main agent "一句话复述：Codex 之前的会话把 ..." (shown c-59e7)
TAGGED_ECHO = "c-4ee041ad284b5577"            # main agent "- `[SUPPORTED]` 当前 ..." (a quoted brief line)
REAL_B1_ANSWER = {"type": "choice", "choice": "insufficient", "confidence": 0.9,
                  "probabilities": {"refutes": 0.07, "supports": 0.01, "both": 0.0, "insufficient": 0.92}}
SUPPORTS = {"type": "choice", "choice": "supports", "confidence": 0.9,
            "probabilities": {"supports": 0.9, "refutes": 0.02, "both": 0.0, "insufficient": 0.08}}


def epoch(ts: str) -> float:
    return float(calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%S")))


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True, text=True)


def make_fixed_calc_project(root: Path) -> Path:
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (root / "tests" / "test_calc.py").write_text(
        "from src.calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root.resolve()


class ScriptedJev:
    """B1: supports when a claude run is among the evidence, else the real answer; A1/A2/A3: 'not the same'."""

    def __init__(self) -> None:
        self.states: List[Dict[str, Any]] = []

    def system_one(self, state, questions, timeout=None):
        q = questions[I.QUESTION_KEY]
        labels = list((q.get("criteria") if isinstance(q, dict) else getattr(q, "criteria", None)) or {})
        self.states.append(json.loads(json.dumps(state)))
        if "supports" in labels:
            own = any("claude" in (e.get("source") or "") for e in state.get("evidence") or [])
            ans = dict(SUPPORTS if own else REAL_B1_ANSWER)
        else:
            ans = {"type": "choice", "confidence": 0.5,
                   "choice": [x for x in labels if x in ("not_contained", "different", "different_events")][0]}
        return {"answers": {I.QUESTION_KEY: ans}, "model": I.JEV_MODEL_DEFAULT,
                "usage": {"input_tokens": 700, "output_tokens": 50}}


class RealReplay(unittest.TestCase):
    """The real rows replayed through the fixed extractor, (fake) Jev and memory builder."""

    @classmethod
    def setUpClass(cls) -> None:
        from hearmemory.judge.jev import JevJudge
        from hearmemory.judge.worker import run_pipeline
        from hearmemory.memory.build import load_or_rebuild
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = make_fixed_calc_project(Path(cls._tmp.name) / "hello-hearmemory")
        cls.store = create_store(cls.root)
        set_config_value(cls.root, "import", "codex_home", str(Path(cls._tmp.name) / "none"))
        set_config_value(cls.root, "worker", "spawn_from_hooks", False)
        for src, dst in (("replay_2_claude_obs.jsonl", "observations.jsonl"),
                         ("replay_2_claude_events.jsonl", "events.jsonl")):
            text = (FIX / src).read_text(encoding="utf-8").replace("{ROOT}", str(cls.root))
            (cls.root / ".hearmemory" / dst).write_text(text, encoding="utf-8")
        cls.cfg = load_config(cls.root)
        cls.client = ScriptedJev()
        judge = JevJudge(cls.cfg, store=cls.store, client=cls.client, environ={I.JEV_API_KEY_ENV: "test-key"})
        judge.capable, judge.reason = True, None
        run_pipeline(cls.store, cls.cfg, 30.0, True, "once", jev_judge=judge,
                     clock=lambda: epoch("2026-09-24T14:40:00"))
        cls.state = load_or_rebuild(cls.store, cls.cfg, allow_rebuild=True, now="2026-09-24T14:40:30.000000Z")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def _claim_of(self, obs_id: str):
        views = [v for v in self.state.claims.values() if v.claim.obs_id == obs_id]
        self.assertEqual(len(views), 1, [v.claim.text for v in views])
        return views[0]

    # ---- problem 1 -------------------------------------------------------------------------------------
    def test_worktree_run_is_the_project_suite_and_backs_subagent_As_claim(self):
        runs = self.state.stats["runs"]
        self.assertNotIn("pytest " + WT, runs)
        self.assertEqual(runs["pytest"]["last"]["obs_id"], A_RUN)       # A's run IS a run of the whole suite
        a_claim = self._claim_of(A_CLAIM_OBS)
        b1 = [c for c in self.store.iter_candidates()
              if c.template_id == "B1" and c.meta.get("claim_id") == a_claim.claim.claim_id]
        self.assertTrue(b1)
        self.assertIn(A_RUN, b1[-1].meta["evidence_obs_ids"])
        self.assertIn(A_RUN, b1[-1].meta["direct_support_ids"])
        self.assertNotEqual(a_claim.status, "insufficient")
        self.assertIn(a_claim.status, ("supported", "same_source_only"))

    # ---- problem 2 -------------------------------------------------------------------------------------
    def test_quoted_hearmemory_output_is_not_extracted(self):
        texts = [c.claim.text for c in self.state.claims.values()]
        self.assertNotIn(TAGGED_ECHO, self.state.claims)
        for t in texts:
            for tag in ("[SUPPORTED]", "[INSUFFICIENT]", "[REFUTED]", "o-a5f1283193ef1829", "c-7c2b0b6b1e289f61"):
                self.assertNotIn(tag, t)
        self.assertFalse([c for c in self.state.claims.values() if c.claim.obs_id == ECHO_OBS[1]])

    def test_restated_memory_is_derived_not_independent(self):
        cl = self.state.claims
        self.assertEqual(cl[CODEX_ECHO].derived_from, CODEX_CLAIM)
        self.assertEqual(cl[CODEX_RECHECK].derived_from, CODEX_CLAIM)
        self.assertEqual(cl[MAIN_ECHO].derived_from, CODEX_ECHO)
        for cid in (CODEX_ECHO, CODEX_RECHECK, MAIN_ECHO):
            self.assertNotEqual(cl[cid].status, "supported", cid)
            self.assertEqual(self.state.source_groups.get(cl[cid].claim.obs_id),
                             self.state.source_groups.get(cl[cl[cid].derived_from].claim.obs_id))
        self.assertIsNone(cl[CODEX_CLAIM].derived_from)
        self.assertEqual(cl[CODEX_CLAIM].status, "supported")          # the original keeps its own support
        self.assertEqual(self.state.stats["gates"].get("derived_from_shown_memory"), 3)

    # ---- problem 3 -------------------------------------------------------------------------------------
    def _brief(self, session: str, **cfg_brief):
        from hearmemory.memory.brief import build_brief
        cfg = json.loads(json.dumps(self.cfg))
        cfg["brief"].update(cfg_brief)
        req = I.BriefRequest(context=I.AgentContext(host="claude", session_id=session), purpose="session_start",
                             max_tokens=600, lang="en")
        return build_brief(self.state, self.store, req, cfg, now="2026-09-24T14:40:54.287717Z")

    def test_next_agents_brief_carries_subagent_Bs_finding_first(self):
        brief = self._brief("next-session-1")
        b_claim = self._claim_of(B_CLAIM_OBS)
        p3 = [it for it in brief.items if it.tier == "P3"]
        self.assertTrue(p3)
        self.assertEqual(p3[0].item_key, f"fact:{b_claim.claim.claim_id}:{b_claim.status}")
        self.assertIn("缺少负数、零、浮点数", brief.text)
        # the echoes are not "new facts from other agents"; the original Codex claim represents them
        keys = {it.item_key.split(":")[1] for it in p3}
        self.assertFalse(keys & {CODEX_ECHO, CODEX_RECHECK, MAIN_ECHO})
        self.assertIn(CODEX_CLAIM, keys)

    def test_finding_keeps_its_slot_when_only_one_fits(self):
        brief = self._brief("next-session-2", p3_max=1)
        self.assertEqual([it.refs[0] for it in brief.items if it.tier == "P3"], [B_CLAIM_OBS])

    def test_brief_records_what_it_showed(self):
        self._brief("next-session-3")
        evs = [e for e in self.store.iter_events() if e.kind == "memory_shown"
               and e.provenance and e.provenance.session_id == "next-session-3"]
        self.assertEqual(len(evs), 1)
        self.assertIn(CODEX_CLAIM, evs[0].data["claim_ids"])
        self.assertEqual(evs[0].data["via"], "brief:session_start")


class WorktreePaths(unittest.TestCase):
    """Problem 1, capture side: a cwd / path inside a linked worktree is the project's own path."""

    ROOT = "/p/hello-hearmemory"

    def test_targets(self):
        from hearmemory.testcmd import canonical_target, placed_target, stored_target
        wt = self.ROOT + "/" + WT
        self.assertEqual(placed_target("python -m pytest -q", self.ROOT, wt), "pytest")
        self.assertEqual(placed_target("pytest tests/test_calc.py -q", self.ROOT, wt), "pytest tests/test_calc.py")
        self.assertEqual(placed_target("pytest " + WT, self.ROOT, self.ROOT), "pytest")
        self.assertEqual(placed_target(f"pytest {wt}/tests/test_calc.py::test_add", self.ROOT, self.ROOT),
                         "pytest tests/test_calc.py::test_add")
        self.assertEqual(placed_target("cd tests && pytest", self.ROOT, wt), "pytest tests")
        self.assertEqual(canonical_target("pytest " + WT), "pytest")          # no root known: pure prefix rule
        # rows stored before the fix are re-keyed on read
        self.assertEqual(stored_target({"test_target": "pytest " + WT, "test_target_v": 2}), "pytest")
        # a directory that merely looks similar is not a worktree
        self.assertEqual(placed_target("pytest", self.ROOT, self.ROOT + "/claude/worktrees/x"),
                         "pytest claude/worktrees/x")

    def test_file_paths(self):
        from hearmemory.judge._compat import norm_relpath
        from hearmemory.testcmd import project_relpath
        self.assertEqual(project_relpath(f"{self.ROOT}/{WT}/src/calc.py", self.ROOT), "src/calc.py")
        self.assertEqual(project_relpath(f"{self.ROOT}/{WT}/src/calc.py", None), "src/calc.py")
        self.assertIsNone(project_relpath("/elsewhere/src/calc.py", self.ROOT))
        self.assertEqual(norm_relpath(f"{self.ROOT}/{WT}/tests/test_calc.py", self.ROOT), "tests/test_calc.py")

    def test_git_linked_worktree_outside_the_project(self):
        from hearmemory.testcmd import placed_target, project_relpath
        with tempfile.TemporaryDirectory() as tmp:
            root = make_fixed_calc_project(Path(tmp) / "proj")
            other = (Path(tmp) / "elsewhere-wt").resolve()
            _git(root, "worktree", "add", "-q", "--detach", str(other))
            self.assertEqual(placed_target("pytest -q", str(root), str(other)), "pytest")
            self.assertEqual(placed_target("pytest tests/test_calc.py", str(root), str(other)),
                             "pytest tests/test_calc.py")
            self.assertEqual(project_relpath(str(other / "src" / "calc.py"), str(root)), "src/calc.py")

    def test_claude_hook_in_a_worktree(self):
        from hearmemory.host import claude
        with tempfile.TemporaryDirectory() as tmp:
            root = make_fixed_calc_project(Path(tmp) / "hello-hearmemory")
            cwd = f"{root}/{WT}"
            base = {"session_id": SID, "agent_id": AGENT_A, "agent_type": "general-purpose", "cwd": cwd,
                    "_root": root, "_cfg": dict(I.DEFAULT_CONFIG), "hook_event_name": "PostToolUse"}
            run = claude.normalize("PostToolUse", dict(base, tool_name="Bash", tool_use_id="toolu_a",
                                                       tool_input={"command": "python -m pytest -q"},
                                                       tool_response={"stdout": "." + " " * 72 + "[100%]\n1 passed in 0.00s\n",
                                                                      "stderr": "", "interrupted": False}))[0]
            self.assertEqual(run.tool.test.target, "pytest")
            self.assertEqual(run.meta["test_target"], "pytest")
            read = claude.normalize("PostToolUse", dict(base, agent_id=AGENT_B, tool_name="Read", tool_use_id="toolu_b",
                                                        tool_input={"file_path": f"{cwd}/tests/test_calc.py"}))[0]
            self.assertEqual(read.paths, ["tests/test_calc.py"])


class EchoMask(unittest.TestCase):
    """Problem 2 (a): hearmemory output quoted by an agent is masked before sentences are cut."""

    def test_tag_and_id_lines_and_header_blocks(self):
        from hearmemory.judge.claims import mask_hearmemory_echo
        text = ("I checked `src/calc.py` myself and add() returns a + b.\n"
                "- `[SUPPORTED]` add() returns a + b — codex · session 01a0d3c3\n"
                "- 子 agent A（`obs_id o-a5f1283193ef1829`）：结果为 1 passed。\n"
                "\n"
                "[hearmemory recall] \"calc\" — 2 results.\n"
                "1. claim · codex · 5m ago · o-082e007b9733b1b1\n"
                "   add() was fixed and pytest passes\n"
                "\n"
                "Tests in tests/test_calc.py pass.")
        m = mask_hearmemory_echo(text)
        self.assertEqual(len(m), len(text))
        kept = [ln for ln in m.splitlines() if ln.strip()]
        self.assertEqual(kept, ["I checked `src/calc.py` myself and add() returns a + b.", "Tests in tests/test_calc.py pass."])
        self.assertEqual(mask_hearmemory_echo("plain text, no hearmemory output"), "plain text, no hearmemory output")
        # the brief's own header / tags are exactly what gets masked
        from hearmemory.memory import render as R
        self.assertTrue(R.t("en", "brief_header", n=1, pending="", asof="").startswith(I.HEARMEMORY_OUTPUT_MARKER))
        self.assertTrue(R.t("zh", "recall_header", q="", n=1, pending="", asof="").startswith(I.HEARMEMORY_OUTPUT_MARKER))


class DerivedRule(unittest.TestCase):
    """Problem 2 (b), pure: only a restatement of memory the author was SHOWN earlier is derived."""

    def _obs(self, oid, ts, host, sid, text):
        return I.Observation(id=oid, ts=ts, kind="claim" if host == "codex" else "assistant_message", event_key=oid,
                             provenance=I.Provenance(host=host, session_id=sid, source="hook:Stop"), text=text)

    def _build(self, shown: bool):
        from hearmemory.memory.build import build_state
        text = "已将 src/calc.py 的 add() 从减法修正为加法，python -m pytest -q 测试通过（1 passed）。"
        o1 = self._obs("o-0000000000000001", "2026-09-24T14:00:00.000000Z", "codex", "s1", text)
        o2 = self._obs("o-0000000000000002", "2026-09-24T14:10:00.000000Z", "claude", "s2",
                       "上一会话把 src/calc.py 的 add() 从减法修正为加法，python -m pytest -q 测试通过。")
        c1 = I.Claim(claim_id="c-0000000000000001", obs_id=o1.id, span=[0, len(o1.text)], text=o1.text,
                     claim_class="status", explicit=True)
        c2 = I.Claim(claim_id="c-0000000000000002", obs_id=o2.id, span=[0, len(o2.text)], text=o2.text,
                     claim_class="status")
        evs = []
        if shown:
            evs.append(I.ControlEvent(id="ev-shown-1", ts="2026-09-24T14:05:00.000000Z", kind="memory_shown",
                                      target="claude:s2", data={"claim_ids": [c1.claim_id], "via": "brief:session_start"},
                                      provenance=I.Provenance(host="claude", session_id="s2")))
        return build_state([o1, o2], [c1, c2], [], [], evs, now="2026-09-24T14:20:00.000000Z")

    def test_shown_then_restated(self):
        st = self._build(True)
        self.assertEqual(st.claims["c-0000000000000002"].derived_from, "c-0000000000000001")
        self.assertIsNone(st.claims["c-0000000000000001"].derived_from)

    def test_not_shown_is_independent(self):
        st = self._build(False)
        self.assertIsNone(st.claims["c-0000000000000002"].derived_from)


class StatusReportsTheWorkersJev(unittest.TestCase):
    """Problem 4: `hearmemory status` from a shell without the key reports the live worker's Jev state."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = make_fixed_calc_project(Path(self._tmp.name) / "s")
        self.store = create_store(self.root)
        self.cfg = load_config(self.root)
        self.state_file = self.root / ".hearmemory" / "state" / "worker.json"

    def _status(self):
        from hearmemory.render_cli import render_status
        env = {k: v for k, v in os.environ.items() if k != I.JEV_API_KEY_ENV}
        with mock.patch.dict(os.environ, env, clear=True):
            info = commands.do_status(self.root, self.store, self.cfg)
        return info, render_status(info)

    def test_live_worker_with_jev(self):
        from hearmemory.judge import _compat as C
        self.state_file.write_text(json.dumps({
            "pid": os.getpid(), "started_ts": "2026-09-24T14:33:14.643170Z", "jev_capable": True,
            "jev_unavailable_reason": None, "mode": "daemon", "launched_by": "orchestrator",
            "last_beat_ts": "2026-09-24T14:44:04.067570Z",
            "jev": {"capable": True, "reason": None, "model": "jev-1.13.0", "day": "2026-09-24", "calls_today": 26,
                    "last_call_ts": "2026-09-24T14:37:03.250316Z"}}))
        lock = C.FLock(str(self.root / ".hearmemory"), "worker")
        self.assertTrue(lock.acquire(1.0))
        self.addCleanup(lock.release)
        with mock.patch("hearmemory.judge.worker.is_hearmemory_worker", return_value=True):
            info, text = self._status()
        self.assertEqual(info["jev"]["source"], "worker")
        self.assertTrue(info["jev"]["capable"])
        self.assertEqual(info["jev"]["this_shell"]["reason"], "no_key")
        line = [ln for ln in text.splitlines() if ln.strip().startswith("jev:")][0]
        self.assertIn(f"available (worker pid {os.getpid()}, jev-1.13.0, 26 calls today)", line)
        self.assertIn("this shell: no_key", line)
        self.assertNotIn("unavailable", line)

    def test_no_live_worker_falls_back_to_this_shell_labelled(self):
        self.state_file.write_text(json.dumps({"pid": 999999, "started_ts": "x", "jev_capable": True, "mode": "daemon",
                                               "exited_ts": "2026-09-24T14:44:04.441558Z"}))
        info, text = self._status()
        self.assertEqual(info["jev"]["source"], "this_shell")
        self.assertEqual(info["jev"]["reason"], "no_key")
        self.assertIn("jev:          unavailable (this shell: no_key; no live worker)", text)

    def test_worker_persists_its_jev_tally(self):
        from hearmemory.judge.worker import run_worker
        calls = iter([{"jev_judgments": 3, "new_obs": 1}])

        def fake_pipeline(store, cfg, deadline, use_jev, mode, **kw):
            return next(calls, {"jev_judgments": 0})
        run_worker(self.root, "once", launched_by="test", pipeline_fn=fake_pipeline, install_signals=False,
                   environ={}, clock=lambda: epoch("2026-09-24T14:37:00"))
        info = json.loads(self.state_file.read_text())
        self.assertEqual(info["jev"]["model"], self.cfg["jev"]["model"])
        self.assertEqual(info["jev"]["calls_today"], 3)
        self.assertEqual(info["jev"]["day"], "2026-09-24")
        self.assertEqual(info["jev"]["last_call_ts"][:19], "2026-09-24T14:37:00")
        self.assertIn("reason", info["jev"])


if __name__ == "__main__":
    unittest.main()
