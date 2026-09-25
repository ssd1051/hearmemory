"""generic extractor v1: A1/A2/A3/B1 questions, rules, actors, caps, time invariance."""
import json
import re
import shutil
import unittest

from test_judge_support import (COMMIT_A, COMMIT_B, T0, Clock, default_cfg, edit, link_event, make_project, obs,
                                prov, run, say)

from hearmemory import interfaces as I
from hearmemory.judge.extract import ExtractIndex, Extractor
from hearmemory.judge.mentions import LOG_LINE_RE
from hearmemory.judge.project_index import ProjectIndex

CX = prov("codex", "cx1", source="import:codex_rollout")
CL = prov("claude", "s2")
SUB = prov("claude", "s2", sub="ag1", sub_type="explorer")
CUR = prov("cursor", "conv9", source="hook:afterAgentResponse")
REL_RE = re.compile(I.RELATIVE_TIME_RE)
FAIL_OUT = ("tests/test_recon.py F.\n=== FAILURES ===\n___ test_reconcile_tz ___\n>   reconcile_day(row)\n"
            "E   KeyError: 'tz'\nsrc/shop/recon.py:5: KeyError\n"
            "FAILED tests/test_recon.py::test_reconcile_tz - KeyError: 'tz'\n1 failed, 1 passed in 0.05s")


def strings(v):
    if isinstance(v, str):
        yield v
    elif isinstance(v, dict):
        for x in v.values():
            yield from strings(x)
    elif isinstance(v, list):
        for x in v:
            yield from strings(x)


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = make_project()
        cls.cfg = default_cfg()
        cls.idx = ProjectIndex.build(cls.root, cls.cfg)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def ex(self, clock=None, actor_map=None, xindex=None, cfg=None):
        return Extractor(self.idx, cfg or self.cfg, actor_map=actor_map, xindex=xindex or ExtractIndex(),
                         clock=clock or Clock(T0 + 7200), project="shop")

    def extract(self, observations, **kw):
        e = self.ex(**kw)
        return e.extract(observations), e

    @staticmethod
    def of(res, tid, rule=None):
        return [c for c in res.candidates if c.template_id == tid and (rule is None or c.rule_hint == rule)]


