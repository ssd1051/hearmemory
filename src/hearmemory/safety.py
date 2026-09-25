"""hearmemory.safety -- hook time budgets.

Deadline gives every hook step a soft slice of the profile's total budget, always
leaving the reserved steps ("startup", "render") their full share. run_guarded is
the hard backstop: it makes sure a hook process exits (code 0) even if something
inside hangs or raises, using SIGALRM where available.
"""
from __future__ import annotations

import signal
import time
from typing import Any, Callable, Mapping, Optional

from .interfaces import (HOOK_BUDGET_SLACK_MS, HOOK_PROFILE_TOTAL_KEY, HOOK_RESERVED_STEPS,
                         HOOK_STEP_BUDGETS_MS, HookResult, hook_budget_fits)


class Deadline:
    """One hook invocation's time budget for a given profile (interfaces.HOOK_PROFILES value)."""

    def __init__(self, profile: str, hooks_cfg: Optional[Mapping[str, Any]] = None) -> None:
        if profile not in HOOK_STEP_BUDGETS_MS:
            raise ValueError(f"unknown deadline profile: {profile!r}")
        self.profile = profile
        self._steps = dict(HOOK_STEP_BUDGETS_MS[profile])
        hooks_cfg = hooks_cfg or {}
        total_key = HOOK_PROFILE_TOTAL_KEY[profile]
        try:
            self.total_ms = float(hooks_cfg.get(total_key, 0) or 0)
        except (TypeError, ValueError):
            self.total_ms = 0.0
        self._start = time.monotonic()
        self._scale = 1.0
        if self.total_ms > 0 and not hook_budget_fits(profile, hooks_cfg):
            reserved_sum = sum(v for k, v in self._steps.items() if k in HOOK_RESERVED_STEPS)
            other_sum = sum(v for k, v in self._steps.items() if k not in HOOK_RESERVED_STEPS)
            budget_for_other = max(0.0, self.total_ms - HOOK_BUDGET_SLACK_MS - reserved_sum)
            if other_sum > 0:
                self._scale = max(0.0, budget_for_other / other_sum)

    def _elapsed_ms(self) -> float:
        return (time.monotonic() - self._start) * 1000.0

    def remaining_ms(self) -> float:
        if self.total_ms <= 0:
            return 0.0
        return max(0.0, self.total_ms - self._elapsed_ms())

    def slice_ms(self, step: str) -> float:
        """min(this step's slice, remaining time - what the still-unused reserved steps need)."""
        base = self._steps.get(step, 0.0)
        if step not in HOOK_RESERVED_STEPS:
            base = base * self._scale
        reserved_needed = sum(v for k, v in self._steps.items() if k in HOOK_RESERVED_STEPS and k != step)
        remaining = self.remaining_ms()
        return max(0.0, min(base, remaining - reserved_needed))

    def expired(self) -> bool:
        return self.total_ms > 0 and self._elapsed_ms() >= self.total_ms


def run_guarded(fn: Callable[[], HookResult], total_ms: float) -> HookResult:
    """Hard backstop for a hook entry point: fn() must return a HookResult; if it raises, hangs
    past total_ms, or returns something else, the hook still exits cleanly with code 0.
    Uses SIGALRM (main thread, POSIX only); elsewhere it is a plain best-effort try/except."""
    total_s = max(0.05, float(total_ms) / 1000.0)
    has_alarm = hasattr(signal, "SIGALRM") and hasattr(signal, "setitimer")
    old_handler = None

    class _Timeout(Exception):
        pass

    def _on_alarm(signum, frame):  # pragma: no cover - exercised via signal only
        raise _Timeout()

    if has_alarm:
        try:
            old_handler = signal.signal(signal.SIGALRM, _on_alarm)
            signal.setitimer(signal.ITIMER_REAL, total_s)
        except (ValueError, RuntimeError):
            has_alarm = False

    try:
        result = fn()
        if not isinstance(result, HookResult):
            return HookResult(exit_code=0, stdout="", stderr="")
        return result
    except _Timeout:
        return HookResult(exit_code=0, stdout="", stderr="hearmemory: hook timed out")
    except Exception as exc:  # noqa: BLE001 - a hook must never propagate
        return HookResult(exit_code=0, stdout="", stderr=f"hearmemory: hook error ({type(exc).__name__})")
    finally:
        if has_alarm:
            try:
                signal.setitimer(signal.ITIMER_REAL, 0)
                if old_handler is not None:
                    signal.signal(signal.SIGALRM, old_handler)
            except (ValueError, RuntimeError):
                pass
