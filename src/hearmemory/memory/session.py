"""Per-session presentation state (state/sessions/<sid>.json, state/git_holds.json).

  sessions/<key>: {"shown": {item_key: {"ts", "count"}}, "holds": {attempt_key: ts}, "last_push_ts": ts}
  git_holds:      {<staged tree hash>: ts}
Derived, safe to lose (worst case an item is shown again or a hold_once holds again). Every write checks
store.is_initialised() first (never creates .hearmemory) and swallows errors."""
from __future__ import annotations

from typing import Any, Dict, Optional

SESSION_PREFIX = "sessions/"


def _initialised(store: Any) -> bool:
    try:
        return bool(store.is_initialised())
    except Exception:
        return False


def load(store: Any, name: str) -> Dict[str, Any]:
    if store is None:
        return {}
    try:
        d = store.read_state(name)
        return dict(d) if isinstance(d, dict) else {}
    except Exception:
        return {}


def save(store: Any, name: str, data: Dict[str, Any]) -> bool:
    if store is None or not _initialised(store):
        return False
    try:
        store.write_state(name, data)
        return True
    except Exception:
        return False


def load_session(store: Any, key: Optional[str]) -> Dict[str, Any]:
    if not key:
        return {}
    d = load(store, SESSION_PREFIX + key)
    d.setdefault("shown", {})
    d.setdefault("holds", {})
    return d


def save_session(store: Any, key: Optional[str], data: Dict[str, Any], max_shown: int = 500) -> bool:
    if not key:
        return False
    shown = data.get("shown") or {}
    if len(shown) > max_shown:
        keep = sorted(shown.items(), key=lambda kv: kv[1].get("ts") or "", reverse=True)[:max_shown]
        data["shown"] = dict(keep)
    return save(store, SESSION_PREFIX + key, data)


def record_shown_event(store: Any, ctx: Any, claim_ids: Any, via: str, source: Optional[str] = None,
                       now: Optional[str] = None) -> bool:
    """append ControlEvent(kind="memory_shown") saying which memory claims hearmemory just delivered to
    the requesting session (brief / recall), so the MemoryBuilder can tell a later restatement of them by that
    agent (memory echo) from an independent finding. One O(1) append; never raises, never creates .hearmemory."""
    ids = sorted({str(c) for c in claim_ids or () if c})
    if not ids or store is None or ctx is None or not getattr(ctx, "host", None) or not _initialised(store):
        return False
    try:
        from hearmemory.interfaces import ControlEvent, Provenance, stable_id
        from hearmemory.textutil import now_ts
        ts = now or now_ts()
        prov = Provenance(host=ctx.host, session_id=getattr(ctx, "session_id", None),
                          subagent_id=getattr(ctx, "subagent_id", None), source=source)
        target = f"{ctx.host}:{getattr(ctx, 'session_id', None) or '?'}"
        ev = ControlEvent(id=stable_id("ev-shown-", target, getattr(ctx, "subagent_id", None), via, ids, ts), ts=ts,
                          kind="memory_shown", target=target, data={"claim_ids": ids, "via": via}, provenance=prov)
        store.append_events([ev])
        return True
    except Exception:
        return False
