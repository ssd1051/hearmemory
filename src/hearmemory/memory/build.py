"""MemoryBuilder and load_or_rebuild.

The memory state is a PURE function of (observations, latest claim generation, candidates, judgments,
control events, config): raw inputs are read with first-occurrence-wins dedupe, the ActorMap resolves
every observation's effective actor, decisions (valid judgments + judgment_override events) and control events
are applied in deterministic (ts, id) order, then source groups, effective claim statuses, issues, archive
tiers and the compact render index are derived. Nothing here calls a model or the network.

load_or_rebuild(store, cfg, allow_rebuild=True, deadline_s=None):
  * memory.json fingerprint == current -> read it;
  * allow_rebuild: take the "pipeline" lock NON-blocking, rebuild within deadline_s, write atomically
    (store.write_state); lock busy or timeout -> the older memory.json marked stale;
  * allow_rebuild=False (every hook): return the older memory.json (stale). Only when memory.json does not
    exist and the project is small (<= hooks.hook_rebuild_max_obs observations by file size) it rebuilds once;
    otherwise it returns an empty stale state (the hook spawns the worker).
  Pass pipeline_locked=True when the caller (worker run_pipeline) already holds the pipeline lock.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from types import SimpleNamespace

from hearmemory.interfaces import (ActorMap, Candidate, Claim, ClaimView, ControlEvent, DEFAULT_CONFIG, Edge,
                              HistoryEntry, Judgment, LAYOUT, Mark, MemoryState, Observation,
                              PRIMARY_OBS_KINDS, Provenance, actor_key, actors_may_coincide, canonical_json,
                              issue_id_for, span_node, stable_id)

from . import archive as archive_mod
from .issues import IssueBook
from .ops import (GUARD_FOR, Decision, b1_claim_id, claim_pair, consume_a1, consume_a2, consume_a3,
                  consume_b1, decision_from_judgment, obs_of_node, pair_nodes, scope_ok)
from .relevance import norm_path, paths_in_text
from .runs import RunIndex, link_dirty_runs, run_record
from .source_groups import compute_source_groups
from .text import char_trigrams, clip, now_ts, ts_seconds
from hearmemory.textutil import GIT_COMMIT_CMD_RE, apply_patch_paths, edit_summary, new_commit_sha

TERMINAL_OUTCOMES = ("valid", "validation_error", "disabled")
# best-supported first: a later model judgment may raise a claim along this scale but never lower it
SUPPORT_RANK = {"supported": 2, "weak_support": 1, "insufficient": 0}
STATUS_WORST_ORDER = ("refuted", "disputed", "insufficient", "outdated", "unjudged", "same_source_only", "weak_support",
                      "supported")
OBS_BYTES_ESTIMATE = 700          # average observations.jsonl line length used to estimate counts by file size
EXCERPT_CHARS = 240
NODE_TEXT_CHARS = 160
# A claim restates a memory claim shown to its author when this share of the shorter
# text's character trigrams also occurs in the other (paraphrases of one fact measured 0.52-0.82 on the real
# data, different facts <= 0.41)
RESTATE_SHOWN_MIN = 0.5
SHOWN_SCAN = 20             # newest memory_shown events per actor looked at for one claim


def restate_overlap(a: str, b: str) -> float:
    ta, tb = char_trigrams(a or ""), char_trigrams(b or "")
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / float(min(len(ta), len(tb)))


def _shown_actor(amap: ActorMap, prov: Provenance) -> str:
    """Actor a memory_shown event was delivered to: a proxy session (MCP / CLI) resolves like a proxy record."""
    return amap.actor_of(SimpleNamespace(id="", provenance=prov))


def mark_derived(views: Mapping[str, ClaimView], obs_by_id: Mapping[str, Observation], actor_of: Mapping[str, str],
                 ev_list: Sequence[ControlEvent], amap: ActorMap, sg: Any, now: str) -> int:
    """a claim restating a memory claim hearmemory had delivered (brief / recall) to its author's session
    before the claim was made is DERIVED from it (ClaimView.derived_from): same source group as the original, and
    never supported on its own (it adds no independent support). Returns how many claims were marked."""
    shown: Dict[str, List[Tuple[str, List[str]]]] = defaultdict(list)       # actor -> [(ts, claim ids)]
    for ev in ev_list:
        if ev.kind == "memory_shown" and ev.provenance is not None:
            ids = [str(c) for c in (ev.data or {}).get("claim_ids") or [] if c]
            if ids:
                shown[_shown_actor(amap, ev.provenance)].append((ev.ts or "", ids))
    if not shown:
        return 0
    n = 0
    for cid, cv in views.items():
        o = obs_by_id.get(cv.claim.obs_id)
        if o is None:
            continue
        author = actor_of.get(o.id) or "?:?"
        best: Optional[Tuple[float, str]] = None
        for actor, rows in shown.items():
            if not actors_may_coincide(actor, author):
                continue
            before = [ids for ts, ids in rows if ts <= (o.ts or "")][-SHOWN_SCAN:]
            for sid in {s for ids in before for s in ids}:
                sv = views.get(sid)
                if sv is None or sid == cid or sv.claim.obs_id == o.id or sv.derived_from == cid:
                    continue
                score = restate_overlap(cv.claim.text, sv.claim.text)
                if score >= RESTATE_SHOWN_MIN and (best is None or (score, sid) > best):
                    best = (score, sid)
        if best is None:
            continue
        orig = views[best[1]]
        cv.derived_from = best[1]
        sg.add(o.id)
        sg.add(orig.claim.obs_id)
        sg.union(orig.claim.obs_id, o.id, "restates_shown_memory")
        cv.history.append(HistoryEntry(ts=cv.status_ts or now, change=f"derived_from {best[1]}",
                                       reason="restates memory shown to its author's session", decision_ref=None))
        if cv.status in ("supported", "weak_support"):
            cv.history.append(HistoryEntry(ts=cv.status_ts or now, change=f"status {cv.status}->same_source_only",
                                           reason="restatement of memory it had been shown: not independent",
                                           decision_ref=None))
            cv.status = "same_source_only"
        n += 1
    return n


ADDRESS_WINDOW_S = 2 * 3600.0     # an edit and the passing run / commit that follows it


def _finding_paths(c: Claim) -> Set[str]:
    paths = {norm_path(p) for p in c.paths or [] if norm_path(p)}
    for m in c.mentions or []:
        if m.kind in ("file", "module", "test"):
            paths |= {norm_path(p.split("::", 1)[0]) for p in m.resolved or [] if norm_path(p.split("::", 1)[0])}
    return paths


def mark_addressed(views: Mapping[str, ClaimView], obs_list: Sequence[Observation],
                   obs_by_id: Mapping[str, Observation], actor_of: Mapping[str, str],
                   run_by_obs: Mapping[str, Mapping[str, Any]], amap: ActorMap) -> int:
    """A FINDING -- an explicitly recorded non-status claim about files ("tests/
    test_calc.py mul coverage insufficient ...") -- is POSSIBLY ADDRESSED (ClaimView.addressed) when, after it, an
    agent edited one of its files and that agent's passing test run or successful commit followed (within
    ADDRESS_WINDOW_S; the commit is the last one, amends included). Nothing is deleted: the brief shows it demoted
    as "[ADDRESSED?] ... → <who> edited <paths>, <run>, <commit>". Returns how many were marked."""
    edits = [o for o in obs_list if o.kind == "file_edit" and not o.excluded
             and (o.tool is None or o.tool.status != "error")]
    if not edits:
        return 0
    follow: Dict[str, List[Tuple[Observation, str, str]]] = defaultdict(list)   # actor -> (obs, "run"/"commit", x)
    for o in obs_list:
        rec = run_by_obs.get(o.id)
        if rec is not None and rec.get("outcome") == "pass":
            follow[actor_of.get(o.id) or "?:?"].append((o, "run", str(rec.get("summary") or "passed")))
        elif o.kind == "command" and o.tool is not None and GIT_COMMIT_CMD_RE.search(o.tool.command or "") \
                and (o.tool.exit_code == 0 or o.tool.status == "ok") and new_commit_sha(o.text or ""):
            follow[actor_of.get(o.id) or "?:?"].append((o, "commit", new_commit_sha(o.text or "")[:12]))
    n = 0
    for cv in views.values():
        c = cv.claim
        co = obs_by_id.get(c.obs_id)
        if co is None or not c.explicit or c.claim_class == "status" or cv.derived_from \
                or cv.status in ("refuted", "disputed", "outdated"):
            continue
        want = _finding_paths(c)
        if not want:
            continue
        for e in edits:
            hit = want & {norm_path(p) for p in e.paths or [] if norm_path(p)}
            if (e.ts or "") <= (co.ts or "") or not hit:
                continue
            actor = actor_of.get(e.id) or "?:?"
            t0 = ts_seconds(e.ts) or 0.0
            after = [(x, k, v) for a, rows in follow.items() if actors_may_coincide(a, actor) for x, k, v in rows
                     if (e.ts or "") < (x.ts or "") and (ts_seconds(x.ts) or 0.0) - t0 <= ADDRESS_WINDOW_S]
            after.sort(key=lambda r: (r[0].ts or "", r[0].id))
            runs = [r for r in after if r[1] == "run"]
            commits = [r for r in after if r[1] == "commit"]
            if not runs and not commits:
                continue
            p = amap.provenance_of(e)
            paths = sorted(hit | {norm_path(q) for x in edits if (x.ts or "") > (co.ts or "")
                                  and actors_may_coincide(actor_of.get(x.id) or "?:?", actor)
                                  for q in x.paths or [] if norm_path(q) in want})
            cv.addressed = {"ts": e.ts, "actor": actor, "host": p.host, "session": p.session_id,
                            "subagent_type": p.subagent_type if p.subagent_id else None, "paths": paths,
                            "edit_id": e.id, "run": runs[0][2] if runs else None,
                            "run_obs_id": runs[0][0].id if runs else None,
                            "commit": commits[-1][2] if commits else None}
            cv.history.append(HistoryEntry(ts=(runs or commits)[0][0].ts or e.ts, change="possibly addressed",
                                           reason=f"{actor} edited {', '.join(paths)}", decision_ref=None))
            n += 1
            break
    return n


def cfg_get(cfg: Optional[Mapping[str, Any]], section: str, key: str) -> Any:
    try:
        sec = (cfg or {}).get(section) or {}
        if key in sec:
            return sec[key]
    except AttributeError:
        pass
    return DEFAULT_CONFIG[section][key]


def config_hash(cfg: Optional[Mapping[str, Any]]) -> str:
    return hashlib.sha256(canonical_json(cfg or {}).encode("utf-8")).hexdigest()[:16]


class _Deadline:
    def __init__(self, deadline_s: Optional[float]):
        self.end = None if deadline_s is None else time.monotonic() + float(deadline_s)

    def __call__(self) -> None:
        if self.end is not None and time.monotonic() > self.end:
            raise TimeoutError("memory rebuild deadline exceeded")


def _dedupe(items: Iterable[Any], key: Callable[[Any], str], stats: Dict[str, int], name: str) -> List[Any]:
    seen: Set[str] = set()
    out = []
    for it in items:
        if it is None:
            continue
        k = key(it)
        if k in seen:
            stats[name] = stats.get(name, 0) + 1
            continue
        seen.add(k)
        out.append(it)
    return out


def _worst(statuses: Iterable[str]) -> Optional[str]:
    sts = [s for s in statuses if s]
    if not sts:
        return None
    return min(sts, key=lambda s: STATUS_WORST_ORDER.index(s) if s in STATUS_WORST_ORDER else len(STATUS_WORST_ORDER))


def independent_supports(cv: ClaimView, obs_by_id: Mapping[str, Observation], actor_of: Mapping[str, str],
                         sg: Any) -> List[str]:
    """Effective status: a supporting observation counts only if it is PRIMARY, outside the claim's own
    source group, and not an output the claim's author (actors_may_coincide) had already produced before making
    the claim. The last condition is source-group rule 3 with the anchor test replaced by the fact that B1 picked
    it as evidence (B1 evidence always shares a mention / path with the claim), so an agent's own command output
    plus its own restatement is never independent - whether written via hook, MCP or CLI, linked or not."""
    claim_obs = obs_by_id.get(cv.claim.obs_id)
    cg = sg.group(cv.claim.obs_id)
    author = actor_of.get(cv.claim.obs_id)
    out = []
    for s in cv.support_ids:
        o = obs_by_id.get(s)
        if o is None or o.kind not in PRIMARY_OBS_KINDS or sg.group(s) == cg:
            continue
        own = bool(author) and actors_may_coincide(author, actor_of.get(s) or "?:?")
        if own and claim_obs is not None and (o.ts, o.id) <= (claim_obs.ts, claim_obs.id):
            continue
        if own and o.kind == "command" and o.tool is not None and GIT_COMMIT_CMD_RE.search(o.tool.command or ""):
            continue            # the author's own commit right after its claim is its act, not a check
        out.append(s)
    return out


def _prov_entry(p: Provenance) -> Dict[str, Any]:
    return {"host": p.host, "session": p.session_id, "subagent": p.subagent_id, "subagent_type": p.subagent_type,
            "label": p.agent_label, "commit": p.git_commit, "branch": p.git_branch, "source": p.source}


def build_state(observations: Iterable[Observation], claims: Iterable[Claim], candidates: Iterable[Candidate],
                judgments: Iterable[Judgment], events: Iterable[ControlEvent], cfg: Optional[Mapping[str, Any]] = None,
                *, now: Optional[str] = None, fingerprint: str = "", obs_offset: int = 0,
                deadline_s: Optional[float] = None) -> MemoryState:
    """Pure rebuild from raw records (the core of MemoryBuilder.build)."""
    check = _Deadline(deadline_s)
    now = now or now_ts()
    dup: Dict[str, int] = {}
    gates: Dict[str, int] = defaultdict(int)
    dropped: Dict[str, int] = defaultdict(int)

    obs_list = _dedupe(observations, lambda o: o.id, dup, "observations")
    obs_list.sort(key=lambda o: (o.ts or "", o.id))
    obs_by_id: Dict[str, Observation] = {o.id: o for o in obs_list}
    check()

    ev_list = _dedupe(events, lambda e: e.id, dup, "events")
    ev_list.sort(key=lambda e: (e.ts or "", e.id))
    amap = ActorMap()
    for ev in ev_list:
        amap.add_event(ev)
    actor_of: Dict[str, str] = {o.id: amap.actor_of(o) for o in obs_list}
    check()

    # claims: first occurrence wins; only the latest extraction generation (meta.generation)
    claim_list = _dedupe(claims, lambda c: c.claim_id, dup, "claims")
    gen_of = {c.claim_id: int((getattr(c, "meta", None) or {}).get("generation", 0) or 0) for c in claim_list}
    if claim_list:
        top = max(gen_of.values())
        claim_list = [c for c in claim_list if gen_of[c.claim_id] == top]
    claim_by_id: Dict[str, Claim] = {c.claim_id: c for c in claim_list}
    node_to_claim: Dict[str, str] = {span_node(c.obs_id, c.span): c.claim_id for c in claim_list}

    # runs (program facts)
    runs = RunIndex()
    run_by_obs: Dict[str, Dict[str, Any]] = {}
    for i, o in enumerate(obs_list):
        if i % 512 == 511:
            check()
        rec = run_record(o, actor_of[o.id])
        if rec is not None:
            run_by_obs[o.id] = rec
    link_dirty_runs(obs_list, actor_of, run_by_obs)       # dirty run -> the commit right after
    for rec in run_by_obs.values():
        runs.add(rec)
    runs.finalize(check)    # coverage once, after all records (deadline-checked)
    check()

    # source groups rules (1)-(3); claims contribute anchors of their observation
    extra: Dict[str, Tuple[List[str], List[str]]] = {}
    for c in claim_list:
        ps, ids = extra.setdefault(c.obs_id, ([], []))
        ps.extend(c.paths or [])
        for m in c.mentions or []:
            if m.kind in ("file", "module", "test"):
                ps.extend(m.resolved or [])
            ids.append(m.surface)
    sg = compute_source_groups(obs_list, actor_of, extra, deadline_check=check)
    check()

    # decisions
    cand_list = _dedupe(candidates, lambda c: c.candidate_id, dup, "candidates")
    cand_by_id: Dict[str, Candidate] = {c.candidate_id: c for c in cand_list}
    j_list = _dedupe(judgments, lambda j: j.judgment_id, dup, "judgments")
    j_list.sort(key=lambda j: (j.ts or "", j.judgment_id))
    decisions: List[Decision] = []
    min_conf = float(cfg_get(cfg, "judge", "b1_min_support_confidence"))
    terminal: Set[str] = set()
    decided: Set[str] = set()
    for j in j_list:
        if j.outcome in TERMINAL_OUTCOMES:
            terminal.add(j.candidate_id)
        cand = cand_by_id.get(j.candidate_id)
        if cand is None:
            dropped["judgment_without_candidate"] += 1
            continue
        d = decision_from_judgment(cand, j, min_conf)
        if d is not None:
            if d.gate:
                gates[d.gate] += 1
            decisions.append(d)
            decided.add(cand.candidate_id)
    overrides: List[Decision] = []
    for ev in ev_list:
        if ev.kind != "judgment_override":
            continue
        cand = cand_by_id.get(ev.target)
        label = (ev.data or {}).get("label")
        if cand is None or not label:
            dropped["override_without_candidate"] += 1
            continue
        fake = Judgment(judgment_id=ev.id, candidate_id=cand.candidate_id, template_id=cand.template_id,
                        template_version=cand.template_version, input_hash=cand.input_hash, provider="manual",
                        outcome="valid", ts=ev.ts, label=label, rule_id=(ev.data or {}).get("rule_id"))
        d = decision_from_judgment(cand, fake)
        if d is None:
            dropped["override_bad_label"] += 1
            continue
        d.provider = "manual"
        overrides.append(d)
        decided.add(cand.candidate_id)
        terminal.add(cand.candidate_id)

    def subject(c: Candidate) -> Tuple[str, str, str]:
        return (c.template_id, c.subject_key, c.direction or "")

    overridden = {subject(d.candidate) for d in overrides}
    superseded_by = {c.supersedes: c.candidate_id for c in cand_list if c.supersedes}
    first_decision: Dict[str, Tuple[str, str]] = {}
    for d in decisions + overrides:
        k = d.candidate.candidate_id
        if k not in first_decision or (d.ts, d.ref) < first_decision[k]:
            first_decision[k] = (d.ts, d.ref)
    kept: List[Decision] = []
    for d in decisions:
        if subject(d.candidate) in overridden:
            dropped["overridden"] += 1
            continue
        newer = superseded_by.get(d.candidate.candidate_id)
        if newer and newer in first_decision and (d.ts, d.ref) > first_decision[newer]:
            dropped["superseded_decision"] += 1     # an older question answered after the newer one: ignored
            continue
        kept.append(d)
    kept.extend(overrides)

    # A3: pair the two directions (latest decision per direction), applied at the later ts
    a3: Dict[str, Dict[str, Decision]] = defaultdict(dict)
    items: List[Tuple[str, str, str, Any]] = []     # (ts, ref, kind, payload)
    for d in kept:
        if d.candidate.template_id == "A3":
            cur = a3[d.candidate.subject_key].get(d.candidate.direction or "")
            if cur is None or (d.ts, d.ref) >= (cur.ts, cur.ref):
                a3[d.candidate.subject_key][d.candidate.direction or ""] = d
        else:
            items.append((d.ts, d.ref, "decision", d))
    for sk, dirs in a3.items():
        ab, ba = dirs.get("a_contains_b"), dirs.get("b_contains_a")
        if ab is None or ba is None:
            dropped["a3_single_direction"] += 1
            continue
        last = max((ab.ts, ab.ref), (ba.ts, ba.ref))
        items.append((last[0], last[1], "a3", (ab, ba)))
    for ev in ev_list:
        if ev.kind in ("issue_open", "issue_close", "issue_reopen"):
            items.append((ev.ts, ev.id, "event", ev))
    items.sort(key=lambda x: (x[0] or "", x[1]))

    # ---- apply ----------------------------------------------------------------------------------------
    views: Dict[str, ClaimView] = {cid: ClaimView(claim=c) for cid, c in claim_by_id.items()}
    edges: Dict[str, Edge] = {}
    marks: Dict[str, Mark] = {}
    book = IssueBook()
    merges: List[Tuple[str, str, str]] = []
    applied = 0
    issues_from_unresolved = bool(cfg_get(cfg, "issues", "from_unresolved_alignment"))
    issues_from_insufficient = bool(cfg_get(cfg, "issues", "from_insufficient_conclusion"))

    def eff_prov(oid: Optional[str]) -> Optional[Provenance]:
        o = obs_by_id.get(oid or "")
        return amap.provenance_of(o) if o is not None else None

    def issue_fields(c: Claim, ev_ids: Sequence[str]) -> Dict[str, Any]:
        paths = {norm_path(p) for p in c.paths or [] if norm_path(p)}
        norms = []
        for m in c.mentions or []:
            norms.append(m.norm)
            if m.kind in ("file", "module"):
                paths |= {norm_path(p) for p in m.resolved or [] if norm_path(p)}
        target = None
        for e in ev_ids:
            r = run_by_obs.get(e)
            if r is not None:
                target = r["target"]
                break
        suggestion = None
        if target:
            suggestion = f"run `{target}`"
        else:
            tests = [m.surface for m in c.mentions or [] if m.kind == "test"]
            if tests:
                suggestion = f"run the test `{tests[0]}`"
        return {"paths": sorted(paths), "mentions": norms, "suggestion": suggestion, "target": target}

    def add_history(cv: ClaimView, ts: str, change: str, reason: str, ref: Optional[str]) -> None:
        cv.history.append(HistoryEntry(ts=ts, change=change, reason=reason, decision_ref=ref))

    def apply_op(op, ts: str) -> None:
        nonlocal applied
        t = op.target
        if op.kind == "edge_add":
            a, b = t["a"], t["b"]
            directed = bool(t.get("directed"))
            ends = (a, b) if directed else tuple(sorted((a, b)))
            eid = stable_id("e-", t["relation"], list(ends))
            e = edges.get(eid)
            want = "verified" if t.get("status") == "verified" else "provisional"
            if e is None:
                e = Edge(edge_id=eid, relation=t["relation"], a=ends[0], b=ends[1], status=want,
                         basis_obs_ids=list(op.basis_obs_ids), decision_refs=[op.decision_ref], directed=directed,
                         history=[HistoryEntry(ts=ts, change=f"status None->{want}", reason=op.reason,
                                               decision_ref=op.decision_ref)])
                edges[eid] = e
            else:
                e.decision_refs.append(op.decision_ref)
                e.basis_obs_ids = sorted(set(e.basis_obs_ids) | set(op.basis_obs_ids))
                if want == "verified" and e.status != "verified":
                    e.history.append(HistoryEntry(ts=ts, change=f"status {e.status}->verified", reason=op.reason,
                                                  decision_ref=op.decision_ref))
                    e.status = "verified"
            _refresh_pair(e, ts, op.decision_ref)
        elif op.kind == "mark_add":
            a, b = t["a"], t.get("b")
            directed = t["mark"] == "covered_by"
            ends = (a, b) if directed or b is None else tuple(sorted((a, b)))
            mid = stable_id("m-", t["mark"], list(ends))
            if mid not in marks:
                marks[mid] = Mark(mark_id=mid, kind=t["mark"], a=ends[0], b=ends[1], decision_ref=op.decision_ref)
            for e in edges.values():
                if {e.a, e.b} == {a, b}:
                    _refresh_pair(e, ts, op.decision_ref)
        elif op.kind == "group_merge":
            merges.append((t["a"], t["b"], t.get("reason") or "merge"))
        elif op.kind == "claim_status_set":
            cv = views.get(t["claim_id"])
            if cv is None:
                return
            prev = cv.status
            if t.get("provider") in ("jev", "cache") and \
                    SUPPORT_RANK.get(t["status"], -1) >= 0 and SUPPORT_RANK.get(prev or "", -1) > SUPPORT_RANK[t["status"]]:
                # a later model judgment that is merely weaker (weak support / insufficient -- e.g. its
                # evidence budget lost the edit that implements the claim) never silently downgrades an earlier
                # support; only a refutation, a dispute or the outdated rule does
                cv.decision_refs.append(op.decision_ref)
                add_history(cv, ts, f"status {prev} kept", op.reason + f" -> {t['status']} ignored: a later, weaker "
                            "model judgment does not downgrade an earlier support", op.decision_ref)
                gates["b1_downgrade_kept"] += 1
                applied += 1
                return
            cv.status = t["status"]
            cv.judged_label = t.get("label")
            cv.support_ids = list(t.get("support_ids") or [])
            if t.get("counter_ids") or t["status"] in ("refuted", "disputed", "outdated"):
                cv.counter_ids = list(t.get("counter_ids") or [])
            cv.decision_refs.append(op.decision_ref)
            cv.status_ts = ts
            add_history(cv, ts, f"status {prev}->{cv.status}", op.reason + (f" ({t['rule_id']})" if t.get("rule_id") else ""),
                        op.decision_ref)
            book.on_claim_status(cv.claim.claim_id, cv.status, prev, ts, op.decision_ref)
            parent_id = cv.claim.parent_claim_id
            if parent_id and parent_id in views:
                pv = views[parent_id]
                if cv.claim.claim_id not in pv.premise_claim_ids:
                    pv.premise_claim_ids.append(cv.claim.claim_id)
                old = pv.premise_status
                pv.premise_status = _worst(views[p].status for p in pv.premise_claim_ids if p in views)
                if pv.premise_status != old:
                    add_history(pv, ts, f"premise {old}->{pv.premise_status}", "premise judged", op.decision_ref)
                if pv.claim.claim_class == "conclusion" and pv.premise_status in ("refuted", "insufficient"):
                    f = issue_fields(pv.claim, cv.counter_ids or cv.support_ids)
                    book.open(issue_id_for("premise_gap", "claim:" + parent_id), "premise_gap",
                              "Premise unsupported: " + cv.claim.text, ts, op.decision_ref, claim_id=parent_id,
                              paths=f["paths"], mentions=f["mentions"], source_obs_ids=[cv.claim.obs_id],
                              suggestion=f["suggestion"], target=f["target"], opened_by=eff_prov(pv.claim.obs_id))
        elif op.kind == "issue_open":
            cid = t.get("claim_id")
            cv = views.get(cid) if cid else None
            f = issue_fields(cv.claim, op.basis_obs_ids) if cv else {"paths": [], "mentions": [], "suggestion": None,
                                                                       "target": None}
            src = [cv.claim.obs_id] if cv else list(op.basis_obs_ids)
            book.open(t["issue_id"], t["kind"], t["title"], ts, op.decision_ref, claim_id=cid, paths=f["paths"],
                      mentions=f["mentions"], source_obs_ids=src, suggestion=f["suggestion"], target=f["target"],
                      opened_by=eff_prov(src[0]) if src else None, status=t.get("status") or "open")
        applied += 1

    def _refresh_pair(e: Edge, ts: str, ref: str) -> None:
        guard = GUARD_FOR.get(e.relation)
        if guard is None or e.status in ("verified", "retracted"):
            return
        guarded = any(m.kind == guard and {m.a, m.b} == {e.a, e.b} for m in marks.values())
        want = "disputed" if guarded else "provisional"
        if e.status != want:
            e.history.append(HistoryEntry(ts=ts, change=f"status {e.status}->{want}",
                                          reason="merge_guard" if guarded else "guard_cleared", decision_ref=ref))
            e.status = want

    def actor_gate(*nodes: Optional[str]) -> bool:
        obs_ids = [obs_of_node(n) for n in nodes]
        if any(o is None for o in obs_ids):
            return False
        a = actor_of.get(obs_ids[0]) or "?:?"
        b = actor_of.get(obs_ids[1]) or "?:?"
        if actors_may_coincide(a, b):
            gates["same_actor_after_link"] += 1
            return False
        return True

    for i, (ts, ref, kind, payload) in enumerate(items):
        if i % 200 == 0:
            check()
        if kind == "decision":
            d: Decision = payload
            tid = d.candidate.template_id
            if tid in ("A1", "A2"):
                a, b = pair_nodes(d.candidate)
                if not a or not b:
                    dropped["pair_without_nodes"] += 1
                    continue
                if not actor_gate(a, b):
                    continue
                ops = consume_a1(d, issues_from_unresolved) if tid == "A1" else consume_a2(d)
            elif tid == "B1":
                cid = b1_claim_id(d.candidate)
                c = claim_by_id.get(cid or "")
                if c is None:
                    dropped["b1_unknown_claim"] += 1
                    continue
                ops = consume_b1(d, c, issues_from_insufficient)
            else:
                continue
            for op in ops:
                apply_op(op, d.ts)
        elif kind == "a3":
            ab, ba = payload
            ca, cb = claim_pair(ab.candidate)
            a_node = span_node(claim_by_id[ca].obs_id, claim_by_id[ca].span) if ca in claim_by_id else None
            b_node = span_node(claim_by_id[cb].obs_id, claim_by_id[cb].span) if cb in claim_by_id else None
            if not a_node or not b_node:
                a_node, b_node = pair_nodes(ab.candidate)
            if not a_node or not b_node:
                dropped["pair_without_nodes"] += 1
                continue
            if not actor_gate(a_node, b_node):
                continue
            oa, ob = obs_of_node(a_node), obs_of_node(b_node)
            guarded = any(m.kind in ("distinct_object", "distinct_event") and
                          {obs_of_node(m.a), obs_of_node(m.b)} == {oa, ob} for m in marks.values())
            if guarded:
                gates["a3_merge_guard"] += 1
            if not scope_ok([ab.candidate, ba.candidate]):
                gates["a3_scope_mismatch"] += 1
            for op in consume_a3(ab, ba, a_node, b_node, scope_ok([ab.candidate, ba.candidate]), guarded):
                apply_op(op, ts)
        elif kind == "event":
            ev: ControlEvent = payload
            data = ev.data or {}
            if ev.kind == "issue_open":
                oid = data.get("obs_id")
                o = obs_by_id.get(oid or "")
                title = data.get("title") or ((o.text or "").strip().splitlines() or [""])[0] if o else data.get("title")
                title = (title or ev.target)[:300]
                paths = list(data.get("paths") or [])
                if o is not None:
                    paths += list(o.paths) + paths_in_text(o.text or "", limit=10)
                prov = eff_prov(oid) or ev.provenance
                book.open(ev.target, data.get("kind") or "manual", title, ev.ts, ev.id, claim_id=data.get("claim_id"),
                          paths=[norm_path(p) for p in paths if norm_path(p)], mentions=data.get("mentions") or [],
                          source_obs_ids=[oid] if oid else [], suggestion=data.get("suggestion"),
                          target=data.get("target"), opened_by=prov)
            elif ev.kind == "issue_close":
                book.close(ev.target, ev.ts, str(data.get("reason") or ""), ev.id)
            elif ev.kind == "issue_reopen":
                book.reopen(ev.target, ev.ts, str(data.get("reason") or ""), ev.id)
    check()

    # rule 4 merges, then effective claim statuses (supported needs an independent primary support)
    for a, b, reason in merges:
        if a and b:
            sg.add(a)
            sg.add(b)
            sg.union(a, b, reason)
    derived = mark_derived(views, obs_by_id, actor_of, ev_list, amap, sg, now)
    if derived:
        gates["derived_from_shown_memory"] += derived
    for cid, cv in views.items():
        if cv.status not in ("supported", "weak_support"):
            continue
        independent = independent_supports(cv, obs_by_id, actor_of, sg)
        if not independent:
            add_history(cv, cv.status_ts or now, f"status {cv.status}->same_source_only",
                        "no supporting primary observation outside the claim's own source group", None)
            cv.status = "same_source_only"
    addressed = mark_addressed(views, obs_list, obs_by_id, actor_of, run_by_obs, amap)
    if addressed:
        gates["possibly_addressed"] += addressed
    # a claim recorded on a dirty tree, then committed by its author, is labelled "<head>+dirty → <sha>"
    dirty_claims = {cv.claim.obs_id: {"obs_id": cv.claim.obs_id, "ts": obs_by_id[cv.claim.obs_id].ts,
                                      "actor": actor_of[cv.claim.obs_id], "dirty": True}
                    for cv in views.values() if cv.claim.obs_id in obs_by_id
                    and obs_by_id[cv.claim.obs_id].provenance.git_dirty is True}
    link_dirty_runs(obs_list, actor_of, dirty_claims)

    # A3 equivalents / covered_by on claims
    for e in edges.values():
        if e.relation == "restates" and e.status in ("provisional", "verified"):
            ca, cb = node_to_claim.get(e.a), node_to_claim.get(e.b)
            if ca and cb and ca in views and cb in views:
                if cb not in views[ca].equivalents:
                    views[ca].equivalents.append(cb)
                if ca not in views[cb].equivalents:
                    views[cb].equivalents.append(ca)
    for m in marks.values():
        if m.kind == "covered_by":
            ca, cb = node_to_claim.get(m.a), node_to_claim.get(m.b or "")
            if ca in views and cb:
                views[ca].covered_by = cb

    # failing_check issues (program rule): a target reported failing by >= 2 actors
    for tgt, cur in runs.targets.items():
        lf = cur.get("last_fail")
        if lf and runs.distinct_fail_actors(tgt) >= 2:
            book.open(issue_id_for("failing_check", "target:" + tgt), "failing_check", f"Failing check: `{tgt}`",
                      lf["ts"], lf["obs_id"], paths=lf.get("paths") or [], source_obs_ids=[lf["obs_id"]],
                      suggestion=f"run `{tgt}`", target=tgt, opened_by=eff_prov(lf["obs_id"]))
    book.resolve_by_passing_checks(runs.passed_after)

    # archive
    restored = [ev.target for ev in ev_list if ev.kind == "archive_restore"]
    latest_run_obs = [c["last"]["obs_id"] for c in runs.targets.values() if c.get("last")]
    obs_meta = {o.id: {"ts": o.ts, "paths": o.paths} for o in obs_list}
    tiers = archive_mod.sweep(obs_meta, views, edges, marks, book.issues, latest_run_obs, restored, now,
                              (cfg or {}).get("archive") or DEFAULT_CONFIG["archive"])
    for cid, cv in views.items():
        if tiers.get(cid) == "archive":
            cv.tier = "archive"
    check()

    # compact render index: everything the brief / check / recall need without reading raw files
    ref_ids: Set[str] = set()
    for cv in views.values():
        ref_ids.add(cv.claim.obs_id)
        ref_ids.update(cv.support_ids)
        ref_ids.update(cv.counter_ids)
    for iss in book.issues.values():
        ref_ids.update(iss.source_obs_ids)
    nodes: Set[str] = set()
    for e in edges.values():
        nodes |= {e.a, e.b}
    for m in marks.values():
        nodes |= {m.a} | ({m.b} if m.b else set())
    ref_ids |= {obs_of_node(n) for n in nodes}
    for cur in runs.targets.values():
        for k in ("last", "last_fail", "last_pass", "covered_by"):
            if cur.get(k):
                ref_ids.add(cur[k]["obs_id"])
    obs_index: Dict[str, Dict[str, Any]] = {}
    for oid in sorted(ref_ids):
        o = obs_by_id.get(oid)
        if o is None:
            continue
        p = amap.provenance_of(o)
        ent = {"ts": o.ts, "kind": o.kind, "actor": actor_of[oid], **_prov_entry(p),
               "paths": list(o.paths)[:10], "excerpt": clip(o.text, EXCERPT_CHARS)}
        if o.tool is not None:
            ent["command"] = (o.tool.command or "")[:300] or None
            ent["status"] = o.tool.status
            ent["exit_code"] = o.tool.exit_code
        legacy_patch = apply_patch_paths((o.tool.command or "") if o.tool is not None else "")
        if o.kind == "file_edit" or legacy_patch:
            # an edit is described as before -> after, never as a clipped command / patch head
            ent["edit_summary"] = edit_summary(o.text or "", (list(o.paths) or legacy_patch or [""])[0])
            ent.pop("command", None)
        if oid in run_by_obs:
            ent["run"] = {k: run_by_obs[oid][k] for k in ("target", "outcome", "summary")}
            ent.update({k: run_by_obs[oid][k] for k in ("dirty", "commit_to") if k in run_by_obs[oid]})
        elif dirty_claims.get(oid, {}).get("commit_to"):
            ent.update({"dirty": True, "commit_to": dirty_claims[oid]["commit_to"]})
        obs_index[oid] = ent
    node_text: Dict[str, str] = {}
    for n in sorted(nodes):
        o = obs_by_id.get(obs_of_node(n) or "")
        if o is None:
            continue
        span = n.split("#", 1)[1] if "#" in n else None
        if span and "-" in span:
            s, e = span.split("-", 1)
            try:
                s_i, e_i = int(s), int(e)
                lo = max(0, s_i - 60)
                node_text[n] = clip(o.text[lo:e_i + 60], NODE_TEXT_CHARS)
                continue
            except ValueError:
                pass
        node_text[n] = clip(o.text, NODE_TEXT_CHARS)

    pending = 0
    for c in cand_list:
        if c.candidate_id in terminal or c.candidate_id in decided:
            continue
        if c.candidate_id in superseded_by:
            continue
        pending += 1
    acked = sorted({ev.target for ev in ev_list if ev.kind == "seen"})

    stats: Dict[str, Any] = {
        "counts": {"observations": len(obs_list), "claims": len(views), "candidates": len(cand_list),
                   "judgments": len(j_list), "events": len(ev_list), "decisions_applied": applied,
                   "edges": len(edges), "marks": len(marks), "issues": len(book.issues),
                   "open_issues": len(book.open_issues()), "archived": len(tiers)},
        "duplicates_skipped": dup, "gates": dict(gates), "dropped": dict(dropped),
        "pending_judgments": pending, "runs": runs.to_dict(), "obs_index": obs_index, "node_text": node_text,
        "acked": acked, "restored": sorted(set(restored)), "issue_targets": dict(sorted(book.targets.items())),
        "linked_obs": sum(1 for o in obs_list if amap.linked(o.id)),
    }
    return MemoryState(fingerprint=fingerprint, built_ts=now, observation_count=len(obs_list), obs_offset=int(obs_offset),
                       claims=dict(sorted(views.items())), edges=dict(sorted(edges.items())),
                       marks=dict(sorted(marks.items())), source_groups=sg.mapping(),
                       issues=dict(sorted(book.issues.items())), tiers=tiers, stats=stats)


# ---------------------------------------------------------------------------------------------------------
# store-facing API
# ---------------------------------------------------------------------------------------------------------
def _obs_file_size(store: Any) -> Optional[int]:
    try:
        return os.path.getsize(Path(store.hearmemory_dir) / LAYOUT["observations"])
    except Exception:
        size = getattr(store, "obs_size", None)
        try:
            return int(size() if callable(size) else size) if size is not None else None
        except Exception:
            return None


def current_fingerprint(store: Any, cfg: Optional[Mapping[str, Any]]) -> Optional[str]:
    try:
        return f"{store.fingerprint()}|cfg:{config_hash(cfg)}"
    except Exception:
        return None


class MemoryBuilder:
    """MemoryBuilderAPI: build(store, config) -> MemoryState (pure recompute from raw)."""

    def build(self, store: Any, config: Optional[Mapping[str, Any]] = None, deadline_s: Optional[float] = None,
              now: Optional[str] = None) -> MemoryState:
        check = _Deadline(deadline_s)
        fp = current_fingerprint(store, config) or ""
        size = _obs_file_size(store)
        observations = []
        max_off = 0
        for i, (off, o) in enumerate(store.iter_observations(0)):
            observations.append(o)
            max_off = max(max_off, int(off or 0))
            if i % 1000 == 0:
                check()
        claims = list(store.iter_claims())
        candidates = list(store.iter_candidates())
        judgments = list(store.iter_judgments())
        events = list(store.iter_events())
        check()
        remaining = None if check.end is None else max(0.0, check.end - time.monotonic())
        return build_state(observations, claims, candidates, judgments, events, config, now=now, fingerprint=fp,
                           obs_offset=size if size is not None else max_off, deadline_s=remaining)


def empty_state(stale: bool = True, reason: str = "") -> MemoryState:
    st = MemoryState(stats={"stale": stale, "empty": True, "stale_reason": reason, "pending_judgments": 0})
    return st


def _read_saved(store: Any) -> Optional[MemoryState]:
    try:
        d = store.read_state("memory")
        if not d:
            return None
        return MemoryState.from_dict(d)
    except Exception:
        return None


def _mark_stale(state: MemoryState, reason: str) -> MemoryState:
    state.stats = dict(state.stats or {})
    state.stats["stale"] = True
    state.stats["stale_reason"] = reason
    return state


def _initialised(store: Any) -> bool:
    try:
        return bool(store.is_initialised())
    except Exception:
        return False


def _try_lock(store: Any) -> Optional[int]:
    try:
        import fcntl
        lock_dir = Path(store.hearmemory_dir) / LAYOUT["locks"]
        if not _initialised(store) or not lock_dir.is_dir():
            return None
        fd = os.open(str(lock_dir / "pipeline.lock"), os.O_RDWR | os.O_CREAT, 0o644)
    except Exception:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except OSError:
        os.close(fd)
        return None


@contextlib.contextmanager
def pipeline_lock(store: Any, already_held: bool = False):
    """Non-blocking flock on .hearmemory/locks/pipeline.lock (the same file core / judge lock). Yields True when
    acquired (or already held by the caller). Never creates .hearmemory or its locks directory."""
    if already_held:
        yield True
        return
    fd = _try_lock(store)
    try:
        yield fd is not None
    finally:
        if fd is not None:
            try:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
            except Exception:
                pass
            os.close(fd)


def _write(store: Any, state: MemoryState) -> bool:
    if not _initialised(store):
        return False
    try:
        store.write_state("memory", state.to_dict())
        return True
    except Exception:
        return False


def load_or_rebuild(store: Any, cfg: Optional[Mapping[str, Any]] = None, allow_rebuild: bool = True,
                    deadline_s: Optional[float] = None, *, pipeline_locked: bool = False,
                    now: Optional[str] = None) -> MemoryState:
    """Never raises for bad / missing state; the returned state carries
    stats["stale"] = True when it may be older than the raw files."""
    fp = current_fingerprint(store, cfg)
    saved = _read_saved(store)
    if saved is not None and fp is not None and saved.fingerprint == fp:
        saved.stats = dict(saved.stats or {})
        saved.stats["stale"] = False
        return saved
    if not allow_rebuild:
        if saved is not None:
            return _mark_stale(saved, "hook_no_rebuild")
        size = _obs_file_size(store)
        max_obs = int(cfg_get(cfg, "hooks", "hook_rebuild_max_obs"))
        if size is None or -(-size // OBS_BYTES_ESTIMATE) > max_obs:
            return empty_state(True, "no_memory_json")
        # tiny project without memory.json: one bounded rebuild inside the hook's memory slice
    with pipeline_lock(store, pipeline_locked) as got:
        if not got and saved is not None and _initialised(store):
            return _mark_stale(saved, "pipeline_busy")      # the worker is rebuilding: serve the older state
        try:
            state = MemoryBuilder().build(store, cfg, deadline_s=deadline_s, now=now)
        except TimeoutError:
            return _mark_stale(saved, "rebuild_timeout") if saved is not None else empty_state(True, "rebuild_timeout")
        except Exception:
            return _mark_stale(saved, "rebuild_error") if saved is not None else empty_state(True, "rebuild_error")
        if got:
            _write(store, state)
            state.stats["stale"] = False
            return state
        # someone else (the worker) holds the pipeline lock: serve the older memory.json when there is one
        if saved is not None:
            return _mark_stale(saved, "pipeline_busy")
        state.stats["stale"] = False
        state.stats["unsaved"] = True
        return state
