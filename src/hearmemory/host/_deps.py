"""Soft, monkeypatch-friendly imports of the core/judge/memory entry points host calls.

The host adapters must keep working when the core / memory / judge modules are not importable
(a partial install or a broken environment). Every
name below is resolved lazily and defensively so that:

  * importing anything under hearmemory.host never raises just because a sibling module has not
    landed yet (ImportError is swallowed, the name is left None);
  * tests can monkeypatch a single, stable attribute HERE (hearmemory.host._deps.NAME) instead of
    reaching into hearmemory.store / hearmemory.config / ... directly, regardless of whether the real
    module is present in this checkout;
  * once core/memory/judge land, `refresh()` (or a fresh interpreter) picks up the real
    implementations automatically -- nothing else under hearmemory.host needs to change.

This module holds no state of its own and has no side effects beyond the best-effort imports.
"""
from __future__ import annotations

import importlib
import re
from datetime import datetime, timezone
from typing import Any, Callable, List, Optional

from hearmemory import interfaces as I

_ENTRY_POINTS = {
    "open_store": "hearmemory.store:open_store",
    "load_config": "hearmemory.config:load_config",
    "file_lock": "hearmemory.locks:file_lock",
    "capture_provenance": "hearmemory.provenance:capture",
    "make_observation": "hearmemory.observe:make_observation",
    "redact": "hearmemory.privacy:redact",
    "is_excluded": "hearmemory.privacy:is_excluded",
    "Deadline": "hearmemory.safety:Deadline",
    "run_guarded": "hearmemory.safety:run_guarded",
    "spawn_worker": "hearmemory.judge.worker:spawn_background",
    "stop_worker_fn": "hearmemory.judge.worker:stop_worker",
    "load_memory": "hearmemory.memory.build:load_or_rebuild",
    "mem_check": "hearmemory.memory.precommit:check",
    "build_brief": "hearmemory.memory.brief:build_brief",
    "parse_test_summary": "hearmemory.observe:parse_test_summary",
}


def _soft(dotted: Optional[str]) -> Optional[Callable[..., Any]]:
    if not dotted:
        return None
    mod_name, _, attr = dotted.rpartition(":")
    if not mod_name:
        return None
    try:
        mod = importlib.import_module(mod_name)
        return getattr(mod, attr, None)
    except Exception:
        return None


def refresh() -> None:
    """Re-resolve every dependency against the current sys.modules. Call this in tests after
    injecting a fake module into sys.modules, or after a real core module becomes importable."""
    g = globals()
    for name, dotted in _ENTRY_POINTS.items():
        g[name] = _soft(dotted)


refresh()


import time as _time


class FallbackDeadline:
    """Minimal stand-in for hearmemory.safety.Deadline, used only while that module is not
    importable. Same two-method surface real callers need: slice_ms(step) is the ms budget left for
    that step (0 once there isn't enough), reserving HOOK_RESERVED_STEPS for steps other than
    themselves; remaining_ms() is what's left of the profile's total."""

    def __init__(self, profile: str, hooks_cfg: Optional[dict]):
        self.profile = profile
        self.steps = dict(I.HOOK_STEP_BUDGETS_MS[profile])
        total_key = I.HOOK_PROFILE_TOTAL_KEY[profile]
        default_total = sum(self.steps.values()) + I.HOOK_BUDGET_SLACK_MS
        self.total_ms = float((hooks_cfg or {}).get(total_key, 0) or default_total)
        self._start = _time.monotonic()
        self._reserved_ms = sum(self.steps.get(s, 0) for s in I.HOOK_RESERVED_STEPS)

    def elapsed_ms(self) -> float:
        return (_time.monotonic() - self._start) * 1000.0

    def remaining_ms(self) -> float:
        return max(0.0, self.total_ms - self.elapsed_ms())

    def slice_ms(self, step: str) -> float:
        want = float(self.steps.get(step, 0))
        reserve = 0.0 if step in I.HOOK_RESERVED_STEPS else float(self._reserved_ms)
        return max(0.0, min(want, self.remaining_ms() - reserve))


import threading as _threading


def run_with_timeout(fn, timeout_s: float, *args, **kwargs) -> None:
    """Best-effort bound on a step that has no cooperative way to check a deadline itself (e.g. a
    Codex rollout import doing file/subprocess IO). Runs `fn` in a
    daemon thread and returns after `timeout_s` REGARDLESS of whether it finished, so a slow step
    never makes the hook late; a step still running in the background when we move on cannot block
    the agent (it is a daemon thread) and, since it only ever APPENDS to append-only files under a
    lock, finishing a little late is harmless."""
    if timeout_s <= 0:
        return
    t = _threading.Thread(target=lambda: _safe_call(fn, args, kwargs), daemon=True)
    t.start()
    t.join(timeout_s)


def _safe_call(fn, args, kwargs) -> None:
    try:
        fn(*args, **kwargs)
    except Exception:
        pass


