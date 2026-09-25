"""Replay 5: regression tests for a real run (Codex -> Claude) where a change claim lost its defining edit on re-judgment.

What happened: Codex session A added `sub(a, b)` to src/calc.py plus `test_sub`, ran its tests together with the
commit (`python -m pytest -q && git add -A && git commit ...`, 12 passed) and recorded "Added sub(a, b) returning
a - b with a test; python -m pytest -q passes all 12 tests." (its dirty paths: tests/test_calc.py). B1 was judged
supports 0.88 (evidence: its own run + its own edits). An independent Claude pytest run then re-opened the
question; the 3-item evidence budget held the two runs + the test-file edit, the edit ADDING `sub` was crowded
out, and the model said supports 0.37 -> shown [WEAK SUPPORT], later [UNVERIFIED].

Fixed: (1) the evidence of a claim naming code identifiers always keeps an edit that defines them (runs fill the
other slots, the newest independent run first); (2) a later, weaker model judgment (weak support / insufficient)
never silently downgrades an earlier support -- only a refutation, a dispute or the outdated rule does.
The observations below are a minimal sanitised reconstruction of that run.
"""
import dataclasses
import shutil
import unittest

from test_judge_support import T0, Clock, default_cfg, edit, make_project, obs, prov, run
from test_mem_support import CLAUDE, CODEX, FakeStore, cand_b1, claim, edit_obs, judg, run_obs
from test_mem_support import obs as mem_obs
from test_mem_support import prov as mem_prov

from hearmemory import interfaces as I
from hearmemory.judge.extract import ExtractIndex, Extractor
from hearmemory.judge.project_index import ProjectIndex
from hearmemory.memory.build import MemoryBuilder

CALC = "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"
TESTS = ("from src.calc import add, sub\n\n\ndef test_add():\n    assert add(2, 3) == 5\n\n\n"
         "def test_sub():\n    assert sub(5, 3) == 2\n")
CLAIM = "Added sub(a, b) returning a - b with a test; python -m pytest -q passes all 12 tests."
OUT = "............                                                             [100%]\n12 passed in 0.02s"


class EvidenceKeepsDefiningEdit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = make_project({"src/calc.py": CALC, "tests/test_calc.py": TESTS})
        cls.cfg = default_cfg()
        cls.idx = ProjectIndex.build(cls.root, cls.cfg)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def scenario(self, claim_prov):
        cx = prov("codex", "cx-a", source="import:codex_rollout")
        cl = prov("claude", "cl-1", sub="ag1", sub_type="general-purpose")
        self.e_test = edit("tests/test_calc.py", "--- a/tests/test_calc.py\n+++ b/tests/test_calc.py\n@@\n"
                           "-from src.calc import add\n+from src.calc import add, sub\n@@\n+def test_sub():\n"
                           "+    assert sub(5, 3) == 2\n+    assert sub(0, 4) == -4\n", 0, cx)
        self.e_calc = edit("src/calc.py", "--- a/src/calc.py\n+++ b/src/calc.py\n@@\n def add(a, b):\n"
                           "     return a + b\n+\n+\n+def sub(a, b):\n+    return a - b\n", 10, cx)
        self.run_a = run("python -m pytest -q && git add -A && git commit -m 'add sub'", OUT, 20, cx, passed=12)
        self.claim = obs("claim", CLAIM, 30, claim_prov, tool=I.ToolInfo(name="record", paths=["tests/test_calc.py"]))
        self.run_c = run("python -m pytest -q", OUT, 300, cl, passed=12)
        objs = {o.id: o for o in (self.e_test, self.e_calc, self.run_a, self.claim, self.run_c)}
        xi = ExtractIndex()
        r1 = Extractor(self.idx, self.cfg, xindex=xi, clock=Clock(T0 + 60), project="p").extract(
            [self.e_test, self.e_calc, self.run_a, self.claim])
        r2 = Extractor(self.idx, self.cfg, xindex=xi, clock=Clock(T0 + 400), project="p",
                       fetch=lambda i, s: objs.get(i)).extract([self.run_c])
        b1 = [[c for c in r.candidates if c.template_id == "B1"] for r in (r1, r2)]
        self.assertEqual([len(x) for x in b1], [1, 1])
        return b1[0][0], b1[1][0]

    def test_rejudge_keeps_the_edit_adding_sub(self):
        first, again = self.scenario(prov("codex", "cx-a", source="cli"))
        self.assertIn(self.e_calc.id, first.meta["evidence_obs_ids"])
        self.assertEqual(again.meta["trigger"], "evidence")
        # the newest (independent) run, the edit defining `sub`, then the author's own run
        self.assertEqual(again.meta["evidence_obs_ids"], [self.run_c.id, self.e_calc.id, self.run_a.id])
        texts = [e["text"] for e in again.state["evidence"]]
        self.assertTrue(any("def sub(a, b):" in t for t in texts), texts)

    def test_unlinked_record_also_gets_the_defining_edit(self):
        first, again = self.scenario(prov("claude", "cli-unlinked", source="cli"))
        self.assertIn(self.e_calc.id, first.meta["evidence_obs_ids"])
        self.assertIn(self.e_calc.id, again.meta["evidence_obs_ids"])
        self.assertEqual(again.meta["evidence_obs_ids"][0], self.run_c.id)


def _jev(cand, label, minutes, conf, **probs):
    return dataclasses.replace(judg(cand, label, minutes), confidence=conf, probabilities=dict(probs))


class NoSilentDowngrade(unittest.TestCase):
    def setUp(self):
        self.run_a = run_obs("codex:run", mem_prov(**CODEX), 1, target="pytest", passed=12)
        self.e_calc = edit_obs("codex:calc", mem_prov(**CODEX), 0, "src/calc.py", "+def sub(a, b):\n+    return a - b")
        self.run_c = run_obs("claude:run", mem_prov(**CLAUDE), 5, target="pytest", passed=12)
        self.say = mem_obs("codex:claim", "claim", CLAIM, mem_prov(**CODEX, source="cli"), 2)
        self.c = claim(self.say, CLAIM, cls="status")
        self.first = cand_b1(self.c, [self.run_c.id, self.e_calc.id], 3)
        self.again = cand_b1(self.c, [self.run_c.id, self.run_a.id], 6, supersedes=self.first.candidate_id)
        self.store = FakeStore().add(self.run_a, self.e_calc, self.run_c, self.say, self.c, self.first, self.again,
                                     _jev(self.first, "supports", 4, 0.88, supports=0.88, insufficient=0.12))

    def status(self, *extra):
        s = MemoryBuilder().build(self.store.add(*extra), {}, now="2026-09-24T12:00:00.000000Z")
        return s.claims[self.c.claim_id]

    def test_weaker_support_keeps_supported(self):
        cv = self.status(_jev(self.again, "supports", 7, 0.37, supports=0.37, insufficient=0.6))
        self.assertEqual(cv.status, "supported")
        self.assertIn(self.e_calc.id, cv.support_ids)
        self.assertTrue(any(h.change == "status supported kept" for h in cv.history), cv.history)

    def test_insufficient_keeps_supported(self):
        cv = self.status(_jev(self.again, "insufficient", 7, 0.7, supports=0.25, insufficient=0.71))
        self.assertEqual(cv.status, "supported")

    def test_decisive_refutation_still_applies(self):
        cv = self.status(_jev(self.again, "refutes", 7, 0.9, refutes=0.9, supports=0.05))
        self.assertEqual(cv.status, "refuted")


if __name__ == "__main__":
    unittest.main()
