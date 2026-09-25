"""locks.py -- fcntl-based file_lock, timeout -> caller falls back to spool."""
from __future__ import annotations

import multiprocessing
import tempfile
import time
import unittest
from pathlib import Path

from hearmemory.locks import file_lock


def _hold_lock(path: str, hold_s: float, ready_flag, release_flag) -> None:
    with file_lock(Path(path), timeout_s=5.0) as ok:
        if ok:
            ready_flag.set()
            release_flag.wait(timeout=hold_s + 5)


class TestFileLock(unittest.TestCase):
    def test_acquire_and_release_same_process(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.lock"
            with file_lock(p, timeout_s=1.0) as ok:
                self.assertTrue(ok)
            with file_lock(p, timeout_s=1.0) as ok2:
                self.assertTrue(ok2)

    def test_second_process_times_out_while_first_holds_lock(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.lock"
            ctx = multiprocessing.get_context("fork")
            ready = ctx.Event()
            release = ctx.Event()
            proc = ctx.Process(target=_hold_lock, args=(str(p), 2.0, ready, release))
            proc.start()
            self.assertTrue(ready.wait(timeout=5))
            start = time.monotonic()
            with file_lock(p, timeout_s=0.3) as ok:
                elapsed = time.monotonic() - start
                self.assertFalse(ok)
                self.assertLess(elapsed, 1.0)
            release.set()
            proc.join(timeout=5)
            self.assertEqual(proc.exitcode, 0)

    def test_non_blocking_zero_timeout_tries_once(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "pipeline.lock"
            ctx = multiprocessing.get_context("fork")
            ready = ctx.Event()
            release = ctx.Event()
            proc = ctx.Process(target=_hold_lock, args=(str(p), 1.0, ready, release))
            proc.start()
            self.assertTrue(ready.wait(timeout=5))
            with file_lock(p, timeout_s=0) as ok:
                self.assertFalse(ok)
            release.set()
            proc.join(timeout=5)

    def test_lock_directory_missing_returns_false_not_raise(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "nosuchdir" / "x.lock"
            with file_lock(p, timeout_s=0.1) as ok:
                self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
