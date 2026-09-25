"""Fresh overlay: what happened after memory.json was built.

Reads at most `max_bytes` of observations.jsonl after MemoryState.obs_offset (store.read_observations_window,
read-side dedupe) and only adds NEW test run results (latest pass / fail per target) and the paths this
session touched. It never judges anything and never writes. Any error -> an empty overlay."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional, Set

from hearmemory.interfaces import MemoryState, Observation, actor_key

from .relevance import norm_path
from .runs import RunIndex, run_record

OVERLAY_MAX_BYTES = 256 * 1024


@dataclass
class Overlay:
    runs: RunIndex
    new_obs: List[Observation] = field(default_factory=list)
    session_paths: List[str] = field(default_factory=list)
    end_offset: int = 0
    ok: bool = False


def fresh_overlay(state: MemoryState, store: Any, max_bytes: int = OVERLAY_MAX_BYTES,
                  session_id: Optional[str] = None) -> Overlay:
    runs = RunIndex((state.stats or {}).get("runs") or {}).copy()
    ov = Overlay(runs=runs, end_offset=int(state.obs_offset or 0))
    if store is None:
        return ov
    try:
        end, obs = store.read_observations_window(int(state.obs_offset or 0), int(max_bytes))
    except Exception:
        return ov
    seen: Set[str] = set()
    paths: List[str] = []
    for o in obs or []:
        if o is None or o.id in seen:
            continue
        seen.add(o.id)
        ov.new_obs.append(o)
        rec = run_record(o, actor_key(o.provenance))
        if rec is not None:
            runs.add(rec)
        if session_id and o.provenance and o.provenance.session_id == session_id:
            for p in o.paths:
                p = norm_path(p)
                if p and p not in paths:
                    paths.append(p)
    ov.session_paths = paths
    ov.end_offset = int(end or ov.end_offset)
    ov.ok = True
    return ov
