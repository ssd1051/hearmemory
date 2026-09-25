"""Relevance scoring for briefs and recall (path, mention and text overlap).

relevance = interfaces.brief_relevance(path_score, #shared distinctive identifiers): path score 1 (same file),
0.5 (same directory, never the repo root), 0 otherwise; identifiers saturate at 2; weights 0.6 / 0.4;
0 when neither a path nor an identifier is shared (absolute gate). Relevance only reads trusted metadata
(paths / identifiers of the agent context), never model reasoning."""
from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from typing import Any, FrozenSet, Iterable, List, Optional, Sequence, Tuple

from hearmemory.interfaces import AgentContext, Provenance, actor_key, brief_relevance

from .text import distinctive_identifiers

_FILE_EXT = (r"py|pyi|js|jsx|ts|tsx|mjs|cjs|go|rs|java|kt|kts|rb|c|h|cc|cpp|hpp|cs|swift|php|scala|sh|bash|"
             r"toml|yaml|yml|json|md|rst|cfg|ini|sql|txt|lock|html|css|scss|vue|svelte|proto|graphql|tf|mk")
_PATH_IN_TEXT = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)*[\w-][\w.-]*\.(?:" + _FILE_EXT + r"))(?![\w/-])")
_DIFF_PATH = re.compile(r"^(?:\+\+\+ b/|--- a/|diff --git a/)(\S+)", re.M)
MAX_CONTEXT_IDENTS = 400


def norm_path(p: Any) -> str:
    s = str(p or "").strip().strip("`'\"").replace("\\", "/")
    if "::" in s:                      # pytest selector: tests/x.py::test_y
        s = s.split("::", 1)[0]
    while s.startswith("./"):
        s = s[2:]
    return s.strip("/")


def _is_glob(p: str) -> bool:
    return any(ch in p for ch in "*?[")


def split_paths(paths: Iterable[Any]) -> Tuple[FrozenSet[str], FrozenSet[str]]:
    """(files, dirs). The repository root never counts as a shared directory."""
    files, dirs = set(), set()
    for raw in paths or ():
        p = norm_path(raw)
        if not p or p == ".":
            continue
        if _is_glob(p):
            parts = []
            for comp in p.split("/"):
                if _is_glob(comp):
                    break
                parts.append(comp)
            if parts:
                dirs.add("/".join(parts))
            continue
        files.add(p)
        d = posixpath.dirname(p)
        if d:
            dirs.add(d)
    return frozenset(files), frozenset(dirs)


def path_score(item_paths: Iterable[Any], context_paths: Iterable[Any]) -> float:
    """1.0 = an item path equals a context file, 0.5 = same directory, 0.0 otherwise."""
    i_files, i_dirs = split_paths(item_paths)
    c_files, c_dirs = split_paths(context_paths)
    if i_files & c_files:
        return 1.0
    if i_dirs & c_dirs:
        return 0.5
    return 0.0


def paths_in_text(text: str, limit: int = 30) -> List[str]:
    """Path-looking tokens with a code/config extension (and diff headers)."""
    out: List[str] = []
    for m in _DIFF_PATH.finditer(text or ""):
        p = norm_path(m.group(1))
        if p and p != "/dev/null" and p not in out:
            out.append(p)
    for m in _PATH_IN_TEXT.finditer(text or ""):
        p = norm_path(m.group(1))
        if p and p not in out and "://" not in p:
            out.append(p)
        if len(out) >= limit:
            break
    return out


def idents(text: str, limit: Optional[int] = None) -> FrozenSet[str]:
    return distinctive_identifiers(text or "", limit=limit)


@dataclass
class Ctx:
    """Resolved agent context for scoring."""
    paths: FrozenSet[str] = frozenset()
    idents: FrozenSet[str] = frozenset()
    actor: Optional[str] = None
    host: Optional[str] = None
    session_key: Optional[str] = None
    extra: dict = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not self.paths and not self.idents


def requester_actor(ctx: Optional[AgentContext]) -> Optional[str]:
    if ctx is None or not ctx.host:
        return None
    return actor_key(Provenance(host=ctx.host, session_id=ctx.session_id, subagent_id=ctx.subagent_id))


def session_key(ctx: Optional[AgentContext]) -> Optional[str]:
    if ctx is None or not ctx.session_id:
        return None
    key = str(ctx.session_id)
    if ctx.subagent_id:
        key += "__" + str(ctx.subagent_id)
    return re.sub(r"[^A-Za-z0-9_.-]", "_", key)[:120]


def make_ctx(ctx: Optional[AgentContext], extra_paths: Iterable[str] = (), extra_text: str = "") -> Ctx:
    paths = set()
    ids = set()
    if ctx is not None:
        paths |= {norm_path(p) for p in ctx.paths or () if norm_path(p)}
        ids |= {str(i).lower() for i in ctx.identifiers or () if i}
        if ctx.query_text:
            ids |= set(idents(ctx.query_text, limit=MAX_CONTEXT_IDENTS))
            paths |= set(paths_in_text(ctx.query_text))
    paths |= {norm_path(p) for p in extra_paths or () if norm_path(p)}
    if extra_text:
        ids |= set(idents(extra_text, limit=MAX_CONTEXT_IDENTS))
        paths |= set(paths_in_text(extra_text, limit=60))
    if len(ids) > MAX_CONTEXT_IDENTS:
        ids = set(sorted(ids)[:MAX_CONTEXT_IDENTS])
    return Ctx(paths=frozenset(paths), idents=frozenset(ids), actor=requester_actor(ctx),
               host=ctx.host if ctx else None, session_key=session_key(ctx))


def item_relevance(item_paths: Iterable[str], item_idents: Iterable[str], ctx: Ctx) -> float:
    if ctx.empty:
        return 0.0
    ps = path_score(item_paths, ctx.paths)
    shared = len(set(item_idents) & ctx.idents)
    return brief_relevance(ps, shared)


def item_idents_of(text: str, paths: Sequence[str] = (), mentions: Sequence[str] = ()) -> FrozenSet[str]:
    """Distinctive identifiers of an item: its text plus mention surfaces (norm tails)."""
    out = set(idents(text, limit=60))
    for m in mentions or ():
        tail = str(m).split(":", 1)[-1]
        base = tail.rsplit("/", 1)[-1]
        out |= set(idents(base))
    return frozenset(out)