class TestA1(_Base):
    def test_ambiguous_basename_across_actors(self):
        res, _ = self.extract([say("The bug is in sync.py: normalize_tz is never called.", 0, CX),
                               say("I fixed src/shop/ledger/sync.py so normalize_tz is called.", 600, CL)])
        a1 = self.of(res, "A1")
        self.assertEqual(len(a1), 1)
        c = a1[0]
        self.assertIsNone(c.rule_hint)
        self.assertEqual(c.state["record_a"]["marked_mention"], "sync.py")
        self.assertEqual(c.state["record_b"]["marked_mention"], "src/shop/ledger/sync.py")
        self.assertEqual(sorted(c.state["trusted_context"]["known_candidates"]),
                         ["src/shop/jobs/sync.py", "src/shop/ledger/sync.py"])
        self.assertEqual(c.state["trusted_context"]["source_a"], "codex session at 2026-09-24T10:00Z, commit a1b2c3d")
        self.assertTrue(c.subject_key.startswith("pair:"))
        self.assertEqual({c.meta["node_a"], c.meta["node_b"]}, set(c.subject_key[5:].split("|")))

    def test_same_actor_never_paired(self):
        res, _ = self.extract([say("The bug is in sync.py: normalize_tz is never called.", 0, CL),
                               say("I fixed src/shop/ledger/sync.py so normalize_tz is called.", 600, CL)])
        self.assertEqual(self.of(res, "A1"), [])
        self.assertGreater(res.dropped.get("a1_same_actor", 0), 0)

    def test_two_unique_spellings_rule_same(self):
        res, _ = self.extract([say("shop.ledger.sync is broken because normalize_tz is skipped.", 0, CX),
                               say("src/shop/ledger/sync.py is fixed now and normalize_tz runs.", 600, CL)])
        rules = self.of(res, "A1", "A1_same_resolved_path")
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].meta["rule_label"], "same")
        self.assertEqual(rules[0].priority, 0)

    def test_ambiguous_symbol_needs_anchors(self):
        # SyncWorker is defined in two files; each mention is anchored near a different file -> ask
        res, _ = self.extract([
            say("SyncWorker in src/shop/jobs/sync.py is broken because it skips normalize_tz.", 0, CX),
            say("SyncWorker in src/shop/ledger/sync.py is fine and retries 3 times.", 600, CL)])
        a1 = [c for c in self.of(res, "A1") if c.meta.get("norm_a") == "symbol:SyncWorker"]
        self.assertEqual(len(a1), 1)
        self.assertIsNone(a1[0].rule_hint)
        # unanchored symbol mentions -> no question
        res2, _ = self.extract([say("SyncWorker is broken because it drops rows.", 0, CX),
                                say("SyncWorker is fine and retries 3 times.", 600, CL)])
        self.assertEqual([c for c in self.of(res2, "A1") if c.meta.get("norm_a") == "symbol:SyncWorker"], [])

    def test_alias_pairs_with_object(self):
        res, _ = self.extract([say("LedgerSyncWorker in src/shop/recon.py retries 3 times before it fails.", 0, CX),
                               say("SyncWorker is defined in src/shop/ledger/sync.py and retries 3 times.", 600, CL)])
        a1 = self.of(res, "A1")
        self.assertTrue(any("alias:LedgerSyncWorker" in (c.meta["norm_a"], c.meta["norm_b"]) for c in a1))

    def test_synthetic_corpus_has_no_bad_endpoints(self):
        """Required: >= 20 records with key=value log lines, UUIDs, field names, test files, exception
        names -> A1 endpoints are never log words / ids / dates / exceptions, and never cross-kind."""
        actors = [CX, CL, SUB, CUR]
        texts = [
            "2026-09-24T10:00:01 INFO sync done day_window_utc=2026-09-23 LedgerSync=ok request_id=3f2a9c1e7b",
            "[10:00:02] WARN reconcile_day retry=2 tz_offset_hours=8 normalize_tz",
            "ERROR 2026-09-24 10:00:03 ReconcileError in reconcile_day: KeyError tz (req 123e4567-e89b-12d3-a456-426614174000)",
            "The field day_window_utc is wrong because reconcile_day ignores tz.",
            "reconcile_day in src/shop/recon.py raises ReconcileError because day_window_utc is naive.",
            "tests/test_recon.py fails with KeyError: tz on commit a1b2c3d4e5.",
            "test_reconcile_tz fails because normalize_tz is never called by LedgerSync.",
            "The root cause is in sync.py: LedgerSync.sync_rows drops the offset.",
            "src/shop/ledger/sync.py is the file that drops the offset in sync_rows.",
            "shop.ledger.sync depends on normalize_tz and is broken.",
            "src/shop/jobs/sync.py is unrelated; SyncWorker there only schedules the job.",
            "The nightly-recon service fails because tz_offset_hours is 8.",
            "export-csv is broken since export_orders_csv drops the currency column.",
            "shop.export.csv_export is fine; export_orders_csv returns rows.",
            "The request id 3f2a9c1e7b failed at 2026-09-24 10:00 with ValueError.",
            "Version 1.2.3 on port 8080 is used by the service.",
            "LedgerSyncWorker retries 3 times before failing.",
            "tests/test_sync.py::test_sync_rows passes after the fix.",
            "test_sync_rows passes now because sync_rows calls normalize_tz.",
            "ReconcileError is raised by reconcile_day when tz is missing.",
            "the json payload_field is missing so KeyError is raised by reconcile_day.",
            "Fixed: normalize_tz is now called in src/shop/ledger/sync.py.",
        ]
        observations = [say(t, 60 * i, actors[i % len(actors)]) for i, t in enumerate(texts)]
        observations.append(run("pytest tests/test_recon.py", FAIL_OUT, 30, CL, exit_code=1, passed=1, failed=1,
                                failed_ids=["tests/test_recon.py::test_reconcile_tz"]))
        cfg = default_cfg()
        cfg["extract"]["a1_max_per_run"] = 50
        cfg["extract"]["max_candidates_per_run"] = 200
        res, e = self.extract(observations, cfg=cfg)
        a1 = self.of(res, "A1")
        self.assertGreater(len(a1), 0)
        by_id = {o.id: o for o in observations}
        bad_surface = re.compile(r"^(?:[0-9a-f]{7,}|\d[\d.:-]*|.*(?:Error|Exception|Warning))$")
        for c in a1:
            for side in ("a", "b"):
                norm = c.meta["norm_" + side]
                surface = c.state["record_" + side]["marked_mention"]
                self.assertRegex(norm, r"^(path|base|symbol|test|service|config|alias):")
                self.assertNotRegex(surface, bad_surface)
                self.assertNotIn(surface, ("day_window_utc", "request_id", "retry", "tz", "payload_field"))
                o = by_id[c.meta["obs_" + side]]
                node = c.meta["node_" + side]
                a, b = [int(x) for x in node.split("#")[1].split("-")]
                line_start = o.text.rfind("\n", 0, a) + 1
                line_end = o.text.find("\n", b)
                line = o.text[line_start:line_end if line_end >= 0 else None]
                self.assertIsNone(LOG_LINE_RE.search(line), "A1 endpoint on a log line: %r" % line)
            ka = I.MENTION_COMPAT.get(c.meta["norm_a"].split(":")[0].replace("base", "file").replace("path", "file"))
            kb = I.MENTION_COMPAT.get(c.meta["norm_b"].split(":")[0].replace("base", "file").replace("path", "file"))
            if c.meta["relation"] != "cross_service" and not c.meta["norm_a"].startswith("alias:") \
                    and not c.meta["norm_b"].startswith("alias:"):
                self.assertEqual(ka, kb, (c.meta["norm_a"], c.meta["norm_b"]))
            self.assertFalse(I.actors_may_coincide(c.meta["actor_a"], c.meta["actor_b"]))


