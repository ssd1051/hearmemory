"""Pre-commit / claim check (C1 / C2 / C4 as program rules).

Warnings, ranked by interfaces.WARNING_RANK, total text <= precommit.max_tokens:
  relies_on_refuted     refuted claim (or conclusion whose premise was refuted) relevant to the payload
                        (relevance >= precommit.min_rel; for action=claim also a shared mention / identifier and
                        trigram Jaccard >= 0.35). Always shown with its counter-evidence. "outdated" never counts.
  relies_on_disputed    the same for disputed claims.
  unresolved_issue      open issue relevant to the payload, or opened by this actor (C1 / C4 rules).
  failing_check         a test target related to the staged paths whose latest run failed with no later pass;
                        the fresh overlay is consulted, so a run that failed seconds ago counts (C2 suggestion).
  unseen_relevant_fact  another actor's supported / unjudged conclusion claim, relevant, unseen here (<= 2).
Modes: off -> allow; warn -> warn; hold_once -> hold the first attempt per attempt_key (Claude: session +
normalised command, git: staged tree hash; hold_window_s), then warn; block -> block only on
BLOCKING_WARNING_KINDS that were not acknowledged (`hearmemory check --ack <item_key>` writes a `seen` event)."""
from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Mapping, Optional, Tuple

from hearmemory.interfaces import (BLOCKING_WARNING_KINDS, OPEN_ISSUE_STATUSES, PRECOMMIT_MODES, WARNING_RANK,
                              AgentContext, CheckRequest, CheckResult, CheckWarning, MemoryState, actor_key)

from . import render as R
from . import session as S
from .brief import _claim_idents, _claim_paths, _counter_line, _is_mine, representative
from .build import cfg_get
from .overlay import Overlay, fresh_overlay
from .relevance import Ctx, item_idents_of, item_relevance, make_ctx, paths_in_text
from .runs import RunIndex, targets_for_paths
from .text import char_trigrams, estimate_tokens, jaccard, now_ts, ts_seconds

CLAIM_TRIGRAM_MIN = 0.35
MAX_UNSEEN_FACTS = 2
PAYLOAD_MAX_CHARS = 20000
GIT_HOLDS_STATE = "git_holds"


def _payload_ctx(req: CheckRequest) -> Ctx:
    base = req.context
    bare = AgentContext(host=base.host, session_id=base.session_id, subagent_id=base.subagent_id) if base else None
    payload = (req.payload_text or "")[:PAYLOAD_MAX_CHARS]
    return make_ctx(bare, extra_paths=list(req.paths or []), extra_text=payload)


def default_attempt_key(req: CheckRequest) -> str:
    sid = req.context.session_id if req.context else ""
    h = hashlib.sha256(" ".join((req.payload_text or "").split()).encode("utf-8")).hexdigest()[:16]
    return f"{sid}:{req.action}:{h}"


def _holds(store: Any, req: CheckRequest, ctx: Ctx) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    """(state name, container dict, holds dict)."""
    if (req.context and req.context.host == "git") or not ctx.session_key:
        d = S.load(store, GIT_HOLDS_STATE)
        return GIT_HOLDS_STATE, d, d
    sess = S.load_session(store, ctx.session_key)
    return S.SESSION_PREFIX + ctx.session_key, sess, sess.setdefault("holds", {})


