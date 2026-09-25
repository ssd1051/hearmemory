"""Judgment queue (.hearmemory/state/queue.json): candidate_id -> {status, attempts, next_try_ts, last_error,
updated_ts, priority, template_id, rule}. Derived state: rebuilt from candidates.jsonl + judgments.jsonl when lost.
pending -> judged | rule_judged | skipped | failed | superseded."""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional

from hearmemory import interfaces as I
from hearmemory.judge import _compat as C

QUEUE_SCHEMA = "hearmemory.queue/1"
BACKOFF_BASE_S = 30.0
BACKOFF_MAX_S = 3600.0


class Queue:
    def __init__(self, entries: Optional[Mapping[str, Mapping[str, Any]]] = None) -> None:
        self.entries: Dict[str, Dict[str, Any]] = {k: dict(v) for k, v in (entries or {}).items()}

    # -- persistence ----------------------------------------------------------------------------------
    @classmethod
    def load(cls, store: Any) -> "Queue":
        try:
            d = store.read_state("queue")
        except Exception:
            d = None
        if isinstance(d, dict) and d.get("schema") == QUEUE_SCHEMA and isinstance(d.get("entries"), dict):
            return cls(d["entries"])
        return cls.rebuild(store)

    @classmethod
    def rebuild(cls, store: Any, clock: Optional[C.Clock] = None) -> "Queue":
        q = cls()
        now = C.now_ts(clock)
        try:
            cands = list(store.iter_candidates())
            js = list(store.iter_judgments())
        except Exception:
            return q
        for c in cands:
            q.add(c, now)
        final = {}
        for j in js:
            if j.outcome in ("valid", "validation_error", "fallback_detected", "disabled"):
                final[j.candidate_id] = j
        for c in cands:
            j = final.get(c.candidate_id)
            if j is not None:
                q.set(c.candidate_id, "rule_judged" if j.provider == "rule" else
                      ("judged" if j.outcome == "valid" else "skipped"), now)
            if c.supersedes and c.supersedes in q.entries and q.entries[c.supersedes]["status"] == "pending":
                q.set(c.supersedes, "superseded", now)
        return q

    def save(self, store: Any) -> None:
        store.write_state("queue", {"schema": QUEUE_SCHEMA, "entries": self.entries})

    # -- transitions ----------------------------------------------------------------------------------
    def add(self, c: I.Candidate, now_ts: str) -> None:
        if c.candidate_id in self.entries:
            return
        self.entries[c.candidate_id] = {"status": "pending", "attempts": 0, "next_try_ts": None, "last_error": None,
                                        "updated_ts": now_ts, "priority": int(c.priority),
                                        "template_id": c.template_id, "rule": bool(c.rule_hint),
                                        "created_ts": c.created_ts}
        if c.supersedes and c.supersedes in self.entries:
            if self.entries[c.supersedes]["status"] == "pending":
                self.set(c.supersedes, "superseded", now_ts)

    def set(self, cid: str, status: str, now_ts: str, error: Optional[str] = None) -> None:
        e = self.entries.get(cid)
        if e is None or status not in I.CANDIDATE_STATUSES:
            return
        e["status"] = status
        e["updated_ts"] = now_ts
        if error is not None:
            e["last_error"] = error[:200]

    def transport_error(self, cid: str, now: float, max_attempts: int, error: str = "") -> str:
        e = self.entries.get(cid)
        if e is None:
            return "missing"
        e["attempts"] = int(e.get("attempts", 0)) + 1
        e["last_error"] = (error or "transport_error")[:200]
        e["updated_ts"] = C.ts_of(now)
        if e["attempts"] >= int(max_attempts):
            e["status"] = "failed"
        else:
            delay = min(BACKOFF_BASE_S * (2 ** e["attempts"]), BACKOFF_MAX_S)
            e["next_try_ts"] = C.ts_of(now + delay)
        return e["status"]

    def defer(self, cid: str, until: float, reason: str = "") -> None:
        e = self.entries.get(cid)
        if e is not None:
            e["next_try_ts"] = C.ts_of(until)
            if reason:
                e["last_error"] = reason[:200]

    def reset_failed(self, now_ts: str) -> int:
        n = 0
        for e in self.entries.values():
            if e.get("status") == "failed":
                e.update({"status": "pending", "attempts": 0, "next_try_ts": None, "updated_ts": now_ts})
                n += 1
        return n

    # -- queries --------------------------------------------------------------------------------------
    def due(self, now: float) -> List[str]:
        """Pending, non-rule candidates whose backoff has elapsed, most urgent first."""
        out = []
        for cid, e in self.entries.items():
            if e.get("status") != "pending" or e.get("rule"):
                continue
            if e.get("next_try_ts") and C.parse_ts(e["next_try_ts"]) > now:
                continue
            out.append(cid)
        return sorted(out, key=lambda cid: (self.entries[cid].get("priority", 50),
                                            self.entries[cid].get("created_ts") or "", cid))

    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for e in self.entries.values():
            out[e.get("status", "?")] = out.get(e.get("status", "?"), 0) + 1
        return out

    def waiting_for_jev(self) -> int:
        return sum(1 for e in self.entries.values() if e.get("status") == "pending" and not e.get("rule"))