class TestActors(_Base):
    def test_awaiting_link_then_linked_actor(self):
        proxy = I.Provenance(host="claude", session_id="mcp-1", source="mcp", git_commit=COMMIT_A, git_branch="main")
        rec = say("The bug is in sync.py: normalize_tz is never called.", 7000, proxy, kind="claim")
        other = say("I fixed src/shop/ledger/sync.py so normalize_tz is called.", 7100, CX)
        xi = ExtractIndex()
        clock = Clock(T0 + 7150)                         # proxy written 150 s ago < link_grace_s
        res, _ = self.extract([rec, other], clock=clock, xindex=xi)
        self.assertEqual(self.of(res, "A1"), [])
        self.assertGreater(res.dropped.get("awaiting_link", 0), 0)
        self.assertIn(rec.id, xi.deferred)
        # the Claude hook later links the MCP record to subagent ag1 -> a different actor than codex
        am = I.ActorMap()
        am.add_event(link_event(rec.id, prov("claude", "s2", sub="ag1", source="hook:PostToolUse"), 7001))
        objs = {rec.id: rec, other.id: other}
        res2 = Extractor(self.idx, self.cfg, actor_map=am, xindex=xi, clock=Clock(T0 + 7400), project="shop",
                         fetch=lambda oid, s: objs.get(oid)).extract([])
        a1 = self.of(res2, "A1")
        self.assertEqual(len(a1), 1)
        self.assertIn("claude:s2:ag1", (a1[0].meta["actor_a"], a1[0].meta["actor_b"]))
        self.assertEqual(xi.deferred, [])

    def test_unlinked_proxy_may_coincide_with_same_host(self):
        proxy = I.Provenance(host="claude", session_id="mcp-1", source="mcp", git_commit=COMMIT_A)
        res, _ = self.extract([say("The bug is in sync.py: normalize_tz is never called.", 0, proxy, kind="claim"),
                               say("I fixed src/shop/ledger/sync.py so normalize_tz is called.", 600, CL)])
        self.assertEqual(self.of(res, "A1"), [])       # "claude:?" may be "claude:s2:main"
        self.assertGreater(res.dropped.get("a1_same_actor", 0), 0)

    def test_cursor_generations_are_one_actor(self):
        g1 = I.Provenance(host="cursor", session_id="conv9", source="hook:afterAgentResponse")
        res, _ = self.extract([say("The bug is in sync.py: normalize_tz is never called.", 0, g1),
                               say("I fixed src/shop/ledger/sync.py so normalize_tz is called.", 600,
                                   I.Provenance(host="cursor", session_id="conv9"))])
        self.assertEqual(self.of(res, "A1"), [])