def check(state: MemoryState, store: Any, req: CheckRequest, cfg: Optional[Mapping[str, Any]] = None, *,
          now: Optional[str] = None, overlay: Any = None, record: bool = True) -> CheckResult:
    now = now or now_ts()
    mode = req.mode if req.mode in PRECOMMIT_MODES else "warn"
    stats = state.stats or {}
    stale = bool(stats.get("stale"))
    as_of = state.built_ts or None
    if mode == "off":
        return CheckResult(decision="allow", mode=mode, memory_as_of=as_of, stale=stale)
    lang = R.lang_of(cfg_get(cfg, "brief", "lang"))
    min_rel = float(cfg_get(cfg, "precommit", "min_rel"))
    max_tokens = int(cfg_get(cfg, "precommit", "max_tokens"))
    obs_index = stats.get("obs_index") or {}
    sid = req.context.session_id if req.context else None
    if overlay is None:
        overlay = fresh_overlay(state, store, session_id=sid)
    elif overlay is False:
        overlay = Overlay(runs=RunIndex(stats.get("runs") or {}))
    ctx = _payload_ctx(req)
    payload = (req.payload_text or "")[:PAYLOAD_MAX_CHARS]
    payload_tris = char_trigrams(payload) if req.action == "claim" else frozenset()
    acked = set(stats.get("acked") or [])
    sess = S.load_session(store, ctx.session_key) if ctx.session_key else {}
    shown = sess.get("shown") or {}
    warnings: List[CheckWarning] = []

    # 1-2: relies on refuted / disputed claims
    for cid, cv in state.claims.items():
        if cv.tier == "archive":
            continue
        premise_bad = cv.claim.claim_class == "conclusion" and cv.premise_status == "refuted" \
            and cv.status not in ("refuted", "disputed")
        if cv.status == "refuted" or premise_bad:
            kind = "relies_on_refuted"
        elif cv.status == "disputed" or (cv.claim.claim_class == "conclusion" and cv.premise_status == "disputed"):
            kind = "relies_on_disputed"
        else:
            continue
        entry = obs_index.get(cv.claim.obs_id) or {}
        idents = _claim_idents(cv)
        rel = item_relevance(_claim_paths(cv, entry), idents, ctx)
        if rel < min_rel:
            continue
        if req.action == "claim":
            shares = bool(idents & ctx.idents) or bool(set(_claim_paths(cv, entry)) & ctx.paths)
            if not shares or jaccard(char_trigrams(cv.claim.text), payload_tris) < CLAIM_TRIGRAM_MIN:
                continue
        counter = _counter_line(cv, state.claims, obs_index, now, lang)
        if counter is None and kind == "relies_on_refuted":
            continue                    # never a [REFUTED] line without counter-evidence
        status = cv.status if cv.status in ("refuted", "disputed") else ("refuted" if premise_bad else "disputed")
        head = f"- {R.tag(status, lang)} {R.quote(cv.claim.text)}"
        if premise_bad or (cv.status not in ("refuted", "disputed")):
            head += f" ({R.t(lang, 'premise')})"
        text = f"{head} — {R.prov_text(entry, now, lang)}" + (f"\n{counter}" if counter else "")
        warnings.append(CheckWarning(kind=kind, item_key=f"claim:{cid}:{cv.status}", text=text,
                                     refs=[cv.claim.obs_id] + list(cv.counter_ids[:1]), relevance=rel))

    # 3: unresolved issues (C1 / C4)
    for iid, iss in state.issues.items():
        if iss.status not in OPEN_ISSUE_STATUSES:
            continue
        rel = item_relevance(iss.paths, item_idents_of(iss.title, iss.paths, iss.mentions), ctx)
        opener = actor_key(iss.opened_by) if iss.opened_by is not None else None
        if not (rel >= min_rel or _is_mine(ctx.actor, opener)):
            continue
        tag = R.tag("issue", lang)
        text = f"- {tag[:-1]} {R.short_issue_id(iid)}] {R.quote(iss.title, 140)[1:-1]}"
        if iss.suggestion:
            text += f" — {R.t(lang, 'suggested')}: {iss.suggestion}"
        warnings.append(CheckWarning(kind="unresolved_issue", item_key=f"issue:{iid}:{iss.status}", text=text,
                                     refs=list(iss.source_obs_ids[:1]), relevance=rel))

    # 4: failing checks related to the staged / named paths (overlay-fresh)
    rel_paths = sorted(ctx.paths | set(paths_in_text(payload, limit=40)))
    for run in targets_for_paths(overlay.runs, rel_paths):
        if run.get("outcome") != "fail":
            continue
        summ = run.get("summary") or ""
        text = R.t(lang, "failing", target=run["target"][:100], summary=f" ({summ})" if summ else "")
        text = f"- {text} — {R.prov_text(run, now, lang)}"
        warnings.append(CheckWarning(kind="failing_check", item_key=f"failing:{run['target']}:{run['obs_id']}",
                                     text=text, refs=[run["obs_id"]], relevance=1.0))

    # 5: unseen relevant facts from other actors
    n_facts = 0
    facts = []
    for cid, cv in state.claims.items():
        if cv.tier == "archive" or not representative(cv, state.claims):
            continue
        if not (cv.status == "supported" or (cv.status == "unjudged" and cv.claim.claim_class == "conclusion")):
            continue
        entry = obs_index.get(cv.claim.obs_id) or {}
        if _is_mine(ctx.actor, entry.get("actor")):
            continue
        key = f"fact:{cid}:{cv.status}"
        if key in shown:
            continue
        rel = item_relevance(_claim_paths(cv, entry), _claim_idents(cv), ctx)
        if rel < min_rel:
            continue
        facts.append((rel, entry.get("ts") or "", key, cv, entry))
    for rel, _ts, key, cv, entry in sorted(facts, key=lambda x: (-x[0], x[1], x[2])):
        if n_facts >= MAX_UNSEEN_FACTS:
            break
        text = f"- {R.t(lang, 'new')} {R.tag(cv.status, lang)} {R.quote(cv.claim.text)} — {R.prov_text(entry, now, lang)}"
        warnings.append(CheckWarning(kind="unseen_relevant_fact", item_key=key, text=text, refs=[cv.claim.obs_id],
                                     relevance=rel))
        n_facts += 1

    warnings.sort(key=lambda w: (WARNING_RANK[w.kind], -w.relevance, w.item_key))
    header_key = {"git_commit": "check_header_git_commit", "claim": "check_header_claim",
                  "finish": "check_header_finish"}.get(req.action, "check_header_git_commit")
    asof = (" " + R.t(lang, "asof", ago=R.ago(state.built_ts, now, lang)).strip()) if stale and state.built_ts else ""
    budget_used = estimate_tokens(R.t(lang, header_key, n=len(warnings)) + asof) + 40
    kept: List[CheckWarning] = []
    for w in warnings:
        cost = estimate_tokens(w.text) + 1
        if budget_used + cost > max_tokens and kept:
            continue
        budget_used += cost
        kept.append(w)
    warnings = kept
    if not warnings:
        return CheckResult(decision="allow", mode=mode, memory_as_of=as_of, stale=stale)

    decision = "warn"
    footer = R.t(lang, "check_warn")
    if mode == "hold_once":
        name, container, holds = _holds(store, req, ctx)
        key = req.attempt_key or default_attempt_key(req)
        prev = holds.get(key)
        age = None
        if prev:
            a, b = ts_seconds(prev), ts_seconds(now)
            age = (b - a) if a is not None and b is not None else None
        window = float(cfg_get(cfg, "precommit", "hold_window_s"))
        if prev is None or age is None or age > window:
            decision = "hold"
            footer = R.t(lang, "check_hold")
            if record:
                holds[key] = now
                if len(holds) > 200:
                    for k in sorted(holds, key=lambda k: holds[k])[:-200]:
                        holds.pop(k, None)
                S.save(store, name, container)
    elif mode == "block":
        blocking = [w for w in warnings if w.kind in BLOCKING_WARNING_KINDS and w.item_key not in acked]
        if blocking:
            decision = "block"
            footer = R.t(lang, "check_block", keys=", ".join(w.item_key for w in blocking[:4]))
    lines = [R.t(lang, header_key, n=len(warnings)) + asof] + [w.text for w in warnings] + [footer]
    if record and ctx.session_key:
        for w in warnings:
            if w.kind == "unseen_relevant_fact":
                prev = shown.get(w.item_key) or {}
                shown[w.item_key] = {"ts": now, "count": int(prev.get("count") or 0) + 1}
        if any(w.kind == "unseen_relevant_fact" for w in warnings):
            sess = S.load_session(store, ctx.session_key)      # re-read: a hold may have been written above
            sess["shown"] = {**(sess.get("shown") or {}), **shown}
            S.save_session(store, ctx.session_key, sess)
    return CheckResult(decision=decision, warnings=warnings, text="\n".join(lines), mode=mode, memory_as_of=as_of,
                       stale=stale)
