"""every template x label consumer behaviour, no-judgment = no write, verified only from rules."""
import unittest

from test_mem_support import (CLAUDE, CODEX, CURSOR, FakeStore, cand_a3, cand_b1, cand_pair, claim, event,
                              judg, obs, prov, run_obs)

import hearmemory.interfaces as I
from hearmemory.memory.build import MemoryBuilder


def build(store, cfg=None):
    return MemoryBuilder().build(store, cfg or {}, now="2026-09-24T12:00:00.000000Z")


def two_mentions():
    """Two observations from different actors, each mentioning sync.py (A1 / A2 endpoints)."""
    a = obs("codex:m1", "assistant_message", "The failure comes from sync.py in the ledger job.", prov(**CODEX), 1)
    b = obs("claude:m2", "assistant_message", "ledger/sync.py drops the tz offset.", prov(**CLAUDE), 2)
    na = I.span_node(a.id, [a.text.find("sync.py"), a.text.find("sync.py") + 7])
    nb = I.span_node(b.id, [0, 14])
    return a, b, na, nb


class A1Consumer(unittest.TestCase):
    def _run(self, label, provider="jev", rule_id=None, cfg=None, extra=()):
        a, b, na, nb = two_mentions()
        c = cand_pair("A1", na, nb, 3)
        st = FakeStore().add(a, b, c, judg(c, label, 4, provider=provider, rule_id=rule_id), *extra)
        return build(st, cfg), na, nb

    def test_same_is_provisional_edge(self):
        s, na, nb = self._run("same")
        (e,) = s.edges.values()
        self.assertEqual((e.relation, e.status), ("same_object", "provisional"))
        self.assertEqual({e.a, e.b}, {na, nb})

    def test_same_by_rule_is_verified(self):
        s, _, _ = self._run("same", provider="rule", rule_id="A1_same_resolved_path")
        self.assertEqual([e.status for e in s.edges.values()], ["verified"])

    def test_model_can_never_verify_even_with_a_rule_id(self):
        s, _, _ = self._run("same", provider="jev", rule_id="A1_same_resolved_path")
        self.assertEqual([e.status for e in s.edges.values()], ["provisional"])

    def test_different_marks_and_disputes_an_earlier_same(self):
        a, b, na, nb = two_mentions()
        c1 = cand_pair("A1", na, nb, 3)
        c2 = cand_pair("A1", na, nb, 5, meta={"v": 2})
        st = FakeStore().add(a, b, c1, c2, judg(c1, "same", 4), judg(c2, "different", 6))
        s = build(st)
        self.assertEqual([m.kind for m in s.marks.values()], ["distinct_object"])
        (e,) = s.edges.values()
        self.assertEqual(e.status, "disputed")
        self.assertIn("merge_guard", [h.reason for h in e.history])

    def test_unresolved_marks_pending_and_opens_issue_only_when_configured(self):
        s, _, _ = self._run("unresolved")
        self.assertEqual([m.kind for m in s.marks.values()], ["pending_alignment"])
        self.assertEqual(s.issues, {})
        s2, _, _ = self._run("unresolved", cfg={"issues": {"from_unresolved_alignment": True}})
        self.assertEqual(len(s2.issues), 1)


class A2Consumer(unittest.TestCase):
    def _nodes(self):
        a = run_obs("codex:r1", prov(**CODEX), 1, failed=1, passed=2, failed_ids=["tests/test_recon.py::test_tz"],
                    target="pytest tests/test_recon.py")
        b = obs("claude:m", "assistant_message", "test_tz fails with KeyError: 'tz' in recon", prov(**CLAUDE), 5)
        return a, b, I.span_node(a.id, [0, 20]), I.span_node(b.id, [0, 20])

    def test_labels(self):
        for label, provider, rule, expect in (("same_event", "jev", None, ("edge", "provisional")),
                                              ("same_event", "rule", "A2_shared_run_id", ("edge", "verified")),
                                              ("different_events", "jev", None, ("mark", "distinct_event")),
                                              ("unresolved", "jev", None, ("mark", "pending_event_alignment"))):
            a, b, na, nb = self._nodes()
            c = cand_pair("A2", na, nb, 6)
            s = build(FakeStore().add(a, b, c, judg(c, label, 7, provider=provider, rule_id=rule)))
            if expect[0] == "edge":
                self.assertEqual([(e.relation, e.status) for e in s.edges.values()], [("same_event", expect[1])])
            else:
                self.assertEqual([m.kind for m in s.marks.values()], [expect[1]])
                self.assertEqual(s.edges, {})


