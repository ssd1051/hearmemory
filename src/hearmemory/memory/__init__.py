"""Memory layer: derived project memory.

Entry points (interfaces.ENTRY_POINTS): MemoryBuilder / load_or_rebuild (build.py), recall (recall.py),
build_brief (brief.py), check (precommit.py). Everything here is a pure recompute from the raw .hearmemory files;
the only writes are state/memory.json (load_or_rebuild) and per-session presentation state
(state/sessions/<sid>.json, state/git_holds.json), always behind store.is_initialised()."""
from __future__ import annotations

__all__ = ["MemoryBuilder", "load_or_rebuild", "recall", "build_brief", "check"]


def __getattr__(name):          # lazy: importing hearmemory.memory stays cheap for hooks
    if name in ("MemoryBuilder", "load_or_rebuild"):
        from . import build
        return getattr(build, name)
    if name == "recall":
        from .recall import recall
        return recall
    if name == "build_brief":
        from .brief import build_brief
        return build_brief
    if name == "check":
        from .precommit import check
        return check
    raise AttributeError(name)
