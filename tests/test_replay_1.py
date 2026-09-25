"""Replay 1: regression tests for the problems seen in a real run (Codex session A, 2026-09-24).

What happened for real: Codex fixed `return a - b` -> `return a + b` in src/calc.py (apply_patch as an
exec_command heredoc), ran `python -m pytest -q` (1 passed) and recorded, via the hearmemory_record MCP tool,
"已将 src/calc.py 的 add() 运算符从减号改为加号，运行 python -m pytest -q 测试通过（1 passed）。".
After `hearmemory import codex` + one worker pass, Jev saw the patch as a raw command plus the pre-fix `sed`
output, answered a low-confidence "both" (p=0.40, supports 0.03, confidence 0.21) and the true claim became
[DISPUTED] with an open issue; the pytest result was never offered as evidence; the MCP record (session
codex-<epoch>-<pid>) and the rollout (session 01a0d392-...) were two different agents.

`RealScenario` rebuilds that session as a TRIMMED, SYNTHETIC rollout (only the relevant lines, no user
config / AGENTS.md content) and runs the real importer, MCP-side record, extractor, (fake) Jev with the
real answer, and memory builder. Every other class pins one of the fixes.
"""
from __future__ import annotations

import importlib.util
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
from hearmemory import commands  # noqa: E402
from hearmemory.config import load_config, set_config_value  # noqa: E402
from hearmemory.store import create_store  # noqa: E402

REAL_CLAIM = "已将 src/calc.py 的 add() 运算符从减号改为加号，运行 python -m pytest -q 测试通过（1 passed）。"
REAL_ANSWER = {"type": "choice", "choice": "both", "confidence": 0.21,
               "probabilities": {"refutes": 0.25, "insufficient": 0.32, "supports": 0.03, "both": 0.40}}
ROLLOUT_SID = "01a0d392-358e-7e52-a543-ccb62e40192f"
GUARD_SID = "01a0d392-3669-7a71-b4d5-fc0b3f2f9d8b"
PATCH_CMD = ("apply_patch <<'PATCH'\n*** Begin Patch\n*** Update File: src/calc.py\n@@\n def add(a, b):\n"
             "-    return a - b\n+    return a + b\n*** End Patch\nPATCH")


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True, text=True)


def make_calc_project(root: Path) -> Path:
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (root / "tests" / "test_calc.py").write_text(
        "from src.calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


def iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch)) + ".%03dZ" % int((epoch % 1) * 1000)


def exec_call(ts: float, call_id: str, cmd: str, root: Path) -> Dict[str, Any]:
    return {"timestamp": iso(ts), "type": "response_item",
            "payload": {"type": "function_call", "name": "exec_command", "call_id": call_id,
                        "arguments": json.dumps({"cmd": cmd, "workdir": str(root)})}}


def call_output(ts: float, call_id: str, output: Any) -> Dict[str, Any]:
    return {"timestamp": iso(ts), "type": "response_item",
            "payload": {"type": "function_call_output", "call_id": call_id, "output": output}}


def agent_msg(ts: float, text: str) -> Dict[str, Any]:
    return {"timestamp": iso(ts), "type": "event_msg", "payload": {"type": "agent_message", "message": text}}


def session_a_before_record(root: Path, t0: float) -> List[Dict[str, Any]]:
    """Trimmed reconstruction of the real rollout up to (not including) the hearmemory_record call."""
    return [
        {"timestamp": iso(t0), "type": "session_meta",
         "payload": {"id": ROLLOUT_SID, "timestamp": iso(t0), "cwd": str(root), "originator": "codex_exec",
                     "cli_version": "0.154.0", "source": "exec", "thread_source": "user"}},
        {"timestamp": iso(t0 + 1), "type": "turn_context",
         "payload": {"cwd": str(root), "model": "gpt-5-codex", "approvals_reviewer": "auto_review"}},
        {"timestamp": iso(t0 + 2), "type": "event_msg",
         "payload": {"type": "user_message", "message": "打开 src/calc.py 把 add() 的减号改成加号，运行 python -m "
                                                        "pytest -q，再用 hearmemory_record 记一条结论。"}},
        agent_msg(t0 + 3, "记忆为空；现在查看 `src/calc.py` 的实现并定位 `add()`。"),
        exec_call(t0 + 4, "call_sed", "sed -n '1,120p' src/calc.py", root),
        call_output(t0 + 4.1, "call_sed", "Chunk ID: 3e57a5\nWall time: 0.0000 seconds\nProcess exited with code 0\n"
                                          "Original token count: 8\nOutput:\ndef add(a, b):\n    return a - b\n"),
        exec_call(t0 + 10, "call_patch", PATCH_CMD, root),
        call_output(t0 + 10.1, "call_patch", "Exit code: 0\nWall time: 0.1 seconds\nOutput:\nSuccess. Updated the "
                                             "following files:\nM src/calc.py\n"),
        exec_call(t0 + 14, "call_pytest", "python -m pytest -q", root),
        call_output(t0 + 14.1, "call_pytest", "Chunk ID: 18c02b\nWall time: 0.0575 seconds\nProcess exited with "
                                              "code 0\nOriginal token count: 25\nOutput:\n."
                                              + " " * 72 + "[100%]\n1 passed in 0.00s\n"),
        agent_msg(t0 + 18, "测试已通过（1 passed）；我现在记录这次改动和验证结果。"),
    ]