class TestA2(_Base):
    def test_report_vs_run_asks_jev(self):
        res, _ = self.extract([
            run("pytest tests/test_recon.py", FAIL_OUT, 0, CX, exit_code=1, passed=1, failed=1,
                failed_ids=["tests/test_recon.py::test_reconcile_tz"]),
            say("test_reconcile_tz fails with KeyError: tz in reconcile_day.", 600, CL)])
        a2 = self.of(res, "A2")
        self.assertEqual(len(a2), 1)
        tc = a2[0].state["trusted_context"]
        self.assertEqual(tc["time_a"], "2026-09-24T10:00Z")
        self.assertEqual(tc["target"], "tests/test_recon.py::test_reconcile_tz")
        self.assertIn("KeyError", a2[0].state["record_a"]["marked_event"])

    def test_shared_run_id_rule(self):
        out = FAIL_OUT + "\nrequest 3f2a9c1e7b0d failed"
        res, _ = self.extract([
            run("pytest tests/test_recon.py", out, 0, CX, exit_code=1, failed=1, failed_ids=["tests/test_recon.py::test_reconcile_tz"]),
            run("pytest tests/test_recon.py", out, 60, CL, exit_code=1, failed=1, failed_ids=["tests/test_recon.py::test_reconcile_tz"])])
        a2 = self.of(res, "A2")
        self.assertEqual([(c.rule_hint, c.meta["rule_label"]) for c in a2], [("A2_shared_run_id", "same_event")])

    def test_different_commit_runs_rule(self):
        pa = prov("codex", "cx1", source="import:codex_rollout", commit=COMMIT_A)
        pb = prov("claude", "s2", commit=COMMIT_B)
        res, _ = self.extract([
            run("pytest tests/test_recon.py", FAIL_OUT, 0, pa, exit_code=1, failed=1, failed_ids=["tests/test_recon.py::test_reconcile_tz"]),
            edit("tests/test_recon.py", "+ assert x", 300, pb),
            run("pytest tests/test_recon.py", FAIL_OUT, 600, pb, exit_code=1, failed=1, failed_ids=["tests/test_recon.py::test_reconcile_tz"])])
        a2 = self.of(res, "A2")
        self.assertEqual([(c.rule_hint, c.meta["rule_label"]) for c in a2], [("A2_different_commit_runs", "different_events")])

    def test_signature_mismatch_and_time_gap(self):
        other = FAIL_OUT.replace("KeyError: 'tz'", "ZeroDivisionError: division by zero")
        res, _ = self.extract([
            run("pytest tests/test_recon.py", FAIL_OUT, 0, CX, exit_code=1, failed=1, failed_ids=["tests/test_recon.py::test_reconcile_tz"]),
            run("pytest tests/test_recon.py", other, 60, CL, exit_code=1, failed=1, failed_ids=["tests/test_recon.py::test_reconcile_tz"])])
        self.assertEqual(self.of(res, "A2"), [])
        self.assertGreater(res.dropped.get("a2_signature_mismatch", 0), 0)
        res2, _ = self.extract([
            run("pytest tests/test_recon.py", FAIL_OUT, 0, CX, exit_code=1, failed=1, failed_ids=["tests/test_recon.py::test_reconcile_tz"]),
            say("test_reconcile_tz fails with KeyError: tz.", 49 * 3600, CL)], clock=Clock(T0 + 50 * 3600))
        self.assertEqual(self.of(res2, "A2"), [])
        self.assertGreater(res2.dropped.get("a2_time_gap", 0), 0)


