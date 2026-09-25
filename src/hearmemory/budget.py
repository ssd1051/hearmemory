"""hearmemory.budget -- daily Jev call/spend cap with reserve/settle/release.

ledger/jev.jsonl is append-only like every other raw file, so a reservation is
never rewritten in place: reserve() appends a "reserved" row, settle() appends
the real outcome row, release() appends a "released" row that cancels it. A
reservation a crashed process never settled still counts against the day's
budget for up to an hour (`_RESERVATION_STALE_S`) -- "better to over-count".
"""
from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .interfaces import (DEFAULT_CONFIG, JEV_USD_PER_MILLION_INPUT, LAYOUT, LOCK_TIMEOUT_S_DEFAULT,
                         BudgetStatus, LedgerRow, estimate_usd)
from .locks import file_lock
from .textutil import now_ts

_RESERVATION_STALE_S = 3600.0


def _today_utc(now: Optional[float] = None) -> str:
    ts = datetime.fromtimestamp(now, tz=timezone.utc) if now is not None else datetime.now(timezone.utc)
    return ts.strftime("%Y-%m-%d")


@dataclass
class _Reservation:
    id: str
    ts: str
    day: str
    est_tokens: int
    est_usd: float
    candidate_id: Optional[str]
    template_id: Optional[str]
    session_id: Optional[str]
    settled: bool = False


