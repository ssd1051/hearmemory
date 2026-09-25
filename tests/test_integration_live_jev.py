"""the integration tests, item (f): a TINY live Jev smoke test on real fixture pairs.

This is the ONLY test in the whole suite allowed to touch the real network / spend real money
(contract MONEY: "Live Jev only in the integration tests: at most 30 tiny calls total, logged in
$REPO/.dev_ledger.jsonl with tokens and est USD at $0.042/1M input"). Every other test file
(including test_integration_e2e.py) unsets TYPESAFE_API_KEY and never imports typesafe_sdk for
real. This file does the opposite on purpose, but only when a key is actually present:

  - No TYPESAFE_API_KEY in the environment -> the whole module is skipped (this is exactly the
    "no key -> degrade, never fail" contract; a contributor without a key, or plain `pytest`, never
    makes a network call or fails because of one).
  - With a key: it fires at most `MAX_LIVE_CALLS` (4, one per judgment template A1/A2/A3/B1) tiny,
    hand-built candidates through the REAL JevJudge -> real typesafe_sdk client -> real API, then
    appends one row per attempted call to `$REPO/.dev_ledger.jsonl` (repo dev ledger, distinct from
    a project's own `.hearmemory/ledger/jev.jsonl`) so the running total against the $1 project cap is
    visible in git history.

Run explicitly (it is not part of the default no-key CI/dev loop):
    TYPESAFE_API_KEY=... .venv/bin/python -m pytest tests/test_integration_live_jev.py -v -s
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
sys.path.insert(0, str(SRC))

import hearmemory.interfaces as I  # noqa: E402
from hearmemory.judge.jev import JevJudge  # noqa: E402
from hearmemory.store import create_store  # noqa: E402

DEV_LEDGER = REPO / ".dev_ledger.jsonl"
MAX_LIVE_CALLS = 4  # one per template; keeps the whole project's cumulative live-call count tiny


def _cand(tid: str, subject_key: str, state: dict, basis_obs_ids=()) -> I.Candidate:
    h = I.input_hash(tid, I.TEMPLATE_VERSIONS[tid], state)
    return I.Candidate(
        candidate_id=I.candidate_id_for(tid, I.TEMPLATE_VERSIONS[tid], subject_key, None, h),
        template_id=tid, template_version=I.TEMPLATE_VERSIONS[tid], subject_key=subject_key, state=state,
        input_hash=h, basis_obs_ids=list(basis_obs_ids) or ["o-smoke0000000001"],
        created_ts="2026-09-24T12:00:00.000000Z")


def _fixture_candidates() -> list:
    """A few small, unambiguous fixture pairs - real answers are not asserted (that is a separate,
    larger accuracy spot-check the maintainer runs by hand per PLAN.md 6.3); this smoke test only
    checks the wire is live end to end: a real HTTP round trip, a parseable typesafe_sdk response,
    and `extract_answer` accepting its shape."""
    return [
        _cand("A1", "pair:path:jobs/sync.py|path:ledger/sync.py", {
            "record_a": {"text": "Codex: the failing job calls jobs/sync.py:run(), which raises KeyError.",
                        "marked_mention": "jobs/sync.py"},
            "record_b": {"text": "Claude: I fixed ledger/sync.py, an unrelated billing reconciliation script.",
                        "marked_mention": "ledger/sync.py"},
            "trusted_context": {"project": "smoke-demo", "object_kind": "path",
                                "object_granularity": "a concrete code object of this project: file / module / "
                                                      "class / function / service / test / config key",
                                "known_candidates": ["jobs/sync.py", "ledger/sync.py"],
                                "source_a": "codex session, at 2026-09-24T11:05Z",
                                "source_b": "claude subagent, at 2026-09-24T12:58Z"}}),
        _cand("A2", "pair:evt:a|evt:b", {
            "record_a": {"text": "pytest tests/test_sync.py -> 1 failed: KeyError: 'tz' in parse_row",
                        "marked_event": "a failing run of tests/test_sync.py raising KeyError('tz')"},
            "record_b": {"text": "pytest tests/test_reconcile.py -> 1 failed: ValueError: bad amount",
                        "marked_event": "a failing run of tests/test_reconcile.py raising ValueError"},
            "trusted_context": {"project": "smoke-demo", "target_a": "tests/test_sync.py",
                                "target_b": "tests/test_reconcile.py", "commit_a": "a1b2c3d", "commit_b": "a1b2c3d",
                                "time_a": "2026-09-24T11:05Z", "time_b": "2026-09-24T11:07Z",
                                "source_a": "codex session, at 2026-09-24T11:05Z",
                                "source_b": "codex session, at 2026-09-24T11:07Z"}}),
        _cand("A3", "pair:container|claim", {
            "record_a": {"text": "All sync jobs in this repo retry on transient network errors up to 3 times."},
            "record_b": {"claim": "jobs/sync.py specifically retries HTTP 503s up to 3 times."},
            "trusted_context": {"project": "smoke-demo", "scope_a": "repo-wide", "scope_b": "jobs/sync.py"}}),
        _cand("B1", "claim:smoke0001", {
            "target_claim": "`pytest tests/test_sync.py` now passes after the fix.",
            "target_scope": {"project": "smoke-demo", "branch": "main", "commit": "a1b2c3d",
                             "paths": ["tests/test_sync.py"]},
            "evidence": [{"text": "$ pytest tests/test_sync.py\n1 failed, 0 passed in 0.10s\n"
                                 "FAILED tests/test_sync.py::test_sync",
                         "source": "command run by claude at 2026-09-24T13:00Z, commit a1b2c3d"}]}),
    ][:MAX_LIVE_CALLS]


@unittest.skipUnless((os.environ.get("TYPESAFE_API_KEY") or "").strip(),
                     "no TYPESAFE_API_KEY: live Jev smoke test skipped (no-key degradation contract)")
class LiveJevSmoke(unittest.TestCase):
    """Runs ONLY when explicitly given a key; see module docstring for how to invoke it."""

    def test_real_api_roundtrip_on_fixture_pairs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "live_smoke_project"
            root.mkdir()
            store = create_store(root)
            self.assertTrue(store.is_initialised())

            cfg = {"jev": {"enabled": True, "max_calls_per_run": MAX_LIVE_CALLS}}
            judge = JevJudge(cfg=cfg, store=store, session_id="integration-live-smoke")
            self.assertTrue(judge.capable, judge.reason)

            cands = _fixture_candidates()
            self.assertLessEqual(len(cands), MAX_LIVE_CALLS)
            t0 = time.time()
            results = judge.judge(cands, deadline_s=60.0)
            self.assertEqual(len(results), len(cands), "every candidate must get a Judgment (never silently dropped)")

            rows = []
            for cand, j in zip(cands, results):
                self.assertIn(j.outcome, I.JUDGMENT_OUTCOMES, j)
                # A transport hiccup on one live call must not fail the whole smoke test; only a
                # judge that never even attempts a call (e.g. capability wrongly false) should.
                if j.outcome == "valid":
                    self.assertIn(j.label, I.TEMPLATE_LABELS[cand.template_id],
                                 f"{cand.template_id}: label {j.label!r} not in {I.TEMPLATE_LABELS[cand.template_id]}")
                rows.append({
                    "ts": j.ts, "run": "integration_live_smoke", "template_id": cand.template_id,
                    "candidate_id": cand.candidate_id, "outcome": j.outcome, "model_requested": j.model_requested,
                    "model_returned": j.model_returned, "label": j.label,
                    "input_tokens": j.input_tokens, "output_tokens": j.output_tokens,
                    "est_usd": j.est_usd, "latency_s": j.latency_s,
                })
            self.assertLessEqual(time.time() - t0, 60.0)

            with DEV_LEDGER.open("a", encoding="utf-8") as f:
                for row in rows:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")

            total_usd = sum((r.get("est_usd") or 0.0) for r in rows)
            print(f"\n[live jev smoke] {len(rows)} calls, ~${total_usd:.6f} est, "
                 f"appended to {DEV_LEDGER}")


if __name__ == "__main__":
    unittest.main()
