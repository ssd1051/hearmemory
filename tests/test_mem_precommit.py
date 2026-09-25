"""pre-commit / claim check - four modes, hold_once attempt keys, block + ack, outdated never blocks,
fresh overlay feeds failing_check, refuted warnings carry counter-evidence."""
import unittest

from test_mem_support import (CLAUDE, CLAUDE_SUB, CODEX, CURSOR, FakeStore, cand_b1, claim, ctx, edit_obs, event,
                              judg, obs, prov, run_obs, ts)

import hearmemory.interfaces as I
from hearmemory.memory.build import MemoryBuilder, load_or_rebuild
from hearmemory.memory.precommit import check

NOW = ts(120)
DIFF = """diff --git a/ledger/sync.py b/ledger/sync.py
--- a/ledger/sync.py
+++ b/ledger/sync.py
@@ -10,3 +10,4 @@
+    # keep the tz offset: sync.py used to drop it
+    return ts.astimezone(LEDGER_TZ)
"""


def refuted_store():
    st = FakeStore()
    say = obs("codex:m", "assistant_message", "The root cause is that ledger/sync.py drops the tz offset.",
              prov(**CODEX), 1)
    c = claim(say, say.text, paths=["ledger/sync.py"])
    run = run_obs("claude:r", prov(**CLAUDE_SUB), 20, target="pytest tests/test_sync.py", passed=6,
                  paths=["tests/test_sync.py", "ledger/sync.py"])
    cand = cand_b1(c, [run.id], 21)
    st.add(say, run, c, cand, judg(cand, "refutes", 22))
    return st, c, run


def req(mode="warn", session="019f36b5-codex", host="codex", payload=DIFF, paths=("ledger/sync.py",), action="git_commit",
        attempt_key=None):
    return I.CheckRequest(context=ctx(host=host, session=session), action=action, payload_text=payload,
                          paths=list(paths), mode=mode, attempt_key=attempt_key)


def state(st, now=NOW):
    return MemoryBuilder().build(st, {}, now=now)


class Modes(unittest.TestCase):
    def test_off_warn_and_no_warning_allow(self):
        st, c, run = refuted_store()
        s = state(st)
        self.assertEqual(check(s, st, req("off"), {}, now=NOW).decision, "allow")
        r = check(s, st, req("warn"), {}, now=NOW)
        self.assertEqual(r.decision, "warn")
        self.assertTrue(r.ok)
        self.assertEqual([w.kind for w in r.warnings], ["relies_on_refuted"])
        self.assertTrue(r.text.startswith("[hearmemory pre-commit check] You are about to commit (not executed yet). "
                                          "1 memory items may matter:"))
        self.assertIn("counter-evidence: test `pytest tests/test_sync.py` passed", r.text)
        quiet = check(s, st, req("warn", payload="docs only", paths=["docs/index.md"]), {}, now=NOW)
        self.assertEqual((quiet.decision, quiet.text, quiet.warnings), ("allow", "", []))

    def test_hold_once_holds_first_attempt_per_key_then_warns(self):
        st, c, run = refuted_store()
        s = state(st)
        r1 = check(s, st, req("hold_once", attempt_key="k1"), {}, now=NOW)
        self.assertEqual(r1.decision, "hold")
        self.assertFalse(r1.ok)
        self.assertIn("Run the same command again to commit anyway", r1.text)
        self.assertEqual(check(s, st, req("hold_once", attempt_key="k1"), {}, now=ts(121)).decision, "warn")
        self.assertEqual(check(s, st, req("hold_once", attempt_key="k2"), {}, now=ts(122)).decision, "hold")
        # after hold_window_s the same key holds again
        self.assertEqual(check(s, st, req("hold_once", attempt_key="k1"), {}, now=ts(140)).decision, "hold")
        self.assertIn("holds", st.state["sessions/019f36b5-codex"])

    def test_git_holds_keyed_by_tree_hash(self):
        st, c, run = refuted_store()
        s = state(st)
        g = req("hold_once", host="git", session=None, attempt_key="tree-abc")
        self.assertEqual(check(s, st, g, {}, now=NOW).decision, "hold")
        self.assertIn("tree-abc", st.state["git_holds"])
        self.assertEqual(check(s, st, g, {}, now=ts(121)).decision, "warn")

    def test_block_on_refuted_and_ack_unblocks(self):
        st, c, run = refuted_store()
        s = state(st)
        r = check(s, st, req("block"), {}, now=NOW)
        self.assertEqual(r.decision, "block")
        key = r.warnings[0].item_key
        self.assertIn(f"hearmemory check --ack <key>' (keys: {key})", r.text)
        st.add(event("seen", key, 121, {"ack": True}))
        self.assertEqual(check(state(st), st, req("block"), {}, now=NOW).decision, "warn")

    def test_block_only_for_blocking_kinds(self):
        st = FakeStore()
        note = obs("cli:n", "note", "Is ledger/sync.py thread safe?", prov(**CURSOR), 1, paths=["ledger/sync.py"])
        iid = I.issue_id_for("manual", "is ledger/sync.py thread safe?")
        st.add(note, event("issue_open", iid, 1, {"obs_id": note.id}))
        r = check(state(st), st, req("block"), {}, now=NOW)
        self.assertEqual([w.kind for w in r.warnings], ["unresolved_issue"])
        self.assertEqual(r.decision, "warn")