class TestA3(_Base):
    def test_near_identical_rule_both_directions(self):
        t = "The root cause is that normalize_tz is never called in src/shop/ledger/sync.py."
        res, _ = self.extract([say(t, 0, CX), say(t, 600, CL)])
        a3 = self.of(res, "A3")
        self.assertEqual(sorted(c.direction for c in a3), ["a_contains_b", "b_contains_a"])
        self.assertTrue(all(c.rule_hint == "A3_near_identical" and c.meta["rule_label"] == "restates" for c in a3))

    def test_paraphrase_asks_both_directions(self):
        res, _ = self.extract([
            say("The root cause is that normalize_tz is never called in src/shop/ledger/sync.py.", 0, CX),
            say("Root cause: src/shop/ledger/sync.py never calls normalize_tz on the rows.", 600, CL)])
        a3 = self.of(res, "A3")
        self.assertEqual(sorted(c.direction for c in a3), ["a_contains_b", "b_contains_a"])
        self.assertTrue(all(c.rule_hint is None for c in a3))
        d = {c.direction: c.state for c in a3}
        self.assertEqual(d["a_contains_b"]["record_a"]["text"], d["b_contains_a"]["record_b"]["claim"])

    def test_scope_mismatch_gate(self):
        res, _ = self.extract([
            say("The root cause is that normalize_tz is never called in src/shop/ledger/sync.py.", 0,
                prov("codex", "cx1", source="import:codex_rollout", branch="feature")),
            say("Root cause: src/shop/ledger/sync.py never calls normalize_tz on the rows.", 600, CL)])
        a3 = self.of(res, "A3")
        self.assertTrue(a3 and all("A3_scope_mismatch" in c.meta["gates"] for c in a3))

    def test_low_overlap_dropped(self):
        res, _ = self.extract([say("normalize_tz is called by LedgerSync.sync_rows for every row.", 0, CX),
                               say("normalize_tz returns the row unchanged when tz is UTC.", 600, CL)])
        self.assertEqual(self.of(res, "A3"), [])


class TestB1Scope(_Base):
    """Required: scope_facts + b1_status_rule cases."""
    TGT = ["tests/test_recon.py::test_reconcile_tz"]

    def _case(self, later_ok, *, edit_between=False, dirty_b=None, commit_b=COMMIT_A, imported=False,
              claim="tests/test_recon.py::test_reconcile_tz fails with KeyError: tz."):
        pa = prov("codex", "cx1", source="import:codex_rollout" if imported else "hook:PostToolUse")
        pb = prov("claude", "s2", commit=commit_b, source="import:codex_rollout" if imported else "hook:PostToolUse")
        dirty = None if imported else {"src/shop/ledger/sync.py": "10:1"}
        items = [run("pytest tests/test_recon.py", FAIL_OUT, 0, pa, exit_code=1, passed=1, failed=1,
                     failed_ids=self.TGT, dirty=dirty),
                 say(claim, 60, pa)]
        if edit_between:
            items.append(edit("src/shop/ledger/sync.py", "+ normalize_tz(r)", 300, pb))
        out = "tests/test_recon.py ..\n2 passed" if later_ok else FAIL_OUT
        items.append(run("pytest tests/test_recon.py", out, 600, pb, exit_code=0 if later_ok else 1,
                         passed=2 if later_ok else 1, failed=0 if later_ok else 1,
                         failed_ids=[] if later_ok else self.TGT,
                         dirty=None if imported else (dirty_b if dirty_b is not None else dirty)))
        res, _ = self.extract(items)
        return self.of(res, "B1"), res

    def test_unchanged_scope_rule_supports_or_refutes(self):
        b1, _ = self._case(later_ok=False)
        self.assertEqual([(c.rule_hint, c.meta["rule_label"]) for c in b1], [("B1_test_status", "supports")])
        b1, _ = self._case(later_ok=True)
        self.assertEqual([(c.rule_hint, c.meta["rule_label"]) for c in b1], [("B1_test_status", "refutes")])

    def test_changed_scope_opposite_is_outdated_never_refuted(self):
        for kw in ({"edit_between": True}, {"dirty_b": {"src/shop/ledger/sync.py": "11:2"}},
                   {"commit_b": COMMIT_B}, {"imported": True}):
            b1, res = self._case(later_ok=True, **kw)
            self.assertEqual([(c.rule_hint, c.meta["rule_label"]) for c in b1],
                             [("B1_test_status_changed", "outdated")], kw)
            self.assertGreater(res.dropped.get("b1_outdated_by_change", 0), 0)
            self.assertFalse(any(c.meta.get("rule_label") == "refutes" for c in res.candidates))

    def test_changed_scope_same_outcome_asks_jev_with_scope(self):
        b1, _ = self._case(later_ok=False, edit_between=True)
        self.assertEqual(len(b1), 1)
        c = b1[0]
        self.assertIsNone(c.rule_hint)
        sc = c.state["target_scope"]
        self.assertEqual(sc["commit_claim"], "a1b2c3d")
        self.assertEqual(sc["commit_run"], "a1b2c3d")
        self.assertEqual(sc["edited_between"], ["src/shop/ledger/sync.py"])
        self.assertIn(sc["worktree_changed"], (True, False))
        b1, _ = self._case(later_ok=False, imported=True)
        self.assertEqual(b1[0].state["target_scope"]["worktree_changed"], "unknown")

    def test_fix_then_pass_is_never_refuted(self):
        b1, res = self._case(later_ok=True, edit_between=True, claim="test_reconcile_tz fails because normalize_tz is skipped.")
        self.assertNotIn("refutes", [c.meta.get("rule_label") for c in res.candidates])


