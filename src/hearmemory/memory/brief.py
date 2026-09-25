"""Memory brief.

Tiers:
  P1 refuted / disputed claims (incl. conclusions whose premise was refuted): relevance >= 0.4, or authored by
     the requesting actor (the author must learn its claim was refuted), or - empty context at session start -
     changed within brief.empty_context_days. <= p1_max. Every [REFUTED] line carries one counter-evidence.
  P2 open issues: relevance >= 0.2 or opened by the requesting actor (empty context: opened recently). <= p2_max.
  P3 new facts from OTHER actors: supported claims, then unjudged conclusions ([UNVERIFIED]); latest test run
     results; at most one A1/A2 alignment linking the context to someone else's record. Relevance >= 0.2
     (empty context: last 24 h), not shown in this session before, A3 restatements collapsed. <= p3_max.
     "outdated" claims are never P3 facts.
     a FINDING -- a claim an agent recorded on purpose (hearmemory_record / `hearmemory record`) that no run can
     settle (a review note, "tests lack negative cases"; unjudged, or insufficient when it is not a test-status
     claim), or an unjudged conclusion -- gets FINDING_SLOTS guaranteed P3 slot(s), ahead of supported facts.
     Claims derived from memory their author had been shown (memory echo) are never the representative.
     another agent's explicitly recorded summary claim that no independent evidence settled
     (insufficient / unjudged / same-source-only, e.g. "新增 div(a, b)...，pytest 通过，并已提交") is REPORTED:
     listed as "[UNVERIFIED] (or [SAME-SOURCE ONLY]) ... (the agent's own report)" after every other P3 fact
     (never outranking one). A claim that merely restates a test run shown in this brief ("pytest run: 3 passed
     ...") collapses into the run line.
Inside a tier: relevance desc -> recency desc -> item_key. Greedy fill of BriefRequest.max_tokens (header and
section titles included); what does not fit goes to dropped_for_budget (later shorter items may still fit).
Dedupe via state/sessions/<sid>.json (a status change is a new item_key); a P1 item may be repeated at most
twice more when the context hits it again with relevance >= 0.4. Empty brief -> text "" (inject nothing)."""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Tuple

from hearmemory.interfaces import (BRIEF_FACT_MIN_REL, OPEN_ISSUE_STATUSES, RELIED_MIN_REL, Brief, BriefItem,
                              BriefRequest, MemoryState, actor_key, actors_may_coincide)
from hearmemory.judge.claims import claimed_outcome, command_targets, restates_run_only

from . import render as R
from . import session as S
from .build import cfg_get
from .overlay import Overlay, fresh_overlay
from .relevance import Ctx, item_idents_of, item_relevance, make_ctx, norm_path
from .text import estimate_tokens, now_ts, ts_seconds

REMINDER_MAX = 2
FINDING_SLOTS = 1
EMPTY_CONTEXT_FACT_HOURS = 24
DAY = 86400.0


class _Cand:
    __slots__ = ("tier", "kind", "key", "lines", "refs", "prov", "rel", "ts", "reminder", "finding", "reported")

    def __init__(self, tier: str, kind: str, key: str, lines: List[str], refs: List[str], prov: str, rel: float,
                 ts: str, reminder: bool = False, finding: bool = False, reported: bool = False):
        self.tier, self.kind, self.key, self.lines, self.refs = tier, kind, key, lines, refs
        self.prov, self.rel, self.ts, self.reminder = prov, rel, ts or "", reminder
        self.finding, self.reported = finding, reported

    def sort_key(self) -> Tuple[float, float, str]:
        return (-self.rel, -(ts_seconds(self.ts) or 0.0), self.key)


def _age_s(ts: Optional[str], now: str) -> Optional[float]:
    a, b = ts_seconds(ts), ts_seconds(now)
    if a is None or b is None:
        return None
    return b - a