class A3Consumer(unittest.TestCase):
    def _claims(self):
        oa = obs("codex:m", "assistant_message", "The root cause is that sync.py drops the tz offset.", prov(**CODEX), 1)
        ob = obs("cursor:m", "assistant_message", "Root cause: sync.py drops the timezone offset.", prov(**CURSOR), 2)
        return oa, ob, claim(oa, oa.text), claim(ob, ob.text)

    def _build(self, la, lb, gates=(), only_one=False):
        oa, ob, ca, cb = self._claims()
        c1 = cand_a3(ca, cb, 3, "a_contains_b", gates)
        c2 = cand_a3(ca, cb, 3, "b_contains_a", gates)
        recs = [oa, ob, ca, cb, c1, c2, judg(c1, la, 4)]
        if not only_one:
            recs.append(judg(c2, lb, 5))
        return build(FakeStore().add(*recs)), ca, cb

    def test_restates_both_ways_is_equivalence_and_one_source(self):
        s, ca, cb = self._build("restates", "restates")
        self.assertEqual([e.relation for e in s.edges.values()], ["restates"])
        self.assertEqual(s.claims[ca.claim_id].equivalents, [cb.claim_id])
        self.assertEqual(s.claims[cb.claim_id].equivalents, [ca.claim_id])
        self.assertEqual(s.source_groups[ca.obs_id], s.source_groups[cb.obs_id])

    def test_single_direction_is_never_enough(self):
        s, ca, cb = self._build("restates", None, only_one=True)
        self.assertEqual((s.edges, s.marks), ({}, {}))
        self.assertEqual(s.stats["dropped"].get("a3_single_direction"), 1)

    def test_one_way_restates_is_covered_by(self):
        s, ca, cb = self._build("restates", "partial")
        self.assertEqual([m.kind for m in s.marks.values()], ["covered_by"])
        self.assertEqual(s.claims[cb.claim_id].covered_by, ca.claim_id)

    def test_generalizes_is_directed_retrieval_edge(self):
        s, ca, cb = self._build("generalizes", "not_contained")
        (e,) = s.edges.values()
        self.assertEqual((e.relation, e.directed), ("generalizes", True))
        self.assertEqual(s.claims[ca.claim_id].equivalents, [])

    def test_partial_and_not_contained_do_nothing(self):
        s, _, _ = self._build("partial", "not_contained")
        self.assertEqual((s.edges, s.marks), ({}, {}))

    def test_scope_mismatch_gate_blocks_equivalence(self):
        s, ca, _ = self._build("restates", "restates", gates=("A3_scope_mismatch",))
        self.assertEqual((s.edges, s.claims[ca.claim_id].equivalents), ({}, []))
        self.assertEqual(s.stats["gates"]["a3_scope_mismatch"], 1)


