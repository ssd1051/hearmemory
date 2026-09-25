"""memory brief - priorities, budget, dedupe, empty brief, outdated, stale header, push, zh."""
import unittest

from test_mem_support import (CLAUDE, CLAUDE_SUB, CODEX, CURSOR, FakeStore, cand_a3, cand_b1, claim,
                              ctx, event, judg, link, obs, prov, run_obs, ts)

import hearmemory.interfaces as I
from hearmemory.memory.brief import build_brief
from hearmemory.memory.build import MemoryBuilder, load_or_rebuild

NOW = ts(120)


def scenario():
    """Codex claims a root cause, Claude refutes it with a run; an open issue; Cursor states a supported fact."""
    st = FakeStore()
    say = obs("codex:m1", "assistant_message", "The root cause is that ledger/sync.py drops the tz offset.",
              prov(**CODEX), 1)
    bad = claim(say, say.text, paths=["ledger/sync.py"])
    run = run_obs("claude:r1", prov(**CLAUDE_SUB), 20, target="pytest tests/test_sync.py", passed=6,
                  paths=["tests/test_sync.py", "ledger/sync.py"])
    c1 = cand_b1(bad, [run.id], 21)
    note = obs("cli:issue", "note", "Does nightly-recon read LEDGER_TZ from ledger/config.toml?", prov(**CODEX), 30,
               paths=["ledger/config.toml"])
    iid = I.issue_id_for("manual", "does nightly-recon read ledger_tz?")
    fact_msg = obs("cursor:m", "assistant_message", "LedgerSyncWorker in ledger/worker.py retries 3 times before failing.",
                   prov(**CURSOR), 60)
    fact = claim(fact_msg, fact_msg.text, paths=["ledger/worker.py"])
    frun = run_obs("claude:r2", prov(**CLAUDE), 61, target="pytest tests/test_worker.py", passed=2,
                   paths=["tests/test_worker.py", "ledger/worker.py"])
    c2 = cand_b1(fact, [frun.id], 62)
    st.add(say, run, note, fact_msg, frun, bad, fact, c1, c2, judg(c1, "refutes", 22), judg(c2, "supports", 63),
           event("issue_open", iid, 30, {"obs_id": note.id, "title": "Does nightly-recon read LEDGER_TZ?",
                                         "paths": ["ledger/config.toml"]}))
    return st, bad, fact, iid, run


def state_of(st, now=NOW):
    return MemoryBuilder().build(st, {}, now=now)