def now_ts() -> str:
    """UTC timestamp in the project's canonical format. Used for host-owned records (manifests,
    control events built here) when hearmemory.textutil.now_ts is not available; the format
    matches the core format exactly."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _fallback_observation(cfg: Optional[dict], kind: str, text: str, provenance: "I.Provenance",
                           tool: Optional["I.ToolInfo"], event_key: Optional[str],
                           refs, meta: Optional[dict]) -> "I.Observation":
    """Degraded observation builder used ONLY while hearmemory.observe.make_observation is not
    importable. It does NOT redact (no hearmemory.privacy yet) -- every observation it builds carries
    meta._fallback_no_redact=True so downstream code / tests can tell. Truncation, id and hash
    follow the real spec so behaviour converges once core lands and this path stops
    being used automatically (make_observation takes priority, see build_observation())."""
    text = text or ""
    max_chars = int(((cfg or {}).get("capture") or {}).get("max_text_chars", I.MAX_TEXT_CHARS_DEFAULT))
    full_sha = I.sha256_text(text)
    truncated = False
    if len(text) > max_chars > 0:
        head = max_chars * 5 // 8
        tail = max(0, max_chars - head)
        omitted = len(text) - head - tail
        marker = f"…[hearmemory: {omitted} chars truncated]…"
        text = text[:head] + marker + (text[len(text) - tail:] if tail else "")
        truncated = True
    ek = event_key or I.stable_id("ek-", kind, full_sha, getattr(provenance, "host", None))
    meta = dict(meta or {})
    meta["_fallback_no_redact"] = True
    return I.Observation(
        id=I.obs_id_for(ek), ts=now_ts(), kind=kind, event_key=ek, provenance=provenance, text=text,
        tool=tool, text_sha256=full_sha, truncated=truncated, redactions=0, excluded=False,
        refs=list(refs or []), meta=meta,
    )


_PYTEST_FAILED_LINE_RE = re.compile(r"^FAILED\s+(\S+)", re.MULTILINE)
_GO_FAIL_RE = re.compile(r"^--- FAIL:\s+(\S+)", re.MULTILINE)


def _normalise_target(cmd: str) -> str:
    parts = (cmd or "").split()
    keep = []
    skip_next = False
    for p in parts:
        if skip_next:
            skip_next = False
            continue
        if p == "-k":
            skip_next = False
            keep.append(p)
            continue
        if p.startswith("-") and p != "-k":
            continue
        keep.append(p)
    return " ".join(keep).strip() or (cmd or "").strip()


def _fallback_parse_test_summary(command: Optional[str], output: Optional[str]):
    """Reduced stand-in for hearmemory.observe.parse_test_summary, used only while that
    module is not importable. Covers pytest/unittest/go well enough for the host layer's own tests; the real
    parser (all of pytest/unittest/jest/go/cargo) takes priority automatically once core lands."""
    text = output or ""
    cmd = command or ""
    m_failed = re.search(r"(\d+)\s+failed", text)
    m_passed = re.search(r"(\d+)\s+passed", text)
    m_error = re.search(r"(\d+)\s+errors?\b", text)
    m_skipped = re.search(r"(\d+)\s+skipped", text)
    if "pytest" in cmd or m_failed or m_passed or m_skipped:
        if not (m_failed or m_passed or m_error or m_skipped):
            return None
        return I.RunnerSummary(runner="pytest", passed=int(m_passed.group(1)) if m_passed else 0,
                                failed=int(m_failed.group(1)) if m_failed else 0,
                                errors=int(m_error.group(1)) if m_error else 0,
                                skipped=int(m_skipped.group(1)) if m_skipped else 0,
                                failed_ids=_PYTEST_FAILED_LINE_RE.findall(text)[:20],
                                target=_normalise_target(cmd))
    ran_m = re.search(r"Ran (\d+) tests?", text)
    if ran_m:
        ok = " OK" in text or text.rstrip().endswith("OK")
        fail_m = re.search(r"failures=(\d+)", text)
        err_m = re.search(r"errors=(\d+)", text)
        n = int(ran_m.group(1))
        failed = int(fail_m.group(1)) if fail_m else (0 if ok else n)
        return I.RunnerSummary(runner="unittest", passed=n - failed, failed=failed,
                                errors=int(err_m.group(1)) if err_m else 0, target=_normalise_target(cmd))
    if "go test" in cmd or _GO_FAIL_RE.search(text):
        fails = _GO_FAIL_RE.findall(text)
        return I.RunnerSummary(runner="go", failed=len(fails), passed=0 if fails else 1,
                                failed_ids=fails[:20], target=_normalise_target(cmd))
    return None


def get_test_summary(command: Optional[str], output: Optional[str]):
    if parse_test_summary is not None:
        return parse_test_summary(command, output)
    return _fallback_parse_test_summary(command, output)


def build_observation(root, cfg: Optional[dict], kind: str, text: str, provenance: "I.Provenance", *,
                       tool: Optional["I.ToolInfo"] = None, event_key: Optional[str] = None,
                       refs=(), meta: Optional[dict] = None, event_ts: Optional[str] = None,
                       historical: bool = False) -> "I.Observation":
    """the host layer's single call site for turning a normalised host event into an Observation. Prefers the
    real core builder (redaction, exclusion, canonical truncation); falls back to a reduced local
    builder when hearmemory.observe is not yet importable (see _fallback_observation)."""
    if make_observation is not None:
        kw = {}
        if event_ts is not None or historical:
            # only passed when set, so a stand-in make_observation without these
            # keywords (tests / older builds) keeps working for live events.
            kw = {"event_ts": event_ts, "historical": historical}
        try:
            return make_observation(root, cfg, kind, text, provenance, tool=tool, event_key=event_key,
                                     refs=refs, meta=meta, **kw)
        except TypeError:
            if not kw:
                raise
            return make_observation(root, cfg, kind, text, provenance, tool=tool, event_key=event_key,
                                     refs=refs, meta=meta)
    obs = _fallback_observation(cfg, kind, text, provenance, tool, event_key, refs, meta)
    if event_ts:
        import dataclasses as _dc
        obs = _dc.replace(obs, ts=event_ts)
    return obs