class TestB1Evidence(_Base):
    def test_no_evidence_no_question(self):
        res, _ = self.extract([say("reconcile_day in src/shop/recon.py drops the tz offset.", 0, CX)])
        self.assertEqual(self.of(res, "B1"), [])
        self.assertEqual(res.dropped.get("b1_no_evidence"), 1)

    def test_evidence_and_premise(self):
        res, _ = self.extract([
            edit("src/shop/ledger/sync.py", "-        return rows\n+        return [normalize_tz(r) for r in rows]", 0, CL),
            say("The root cause is in src/shop/ledger/sync.py because normalize_tz is never called by sync_rows.", 60, CX)])
        b1 = self.of(res, "B1")
        classes = sorted(c.meta["claim_class"] for c in b1)
        self.assertEqual(classes, ["conclusion", "premise"])
        prem = [c for c in b1 if c.meta["claim_class"] == "premise"][0]
        concl = [c for c in b1 if c.meta["claim_class"] == "conclusion"][0]
        self.assertEqual(prem.meta["parent_claim_id"], concl.meta["claim_id"])
        self.assertEqual((concl.priority, prem.priority), (10, 35))
        ev = concl.state["evidence"]
        self.assertEqual(ev[0]["source"], "file edit by claude main agent at 2026-09-24T10:00Z, commit a1b2c3d")
        self.assertNotIn("o-", json.dumps(concl.state))            # ids are not model-visible

    def test_new_evidence_supersedes_and_rejudge_cap(self):
        xi = ExtractIndex()
        clock = Clock(T0 + 7200)
        e = Extractor(self.idx, self.cfg, xindex=xi, clock=clock, project="shop")
        r1 = e.extract([edit("src/shop/ledger/sync.py", "+ normalize_tz(r)", 0, CL),
                        say("normalize_tz is never called by sync_rows in src/shop/ledger/sync.py.", 60, CX)])
        first = self.of(r1, "B1")[0]
        # identical input again -> duplicate, no new question
        self.assertEqual(self.of(Extractor(self.idx, self.cfg, xindex=xi, clock=clock).extract([]), "B1"), [])
        ids = [first.candidate_id]
        for i in range(5):
            r = Extractor(self.idx, self.cfg, xindex=xi, clock=clock, project="shop").extract(
                [run("grep -n normalize_tz src/shop/ledger/sync.py", "3: normalize_tz(r) # pass %d" % i, 100 + i, CL)])
            ids += [c.candidate_id for c in self.of(r, "B1")]
            if i == 0:
                self.assertEqual(self.of(r, "B1")[0].supersedes, first.candidate_id)
        self.assertEqual(len(ids), 1 + 3, "at most 3 re-judgments per claim per day")


