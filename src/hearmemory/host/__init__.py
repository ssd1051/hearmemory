"""host integration for Claude Code, Codex, Cursor and git. Owns everything under
src/hearmemory/host/. See docs/DESIGN.md (host adapters) and hearmemory.interfaces
ENTRY_POINTS for the contract every function here implements.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping

from hearmemory.host import claude as _claude
from hearmemory.host import codex as _codex
from hearmemory.host import cursor as _cursor
from hearmemory.host import git as _git


class _ModuleAdapter:
    """Adapts a host module's (normalize, handle_hook, install) functions to HostAdapterAPI."""

    def __init__(self, name: str, module) -> None:
        self.name = name
        self._module = module

    def install(self, root: Any, python: str, config: Mapping[str, Any]):
        return self._module.install(root, python, config)

    def normalize(self, event: str, payload: Mapping[str, Any]):
        return self._module.normalize(event, payload)

    def handle_hook(self, event: str, payload: Mapping[str, Any]):
        return self._module.handle_hook(event, payload)


ADAPTERS: Dict[str, _ModuleAdapter] = {
    "claude": _ModuleAdapter("claude", _claude),
    "codex": _ModuleAdapter("codex", _codex),
    "cursor": _ModuleAdapter("cursor", _cursor),
    "git": _ModuleAdapter("git", _git),
}
