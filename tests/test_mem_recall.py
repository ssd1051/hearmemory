"""recall - BM25 + context, tags, counter-evidence with refuted items, A3 representative, archive."""
import unittest

from test_mem_support import (CLAUDE, CODEX, CURSOR, FakeStore, cand_a3, cand_b1, cand_pair, claim, ctx, judg,
                              obs, prov, run_obs, ts)

import hearmemory.interfaces as I
from hearmemory.memory.build import MemoryBuilder
from hearmemory.memory.recall import recall

NOW = ts(120)


class Recall(unittest.TestCase):
    def setUp(self):
        st = FakeStore()
        say = obs("codex:m", "assistant_message", "The root cause is that ledger/sync.py drops the tz offset.",
                  prov(**CODEX), 1)
        self.bad = claim(say, say.text, paths=["ledger/sync.py"])
        self.run = run_obs("claude:r", prov(**CLAUDE), 20, target="pytest tests/test_sync.py", passed=6,
                           paths=["tests/test_sync.py", "ledger/sync.py"])
        cand = cand_b1(self.bad, [self.run.id], 21)
        other = obs("cursor:m", "assistant_message", "Payments export uses export_csv.py and is unrelated.",
                    prov(**CURSOR), 30)
        self.say, self.other = say, other
        st.add(say, self.run, other, self.bad, cand, judg(cand, "refutes", 22))
        self.st = st
        self.state = MemoryBuilder().build(st, {}, now=NOW)

    def test_query_ranks_and_tags_refuted_with_counter_evidence(self):
        r = recall(self.state, self.st, I.RecallQuery(query="tz offset sync"), {}, now=NOW)
        self.assertEqual(r.items[0].obs_id, self.say.id)
        self.assertEqual(r.items[0].tags, ["[REFUTED]"])
        self.assertIn("counter-evidence: test `pytest tests/test_sync.py` passed", r.text)
        self.assertNotIn(self.other.id, [i.obs_id for i in r.items])
        self.assertTrue(r.text.startswith('[hearmemory recall] "tz offset sync" — '))

    def test_context_only_recall_and_limit(self):
        r = recall(self.state, self.st, I.RecallQuery(query="", context=ctx(paths=["ledger/sync.py"]), limit=1), {},
                   now=NOW)
        self.assertEqual(len(r.items), 1)

    def test_archive_hidden_unless_requested(self):
        st = self.st
        old = obs("old:n", "note", "legacy exporter notes about export_csv.py", prov(**CURSOR), 0)
        st.add(old)
        s = MemoryBuilder().build(st, {}, now=ts(60 * 24 * 60))
        self.assertEqual(s.tiers.get(old.id), "archive")
        r = recall(s, st, I.RecallQuery(query="exporter export_csv"), {}, now=ts(60 * 24 * 60))
        self.assertNotIn(old.id, [i.obs_id for i in r.items])
        r = recall(s, st, I.RecallQuery(query="exporter export_csv", include_archive=True), {}, now=ts(60 * 24 * 60))
        item = [i for i in r.items if i.obs_id == old.id][0]
        self.assertTrue(item.archived)
        self.assertIn("[ARCHIVED]", item.tags)

    def test_a3_equivalent_shows_representative_only(self):
        st = FakeStore()
        oa = obs("codex:m", "assistant_message", "Root cause: ledger/sync.py drops the tz offset.", prov(**CODEX), 1)
        ob = obs("cursor:m", "assistant_message", "The root cause is ledger/sync.py dropping the tz offset.",
                 prov(**CURSOR), 2)
        ca, cb = claim(oa, oa.text), claim(ob, ob.text)
        k1, k2 = cand_a3(ca, cb, 3, "a_contains_b"), cand_a3(ca, cb, 3, "b_contains_a")
        st.add(oa, ob, ca, cb, k1, k2, judg(k1, "restates", 4), judg(k2, "restates", 5))
        s = MemoryBuilder().build(st, {}, now=NOW)
        r = recall(s, st, I.RecallQuery(query="root cause tz offset sync"), {}, now=NOW)
        self.assertEqual(len(r.items), 1)

    def test_one_hop_alignment_expansion(self):
        st = self.st
        na = I.span_node(self.say.id, [23, 37])
        nb = I.span_node(self.run.id, [0, 20])
        c = cand_pair("A1", na, nb, 40)
        st.add(c, judg(c, "same", 41))
        s = MemoryBuilder().build(st, {}, now=NOW)
        r = recall(s, st, I.RecallQuery(query="root cause drops tz"), {}, now=NOW)
        top = r.items[0]
        self.assertEqual(top.related, [self.run.id])
        self.assertIn("(may be the same object)", r.text)

    def test_pending_judgments_reported(self):
        st = self.st
        st.add(cand_b1(self.bad, [self.run.id], 50, extra_state={"v": 2}))
        s = MemoryBuilder().build(st, {}, now=NOW)
        r = recall(s, st, I.RecallQuery(query="sync"), {}, now=NOW)
        self.assertEqual(r.pending_judgments, 1)
        self.assertIn("(1 judgments pending)", r.text)


if __name__ == "__main__":
    unittest.main()
