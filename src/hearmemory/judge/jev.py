"""Jev judge (optional dependency `typesafe_sdk`, imported lazily).

Availability has two layers:
  * process-local (interfaces.JEV_LOCAL_REASONS: no key / no SDK / config or privacy off / Codex sandbox
    without network). These describe THIS process only: never written to state/jev_health.json, never a
    Judgment, never a queue change. `jev_capability(cfg, environ)` reports them.
  * project-shared (interfaces.JevHealth): only facts true for every process. `unreachable_until` is written
    only by a capable process after a REAL transport error; `auth_denied` is keyed by key_fingerprint(key),
    so a 401 for one key never blocks another key. Any success clears `unreachable_until`.

One call per candidate, question key interfaces.QUESTION_KEY; validation follows
jev_client_co._extract_answers; cache before network; budget reserve -> settle / release (core Budget).
The API key is read from the environment only and is never logged, stored, or put in an error string.
"""
from __future__ import annotations

import importlib.util
import json
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from hearmemory import interfaces as I
from hearmemory.judge import _compat as C
from hearmemory.judge.cache import JudgmentCache
from hearmemory.judge.templates import TEMPLATES, make_question, question_payload

UNREACHABLE_BACKOFF_S = 600.0
AUTH_DENIED_BACKOFF_S = 3600.0
MIN_CALL_TIME_S = 0.5
HEALTH_LOCK = threading.Lock()


# ---------------------------------------------------------------------------------------------------------
def sdk_available() -> bool:
    try:
        return importlib.util.find_spec("typesafe_sdk") is not None
    except (ImportError, ValueError):
        return False


def jev_capability(cfg: Optional[Mapping[str, Any]], environ: Optional[Mapping[str, str]] = None
                   ) -> Tuple[bool, Optional[str]]:
    """(capable, reason). reason is one of interfaces.JEV_LOCAL_REASONS when not capable (process-local)."""
    env = os.environ if environ is None else environ
    if not C.cfg_get(cfg, "jev", "enabled", True):
        return False, "config_disabled"
    if not C.cfg_get(cfg, "privacy", "send_to_jev", True):
        return False, "privacy_disabled"
    if not (env.get(I.JEV_API_KEY_ENV) or "").strip():
        return False, "no_key"
    for var in I.SANDBOX_NO_NETWORK_ENV:
        if str(env.get(var, "")).strip() == "1":
            return False, "sandbox_no_network"
    if not sdk_available():
        return False, "no_sdk"
    return True, None


# ---------------------------------------------------------------------------------------------------------
def read_health(store: Any) -> I.JevHealth:
    try:
        d = store.read_state("jev_health")
    except Exception:
        d = None
    try:
        return I.JevHealth.from_dict(d) if isinstance(d, dict) else I.JevHealth()
    except Exception:
        return I.JevHealth()


def _update_health(store: Any, fn) -> None:
    if store is None:
        return
    with HEALTH_LOCK:
        h = read_health(store)
        fn(h)
        try:
            store.write_state("jev_health", h.to_dict())
        except Exception:
            pass


def health_block(store: Any, api_key: Optional[str], now: float) -> Optional[str]:
    """'unreachable' / 'auth_denied' when the shared health says this key should not call now."""
    if store is None:
        return None
    h = read_health(store)
    if h.unreachable_until and C.parse_ts(h.unreachable_until) > now:
        return "unreachable"
    if api_key:
        until = (h.auth_denied or {}).get(I.key_fingerprint(api_key))
        if until and C.parse_ts(until) > now:
            return "auth_denied"
    return None


def classify_error(exc: BaseException) -> Tuple[str, bool]:
    """(outcome, network_fault). network_fault=True only for real transport failures (connection refused,
    DNS, TLS, timeout, 5xx/408/429) - the only errors allowed to write JevHealth.unreachable_until."""
    status = getattr(exc, "status", None)
    if status is None:
        status = getattr(exc, "status_code", None)
    name = type(exc).__name__
    if status in (401, 403) or name in ("TypeSafeAuthenticationError", "TypeSafePermissionDeniedError"):
        return "permission_denied", False
    if "ResponseValidation" in name:
        return "validation_error", False
    if isinstance(status, int):
        if status >= 500 or status in (408, 429):
            return "transport_error", True
        return "validation_error", False
    if isinstance(exc, (ConnectionError, TimeoutError)) or "Connection" in name or "Timeout" in name \
            or "RateLimit" in name or "InternalServer" in name:
        return "transport_error", True
    if isinstance(exc, OSError):
        return "transport_error", True
    return "transport_error", False


