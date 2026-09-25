"""Test / check run index (program facts for P3 run results, failing_check and C2 suggestions).

A "run" is a command observation that ran a test (tool.test set, or a known test-runner command). For each
normalised target we keep the latest run, the latest failing and passing run and the actors that saw it
fail. Built from raw observations by the MemoryBuilder and topped up by the fresh overlay."""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from hearmemory.interfaces import Observation, actors_may_coincide
from hearmemory.testcmd import (_pytest_parts, canonical_target, is_test_command as _is_test_command, stored_target,
                           target_covers, test_outcome)

from hearmemory.textutil import GIT_COMMIT_CMD_RE, is_amend_commit, new_commit_sha

from .relevance import norm_path, paths_in_text
from .text import ts_seconds

MAX_FAIL_ACTORS = 6
_BOUNDARY_RE = re.compile(r"/|::")


def is_test_command(cmd: Optional[str]) -> bool:
    return _is_test_command(cmd)


def normalize_target(cmd: str) -> str:
    """The shared canonical test target: one function for the run index, the extractor,
    RunnerSummary.target and claims, so `python -m pytest -q x`, `cd <proj> && pytest x 2>&1 | tail`
    and `.venv/bin/pytest x` are the same target."""
    return canonical_target(cmd)


def run_outcome(obs: Observation) -> Optional[str]:
    """pass / fail / None. A run that is still going (status "running") or whose end is unknown
    (no runner summary and no exit code) is None -- never a pass, and never overwrites a failure."""
    return test_outcome(obs.tool)


def _key(rec: Mapping[str, Any]) -> Tuple[str, str]:
    return (rec["ts"], rec["obs_id"])


def run_record(obs: Observation, actor: str) -> Optional[Dict[str, Any]]:
    """Observation -> run record, or None when it is not a test run with a known outcome."""
    if obs.kind != "command" or obs.tool is None or obs.excluded:
        return None
    t = obs.tool
    if t.test is None and not is_test_command(t.command):
        return None
    outcome = run_outcome(obs)
    if outcome is None:
        return None
    meta = obs.meta if isinstance(obs.meta, dict) else {}
    # target: canonicalised at capture time with the project root and cwd (observe._finish_tool) and
    # used as-is ("" = a runner that ran no test we can place -> not a run). Observations stored
    # by older versions are canonicalised again here so they get the same key.
    target = stored_target(meta)
    if target is None:
        raw = ((t.test.target if t.test is not None and t.test.target else None) or meta.get("test_target")
               or t.command or "")
        target = canonical_target(raw)
    if not target:
        return None
    paths = [norm_path(p) for p in (t.paths or []) if norm_path(p)]
    failed_ids = list((t.test.failed_ids if t.test else []) or [])[:20]
    for fid in failed_ids:
        p = norm_path(fid)
        if p and p not in paths:
            paths.append(p)
    for p in paths_in_text(t.command or "", limit=10):
        if p not in paths:
            paths.append(p)
    prov = obs.provenance
    summary = ""
    if t.test is not None:
        bits = []
        for name in ("failed", "errors", "passed", "skipped"):
            v = getattr(t.test, name, 0) or 0
            if v:
                bits.append(f"{v} {name}")
        summary = ", ".join(bits)
    elif t.exit_code is not None:
        summary = f"exit {t.exit_code}"
    rec = {"obs_id": obs.id, "ts": obs.ts, "target": target, "outcome": outcome, "actor": actor,
           "host": prov.host if prov else None, "session": prov.session_id if prov else None,
           "subagent": prov.subagent_id if prov else None, "subagent_type": prov.subagent_type if prov else None,
           "commit": prov.git_commit if prov else None, "paths": paths[:20], "failed_ids": failed_ids,
           "summary": summary, "command": (t.command or "")[:300]}
    if prov is not None and prov.git_dirty is True:
        rec["dirty"] = True         # ran with uncommitted changes -- not exactly `commit`
    return rec


RUN_COMMIT_WINDOW_S = 2 * 3600.0