def session_a_after_record(t: float, obs_id: str) -> List[Dict[str, Any]]:
    # the MCP call's exact 0.154 shape was not visible in the recorded data; a namespaced name with a
    # content-list output (both of which the old importer missed) is used here
    return [
        {"timestamp": iso(t), "type": "response_item",
         "payload": {"type": "function_call", "name": "hearmemory_record", "namespace": "mcp__hearmemory__",
                     "call_id": "call_rec", "arguments": json.dumps({"text": REAL_CLAIM, "kind": "claim"})}},
        call_output(t + 0.2, "call_rec", [{"type": "text", "text": I.RECORD_ECHO_PREFIX + obs_id}]),
        agent_msg(t + 4, "已完成：将 `src/calc.py` 中 `add()` 的减号改为加号；`python -m pytest -q`：`1 passed`。"),
    ]


def guardian_rollout(root: Path, t0: float) -> List[Dict[str, Any]]:
    """The approval reviewer session Codex runs next to the main one (model codex-auto-review)."""
    return [
        {"timestamp": iso(t0), "type": "session_meta",
         "payload": {"id": GUARD_SID, "timestamp": iso(t0), "cwd": str(root), "originator": "codex_exec",
                     "cli_version": "0.154.0", "source": "exec"}},
        {"timestamp": iso(t0 + 0.1), "type": "turn_context", "payload": {"cwd": str(root), "model": "codex-auto-review"}},
        {"timestamp": iso(t0 + 0.2), "type": "event_msg",
         "payload": {"type": "user_message", "message": "The following is the Codex agent history whose request "
                                                        "action you are assessing. ... src/calc.py ..."}},
        agent_msg(t0 + 5, '{"risk_level":"low","outcome":"allow","rationale":"read-only src/calc.py lookup"}'),
    ]


def write_jsonl(path: Path, rows: List[Dict[str, Any]], mode: str = "w") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, mode, encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


class FakeJevClient:
    """Answers every question with the REAL Jev answer of test 1 and keeps the states it was asked."""

    def __init__(self, answer: Dict[str, Any]) -> None:
        self.answer = answer
        self.states: List[Dict[str, Any]] = []

    def system_one(self, state, questions, timeout=None):
        self.states.append(json.loads(json.dumps(state)))
        return {"answers": {I.QUESTION_KEY: dict(self.answer)}, "model": I.JEV_MODEL_DEFAULT,
                "usage": {"input_tokens": 896, "output_tokens": 49}}


def fake_judge(store, cfg, client):
    from hearmemory.judge.jev import JevJudge
    j = JevJudge(cfg, store=store, client=client, environ={I.JEV_API_KEY_ENV: "test-key"})
    j.capable, j.reason = True, None       # no SDK needed: the client is injected
    return j