class Priorities(unittest.TestCase):
    def test_tiers_order_and_counter_evidence(self):
        st, bad, fact, iid, run = scenario()
        s = state_of(st)
        req = I.BriefRequest(context=ctx(**{"host": "claude", "session": "s-new"}, paths=["ledger/sync.py",
                                                                                        "ledger/config.toml",
                                                                                        "ledger/worker.py"]))
        b = build_brief(s, st, req, {}, now=NOW)
        tiers = [i.tier for i in b.items]
        self.assertEqual(tiers, sorted(tiers))
        self.assertEqual(set(tiers), {"P1", "P2", "P3"})
        text = b.text
        self.assertTrue(text.startswith("[hearmemory] Shared project memory — "))
        self.assertLess(text.index("Refuted / disputed:"), text.index("Open issues:"))
        self.assertLess(text.index("Open issues:"), text.index("New from other agents:"))
        self.assertIn('[REFUTED] "The root cause is that ledger/sync.py drops the tz offset."', text)
        self.assertRegex(text, r"counter-evidence: test `pytest tests/test_sync.py` passed \(6 passed\) \(claude")
        self.assertIn("[ISSUE i-", text)
        self.assertIn("[SUPPORTED] \"LedgerSyncWorker", text)
        self.assertIn("codex · session 019f36b5 · 1h ago · a1b2c3d", text)
        self.assertLessEqual(b.token_estimate, req.max_tokens)
        # the run already cited as counter-evidence is not repeated as a separate P3 fact
        self.assertNotIn(f"run:pytest tests/test_sync.py:{run.id}", [i.item_key for i in b.items])
        # the refuted tag never appears without the counter-evidence line right after it
        lines = text.splitlines()
        for i, ln in enumerate(lines):
            if "[REFUTED]" in ln:
                self.assertIn("counter-evidence:", lines[i + 1])

    def test_author_learns_refutation_even_without_relevant_context(self):
        st, bad, *_ = scenario()
        s = state_of(st)
        b = build_brief(s, st, I.BriefRequest(context=ctx(host="codex", session="019f36b5-codex",
                                                          paths=["docs/unrelated.md"])), {}, now=NOW)
        self.assertEqual([i.item_key for i in b.items if i.tier == "P1"], [f"claim:{bad.claim_id}:refuted"])

    def test_linked_proxy_author_is_notified(self):
        st = FakeStore()
        say = obs("mcp:x", "claim", "tests/test_sync.py::test_tz fails on main", prov(host="claude",
                  session="mcp-1", source="mcp"), 1)
        c = claim(say, say.text, cls="status", paths=["tests/test_sync.py"])
        run = run_obs("codex:r", prov(**CODEX), 5, passed=4, paths=["tests/test_sync.py"])
        cand = cand_b1(c, [run.id], 6)
        st.add(say, run, c, cand, judg(cand, "refutes", 7), link(say.id, prov(**CLAUDE_SUB), 1))
        s = state_of(st)
        me = ctx(host="claude", session="s-claude-1", sub="agent-7", paths=["README.md"])
        other = ctx(host="claude", session="s-claude-1", paths=["README.md"])
        self.assertEqual(len(build_brief(s, st, I.BriefRequest(context=me), {}, now=NOW).items), 1)
        self.assertEqual(build_brief(s, None, I.BriefRequest(context=other), {}, now=NOW).text, "")

    def test_own_facts_are_not_news_but_other_agents_facts_are(self):
        st, bad, fact, iid, run = scenario()
        s = state_of(st)
        mine = build_brief(s, st, I.BriefRequest(context=ctx(host="cursor", session="conv-42",
                                                             paths=["ledger/worker.py"])), {}, now=NOW)
        self.assertNotIn(f"fact:{fact.claim_id}:supported", [i.item_key for i in mine.items])
        theirs = build_brief(s, st, I.BriefRequest(context=ctx(host="codex", session="other",
                                                               paths=["ledger/worker.py"])), {}, now=NOW)
        self.assertIn(f"fact:{fact.claim_id}:supported", [i.item_key for i in theirs.items])


class BudgetAndDedupe(unittest.TestCase):
    def test_budget_counts_header_and_drops_what_does_not_fit(self):
        st, *_ = scenario()
        s = state_of(st)
        wide = ctx(host="claude", session="s-b", paths=["ledger/sync.py", "ledger/config.toml", "ledger/worker.py"])
        full = build_brief(s, None, I.BriefRequest(context=wide, max_tokens=600), {}, now=NOW)
        small = build_brief(s, None, I.BriefRequest(context=wide, max_tokens=full.token_estimate - 30), {}, now=NOW)
        self.assertLess(len(small.items), len(full.items))
        self.assertTrue(small.dropped_for_budget)
        self.assertLessEqual(small.token_estimate, full.token_estimate - 30)
        self.assertEqual(build_brief(s, None, I.BriefRequest(context=wide, max_tokens=20), {}, now=NOW).text, "")

    def test_session_dedupe_and_p1_reminders(self):
        st, bad, fact, iid, run = scenario()
        s = state_of(st)
        c = ctx(host="claude", session="s-d", paths=["ledger/sync.py", "ledger/worker.py"])
        first = build_brief(s, st, I.BriefRequest(context=c), {}, now=NOW)
        keys = [i.item_key for i in first.items]
        self.assertIn(f"claim:{bad.claim_id}:refuted", keys)
        self.assertIn("sessions/s-d", st.state)
        second = build_brief(s, st, I.BriefRequest(context=c), {}, now=NOW)
        self.assertEqual([i.item_key for i in second.items], [f"claim:{bad.claim_id}:refuted"])   # reminder 1
        third = build_brief(s, st, I.BriefRequest(context=c), {}, now=NOW)
        self.assertEqual(len(third.items), 1)                                                     # reminder 2
        fourth = build_brief(s, st, I.BriefRequest(context=c), {}, now=NOW)
        self.assertEqual(fourth.text, "")
        # a status change is a new item key -> shown again
        c3 = cand_b1(bad, [run.id], 100, extra_state={"v": 2})
        st.add(c3, judg(c3, "both", 101))
        again = build_brief(state_of(st), st, I.BriefRequest(context=c), {}, now=NOW)
        self.assertIn(f"claim:{bad.claim_id}:disputed", [i.item_key for i in again.items])

    def test_empty_memory_gives_empty_brief(self):
        st = FakeStore()
        s = state_of(st)
        b = build_brief(s, st, I.BriefRequest(context=ctx()), {}, now=NOW)
        self.assertEqual((b.text, b.items), ("", []))
        self.assertEqual(st.state_writes, [])