class Content(unittest.TestCase):
    def test_outdated_claim_never_warns_or_blocks(self):
        st = FakeStore()
        say = obs("codex:m", "assistant_message", "tests/test_sync.py::test_tz fails because ledger/sync.py drops tz",
                  prov(**CODEX), 1)
        c = claim(say, say.text, cls="status", paths=["ledger/sync.py", "tests/test_sync.py"])
        fix = edit_obs("claude:e", prov(**CLAUDE), 10, "ledger/sync.py")
        later = run_obs("claude:r", prov(**CLAUDE), 11, passed=4, paths=["tests/test_sync.py"])
        cand = cand_b1(c, [later.id], 12)
        st.add(say, fix, later, c, cand, judg(cand, "outdated", 13, provider="rule", rule_id="B1_test_status_changed"))
        r = check(state(st), st, req("block", host="cursor", session="conv-42"), {}, now=NOW)
        self.assertNotIn("relies_on_refuted", [w.kind for w in r.warnings])
        self.assertIn(r.decision, ("allow", "warn"))

    def test_fresh_overlay_turns_a_just_failed_run_into_failing_check(self):
        st, c, run = refuted_store()
        s = load_or_rebuild(st, {}, now=ts(100))
        st.add(run_obs("cursor:f", prov(**CURSOR), 110, target="pytest tests/test_sync.py", failed=1, passed=5,
                       failed_ids=["tests/test_sync.py::test_tz"], paths=["tests/test_sync.py"]))
        stale = load_or_rebuild(st, {}, allow_rebuild=False)
        self.assertTrue(stale.stats["stale"])
        r = check(stale, st, req("block", host="claude", session="s-claude-1"), {}, now=NOW)
        kinds = [w.kind for w in r.warnings]
        self.assertIn("failing_check", kinds)
        self.assertEqual(r.decision, "block")
        self.assertTrue(r.stale)
        self.assertIn("memory as of 20m ago", r.text.splitlines()[0])
        # without the overlay the stale state alone would not know the run failed
        r2 = check(stale, st, req("warn", host="claude", session="s-claude-1"), {}, now=NOW, overlay=False)
        self.assertNotIn("failing_check", [w.kind for w in r2.warnings])

    def test_claim_action_requires_textual_overlap(self):
        st, c, run = refuted_store()
        s = state(st)
        related = check(s, st, req(action="claim", payload="Root cause: ledger/sync.py drops the tz offset.",
                                   paths=["ledger/sync.py"]), {}, now=NOW)
        self.assertEqual([w.kind for w in related.warnings], ["relies_on_refuted"])
        self.assertTrue(related.text.startswith("[hearmemory check] Before relying on this claim"))
        unrelated = check(s, st, req(action="claim", payload="ledger/sync.py now logs at INFO level.",
                                     paths=["ledger/sync.py"]), {}, now=NOW)
        self.assertEqual(unrelated.warnings, [])

    def test_unseen_fact_from_other_actor_then_not_repeated(self):
        st = FakeStore()
        m = obs("cursor:m", "assistant_message", "LedgerSyncWorker in ledger/sync.py retries 3 times.", prov(**CURSOR), 1)
        c = claim(m, m.text, paths=["ledger/sync.py"])
        r = run_obs("claude:r", prov(**CLAUDE), 2, passed=3, paths=["ledger/sync.py"])
        cand = cand_b1(c, [r.id], 3)
        st.add(m, r, c, cand, judg(cand, "supports", 4))
        s = state(st)
        first = check(s, st, req(), {}, now=NOW)
        self.assertEqual([w.kind for w in first.warnings], ["unseen_relevant_fact"])
        self.assertIn('[NEW] [SUPPORTED] "LedgerSyncWorker', first.text)
        self.assertEqual(check(s, st, req(), {}, now=NOW).warnings, [])

    def test_text_is_bounded(self):
        st = FakeStore()
        for i in range(30):
            note = obs(f"n{i}", "note", f"Open question {i} about ledger/sync.py and LEDGER_TZ_{i}", prov(**CURSOR), i,
                       paths=["ledger/sync.py"])
            st.add(note, event("issue_open", I.issue_id_for("manual", f"q{i}"), i, {"obs_id": note.id}))
        r = check(state(st), st, req(), {"precommit": {"max_tokens": 150}}, now=NOW)
        self.assertLess(len(r.warnings), 30)
        from hearmemory.memory.text import estimate_tokens
        self.assertLessEqual(estimate_tokens(r.text), 200)


if __name__ == "__main__":
    unittest.main()
