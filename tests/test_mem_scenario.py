"""memory end-to-end on a synthetic session (a small multi-agent debugging scenario):
  * a refuted root-cause claim is surfaced to its author and blocks a commit that relies on it (block mode),
    and holds exactly once in hold_once mode;
  * the same source is not double counted (own run + two own restatements -> SAME-SOURCE ONLY), while an
    independent run by another agent makes it SUPPORTED;
  * a fact found by one parallel subagent is surfaced to another subagent and to the next sequential agent;
  * "fixed after the change" is OUTDATED, never REFUTED, and never blocks."""
import unittest

from test_mem_support import (FakeStore, cand_b1, claim, ctx, edit_obs, judg, link, obs, prov, run_obs, ts)

import hearmemory.interfaces as I
from hearmemory.memory.brief import build_brief
from hearmemory.memory.build import load_or_rebuild
from hearmemory.memory.precommit import check

CODEX_1 = dict(host="codex", session="019f-codex-a")          # first sequential agent
CODEX_2 = dict(host="codex", session="019f-codex-b")          # the next sequential agent (new rollout)
CLAUDE_MAIN = dict(host="claude", session="s-main")
SUB_A = dict(host="claude", session="s-main", sub="agent-a", sub_type="explorer")
SUB_B = dict(host="claude", session="s-main", sub="agent-b", sub_type="fixer")
CURSOR = dict(host="cursor", session="conv-1")