class Statuses(unittest.TestCase):
    def test_outdated_is_neither_p1_nor_a_fact(self):
        st = FakeStore()
        say = obs("codex:m", "assistant_message", "tests/test_x.py::test_tz fails with KeyError in src/x.py",
                  prov(**CODEX), 1)
        c = claim(say, say.text, cls="status", paths=["src/x.py", "tests/test_x.py"])
        later = run_obs("claude:r", prov(**CLAUDE), 30, target="pytest tests/test_x.py", passed=3,
                        paths=["tests/test_x.py"])
        cand = cand_b1(c, [later.id], 31)
        st.add(say, later, c, cand, judg(cand, "outdated", 32, provider="rule", rule_id="B1_test_status_changed"))
        s = state_of(st)
        self.assertEqual(s.claims[c.claim_id].status, "outdated")
        b = build_brief(s, None, I.BriefRequest(context=ctx(host="cursor", session="c", paths=["src/x.py",
                                                                                               "tests/test_x.py"])),
                        {}, now=NOW)
        self.assertNotIn("[OUTDATED]", b.text)
        self.assertNotIn("[REFUTED]", b.text)
        self.assertFalse([i for i in b.items if i.item_key.startswith("claim:") or i.item_key.startswith("fact:")])

    def test_unjudged_conclusion_is_unverified_fact(self):
        st = FakeStore()
        say = obs("codex:m", "assistant_message", "Fixed: ledger/sync.py now keeps the tz offset.", prov(**CODEX), 1)
        c = claim(say, say.text, paths=["ledger/sync.py"])
        st.add(say, c)
        b = build_brief(state_of(st), None, I.BriefRequest(context=ctx(paths=["ledger/sync.py"])), {}, now=NOW)
        self.assertIn('[UNVERIFIED] "Fixed: ledger/sync.py now keeps the tz offset."', b.text)

    def test_same_source_only_is_not_presented_as_supported(self):
        st = FakeStore()
        run = run_obs("codex:r", prov(**CODEX), 1, passed=5, paths=["tests/test_sync.py"])
        say = obs("codex:m", "assistant_message", "tests/test_sync.py passes, so ledger/sync.py is fixed.",
                  prov(**CODEX), 2)
        c = claim(say, say.text, paths=["ledger/sync.py"])
        cand = cand_b1(c, [run.id], 3)
        st.add(run, say, c, cand, judg(cand, "supports", 4))
        b = build_brief(state_of(st), None, I.BriefRequest(context=ctx(paths=["ledger/sync.py"])), {}, now=NOW)
        self.assertNotIn("[SUPPORTED]", b.text)

    def test_a3_equivalents_are_collapsed(self):
        st = FakeStore()
        oa = obs("codex:m", "assistant_message", "Root cause: ledger/sync.py drops the tz offset.", prov(**CODEX), 1)
        ob = obs("cursor:m", "assistant_message", "The root cause is that ledger/sync.py drops the timezone offset.",
                 prov(**CURSOR), 2)
        ca, cb = claim(oa, oa.text, paths=["ledger/sync.py"]), claim(ob, ob.text, paths=["ledger/sync.py"])
        k1, k2 = cand_a3(ca, cb, 3, "a_contains_b"), cand_a3(ca, cb, 3, "b_contains_a")
        st.add(oa, ob, ca, cb, k1, k2, judg(k1, "restates", 4), judg(k2, "restates", 5))
        b = build_brief(state_of(st), None, I.BriefRequest(context=ctx(paths=["ledger/sync.py"])), {}, now=NOW)
        facts = [i for i in b.items if i.kind == "fact"]
        self.assertEqual(len(facts), 1)
        self.assertIn("restatement", facts[0].text)


