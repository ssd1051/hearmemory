"""Shared test scaffolding for the host-adapter tests (not a test module itself).

Provides a minimal in-memory FakeStore (structurally satisfies hearmemory.interfaces.StoreAPI) and a
helper to stand up a throwaway project (.hearmemory/VERSION only, optionally a real git repo) so the host
tests run against the Protocols in hearmemory.interfaces, independently of hearmemory.store/hearmemory.config.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import hearmemory.interfaces as I  # noqa: E402


class FakeStore:
    """In-memory stand-in for hearmemory.store.Store, structurally matching StoreAPI."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.hearmemory_dir = self.root / I.HEARMEMORY_DIRNAME
        self._obs: List[I.Observation] = []
        self._claims: List[I.Claim] = []
        self._candidates: List[I.Candidate] = []
        self._judgments: List[I.Judgment] = []
        self._events: List[I.ControlEvent] = []
        self._state: Dict[str, Dict[str, Any]] = {}

    def append_observations(self, obs: Sequence[I.Observation]) -> List[str]:
        ids = []
        for o in obs:
            self._obs.append(o)
            ids.append(o.id)
        return ids

    def iter_observations(self, since_offset: int = 0) -> Iterator[Tuple[int, I.Observation]]:
        seen = set()
        for i, o in enumerate(self._obs):
            if i < since_offset or o.id in seen:
                continue
            seen.add(o.id)
            yield i, o

    def append_candidates(self, cands: Sequence[I.Candidate]) -> int:
        self._candidates.extend(cands)
        return len(cands)

    def iter_candidates(self) -> Iterator[I.Candidate]:
        yield from self._candidates

    def append_judgments(self, js: Sequence[I.Judgment]) -> int:
        self._judgments.extend(js)
        return len(js)

    def iter_judgments(self) -> Iterator[I.Judgment]:
        yield from self._judgments

    def append_claims(self, claims: Sequence[I.Claim]) -> int:
        self._claims.extend(claims)
        return len(claims)

    def iter_claims(self) -> Iterator[I.Claim]:
        yield from self._claims

    def append_events(self, evs: Sequence[I.ControlEvent]) -> int:
        self._events.extend(evs)
        return len(evs)

    def iter_events(self) -> Iterator[I.ControlEvent]:
        yield from self._events

    def read_state(self, name: str) -> Optional[Dict[str, Any]]:
        v = self._state.get(name)
        return dict(v) if v is not None else None

    def write_state(self, name: str, data: Dict[str, Any]) -> None:
        self._state[name] = dict(data)

    def fingerprint(self) -> str:
        return I.stable_id("fake-fp-", len(self._obs))

    def is_initialised(self) -> bool:
        return (self.hearmemory_dir / "VERSION").is_file()

    def read_observations_window(self, from_offset: int, max_bytes: int) -> Tuple[int, List[I.Observation]]:
        return from_offset, list(self._obs[from_offset:])


def make_project(tmp_path: Path, *, git: bool = False) -> Path:
    """A throwaway project with just .hearmemory/VERSION (what `hearmemory init` would have created before
    calling the host layer's install()); optionally a real git repo (needed for the git-hook tests)."""
    root = tmp_path / "proj"
    root.mkdir()
    if git:
        _git(root, "init", "-q")
        _git(root, "config", "user.email", "t@example.com")
        _git(root, "config", "user.name", "t")
        (root / "README.md").write_text("hello\n", encoding="utf-8")
        _git(root, "add", "README.md")
        _git(root, "commit", "-q", "-m", "init")
    (root / I.HEARMEMORY_DIRNAME).mkdir()
    (root / I.HEARMEMORY_DIRNAME / "VERSION").write_text(I.STORE_FORMAT + "\n", encoding="utf-8")
    return root


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True, check=True)


def load_fixture(name: str) -> str:
    return (REPO / "tests" / "fixtures" / name).read_text(encoding="utf-8")


def load_fixture_json(name: str) -> Dict[str, Any]:
    import json
    return json.loads(load_fixture(name))
