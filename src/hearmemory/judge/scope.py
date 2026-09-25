"""Scope facts: did the code change between moment A and moment B?

Used by rule B1_test_status (via interfaces.b1_status_rule) and rule A2_different_commit_runs. Facts come
from observations of ANY actor: HEAD at both moments, file edits in (ts_a, ts_b] touching watched paths,
and the per-run worktree fingerprint meta.dirty_state (absent on imported runs -> worktree_known=False).
Accepts Observation objects or the extractor's compact summaries (dicts with the same keys).
"""
from __future__ import annotations

from typing import Any, Iterable, List, Mapping, Optional, Sequence

from hearmemory import interfaces as I
from hearmemory.judge import _compat as C


def _g(o: Any, key: str, default: Any = None) -> Any:
    if isinstance(o, Mapping):
        return o.get(key, default)
    return getattr(o, key, default)


def obs_ts(o: Any) -> str:
    return _g(o, "ts") or ""


def obs_commit(o: Any) -> Optional[str]:
    if isinstance(o, Mapping):
        return o.get("commit")
    prov = getattr(o, "provenance", None)
    return getattr(prov, "git_commit", None) if prov is not None else None


def obs_paths(o: Any) -> List[str]:
    if isinstance(o, Mapping):
        return list(o.get("paths") or [])
    try:
        return list(o.paths)
    except Exception:
        return []


def obs_dirty(o: Any) -> Optional[Mapping[str, str]]:
    """meta.dirty_state, or None when unknown (missing, truncated, or imported history)."""
    if isinstance(o, Mapping):
        d = o.get("dirty")
        return d if isinstance(d, Mapping) and not o.get("dirty_truncated") else None
    meta = getattr(o, "meta", None) or {}
    d = meta.get("dirty_state")
    if not isinstance(d, Mapping) or meta.get("dirty_state_truncated"):
        return None
    return d


def scope_facts(observations: Iterable[Any], run_a: Any, run_b: Any, watched_paths: Sequence[str]) -> I.ScopeFacts:
    """ScopeFacts between moment A (run_a: the claim's evidence run, or the claim observation itself) and
    moment B (run_b: a later run of the same target)."""
    ts_a, ts_b = obs_ts(run_a), obs_ts(run_b)
    ea, eb = C.parse_ts(ts_a), C.parse_ts(ts_b)
    if eb < ea:
        ea, eb = eb, ea
    watched = sorted({p for p in watched_paths if p})
    wset = set(watched)
    edited: List[str] = []
    edit_ids: List[str] = []
    for o in observations:
        if _g(o, "kind") != "file_edit":
            continue
        t = C.parse_ts(obs_ts(o))
        if not (ea < t <= eb):
            continue
        hit = [p for p in obs_paths(o) if p in wset]
        if hit:
            edited.extend(hit)
            edit_ids.append(_g(o, "id"))
    da, db = obs_dirty(run_a), obs_dirty(run_b)
    known = da is not None and db is not None
    dirty_changed = [p for p in watched if known and da.get(p) != db.get(p)]
    return I.ScopeFacts(ts_a=ts_a, ts_b=ts_b, commit_a=obs_commit(run_a), commit_b=obs_commit(run_b),
                        watched_paths=watched, edited_paths_between=sorted(set(edited)),
                        dirty_changed_paths=dirty_changed, worktree_known=known,
                        edit_obs_ids=sorted({i for i in edit_ids if i}))