def _claim_paths(cv, entry: Optional[Mapping[str, Any]]) -> List[str]:
    paths = [norm_path(p) for p in cv.claim.paths or [] if norm_path(p)]
    for m in cv.claim.mentions or []:
        if m.kind in ("file", "module", "test"):
            paths += [norm_path(p) for p in m.resolved or [] if norm_path(p)]
    if entry:
        paths += [norm_path(p) for p in entry.get("paths") or [] if norm_path(p)]
    return list(dict.fromkeys(paths))


def _claim_idents(cv) -> frozenset:
    return item_idents_of(cv.claim.text, cv.claim.paths, [m.norm for m in cv.claim.mentions or []])


def _is_mine(me: Optional[str], author: Optional[str]) -> bool:
    return bool(me and author and actors_may_coincide(me, author))


def _surely_mine(me: Optional[str], author: Optional[str]) -> bool:
    """an unlinked proxy record ("claude:?" -- e.g. a Codex `hearmemory record` run under an inherited CLAUDECODE=1)
    MAY be anyone of that host; as news it is shown rather than hidden as the requesting session's own."""
    return _is_mine(me, author) and (me == author or (author or "").split(":")[1:2] != ["?"])


def representative(cv, claims: Mapping[str, Any]) -> bool:
    """A3: only one representative of an equivalence group is shown; covered claims are listed later/never as P3."""
    if cv.covered_by and cv.covered_by in claims:
        return False
    group = [cv.claim.claim_id] + [c for c in cv.equivalents if c in claims]
    # an echo of memory (derived_from) never represents the group its original belongs to
    originals = [c for c in group if not getattr(cv if c == cv.claim.claim_id else claims[c], "derived_from", None)]
    return cv.claim.claim_id == min(originals or group)


def is_finding(cv) -> bool:
    """a deliberately recorded claim no test run can settle (review note), or an unjudged conclusion."""
    if getattr(cv, "derived_from", None) or getattr(cv, "addressed", None):
        return False                    # a possibly addressed finding is no longer news
    c = cv.claim
    if cv.status == "unjudged":
        return bool(c.explicit) or c.claim_class == "conclusion"
    return cv.status in ("insufficient", "weak_support") and bool(c.explicit) and c.claim_class != "status"


def is_reported(cv) -> bool:
    """an explicitly recorded claim (an agent's own summary) that independent evidence did not settle."""
    return (not getattr(cv, "derived_from", None) and bool(cv.claim.explicit)
            and cv.status in ("insufficient", "unjudged", "same_source_only", "weak_support"))


def _restates_run(cv) -> bool:
    return cv.claim.claim_class == "status" and restates_run_only(cv.claim.text)


def restated_note(cv, claims: Mapping[str, Any], obs_index: Mapping[str, Any], lang: str) -> str:
    eq = [c for c in cv.equivalents if c in claims and not getattr(claims[c], "derived_from", None)]
    if not eq:
        return ""
    hosts = sorted({(obs_index.get(claims[c].claim.obs_id) or {}).get("host") or "?" for c in eq})
    return " " + R.t(lang, "restated", n=len(eq), src=", ".join(hosts))


def _counter_line(cv, claims: Mapping[str, Any], obs_index, now: str, lang: str) -> Optional[str]:
    ids = list(cv.counter_ids)
    if not ids and cv.premise_status in ("refuted", "disputed"):
        for pid in cv.premise_claim_ids:
            pv = claims.get(pid)
            if pv is not None and pv.status in ("refuted", "disputed"):
                ids += list(pv.counter_ids)
    if not ids:
        return None
    return f"    {R.t(lang, 'counter')}{R.t(lang, 'colon')}{R.evidence_text(ids[0], obs_index, now, lang)}"


