"""Generic extractor v1: turns new observations into claims and judgment candidates.

Principle: fewer, better questions ("宁缺毋滥"). Per run at most 20 Jev questions (A1<=4, A2<=4, A3<=6,
B1<=10); everything a program can decide becomes a provider="rule" candidate (priority 0) that RuleJudge
answers without Jev; every discarded candidate is counted by reason in ExtractResult.dropped.

Incremental and bounded: the extractor only reads NEW observations; history lives in a compact
ExtractIndex (state/extract_index.json, window extract.history_window_days / history_max_obs). Original
text is fetched back only for the few observations a candidate state needs.

Model-visible state never contains ids or relative times: times are interfaces.jev_time() and
sources are described in words, so the same question always has the same input_hash.
"""
from __future__ import annotations

import collections
import re
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from hearmemory import interfaces as I
from hearmemory.judge import _compat as C
from hearmemory.judge._text import bm25_scores, ident_tokens, jaccard, normalize_text, trigram_jaccard
from hearmemory.judge.claims import claimed_outcome, command_targets, extract_claims, normalize_command
from hearmemory.testcmd import is_test_command, stored_target, test_outcome
from hearmemory.textutil import GIT_COMMIT_CMD_RE, apply_patch_paths, compact_diff
from hearmemory.judge.mentions import Hit, exception_signature, run_ids, scan, signatures_match
from hearmemory.judge.project_index import ProjectIndex
from hearmemory.judge.scope import scope_facts
from hearmemory.judge.templates import A1_OBJECT_GRANULARITY

XINDEX_SCHEMA = "hearmemory.extract_index/1"
RULE_CAP_PER_RUN = 60
PARTNERS_PER_NORM = 20
B1_EVIDENCE_POOL = 40
DEFERRED_MAX = 500
A1_WINDOW = 300
A2_WINDOW = 600
TEST_CMD_RE = re.compile(r"(?:^|[\s;&|/])(?:pytest|py\.test|unittest|jest|vitest|mocha|tox|nox|go test|cargo test|"
                         r"npm (?:run )?test|yarn test|pnpm test|make test|rspec|phpunit)\b")
_REL_RE = re.compile(I.RELATIVE_TIME_RE)
PRIORITY = {"B1:conclusion": 10, "B1:status": 20, "A2": 30, "B1:premise": 35, "A1": 40, "B1:other": 45, "A3": 50}
PROV_KEYS = ("host", "session_id", "subagent_id", "subagent_type", "agent_label", "source")
# a claim about a test outcome is judged against the latest test run of the SAME actor before it
# (e.g. Codex ran `python -m pytest -q` -> 1 passed, then recorded "... tests pass (1 passed)").
SESSION_RUN_WINDOW_S = 2 * 3600.0
COMMIT_AFTER_CLAIM_S = 900.0      # a commit this soon after a change claim, by its author, is its commit
Fetch = Callable[[str, Mapping[str, Any]], Optional[I.Observation]]


class _Stub:
    """Just enough of an Observation for interfaces.ActorMap (id + provenance)."""
    __slots__ = ("id", "provenance")

    def __init__(self, oid: str, prov: I.Provenance) -> None:
        self.id = oid
        self.provenance = prov


def _prov_of(s: Mapping[str, Any]) -> I.Provenance:
    p = s.get("prov") or {}
    return I.Provenance(host=p.get("host") or I.ACTOR_WILDCARD, **{k: p.get(k) for k in PROV_KEYS if k != "host"})


def _clean(text: str) -> str:
    """Model-visible text: redacted again and free of relative times."""
    out, _ = C.redact(text or "")
    return _REL_RE.sub("[time]", out)


def _clean_state(v: Any) -> Any:
    if isinstance(v, str):
        return _REL_RE.sub("[time]", v)
    if isinstance(v, list):
        return [_clean_state(x) for x in v]
    if isinstance(v, dict):
        return {k: _clean_state(x) for k, x in v.items()}
    return v


def _short(commit: Optional[str]) -> str:
    return commit[:7] if commit else "unknown"


# =========================================================================================================
class ExtractIndex:
    """Compact, bounded extractor history. Persisted as state/extract_index.json."""

    def __init__(self, data: Optional[Mapping[str, Any]] = None) -> None:
        d = dict(data or {})
        if d.get("schema") not in (None, XINDEX_SCHEMA):
            d = {}
        self.obs: Dict[str, Dict[str, Any]] = dict(d.get("obs") or {})
        self.claims: Dict[str, Dict[str, Any]] = dict(d.get("claims") or {})
        self.asked: Dict[str, Dict[str, Any]] = dict(d.get("asked") or {})
        self.a1_pairs: Dict[str, float] = dict(d.get("a1_pairs") or {})
        self.rejudge: Dict[str, Dict[str, int]] = {k: dict(v) for k, v in (d.get("rejudge") or {}).items()}
        self.deferred: List[str] = list(d.get("deferred") or [])
        self.stats: Dict[str, Any] = dict(d.get("stats") or {})
        self.meta: Dict[str, Any] = dict(d.get("meta") or {})
        self.derive()

    def derive(self) -> None:
        order = sorted(self.obs.values(), key=lambda s: (s.get("ep", 0.0), s["id"]))
        self.order: List[str] = [s["id"] for s in order]
        self.norm_obs: Dict[str, List[str]] = collections.defaultdict(list)
        self.alias_rev: Dict[str, List[str]] = collections.defaultdict(list)
        self.path_obs: Dict[str, List[str]] = collections.defaultdict(list)
        self.key_runs: Dict[str, List[str]] = collections.defaultdict(list)
        self.edits: List[Dict[str, Any]] = []
        for s in order:
            for h in s.get("hits") or ():
                if s["id"] not in self.norm_obs[h["n"]][-1:]:
                    self.norm_obs[h["n"]].append(s["id"])
                if h.get("al"):
                    for r in h.get("r") or ():
                        self.alias_rev[r].append(s["id"])
            for p in s.get("paths") or ():
                self.path_obs[p].append(s["id"])
            if s.get("kind") == "command" and s.get("outcome"):
                for k in s.get("keys") or ():
                    self.key_runs[k].append(s["id"])
            if s.get("kind") == "file_edit":
                self.edits.append(s)
        corder = sorted(self.claims.values(), key=lambda c: (c.get("ep", 0.0), c["cid"]))
        self.claim_norms: Dict[str, List[str]] = collections.defaultdict(list)
        self.claim_tkeys: Dict[str, List[str]] = collections.defaultdict(list)
        self.claim_texts: Dict[Tuple[str, str], str] = {}
        self.fail_reports: Dict[str, List[str]] = collections.defaultdict(list)
        for c in corder:
            for n in c.get("norms") or ():
                self.claim_norms[n].append(c["cid"])
            for k in c.get("tkeys") or ():
                self.claim_tkeys[k].append(c["cid"])
            self.claim_texts.setdefault((c.get("actor") or "", normalize_text(c.get("text", ""))), c["cid"])
            if c.get("outcome") == "fail":
                for k in c.get("tkeys") or ():
                    self.fail_reports[k].append(c["cid"])

    def add_obs(self, s: Dict[str, Any]) -> None:
        self.obs[s["id"]] = s
        self.order.append(s["id"])
        for h in s.get("hits") or ():
            if s["id"] not in self.norm_obs[h["n"]][-1:]:
                self.norm_obs[h["n"]].append(s["id"])
            if h.get("al"):
                for r in h.get("r") or ():
                    self.alias_rev[r].append(s["id"])
        for p in s.get("paths") or ():
            self.path_obs[p].append(s["id"])
        if s.get("kind") == "command" and s.get("outcome"):
            for k in s.get("keys") or ():
                self.key_runs[k].append(s["id"])
        if s.get("kind") == "file_edit":
            self.edits.append(s)

    def add_claim(self, c: Dict[str, Any]) -> None:
        self.claims[c["cid"]] = c
        for n in c.get("norms") or ():
            self.claim_norms[n].append(c["cid"])
        for k in c.get("tkeys") or ():
            self.claim_tkeys[k].append(c["cid"])
        self.claim_texts.setdefault((c.get("actor") or "", normalize_text(c.get("text", ""))), c["cid"])
        if c.get("outcome") == "fail":
            for k in c.get("tkeys") or ():
                self.fail_reports[k].append(c["cid"])

    def evict(self, now: float, window_days: float, max_obs: int) -> int:
        cutoff = now - float(window_days) * 86400.0
        keep = [s for s in sorted(self.obs.values(), key=lambda s: (s.get("ep", 0.0), s["id"])) if s.get("ep", 0.0) >= cutoff]
        if len(keep) > max_obs:
            keep = keep[-int(max_obs):]
        n_before = len(self.obs)
        self.obs = {s["id"]: s for s in keep}
        self.claims = {k: c for k, c in self.claims.items() if c.get("obs_id") in self.obs}
        self.asked = {k: v for k, v in self.asked.items() if v.get("t", 0.0) >= cutoff}
        self.a1_pairs = {k: t for k, t in self.a1_pairs.items() if t >= now - 86400.0}
        today = C.utc_day(now)
        self.rejudge = {k: {d: n for d, n in v.items() if d == today} for k, v in self.rejudge.items()
                        if today in v and k in self.claims}
        self.deferred = [d for d in self.deferred if d in self.obs][-DEFERRED_MAX:]
        self.derive()
        return n_before - len(self.obs)

    def to_dict(self) -> Dict[str, Any]:
        return {"schema": XINDEX_SCHEMA, "obs": self.obs, "claims": self.claims, "asked": self.asked,
                "a1_pairs": self.a1_pairs, "rejudge": self.rejudge, "deferred": self.deferred,
                "stats": self.stats, "meta": self.meta}