class RealScenario(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.root = make_calc_project(base / "hello-hearmemory").resolve()
        self.home = base / "codex-home"
        self.store = create_store(self.root)
        self.assertTrue(set_config_value(self.root, "import", "codex_home", str(self.home)))
        # no background worker racing the test's own pipeline pass for the pipeline lock
        self.assertTrue(set_config_value(self.root, "worker", "spawn_from_hooks", False))
        self.cfg = load_config(self.root)
        self.addCleanup(self._stop_worker)
        now = time.time()
        self.launch = f"codex-{int(now) - 60}-81139"           # what launch.sh exports as HEARMEMORY_SESSION_ID
        self.t0 = int(now) - 55                                   # the rollout starts ~5 s after the launch
        day = self.home / "sessions" / "2026" / "09" / "24"
        self.rollout = day / f"rollout-2026-09-24T21-19-29-{ROLLOUT_SID}.jsonl"
        write_jsonl(self.rollout, session_a_before_record(self.root, self.t0))
        write_jsonl(day / f"rollout-2026-09-24T21-19-43-{GUARD_SID}.jsonl", guardian_rollout(self.root, self.t0 + 14))

    def _stop_worker(self) -> None:
        from hearmemory.judge.worker import stop_worker
        stop_worker(self.root, 5.0)

    def _record_claim_like_mcp(self) -> str:
        res = commands.do_record(self.root, self.store, self.cfg, text=REAL_CLAIM, kind="claim", paths=["src/calc.py"],
                                 host="codex", session_id=self.launch, source="mcp")
        write_jsonl(self.rollout, session_a_after_record(self.t0 + 25, res["obs_id"]), mode="a")
        return res["obs_id"]

    def _pipeline(self, client) -> Dict[str, Any]:
        from hearmemory.judge.worker import run_pipeline
        return run_pipeline(self.store, self.cfg, deadline_s=20.0, use_jev=True, mode="once",
                            jev_judge=fake_judge(self.store, self.cfg, client))

    def test_true_claim_is_not_disputed_and_sees_its_test_run(self) -> None:
        claim_obs = self._record_claim_like_mcp()
        client = FakeJevClient(REAL_ANSWER)
        st = self._pipeline(client)
        self.assertNotIn("error", st, st)

        b1 = [c for c in self.store.iter_candidates() if c.template_id == "B1"]
        self.assertTrue(b1, "no B1 question for the claim")
        cand = b1[-1]
        texts = [e["text"] for e in cand.state["evidence"]]
        joined = "\n".join(texts)
        # problem 2: the session's own `python -m pytest -q` -> 1 passed is direct evidence
        self.assertTrue(any("1 passed" in t and "pytest" in t for t in texts), texts)
        # problem 1: the edit is a before -> after diff with BOTH sides, not a head-truncated command
        edit = [t for t in texts if t.startswith("edit src/calc.py")]
        self.assertEqual(len(edit), 1, texts)
        self.assertIn("- ", edit[0])
        self.assertIn("return a - b", edit[0])
        self.assertIn("+ ", edit[0])
        self.assertIn("return a + b", edit[0])
        self.assertNotIn("apply_patch <<", joined)
        # the pre-fix `sed` output (old code, read BEFORE the edit) is not evidence about the claim
        self.assertNotIn("sed -n", joined)
        meta = cand.meta
        self.assertTrue(meta["direct_support_ids"], meta)
        self.assertTrue(meta["implementing_edit_ids"], meta)
        self.assertTrue(client.states, "Jev was never asked")

        # the real low-confidence "both" can no longer flip the claim to DISPUTED / open an issue
        from hearmemory.memory.build import load_or_rebuild
        state = load_or_rebuild(self.store, self.cfg, allow_rebuild=True)
        views = [v for v in state.claims.values() if v.claim.obs_id == claim_obs]
        self.assertEqual(len(views), 1)
        self.assertNotIn(views[0].status, ("disputed", "refuted"), views[0].to_dict())
        self.assertIn(views[0].status, ("supported", "same_source_only"))
        self.assertFalse([i for i in state.issues.values() if i.kind == "disputed_claim"])
        self.assertNotIn("edit", " ".join(views[0].counter_ids))

    def test_one_codex_session_is_one_actor(self) -> None:
        claim_obs = self._record_claim_like_mcp()
        from hearmemory.judge.worker import run_pipeline
        run_pipeline(self.store, self.cfg, deadline_s=20.0, use_jev=False, mode="once")
        # problem 3: the launch session id is an alias of the rollout session
        self.assertEqual(commands.native_session_id(self.store, "codex", self.launch), ROLLOUT_SID)
        amap = I.ActorMap()
        for ev in sorted(self.store.iter_events(), key=lambda e: (e.ts, e.id)):
            amap.add_event(ev)
        obs = {o.id: o for _, o in self.store.iter_observations()}
        self.assertEqual(amap.actor_of(obs[claim_obs]), f"codex:{ROLLOUT_SID}")
        pytest_obs = [o for o in obs.values() if o.kind == "command" and "pytest" in (o.text or "")]
        self.assertEqual(len(pytest_obs), 1)
        self.assertEqual(amap.actor_of(pytest_obs[0]), amap.actor_of(obs[claim_obs]))
        # the approval-reviewer sidecar session is not a second agent
        self.assertFalse([o for o in obs.values() if o.provenance.session_id == GUARD_SID])
        # the apply_patch heredoc is a file edit, not a command
        edits = [o for o in obs.values() if o.kind == "file_edit"]
        self.assertEqual([o.paths for o in edits], [["src/calc.py"]])
        self.assertFalse([o for o in obs.values() if o.kind == "command" and "apply_patch" in (o.text or "")])
        # the brief for this Codex session never shows its own run / claim as "from other agents"
        brief = commands.do_recall(self.root, self.store, self.cfg, brief=True, host="codex", session_id=self.launch,
                                   purpose="session_start")
        p3_refs = {r for it in brief.items if it.tier == "P3" for r in it.refs}
        self.assertNotIn(pytest_obs[0].id, p3_refs)
        self.assertNotIn(claim_obs, p3_refs)

    def test_recall_judges_inline_when_no_worker_is_alive(self) -> None:
        # problem 4: nobody ran `hearmemory worker --once`, the worker is not alive -> recall runs a bounded pass
        self._record_claim_like_mcp()
        self.assertFalse(list(self.store.iter_candidates()))
        commands.do_recall(self.root, self.store, self.cfg, query="calc", host="codex", session_id=self.launch)
        self.assertTrue([c for c in self.store.iter_candidates() if c.template_id == "B1"])

    def test_record_imports_the_sessions_earlier_lines_first(self) -> None:
        # problem 5: a Codex record / recall imports pending rollout lines itself (no manual `hearmemory import`)
        self._record_claim_like_mcp()
        kinds = [o.kind for _, o in self.store.iter_observations()]
        self.assertIn("file_edit", kinds)
        self.assertIn("command", kinds)
        self.assertLess(kinds.index("command"), kinds.index("claim"))


class RealObservationsReplay(unittest.TestCase):
    """The five relevant observations of the real run (trimmed, sanitised fixture, imported by the OLD importer: the
    patch is a `command`), replayed through the fixed extractor + the real Jev answer: no dispute."""

    def test_legacy_rows_no_longer_dispute_the_claim(self):
        import shutil
        from hearmemory.judge.worker import run_pipeline
        from hearmemory.memory.build import load_or_rebuild
        with tempfile.TemporaryDirectory() as tmp:
            root = make_calc_project(Path(tmp) / "hello-hearmemory").resolve()
            store = create_store(root)
            set_config_value(root, "import", "codex_home", str(Path(tmp) / "none"))
            set_config_value(root, "worker", "spawn_from_hooks", False)
            shutil.copy(REPO / "tests" / "fixtures" / "replay_1_session_a_obs.jsonl",
                        root / ".hearmemory" / "observations.jsonl")
            cfg = load_config(root)
            client = FakeJevClient(REAL_ANSWER)
            run_pipeline(store, cfg, 20.0, True, "once", jev_judge=fake_judge(store, cfg, client),
                         clock=lambda: 1790256128.0)                       # 2026-09-24T13:22:08Z, as for real
            self.assertEqual(len(client.states), 1)
            texts = [e["text"] for e in client.states[0]["evidence"]]
            self.assertTrue(texts[0].startswith("$ python -m pytest -q") and "1 passed" in texts[0], texts)
            self.assertTrue(texts[1].startswith("edit src/calc.py (before -> after):"), texts)
            self.assertIn("+     return a + b", texts[1])
            self.assertFalse([t for t in texts if "sed -n" in t])
            state = load_or_rebuild(store, cfg, allow_rebuild=True)
            (view,) = state.claims.values()
            self.assertIn(view.status, ("supported", "same_source_only"))
            self.assertEqual(view.counter_ids, [])
            self.assertFalse(state.issues)


class B1LabelGate(unittest.TestCase):
    """Problem 1, memory side: how a model's refutes / both becomes DISPUTED."""

    def _cand(self, **meta):
        m = {"claim_id": "c-1", "evidence_obs_ids": ["o-run", "o-edit"]}
        m.update(meta)
        return I.Candidate(candidate_id="k-1", template_id="B1", template_version="hm-B1.1", subject_key="claim:c-1",
                           state={}, input_hash="h", basis_obs_ids=["o-claim", "o-run", "o-edit"], created_ts="t",
                           meta=m)

    def _j(self, label, probs=None, provider="jev"):
        return I.Judgment(judgment_id="j-1", candidate_id="k-1", template_id="B1", template_version="hm-B1.1",
                          input_hash="h", provider=provider, outcome="valid", ts="2026-09-24T13:22:11Z", label=label,
                          probabilities=probs or {})

    def _claim(self):
        return I.Claim(claim_id="c-1", obs_id="o-claim", span=[0, 10], text=REAL_CLAIM, claim_class="status")

    def test_real_answer_without_program_support_is_insufficient(self):
        from hearmemory.memory.ops import decision_from_judgment, consume_b1
        d = decision_from_judgment(self._cand(), self._j("both", REAL_ANSWER["probabilities"]))
        self.assertEqual((d.label, d.gate), ("insufficient", "b1_negative_not_decisive"))
        ops = consume_b1(d, self._claim())
        self.assertEqual([o.kind for o in ops], ["claim_status_set"])
        self.assertEqual(ops[0].target["status"], "insufficient")

    def test_real_answer_with_program_support_is_supports(self):
        from hearmemory.memory.ops import decision_from_judgment
        d = decision_from_judgment(self._cand(direct_support_ids=["o-run"]), self._j("both", REAL_ANSWER["probabilities"]))
        self.assertEqual(d.label, "supports")

    def test_decisive_refutes_against_program_support_is_at_most_disputed(self):
        from hearmemory.memory.ops import decision_from_judgment
        d = decision_from_judgment(self._cand(direct_support_ids=["o-run"]),
                                   self._j("refutes", {"refutes": 0.9, "supports": 0.02, "both": 0.05, "insufficient": 0.03}))
        self.assertEqual((d.label, d.gate), ("both", "b1_refutes_vs_program_support"))

    def test_decisive_both_with_real_counter_evidence_still_disputes(self):
        from hearmemory.memory.ops import decision_from_judgment, consume_b1
        d = decision_from_judgment(self._cand(implementing_edit_ids=["o-edit"]),
                                   self._j("both", {"both": 0.8, "supports": 0.1, "refutes": 0.05, "insufficient": 0.05}))
        ops = consume_b1(d, self._claim())
        self.assertEqual(ops[0].target["status"], "disputed")
        self.assertEqual(ops[0].target["counter_ids"], ["o-run"])      # the implementing edit is never counter
        self.assertEqual(ops[1].kind, "issue_open")

    def test_only_implementing_evidence_is_no_dispute(self):
        from hearmemory.memory.ops import decision_from_judgment, consume_b1
        c = self._cand(implementing_edit_ids=["o-edit"], direct_support_ids=["o-run"])
        d = decision_from_judgment(c, self._j("both", {"both": 0.9, "supports": 0.05, "refutes": 0.03, "insufficient": 0.02}))
        ops = consume_b1(d, self._claim())
        self.assertEqual([o.kind for o in ops], ["claim_status_set"])
        self.assertEqual(ops[0].target["status"], "supported")

    def test_rules_and_old_rows_are_not_gated(self):
        from hearmemory.memory.ops import decision_from_judgment
        self.assertEqual(decision_from_judgment(self._cand(), self._j("refutes", provider="rule")).label, "refutes")
        self.assertEqual(decision_from_judgment(self._cand(), self._j("refutes")).label, "refutes")   # no probabilities


class EditRendering(unittest.TestCase):
    def test_compact_diff_keeps_both_sides(self):
        from hearmemory.textutil import compact_diff, edit_summary
        seg = "\n@@\n def add(a, b):\n-    return a - b\n+    return a + b"
        out = compact_diff(seg, 800, focus=["add"], path="src/calc.py")
        self.assertEqual(out.splitlines()[0], "edit src/calc.py (before -> after):")
        self.assertIn("-     return a - b", out)
        self.assertIn("+     return a + b", out)
        self.assertEqual(edit_summary(seg, "src/calc.py"), 'src/calc.py: "return a - b" -> "return a + b"')

    def test_long_change_keeps_the_added_side(self):
        from hearmemory.textutil import compact_diff
        seg = "@@\n" + "\n".join("-old line %d" % i for i in range(300)) + "\n" + "\n".join(
            "+new line %d" % i for i in range(300))
        out = compact_diff(seg, 400)
        self.assertLessEqual(len(out), 400)
        self.assertIn("- old line 0", out)
        self.assertIn("+ new line 0", out)

    def test_evidence_line_for_an_edit(self):
        from hearmemory.memory.render import evidence_text
        idx = {"o-e": {"kind": "file_edit", "edit_summary": 'src/calc.py: "return a - b" -> "return a + b"',
                       "host": "codex", "ts": "2026-09-24T13:20:01Z"}}
        line = evidence_text("o-e", idx, "2026-09-24T13:30:00Z")
        self.assertIn('"return a - b" -> "return a + b"', line)


class ClaimTargets(unittest.TestCase):
    def test_bare_test_command_in_a_chinese_claim(self):
        from hearmemory.judge.claims import command_targets
        self.assertEqual(command_targets(REAL_CLAIM), ["pytest"])
        self.assertEqual(command_targets("ran pytest tests/test_x.py::test_a -q, it fails"),
                         ["pytest tests/test_x.py::test_a"])


class ImportCli(unittest.TestCase):
    def _run(self, *args, env=None):
        full = dict(os.environ)
        full["PYTHONPATH"] = str(REPO / "src")
        full.pop(I.JEV_API_KEY_ENV, None)
        full.update(env or {})
        return subprocess.run([sys.executable, "-m", "hearmemory", "--project", str(self.root), *args], cwd=str(self.root),
                              env=full, capture_output=True, text=True, timeout=60)

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = make_calc_project(Path(self._tmp.name) / "p").resolve()
        self.home = Path(self._tmp.name) / "ch"
        r = self._run("init", "--hosts", "claude,codex,cursor")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.addCleanup(self._stop)
        write_jsonl(self.home / "sessions" / "2026" / "09" / "24" / f"rollout-x-{ROLLOUT_SID}.jsonl",
                    session_a_before_record(self.root, time.time() - 30))

    def _stop(self):
        from hearmemory.judge.worker import stop_worker
        stop_worker(self.root, 5.0)

    def test_dry_run_says_would_import(self):
        # problem 6
        r = self._run("import", "codex", "--dry-run", "--codex-home", str(self.home))
        self.assertEqual(r.returncode, 0, r.stderr)
        # prompt + 2 assistant messages + sed + pytest commands + the patch as ONE file edit
        self.assertIn("would import 6 observation(s)", r.stdout)
        self.assertIn("dry run", r.stdout)
        self.assertNotIn("imported", r.stdout)
        self.assertFalse((self.root / ".hearmemory" / "observations.jsonl").exists()
                         and (self.root / ".hearmemory" / "observations.jsonl").read_text().strip())
        r = self._run("import", "codex", "--codex-home", str(self.home))
        self.assertIn("imported 6 observation(s)", r.stdout)

    def test_enabled_hosts_match_what_init_installed(self):
        # problem 7
        cfg = load_config(self.root)
        self.assertEqual(cfg["hosts"]["enabled"], ["claude", "codex", "cursor"])
        self.assertTrue((self.root / ".cursor").exists())
        text = (self.root / ".hearmemory" / "config.toml").read_text(encoding="utf-8")
        self.assertIn("# hearmemory config.toml", text)          # the rest of the file is kept as it was


class WorkerLiveness(unittest.TestCase):
    """Problem 4: `worker --daemon` really is a daemon, and status never reports a dead worker."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = make_calc_project(Path(self._tmp.name) / "w").resolve()
        create_store(self.root)
        self.addCleanup(self._stop)

    def _stop(self):
        from hearmemory.judge.worker import stop_worker
        stop_worker(self.root, 5.0)

    def test_stale_worker_json_is_not_running(self):
        from hearmemory.judge.worker import worker_status
        from hearmemory.render_cli import worker_line
        state = self.root / ".hearmemory" / "state" / "worker.json"
        state.write_text(json.dumps({"pid": 0, "started_ts": "", "jev_capable": False, "mode": "daemon",
                                     "last_spawn_ts": "2026-09-24T13:14:00Z"}))
        live = worker_status(self.root)["liveness"]
        self.assertEqual(live["state"], "stopped")
        self.assertEqual(worker_line(live), "not running")
        state.write_text(json.dumps({"pid": 999999, "started_ts": "x", "jev_capable": True, "mode": "daemon"}))
        live = worker_status(self.root)["liveness"]
        self.assertEqual(live["state"], "stopped")
        self.assertIn("not running (last worker pid 999999", worker_line(live))

    def test_spawned_daemon_survives_and_writes_its_pid(self):
        from hearmemory.judge.worker import spawn_background, worker_status
        env = {k: v for k, v in os.environ.items() if k != I.JEV_API_KEY_ENV}
        env["PYTHONPATH"] = str(REPO / "src")
        # without Jev the daemon is one pass by design (single_pass); what matters here is that the process
        # spawn_background starts is the real worker: it takes the lock, writes ITS pid, and marks its exit
        cfg = load_config(self.root)
        argv_seen = []

        def popen(argv):
            argv_seen.append(list(argv))
            return subprocess.Popen(list(argv) + ["--no-jev"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True, env=env)
        self.assertTrue(spawn_background(self.root, "launch.sh", cfg=cfg, popen=popen))
        self.assertIn("--daemon", argv_seen[0])
        deadline = time.time() + 15
        live = {}
        while time.time() < deadline:
            live = worker_status(self.root)["liveness"]
            info = json.loads((self.root / ".hearmemory" / "state" / "worker.json").read_text())
            if info.get("pid"):
                break
            time.sleep(0.1)
        self.assertGreater(info.get("pid") or 0, 0, "the spawned worker never wrote its own pid")
        self.assertEqual(info.get("launched_by"), "launch.sh")
        # --no-jev = not capable -> one pass and exit (single_pass), and then it says so honestly
        deadline = time.time() + 15
        while time.time() < deadline and worker_status(self.root)["liveness"]["state"] != "stopped":
            time.sleep(0.1)
        live = worker_status(self.root)["liveness"]
        self.assertEqual(live["state"], "stopped")
        self.assertTrue(live["exited_ts"])

    @unittest.skipUnless(importlib.util.find_spec("typesafe_sdk"), "needs typesafe_sdk for a capable worker")
    def test_capable_daemon_stays_running_until_stopped(self):
        from hearmemory.judge.worker import spawn_background, stop_worker, worker_status
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO / "src")
        env[I.JEV_API_KEY_ENV] = "fake-key-no-network-needed"   # nothing to judge -> no call is ever made

        def popen(argv):
            return subprocess.Popen(list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True, env=env)
        self.assertTrue(spawn_background(self.root, "launch.sh", cfg=load_config(self.root), popen=popen))
        deadline = time.time() + 15
        while time.time() < deadline and worker_status(self.root)["liveness"]["state"] != "running":
            time.sleep(0.1)
        live = worker_status(self.root)["liveness"]
        self.assertEqual(live["state"], "running", live)
        self.assertGreater(live["pid"], 0)
        time.sleep(1.5)                                          # the launcher is long gone; the daemon is not
        self.assertEqual(worker_status(self.root)["liveness"]["state"], "running")
        self.assertTrue(stop_worker(self.root, 5.0))
        self.assertEqual(worker_status(self.root)["liveness"]["state"], "stopped")

    def test_daemon_cli_runs_the_worker_loop(self):
        # `hearmemory worker --daemon` must be the singleton worker (lock + pid), not one pipeline pass
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO / "src")
        env.pop(I.JEV_API_KEY_ENV, None)
        r = subprocess.run([sys.executable, "-m", "hearmemory", "--project", str(self.root), "worker", "--daemon",
                            "--launched-by", "test"], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        info = json.loads((self.root / ".hearmemory" / "state" / "worker.json").read_text())
        self.assertGreater(info["pid"], 0)
        self.assertEqual(info["launched_by"], "test")
        self.assertEqual(info["mode"], "single_pass")        # no key here: one pass, then exit
        self.assertTrue(info.get("exited_ts"))


class LaunchScript(unittest.TestCase):
    """Problem 5: launch.sh runs codex as a child, imports its session afterwards, keeps the exit code."""

    def test_launch_sh_imports_after_codex_exits_and_keeps_the_exit_code(self):
        from hearmemory.host import snippets as S
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = make_calc_project(base / "p").resolve()
            create_store(root)
            script = base / "launch.sh"
            wrapper = base / "py"
            wrapper.write_text("#!/bin/sh\nPYTHONPATH=%s exec %s \"$@\"\n" % (REPO / "src", sys.executable))
            wrapper.chmod(0o755)
            script.write_text(S.codex_launch_sh(str(wrapper), str(root)))
            fake_bin = base / "bin"
            fake_bin.mkdir()
            (fake_bin / "codex").write_text("#!/bin/sh\necho \"$HEARMEMORY_SESSION_ID\" > \"$FAKE_OUT\"\nexit 3\n")
            (fake_bin / "codex").chmod(0o755)
            env = dict(os.environ)
            env.pop(I.JEV_API_KEY_ENV, None)
            env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
            env["FAKE_OUT"] = str(base / "sid")
            env["CODEX_HOME"] = str(base / "codex-home")
            self.assertNotIn("exec codex", script.read_text())
            r = subprocess.run(["sh", str(script), "--x"], env=env, capture_output=True, text=True, timeout=120)
            try:
                self.assertEqual(r.returncode, 3, r.stderr)
                sid = (base / "sid").read_text().strip()
                self.assertTrue(sid.startswith("codex-"))
                reg = json.loads((root / ".hearmemory" / "state" / "codex_launches.json").read_text())
                self.assertIn(sid, reg["launches"])
                self.assertTrue(reg["launches"][sid].get("ended"), "the post-exit import never ran")
            finally:
                from hearmemory.judge.worker import stop_worker
                stop_worker(root, 5.0)


class McpRecordShapes(unittest.TestCase):
    """Problem 3: the record link is found whatever shape this Codex version logs the MCP call in."""

    def _import(self, rows):
        from hearmemory.host.codex import import_rollouts
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "p"
            root.mkdir()
            store = create_store(root)
            home = Path(tmp) / "h"
            meta = {"timestamp": iso(time.time() - 20), "type": "session_meta",
                    "payload": {"id": "sess-x", "cwd": str(root.resolve()), "source": "cli"}}
            write_jsonl(home / "sessions" / "rollout-sess-x.jsonl", [meta] + rows)
            import_rollouts(store, load_config(root), codex_home=str(home))
            return [e for e in store.iter_events() if e.kind == "provenance_link"]

    def test_item_completed_mcp_tool_call(self):
        oid = "o-00112233445566aa"
        evs = self._import([{"timestamp": iso(time.time() - 10), "type": "event_msg",
                             "payload": {"type": "item_completed", "item": {
                                 "type": "McpToolCall", "server": "hearmemory", "tool": "hearmemory_record",
                                 "result": {"content": [{"type": "text", "text": I.RECORD_ECHO_PREFIX + oid}]}}}}])
        self.assertEqual([(e.target, e.provenance.session_id) for e in evs], [(oid, "sess-x")])

    def test_namespaced_function_call_with_content_list_output(self):
        oid = "o-00112233445566bb"
        evs = self._import([
            {"timestamp": iso(time.time() - 10), "type": "response_item",
             "payload": {"type": "function_call", "name": "hearmemory.hearmemory_record", "call_id": "c1", "arguments": "{}"}},
            call_output(time.time() - 9, "c1", [{"type": "text", "text": I.RECORD_ECHO_PREFIX + oid}])])
        self.assertEqual([e.target for e in evs], [oid])


class SessionAssignment(unittest.TestCase):
    def test_each_rollout_goes_to_the_latest_earlier_launch(self):
        from hearmemory.host.codex import assign_launches
        launches = {"codex-100-1": {"epoch": 100.0}, "codex-200-2": {"epoch": 200.0, "ended": 400.0}}
        rollouts = [{"session_id": "ra", "start": 104.0}, {"session_id": "rb", "start": 203.0},
                    {"session_id": "late", "start": 1000.0}]
        out = assign_launches(launches, rollouts)
        self.assertEqual([r["session_id"] for r in out["codex-100-1"]], ["ra"])
        self.assertEqual([r["session_id"] for r in out["codex-200-2"]], ["rb"])   # "late" is after launch 2 ended


if __name__ == "__main__":
    unittest.main()