def p1_candidates(state: MemoryState, ctx: Ctx, now: str, lang: str, empty_days: float, allow_recent: bool,
                  since_ts: Optional[str] = None) -> List[_Cand]:
    obs_index = (state.stats or {}).get("obs_index") or {}
    out = []
    for cid, cv in state.claims.items():
        if cv.tier == "archive":
            continue
        premise_bad = cv.claim.claim_class == "conclusion" and cv.premise_status in ("refuted", "disputed") \
            and cv.status not in ("refuted", "disputed")
        if cv.status not in ("refuted", "disputed") and not premise_bad:
            continue
        if since_ts and (cv.status_ts or "") <= since_ts:
            continue
        entry = obs_index.get(cv.claim.obs_id) or {}
        rel = item_relevance(_claim_paths(cv, entry), _claim_idents(cv), ctx)
        mine = _is_mine(ctx.actor, entry.get("actor"))
        age = _age_s(cv.status_ts or entry.get("ts"), now)
        recent = allow_recent and ctx.empty and age is not None and age <= empty_days * DAY
        if not (rel >= RELIED_MIN_REL or mine or recent):
            continue
        counter = _counter_line(cv, state.claims, obs_index, now, lang)
        if counter is None and (cv.status == "refuted" or premise_bad):
            continue                    # a refuted tag is never rendered without its counter-evidence
        status = cv.status if cv.status in ("refuted", "disputed") else "unjudged"
        head = f"- {R.tag(status, lang)} {R.quote(cv.claim.text)}"
        if premise_bad:
            head += f" ({R.t(lang, 'premise')})"
        prov = R.prov_text(entry, now, lang)
        lines = [f"{head} — {prov}"]
        if counter:
            lines.append(counter)
        key = f"claim:{cid}:{cv.status}" + (":premise" if premise_bad else "")
        out.append(_Cand("P1", "claim_status", key, lines, [cv.claim.obs_id] + list(cv.counter_ids[:1]), prov,
                         rel, cv.status_ts or entry.get("ts") or "", reminder=rel >= RELIED_MIN_REL))
    return out


def p2_candidates(state: MemoryState, ctx: Ctx, now: str, lang: str, empty_days: float, allow_recent: bool,
                  since_ts: Optional[str] = None) -> List[_Cand]:
    out = []
    for iid, iss in state.issues.items():
        if iss.status not in OPEN_ISSUE_STATUSES:
            continue
        last_ts = iss.history[-1].ts if iss.history else (iss.opened_ts or "")
        if since_ts and last_ts <= since_ts:
            continue
        idents = item_idents_of(iss.title, iss.paths, iss.mentions)
        rel = item_relevance(iss.paths, idents, ctx)
        opener = actor_key(iss.opened_by) if iss.opened_by is not None else None
        mine = _is_mine(ctx.actor, opener)
        age = _age_s(iss.opened_ts, now)
        recent = allow_recent and ctx.empty and age is not None and age <= empty_days * DAY
        if not (rel >= BRIEF_FACT_MIN_REL or mine or recent):
            continue
        tag = R.tag("issue", lang)
        line = f"- {tag[:-1]} {R.short_issue_id(iid)}] {R.quote(iss.title, 140)[1:-1]}"
        if iss.suggestion:
            line += f" — {R.t(lang, 'suggested')}: {iss.suggestion.replace('run ', '', 1) if iss.suggestion.startswith('run `') else iss.suggestion}"
        out.append(_Cand("P2", "issue", f"issue:{iid}:{iss.status}", [line], list(iss.source_obs_ids[:1]), "",
                         rel, last_ts))
    return out


