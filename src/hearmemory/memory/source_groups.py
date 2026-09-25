"""Code-computed source groups.

A source group is a set of observations that must NOT count as independent sources of each other.
Union-find, merge only (never split), so every rule can only LOWER independence -- conservative by
construction. Group id = "sg-" + smallest member obs id (deterministic, order independent).

Rules:
  (1) primary observations with identical content (text_sha256), or the same tool over the same single
      path with overlapping line ranges (missing range = whole file; file_read / search only);
  (2) an observation with each obs id it cites in `refs`;
  (3) an assertive observation restating a primary observation that the SAME actor had already seen:
      >= 2 shared anchor keys or character-trigram Jaccard >= 0.5. Actors are compared with
      interfaces.actors_may_coincide, so an unlinked proxy record ("claude:?") is treated as having seen
      every earlier primary output of that host (conservative direction).
      Anchor keys: file basenames ("b:<name>") of paths
      and path-looking tokens, test targets, and distinctive identifiers; one file mention = one key.
  (4) both ends of an A3 restates (both directions, same scope) edge or a covered_by mark (merge_pair).
"""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Any, Deque, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from hearmemory.interfaces import ASSERTIVE_OBS_KINDS, PRIMARY_OBS_KINDS, Observation, actors_may_coincide

from .relevance import norm_path, paths_in_text
from .text import char_trigrams, distinctive_identifiers, jaccard

ANCHOR_MIN_SHARED = 2
TRIGRAM_JACCARD_MIN = 0.5
ANCHOR_TEXT_CHARS = 1500
MAX_ANCHORS = 60
POSTING_SCAN = 200          # newest entries kept per (anchor, actor) posting list (bounded cost)
TRIGRAM_RECENT = 20         # newest primaries per actor / host compared by trigram Jaccard
_FILEISH = (".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".rb", ".toml", ".yaml", ".yml",
            ".json", ".md", ".cfg", ".ini", ".sh", ".sql", ".c", ".h", ".cpp", ".kt", ".php", ".swift")


def _line_range(meta: Mapping[str, Any]) -> Optional[Tuple[int, int]]:
    for key in ("line_range", "lines"):
        v = meta.get(key) if isinstance(meta, Mapping) else None
        if isinstance(v, (list, tuple)) and len(v) == 2 and all(isinstance(x, int) for x in v):
            return int(v[0]), int(v[1])
    return None


def _ranges_overlap(a: Optional[Tuple[int, int]], b: Optional[Tuple[int, int]]) -> bool:
    if a is None or b is None:
        return True
    return a[0] <= b[1] and b[0] <= a[1]


def anchor_keys(obs: Observation, extra_paths: Iterable[str] = (), extra_idents: Iterable[str] = ()) -> FrozenSet[str]:
    """Anchor keys of one observation: file basenames, test targets, distinctive identifiers."""
    keys: Set[str] = set()
    paths = list(obs.paths or []) + list(extra_paths or [])
    text = (obs.text or "")[:ANCHOR_TEXT_CHARS]
    paths += paths_in_text(text, limit=20)
    if obs.tool is not None:
        if obs.tool.test is not None:
            paths += list(obs.tool.test.failed_ids or [])
            if obs.tool.test.target:
                keys.add("t:" + obs.tool.test.target)
        if obs.tool.command:
            paths += paths_in_text(obs.tool.command, limit=10)
    for p in paths:
        base = norm_path(p).rsplit("/", 1)[-1]
        if base:
            keys.add("b:" + base.lower())
    for ident in list(distinctive_identifiers(text, limit=MAX_ANCHORS)) + [str(i).lower() for i in extra_idents or ()]:
        last = ident.rsplit("/", 1)[-1]
        if last.endswith(_FILEISH):
            keys.add("b:" + last)
        elif "::" in ident:
            keys.add("b:" + ident.split("::", 1)[0].rsplit("/", 1)[-1])
            keys.add(ident.split("::", 1)[1])
        else:
            keys.add(ident)
        if len(keys) >= MAX_ANCHORS:
            break
    return frozenset(keys)


class SourceGroups:
    """Union-find over obs ids; nothing is ever split."""

    def __init__(self) -> None:
        self._parent: Dict[str, str] = {}
        self.reasons: List[Tuple[str, str, str]] = []

    def add(self, oid: str) -> None:
        self._parent.setdefault(oid, oid)

    def _find(self, x: str) -> str:
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a: str, b: str, reason: str) -> bool:
        if a == b or a not in self._parent or b not in self._parent:
            return False
        ra, rb = self._find(a), self._find(b)
        if ra == rb:
            return False
        lo, hi = (ra, rb) if ra < rb else (rb, ra)
        self._parent[hi] = lo
        self.reasons.append((min(a, b), max(a, b), reason))
        return True

    merge_pair = union

    def group(self, oid: str) -> str:
        if oid not in self._parent:
            return "sg-" + oid
        return "sg-" + self._find(oid)

    def same_group(self, a: str, b: str) -> bool:
        return self.group(a) == self.group(b)

    def mapping(self) -> Dict[str, str]:
        """obs id -> group id, only for members of non-singleton groups (absent = own singleton)."""
        roots: Dict[str, List[str]] = defaultdict(list)
        for o in self._parent:
            roots[self._find(o)].append(o)
        out = {}
        for root, members in roots.items():
            if len(members) > 1:
                for m in members:
                    out[m] = "sg-" + root
        return dict(sorted(out.items()))


