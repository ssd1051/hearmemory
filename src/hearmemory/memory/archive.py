"""Archive sweep (program rule, never asks a model, never deletes).

Archived = left out of default recall and briefs; data untouched; `archive_restore` events bring an id back
to hot; `recall --include-archive` shows it tagged [ARCHIVED]. An observation / claim is archived when it is
older than archive.min_age_days and no blocker applies:
  * related to an open issue (source observation, the issue's claim, or a shared path);
  * endpoint of a non-retracted edge or of a mark;
  * a refuted / disputed claim whose status changed within archive.keep_disputed_days, or its counter-evidence;
  * the latest run of a test target (the current program fact);
  * (limitation) "shown in a brief / check within 30 days" is not tracked here: session state is per
    session and not a raw input of the pure rebuild.
"""
from __future__ import annotations

from typing import Dict, Iterable, Mapping, Set

from hearmemory.interfaces import OPEN_ISSUE_STATUSES

from .ops import obs_of_node
from .relevance import norm_path
from .text import ts_seconds

DAY = 86400.0


def sweep(obs_meta: Mapping[str, Mapping], claims: Mapping, edges: Mapping, marks: Mapping, issues: Mapping,
          latest_run_obs: Iterable[str], restored: Iterable[str], now_ts: str, cfg_archive: Mapping) -> Dict[str, str]:
    """obs_meta[obs_id] = {"ts", "paths"}; claims = {claim_id: ClaimView}. Returns tiers {id: "archive"}."""
    now = ts_seconds(now_ts)
    if now is None:
        return {}
    min_age = float(cfg_archive.get("min_age_days", 7)) * DAY
    keep_disputed = float(cfg_archive.get("keep_disputed_days", 30)) * DAY
    restored = set(restored)
    blocked: Set[str] = set(latest_run_obs) | restored
    open_paths: Set[str] = set()
    for iss in issues.values():
        if iss.status not in OPEN_ISSUE_STATUSES:
            continue
        blocked |= set(iss.source_obs_ids)
        if iss.claim_id:
            blocked.add(iss.claim_id)
            cv = claims.get(iss.claim_id)
            if cv is not None:
                blocked.add(cv.claim.obs_id)
        open_paths |= {norm_path(p) for p in iss.paths}
    for e in edges.values():
        if e.status != "retracted":
            blocked.add(obs_of_node(e.a))
            blocked.add(obs_of_node(e.b))
    for m in marks.values():
        blocked.add(obs_of_node(m.a))
        if m.b:
            blocked.add(obs_of_node(m.b))
    for cid, cv in claims.items():
        if cv.status in ("refuted", "disputed") or cv.premise_status in ("refuted", "disputed"):
            st = ts_seconds(cv.status_ts) or now
            if now - st < keep_disputed:
                blocked |= {cid, cv.claim.obs_id} | set(cv.counter_ids)
    tiers: Dict[str, str] = {}
    for oid, meta in obs_meta.items():
        if oid in blocked:
            continue
        t = ts_seconds(meta.get("ts"))
        if t is None or now - t < min_age:
            continue
        if open_paths and {norm_path(p) for p in meta.get("paths") or ()} & open_paths:
            continue
        tiers[oid] = "archive"
    for cid, cv in claims.items():
        if cid in blocked or cid in restored:
            continue
        if tiers.get(cv.claim.obs_id) == "archive":
            tiers[cid] = "archive"
    return dict(sorted(tiers.items()))
