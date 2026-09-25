"""hearmemory.store -- the .hearmemory/ append-only store.

Every raw file (observations/claims/candidates/judgments/events/ledger) is
append-only. append_* take a lock, write one line per record and release it --
O(1), no read, no scan, no dedupe. When the lock cannot be
acquired within the timeout the batch is written to spool/ instead (lock-free,
unique filename) and merged back in later by merge_spool(). All reads dedupe by
id, first occurrence wins, and tolerate a truncated trailing line or corrupt
JSON (skipped and counted for `doctor`).

No method here ever creates `.hearmemory` itself: every write checks that
`.hearmemory/VERSION` exists first, and sub-directories are created one level at a
time with a bare `os.mkdir` so a deleted `.hearmemory` cannot be silently revived.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple, Type, Union

from .interfaces import (HEARMEMORY_DIRNAME, LAYOUT, LOCK_TIMEOUT_S_DEFAULT, Candidate, ControlEvent,
                         Claim, Judgment, Observation, Record, STORE_FORMAT, sha256_text)
from .locks import file_lock

PathLike = Union[str, "Path"]

# raw-file key -> (dataclass, id field, lock name). Lock names follow interfaces.LOCK_NAMES,
# which abbreviates "observations" to "obs".
_RAW_SPEC: Dict[str, Tuple[Type[Record], str, str]] = {
    "observations": (Observation, "id", "obs"),
    "claims": (Claim, "claim_id", "claims"),
    "candidates": (Candidate, "candidate_id", "candidates"),
    "judgments": (Judgment, "judgment_id", "judgments"),
    "events": (ControlEvent, "id", "events"),
}


def _dumps(d: Mapping[str, Any]) -> str:
    return json.dumps(d, ensure_ascii=False, sort_keys=False, separators=(",", ":"))




class Store:
    """the core's implementation of `interfaces.StoreAPI`."""

    def __init__(self, root: PathLike) -> None:
        self.root = Path(root)
        self.hearmemory_dir = self.root / HEARMEMORY_DIRNAME
        self.last_read_stats: Dict[str, Dict[str, int]] = {}

    # -- initialisation state -------------------------------------------------
    def is_initialised(self) -> bool:
        return (self.hearmemory_dir / LAYOUT["version"]).is_file()

    def version(self) -> Optional[str]:
        try:
            return (self.hearmemory_dir / LAYOUT["version"]).read_text(encoding="utf-8").strip()
        except OSError:
            return None

    def version_recognised(self) -> bool:
        return self.version() == STORE_FORMAT

    # -- paths ------------------------------------------------------------------
    def _raw_path(self, key: str) -> Path:
        return self.hearmemory_dir / LAYOUT[key]

    def _lock_path(self, name: str) -> Path:
        return self.hearmemory_dir / LAYOUT["locks"] / f"{name}.lock"

    def _spool_dir(self) -> Path:
        return self.hearmemory_dir / LAYOUT["spool"]

    def _ensure_dir(self, rel_parts: Sequence[str]) -> Optional[Path]:
        """mkdir one level at a time under hearmemory_dir, only if .hearmemory/VERSION exists."""
        if not self.is_initialised():
            return None
        cur = self.hearmemory_dir
        for part in rel_parts:
            cur = cur / part
            if not cur.exists():
                try:
                    os.mkdir(cur)
                except FileExistsError:
                    pass
                except OSError:
                    return None
        return cur

    # -- append (O(1)) -----------------------------------------------------
    def _append(self, key: str, rows: Sequence[Mapping[str, Any]], *, fsync: bool = False) -> bool:
        if not rows:
            return True
        if not self.is_initialised():
            return False
        _, _, lock_name = _RAW_SPEC[key]
        path = self._raw_path(key)
        if key == "ledger":
            self._ensure_dir([Path(LAYOUT["ledger"]).parent.name])
        payload = "".join(_dumps(r) + "\n" for r in rows)
        self._ensure_dir(["locks"])
        lock_path = self._lock_path(lock_name)
        with file_lock(lock_path, LOCK_TIMEOUT_S_DEFAULT) as ok:
            if ok:
                try:
                    with open(path, "a", encoding="utf-8") as f:
                        f.write(payload)
                        if fsync:
                            f.flush()
                            os.fsync(f.fileno())
                    return True
                except OSError:
                    return False
        return self._spool_write(key, payload)

    def _spool_write(self, key: str, payload: str) -> bool:
        spool_dir = self._ensure_dir(["spool"])
        if spool_dir is None:
            return False
        fname = f"{key}-{os.getpid()}-{time.time_ns()}.jsonl"
        try:
            with open(spool_dir / fname, "w", encoding="utf-8") as f:
                f.write(payload)
            return True
        except OSError:
            return False

    def append_observations(self, obs: Sequence[Observation], *, fsync: bool = False) -> List[str]:
        self._append("observations", [o.to_dict() for o in obs], fsync=fsync)
        return [o.id for o in obs]

    def append_claims(self, claims: Sequence[Claim], *, fsync: bool = False) -> int:
        self._append("claims", [c.to_dict() for c in claims], fsync=fsync)
        return len(claims)

    def append_candidates(self, cands: Sequence[Candidate], *, fsync: bool = False) -> int:
        self._append("candidates", [c.to_dict() for c in cands], fsync=fsync)
        return len(cands)

    def append_judgments(self, js: Sequence[Judgment], *, fsync: bool = False) -> int:
        self._append("judgments", [j.to_dict() for j in js], fsync=fsync)
        return len(js)

    def append_events(self, evs: Sequence[ControlEvent], *, fsync: bool = False) -> int:
        self._append("events", [e.to_dict() for e in evs], fsync=fsync)
        return len(evs)

    # -- read (dedupe by id, first wins) -----------------------------------
    def _iter_raw(self, key: str, since_offset: int = 0) -> Iterator[Tuple[int, Any]]:
        cls, id_field, _ = _RAW_SPEC[key]
        path = self._raw_path(key)
        corrupt = 0
        duplicate = 0
        seen: set = set()
        if not path.exists():
            self.last_read_stats[key] = {"corrupt_lines": 0, "duplicate_ids": 0}
            return
        with open(path, "rb") as f:
            f.seek(max(0, since_offset))
            offset = max(0, since_offset)
            while True:
                line = f.readline()
                if not line:
                    break
                if not line.endswith(b"\n"):
                    break  # truncated trailing line: ignore, don't advance the offset past it
                offset += len(line)
                text = line.decode("utf-8", errors="replace")
                stripped = text.strip()
                if not stripped:
                    continue
                try:
                    d = json.loads(stripped)
                except (ValueError, TypeError):
                    corrupt += 1
                    continue
                rid = d.get(id_field) if isinstance(d, dict) else None
                if rid is not None:
                    if rid in seen:
                        duplicate += 1
                        continue
                    seen.add(rid)
                try:
                    obj = cls.from_dict(d)
                except Exception:
                    corrupt += 1
                    continue
                yield offset, obj
        self.last_read_stats[key] = {"corrupt_lines": corrupt, "duplicate_ids": duplicate}

    def iter_observations(self, since_offset: int = 0) -> Iterator[Tuple[int, Observation]]:
        for offset, obj in self._iter_raw("observations", since_offset):
            yield offset, obj

    def iter_claims(self) -> Iterator[Claim]:
        for _, obj in self._iter_raw("claims"):
            yield obj

    def iter_candidates(self) -> Iterator[Candidate]:
        for _, obj in self._iter_raw("candidates"):
            yield obj

    def iter_judgments(self) -> Iterator[Judgment]:
        for _, obj in self._iter_raw("judgments"):
            yield obj

    def iter_events(self) -> Iterator[ControlEvent]:
        for _, obj in self._iter_raw("events"):
            yield obj

    def read_observations_window(self, from_offset: int, max_bytes: int) -> Tuple[int, List[Observation]]:
        """Batch-local dedupe only: used by the extractor's incremental reads."""
        path = self._raw_path("observations")
        if not path.exists():
            return from_offset, []
        out: List[Observation] = []
        seen: set = set()
        offset = max(0, from_offset)
        consumed = 0
        with open(path, "rb") as f:
            f.seek(offset)
            while consumed < max_bytes:
                line = f.readline()
                if not line or not line.endswith(b"\n"):
                    break
                consumed += len(line)
                offset += len(line)
                stripped = line.decode("utf-8", errors="replace").strip()
                if not stripped:
                    continue
                try:
                    d = json.loads(stripped)
                except (ValueError, TypeError):
                    continue
                oid = d.get("id")
                if oid is not None:
                    if oid in seen:
                        continue
                    seen.add(oid)
                try:
                    out.append(Observation.from_dict(d))
                except Exception:
                    continue
        return offset, out

    # -- state (derived, rebuildable; atomic writes) -----------------------------
    def _state_path(self, name: str) -> Path:
        from .interfaces import STATE_FILES
        rel = STATE_FILES.get(name, f"state/{name}.json")
        return self.hearmemory_dir / rel

    def read_state(self, name: str) -> Optional[Dict[str, Any]]:
        path = self._state_path(name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def write_state(self, name: str, data: Mapping[str, Any]) -> None:
        path = self._state_path(name)
        rel_dir_parts = path.relative_to(self.hearmemory_dir).parent.parts
        if rel_dir_parts:
            self._ensure_dir(list(rel_dir_parts))
        if not self.is_initialised():
            return
        tmp = path.with_suffix(path.suffix + f".tmp-{os.getpid()}-{time.time_ns()}")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, sort_keys=True)
            os.replace(tmp, path)
        except OSError:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass

    # -- fingerprint -------------------------------------------------------
    def fingerprint(self) -> str:
        from .interfaces import RAW_FILES
        parts = []
        for key in RAW_FILES:
            path = self._raw_path(key)
            try:
                st = path.stat()
                parts.append(f"{key}:{st.st_size}:{st.st_mtime_ns}")
            except OSError:
                parts.append(f"{key}:0:0")
        return sha256_text("|".join(parts))

    # -- spool merge -------------------------------------------------------
    def merge_spool(self) -> Dict[str, int]:
        """Append every spool/*.jsonl file into its raw file (under that file's lock), then move
        it to archive/spool/. Returns {"merged": n, "skipped": n} (skipped = lock busy, try later)."""
        if not self.is_initialised():
            return {"merged": 0, "skipped": 0}
        spool_dir = self._spool_dir()
        if not spool_dir.exists():
            return {"merged": 0, "skipped": 0}
        merged = 0
        skipped = 0
        for path in sorted(spool_dir.glob("*.jsonl")):
            key = path.name.split("-", 1)[0]
            if key not in _RAW_SPEC:
                skipped += 1
                continue
            try:
                payload = path.read_text(encoding="utf-8")
            except OSError:
                skipped += 1
                continue
            _, _, lock_name = _RAW_SPEC[key]
            if key == "ledger":
                self._ensure_dir([Path(LAYOUT["ledger"]).parent.name])
            ok_written = False
            with file_lock(self._lock_path(lock_name), LOCK_TIMEOUT_S_DEFAULT) as ok:
                if ok:
                    try:
                        with open(self._raw_path(key), "a", encoding="utf-8") as f:
                            f.write(payload)
                        ok_written = True
                    except OSError:
                        ok_written = False
            if not ok_written:
                skipped += 1
                continue
            archive_dir = self._ensure_dir(["archive", "spool"])
            if archive_dir is not None:
                try:
                    os.replace(path, archive_dir / path.name)
                except OSError:
                    pass
            merged += 1
        return {"merged": merged, "skipped": skipped}