def link_dirty_runs(obs_list: Iterable[Observation], actor_of: Mapping[str, str],
                    recs: Mapping[str, Dict[str, Any]]) -> None:
    """a run on a dirty tree (rec["dirty"]) followed -- with no edit by the same actor in between --
    by that actor's successful `git commit` tested exactly what the commit contains: rec["commit_to"] = its sha
    (rendered "9929756+dirty → 8761b49"). That actor's later `git commit --amend`s of it (no edit of
    its in between) are followed -- commit_to is the last amended sha (387134d -> dfd325c -> 1dcbbe0); an edit made
    at the very same time stamp as the run (one Codex code-mode snippet: apply_patch, then pytest) is not after
    it. `obs_list` is in (ts, id) order; recs: obs_id -> record with "ts", "actor" (a run record, or any record
    made on a dirty tree)."""
    waiting: List[Dict[str, Any]] = []
    linked: List[Dict[str, Any]] = []
    for o in obs_list:
        actor = actor_of.get(o.id) or "?:?"
        if o.id in recs and recs[o.id].get("dirty"):
            waiting.append(recs[o.id])
            continue
        if not waiting and not linked:
            continue
        if o.kind == "file_edit":
            waiting = [r for r in waiting if not actors_may_coincide(r["actor"], actor) or (o.ts or "") <= (r["ts"] or "")]
            linked = [r for r in linked if not actors_may_coincide(r["actor"], actor)]
            continue
        t = o.tool
        if o.kind != "command" or t is None or not GIT_COMMIT_CMD_RE.search(t.command or ""):
            continue
        sha = new_commit_sha(o.text or "") if (t.exit_code == 0 or t.status == "ok") else None
        if not sha:
            continue
        if is_amend_commit(t.command or ""):
            for r in linked:
                if actors_may_coincide(r["actor"], actor):
                    r["commit_to"] = sha[:12]
        end = ts_seconds(o.ts) or 0.0
        for r in [r for r in waiting if actors_may_coincide(r["actor"], actor)]:
            if end - (ts_seconds(r["ts"]) or 0.0) <= RUN_COMMIT_WINDOW_S:
                r["commit_to"] = sha[:12]
                linked.append(r)
            waiting.remove(r)