# =========================================================================================================
class _Spec:
    """A candidate before caps / dedupe registration."""
    __slots__ = ("cand", "group", "family", "seq", "a1_pair", "rejudge_claim")

    def __init__(self, cand: I.Candidate, family: str, seq: int, group: Optional[str] = None,
                 a1_pair: Optional[str] = None, rejudge_claim: Optional[str] = None) -> None:
        self.cand, self.family, self.seq, self.group = cand, family, seq, group
        self.a1_pair, self.rejudge_claim = a1_pair, rejudge_claim


class Extractor:
    """ExtractorAPI. One instance per pipeline run; `xindex` carries history between runs."""

    def __init__(self, index: ProjectIndex, cfg: Optional[Mapping[str, Any]] = None,
                 actor_map: Optional[I.ActorMap] = None, xindex: Optional[ExtractIndex] = None,
                 clock: Optional[C.Clock] = None, fetch: Optional[Fetch] = None, project: Optional[str] = None) -> None:
        self.index = index
        self.cfg = cfg or {}
        self.actor_map = actor_map or I.ActorMap()
        self.x = xindex if xindex is not None else ExtractIndex()
        self.clock = clock or __import__("time").time
        self.fetch = fetch
        self.project = project or C.cfg_get(cfg, "project", "name", "") or index.project or "project"
        self._objs: Dict[str, I.Observation] = {}
        self._seq = 0
        g = lambda k: C.cfg_get(self.cfg, "extract", k)   # noqa: E731
        self.caps = {"A1": int(g("a1_max_per_run")), "A2": int(g("a2_max_per_run")), "A3": int(g("a3_max_per_run")),
                     "B1": int(g("b1_max_per_run"))}
        self.total_cap = int(g("max_candidates_per_run"))
        self.link_grace_s = float(g("link_grace_s"))
        self.a3_min = float(g("a3_min_jaccard"))
        self.a3_rule = float(g("a3_rule_restates_jaccard"))
        self.a2_sig = float(g("a2_signature_jaccard"))
        self.a2_gap_s = float(g("a2_max_gap_hours")) * 3600.0
        self.alias_thr = float(g("a1_alias_token_jaccard"))
        self.b1_max_ev = int(g("b1_max_evidence"))
        self.b1_ev_chars = int(g("b1_evidence_chars"))
        self.b1_rejudge = int(g("b1_rejudge_per_claim_per_day"))
        self.window_days = float(g("history_window_days"))
        self.max_obs = int(g("history_max_obs"))

    # ------------------------------------------------------------------ public
    def extract(self, new_obs: Sequence[I.Observation], history: Sequence[I.Observation] = (),
                state: Optional[I.MemoryState] = None, offsets: Optional[Mapping[str, int]] = None) -> I.ExtractResult:
        now = float(self.clock())
        self._now = now
        dropped: Dict[str, int] = collections.Counter()
        self._dropped = dropped
        offsets = offsets or {}
        # history: index silently (used after an index loss / for tests); never produces questions
        for o in sorted(_dedupe(history), key=lambda o: (C.parse_ts(o.ts), o.id)):
            if o.id in self.x.obs:
                continue
            self._objs[o.id] = o
            s, claims = self._ingest(o, offsets.get(o.id))
            self._mark_use(s, claims)
            self.x.add_obs(s)
            for c in claims:
                self.x.add_claim(self._claim_summary(c, s))
        result = I.ExtractResult()
        new_ids: List[str] = []
        new_claims: List[str] = []
        for o in sorted(_dedupe(new_obs, dropped), key=lambda o: (C.parse_ts(o.ts), o.id)):
            if o.id in self.x.obs:
                dropped["already_indexed"] += 1
                continue
            self._objs[o.id] = o
            s, claims = self._ingest(o, offsets.get(o.id))
            actor = self._actor(s)
            kept = []
            for c in claims:
                key = (actor, normalize_text(c.text))
                if key in self.x.claim_texts and self.x.claim_texts[key] != c.claim_id:
                    dropped["claim_duplicate_same_actor"] += 1
                    continue
                kept.append(c)
            kept_ids = {c.claim_id for c in kept}
            kept = [c for c in kept if not c.parent_claim_id or c.parent_claim_id in kept_ids]
            self._mark_use(s, kept)
            self.x.add_obs(s)
            for c in kept:
                self.x.add_claim(self._claim_summary(c, s))
                new_claims.append(c.claim_id)
                result.claims.append(c)
            new_ids.append(o.id)

        specs: List[_Spec] = []
        pairing = C.uniq(list(self.x.deferred) + new_ids)
        still_deferred: List[str] = []
        for oid in pairing:
            s = self.x.obs.get(oid)
            if s is None:
                continue
            if self._awaiting_link(s):
                dropped["awaiting_link"] += 1
                still_deferred.append(oid)
                continue
            specs += self._a1(s)
            specs += self._a2(s)
            for cid in self._claims_of(oid):
                specs += self._a3(self.x.claims[cid])
        new_set = set(new_claims)
        for cid in new_claims:
            specs += self._b1(self.x.claims[cid], trigger="new")
        # new evidence for older claims -> re-judge (<= b1_rejudge_per_claim_per_day)
        touched: List[str] = []
        for oid in new_ids:
            s = self.x.obs[oid]
            if s.get("kind") not in I.PRIMARY_OBS_KINDS or s.get("excluded"):
                continue
            for n in self._norms(s, grounded_only=True) + ["path:" + p for p in s.get("paths") or ()]:
                for cid in self.x.claim_norms.get(n, ())[-PARTNERS_PER_NORM:]:
                    if cid not in new_set and cid not in touched:
                        touched.append(cid)
            if s.get("kind") == "command" and s.get("outcome"):
                # a test run imported AFTER the claim it supports (Codex rollouts arrive late) is
                # new evidence for claims naming its target, and for the same actor's outcome claims just after it
                for k in s.get("keys") or ():
                    for cid in self.x.claim_tkeys.get(k, ())[-PARTNERS_PER_NORM:]:
                        if cid not in new_set and cid not in touched:
                            touched.append(cid)
                run_actor = self._actor(s)
                for cid, cc in list(self.x.claims.items()):
                    if cid in new_set or cid in touched or cc.get("outcome") not in I.RUN_OUTCOMES:
                        continue
                    gap = float(cc.get("ep", 0.0)) - float(s.get("ep", 0.0))
                    cs0 = self.x.obs.get(cc.get("obs_id"))
                    if 0.0 <= gap <= SESSION_RUN_WINDOW_S and cs0 is not None \
                            and I.actors_may_coincide(self._actor(cs0), run_actor):
                        touched.append(cid)
        for cid in touched:
            if cid in self.x.claims:
                specs += self._b1(self.x.claims[cid], trigger="evidence")
        self.x.deferred = C.uniq(still_deferred)[-DEFERRED_MAX:]
        result.candidates = self._select(specs)
        self.x.evict(now, self.window_days, self.max_obs)
        tot = self.x.stats.setdefault("dropped_total", {})
        for k, v in dropped.items():
            tot[k] = tot.get(k, 0) + v
        self.x.stats["runs"] = int(self.x.stats.get("runs", 0)) + 1
        result.dropped = dict(sorted(dropped.items()))
        return result

    # ------------------------------------------------------------------ ingest / summaries
    def _ingest(self, o: I.Observation, off: Optional[int]) -> Tuple[Dict[str, Any], List[I.Claim]]:
        text = o.text or ""
        hits: List[Hit] = [] if o.excluded else scan(o.id, text, self.index, self.cfg)
        tool = o.tool
        prov = o.provenance
        s: Dict[str, Any] = {
            "id": o.id, "ts": o.ts, "ep": C.parse_ts(o.ts), "kind": o.kind, "off": off,
            "prov": {k: getattr(prov, k, None) for k in PROV_KEYS},
            "commit": getattr(prov, "git_commit", None), "branch": getattr(prov, "git_branch", None),
            "excluded": bool(o.excluded), "paths": [p for p in o.paths if p and not (o.meta or {}).get("outside_project")],
            "gdirty": getattr(prov, "git_dirty", None) is True,
            "hits": [{"n": h.norm, "k": h.mention.kind, "s": h.mention.surface, "sp": list(h.mention.span),
                      "g": h.mention.grounded, "r": list(h.mention.resolved), "a1": h.a1_ok, "al": h.alias}
                     for h in hits],
        }
        legacy_patch = apply_patch_paths(tool.command or "") if (o.kind == "command" and tool is not None) else []
        if legacy_patch:
            # a Codex apply_patch heredoc imported (by older versions) as a command: it is a file edit
            root_paths = [p for p in legacy_patch if not p.startswith("/")]
            s.update({"kind": "file_edit", "paths": C.uniq(list(s["paths"]) + root_paths)})
        elif o.kind == "command" and tool is not None:
            test = tool.test
            cmd = tool.command or ""
            meta0 = o.meta if isinstance(o.meta, dict) else {}
            target = stored_target(meta0)
            if target is None:
                target = normalize_command((test.target if test and test.target else None)
                                           or meta0.get("test_target") or cmd)
            elif not target:
                target = normalize_command(cmd)   # ran no placeable test -> a plain command key
            outcome = None
            if test is not None or TEST_CMD_RE.search(" " + cmd) or is_test_command(cmd):
                # shared rule -- a running / unfinished run is never a pass
                outcome = test_outcome(tool)
            fids = list(test.failed_ids) if test else []
            keys = {"cmd:" + target} if target else set()
            for h in s["hits"]:
                if h["k"] in ("test", "file") and h["g"]:
                    keys.add(h["n"])
            for fid in fids:
                tres = self.index.resolve("test", fid)
                keys.add("test:" + tres[0] if len(tres) == 1 else "fid:" + fid)
                f = fid.split("::", 1)[0]
                if self.index.resolve("file", f) == [f]:
                    keys.add("path:" + f)
            meta = o.meta or {}
            s.update({"status": tool.status, "exit": tool.exit_code, "target": target, "outcome": outcome,
                      "failed_ids": fids, "keys": sorted(keys), "sig": exception_signature(text) if outcome == "fail" else None,
                      "runids": run_ids(text, exclude=[s["commit"] or ""]), "cmd": cmd[:300],
                      "dirty": meta.get("dirty_state") if isinstance(meta.get("dirty_state"), dict) else None,
                      "dirty_truncated": bool(meta.get("dirty_state_truncated")),
                      "imported": str(getattr(prov, "source", "") or "").startswith("import:")})
        claims = extract_claims(o, hits, self.cfg) if o.kind in I.ASSERTIVE_OBS_KINDS else []
        return s, claims

    def _mark_use(self, s: Dict[str, Any], claims: Sequence[I.Claim]) -> None:
        """A1 rule 4: a mention is 'in use' inside a claim sentence or in a failing command/test output."""
        failing = s.get("kind") == "command" and s.get("outcome") == "fail"
        spans = [c.span for c in claims]
        for h in s.get("hits") or ():
            a, b = h["sp"]
            h["use"] = bool(failing or any(x <= a and b <= y for x, y in spans))

    def _claim_summary(self, c: I.Claim, s: Mapping[str, Any]) -> Dict[str, Any]:
        tkeys = set()
        for m in c.mentions:
            if not m.grounded:
                continue
            if m.kind == "test":
                tkeys.add(m.norm)
                for r in m.resolved:
                    tkeys.add("test:" + r)
            elif m.kind == "file" and len(m.resolved) == 1 and self.index.is_test_file(m.resolved[0]):
                tkeys.add(m.norm)
        for t in command_targets(c.text):
            tkeys.add("cmd:" + t)
        return {"cid": c.claim_id, "obs_id": c.obs_id, "span": list(c.span), "text": c.text, "class": c.claim_class,
                "norms": C.uniq(m.norm for m in c.mentions), "gnorms": C.uniq(m.norm for m in c.mentions if m.grounded),
                "paths": list(c.paths), "parent": c.parent_claim_id, "actor": self._actor(s), "ep": s.get("ep", 0.0),
                "ts": s.get("ts"), "branch": s.get("branch"), "commit": s.get("commit"),
                "outcome": claimed_outcome(c.text), "tkeys": sorted(tkeys), "sig": exception_signature(c.text),
                "explicit": c.explicit}

    # ------------------------------------------------------------------ helpers
    def _actor(self, s: Mapping[str, Any]) -> str:
        return self.actor_map.actor_of(_Stub(s["id"], _prov_of(s)))

    def _eff_prov(self, s: Mapping[str, Any]) -> I.Provenance:
        return self.actor_map.provenance_of(_Stub(s["id"], _prov_of(s)))

    def _awaiting_link(self, s: Mapping[str, Any]) -> bool:
        p = s.get("prov") or {}
        if p.get("source") not in I.PROXY_SOURCES or p.get("host") not in I.AGENT_HOSTS:
            return False
        if self.actor_map.resolved(_Stub(s["id"], _prov_of(s))):     # link, or session_alias
            return False
        return self._now - float(s.get("ep", 0.0)) < self.link_grace_s

    def _claims_of(self, oid: str) -> List[str]:
        return sorted((c["cid"] for c in self.x.claims.values() if c.get("obs_id") == oid),
                      key=lambda cid: self.x.claims[cid]["span"][0])

    @staticmethod
    def _norms(s: Mapping[str, Any], grounded_only: bool = False) -> List[str]:
        return C.uniq(h["n"] for h in s.get("hits") or () if h.get("g") or not grounded_only)

    def _text(self, s: Mapping[str, Any]) -> Optional[str]:
        o = self._objs.get(s["id"])
        if o is None and self.fetch is not None:
            try:
                o = self.fetch(s["id"], s)
            except Exception:
                o = None
            if o is not None:
                self._objs[s["id"]] = o
        return None if o is None else (o.text or "")

    def _source(self, s: Mapping[str, Any], what: Optional[str] = None) -> str:
        p = self._eff_prov(s)
        host = p.host or "unknown"
        if host == "claude":
            who = "claude subagent %s" % (p.subagent_type or "agent") if p.subagent_id else "claude main agent"
        elif host == "codex":
            who = "codex session"
        elif host == "cursor":
            who = "cursor conversation"
        elif host == "cli":
            who = "a human (hearmemory cli)"
        else:
            who = host
        when = I.jev_time(s.get("ts")) or "unknown time"
        out = "%s%s at %s" % ((what + " by ") if what else "", who, when)
        if s.get("commit"):
            out += ", commit %s" % _short(s.get("commit"))
            if s.get("gdirty"):
                out += " plus uncommitted changes"     # not a run of that commit as committed
        return out

    def _cand(self, tid: str, subject_key: str, state: Dict[str, Any], basis: Sequence[str], priority: int,
              family: str, direction: Optional[str] = None, rule: Optional[str] = None, label: Optional[str] = None,
              meta: Optional[Dict[str, Any]] = None, group: Optional[str] = None, a1_pair: Optional[str] = None,
              rejudge_claim: Optional[str] = None) -> Optional[_Spec]:
        ver = I.TEMPLATE_VERSIONS[tid]
        state = _clean_state(state)
        h = I.input_hash(tid, ver, state)
        cid = I.candidate_id_for(tid, ver, subject_key, direction, h)
        akey = subject_key + "|" + (direction or "")
        prev = self.x.asked.get(akey)
        if prev and prev.get("h") == h:
            self._dropped["duplicate_question"] += 1
            return None
        m = dict(meta or {})
        if rule:
            m["rule_label"] = label
        cand = I.Candidate(candidate_id=cid, template_id=tid, template_version=ver, subject_key=subject_key,
                           state=state, input_hash=h, basis_obs_ids=C.uniq(basis), created_ts=C.ts_of(self._now),
                           direction=direction, priority=0 if rule else priority, rule_hint=rule,
                           supersedes=prev.get("cid") if prev else None, meta=m)
        self._seq += 1
        return _Spec(cand, family, self._seq, group=group, a1_pair=a1_pair, rejudge_claim=rejudge_claim)

    def _select(self, specs: Sequence[_Spec]) -> List[I.Candidate]:
        """Caps, per-batch subject dedupe and registration (asked / a1_pairs / rejudge)."""
        seen: Set[str] = set()
        rules: List[_Spec] = []
        jev: List[_Spec] = []
        for sp in specs:
            k = sp.cand.subject_key + "|" + (sp.cand.direction or "")
            if k in seen:
                self._dropped["duplicate_in_batch"] += 1
                continue
            seen.add(k)
            (rules if sp.cand.rule_hint else jev).append(sp)
        chosen: List[_Spec] = []
        for sp in sorted(rules, key=lambda sp: sp.seq)[:RULE_CAP_PER_RUN]:
            chosen.append(sp)
        self._dropped["cap_rule"] += max(0, len(rules) - RULE_CAP_PER_RUN)
        # group A3 directions: both or neither
        groups: Dict[str, List[_Spec]] = collections.OrderedDict()
        for sp in sorted(jev, key=lambda sp: (sp.cand.priority, sp.seq)):
            groups.setdefault(sp.group or ("#%d" % sp.seq), []).append(sp)
        used = collections.Counter()
        total = 0
        pairs_this_run: Set[str] = set()
        for g, members in groups.items():
            fam = members[0].family
            n = len(members)
            if fam == "A3" and n != 2:
                self._dropped["a3_missing_direction"] += 1
                continue
            a1p = members[0].a1_pair
            if a1p and a1p in pairs_this_run:
                self._dropped["a1_pair_per_day"] += 1
                continue
            if used[fam] + n > self.caps.get(fam, 0):
                self._dropped["cap_" + fam.lower()] += 1
                continue
            if total + n > self.total_cap:
                self._dropped["cap_total"] += 1
                continue
            used[fam] += n
            total += n
            if a1p:
                pairs_this_run.add(a1p)
            chosen.extend(members)
        day = C.utc_day(self._now)
        for sp in chosen:
            c = sp.cand
            self.x.asked[c.subject_key + "|" + (c.direction or "")] = {"h": c.input_hash, "cid": c.candidate_id,
                                                                        "t": self._now}
            if sp.a1_pair:
                self.x.a1_pairs[sp.a1_pair] = self._now
            if sp.rejudge_claim:
                r = self.x.rejudge.setdefault(sp.rejudge_claim, {})
                r[day] = r.get(day, 0) + 1
        return [sp.cand for sp in sorted(chosen, key=lambda sp: (sp.cand.priority, sp.seq))]

    # ------------------------------------------------------------------ A1 same object
    def _a1(self, s: Mapping[str, Any]) -> List[_Spec]:
        out: List[_Spec] = []
        for h in s.get("hits") or ():
            if not h.get("a1") or not h.get("use"):
                continue
            for pnorm, relation in self._a1_partner_norms(h):
                src = self.x.alias_rev if relation == "alias_rev" else self.x.norm_obs
                key_norm = h["n"] if relation == "alias_rev" else pnorm
                for pid in reversed(src.get(key_norm, [])[-PARTNERS_PER_NORM:]):
                    if pid == s["id"]:
                        continue
                    ps = self.x.obs.get(pid)
                    if ps is None:
                        continue
                    for ph in ps.get("hits") or ():
                        if relation == "alias_rev":
                            if not (ph.get("al") and h["n"] in (ph.get("r") or ())):
                                continue
                        elif ph["n"] != pnorm:
                            continue
                        if not ph.get("a1") or not ph.get("use"):
                            continue
                        sp = self._a1_pair(s, h, ps, ph, relation)
                        if sp is not None:
                            out.append(sp)
        return out

    def _a1_partner_norms(self, h: Mapping[str, Any]) -> List[Tuple[str, str]]:
        n, kind = h["n"], h["k"]
        out: List[Tuple[str, str]] = []
        if h.get("al"):
            return [(r, "alias") for r in h.get("r") or ()]
        if n.startswith("base:"):
            out.append((n, "ambiguous"))
            out += [("path:" + f, "ambiguous") for f in h.get("r") or ()]
        elif n.startswith("path:"):
            f = n[5:]
            base = f.rsplit("/", 1)[-1]
            if len(self.index.basenames.get(base, [])) >= 2:
                out.append(("base:" + base, "ambiguous"))
            out.append((n, "same_norm"))
            for sv, j in self._similar_services(n):
                out.append((sv, "cross_service"))
        elif n.startswith("symbol:"):
            out.append((n, "symbol"))
        elif n.startswith("test:*::"):
            out.append((n, "ambiguous"))
            out += [("test:" + r, "ambiguous") for r in h.get("r") or ()]
        elif n.startswith("test:"):
            out.append((n, "same_norm"))
            name = n.split("::", 1)[-1]
            if len(self.index.test_names.get(name, [])) >= 2:
                out.append(("test:*::" + name, "ambiguous"))
        elif n.startswith("service:"):
            for pn, j in self._similar_paths(n[8:]):
                out.append((pn, "cross_service"))
        if h.get("g") and not h.get("al"):
            out.append((n, "alias_rev"))
        return out

    def _similar_paths(self, service: str) -> List[Tuple[str, float]]:
        toks = ident_tokens(service)
        res = []
        for pn, ptoks in self.index.object_tokens.items():
            if pn.startswith("path:") and len(toks & ptoks) >= 2:
                j = jaccard(toks, ptoks)
                if round(j, 2) >= self.alias_thr:
                    res.append((pn, j))
        return sorted(res, key=lambda x: (-x[1], x[0]))[:3]

    def _similar_services(self, path_norm: str) -> List[Tuple[str, float]]:
        ptoks = self.index.object_tokens.get(path_norm)
        if not ptoks:
            return []
        res = []
        for sv in sorted(self.index.services):
            toks = ident_tokens(sv)
            if len(toks & ptoks) >= 2:
                j = jaccard(toks, ptoks)
                if round(j, 2) >= self.alias_thr:
                    res.append(("service:" + sv, j))
        return res[:3]

    def _a1_pair(self, s: Mapping[str, Any], h: Mapping[str, Any], ps: Mapping[str, Any], ph: Mapping[str, Any],
                 relation: str) -> Optional[_Spec]:
        d = self._dropped
        ka, kb = I.MENTION_COMPAT.get(h["k"]), I.MENTION_COMPAT.get(ph["k"])
        if ka != kb and relation != "cross_service":
            d["a1_incompatible_kinds"] += 1
            return None
        if self._awaiting_link(ps):
            d["awaiting_link"] += 1
            return None
        aa, ab = self._actor(s), self._actor(ps)
        if I.actors_may_coincide(aa, ab):
            d["a1_same_actor"] += 1
            return None
        rule = None
        if relation in ("same_norm", "symbol"):
            if relation == "symbol":
                defs = self.index.symbols.get(h["n"][7:], [])
                if len(defs) < 2:
                    if h["s"] == ph["s"]:
                        d["a1_trivial_same"] += 1
                        return None
                    rule = "A1_same_resolved_path"
                else:
                    na, nb = self._near_paths(s, h, defs), self._near_paths(ps, ph, defs)
                    if not na or not nb:
                        d["a1_unanchored"] += 1
                        return None
                    if na == nb and len(na) == 1:
                        rule = "A1_same_resolved_path"
                    elif na & nb:
                        d["a1_unanchored"] += 1
                        return None
            else:
                if h["s"] == ph["s"]:
                    d["a1_trivial_same"] += 1
                    return None
                rule = "A1_same_resolved_path"
        # order: a = earlier
        (s1, h1), (s2, h2) = sorted([(s, h), (ps, ph)], key=lambda x: (x[0].get("ep", 0.0), x[0]["id"]))
        pair_key = "|".join(sorted([h1["n"], h2["n"]]))
        if pair_key in self.x.a1_pairs and self._now - self.x.a1_pairs[pair_key] < 86400.0:
            d["a1_pair_per_day"] += 1
            return None
        ta, tb = self._text(s1), self._text(s2)
        if ta is None or tb is None:
            d["text_unavailable"] += 1
            return None
        node_a, node_b = I.span_node(s1["id"], h1["sp"]), I.span_node(s2["id"], h2["sp"])
        known = C.uniq([r for r in (h1.get("r") or []) + (h2.get("r") or [])])[:8]
        state = {
            "record_a": {"text": _clean(_window(ta, h1["sp"], A1_WINDOW)), "marked_mention": h1["s"]},
            "record_b": {"text": _clean(_window(tb, h2["sp"], A1_WINDOW)), "marked_mention": h2["s"]},
            "trusted_context": {"project": self.project, "object_kind": I.MENTION_COMPAT.get(h1["k"], h1["k"]),
                                "object_granularity": A1_OBJECT_GRANULARITY, "known_candidates": known,
                                "source_a": self._source(s1), "source_b": self._source(s2)},
        }
        meta = {"node_a": node_a, "node_b": node_b, "obs_a": s1["id"], "obs_b": s2["id"], "norm_a": h1["n"],
                "norm_b": h2["n"], "actor_a": self._actor(s1), "actor_b": self._actor(s2), "relation": relation}
        return self._cand("A1", "pair:" + "|".join(sorted([node_a, node_b])), state, [s1["id"], s2["id"]],
                          PRIORITY["A1"], "A1", rule=rule, label="same" if rule else None, meta=meta,
                          a1_pair=None if rule else pair_key)

    def _near_paths(self, s: Mapping[str, Any], h: Mapping[str, Any], defs: Sequence[str]) -> Set[str]:
        a, b = h["sp"]
        out: Set[str] = set()
        dset = set(defs)
        for x in s.get("hits") or ():
            if x["k"] in ("file", "module") and len(x.get("r") or []) == 1 and x["r"][0] in dset:
                if abs(x["sp"][0] - a) <= A1_WINDOW or abs(x["sp"][1] - b) <= A1_WINDOW:
                    out.add(x["r"][0])
        for p in s.get("paths") or ():
            if p in dset:
                out.add(p)
        return out

    # ------------------------------------------------------------------ A2 same event
    def _broad_watched(self, watched: Sequence[str], a: Mapping[str, Any], b: Mapping[str, Any]) -> List[str]:
        """Conservative widening of the watched paths: the code under test is unknown, so ANY project file
        edited between the two moments, or any path whose worktree fingerprint differs, counts as a change.
        A fix anywhere therefore makes an opposite result "outdated", never "refuted"."""
        ea, eb = sorted([float(a.get("ep", 0.0)), float(b.get("ep", 0.0))])
        out = set(watched)
        for e in self.x.edits:
            if ea < float(e.get("ep", 0.0)) <= eb:
                out.update(e.get("paths") or ())
        for s in (a, b):
            d = s.get("dirty")
            if isinstance(d, Mapping):
                out.update(d.keys())
        return sorted(p for p in out if p)

    def _events_of(self, s: Mapping[str, Any]) -> List[Dict[str, Any]]:
        evs = []
        if s.get("kind") == "command" and s.get("outcome") == "fail" and s.get("keys"):
            evs.append({"s": s, "claim": None, "keys": set(s["keys"]), "sig": s.get("sig"), "run": True,
                        "node": s["id"], "fids": set(s.get("failed_ids") or ())})
        for cid in self._claims_of(s["id"]):
            c = self.x.claims[cid]
            if c.get("outcome") == "fail" and c.get("tkeys"):
                evs.append({"s": s, "claim": c, "keys": set(c["tkeys"]), "sig": c.get("sig"), "run": False,
                            "node": I.span_node(s["id"], c["span"]), "fids": set()})
        return evs

    def _a2(self, s: Mapping[str, Any]) -> List[_Spec]:
        out: List[_Spec] = []
        for ev in self._events_of(s):
            partners: List[Tuple[str, Optional[str]]] = []
            for k in sorted(ev["keys"]):
                partners += [(pid, None) for pid in self.x.key_runs.get(k, ())[-PARTNERS_PER_NORM:]]
                partners += [(self.x.claims[cid]["obs_id"], cid) for cid in self.x.fail_reports.get(k, ())[-PARTNERS_PER_NORM:]
                             if cid in self.x.claims]
            seen: Set[str] = set()
            for pid, pcid in partners:
                ps = self.x.obs.get(pid)
                if ps is None or pid == s["id"]:
                    continue
                for pev in self._events_of(ps):
                    pkey = pev["node"]
                    if pkey in seen or (pcid is None) != pev["run"] or (pcid and pev["claim"]["cid"] != pcid):
                        continue
                    seen.add(pkey)
                    sp = self._a2_pair(ev, pev)
                    if sp is not None:
                        out.append(sp)
        return out

    def _a2_pair(self, ea: Dict[str, Any], eb: Dict[str, Any]) -> Optional[_Spec]:
        d = self._dropped
        sa, sb = ea["s"], eb["s"]
        shared = sorted(ea["keys"] & eb["keys"])
        if not shared and not (ea["fids"] & eb["fids"]):
            d["a2_no_shared_target"] += 1
            return None
        if self._awaiting_link(sb):
            d["awaiting_link"] += 1
            return None
        if I.actors_may_coincide(self._actor(sa), self._actor(sb)):
            d["a2_same_actor"] += 1
            return None
        if abs(float(sa.get("ep", 0.0)) - float(sb.get("ep", 0.0))) > self.a2_gap_s:
            d["a2_time_gap"] += 1
            return None
        siga, sigb = ea.get("sig"), eb.get("sig")
        if siga and sigb:
            if not signatures_match(siga, sigb, self.a2_sig):
                d["a2_signature_mismatch"] += 1
                return None
        elif ea["run"] and eb["run"] and not (ea["fids"] & eb["fids"]):
            d["a2_signature_missing"] += 1
            return None
        (e1, e2) = sorted([ea, eb], key=lambda e: (e["s"].get("ep", 0.0), e["node"]))
        s1, s2 = e1["s"], e2["s"]
        rule, label = None, None
        shared_ids = set(s1.get("runids") or ()) & set(s2.get("runids") or ()) if e1["run"] and e2["run"] else set()
        if not shared_ids and not (e1["run"] and e2["run"]):
            t1 = self._event_text(e1) or ""
            t2 = self._event_text(e2) or ""
            shared_ids = set(run_ids(t1, [s1.get("commit") or ""])) & set(run_ids(t2, [s2.get("commit") or ""]))
        if shared_ids:
            rule, label = "A2_shared_run_id", "same_event"
        elif e1["run"] and e2["run"] and s1.get("commit") and s2.get("commit") and s1["commit"] != s2["commit"]:
            watched = sorted({k[5:] for k in shared if k.startswith("path:")} |
                             {f.split("::", 1)[0] for f in (e1["fids"] | e2["fids"])} |
                             set(s1.get("paths") or ()) | set(s2.get("paths") or ()))
            facts = scope_facts(self.x.edits, s1, s2, self._broad_watched(watched, s1, s2))
            if facts.edited_paths_between or facts.dirty_changed_paths:
                rule, label = "A2_different_commit_runs", "different_events"
        t1, t2 = self._event_text(e1), self._event_text(e2)
        if t1 is None or t2 is None:
            d["text_unavailable"] += 1
            return None
        target = shared[0].split(":", 1)[1] if shared else sorted(e1["fids"] & e2["fids"])[0]
        state = {
            "record_a": {"text": _clean(t1), "marked_event": self._event_desc(e1)},
            "record_b": {"text": _clean(t2), "marked_event": self._event_desc(e2)},
            "trusted_context": {"project": self.project, "target": target, "commit_a": _short(s1.get("commit")),
                                "commit_b": _short(s2.get("commit")), "time_a": I.jev_time(s1.get("ts")),
                                "time_b": I.jev_time(s2.get("ts")), "source_a": self._source(s1),
                                "source_b": self._source(s2)},
        }
        meta = {"node_a": e1["node"], "node_b": e2["node"], "obs_a": s1["id"], "obs_b": s2["id"],
                "claim_a": e1["claim"]["cid"] if e1["claim"] else None,
                "claim_b": e2["claim"]["cid"] if e2["claim"] else None,
                "actor_a": self._actor(s1), "actor_b": self._actor(s2), "target": target}
        return self._cand("A2", "pair:" + "|".join(sorted([e1["node"], e2["node"]])), state, [s1["id"], s2["id"]],
                          PRIORITY["A2"], "A2", rule=rule, label=label, meta=meta)

    def _event_text(self, e: Mapping[str, Any]) -> Optional[str]:
        t = self._text(e["s"])
        if t is None:
            return None
        if e["claim"] is not None:
            a, b = e["claim"]["span"]
            return _window(t, [a, b], A2_WINDOW // 2)
        return _failure_window(t, e.get("sig"), A2_WINDOW)

    def _event_desc(self, e: Mapping[str, Any]) -> str:
        s = e["s"]
        if e["run"]:
            desc = "failing run of `%s`" % (s.get("target") or "a test command")
        else:
            tk = sorted(e["keys"])[0].split(":", 1)[1] if e["keys"] else "a test"
            desc = "reported failure of %s" % tk
        if e.get("sig"):
            desc += " (%s)" % e["sig"]
        return desc[:300]

    # ------------------------------------------------------------------ A3 duplicate / containment
    def _a3(self, c: Mapping[str, Any]) -> List[_Spec]:
        if c.get("class") == "premise":
            return []
        out: List[_Spec] = []
        seen: Set[str] = set()
        for n in c.get("gnorms") or ():
            for pcid in reversed(self.x.claim_norms.get(n, [])[-PARTNERS_PER_NORM:]):
                if pcid == c["cid"] or pcid in seen:
                    continue
                seen.add(pcid)
                pc = self.x.claims.get(pcid)
                if pc is None or pc.get("class") == "premise":
                    continue
                out += self._a3_pair(c, pc)
        return out

    def _a3_pair(self, ca: Mapping[str, Any], cb: Mapping[str, Any]) -> List[_Spec]:
        d = self._dropped
        sa, sb = self.x.obs.get(ca["obs_id"]), self.x.obs.get(cb["obs_id"])
        if sa is None or sb is None:
            return []
        if self._awaiting_link(sb):
            d["awaiting_link"] += 1
            return []
        if I.actors_may_coincide(self._actor(sa), self._actor(sb)):
            d["a3_same_actor"] += 1
            return []
        j = trigram_jaccard(ca["text"], cb["text"])
        if j < self.a3_min:
            d["a3_low_overlap"] += 1
            return []
        (c1, s1), (c2, s2) = sorted([(ca, sa), (cb, sb)], key=lambda x: (x[0].get("ep", 0.0), x[0]["cid"]))
        gates = []
        if s1.get("branch") and s2.get("branch") and s1["branch"] != s2["branch"]:
            gates.append("A3_scope_mismatch")
        else:
            watched = sorted(set(c1.get("paths") or ()) | set(c2.get("paths") or ()))
            if watched:
                facts = scope_facts(self.x.edits, s1, s2, watched)
                if facts.edited_paths_between:
                    gates.append("A3_scope_mismatch")
        node_a, node_b = I.span_node(c1["obs_id"], c1["span"]), I.span_node(c2["obs_id"], c2["span"])
        subject = "pair:" + "|".join(sorted([node_a, node_b]))
        near = normalize_text(c1["text"]) == normalize_text(c2["text"]) or j >= self.a3_rule
        rule = "A3_near_identical" if near else None
        scope1 = "branch %s, commit %s" % (s1.get("branch") or "unknown", _short(s1.get("commit")))
        scope2 = "branch %s, commit %s" % (s2.get("branch") or "unknown", _short(s2.get("commit")))
        base_meta = {"node_a": node_a, "node_b": node_b, "claim_a": c1["cid"], "claim_b": c2["cid"],
                     "obs_a": c1["obs_id"], "obs_b": c2["obs_id"], "actor_a": self._actor(s1),
                     "actor_b": self._actor(s2), "gates": gates, "trigram_jaccard": round(j, 4)}
        out = []
        for direction, (cont, clm, sc_a, sc_b) in (("a_contains_b", (c1, c2, scope1, scope2)),
                                                    ("b_contains_a", (c2, c1, scope2, scope1))):
            state = {"record_a": {"text": _clean(cont["text"])}, "record_b": {"claim": _clean(clm["text"])},
                     "trusted_context": {"project": self.project, "scope_a": sc_a, "scope_b": sc_b}}
            sp = self._cand("A3", subject, state, [c1["obs_id"], c2["obs_id"]], PRIORITY["A3"], "A3",
                            direction=direction, rule=rule, label="restates" if rule else None, meta=dict(base_meta),
                            group="A3:" + subject)
            if sp is None:
                return []
            out.append(sp)
        return out

    # ------------------------------------------------------------------ B1 evidence supports / refutes
    def _runs_for(self, tkeys: Iterable[str]) -> List[Dict[str, Any]]:
        ids: List[str] = []
        for k in tkeys:
            ids += self.x.key_runs.get(k, ())
        runs = [self.x.obs[i] for i in C.uniq(ids) if i in self.x.obs]
        return sorted(runs, key=lambda s: (s.get("ep", 0.0), s["id"]))

    def _b1(self, c: Mapping[str, Any], trigger: str) -> List[_Spec]:
        d = self._dropped
        cs = self.x.obs.get(c["obs_id"])
        if cs is None:
            return []
        if trigger == "evidence":
            n = self.x.rejudge.get(c["cid"], {}).get(C.utc_day(self._now), 0)
            if n >= self.b1_rejudge:
                d["b1_rejudge_cap"] += 1
                return []
        family_pri = PRIORITY["B1:" + c.get("class", "other")] if ("B1:" + c.get("class", "other")) in PRIORITY else PRIORITY["B1:other"]
        runs = self._runs_for(c.get("tkeys") or ())
        claim_ep = float(c.get("ep", 0.0))
        extra_scope: Dict[str, Any] = {}
        forced: List[Dict[str, Any]] = []
        rule, label = None, None
        if c.get("class") == "status" and c.get("outcome") in I.RUN_OUTCOMES and runs:
            later = [r for r in runs if r.get("ep", 0.0) > claim_ep]
            if later:
                run_b = later[-1]
                before = [r for r in runs if r.get("ep", 0.0) <= claim_ep]
                run_a = before[-1] if before else cs
                watched = sorted(set(c.get("paths") or ()) | set(_target_paths(run_b)) |
                                 (set(_target_paths(run_a)) if run_a is not cs else set()) |
                                 {k[5:] for k in c.get("tkeys") or () if k.startswith("path:")} |
                                 {k[5:].split("::", 1)[0] for k in c.get("tkeys") or () if k.startswith("test:") and "::" in k and not k.startswith("test:*")})
                facts = scope_facts(self.x.edits, run_a, run_b, self._broad_watched(watched, run_a, run_b))
                verdict = I.b1_status_rule(c["outcome"], run_b["outcome"], facts)
                forced = [run_b] + ([run_a] if run_a is not cs else [])
                if verdict in ("supports", "refutes"):
                    rule, label = "B1_test_status", verdict
                elif verdict == "outdated":
                    rule, label = "B1_test_status_changed", "outdated"
                    d["b1_outdated_by_change"] += 1
                else:
                    extra_scope = {"commit_claim": _short(run_a.get("commit")), "commit_run": _short(run_b.get("commit")),
                                   "edited_between": list(facts.edited_paths_between),
                                   "worktree_changed": (bool(facts.dirty_changed_paths) if facts.worktree_known else "unknown")}
        if not forced and runs:
            forced = [runs[-1]]
        claim_actor = self._actor(cs)
        if rule is None:
            if not runs and c.get("outcome") in I.RUN_OUTCOMES:
                forced = self._session_runs(c, claim_actor)
            # a run / read from BEFORE an edit of the paths in question (made before the claim)
            # shows the old code -- never evidence about the claim (the pre-fix `sed` output was)
            kept_forced = []
            for f in forced:
                if self._stale_by(f, c) is not None:
                    d["b1_stale_evidence"] += 1
                else:
                    kept_forced.append(f)
            forced = kept_forced
        work: List[Dict[str, Any]] = []
        if rule is None and (c.get("class") in ("status", "conclusion") or c.get("explicit")):
            work = self._session_work(c, cs, claim_actor)
        evidence = self._b1_evidence(c, cs, forced, rule is not None, work)
        if not evidence:
            d["b1_no_evidence"] += 1
            return []
        ev_state = []
        for es in evidence:
            t = self._text(es)
            if t is None:
                continue
            ev_state.append({"text": _clean(self._evidence_window(es, t, c)), "source": self._source(es, _what(es))})
        if not ev_state:
            d["text_unavailable"] += 1
            return []
        scope = {"project": self.project, "branch": cs.get("branch") or "unknown", "commit": _short(cs.get("commit")),
                 "paths": list(c.get("paths") or [])}
        scope.update(extra_scope)
        state = {"target_claim": _clean(c["text"]), "target_scope": scope, "evidence": ev_state}
        ev_ids = [e["id"] for e in evidence]
        claim_ep = float(c.get("ep", 0.0))
        cpaths = set(c.get("paths") or ())
        tkeys = set(c.get("tkeys") or ())
        # program facts for the memory layer (never sent to Jev): the claim author's own edits of the claimed
        # paths made before the claim IMPLEMENT it (never counter-evidence by construction), and runs whose
        # parsed outcome equals the claimed one with no later edit are program-checked direct support.
        implementing = [e["id"] for e in evidence if e.get("kind") == "file_edit"
                        and float(e.get("ep", 0.0)) <= claim_ep + 1.0
                        and I.actors_may_coincide(self._actor(e), claim_actor)
                        and (not cpaths or cpaths & set(e.get("paths") or ()))]
        direct = [e["id"] for e in evidence if e.get("kind") == "command" and e.get("outcome")
                  and c.get("outcome") in I.RUN_OUTCOMES and e.get("outcome") == c.get("outcome")
                  and float(e.get("ep", 0.0)) <= claim_ep + 1.0 and self._stale_by(e, c) is None
                  and (not tkeys or tkeys & set(e.get("keys") or ()))
                  and (tkeys or I.actors_may_coincide(self._actor(e), claim_actor))]
        meta = {"claim_id": c["cid"], "claim_class": c.get("class"), "parent_claim_id": c.get("parent"),
                "claim_obs_id": c["obs_id"], "claim_node": I.span_node(c["obs_id"], c["span"]),
                "evidence_obs_ids": ev_ids, "evidence_paths": C.uniq(p for e in evidence for p in (e.get("paths") or ())),
                "actor": claim_actor, "trigger": trigger,
                "implementing_edit_ids": implementing, "direct_support_ids": direct,
                "claimed_outcome": c.get("outcome")}
        sp = self._cand("B1", "claim:" + c["cid"], state, [c["obs_id"]] + ev_ids, family_pri, "B1", rule=rule,
                        label=label, meta=meta, rejudge_claim=c["cid"] if trigger == "evidence" else None)
        return [sp] if sp is not None else []

    def _b1_evidence(self, c: Mapping[str, Any], cs: Mapping[str, Any], forced: Sequence[Mapping[str, Any]],
                     rule_only: bool, work: Sequence[Mapping[str, Any]] = ()) -> List[Mapping[str, Any]]:
        forced = [f for f in forced if not f.get("excluded")]
        chosen: List[Mapping[str, Any]] = forced[:self.b1_max_ev]
        if rule_only:
            return chosen
        if work:
            # the author's best-matching edit and its commit come right after the first forced run
            chosen = forced[:1]
            for w in list(work[:2]) + forced[1:] + list(work[2:]):
                if len(chosen) < self.b1_max_ev and w["id"] not in {f["id"] for f in chosen}:
                    chosen.append(w)
        pool_ids: List[str] = []
        for n in c.get("gnorms") or ():
            pool_ids += self.x.norm_obs.get(n, [])[-B1_EVIDENCE_POOL:]
        for p in c.get("paths") or ():
            pool_ids += self.x.path_obs.get(p, [])[-B1_EVIDENCE_POOL:]
        have = {f["id"] for f in chosen}
        pool = []
        for pid in C.uniq(pool_ids):
            ps = self.x.obs.get(pid)
            if ps is None or pid in have or pid == cs["id"] or ps.get("excluded"):
                continue
            if ps.get("kind") not in I.PRIMARY_OBS_KINDS:
                continue
            if ps.get("kind") == "file_read" and not self._text(ps):
                continue
            if ps.get("kind") != "file_edit" and self._stale_by(ps, c) is not None:
                self._dropped["b1_stale_evidence"] += 1
                continue
            pool.append(ps)
        pool = sorted(pool, key=lambda s: (-s.get("ep", 0.0), s["id"]))[:B1_EVIDENCE_POOL]
        texts = [self._text(p) or "" for p in pool]
        scores = bm25_scores(c["text"], texts)
        ranked = sorted(zip(pool, scores), key=lambda x: (-x[1], -x[0].get("ep", 0.0), x[0]["id"]))
        for ps, sc in ranked:
            if len(chosen) >= self.b1_max_ev:
                break
            if sc <= 0 and chosen:
                continue
            chosen.append(ps)
        return self._keep_defining_edit(c, cs, chosen, list(work) + pool)

    def _keep_defining_edit(self, c: Mapping[str, Any], cs: Mapping[str, Any], chosen: List[Mapping[str, Any]],
                            seen: Sequence[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
        """a change claim ("Added sub(a, b) returning a - b with a test; python -m pytest -q passes all
        12 tests.") keeps at least one edit whose added lines contain the identifiers it names -- one DEFINING them
        when such an edit exists. Before, a later independent test run filled the budget with two runs plus the
        test-file edit; the edit adding `sub` was crowded out and the model's support fell from 0.88 to 0.37.
        The other items keep their order (the newest run first); to make room, non-run items go first, then the
        claim author's own runs (oldest first), then other runs."""
        idents = _code_idents(c.get("text", ""))
        if not idents or self.b1_max_ev < 2:
            return chosen
        claim_ep = float(c.get("ep", 0.0))
        claim_actor = self._actor(cs)
        want = set(c.get("paths") or ()) | set(cs.get("paths") or ())
        cands: Dict[str, Mapping[str, Any]] = {}
        for e in list(seen) + list(self.x.edits[-B1_EVIDENCE_POOL * 5:]):
            if e.get("kind") == "file_edit" and not e.get("excluded") and float(e.get("ep", 0.0)) <= claim_ep + 1.0:
                cands.setdefault(e["id"], e)
        for e in chosen:
            if e.get("kind") == "file_edit":
                cands.setdefault(e["id"], e)
        match = {i: _ident_match(self._text(e) or "", idents) for i, e in cands.items()}

        def level(e: Mapping[str, Any]) -> int:
            d, n = match.get(e["id"], (False, 0))
            return 2 if d else 1 if n else 0
        best = max((level(e) for e in cands.values()), default=0)
        if best == 0 or any(e.get("kind") == "file_edit" and level(e) >= best for e in chosen):
            return chosen
        key = sorted((e for e in cands.values() if level(e) == best),
                     key=lambda e: (not I.actors_may_coincide(self._actor(e), claim_actor),
                                    not (want & set(e.get("paths") or ())), -match[e["id"]][1],
                                    -float(e.get("ep", 0.0)), e["id"]))[0]
        lead = chosen[:1] if chosen and chosen[0].get("kind") == "command" else []
        rest = list(chosen[len(lead):])

        def drop_rank(item: Tuple[int, Mapping[str, Any]]) -> Tuple[int, float, int]:
            ix, e = item
            if e.get("kind") != "command" or not e.get("outcome"):
                return (0, 0.0, -ix)                            # non-run items first, the last one first
            own = I.actors_may_coincide(self._actor(e), claim_actor)
            return (1 if own else 2, float(e.get("ep", 0.0)), ix)
        while rest and len(lead) + 1 + len(rest) > self.b1_max_ev:
            del rest[min(enumerate(rest), key=drop_rank)[0]]
        self._dropped["b1_defining_edit_kept"] += 1
        return (lead + [key] + rest)[:self.b1_max_ev]

    def _session_work(self, c: Mapping[str, Any], cs: Mapping[str, Any], claim_actor: str) -> List[Dict[str, Any]]:
        """what the claim's author itself did shortly before a status / change claim ("新增 div(a, b)
        ...; pytest 通过（3 passed），并已提交") -- its file edits and its `git commit`. Without them Jev saw only the
        test run and answered insufficient twice. Returned in the order they may use the evidence budget: the edit
        best matching the claim (recorded / claimed paths first), the latest successful commit, the other edits."""
        claim_ep = float(c.get("ep", 0.0))
        lo = claim_ep - SESSION_RUN_WINDOW_S
        want = set(c.get("paths") or ()) | set(cs.get("paths") or ())

        def own(s: Mapping[str, Any]) -> bool:
            ep = float(s.get("ep", 0.0))
            return (not s.get("excluded") and lo <= ep <= claim_ep + 1.0
                    and I.actors_may_coincide(self._actor(s), claim_actor))

        def own_actor(s: Mapping[str, Any]) -> bool:
            return I.actors_may_coincide(self._actor(s), claim_actor)
        latest: Dict[str, Dict[str, Any]] = {}
        for e in sorted((e for e in self.x.edits if own(e)), key=lambda s: (s.get("ep", 0.0), s["id"])):
            for p in e.get("paths") or ("?",):
                latest[p] = e                          # the newest edit of each path
        edits = list({e["id"]: e for e in latest.values()}.values())
        texts = {e["id"]: self._text(e) or "" for e in edits}
        scores = dict(zip((e["id"] for e in edits), bm25_scores(c["text"], [texts[e["id"]] for e in edits])))
        # a claim naming no file ("新增 mul(a, b) 返回 a * b ...") -- the edit whose diff defines / uses
        # the identifiers it names comes first, else the session's latest edits
        idents = _code_idents(c.get("text", ""))
        match = {e["id"]: _ident_match(texts[e["id"]], idents) for e in edits}
        edits.sort(key=lambda e: (not (want & set(e.get("paths") or ())), not match[e["id"]][0],
                                  -match[e["id"]][1], -scores.get(e["id"], 0.0), -float(e.get("ep", 0.0)), e["id"]))
        commit, after = None, None
        for s in self.x.obs.values():
            if s.get("kind") != "command" or s.get("excluded") or not GIT_COMMIT_CMD_RE.search(s.get("cmd") or ""):
                continue
            if not (s.get("exit") == 0 or (s.get("exit") is None and s.get("status") == "ok")):
                continue
            key = (float(s.get("ep", 0.0)), s["id"])
            if own(s):
                if commit is None or key > (float(commit.get("ep", 0.0)), commit["id"]):
                    commit = s
            elif claim_ep + 1.0 < key[0] <= claim_ep + COMMIT_AFTER_CLAIM_S \
                    and I.actors_may_coincide(self._actor(s), claim_actor) \
                    and not any(claim_ep + 1.0 < float(e.get("ep", 0.0)) < key[0] and own_actor(e) for e in self.x.edits):
                # `hearmemory record "新增 mul ..." && git add && git commit` -- the commit right after the
                # claim (no edit of the author in between) is what the claim reported
                if after is None or key < (float(after.get("ep", 0.0)), after["id"]):
                    after = s
        commit = commit or after
        return edits[:1] + ([commit] if commit is not None else []) + edits[1:]

    def _session_runs(self, c: Mapping[str, Any], claim_actor: str) -> List[Dict[str, Any]]:
        """The latest test run (parsed outcome) of the claim's own actor shortly before the claim."""
        claim_ep = float(c.get("ep", 0.0))
        best: Optional[Dict[str, Any]] = None
        for s in self.x.obs.values():
            if s.get("kind") != "command" or not s.get("outcome") or s.get("excluded"):
                continue
            ep = float(s.get("ep", 0.0))
            if ep > claim_ep + 1.0 or ep < claim_ep - SESSION_RUN_WINDOW_S:
                continue
            if not I.actors_may_coincide(self._actor(s), claim_actor):
                continue
            if best is None or (ep, s["id"]) > (float(best.get("ep", 0.0)), best["id"]):
                best = s
        return [best] if best is not None else []

    def _stale_by(self, s: Mapping[str, Any], c: Mapping[str, Any]) -> Optional[str]:
        """Id of a file edit made after observation `s` and no later than the claim, touching a path that
        `s` or the claim is about (then `s` shows code as it was BEFORE that edit). None when not stale."""
        watched = set(c.get("paths") or ()) | set(s.get("paths") or ())
        for h in s.get("hits") or ():
            if h.get("k") == "file" and h.get("g") and len(h.get("r") or ()) == 1:
                watched.add(h["r"][0])
        if not watched:
            return None
        ea, eb = float(s.get("ep", 0.0)), float(c.get("ep", 0.0)) + 1.0
        for e in self.x.edits:
            if e.get("id") == s.get("id"):
                continue
            t = float(e.get("ep", 0.0))
            if ea < t <= eb and watched & set(e.get("paths") or ()):
                return e["id"]
        return None

    def _evidence_window(self, es: Mapping[str, Any], text: str, c: Mapping[str, Any]) -> str:
        size = self.b1_ev_chars
        header = ""
        body = text
        if es.get("kind") == "file_edit":
            # an edit is shown as before -> after (both - and + lines), never head-truncated
            focus = [n.split(":", 1)[-1].rsplit("/", 1)[-1] for n in c.get("gnorms") or ()]
            focus += sorted(t for t in ident_tokens(c.get("text", "")) if len(t) >= 3)[:24]
            return compact_diff(text, size, focus=focus, path=(es.get("paths") or [""])[0])
        if es.get("kind") == "command":
            first, _, rest = text.partition("\n")
            header, body = (first + "\n", rest) if first.startswith("$ ") else ("", text)
            test = getattr(getattr(self._objs.get(es["id"]), "tool", None), "test", None)
            if test is not None:
                return _test_run_window(header, body, test, size, es.get("sig") if es.get("outcome") == "fail" else None)
            if es.get("outcome") == "fail":
                return (header + _failure_window(body, es.get("sig"), size - len(header)))[:size]
        norms = set(c.get("gnorms") or ())
        spans = [h["sp"] for h in es.get("hits") or () if h["n"] in norms]
        if spans and header:
            off = len(header)
            spans = [[a - off, b - off] for a, b in spans if a >= off]
        if spans:
            return (header + _window(body, spans[0], (size - len(header)) // 2))[:size]
        return (header + body[-(size - len(header)):])[:size] if len(header + body) > size else header + body


# =========================================================================================================
def _dedupe(objs: Iterable[I.Observation], dropped: Optional[Dict[str, int]] = None) -> List[I.Observation]:
    seen: Set[str] = set()
    out = []
    for o in objs:
        if o is None or o.id in seen:
            if dropped is not None and o is not None:
                dropped["duplicate_obs"] += 1
            continue
        seen.add(o.id)
        out.append(o)
    return out


def _window(text: str, span: Sequence[int], radius: int) -> str:
    a, b = int(span[0]), int(span[1])
    lo = max(0, a - radius)
    hi = min(len(text), b + radius)
    out = text[lo:hi]
    return ("…" if lo > 0 else "") + out + ("…" if hi < len(text) else "")


def _failure_window(text: str, sig: Optional[str], size: int) -> str:
    """Failure text: around the exception signature when present, else the tail (summaries are at the end)."""
    size = max(100, int(size))
    if sig:
        exc = sig.split(":", 1)[0]
        i = text.find(exc)
        if i >= 0:
            return _window(text, [i, i + len(exc)], size // 2)
    if len(text) <= size:
        return text
    return "…" + text[-size:]


_CODE_IDENT_RE = re.compile(r"`([A-Za-z_][\w.]*)|\b([A-Za-z_]\w*)\s*\(|\b([a-z]+_\w+|[a-z]+[A-Z]\w*)\b")
_IDENT_STOP = {"python", "python3", "pytest", "test", "tests", "src", "git"}


def _code_idents(text: str) -> Set[str]:
    """Identifiers a claim names as code: `name`, name(...), snake_case / camelCase words."""
    out = set()
    for m in _CODE_IDENT_RE.finditer(text or ""):
        t = (m.group(1) or m.group(2) or m.group(3) or "").rsplit(".", 1)[-1]
        if len(t) >= 2 and t.lower() not in _IDENT_STOP:
            out.add(t)
    return out


def _ident_match(edit_text: str, idents: Set[str]) -> Tuple[bool, int]:
    """(an added line defines one of `idents`, number of added lines using one of them)."""
    if not idents:
        return False, 0
    added = [ln[1:] for ln in (edit_text or "").splitlines() if ln.startswith("+") and not ln.startswith("+++")]
    alt = "|".join(re.escape(i) for i in sorted(idents))
    use = re.compile(r"\b(?:%s)\b" % alt)
    define = re.compile(r"\b(?:def|class|function|func|fn|const|let|var|type|interface)\s+(?:%s)\b" % alt)
    return any(define.search(ln) for ln in added), sum(1 for ln in added if use.search(ln))


# the runner's own summary lines ("7 passed in 0.00s", "Ran 3 tests", "OK", "test result: ...")
_SUMMARY_LINE_RE = re.compile(r"(?i)\b\d+\s+(?:passed|failed|errors?|skipped|xfailed|xpassed)\b|^\s*Ran \d+ tests?\b|"
                              r"^\s*(?:OK|FAILED)\b(?:\s*\(|\s*$)|^\s*Tests:\s+\d|test result:|^(?:ok|FAIL|--- FAIL)\s")


def _test_run_window(header: str, body: str, test: Any, size: int, sig: Optional[str]) -> str:
    """a test run as evidence ALWAYS shows its command, the parsed result and the runner's summary
    line(s); the rest of the budget holds other output (head + tail, or around a failure), cut honestly. Before,
    `pytest -q && git diff && git status` kept only the tail -- mid-diff -- so "7 passed" never reached Jev."""
    counts = ", ".join([f"{int(getattr(test, 'passed', 0) or 0)} passed", f"{int(getattr(test, 'failed', 0) or 0)} failed"]
                       + [f"{int(getattr(test, k) or 0)} {k}" for k in ("errors", "skipped") if getattr(test, k, 0)])
    lines = body.splitlines()
    summ = [i for i, ln in enumerate(lines) if _SUMMARY_LINE_RE.search(ln)][-3:]
    head = header + f"[hearmemory: parsed {getattr(test, 'runner', None) or 'test'} result: {counts}]\n" + \
        "".join(_clip_line(lines[i], 200) + "\n" for i in summ)
    rest = "\n".join(ln for i, ln in enumerate(lines) if i not in set(summ))
    room = size - len(head)
    if not rest.strip() or room < 40:
        return head[:size] if len(head) > size else head
    if sig:
        return head + _failure_window(rest, sig, room)[:room]
    if len(rest) + 1 <= room:
        return head + rest
    keep = room - 40
    mark = f"\n…[hearmemory: {len(rest) - keep} chars omitted]…\n"
    return (head + rest[:keep // 2] + mark + rest[len(rest) - (keep - keep // 2):])[:size]


def _clip_line(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n - 1] + "…"


def _what(s: Mapping[str, Any]) -> str:
    return {"command": "command run", "file_edit": "file edit", "file_read": "file read",
            "search": "search"}.get(s.get("kind"), s.get("kind") or "record")


def _target_paths(run: Mapping[str, Any]) -> List[str]:
    out = [k[5:] for k in run.get("keys") or () if k.startswith("path:")]
    out += [f.split("::", 1)[0] for f in run.get("failed_ids") or ()]
    out += list(run.get("paths") or ())
    return C.uniq(out)