def _has_version(path: Path) -> bool:
    return (path / HEARMEMORY_DIRNAME / LAYOUT["version"]).is_file()


GITIGNORE_TEXT = "# generated by hearmemory: keep the local memory store out of git\n*\n"


def ensure_gitignore(hearmemory_dir: Path) -> None:
    """Write `.hearmemory/.gitignore` (`*`) when it is missing. Best-effort, never raises."""
    try:
        gi = Path(hearmemory_dir) / ".gitignore"
        if not gi.exists():
            gi.write_text(GITIGNORE_TEXT, encoding="utf-8")
    except OSError:
        pass


def create_store(root: PathLike) -> Store:
    """Create a brand-new `.hearmemory/` at `root` (the ONLY place that may create the directory
    itself): VERSION, config.toml (defaults) and the top-level layout directories.
    Idempotent: if `.hearmemory/VERSION` already exists, returns the existing Store unchanged."""
    from .config import write_default_config

    root_path = Path(root)
    store = Store(root_path)
    if store.is_initialised():
        ensure_gitignore(store.hearmemory_dir)  # older stores get it on the next init
        return store
    hearmemory_dir = store.hearmemory_dir
    hearmemory_dir.mkdir(parents=True, exist_ok=True)
    # `.hearmemory/` must never be staged/committed, whatever happens to the git hook install
    # (husky / global core.hooksPath / --no-git-hook / hosts without git). A `.gitignore` of `*`
    # inside the directory itself ignores the whole store (including this file) in every repo.
    ensure_gitignore(hearmemory_dir)
    for name in ("state", "archive", "host", "locks", "logs", "spool"):
        (hearmemory_dir / name).mkdir(exist_ok=True)
    (hearmemory_dir / "state" / "sessions").mkdir(exist_ok=True)
    (hearmemory_dir / "archive" / "spool").mkdir(exist_ok=True)
    ledger_dir = (hearmemory_dir / LAYOUT["ledger"]).parent
    ledger_dir.mkdir(exist_ok=True)
    # VERSION last: everything above must exist before the store counts as "initialised".
    write_default_config(root_path)
    (hearmemory_dir / LAYOUT["version"]).write_text(STORE_FORMAT + "\n", encoding="utf-8")
    # when this store was created -- the Codex importer skips sessions that ended before it
    from .textutil import now_ts
    store.write_state("init", {"init_ts": now_ts()})
    return store


def open_store(start: Optional[PathLike] = None, create: bool = False) -> Optional[Store]:
    """Find the nearest initialised `.hearmemory/` at or above `start` (default: cwd). With
    create=True and none found, return an *uninitialised* Store rooted at `start` for
    `hearmemory init` to populate; otherwise return None."""
    base = Path(start).resolve() if start else Path.cwd()
    cur = base
    while True:
        if _has_version(cur):
            return Store(cur)
        if cur.parent == cur:
            break
        cur = cur.parent
    if create:
        return Store(base)
    return None