class ScenarioLike(unittest.TestCase):
    def setUp(self):
        st = FakeStore()
        # t=0..10 Codex session 1: runs the failing test, blames sync.py, records it via CLI (proxy) twice
        fail = run_obs("codex:r1", prov(**CODEX_1), 1, target="pytest tests/test_recon.py", failed=1, passed=4,
                       failed_ids=["tests/test_recon.py::test_tz"], paths=["tests/test_recon.py"])
        m1 = obs("codex:m1", "assistant_message",
                 "The root cause is that ledger/sync.py drops the tz offset before nightly-recon runs.",
                 prov(**CODEX_1), 3)
        bad = claim(m1, "The root cause is that ledger/sync.py drops the tz offset before nightly-recon runs.",
                    paths=["ledger/sync.py"])
        rec = obs("cli:rec1", "claim", "tests/test_recon.py::test_tz fails with KeyError tz", prov(
            host="codex", session="codex-launch-1", source="cli"), 4)
        status = claim(rec, rec.text, cls="status", paths=["tests/test_recon.py"])
        # t=20..40 Claude parallel subagents
        read = obs("claude:read", "file_read", "def sync(ts):\n    return ts.astimezone(LEDGER_TZ)\n", prov(**SUB_A),
                   20, tool=I.ToolInfo(name="Read", paths=["ledger/sync.py"], status="ok"))
        grep = obs("claude:grep", "search", "recon/nightly.py:14: tz = os.environ['RECON_TZ']", prov(**SUB_A), 21,
                   tool=I.ToolInfo(name="Grep", paths=["recon/nightly.py"], status="ok"))
        found = obs("claude:subA", "subagent_result",
                    "nightly-recon reads RECON_TZ in recon/nightly.py, not LEDGER_TZ; ledger/sync.py keeps the offset.",
                    prov(**SUB_A), 22)
        fact = claim(found, "nightly-recon reads RECON_TZ in recon/nightly.py, not LEDGER_TZ",
                     paths=["recon/nightly.py"])
        # judgments (worker, later): Claude's read refutes Codex's root cause; Codex's own evidence only
        # "supports" its own status claim; Claude's grep supports sub A's fact
        k_bad = cand_b1(bad, [read.id, grep.id], 30)
        k_status = cand_b1(status, [fail.id], 30)
        k_fact = cand_b1(fact, [grep.id], 30)
        st.add(fail, m1, rec, read, grep, found, bad, status, fact, k_bad, k_status, k_fact,
               judg(k_bad, "refutes", 31), judg(k_status, "supports", 31), judg(k_fact, "supports", 31),
               # the CLI record gets linked to Codex session 1 when its rollout is imported
               link(rec.id, prov(**CODEX_1), 4))
        self.st, self.bad, self.status, self.fact, self.fail_run, self.k_status = st, bad, status, fact, fail, k_status
        self.state = load_or_rebuild(st, {}, now=ts(40))

    def test_same_source_not_double_counted_then_independent_support(self):
        s = self.state
        # the fact's evidence is the same subagent's own grep, seen before it wrote the report -> same source
        self.assertEqual(s.claims[self.fact.claim_id].status, "same_source_only")
        # codex's status claim is backed only by codex's own run -> same source
        self.assertEqual(s.claims[self.status.claim_id].status, "same_source_only")
        # an independent Cursor run of the same target makes it SUPPORTED
        crun = run_obs("cursor:r", prov(**CURSOR), 50, target="pytest tests/test_recon.py", failed=1, passed=4,
                       failed_ids=["tests/test_recon.py::test_tz"], paths=["tests/test_recon.py"])
        k2 = cand_b1(self.status, [self.fail_run.id, crun.id], 51, supersedes=self.k_status.candidate_id)
        self.st.add(crun, k2, judg(k2, "supports", 52))
        s2 = load_or_rebuild(self.st, {}, now=ts(55))
        self.assertEqual(s2.claims[self.status.claim_id].status, "supported")

    def test_refuted_claim_reaches_the_next_sequential_agent_and_blocks_its_commit(self):
        # Codex session 2 works on ledger/sync.py: P1 shows the refuted root cause with Claude's counter-evidence
        c2 = ctx(host="codex", session=CODEX_2["session"], paths=["ledger/sync.py"])
        b = build_brief(self.state, self.st, I.BriefRequest(context=c2), {}, now=ts(60))
        p1 = [i for i in b.items if i.tier == "P1"]
        self.assertEqual([i.item_key for i in p1], [f"claim:{self.bad.claim_id}:refuted"])
        self.assertIn("counter-evidence:", p1[0].text)
        self.assertIn("subagent explorer", p1[0].text)
        diff = ("diff --git a/ledger/sync.py b/ledger/sync.py\n+++ b/ledger/sync.py\n"
                "+    # root cause: sync.py drops the tz offset before nightly-recon\n")
        r = I.CheckRequest(context=c2, payload_text="fix: sync.py drops the tz offset\n" + diff,
                           paths=["ledger/sync.py"], mode="block")
        res = check(self.state, self.st, r, {}, now=ts(61))
        self.assertEqual(res.decision, "block")
        self.assertEqual(res.warnings[0].kind, "relies_on_refuted")
        r.mode = "hold_once"
        r.attempt_key = "codex:git commit -m fix"
        self.assertEqual(check(self.state, self.st, r, {}, now=ts(62)).decision, "hold")
        self.assertEqual(check(self.state, self.st, r, {}, now=ts(63)).decision, "warn")

    def test_parallel_subagent_fact_surfaces_to_the_other_subagent(self):
        # another grep of the same file is the SAME source (rule 1: same tool, same path) -> still same-source
        cg = obs("cursor:grep", "search", "recon/nightly.py:14: tz = os.environ['RECON_TZ']  # nightly-recon",
                 prov(**CURSOR), 45, tool=I.ToolInfo(name="Grep", paths=["recon/nightly.py"], status="ok"))
        k0 = cand_b1(self.fact, [cg.id], 44)
        self.st.add(cg, k0, judg(k0, "supports", 45))
        self.assertEqual(load_or_rebuild(self.st, {}, now=ts(46)).claims[self.fact.claim_id].status,
                         "same_source_only")
        # an independent test run by Cursor makes it SUPPORTED; then subagent B (different actor) sees it
        crun = run_obs("cursor:r", prov(**CURSOR), 45, target="pytest tests/test_nightly.py -k recon_tz", passed=2,
                       paths=["tests/test_nightly.py", "recon/nightly.py"])
        k = cand_b1(self.fact, [cg.id, crun.id], 46, supersedes=k0.candidate_id)
        self.st.add(crun, k, judg(k, "supports", 47))
        s = load_or_rebuild(self.st, {}, now=ts(50))
        self.assertEqual(s.claims[self.fact.claim_id].status, "supported")
        sub_b = ctx(host="claude", session="s-main", sub="agent-b", paths=["recon/nightly.py"])
        b = build_brief(s, self.st, I.BriefRequest(context=sub_b, purpose="subagent_start", max_tokens=300), {},
                        now=ts(60))
        self.assertIn(f"fact:{self.fact.claim_id}:supported", [i.item_key for i in b.items])
        # ... but not to subagent A, who found it
        sub_a = ctx(host="claude", session="s-main", sub="agent-a", paths=["recon/nightly.py"])
        b = build_brief(s, None, I.BriefRequest(context=sub_a, max_tokens=300), {}, now=ts(60))
        self.assertNotIn(f"fact:{self.fact.claim_id}:supported", [i.item_key for i in b.items])

    def test_fixed_after_change_is_outdated_not_refuted(self):
        edit = edit_obs("claude:fix", prov(**SUB_B), 70, "ledger/sync.py")
        ok = run_obs("claude:r2", prov(**SUB_B), 71, target="pytest tests/test_recon.py", passed=5,
                     paths=["tests/test_recon.py"])
        k = cand_b1(self.status, [ok.id], 72, extra_state={"after": "fix"})
        self.st.add(edit, ok, k, judg(k, "outdated", 73, provider="rule", rule_id="B1_test_status_changed"))
        s = load_or_rebuild(self.st, {}, now=ts(80))
        self.assertEqual(s.claims[self.status.claim_id].status, "outdated")
        c2 = ctx(host="codex", session=CODEX_2["session"], paths=["tests/test_recon.py"])
        res = check(s, self.st, I.CheckRequest(context=c2, payload_text="tests/test_recon.py::test_tz fails",
                                              paths=["tests/test_recon.py"], mode="block"), {}, now=ts(81))
        self.assertNotIn("relies_on_refuted", [w.kind for w in res.warnings])
        self.assertNotEqual(res.decision, "block")


if __name__ == "__main__":
    unittest.main()
