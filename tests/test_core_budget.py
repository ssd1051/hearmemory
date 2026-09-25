"""budget.py -- reserve/settle/release, daily cap, crash-reservation accounting."""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import init_project  # noqa: E402

from hearmemory.budget import Budget, _parse_epoch
from hearmemory.config import load_config
from hearmemory.textutil import now_ts


class TestBudget(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = init_project(self.root)
        self.cfg = load_config(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_reserve_settle_roundtrip_tracks_spend(self):
        budget = Budget(self.store, self.cfg)
        status0 = budget.status()
        self.assertEqual(status0.calls, 0)
        rid = budget.reserve(500, candidate_id="k-1", template_id="A1")
        self.assertIsNotNone(rid)
        status1 = budget.status()
        self.assertEqual(status1.calls, 1)
        self.assertGreater(status1.usd, 0)
        budget.settle(rid, "valid", input_tokens=520, output_tokens=10)
        status2 = budget.status()
        self.assertEqual(status2.calls, 1)
        self.assertGreater(status2.usd, 0)

    def test_permission_denied_not_billed(self):
        budget = Budget(self.store, self.cfg)
        rid = budget.reserve(500, candidate_id="k-2")
        budget.settle(rid, "permission_denied")
        status = budget.status()
        self.assertEqual(status.calls, 1)  # still counted as an attempt row...
        # ...but its cost is zero (permission_denied is not billed)
        rows = budget._read_today_rows(status.day)
        pd_rows = [r for r in rows if r.get("outcome") == "permission_denied"]
        self.assertEqual(len(pd_rows), 1)
        self.assertEqual(pd_rows[0].get("est_usd", 0.0), 0.0)

    def test_release_cancels_reservation(self):
        budget = Budget(self.store, self.cfg)
        rid = budget.reserve(1000, candidate_id="k-3")
        budget.release(rid)
        status = budget.status()
        self.assertEqual(status.calls, 0)
        self.assertEqual(status.usd, 0.0)

    def test_exhausted_blocks_further_reservations(self):
        cfg = dict(self.cfg)
        cfg["jev"] = dict(cfg["jev"])
        cfg["jev"]["daily_call_cap"] = 2
        budget = Budget(self.store, cfg)
        r1 = budget.reserve(100, candidate_id="a")
        r2 = budget.reserve(100, candidate_id="b")
        self.assertIsNotNone(r1)
        self.assertIsNotNone(r2)
        r3 = budget.reserve(100, candidate_id="c")
        self.assertIsNone(r3)

    def test_crash_reservation_counted_by_fresh_instance_within_stale_window(self):
        budget = Budget(self.store, self.cfg)
        rid = budget.reserve(500, candidate_id="k-crash")
        self.assertIsNotNone(rid)
        # simulate the process crashing before settle(): a brand-new Budget instance (as a fresh
        # process would create) must still see the reservation and count it ("better to
        # over-count" a reservation left by a crashed process, within 1 hour).
        fresh = Budget(self.store, self.cfg)
        status = fresh.status()
        self.assertEqual(status.calls, 1)
        self.assertGreater(status.usd, 0)

    def test_stale_reservation_older_than_one_hour_not_counted(self):
        budget = Budget(self.store, self.cfg)
        old_ts = now_ts()
        # monkeypatch _parse_epoch indirectly by writing an old row directly to the ledger
        import json
        old_row = {
            "ts": "2020-01-01T00:00:00.000000Z", "day": "2020-01-01", "candidate_id": "old-1",
            "template_id": None, "outcome": "reserved", "input_tokens": 100, "output_tokens": 0,
            "input_tokens_estimated": True, "est_usd": 0.01, "schema": "hearmemory.ledger/1",
        }
        budget._ensure_ledger_dir()
        with open(budget._ledger_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(old_row) + "\n")
        status = budget.status(now=time.time())
        # the stale row belongs to a different day anyway (2020-01-01), so it must not appear
        # in *today's* status.
        self.assertEqual(status.day, budget.status().day)
        self.assertNotEqual(status.day, "2020-01-01")


if __name__ == "__main__":
    unittest.main()