class StaleAndPush(unittest.TestCase):
    def test_stale_state_header_says_as_of(self):
        st, *_ = scenario()
        load_or_rebuild(st, {}, now=ts(100))
        st.add(obs("claude:n", "note", "new", prov(**CLAUDE), 110))
        s = load_or_rebuild(st, {}, allow_rebuild=False)
        b = build_brief(s, None, I.BriefRequest(context=ctx(paths=["ledger/sync.py"])), {}, now=NOW)
        self.assertTrue(b.stale)
        self.assertEqual(b.memory_as_of, ts(100))
        self.assertIn("(memory as of 20m ago)", b.text.splitlines()[0])

    def test_overlay_adds_fresh_run_results(self):
        st, *_ = scenario()
        s = load_or_rebuild(st, {}, now=ts(100))
        st.add(run_obs("codex:new", prov(**CODEX), 110, target="pytest tests/test_recon.py", failed=1, passed=1,
                       failed_ids=["tests/test_recon.py::test_tz"], paths=["tests/test_recon.py"]))
        b = build_brief(s, st, I.BriefRequest(context=ctx(session="s-o", paths=["tests/test_recon.py"])), {}, now=NOW)
        self.assertIn("test `pytest tests/test_recon.py` failed (1 failed, 1 passed)", b.text)
        self.assertGreater(st.window_calls, 0)

    def test_push_only_new_changes_and_min_interval(self):
        st, bad, *_ = scenario()
        s = state_of(st)
        c = ctx(session="s-p", paths=["ledger/sync.py"])
        b = build_brief(s, st, I.BriefRequest(context=c, purpose="push", max_tokens=200, since_ts=ts(50)), {},
                        now=NOW)
        self.assertEqual(b.text, "")                        # the refutation (ts 22) is older than since_ts
        b = build_brief(s, st, I.BriefRequest(context=c, purpose="push", max_tokens=200, since_ts=ts(10)), {},
                        now=NOW)
        self.assertEqual([i.tier for i in b.items], ["P1", "P2"])      # refutation (22) and issue (30) are newer
        self.assertLessEqual(b.token_estimate, 200)
        c2 = cand_b1(bad, [], 125, extra_state={"v": 9})
        st.add(c2, judg(c2, "both", 126))
        b = build_brief(state_of(st, ts(127)), st, I.BriefRequest(context=c, purpose="push", max_tokens=200,
                                                                   since_ts=ts(10)), {}, now=ts(120.5))
        self.assertEqual(b.text, "")                        # within push_min_interval_s of the last push


class Chinese(unittest.TestCase):
    def test_zh_rendering(self):
        st, *_ = scenario()
        s = state_of(st)
        b = build_brief(s, None, I.BriefRequest(context=ctx(paths=["ledger/sync.py", "ledger/config.toml"]),
                                                lang="zh"), {}, now=NOW)
        self.assertTrue(b.text.startswith("[hearmemory] 项目共享记忆"))
        self.assertIn("[已被反驳]", b.text)
        self.assertIn("反证：", b.text)
        self.assertIn("[未决问题 i-", b.text)
        self.assertIn("小时前", b.text)


if __name__ == "__main__":
    unittest.main()