def effective_last(cur: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The latest run that says something about this target: its own latest run, or a NEWER passing
    run of a wider target covering it (`pytest tests/x.py` passing after
    `pytest tests/x.py::test_f` failed; a bare `pytest` passing covers every pytest target)."""
    last = cur.get("last")
    cb = cur.get("covered_by")
    if cb and (last is None or _key(cb) > _key(last)):
        return cb
    return last


def _path_key(p: str) -> str:
    return p if "::" in p else (p.rstrip("/") or p)


def _cover_keys(wide: str) -> List[Tuple[str, str]]:
    """Where a (passing) target is filed for coverage lookups: each `&&` part under ("eq", part);
    a bare pytest part under ("bare", ""); a pytest part with paths under ("path", p) per path."""
    keys: List[Tuple[str, str]] = []
    for part in wide.split(" && "):
        keys.append(("eq", part))
        pp = _pytest_parts(part)
        if pp is None:
            continue
        if not pp[1]:
            keys.append(("bare", ""))
        for p in pp[1]:
            keys.append(("path", _path_key(p)))
    return keys


def _ancestors(p: str) -> List[str]:
    """`tests/unit/x.py::T::t` -> itself, `tests`, `tests/unit`, `tests/unit/x.py`, `tests/unit/x.py::T`."""
    out = [p]
    for m in _BOUNDARY_RE.finditer(p):
        if m.start():
            out.append(p[:m.start()])
    return out


def _candidate_wides(narrow: str, by_key: Mapping[Tuple[str, str], List[str]]) -> List[str]:
    """Targets that MAY cover `narrow` (target_covers decides): whatever covers it must cover its
    first part, i.e. be equal to it, be a bare `pytest`, or name a path that is a prefix of its first
    path."""
    first = narrow.split(" && ", 1)[0]
    keys: List[Tuple[str, str]] = [("eq", first)]
    pp = _pytest_parts(first)
    if pp is not None:
        keys.append(("bare", ""))
        if pp[1]:
            keys.extend(("path", a) for a in _ancestors(pp[1][0]))
    out: Dict[str, None] = {}
    for k in keys:
        for w in by_key.get(k, ()):
            out[w] = None
    return list(out)


class RunIndex:
    """target -> {"last": rec, "last_fail": rec|None, "last_pass": rec|None, "fail_actors": [actor, ...],
    "covered_by": rec|None (newest passing run of a wider target that covers this one)}.

    `covered_by` is NOT computed on every add() (that compared each new record with every
    target: records x targets calls of target_covers). add() only marks the index stale; the
    coverage is recomputed once, on the next read (`targets`, to_dict(), ...) or by finalize(), in
    about O(targets): the newest passing run of every target is indexed by the paths it names, and each
    target only checks the wide targets found under its own path prefixes (plus bare `pytest` runs)."""

    def __init__(self, data: Optional[Mapping[str, Any]] = None):
        self._targets: Dict[str, Dict[str, Any]] = {k: dict(v) for k, v in (data or {}).items()}
        self._stale = False

    @property
    def targets(self) -> Dict[str, Dict[str, Any]]:
        self.finalize()
        return self._targets

    def add(self, rec: Mapping[str, Any]) -> None:
        tgt = rec["target"]
        cur = self._targets.setdefault(tgt, {"last": None, "last_fail": None, "last_pass": None, "fail_actors": []})

        def newer(a: Optional[Mapping[str, Any]]) -> bool:
            return a is None or (rec["ts"], rec["obs_id"]) >= (a["ts"], a["obs_id"])

        if cur.get("last") and cur["last"]["obs_id"] == rec["obs_id"]:
            return
        if newer(cur.get("last")):
            cur["last"] = dict(rec)
        key = "last_fail" if rec["outcome"] == "fail" else "last_pass"
        if newer(cur.get(key)):
            cur[key] = dict(rec)
        if rec["outcome"] == "fail":
            fa = cur.setdefault("fail_actors", [])
            if rec["actor"] not in fa and len(fa) < MAX_FAIL_ACTORS:
                fa.append(rec["actor"])
        self._stale = True

    def finalize(self, check: Optional[Callable[[], None]] = None) -> None:
        """Recompute every target's `covered_by` if records were added since the last time. `check`
        (a deadline callback that raises) is called every few hundred targets; the index then stays
        stale and is recomputed on the next read."""
        if not self._stale:
            return
        wide: Dict[str, Mapping[str, Any]] = {t: c["last_pass"] for t, c in self._targets.items()
                                              if c.get("last_pass")}
        by_key: Dict[Tuple[str, str], List[str]] = {}
        for t in wide:
            for k in _cover_keys(t):
                by_key.setdefault(k, []).append(t)
        for i, (n, cur) in enumerate(self._targets.items()):
            if check is not None and i % 256 == 255:
                check()
            best: Optional[Mapping[str, Any]] = None
            for w in _candidate_wides(n, by_key):
                if w == n:
                    continue
                lp = wide[w]
                if best is not None and _key(lp) <= _key(best):
                    continue
                if target_covers(w, n):
                    best = lp
            if best is not None:
                cur["covered_by"] = dict(best)
            else:
                cur.pop("covered_by", None)
        self._stale = False

    def to_dict(self) -> Dict[str, Any]:
        targets = self.targets
        return {k: targets[k] for k in sorted(targets)}

    def copy(self) -> "RunIndex":
        return RunIndex({k: {kk: (dict(vv) if isinstance(vv, dict) else list(vv) if isinstance(vv, list) else vv)
                             for kk, vv in v.items()} for k, v in self.targets.items()})

    def latest(self, target: str) -> Optional[Dict[str, Any]]:
        cur = self.targets.get(target)
        return effective_last(cur) if cur else None

    def failing(self) -> List[Dict[str, Any]]:
        """Targets whose latest run failed (no later pass of it, or of a wider target covering it)."""
        out = []
        for tgt, cur in self.targets.items():
            last = effective_last(cur)
            if last and last["outcome"] == "fail":
                out.append(last)
        return sorted(out, key=lambda r: (r["ts"], r["obs_id"]), reverse=True)

    def all_latest(self) -> List[Dict[str, Any]]:
        seen: Dict[str, Dict[str, Any]] = {}
        for c in self.targets.values():
            e = effective_last(c)
            if e and e["obs_id"] not in seen:
                seen[e["obs_id"]] = e
        return sorted(seen.values(), key=lambda r: (r["ts"], r["obs_id"]), reverse=True)

    def passed_after(self, target: str, ts: str) -> Optional[Dict[str, Any]]:
        cur = self.targets.get(target)
        e = effective_last(cur) if cur else None
        if e and e["outcome"] == "pass" and e["ts"] > ts:
            return e
        return None

    def distinct_fail_actors(self, target: str) -> int:
        cur = self.targets.get(target) or {}
        reps: List[str] = []
        for a in cur.get("fail_actors") or []:
            if not any(actors_may_coincide(a, r) for r in reps):
                reps.append(a)
        return len(reps)


def targets_for_paths(index: RunIndex, paths: Iterable[str]) -> List[Dict[str, Any]]:
    """Latest runs of targets related to `paths`: path overlap, or test_<stem> naming of a staged source file."""
    want = {norm_path(p) for p in paths if norm_path(p)}
    if not want:
        return []
    stems = set()
    for p in want:
        base = p.rsplit("/", 1)[-1]
        stem = base.rsplit(".", 1)[0]
        if stem and not stem.startswith("test_"):
            stems.add(stem)
    out = []
    seen = set()
    for tgt, cur in index.targets.items():
        last = cur.get("last")
        if not last:
            continue
        rpaths = set(last.get("paths") or [])
        hit = bool(rpaths & want)
        if not hit:
            for rp in rpaths | set(paths_in_text(tgt)):
                base = rp.rsplit("/", 1)[-1].rsplit(".", 1)[0]
                if base.startswith("test_") and base[5:] in stems:
                    hit = True
                    break
                if base.endswith("_test") and base[:-5] in stems:
                    hit = True
                    break
        if hit:
            e = effective_last(cur) or last
            if e["obs_id"] not in seen:
                seen.add(e["obs_id"])
                out.append(e)
    return sorted(out, key=lambda r: (r["ts"], r["obs_id"]), reverse=True)
