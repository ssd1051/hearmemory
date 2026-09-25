"""Shared test helpers for the core's tests (tests/helpers.py is core-owned)."""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from hearmemory.interfaces import Observation, Provenance
from hearmemory.store import Store, create_store
from hearmemory.textutil import now_ts


def init_project(tmp_path: Path, git: bool = False) -> Store:
    """Create a fresh `.hearmemory/` under `tmp_path` (optionally a real git repo)."""
    if git:
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    return create_store(tmp_path)


def make_provenance(host: str = "cli", session_id: str = "s1", **kw) -> Provenance:
    return Provenance(host=host, session_id=session_id, source=kw.pop("source", "cli"), **kw)


def make_observation(kind: str = "note", text: str = "hello world", event_key: Optional[str] = None,
                     **kw) -> Observation:
    from hearmemory.interfaces import obs_id_for, sha256_text

    prov = kw.pop("provenance", None) or make_provenance()
    ek = event_key or f"cli:{text}:{id(text)}"
    return Observation(id=obs_id_for(ek), ts=now_ts(), kind=kind, event_key=ek, provenance=prov,
                       text=text, text_sha256=sha256_text(text), **kw)