def compute_source_groups(observations: Sequence[Observation], actor_of: Mapping[str, str],
                          extra_anchors: Optional[Mapping[str, Tuple[Sequence[str], Sequence[str]]]] = None,
                          deadline_check=None) -> SourceGroups:
    """Rules (1)-(3) over observations sorted by (ts, id). `actor_of[obs_id]` = resolved actor (ActorMap);
    `extra_anchors[obs_id]` = (paths, identifiers) contributed by claims extracted from that observation."""
    sg = SourceGroups()
    by_sha: Dict[str, str] = {}
    by_tool_path: Dict[Tuple[str, str], List[Tuple[str, Optional[Tuple[int, int]]]]] = defaultdict(list)
    posting: Dict[str, Dict[str, Deque[str]]] = defaultdict(dict)     # anchor -> actor -> newest obs ids
    recent_by_host: Dict[str, Deque[Tuple[str, str]]] = defaultdict(lambda: deque(maxlen=TRIGRAM_RECENT * 4))
    whole_rep: Dict[Tuple[str, str], str] = {}
    texts: Dict[str, str] = {}
    tri_cache: Dict[str, FrozenSet[str]] = {}
    coincide: Dict[Tuple[str, str], bool] = {}
    ids = {o.id for o in observations}
    extra_anchors = extra_anchors or {}

    def may_coincide(a: str, b: str) -> bool:
        k = (a, b)
        v = coincide.get(k)
        if v is None:
            v = coincide[k] = actors_may_coincide(a, b)
        return v

    def tris(oid: str) -> FrozenSet[str]:
        t = tri_cache.get(oid)
        if t is None:
            t = tri_cache[oid] = char_trigrams(texts.get(oid, ""))
        return t

    for i, o in enumerate(observations):
        if deadline_check is not None and i % 500 == 0:
            deadline_check()
        oid = o.id
        if o.kind not in PRIMARY_OBS_KINDS and o.kind not in ASSERTIVE_OBS_KINDS:
            continue
        sg.add(oid)
        actor = actor_of.get(oid) or "?:?"
        if o.kind in PRIMARY_OBS_KINDS:
            if o.excluded or not (o.text or "").strip():
                continue
            if o.text_sha256:
                prev = by_sha.get(o.text_sha256)
                if prev is not None:
                    sg.union(prev, oid, "identical_content_sha256")
                else:
                    by_sha[o.text_sha256] = oid
            paths = [norm_path(p) for p in o.paths if norm_path(p)]
            if o.kind in ("file_read", "search") and o.tool is not None and len(paths) == 1:
                key = (o.tool.name, paths[0])
                rng = _line_range(o.meta or {})
                rep = whole_rep.get(key)
                if rep is not None:            # a whole-file entry overlaps everything: one union is enough
                    sg.union(rep, oid, "same_tool_same_path_overlap")
                elif rng is None:
                    for other, _orng in by_tool_path.pop(key, []):
                        sg.union(other, oid, "same_tool_same_path_overlap")
                    whole_rep[key] = oid
                else:
                    lst = by_tool_path[key]
                    for other, orng in lst:
                        if _ranges_overlap(rng, orng):
                            sg.union(other, oid, "same_tool_same_path_overlap")
                    lst.append((oid, rng))
                    if len(lst) > POSTING_SCAN:
                        del lst[0]
            for k in anchor_keys(o):
                per_actor = posting[k]
                dq = per_actor.get(actor)
                if dq is None:
                    dq = per_actor[actor] = deque(maxlen=POSTING_SCAN)
                dq.append(oid)
            texts[oid] = o.text
            recent_by_host[actor.split(":", 1)[0]].append((oid, actor))
            continue
        # assertive
        for r in o.refs or []:
            if r in ids:
                sg.add(r)
                sg.union(r, oid, "cites_ref")
        ex_paths, ex_idents = extra_anchors.get(oid, ((), ()))
        counts: Dict[str, int] = defaultdict(int)
        for k in anchor_keys(o, ex_paths, ex_idents):
            per_actor = posting.get(k)
            if not per_actor:
                continue
            for sactor, dq in per_actor.items():
                if may_coincide(actor, sactor):
                    for sid in dq:
                        counts[sid] += 1
        merged_roots = {sg._find(oid)}
        for sid, n in counts.items():
            if n >= ANCHOR_MIN_SHARED:
                r = sg._find(sid)
                if r not in merged_roots:
                    sg.union(sid, oid, "restates_seen_primary_anchors")
                    merged_roots = {sg._find(oid)}
        if not (o.text or "").strip():
            continue
        mine = char_trigrams(o.text)
        pool = [sid for sid, a in recent_by_host.get(actor.split(":", 1)[0], ()) if may_coincide(actor, a)]
        for sid in pool[-TRIGRAM_RECENT:]:
            if counts.get(sid, 0) >= ANCHOR_MIN_SHARED or sg._find(sid) in merged_roots:
                continue
            if jaccard(mine, tris(sid)) >= TRIGRAM_JACCARD_MIN:
                sg.union(sid, oid, "restates_seen_primary_trigram")
                merged_roots = {sg._find(oid)}
    return sg
