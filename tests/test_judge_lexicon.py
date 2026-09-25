"""ProjectIndex, typed mentions and the "never an object" classes, claims."""
import os
import shutil
import unittest

from test_judge_support import (FakeStore, PROJECT_FILES, default_cfg, make_project, obs, say)

from hearmemory import interfaces as I
from hearmemory.judge.claims import claimed_outcome, extract_claims
from hearmemory.judge.mentions import exception_signature, run_ids, scan, signatures_match
from hearmemory.judge.project_index import ProjectIndex, distinctive_key


class _Project(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = make_project()
        cls.cfg = default_cfg()
        cls.idx = ProjectIndex.build(cls.root, cls.cfg)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def hits(self, text):
        return scan("o-x", text, self.idx, self.cfg)

    def norms(self, text):
        return [h.norm for h in self.hits(text)]


class TestProjectIndex(_Project):
    def test_files_modules_and_privacy(self):
        files = self.idx.files()
        self.assertIn("src/shop/ledger/sync.py", files)
        self.assertIn(".env.example", files)
        self.assertNotIn(".env", files)
        self.assertEqual(self.idx.modules["shop.ledger.sync"], "src/shop/ledger/sync.py")
        self.assertEqual(self.idx.modules["shop.ledger"], "src/shop/ledger/__init__.py")

    def test_dotenv_excluded_even_without_git(self):
        root = make_project(git=False)
        try:
            idx = ProjectIndex.build(root, self.cfg)
            self.assertNotIn(".env", idx.files())
            self.assertNotIn("SECRET_ONLY_IN_DOTENV", idx.config_keys)
            self.assertIn("LEDGER_TZ", idx.config_keys)      # from .env.example
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_symbols_tests_services_config(self):
        self.assertEqual(self.idx.symbols["SyncWorker"], ["src/shop/jobs/sync.py", "src/shop/ledger/sync.py"])
        self.assertIn("normalize_tz", self.idx.symbols)
        self.assertIn("tests/test_recon.py::test_reconcile_tz", self.idx.tests)
        self.assertTrue({"nightly-recon", "export-csv", "shop-cli"} <= self.idx.services)
        self.assertIn("ledger.tz_offset_hours", self.idx.config_keys)
        self.assertNotIn("name", self.idx.config_keys)       # not distinctive
        self.assertTrue(distinctive_key("retryCount") and not distinctive_key("image"))

    def test_symbol_stopwords(self):
        files = dict(PROJECT_FILES)
        files["src/shop/misc.py"] = "def main():\n    pass\ndef run():\n    pass\ndef helper():\n    pass\ndef get(): pass\n"
        root = make_project(files)
        try:
            idx = ProjectIndex.build(root, self.cfg)
            for w in ("main", "run", "helper", "get"):
                self.assertNotIn(w, idx.symbols)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_resolve(self):
        r = self.idx.resolve
        self.assertEqual(r("file", "ledger/sync.py"), ["src/shop/ledger/sync.py"])
        self.assertEqual(sorted(r("file", "sync.py")), ["src/shop/jobs/sync.py", "src/shop/ledger/sync.py"])
        self.assertEqual(r("file", os.path.join(self.root, "src/shop/recon.py")), ["src/shop/recon.py"])
        self.assertEqual(r("file", "/etc/passwd"), [])
        self.assertEqual(r("test", "test_reconcile_tz"), ["tests/test_recon.py::test_reconcile_tz"])
        self.assertEqual(r("module", "shop.recon"), ["src/shop/recon.py"])
        self.assertEqual(r("service", "nightly-recon"), ["nightly-recon"])
        self.assertEqual(r("symbol", "NoSuchThing"), [])

    def test_cache_roundtrip(self):
        store = FakeStore.init(self.root)
        try:
            a = ProjectIndex.load_or_build(store, self.cfg)
            self.assertIsNotNone(store.read_state("index"))
            b = ProjectIndex.load_or_build(store, self.cfg)
            self.assertEqual(a.to_dict(), b.to_dict())
            c = ProjectIndex.from_dict(a.to_dict())
            self.assertEqual(c.resolve("file", "sync.py"), a.resolve("file", "sync.py"))
        finally:
            shutil.rmtree(os.path.join(self.root, ".hearmemory"), ignore_errors=True)


class TestMentions(_Project):
    def test_log_lines_are_not_objects(self):
        text = ("2026-09-24T10:00:01 INFO sync done day_window_utc=2026-09-23 LedgerSync retries=3\n"
                "[10:00:02] reconcile_day(x) normalize_tz\n"
                "WARNING tz_offset_hours: 8 in src/shop/ledger/sync.py")
        hs = self.hits(text)
        self.assertEqual([h.norm for h in hs], ["path:src/shop/ledger/sync.py"])
        self.assertFalse(hs[0].a1_ok, "a file path on a log line is an evidence locator, never an A1 endpoint")

    def test_ids_numbers_dates_versions(self):
        text = ("commit a1b2c3d4e5 request 3f2a9c1e7b uuid 123e4567-e89b-12d3-a456-426614174000 on 2026-09-23 "
                "at 10:05 v1.2.3 port 8080 retries 3 deadbeef12")
        self.assertEqual(self.hits(text), [])

    def test_urls_emails_outside_excluded_hearmemory_paths(self):
        text = ("see https://example.com/src/shop/recon.py and bob@example.com; /etc/hosts ~/.ssh/id_rsa "
                ".env and .hearmemory/state/memory.json and ../other/src/shop/recon.py")
        self.assertEqual(self.hits(text), [])

    def test_stdlib_builtins_exceptions(self):
        text = "os.path.join and json.loads raise ValueError; ReconcileError and KeyError: tz; `print` and `len`"
        self.assertEqual(self.hits(text), [])

    def test_ungrounded_words(self):
        self.assertEqual(self.hits("FooBarBaz calls do_the_thing() via `payload_json`"), [])

    def test_grounded_kinds(self):
        n = self.norms("sync.py, ledger/sync.py, shop.recon, LedgerSync, normalize_tz(), "
                       "tests/test_recon.py::test_reconcile_tz, test_sync_rows, nightly-recon, `ledger.tz_offset_hours`")
        self.assertEqual(n, ["base:sync.py", "path:src/shop/ledger/sync.py", "path:src/shop/recon.py", "symbol:LedgerSync",
                             "symbol:normalize_tz", "path:tests/test_recon.py",
                             "test:tests/test_recon.py::test_reconcile_tz", "test:tests/test_sync.py::test_sync_rows",
                             "service:nightly-recon", "config:ledger.tz_offset_hours"])

    def test_config_key_needs_context(self):
        self.assertEqual(self.norms("we changed tz_offset_hours today"), [])
        self.assertEqual(self.norms("set tz_offset_hours = 9"), ["config:tz_offset_hours"])

    def test_alias_rule(self):
        hs = self.hits("LedgerSyncWorker retries three times")
        self.assertEqual(len(hs), 1)
        m = hs[0].mention
        self.assertTrue(hs[0].alias)
        self.assertFalse(m.grounded)
        self.assertEqual(m.norm, "alias:LedgerSyncWorker")
        self.assertTrue(set(m.resolved) & {"symbol:LedgerSync", "symbol:SyncWorker"})

    def test_cap_on_distinct_norms(self):
        files = dict(PROJECT_FILES)
        for i in range(20):
            files["src/shop/m%02d.py" % i] = "def func_number_%02d():\n    pass\n" % i
        root = make_project(files)
        try:
            idx = ProjectIndex.build(root, self.cfg)
            text = " ".join("func_number_%02d()" % i for i in range(20)) + " func_number_00()"
            hs = scan("o", text, idx, self.cfg)
            self.assertEqual(len({h.norm for h in hs}), 12)
            self.assertEqual(sum(1 for h in hs if h.norm == "symbol:func_number_00"), 2)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_signature_and_run_ids(self):
        self.assertEqual(exception_signature("E  KeyError: 'tz' at src/a/b.py:12"), "KeyError: 'tz'")
        self.assertEqual(exception_signature("ValueError: bad row 42 in src/a/b.py"), "ValueError: bad row #")
        self.assertEqual(exception_signature("OSError: cannot open src/a/b.py"), "OSError: cannot open b.py")
        self.assertTrue(signatures_match("KeyError: 'tz'", "KeyError: tz", 0.5))
        self.assertTrue(signatures_match("KeyError", "KeyError: tz", 0.5))
        self.assertFalse(signatures_match("KeyError: tz", "ValueError: tz", 0.5))
        self.assertFalse(signatures_match("KeyError: currency", "KeyError: timezone offset", 0.5))
        self.assertIsNone(exception_signature("all good"))
        self.assertEqual(run_ids("request 3f2a9c1e7b failed; commit 9a8b7c6d5e4f"), ["3f2a9c1e7b"])


class TestClaims(_Project):
    def claims(self, text, kind="assistant_message"):
        o = say(text, 0, kind=kind)
        return extract_claims(o, scan(o.id, o.text, self.idx, self.cfg), self.cfg)

    def test_non_claims(self):
        for text in ["Should we change src/shop/recon.py?",
                     "Let me check whether src/shop/recon.py is used.",
                     "Next, I will fix normalize_tz in src/shop/ledger/sync.py.",
                     "## Root cause in src/shop/recon.py",
                     "The failing places in src/shop/recon.py are:",
                     "2026-09-24 10:00 ERROR src/shop/recon.py failed hard",
                     "$ pytest tests/test_recon.py fails",
                     "```\nsrc/shop/recon.py is broken\n```",
                     "The config is fine and nothing is broken here.",
                     "请确认 src/shop/recon.py 是否需要修改吗"]:
            self.assertEqual(self.claims(text), [], text)

    def test_tool_output_never_claims(self):
        o = obs("command", "src/shop/recon.py is broken because normalize_tz is missing.", 0)
        self.assertEqual(extract_claims(o, scan(o.id, o.text, self.idx, self.cfg), self.cfg), [])

    def test_classes_and_premise(self):
        cs = self.claims("The root cause is that normalize_tz is skipped in src/shop/ledger/sync.py because "
                         "LedgerSync.sync_rows never calls normalize_tz. tests/test_recon.py fails on main. "
                         "src/shop/recon.py uses reconcile_day for every run.")
        classes = [c.claim_class for c in cs]
        self.assertEqual(classes, ["conclusion", "premise", "status", "other"])
        premise = cs[1]
        self.assertEqual(premise.parent_claim_id, cs[0].claim_id)
        self.assertTrue(premise.text.startswith("LedgerSync.sync_rows never calls"))
        self.assertEqual(cs[0].claim_id, I.claim_id_for(cs[0].obs_id, cs[0].span))
        self.assertIn("src/shop/ledger/sync.py", cs[0].paths)

    def test_cap_and_priority(self):
        text = " ".join("src/shop/m%d.py is unused by reconcile_day." % i for i in range(3)) + \
            " Fixed: reconcile_day now calls normalize_tz. tests/test_sync.py passes. " + \
            " ".join("normalize_tz is used in src/shop/ledger/sync.py step %d." % i for i in range(4))
        cs = self.claims(text)
        self.assertLessEqual(len(cs), 5)
        self.assertIn("conclusion", [c.claim_class for c in cs])
        self.assertIn("status", [c.claim_class for c in cs])

    def test_explicit_claim(self):
        cs = self.claims("normalize_tz fixed it", kind="claim")
        self.assertEqual(len(cs), 1)
        self.assertTrue(cs[0].explicit)

    def test_claimed_outcome(self):
        self.assertEqual(claimed_outcome("test_x fails with KeyError"), "fail")
        self.assertEqual(claimed_outcome("test_x no longer fails"), "pass")
        self.assertEqual(claimed_outcome("test_x passes now"), "pass")
        self.assertEqual(claimed_outcome("test_x does not pass"), "fail")
        self.assertEqual(claimed_outcome("测试不通过"), "fail")
        self.assertEqual(claimed_outcome("测试通过了"), "pass")
        self.assertIsNone(claimed_outcome("3 passed, 1 failed"))


if __name__ == "__main__":
    unittest.main()