def extract_answer(raw: Mapping[str, Any], template_id: str) -> Tuple[str, Dict[str, float], float]:
    """Ported from jev_client_co._extract_answers (choice only). Raises ValueError on any shape problem."""
    criteria = TEMPLATES[template_id]["criteria"]
    answers = raw.get("answers") or {}
    if I.QUESTION_KEY not in answers:
        raise ValueError("answer missing for question %r" % I.QUESTION_KEY)
    item = answers[I.QUESTION_KEY]
    if not isinstance(item, Mapping) or item.get("type") != "choice":
        raise ValueError("expected choice answer, got %r" % (item.get("type") if isinstance(item, Mapping) else None))
    label = item.get("choice")
    if label not in criteria:
        raise ValueError("choice %r not in criteria" % (label,))
    probs = item.get("probabilities") or {}
    if set(probs) != set(criteria):
        raise ValueError("choice probabilities do not cover exactly the criteria")
    return str(label), {k: float(v) for k, v in probs.items()}, float(item.get("confidence", 0.0) or 0.0)


def estimate_input_tokens(state: Any, template_id: str) -> int:
    s = json.dumps(state, ensure_ascii=False, sort_keys=True)
    q = json.dumps({I.QUESTION_KEY: question_payload(template_id)}, ensure_ascii=False, sort_keys=True)
    return max(1, int(math.ceil((len(s) + len(q)) / 2.0)))


def _deep_redact(v: Any, cfg: Optional[Mapping[str, Any]]) -> Any:
    if isinstance(v, str):
        return C.redact(v, cfg)[0]
    if isinstance(v, list):
        return [_deep_redact(x, cfg) for x in v]
    if isinstance(v, dict):
        return {k: _deep_redact(x, cfg) for k, x in v.items()}
    return v


def _safe_error(msg: str, secrets: Sequence[str] = ()) -> str:
    out = C.redact(str(msg or ""))[0]
    for s in secrets:
        if s:
            out = out.replace(s, "[REDACTED]")
    return out[:200]


