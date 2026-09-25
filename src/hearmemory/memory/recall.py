"""Recall: BM25 (ASCII words + CJK bigrams) over hot observations, boosted by relevance to the
AgentContext; each result carries an excerpt (600 chars around the best match), provenance, claim status tags,
one-hop edge expansions; A3 equivalents show only the representative; a refuted claim always comes with its
counter-evidence. Archived items only with include_archive (tagged [ARCHIVED])."""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Set

from hearmemory.interfaces import (NON_EVIDENCE_OBS_KINDS, MemoryState, Observation, RecallItem, RecallQuery,
                              RecallResult, actor_key)

from . import render as R
from .brief import _counter_line, representative
from .build import cfg_get
from .relevance import item_idents_of, item_relevance, make_ctx
from .text import bm25_tokens, clip, now_ts, tokenize, ts_seconds

EXCERPT_CHARS = 600
REL_WEIGHT = 0.5
MAX_RELATED = 2


def _excerpt(text: str, q_tokens: List[str]) -> str:
    text = text or ""
    if len(text) <= EXCERPT_CHARS:
        return text
    low = text.lower()
    hits = [p for p in (low.find(t) for t in q_tokens if t) if p >= 0]
    pos = min(hits) if hits else 0
    start = max(0, min(pos - EXCERPT_CHARS // 4, len(text) - EXCERPT_CHARS))
    return ("…" if start else "") + text[start:start + EXCERPT_CHARS] + ("…" if start + EXCERPT_CHARS < len(text) else "")


def _prov_entry(o: Observation, obs_index: Mapping[str, Any]) -> Dict[str, Any]:
    e = obs_index.get(o.id)
    if e:
        return dict(e)
    p = o.provenance
    return {"ts": o.ts, "host": p.host, "session": p.session_id, "subagent": p.subagent_id,
            "subagent_type": p.subagent_type, "commit": p.git_commit, "actor": actor_key(p)}


def recall(state: MemoryState, store: Any, q: RecallQuery, cfg: Optional[Mapping[str, Any]] = None, *,
           now: Optional[str] = None) -> RecallResult:
    now = now or now_ts()
    lang = R.lang_of(cfg_get(cfg, "brief", "lang"))
    stats = state.stats or {}
    obs_index = stats.get("obs_index") or {}
    node_text = stats.get("node_text") or {}
    pending = int(stats.get("pending_judgments") or 0)
    limit = max(1, min(int(q.limit or 8), 50))
    obs: List[Observation] = []
    seen: Set[str] = set()
    if store is not None:
        try:
            for _off, o in store.iter_observations(0):
                if o is None or o.id in seen:
                    continue
                seen.add(o.id)
                obs.append(o)
        except Exception:
            obs = []
    kinds = set(q.kinds or [])
    pool = []
    for o in obs:
        if o.kind in NON_EVIDENCE_OBS_KINDS or (kinds and o.kind not in kinds):
            continue
        archived = state.tiers.get(o.id) == "archive"
        if archived and not q.include_archive:
            continue
        pool.append((o, archived))
    claims_by_obs: Dict[str, List[Any]] = {}
    for cv in state.claims.values():
        claims_by_obs.setdefault(cv.claim.obs_id, []).append(cv)
    q_tokens = tokenize(q.query or "")
    docs = []
    for o, _a in pool:
        extra = " ".join([o.tool.command or "" if o.tool else ""] + list(o.paths))
        docs.append((o.id, tokenize((o.text or "")[:6000] + " " + extra)))
    scores = bm25_tokens(q_tokens, docs) if q_tokens else {}
    top = max(scores.values()) if scores else 0.0
    ctx = make_ctx(q.context)
    ranked = []
    for o, archived in pool:
        s = (scores.get(o.id, 0.0) / top) if top > 0 else 0.0
        rel = item_relevance(o.paths, item_idents_of((o.text or "")[:2000], o.paths), ctx) if not ctx.empty else 0.0
        if q_tokens and s <= 0 and rel <= 0:
            continue
        if not q_tokens and not ctx.empty and rel <= 0:
            continue
        ranked.append((s + REL_WEIGHT * rel, ts_seconds(o.ts) or 0.0, o, archived))
    ranked.sort(key=lambda x: (-x[0], -x[1], x[2].id))
    in_results = {r[2].id for r in ranked[: limit * 3]}
    items: List[RecallItem] = []
    lines: List[str] = []
    for score, _t, o, archived in ranked:
        if len(items) >= limit:
            break
        cvs = claims_by_obs.get(o.id, [])
        if cvs and all(not representative(cv, state.claims) for cv in cvs):
            reps = set()
            for cv in cvs:
                grp = [cv.claim.claim_id] + list(cv.equivalents)
                rep = cv.covered_by or min(grp)
                if rep in state.claims:
                    reps.add(state.claims[rep].claim.obs_id)
            if reps & in_results:
                continue                # an A3 equivalent: its representative is shown instead
        tags: List[str] = []
        extra_lines: List[str] = []
        for cv in cvs:
            st = cv.status
            if st == "unjudged" and cv.claim.claim_class != "conclusion":
                continue
            counter = _counter_line(cv, state.claims, obs_index, now, lang) if st in ("refuted", "disputed") else None
            if st == "refuted" and counter is None:
                continue
            tg = R.tag(st, lang)
            if tg not in tags:
                tags.append(tg)
            if counter and counter not in extra_lines:
                extra_lines.append(counter)
        if archived:
            tags.append(R.tag("archived", lang))
        related: List[str] = []
        for e in state.edges.values():
            if e.relation not in ("same_object", "same_event") or e.status not in ("provisional", "verified"):
                continue
            oa, ob = e.a.split("#", 1)[0], e.b.split("#", 1)[0]
            if o.id not in (oa, ob) or len(related) >= MAX_RELATED:
                continue
            other_node = e.b if oa == o.id else e.a
            other = ob if oa == o.id else oa
            related.append(other)
            label = {("same_object", "verified"): "same_object", ("same_object", "provisional"): "may_same_object",
                     ("same_event", "verified"): "same_event",
                     ("same_event", "provisional"): "likely_same_event"}[(e.relation, e.status)]
            extra_lines.append(f"    {R.t(lang, 'related')}: {R.t(lang, label)} "
                               f"{R.quote(node_text.get(other_node, other), 120)} "
                               f"({R.prov_text(obs_index.get(other), now, lang)})")
        entry = _prov_entry(o, obs_index)
        prov = R.prov_text(entry, now, lang)
        excerpt = _excerpt(o.text or "", q_tokens)
        items.append(RecallItem(obs_id=o.id, kind=o.kind, excerpt=excerpt, score=round(score, 6), provenance_text=prov,
                                tags=tags, claim_ids=[cv.claim.claim_id for cv in cvs], related=related,
                                archived=archived))
        head = f"{len(items)}. {' '.join(tags) + ' ' if tags else ''}{o.kind} · {prov} · {o.id}"
        lines.append(head)
        lines.append("   " + clip(excerpt, 400))
        lines.extend(extra_lines)
    pend = R.t(lang, "pending", k=pending) if pending else ""
    asof = R.t(lang, "asof", ago=R.ago(state.built_ts, now, lang)) if stats.get("stale") and state.built_ts else ""
    if items:
        qtxt = f'"{clip(q.query, 80)}" — ' if q.query else ""
        header = R.t(lang, "recall_header", q=qtxt, n=len(items), pending=pend, asof=asof)
    else:
        header = R.t(lang, "recall_empty", pending=pend, asof=asof)
    return RecallResult(query=q.query or "", items=items, text="\n".join([header] + lines), pending_judgments=pending)
