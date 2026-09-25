"""store.py -- JSONL append/iter, dedupe, concurrency, corruption recovery."""
from __future__ import annotations

import json
import multiprocessing
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import init_project, make_observation, make_provenance  # noqa: E402

from hearmemory.interfaces import LAYOUT, STORE_FORMAT, Candidate, ControlEvent, Observation
from hearmemory.store import Store, create_store, open_store


def _write_batch(root: str, worker_id: int, n: int) -> None:
    """Runs in a child process (multiprocessing stress test)."""
    store = Store(Path(root))
    obs = []
    for j in range(n - 10):
        obs.append(make_observation(event_key=f"w{worker_id}-{j}", text=f"obs {worker_id}-{j}"))
    for k in range(10):
        obs.append(make_observation(event_key=f"dup-{k}", text=f"shared {k} from {worker_id}"))
    # append in small chunks to increase interleaving between processes
    for i in range(0, len(obs), 7):
        store.append_observations(obs[i:i + 7])


class TestStoreBasics(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = init_project(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_is_initialised_and_version(self):
        self.assertTrue(self.store.is_initialised())
        self.assertEqual(self.store.version(), STORE_FORMAT)
        self.assertTrue(self.store.version_recognised())

    def test_open_store_finds_root_from_subdir(self):
        sub = self.root / "a" / "b"
        sub.mkdir(parents=True)
        found = open_store(sub)
        self.assertIsNotNone(found)
        self.assertEqual(found.root, self.root)

    def test_open_store_missing_returns_none(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(open_store(d, create=False))
            created = open_store(d, create=True)
            self.assertIsNotNone(created)
            self.assertFalse(created.is_initialised())

    def test_append_and_iter_observations_roundtrip(self):
        obs = [make_observation(event_key=f"k{i}", text=f"text {i}") for i in range(5)]
        ids = self.store.append_observations(obs)
        self.assertEqual(len(ids), 5)
        got = [o for _, o in self.store.iter_observations()]
        self.assertEqual([o.id for o in got], ids)
        self.assertEqual([o.text for o in got], [f"text {i}" for i in range(5)])

    def test_append_is_O1_no_read_of_raw_file(self):
        """append_* must never read the raw file it writes to: patch `open` to fail
        on read mode and confirm appends still succeed."""
        obs = [make_observation(event_key=f"o1k{i}") for i in range(3)]
        self.store.append_observations(obs)
        import builtins
        real_open = builtins.open

        def guarded_open(file, mode="r", *a, **kw):
            if "r" in mode and "observations.jsonl" in str(file) and "a" not in mode:
                raise AssertionError("append_observations must not read the raw file")
            return real_open(file, mode, *a, **kw)

        builtins.open = guarded_open
        try:
            more = [make_observation(event_key=f"o1k-more-{i}") for i in range(3)]
            self.store.append_observations(more)
        finally:
            builtins.open = real_open
        got = list(self.store.iter_observations())
        self.assertEqual(len(got), 6)

    def test_read_dedupes_by_id_first_occurrence_wins(self):
        path = self.store._raw_path("observations")
        o1 = make_observation(event_key="dupk", text="first")
        o2 = make_observation(event_key="dupk", text="second")
        self.assertEqual(o1.id, o2.id)
        self.store.append_observations([o1])
        self.store.append_observations([o2])
        got = [o for _, o in self.store.iter_observations()]
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].text, "first")
        self.assertEqual(self.store.last_read_stats["observations"]["duplicate_ids"], 1)

    def test_iter_events_and_candidates_and_judgments_and_claims(self):
        from hearmemory.interfaces import Judgment, Claim
        cand = Candidate(candidate_id="k-1", template_id="A1", template_version="hm-A1.1",
                         subject_key="pair:a|b", state={}, input_hash="h1", basis_obs_ids=[],
                         created_ts="2026-09-24T00:00:00.000000Z")
        self.store.append_candidates([cand])
        self.assertEqual([c.candidate_id for c in self.store.iter_candidates()], ["k-1"])

        judgment = Judgment(judgment_id="j-1", candidate_id="k-1", template_id="A1",
                           template_version="hm-A1.1", input_hash="h1", provider="rule",
                           outcome="valid", ts="2026-09-24T00:00:00.000000Z", label="same")
        self.store.append_judgments([judgment])
        self.assertEqual([j.judgment_id for j in self.store.iter_judgments()], ["j-1"])

        claim = Claim(claim_id="c-1", obs_id="o-1", span=[0, 5], text="hello", claim_class="other")
        self.store.append_claims([claim])
        self.assertEqual([c.claim_id for c in self.store.iter_claims()], ["c-1"])

        ev = ControlEvent(id="e-1", ts="2026-09-24T00:00:00.000000Z", kind="seen", target="o-1")
        self.store.append_events([ev])
        self.assertEqual([e.id for e in self.store.iter_events()], ["e-1"])

    def test_read_observations_window_advances_offset_and_dedupes_within_batch(self):
        obs = [make_observation(event_key=f"win{i}") for i in range(3)]
        obs.append(make_observation(event_key="win0"))  # duplicate id within the same window
        self.store.append_observations(obs)
        offset, got = self.store.read_observations_window(0, 10 ** 9)
        self.assertEqual(len(got), 3)
        self.assertGreater(offset, 0)
        offset2, got2 = self.store.read_observations_window(offset, 10 ** 9)
        self.assertEqual(got2, [])
        self.assertEqual(offset2, offset)

    def test_corrupted_raw_file_recovery(self):
        obs = [make_observation(event_key=f"c{i}") for i in range(3)]
        self.store.append_observations(obs)
        path = self.store._raw_path("observations")
        # two corrupt-but-newline-terminated lines (a writer's append is a single write() of a
        # complete line, so a malformed line never blocks the next writer) ...
        with open(path, "a", encoding="utf-8") as f:
            f.write("{not valid json\n")
            f.write(json.dumps({"schema": "hearmemory.obs/1"}) + "\n")  # valid json, missing required fields

        # ... more good rows appended normally through the API still land as complete lines ...
        good = [make_observation(event_key=f"c{i}") for i in range(3, 5)]
        self.store.append_observations(good)

        # ... and a torn trailing line (simulating a crash mid-write) is tolerated by being
        # ignored, without disturbing anything that was already read.
        with open(path, "a", encoding="utf-8") as f:
            f.write('{"id": "o-partiallinewithoutnewline"')  # truncated, no trailing \n

        got = [o for _, o in self.store.iter_observations()]
        # 3 original + 2 more good ones; the malformed / incomplete-field / truncated lines are skipped
        self.assertEqual(len(got), 5)
        stats = self.store.last_read_stats["observations"]
        self.assertGreaterEqual(stats["corrupt_lines"], 2)

    def test_missing_version_blocks_writes_without_recreating_hearmemory(self):
        (self.store.hearmemory_dir / LAYOUT["version"]).unlink()
        self.assertFalse(self.store.is_initialised())
        # append_* never raises; the write is simply dropped because .hearmemory/VERSION is gone, and no
        # new file/dir is created as a side effect of trying.
        self.store.append_observations([make_observation(event_key="never")])
        got = list(self.store.iter_observations())
        self.assertEqual(got, [])
        obs_path = self.store.hearmemory_dir / LAYOUT["observations"]
        self.assertTrue(not obs_path.exists() or obs_path.stat().st_size == 0)

    def test_merge_spool_folds_spool_files_into_raw_and_archives_them(self):
        spool_dir = self.store.hearmemory_dir / "spool"
        obs = make_observation(event_key="spooled1")
        with open(spool_dir / "observations-999-1.jsonl", "w", encoding="utf-8") as f:
            f.write(json.dumps(obs.to_dict(), ensure_ascii=False) + "\n")
        result = self.store.merge_spool()
        self.assertEqual(result["merged"], 1)
        got = [o for _, o in self.store.iter_observations()]
        self.assertEqual([o.id for o in got], [obs.id])
        self.assertFalse((spool_dir / "observations-999-1.jsonl").exists())
        self.assertTrue((self.store.hearmemory_dir / "archive" / "spool" / "observations-999-1.jsonl").exists())

    def test_fingerprint_changes_after_append(self):
        fp1 = self.store.fingerprint()
        self.store.append_observations([make_observation(event_key="fpk")])
        fp2 = self.store.fingerprint()
        self.assertNotEqual(fp1, fp2)

    def test_state_read_write_roundtrip_atomic(self):
        self.assertIsNone(self.store.read_state("worker"))
        self.store.write_state("worker", {"pid": 123, "mode": "daemon"})
        data = self.store.read_state("worker")
        self.assertEqual(data["pid"], 123)

    def test_write_state_creates_nested_subdirectory(self):
        """A per-session state file lives under state/sessions/ (a subdirectory core must create
        one level at a time); write_state() with a name not in STATE_FILES falls back to
        state/<name>.json, exercising the same nested-directory path when `name` has a "/"."""
        self.store._state_path = lambda name: self.store.hearmemory_dir / "state" / "sessions" / f"{name}.json"
        try:
            self.store.write_state("sid1", {"count": 1})
            self.assertEqual(self.store.read_state("sid1")["count"], 1)
        finally:
            del self.store._state_path


class TestStoreConcurrency(unittest.TestCase):
    def test_eight_process_stress_with_duplicate_event_keys(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            init_project(root)
            n_workers = 8
            n_per_worker = 200
            ctx = multiprocessing.get_context("fork" if hasattr(os, "fork") else "spawn")
            procs = [ctx.Process(target=_write_batch, args=(str(root), i, n_per_worker))
                    for i in range(n_workers)]
            for p in procs:
                p.start()
            for p in procs:
                p.join(timeout=60)
                self.assertEqual(p.exitcode, 0)

            store = Store(root)
            merged = store.merge_spool()
            self.assertIsInstance(merged["merged"], int)

            seen_ids = set()
            corrupt_total = 0
            for _, obs in store.iter_observations():
                self.assertNotIn(obs.id, seen_ids)
                seen_ids.add(obs.id)
            corrupt_total += store.last_read_stats["observations"]["corrupt_lines"]

            expected_unique = n_workers * (n_per_worker - 10) + 10  # 10 dup keys shared across all workers
            self.assertEqual(len(seen_ids), expected_unique)
            self.assertEqual(corrupt_total, 0)


if __name__ == "__main__":
    unittest.main()
