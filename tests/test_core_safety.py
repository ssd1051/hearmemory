"""safety.py -- Deadline slices/reserves, run_guarded hard backstop."""
from __future__ import annotations

import time
import unittest

from hearmemory.interfaces import HookResult, hook_budget_fits
from hearmemory.safety import Deadline, run_guarded


class TestDeadline(unittest.TestCase):
    def test_record_profile_fits_default_budget(self):
        cfg = {"timeout_ms": 1500}
        self.assertTrue(hook_budget_fits("record", cfg))
        d = Deadline("record", cfg)
        self.assertAlmostEqual(d._scale, 1.0)
        self.assertGreater(d.slice_ms("startup"), 0)

    def test_render_and_startup_are_reserved_full_share(self):
        d = Deadline("push", {"timeout_ms": 1500})
        self.assertEqual(d.slice_ms("render"), d._steps["render"])

    def test_slice_shrinks_as_time_elapses(self):
        d = Deadline("record", {"timeout_ms": 1500})
        first = d.slice_ms("normalize_append")
        time.sleep(0.05)
        second = d.slice_ms("normalize_append")
        self.assertLessEqual(second, first)

    def test_unknown_profile_raises(self):
        with self.assertRaises(ValueError):
            Deadline("no-such-profile", {})

    def test_scales_down_non_reserved_steps_when_total_too_small(self):
        # session_start needs 250+700+600+400+50=2000ms of steps + 150 slack = 2150,
        # but we configure only 1000ms total -> hook_budget_fits is False -> steps scale down.
        cfg = {"session_start_budget_ms": 1000}
        self.assertFalse(hook_budget_fits("session_start", cfg))
        d = Deadline("session_start", cfg)
        self.assertLess(d._scale, 1.0)
        # reserved steps keep their full nominal size regardless of scale
        self.assertEqual(d.slice_ms("startup"), d._steps["startup"])
        self.assertEqual(d.slice_ms("render"), d._steps["render"])
        # a non-reserved step is scaled down
        self.assertLess(d.slice_ms("memory"), d._steps["memory"])

    def test_remaining_ms_never_negative(self):
        d = Deadline("record", {"timeout_ms": 1})
        time.sleep(0.02)
        self.assertEqual(d.remaining_ms(), 0.0)
        self.assertEqual(d.slice_ms("startup"), 0.0)


class TestRunGuarded(unittest.TestCase):
    def test_normal_result_passes_through(self):
        result = run_guarded(lambda: HookResult(exit_code=0, stdout="ok"), total_ms=500)
        self.assertEqual(result.stdout, "ok")
        self.assertEqual(result.exit_code, 0)

    def test_exception_is_swallowed_and_exit_is_zero(self):
        def boom():
            raise RuntimeError("kaboom")

        result = run_guarded(boom, total_ms=500)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("error", result.stderr)

    def test_hang_past_deadline_is_cut_off_and_exit_is_zero(self):
        def hang():
            time.sleep(5)
            return HookResult(exit_code=0)

        start = time.monotonic()
        result = run_guarded(hang, total_ms=200)
        elapsed = time.monotonic() - start
        self.assertEqual(result.exit_code, 0)
        self.assertLess(elapsed, 2.0)

    def test_non_hookresult_return_value_normalised(self):
        result = run_guarded(lambda: "not a HookResult", total_ms=200)
        self.assertIsInstance(result, HookResult)
        self.assertEqual(result.exit_code, 0)


if __name__ == "__main__":
    unittest.main()
