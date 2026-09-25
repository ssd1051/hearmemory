"""hearmemory.provenance -- git-aware Provenance capture.

capture() shells out to `git` with a short timeout and a 2s per-process cache
(keyed by root) so a burst of hook calls does not spawn a git process each time.
Never raises: with no git repo (or on any error) branch/commit/dirty stay None.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

from .interfaces import Provenance

PathLike = Union[str, "Path"]

_CACHE_TTL_S = 2.0
_GIT_TIMEOUT_S = 0.3
_cache: Dict[str, Tuple[float, Tuple[Optional[str], Optional[str], Optional[bool]]]] = {}


def _run_git(root: PathLike, args: List[str]) -> Optional[str]:
    try:
        proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                              timeout=_GIT_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _git_status_lines(root: PathLike) -> Optional[List[str]]:
    out = _run_git(root, ["status", "--porcelain", "-uno"])
    if out is None:
        return None
    return [ln for ln in out.splitlines() if ln.strip()]


def git_info(root: PathLike) -> Tuple[Optional[str], Optional[str], Optional[bool]]:
    """(branch, commit, dirty) with a 2s per-root cache; (None, None, None) when there is no git repo."""
    key = str(Path(root))
    now = time.monotonic()
    cached = _cache.get(key)
    if cached is not None and now - cached[0] < _CACHE_TTL_S:
        return cached[1]
    branch = _run_git(root, ["rev-parse", "--abbrev-ref", "HEAD"])
    commit = _run_git(root, ["rev-parse", "HEAD"])
    dirty: Optional[bool] = None
    lines = _git_status_lines(root)
    if lines is not None:
        dirty = len(lines) > 0
    info = (branch, commit, dirty)
    _cache[key] = (now, info)
    return info


def commit_at(root: PathLike, ts: str) -> Optional[str]:
    """HEAD as of `ts` (imported/historical records): `git rev-list -1 --before=<ts> HEAD`."""
    return _run_git(root, ["rev-list", "-1", f"--before={ts}", "HEAD"])


def dirty_state(root: PathLike, limit: int = 100) -> Tuple[Optional[Dict[str, str]], bool]:
    """meta.dirty_state for `command` observations: {relpath: "size:mtime_ns"} of modified tracked
    files (from `git status --porcelain -uno`), capped at `limit`. (None, False) with no git repo."""
    lines = _git_status_lines(root)
    if lines is None:
        return None, False
    paths: List[str] = []
    for line in lines:
        if len(line) < 4:
            continue
        rel = line[3:].strip()
        if " -> " in rel:
            rel = rel.split(" -> ", 1)[1]
        rel = rel.strip('"')
        if rel:
            paths.append(rel)
    truncated = len(paths) > limit
    out: Dict[str, str] = {}
    for rel in paths[:limit]:
        try:
            st = (Path(root) / rel).stat()
        except OSError:
            continue
        out[rel] = f"{st.st_size}:{st.st_mtime_ns}"
    return out, truncated


def capture(root: PathLike, host: str, *, session_id: Optional[str] = None,
            subagent_id: Optional[str] = None, subagent_type: Optional[str] = None,
            agent_label: Optional[str] = None, model: Optional[str] = None,
            cwd: Optional[str] = None, source: Optional[str] = None,
            transcript_ref: Optional[str] = None, historical_ts: Optional[str] = None) -> Provenance:
    """Build a Provenance for `host`. `historical_ts` (imported records) resolves the commit
    that was HEAD at that time instead of the current HEAD."""
    branch = commit = None
    dirty: Optional[bool] = None
    try:
        if historical_ts:
            commit = commit_at(root, historical_ts)
        else:
            branch, commit, dirty = git_info(root)
    except Exception:
        pass
    return Provenance(host=host, session_id=session_id, subagent_id=subagent_id,
                       subagent_type=subagent_type, agent_label=agent_label, model=model,
                       git_branch=branch, git_commit=commit, git_dirty=dirty, cwd=cwd,
                       source=source, transcript_ref=transcript_ref)