def p3_candidates(state: MemoryState, overlay: Overlay, ctx: Ctx, now: str, lang: str) -> List[_Cand]:
    obs_index = (state.stats or {}).get("obs_index") or {}
    node_text = (state.stats or {}).get("node_text") or {}
    out: List[_Cand] = []

    def fresh(ts: Optional[str]) -> bool:
        age = _age_s(ts, now)
        return age is not None and age <= EMPTY_CONTEXT_FACT_HOURS * 3600

    for cid, cv in state.claims.items():
        if cv.tier == "archive" or not representative(cv, state.claims):
            continue
        finding = is_finding(cv)
        reported = False
        addressed = getattr(cv, "addressed", None) if cv.status not in ("refuted", "disputed", "outdated") else None
        if addressed:
            rank, reported = 3, True    # demoted after every other P3 fact, never a finding slot
        elif cv.status == "supported":
            rank = 0
        elif finding:
            rank = 1
        elif is_reported(cv):
            rank, reported = 2, True
        else:
            continue                    # outdated / refuted / insufficient / same-source-only are not new facts
        entry = obs_index.get(cv.claim.obs_id) or {}
        if _surely_mine(ctx.actor, entry.get("actor")):
            continue
        rel = item_relevance(_claim_paths(cv, entry), _claim_idents(cv), ctx)
        if not (rel >= BRIEF_FACT_MIN_REL or (ctx.empty and fresh(entry.get("ts")))):
            continue
        prov = R.prov_text(entry, now, lang)
        if addressed:
            line = f"- {R.tag('addressed', lang)} {R.quote(cv.claim.text)} {R.addressed_text(addressed, lang)} — {prov}"
        elif reported:
            tag = R.tag(cv.status if cv.status in ("same_source_only", "weak_support") else "unjudged", lang)
            line = f"- {tag} {R.quote(cv.claim.text)} {R.t(lang, 'reported')} — {prov}"
        else:
            line = f"- {R.tag(cv.status, lang)} {R.quote(cv.claim.text)}{restated_note(cv, state.claims, obs_index, lang)} — {prov}"
        c = _Cand("P3", "fact", f"fact:{cid}:{'addressed' if addressed else cv.status}", [line],
                  [cv.claim.obs_id] + ([addressed["edit_id"]] if addressed else []), prov, rel - 0.001 * rank,
                  entry.get("ts") or "", finding=finding, reported=reported)
        out.append(c)
    # (an agent's own report is not what vouches for its run: that run is still shown)
    cited = {r for c in out if not c.reported for cid in [c.key.split(":")[1]]
             for r in (state.claims[cid].support_ids if cid in state.claims else [])}
    for p1 in state.claims.values():
        if p1.status in ("refuted", "disputed"):
            cited.update(p1.counter_ids)
    # "pytest run: 3 passed, 0 failed ... Test suite is green." next to "test `pytest` passed (3 passed)"
    # from the same agent wasted a slot: a claim that only restates a latest run -- its author's own, or a newer
    # run of the target it names -- collapses into that run's line (shown even if another claim cites the run)
    latest = overlay.runs.all_latest()
    restating: List[Tuple[_Cand, List[str]]] = []
    for c in out:
        cv = state.claims.get(c.key.split(":")[1]) if c.key.startswith("fact:c-") else None
        if cv is None or not _restates_run(cv):
            continue
        author = (obs_index.get(cv.claim.obs_id) or {}).get("actor") or ""
        said = claimed_outcome(cv.claim.text)
        named = set(command_targets(cv.claim.text))
        rids = [r["obs_id"] for r in latest if (not named or r.get("target") in named) and (
            (named and (r.get("ts") or "") > c.ts)
            or (r.get("outcome") == said and actors_may_coincide(r.get("actor") or "?:?", author)))]
        if rids:
            restating.append((c, rids))
            cited.difference_update(rids)
    shown_runs: List[str] = []
    for run in latest:
        if _is_mine(ctx.actor, run.get("actor")) or run["obs_id"] in cited:
            continue                    # own runs are not news; runs already cited as evidence are not repeated
        idents = item_idents_of(run.get("target") or "", run.get("paths") or [])
        rel = item_relevance(run.get("paths") or [], idents, ctx)
        if not (rel >= BRIEF_FACT_MIN_REL or (ctx.empty and fresh(run.get("ts")))):
            continue
        prov = R.prov_text(run, now, lang)
        out.append(_Cand("P3", "fact", f"run:{run['target']}:{run['obs_id']}", [f"- {R.run_text(run, lang)} — {prov}"],
                         [run["obs_id"]], prov, rel, run.get("ts") or ""))
        shown_runs.append(run["obs_id"])
    for c, rids in restating:
        if set(rids) & set(shown_runs):
            out.remove(c)
    best_align: Optional[_Cand] = None
    for eid, e in state.edges.items():
        if e.relation not in ("same_object", "same_event") or e.status not in ("provisional", "verified"):
            continue
        oa, ob = e.a.split("#", 1)[0], e.b.split("#", 1)[0]
        ea, eb = obs_index.get(oa) or {}, obs_index.get(ob) or {}
        mine_a, mine_b = _is_mine(ctx.actor, ea.get("actor")), _is_mine(ctx.actor, eb.get("actor"))
        rel = max(item_relevance(ea.get("paths") or [], item_idents_of(node_text.get(e.a, "")), ctx),
                  item_relevance(eb.get("paths") or [], item_idents_of(node_text.get(e.b, "")), ctx))
        if mine_a and mine_b:
            continue
        if not ((mine_a or mine_b) or rel >= BRIEF_FACT_MIN_REL):
            continue
        key_label = {("same_object", "verified"): "same_object", ("same_object", "provisional"): "may_same_object",
                     ("same_event", "verified"): "same_event", ("same_event", "provisional"): "likely_same_event"}
        label = R.t(lang, key_label[(e.relation, e.status)])
        line = (f"- {label} {R.quote(node_text.get(e.a, oa), 90)} ({R.prov_text(ea, now, lang)}) ~ "
                f"{R.quote(node_text.get(e.b, ob), 90)} ({R.prov_text(eb, now, lang)})")
        cand = _Cand("P3", "alignment", f"align:{eid}:{e.status}", [line], [oa, ob], "", max(rel, 0.2),
                     max(ea.get("ts") or "", eb.get("ts") or ""))
        if best_align is None or cand.sort_key() < best_align.sort_key():
            best_align = cand
    if best_align is not None:
        out.append(best_align)
    return out