class TestCapsAndTime(_Base):
    def test_caps(self):
        items = [edit("src/shop/ledger/sync.py", "+ normalize_tz(r)\n+ sync_rows", 0, CL)]
        for i in range(30):
            items.append(say("normalize_tz is used by sync_rows in src/shop/ledger/sync.py, case %d." % i, 10 + i,
                             prov("codex", "cx%d" % i, source="import:codex_rollout")))
        res, _ = self.extract(items)
        jev = [c for c in res.candidates if not c.rule_hint]
        self.assertLessEqual(len([c for c in jev if c.template_id == "B1"]), 10)
        self.assertLessEqual(len([c for c in jev if c.template_id == "A3"]), 6)
        self.assertLessEqual(len(jev), 20)
        self.assertTrue(any(k.startswith("cap_") and v for k, v in res.dropped.items()))
        pri = [c.priority for c in res.candidates]
        self.assertEqual(pri, sorted(pri))

    def test_same_questions_at_different_times_and_no_relative_time(self):
        def batch():
            return [run("pytest tests/test_recon.py", FAIL_OUT + "\nlast deploy 2 hours ago", 0, CX, exit_code=1,
                        passed=1, failed=1, failed_ids=["tests/test_recon.py::test_reconcile_tz"],
                        dirty={"src/shop/ledger/sync.py": "1:1"}),
                    say("The bug is in sync.py: test_reconcile_tz fails with KeyError: tz, since 5 minutes ago.", 60, CX),
                    edit("src/shop/ledger/sync.py", "+ normalize_tz(r)", 300, CL),
                    say("I fixed src/shop/ledger/sync.py; test_reconcile_tz fails with KeyError: tz just now.", 600, CL),
                    say("The bug is in sync.py: test_reconcile_tz fails with KeyError: tz.", 700, SUB)]
        import test_judge_support as S
        n0 = S._N[0]
        r1, _ = self.extract(batch(), clock=Clock(T0 + 3600))
        S._N[0] = n0                                     # same event keys -> same observation ids
        r2, _ = self.extract(batch(), clock=Clock(T0 + 30 * 3600))
        k1 = sorted((c.candidate_id, c.input_hash) for c in r1.candidates)
        k2 = sorted((c.candidate_id, c.input_hash) for c in r2.candidates)
        self.assertTrue(k1)
        self.assertEqual(k1, k2)
        for c in r1.candidates:
            for s in strings(c.state):
                self.assertIsNone(REL_RE.search(s), s)


class TestIncrementalIndex(_Base):
    def test_history_from_index_and_fetch(self):
        xi = ExtractIndex()
        first = say("The bug is in sync.py: normalize_tz is never called.", 0, CX)
        Extractor(self.idx, self.cfg, xindex=xi, clock=Clock(T0 + 7200), project="shop").extract([first])
        xi2 = ExtractIndex(json.loads(json.dumps(xi.to_dict())))        # persisted and reloaded
        fetched = []

        def fetch(oid, s):
            fetched.append(oid)
            return first if oid == first.id else None
        e = Extractor(self.idx, self.cfg, xindex=xi2, clock=Clock(T0 + 7300), project="shop", fetch=fetch)
        res = e.extract([say("I fixed src/shop/ledger/sync.py so normalize_tz is called.", 600, CL)])
        self.assertEqual(len(self.of(res, "A1")), 1)
        self.assertEqual(fetched, [first.id])

    def test_window_eviction(self):
        xi = ExtractIndex()
        old = [say("normalize_tz is used in src/shop/ledger/sync.py %d." % i, i, CX) for i in range(5)]
        Extractor(self.idx, self.cfg, xindex=xi, clock=Clock(T0 + 100), project="shop").extract(old)
        self.assertEqual(len(xi.obs), 5)
        cfg = default_cfg()
        cfg["extract"]["history_max_obs"] = 3
        Extractor(self.idx, cfg, xindex=xi, clock=Clock(T0 + 200), project="shop").extract([])
        self.assertEqual(len(xi.obs), 3)
        Extractor(self.idx, cfg, xindex=xi, clock=Clock(T0 + 15 * 86400), project="shop").extract([])
        self.assertEqual(len(xi.obs), 0)
        self.assertEqual(xi.claims, {})


if __name__ == "__main__":
    unittest.main()