class B1Consumer(unittest.TestCase):
    def _setup(self):
        say = obs("codex:m", "assistant_message", "The root cause is that sync.py drops the tz offset.",
                  prov(**CODEX), 1)
        c = claim(say, say.text, paths=["ledger/sync.py"])
        ev = run_obs("claude:r", prov(**CLAUDE), 5, target="pytest tests/test_sync.py", paths=["tests/test_sync.py"])
        cand = cand_b1(c, [ev.id], 6)
        return say, c, ev, cand

    def test_labels_to_status(self):
        for label, status, issue_kinds in (("supports", "supported", []), ("refutes", "refuted", []),
                                           ("both", "disputed", ["disputed_claim"]),
                                           ("insufficient", "insufficient", ["unverified_conclusion"])):
            say, c, ev, cand = self._setup()
            s = build(FakeStore().add(say, ev, c, cand, judg(cand, label, 7)))
            cv = s.claims[c.claim_id]
            self.assertEqual(cv.status, status, label)
            self.assertEqual(cv.judged_label, label)
            self.assertEqual(sorted(i.kind for i in s.issues.values()), issue_kinds)
            if label in ("refutes", "both"):
                self.assertEqual(cv.counter_ids, [ev.id])
            if label == "both":
                self.assertEqual(list(s.issues.values())[0].status, "disputed")

    def test_insufficient_non_conclusion_opens_no_issue(self):
        say = obs("codex:m", "assistant_message", "sync.py uses LEDGER_TZ for the offset.", prov(**CODEX), 1)
        c = claim(say, say.text, cls="other")
        ev = run_obs("claude:r", prov(**CLAUDE), 5)
        cand = cand_b1(c, [ev.id], 6)
        s = build(FakeStore().add(say, ev, c, cand, judg(cand, "insufficient", 7)))
        self.assertEqual((s.claims[c.claim_id].status, s.issues), ("insufficient", {}))

    def test_outdated_only_from_the_program_rule(self):
        say, c, ev, cand = self._setup()
        s = build(FakeStore().add(say, ev, c, cand, judg(cand, "outdated", 7, provider="rule",
                                                          rule_id="B1_test_status_changed")))
        self.assertEqual(s.claims[c.claim_id].status, "outdated")
        say, c, ev, cand = self._setup()
        s = build(FakeStore().add(say, ev, c, cand, judg(cand, "outdated", 7, provider="jev")))
        self.assertEqual(s.claims[c.claim_id].status, "unjudged")

    def test_premise_refuted_marks_parent_and_opens_premise_gap(self):
        say = obs("codex:m", "assistant_message",
                  "The bug is fixed in sync.py because LEDGER_TZ is always set in CI.", prov(**CODEX), 1)
        parent = claim(say, "The bug is fixed in sync.py because LEDGER_TZ is always set in CI.")
        prem = claim(say, "LEDGER_TZ is always set in CI", cls="premise", parent=parent.claim_id)
        ev = obs("claude:grep", "search", "ci.yml: env: {} # no LEDGER_TZ", prov(**CLAUDE), 3,
                 tool=I.ToolInfo(name="Grep", paths=[".github/ci.yml"], status="ok"))
        cand = cand_b1(prem, [ev.id], 4)
        s = build(FakeStore().add(say, ev, parent, prem, cand, judg(cand, "refutes", 5)))
        pv = s.claims[parent.claim_id]
        self.assertEqual((pv.status, pv.premise_status, pv.premise_claim_ids), ("unjudged", "refuted", [prem.claim_id]))
        self.assertEqual([i.kind for i in s.issues.values()], ["premise_gap"])

    def test_no_judgment_rows_write_nothing(self):
        for outcome in ("transport_error", "validation_error", "budget_blocked", "disabled", "fallback_detected",
                        "permission_denied"):
            say, c, ev, cand = self._setup()
            a, b, na, nb = two_mentions()
            pc = cand_pair("A1", na, nb, 3)
            s = build(FakeStore().add(say, ev, c, cand, a, b, pc, judg(cand, "refutes", 7, outcome=outcome),
                                      judg(pc, "same", 7, outcome=outcome)))
            self.assertEqual(s.claims[c.claim_id].status, "unjudged", outcome)
            self.assertEqual((s.edges, s.marks, s.issues), ({}, {}, {}), outcome)
            self.assertEqual(s.stats["counts"]["decisions_applied"], 0)

    def test_label_outside_template_is_ignored(self):
        say, c, ev, cand = self._setup()
        s = build(FakeStore().add(say, ev, c, cand, judg(cand, "same", 7)))
        self.assertEqual(s.claims[c.claim_id].status, "unjudged")

    def test_judgment_override_beats_any_judgment(self):
        say, c, ev, cand = self._setup()
        s = build(FakeStore().add(say, ev, c, cand, judg(cand, "refutes", 7),
                                  event("judgment_override", cand.candidate_id, 3, {"label": "supports"})))
        self.assertEqual(s.claims[c.claim_id].judged_label, "supports")

    def test_latest_judgment_wins_and_superseded_candidates_ignored(self):
        say, c, ev, cand = self._setup()
        ev2 = run_obs("cursor:r", prov(**CURSOR), 9, target="pytest tests/test_sync.py", passed=4)
        newer = cand_b1(c, [ev.id, ev2.id], 10, supersedes=cand.candidate_id)
        s = build(FakeStore().add(say, ev, ev2, c, cand, newer, judg(newer, "supports", 11),
                                  judg(cand, "refutes", 12)))   # the old candidate judged late: ignored
        self.assertEqual(s.claims[c.claim_id].judged_label, "supports")
        self.assertEqual(s.stats["dropped"]["superseded_decision"], 1)


if __name__ == "__main__":
    unittest.main()