# ---------------------------------------------------------------------------------------------------------
class JevJudge:
    """JudgeAPI for provider "jev". `client` may be injected (tests: a fake with .system_one)."""
    name = "jev"

    def __init__(self, cfg: Optional[Mapping[str, Any]] = None, store: Any = None, budget: Any = None,
                 client: Any = None, environ: Optional[Mapping[str, str]] = None, clock: Optional[C.Clock] = None,
                 cache: Optional[JudgmentCache] = None, session_id: Optional[str] = None) -> None:
        self.cfg = cfg or {}
        self.store = store
        env = os.environ if environ is None else environ
        self.capable, self.reason = jev_capability(self.cfg, env)
        self._api_key = (env.get(I.JEV_API_KEY_ENV) or "").strip() or None
        self.model = str(C.cfg_get(self.cfg, "jev", "model", I.JEV_MODEL_DEFAULT))
        self.base_url = str(C.cfg_get(self.cfg, "jev", "base_url", I.JEV_BASE_URL_DEFAULT))
        self.timeout_s = float(C.cfg_get(self.cfg, "jev", "timeout_s", 8.0))
        self.max_calls = int(C.cfg_get(self.cfg, "jev", "max_calls_per_run", 40))
        self.concurrency = max(1, int(C.cfg_get(self.cfg, "jev", "max_concurrency", 4)))
        self.clock = clock or time.time
        self.session_id = session_id
        self._client = client
        self._budget = budget
        self._budget_loaded = budget is not None
        self.cache = cache if cache is not None else (JudgmentCache.from_store(store) if store is not None else JudgmentCache())
        self._lock = threading.Lock()
        self._reserve_lock = threading.Lock()
        self._calls = 0
        self.stop_reason: Optional[str] = None
        self.stats: Dict[str, int] = {}

    # -- lazy resources ---------------------------------------------------------------------------------
    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        with self._lock:                # integration bug (resume 2026-09-24): see budget() below.
            if self._client is None:
                from typesafe_sdk import RetryPolicy, TypeSafeClient
                self._client = TypeSafeClient(api_key=self._api_key, model=self.model, base_url=self.base_url,
                                              retry=RetryPolicy(max_retries=0, timeout=None))
        return self._client

    def budget(self) -> Any:
        """Thread-safe double-checked init (integration bug found live 2026-09-24, `judge()` runs
        candidates concurrently via ThreadPoolExecutor by default - max_concurrency=4): the old
        version set `self._budget_loaded = True` BEFORE actually constructing `self._budget`, with
        no lock. A second thread's `budget()` call landing in that window saw `_budget_loaded=True`
        and returned the still-None `self._budget`, which `_judge_one` then reported as outcome
        "budget_blocked"/error="no budget" for a perfectly healthy, non-exhausted budget - losing
        real judgments (and their Jev spend) to a race, not to an actual cap. Reproduced live: of 4
        concurrent candidates in an integration smoke test, only the first got a real judgment;
        the other 3 spuriously failed with "no budget" even though nothing was exhausted."""
        if self._budget_loaded:
            return self._budget
        with self._lock:
            if not self._budget_loaded:
                try:
                    from hearmemory.budget import Budget
                    self._budget = Budget(self.store, self.cfg) if self.store is not None else None
                except Exception:
                    self._budget = None
                finally:
                    self._budget_loaded = True
        return self._budget

    def budget_exhausted(self) -> bool:
        b = self.budget()
        if b is None:
            return True
        try:
            return bool(b.status(now=self.clock()).exhausted)
        except TypeError:
            return bool(b.status().exhausted)
        except Exception:
            return True

    def blocked(self) -> Optional[str]:
        """Why no call may be made right now (process-local reason, shared health, or a stop in this run)."""
        if not self.capable:
            return self.reason
        if self.stop_reason:
            return self.stop_reason
        return health_block(self.store, self._api_key, self.clock())

    def _count(self, k: str) -> None:
        with self._lock:
            self.stats[k] = self.stats.get(k, 0) + 1

    # -- JudgeAPI -----------------------------------------------------------------------------------------
    def judge(self, cands: Sequence[I.Candidate], deadline_s: float = 30.0) -> List[I.Judgment]:
        if not self.capable:
            return []                     # process-local: no judgment, no health write, no queue change
        why = health_block(self.store, self._api_key, self.clock())
        if why:
            self.stop_reason = why
            return []
        t_end = time.monotonic() + max(0.0, float(deadline_s))
        cands = [c for c in cands if c.template_id in TEMPLATES]
        if not cands:
            return []
        out: List[Optional[I.Judgment]] = [None] * len(cands)
        if self.concurrency == 1 or len(cands) == 1:
            for i, c in enumerate(cands):
                out[i] = self._judge_one(c, t_end)
        else:
            with ThreadPoolExecutor(max_workers=min(self.concurrency, len(cands))) as ex:
                futs = [ex.submit(self._judge_one, c, t_end) for c in cands]
                for i, f in enumerate(futs):
                    try:
                        out[i] = f.result()
                    except Exception:
                        out[i] = None
        return [j for j in out if j is not None]

    def _mk(self, c: I.Candidate, outcome: str, **kw: Any) -> I.Judgment:
        ts = C.now_ts(self.clock)
        return I.Judgment(judgment_id=I.stable_id("j-", c.candidate_id, "jev", ts, outcome, threading.get_ident()),
                          candidate_id=c.candidate_id, template_id=c.template_id,
                          template_version=c.template_version, input_hash=c.input_hash, provider="jev",
                          outcome=outcome, ts=ts, model_requested=self.model, **kw)

    def _judge_one(self, c: I.Candidate, t_end: float) -> Optional[I.Judgment]:
        if self.stop_reason:
            return None
        remaining = t_end - time.monotonic()
        if remaining < MIN_CALL_TIME_S:
            self._count("deadline")
            return None
        meta = c.meta or {}
        ev_paths = list(meta.get("evidence_paths") or [])
        if any(C.jev_excluded(p, self.cfg) for p in ev_paths):
            self._count("disabled")
            return self._mk(c, "disabled", error="evidence under privacy.jev_exclude_globs")
        hit = self.cache.lookup(c, self.model, self.clock)
        if hit is not None:
            self._count("cache")
            return hit
        with self._lock:
            if self._calls >= self.max_calls:
                self.stats["call_cap"] = self.stats.get("call_cap", 0) + 1
                return None
            self._calls += 1
        state = _deep_redact(c.state, self.cfg)
        est = estimate_input_tokens(state, c.template_id)
        budget = self.budget()
        rid = None
        if budget is not None:
            with self._reserve_lock:          # check-then-reserve must be atomic across this judge's threads
                try:
                    rid = budget.reserve(est, candidate_id=c.candidate_id, template_id=c.template_id,
                                         session_id=self.session_id)
                except Exception:
                    rid = None
        if rid is None:
            self._count("budget_blocked")
            return self._mk(c, "budget_blocked", error="daily Jev budget reached" if budget is not None else "no budget")
        try:
            question = make_question(c.template_id)
        except ImportError:
            question = question_payload(c.template_id)
        t0 = time.monotonic()
        raw: Any = None
        err: Optional[Tuple[str, bool, str]] = None
        try:
            resp = self._get_client().system_one(state=state, questions={I.QUESTION_KEY: question},
                                                 timeout=max(MIN_CALL_TIME_S, min(self.timeout_s, remaining)))
            raw = resp.model_dump() if hasattr(resp, "model_dump") else resp
        except BaseException as exc:          # noqa: BLE001 - any SDK failure is classified, never raised
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            kind, net = classify_error(exc)
            err = (kind, net, "%s: %s" % (type(exc).__name__, exc))
        latency = round(time.monotonic() - t0, 3)
        if err is not None:
            kind, net, msg = err
            msg = _safe_error(msg, [self._api_key or ""])
            if kind == "permission_denied":
                self._release(rid)
                self.stop_reason = "auth_denied"
                fp = I.key_fingerprint(self._api_key or "")
                until = C.ts_of(self.clock() + AUTH_DENIED_BACKOFF_S)

                def _auth(h: I.JevHealth) -> None:
                    h.auth_denied = dict(h.auth_denied or {})
                    h.auth_denied[fp] = until
                    h.last_error_kind = "permission_denied"
                _update_health(self.store, _auth)
                self._count("permission_denied")
                return self._mk(c, "permission_denied", error=msg, latency_s=latency)
            self._settle(rid, kind, est, 0, True, latency, None)
            if kind == "transport_error" and net:
                self.stop_reason = "unreachable"
                until = C.ts_of(self.clock() + UNREACHABLE_BACKOFF_S)

                def _unreach(h: I.JevHealth) -> None:
                    h.unreachable_until = until
                    h.unreachable_reporter_pid = os.getpid()
                    h.last_error_kind = "transport_error"
                _update_health(self.store, _unreach)
            self._count(kind)
            return self._mk(c, kind, error=msg, latency_s=latency, input_tokens=est,
                            est_usd=I.estimate_usd(est, self._rate()))
        # ---- response
        self._mark_ok()
        if not isinstance(raw, Mapping) or "answers" not in raw:
            self._settle(rid, "validation_error", est, 0, True, latency, None)
            self._count("validation_error")
            return self._mk(c, "validation_error", error="response missing answers", latency_s=latency)
        model_returned = raw.get("model")
        usage = raw.get("usage") or {}
        in_tok = usage.get("input_tokens") if isinstance(usage, Mapping) else None
        out_tok = (usage.get("output_tokens") if isinstance(usage, Mapping) else None) or 0
        estimated = not in_tok
        in_tok = int(in_tok) if in_tok else est
        usd = I.estimate_usd(in_tok, self._rate())
        if model_returned and model_returned != self.model:
            self._settle(rid, "fallback_detected", in_tok, out_tok, estimated, latency, model_returned)
            self._count("fallback_detected")
            return self._mk(c, "fallback_detected", model_returned=model_returned, latency_s=latency,
                            input_tokens=in_tok, output_tokens=int(out_tok), est_usd=usd,
                            error="returned model differs from pinned model")
        try:
            label, probs, conf = extract_answer(raw, c.template_id)
        except ValueError as exc:
            self._settle(rid, "validation_error", in_tok, out_tok, estimated, latency, model_returned)
            self._count("validation_error")
            return self._mk(c, "validation_error", model_returned=model_returned, latency_s=latency,
                            input_tokens=in_tok, output_tokens=int(out_tok), est_usd=usd, error=_safe_error(str(exc)))
        self._settle(rid, "valid", in_tok, out_tok, estimated, latency, model_returned)
        self._count("valid")
        j = self._mk(c, "valid", label=label, probabilities=probs, confidence=conf, model_returned=model_returned,
                     latency_s=latency, input_tokens=in_tok, output_tokens=int(out_tok), est_usd=usd)
        self.cache.add(j)
        return j

    # -- helpers ------------------------------------------------------------------------------------------
    def _rate(self) -> float:
        return float(C.cfg_get(self.cfg, "jev", "usd_per_million_input", I.JEV_USD_PER_MILLION_INPUT))

    def _settle(self, rid: Any, outcome: str, in_tok: int, out_tok: int, estimated: bool, latency: float,
                model: Optional[str]) -> None:
        b = self.budget()
        if b is None or rid is None:
            return
        try:
            b.settle(rid, outcome, input_tokens=int(in_tok), output_tokens=int(out_tok or 0),
                     input_tokens_estimated=bool(estimated), latency_s=latency, model=model)
        except Exception:
            pass

    def _release(self, rid: Any) -> None:
        b = self.budget()
        if b is None or rid is None:
            return
        try:
            b.release(rid)
        except Exception:
            pass

    def _mark_ok(self) -> None:
        now_ts = C.now_ts(self.clock)

        def _ok(h: I.JevHealth) -> None:
            h.unreachable_until = None
            h.unreachable_reporter_pid = None
            h.last_ok_ts = now_ts
        h = read_health(self.store) if self.store is not None else None
        if h is not None and (h.unreachable_until or not h.last_ok_ts or
                              C.parse_ts(now_ts) - C.parse_ts(h.last_ok_ts) > 60):
            _update_health(self.store, _ok)