def _fits(lines: List[str], budget: int, used: int) -> Optional[int]:
    cost = estimate_tokens("\n".join(lines)) + len(lines)
    return cost if used + cost <= budget else None


def build_brief(state: MemoryState, store: Any, req: BriefRequest, cfg: Optional[Mapping[str, Any]] = None, *,
                now: Optional[str] = None, overlay: Any = None, record_shown: bool = True,
                shown_source: Optional[str] = None) -> Brief:
    """overlay: None -> computed here (bounded read); False -> skipped; an Overlay -> used as is.
    shown_source: "mcp" / "cli" when the request came through hearmemory's own MCP server / CLI (memory_shown event)."""
    now = now or now_ts()
    lang = R.lang_of(req.lang or cfg_get(cfg, "brief", "lang"))
    stats = state.stats or {}
    pending = int(stats.get("pending_judgments") or 0)
    stale = bool(stats.get("stale"))
    ctx_obj = req.context
    sid = ctx_obj.session_id if ctx_obj else None
    if overlay is None:
        overlay = fresh_overlay(state, store, session_id=sid)
    elif overlay is False:
        from .runs import RunIndex
        overlay = Overlay(runs=RunIndex(stats.get("runs") or {}))
    ctx = make_ctx(ctx_obj, extra_paths=overlay.session_paths)
    sess = S.load_session(store, ctx.session_key)
    shown: Dict[str, Dict[str, Any]] = sess.get("shown") or {}
    purpose = req.purpose or "session_start"
    empty_days = float(cfg_get(cfg, "brief", "empty_context_days"))
    allow_recent = purpose in ("session_start", "subagent_start", "manual")
    memory_as_of = state.built_ts or None

    def empty() -> Brief:
        return Brief(text="", pending_judgments=pending, memory_as_of=memory_as_of, stale=stale)

    if purpose == "push":
        last = _age_s(sess.get("last_push_ts"), now)
        if last is not None and last < float(cfg_get(cfg, "brief", "push_min_interval_s")):
            return empty()

    since = req.since_ts if purpose == "push" else None
    tiers: Dict[str, List[_Cand]] = {"P1": p1_candidates(state, ctx, now, lang, empty_days, allow_recent, since),
                                     "P2": p2_candidates(state, ctx, now, lang, empty_days, allow_recent, since),
                                     "P3": [] if purpose == "push" else p3_candidates(state, overlay, ctx, now, lang)}
    caps = {"P1": int(cfg_get(cfg, "brief", "p1_max")), "P2": int(cfg_get(cfg, "brief", "p2_max")),
            "P3": int(cfg_get(cfg, "brief", "p3_max"))}
    chosen_pool: Dict[str, List[_Cand]] = {}
    for tier, cands in tiers.items():
        keep = []
        n_align = 0
        ordered = sorted(cands, key=_Cand.sort_key)
        if tier == "P3":
            # the newest unseen finding(s) of other agents first, ahead of supported restatements
            fresh_findings = [c for c in ordered if c.finding and c.key not in shown][:FINDING_SLOTS]
            rest = [c for c in ordered if c not in fresh_findings]
            # an agent's unsettled own report never outranks another P3 fact
            ordered = fresh_findings + [c for c in rest if not c.reported] + [c for c in rest if c.reported]
        for c in ordered:
            prev = shown.get(c.key)
            if prev is not None:
                if not (tier == "P1" and c.reminder and int(prev.get("count") or 1) <= REMINDER_MAX):
                    continue
            if c.kind == "alignment":
                if n_align >= 1:
                    continue
                n_align += 1
            keep.append(c)
            if len(keep) >= caps[tier]:
                break
        chosen_pool[tier] = keep
    total = sum(len(v) for v in chosen_pool.values())
    if total == 0:
        return empty()

    asof = R.t(lang, "asof", ago=R.ago(state.built_ts, now, lang)) if stale and state.built_ts else ""
    pend = R.t(lang, "pending", k=pending) if pending else ""
    header_upper = R.t(lang, "brief_header", n=total, pending=pend, asof=asof)
    footer = R.t(lang, "brief_footer")
    budget = int(req.max_tokens or 0)
    used = estimate_tokens(header_upper) + estimate_tokens(footer) + 2
    if used >= budget:
        return empty()
    picked: Dict[str, List[Tuple[_Cand, int]]] = {"P1": [], "P2": [], "P3": []}
    dropped: List[str] = []
    for tier in ("P1", "P2", "P3"):
        for c in chosen_pool[tier]:
            lines = ([R.t(lang, tier.lower())] if not picked[tier] else []) + c.lines
            cost = _fits(lines, budget, used)
            if cost is None:
                dropped.append(c.key)
                continue
            used += cost
            picked[tier].append((c, cost))
    n = sum(len(v) for v in picked.values())
    if n == 0:
        b = empty()
        b.dropped_for_budget = dropped
        return b
    header = R.t(lang, "brief_header", n=n, pending=pend, asof=asof)
    out_lines = [header]
    items: List[BriefItem] = []
    for tier in ("P1", "P2", "P3"):
        if not picked[tier]:
            continue
        out_lines.append(R.t(lang, tier.lower()))
        for c, cost in picked[tier]:
            out_lines.extend(c.lines)
            items.append(BriefItem(tier=tier, kind=c.kind, item_key=c.key, text="\n".join(c.lines), refs=c.refs,
                                   provenance_text=c.prov, relevance=round(c.rel, 6), tokens=cost))
    out_lines.append(footer)
    text = "\n".join(out_lines)
    if record_shown and ctx.session_key:
        for it in items:
            prev = shown.get(it.item_key) or {}
            shown[it.item_key] = {"ts": now, "count": int(prev.get("count") or 0) + 1}
        sess["shown"] = shown
        if purpose == "push":
            sess["last_push_ts"] = now
        S.save_session(store, ctx.session_key, sess)
        shown_claims = [it.item_key.split(":")[1] for it in items
                        if it.item_key.split(":")[0] in ("fact", "claim") and it.item_key.split(":")[1].startswith("c-")]
        S.record_shown_event(store, ctx_obj, shown_claims, "brief:" + purpose, shown_source, now)
    return Brief(text=text, items=items, dropped_for_budget=dropped, token_estimate=estimate_tokens(text),
                 pending_judgments=pending, memory_as_of=memory_as_of, stale=stale)