class Budget:
    """the core's implementation of `interfaces.ENTRY_POINTS["budget"]`."""

    def __init__(self, store: Any, config: Mapping[str, Any]) -> None:
        self.store = store
        self.config = config or {}
        self._reservations: Dict[str, _Reservation] = {}

    def _jev_cfg(self) -> Dict[str, Any]:
        merged = dict(DEFAULT_CONFIG["jev"])
        merged.update(self.config.get("jev") or {})
        return merged

    def _ledger_path(self) -> Path:
        return self.store.hearmemory_dir / LAYOUT["ledger"]

    def _lock_path(self) -> Path:
        return self.store.hearmemory_dir / "locks" / "ledger.lock"

    def _ensure_ledger_dir(self) -> None:
        ledger_dir = self._ledger_path().parent
        if self.store.is_initialised() and not ledger_dir.exists():
            try:
                os.mkdir(ledger_dir)
            except FileExistsError:
                pass
            except OSError:
                pass
        locks_dir = self._lock_path().parent
        if self.store.is_initialised() and not locks_dir.exists():
            try:
                os.mkdir(locks_dir)
            except OSError:
                pass

    def _append_row(self, row: LedgerRow) -> None:
        if not self.store.is_initialised():
            return
        self._ensure_ledger_dir()
        import json
        line = json.dumps(row.to_dict(), ensure_ascii=False) + "\n"
        with file_lock(self._lock_path(), LOCK_TIMEOUT_S_DEFAULT) as ok:
            if ok:
                try:
                    with open(self._ledger_path(), "a", encoding="utf-8") as f:
                        f.write(line)
                    return
                except OSError:
                    pass
        spool_dir = self.store.hearmemory_dir / "spool"
        try:
            if self.store.is_initialised() and not spool_dir.exists():
                os.mkdir(spool_dir)
            with open(spool_dir / f"ledger-{os.getpid()}-{time.time_ns()}.jsonl", "w",
                      encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass

    def _read_today_rows(self, day: str) -> List[Dict[str, Any]]:
        path = self._ledger_path()
        rows: List[Dict[str, Any]] = []
        if not path.exists():
            return rows
        import json
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("day") == day:
                    rows.append(d)
        return rows

    def status(self, *, now: Optional[float] = None) -> BudgetStatus:
        jcfg = self._jev_cfg()
        day = _today_utc(now)
        rows = self._read_today_rows(day)
        calls = 0
        usd = 0.0
        # persisted rows: every non-reserved/released row is a completed call attempt.
        for r in rows:
            outcome = r.get("outcome")
            if outcome in ("reserved", "released"):
                continue
            calls += 1
            usd += float(r.get("est_usd") or 0.0)
        # outstanding reservations from *this* process (in memory: authoritative, and cheap).
        wall_now = now if now is not None else time.time()
        in_memory_candidate_ids = set()
        in_memory_ts = set()
        for res in self._reservations.values():
            if res.settled or res.day != day:
                continue
            if res.candidate_id:
                in_memory_candidate_ids.add(res.candidate_id)
            else:
                in_memory_ts.add(res.ts)
            if wall_now - _parse_epoch(res.ts) <= _RESERVATION_STALE_S:
                calls += 1
                usd += res.est_usd
        # outstanding reservations recorded on disk by *any other* process (crash recovery): a
        # "reserved" row with no later non-reserved/released row for the same candidate_id, and
        # not already counted above from this process's own in-memory bookkeeping.
        reserved_rows = [r for r in rows if r.get("outcome") == "reserved"]
        settled_candidate_ids = {r.get("candidate_id") for r in rows
                                 if r.get("outcome") not in ("reserved", None) and r.get("candidate_id")}
        for r in reserved_rows:
            cid = r.get("candidate_id")
            if cid:
                if cid in settled_candidate_ids or cid in in_memory_candidate_ids:
                    continue
            elif r.get("ts") in in_memory_ts:
                continue
            if wall_now - _parse_epoch(r.get("ts", "")) > _RESERVATION_STALE_S:
                continue
            calls += 1
            usd += float(r.get("est_usd") or 0.0)
        return BudgetStatus(day=day, calls=calls, usd=round(usd, 8),
                             call_cap=int(jcfg.get("daily_call_cap", 200)),
                             usd_cap=float(jcfg.get("daily_usd_cap", 0.05)))

    def reserve(self, est_tokens: int, *, candidate_id: Optional[str] = None,
                template_id: Optional[str] = None, session_id: Optional[str] = None,
                now: Optional[float] = None) -> Optional[str]:
        """Returns a reservation id, or None if the day's budget is already exhausted."""
        jcfg = self._jev_cfg()
        if self.status(now=now).exhausted:
            return None
        rate = float(jcfg.get("usd_per_million_input", JEV_USD_PER_MILLION_INPUT))
        est_usd = estimate_usd(int(est_tokens), rate)
        day = _today_utc(now)
        rid = uuid.uuid4().hex
        res = _Reservation(id=rid, ts=now_ts(), day=day, est_tokens=int(est_tokens), est_usd=est_usd,
                            candidate_id=candidate_id, template_id=template_id, session_id=session_id)
        self._reservations[rid] = res
        self._append_row(LedgerRow(ts=res.ts, day=day, candidate_id=candidate_id, template_id=template_id,
                                   outcome="reserved", input_tokens=int(est_tokens),
                                   input_tokens_estimated=True, est_usd=est_usd, session_id=session_id))
        return rid

    def settle(self, reservation_id: str, outcome: str, *, input_tokens: Optional[int] = None,
               output_tokens: Optional[int] = None, input_tokens_estimated: bool = False,
               latency_s: Optional[float] = None, model: Optional[str] = None,
               error: Optional[str] = None) -> None:
        res = self._reservations.get(reservation_id)
        if res is None:
            return
        res.settled = True
        jcfg = self._jev_cfg()
        rate = float(jcfg.get("usd_per_million_input", JEV_USD_PER_MILLION_INPUT))
        if outcome == "permission_denied":
            row = LedgerRow(ts=now_ts(), day=res.day, candidate_id=res.candidate_id,
                            template_id=res.template_id, outcome=outcome, session_id=res.session_id,
                            model=model, latency_s=latency_s)
        else:
            tokens = input_tokens if input_tokens is not None else res.est_tokens
            estimated = bool(input_tokens_estimated or input_tokens is None)
            est_usd = estimate_usd(int(tokens or 0), rate)
            row = LedgerRow(ts=now_ts(), day=res.day, candidate_id=res.candidate_id,
                            template_id=res.template_id, outcome=outcome, input_tokens=int(tokens or 0),
                            output_tokens=int(output_tokens or 0), input_tokens_estimated=estimated,
                            est_usd=est_usd, latency_s=latency_s, model=model, session_id=res.session_id)
        self._append_row(row)
        self._reservations.pop(reservation_id, None)

    def release(self, reservation_id: str) -> None:
        res = self._reservations.pop(reservation_id, None)
        if res is None:
            return
        self._append_row(LedgerRow(ts=now_ts(), day=res.day, candidate_id=res.candidate_id,
                                   template_id=res.template_id, outcome="released",
                                   session_id=res.session_id))


def _parse_epoch(ts: str) -> float:
    if not ts:
        return 0.0
    try:
        s = ts.rstrip("Z")
        dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f") if "." in s else datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
        return dt.replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 0.0
