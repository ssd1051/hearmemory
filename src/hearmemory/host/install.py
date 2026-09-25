"""Top-level install()/uninstall() orchestrator. Delegates the actual file generation
to each host module (claude.py/codex.py/cursor.py/git.py) and owns only the parts that are common
to all of them: the install manifest, idempotent re-install, and undoing every recorded action in
reverse order on uninstall.

Never writes anything under $HOME (~/.claude, ~/.codex, ~/.cursor, global git config): every path
written by every host module is rooted at `root`, `root/.git`, or a project file, never at a
user-level location. host tests assert this with a temporary HOME.
"""
from __future__ import annotations

import copy
import dataclasses
import shutil
import sys
import time
from pathlib import Path
from typing import Any, List, Mapping, Sequence

from hearmemory import interfaces as I
from hearmemory.host import _deps, claude as _claude, codex as _codex, cursor as _cursor, git as _git
from hearmemory.host import manifest as M
from hearmemory.host import snippets as S


def _carry_forward(old_records: Sequence["I.InstallRecord"], new_records: List["I.InstallRecord"]
                   ) -> List["I.InstallRecord"]:
    """A repeated `hearmemory init` regenerates every file's content, but a json_merged/block_inserted
    record whose keys/markers already existed reports nothing NEW as added this time (idempotent
    idempotency in manifest.deep_merge_tracked). Carry the earlier record's bookkeeping forward so
    a later `uninstall` still knows what hearmemory originally added."""
    old_by_path: dict = {}
    for r in old_records:
        old_by_path.setdefault(r.path, []).append(r)
    merged = []
    for r in new_records:
        if r.action == "reused":
            # Re-init finds the dispatcher THIS project installed earlier (it carries the marker):
            # keep the original created/chained record, or uninstall would leave it behind.
            prior = next((o for o in old_by_path.get(r.path, [])
                          if o.host == r.host and o.action in ("created", "chained")), None)
            if prior is not None:
                merged.append(prior)
                continue
        old = next((o for o in old_by_path.get(r.path, []) if o.host == r.host and o.action == r.action), None)
        if old is not None:
            r = dataclasses.replace(
                r, json_keys=(r.json_keys or old.json_keys),
                created_file=(old.created_file or r.created_file),
                created_parent_dirs=sorted(set(r.created_parent_dirs) | set(old.created_parent_dirs)),
                backup_path=(r.backup_path or old.backup_path),
                sha256_after=(r.sha256_after if r.sha256_after is not None else old.sha256_after),
            )
        merged.append(r)
    return merged


def install(root: Any, hosts: Sequence[str], cfg: Mapping[str, Any], *, python: str | None = None,
           claude_persist: bool = False, force_hooks_path: bool = False, no_git_hook: bool = False,
           with_codex_hooks: bool = False, force: bool = False) -> "I.InstallManifest":
    """(root, hosts, cfg) -> InstallManifest (ENTRY_POINTS). The keyword-only options mirror the
    `hearmemory init` flags; CLI/MCP passes them through when present, and every one defaults to the
    plain 3-argument call. Assumes `.hearmemory/VERSION` already exists (hearmemory init creates it before
    calling this) -- this function itself never creates `.hearmemory/`."""
    root = Path(root)
    py = python or sys.executable
    # never generate hook commands / scripts for a path that cannot be quoted safely.
    S.check_safe_path(str(root), "project path")
    S.check_safe_path(str(py), "Python interpreter path")
    old = None if force else M.read_manifest(root)

    manifest = M.new_manifest(root, py)
    hosts = list(hosts)
    collected: List["I.InstallRecord"] = []

    def _save() -> None:
        # Written after EVERY host and again in `finally`: a host adapter that raises
        # half-way (or a skipped/odd target) must still leave an install_manifest.json listing
        # every file already written, so `hearmemory uninstall` can undo them.
        manifest.records = (_carry_forward(old.records, list(collected)) if old is not None
                            else list(collected))
        M.write_manifest(root, manifest)

    steps = []
    if "claude" in hosts:
        steps.append(lambda: _claude.install(root, py, cfg, persist=claude_persist, records=collected))
    if "codex" in hosts:
        steps.append(lambda: _codex.install(root, py, cfg, with_hooks=with_codex_hooks, records=collected))
    if "cursor" in hosts:
        steps.append(lambda: _cursor.install(root, py, cfg, records=collected))
    if "git" in hosts and not no_git_hook:
        steps.append(lambda: _git.install(root, py, cfg, force_hooks_path=force_hooks_path,
                                          no_git_hook=no_git_hook, records=collected))
    try:
        for step in steps:
            step()
            _save()
    finally:
        _save()
    return manifest


def uninstall(root: Any, purge: bool = False) -> List[str]:
    """(root, purge=False) -> List[str] notes (ENTRY_POINTS). Stop the worker FIRST, undo
    every recorded file in reverse order, delete the manifest, then (purge only) delete
    .hearmemory/VERSION, wait, and remove the whole .hearmemory/ tree so nothing can recreate it afterwards
    (only `hearmemory init` may create .hearmemory/)."""
    root = Path(root)
    notes: List[str] = []

    if _deps.stop_worker_fn is not None:
        try:
            stopped = _deps.stop_worker_fn(root, 3.0)
            notes.append(f"worker stop requested (stopped={stopped})")
        except Exception as e:  # pragma: no cover - defensive
            notes.append(f"worker stop raised {type(e).__name__}: {e}")
    else:
        notes.append("worker stop skipped (hearmemory.judge.worker not available)")

    manifest = M.read_manifest(root)
    if manifest is not None:
        notes.extend(_undo_records(root, manifest.records))
        try:
            M.manifest_path(root).unlink()
        except FileNotFoundError:
            pass
        notes.append("install_manifest.json removed")
    else:
        notes.append("no install_manifest.json found; nothing to undo")

    if purge:
        version_path = root / I.HEARMEMORY_DIRNAME / "VERSION"
        try:
            version_path.unlink()
        except FileNotFoundError:
            pass
        notes.append(".hearmemory/VERSION removed (writers stop touching .hearmemory from this point on)")
        time.sleep(1.0)
        hearmemory_dir = root / I.HEARMEMORY_DIRNAME
        shutil.rmtree(hearmemory_dir, ignore_errors=True)
        if hearmemory_dir.exists():
            shutil.rmtree(hearmemory_dir, ignore_errors=True)
        notes.append("purged" if not hearmemory_dir.exists() else "purge incomplete: .hearmemory/ still present")
    return notes


def _resolve_record_path(root: Path, rec: "I.InstallRecord") -> Path:
    p = Path(rec.path)
    return p if p.is_absolute() else (root / p)


def _archive(root: Path, p: Path) -> Path:
    ts = _deps.now_ts().replace(":", "").replace(".", "")
    dest_dir = root / I.HEARMEMORY_DIRNAME / "archive" / f"uninstall-{ts}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / p.name
    p.replace(dest)
    return dest


def _archive_copy(root: Path, p: Path, rel: str) -> "Path | None":
    """Copy `p` into .hearmemory/archive/uninstall-<ts>/<its project-relative path> BEFORE uninstall
    changes or deletes it (recoverable, never a silent loss). None when no safe copy could be
    made (then the caller does not touch the file)."""
    try:
        ts = _deps.now_ts().replace(":", "").replace(".", "")
        rel_p = Path(rel)
        if rel_p.is_absolute() or ".." in rel_p.parts:
            rel_p = Path(p.name)
        hearmemory_dir = root / I.HEARMEMORY_DIRNAME
        if not hearmemory_dir.is_dir() or not M.is_within_real(hearmemory_dir, root):
            return None
        dest = hearmemory_dir / "archive" / f"uninstall-{ts}" / rel_p
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not M.is_within_real(dest, root):
            return None
        shutil.copy2(p, dest)
        return dest
    except Exception:
        return None


def _undo_bases(root: Path, host: str) -> List[Path]:
    """Where an undo may touch files: the project; for git records also this repo's git
    dir and its (possibly --force-hooks-path) hooks dir."""
    bases: List[Path] = [root]
    if host == "git":
        for extra in (_git.git_common_dir(root), _git.hooks_dir(root)):
            if extra is not None:
                bases.append(extra)
    return bases


def _undo_records(root: Path, records: Sequence["I.InstallRecord"]) -> List[str]:
    notes: List[str] = []
    for rec in reversed(list(records)):
        p = _resolve_record_path(root, rec)
        if not any(M.is_within_real(p, b) for b in _undo_bases(root, rec.host)):
            # A path that (now) resolves outside the project -- e.g. a directory replaced by a
            # symlink after init -- is never modified or deleted through the link.
            notes.append(f"left {rec.path} untouched: it resolves outside the project")
            continue
        try:
            notes.append(_undo_one(root, rec, p))
        except Exception as e:  # pragma: no cover - defensive: uninstall must not crash
            notes.append(f"failed to undo {rec.path}: {type(e).__name__}: {e}")
    return notes


def _shared_dispatcher_still_needed(root: Path) -> bool:
    others = [w for w in _git.worktree_pre_commit_paths(root) if w.resolve() != root.resolve()]
    return bool(others)


def _remove_empty_dirs(dirs: Sequence[str]) -> None:
    """Remove directories hearmemory created, innermost first, only while they are empty."""
    for d in sorted(dirs, key=lambda x: len(Path(x).parts), reverse=True):
        dp = Path(d)
        try:
            if dp.is_dir() and not any(dp.iterdir()):
                dp.rmdir()
        except OSError:
            pass


def _undo_one(root: Path, rec: "I.InstallRecord", p: Path) -> str:
    if rec.action == "reused":
        return f"left shared git dispatcher untouched (reused): {rec.path}"

    if rec.action == "created":
        if rec.shared and _shared_dispatcher_still_needed(root):
            return f"kept shared git dispatcher (still used by another worktree): {rec.path}"
        if p.exists() and p.is_file():
            current_sha = M.sha256_of(p.read_text(encoding="utf-8", errors="replace"))
            if rec.sha256_after and current_sha == rec.sha256_after:
                p.unlink()
                _remove_empty_dirs(rec.created_parent_dirs)
                return f"removed {rec.path}"
            dest = _archive(root, p)
            return f"archived modified {rec.path} -> {dest}"
        return f"already absent: {rec.path}"

    if rec.action == "chained":
        if rec.shared and _shared_dispatcher_still_needed(root):
            return f"kept shared git dispatcher (chained; still used by another worktree): {rec.path}"
        if rec.backup_path:
            backup = _resolve_record_path(root, dataclasses.replace(rec, path=rec.backup_path))
            if backup.exists():
                backup.replace(p)
                return f"restored original {rec.path}"
        return f"no backup found for {rec.path}"

    if rec.action == "block_inserted":
        if p.exists() and S.BEGIN_MARKER in p.read_text(encoding="utf-8", errors="replace"):
            if _archive_copy(root, p, rec.path) is None:
                return f"left {rec.path} untouched (could not archive a copy first); remove the hearmemory block by hand"
        now_empty = M.remove_marker_block(p, S.BEGIN_MARKER, S.END_MARKER)
        if now_empty and rec.created_file and p.exists():
            p.unlink()
            return f"removed {rec.path} (hearmemory created it; now empty)"
        return f"removed hearmemory block from {rec.path}"

    if rec.action == "json_merged":
        if not p.exists():
            _remove_empty_dirs(rec.created_parent_dirs)
            return f"already absent: {rec.path}"
        try:
            data = M.read_json(p)
        except M.UnparsableJSON:
            # The user has since hand-edited it into something we cannot parse: never overwrite.
            return (f"left {rec.path} untouched (no longer valid JSON); remove hearmemory's entries "
                    f"{rec.json_keys} by hand")
        if not rec.json_keys:
            return f"no hearmemory keys recorded for {rec.path}; left untouched"
        # remove ONLY hearmemory's own leaf entries; delete the file only when hearmemory created it AND
        # nothing but an empty skeleton is left; archive a copy before any change.
        new = copy.deepcopy(data)
        M.remove_json_keys(new, rec.json_keys)
        delete = bool(rec.created_file) and M.is_empty_skeleton(new)
        if new == data and not delete:
            return f"no hearmemory entries left in {rec.path}; left untouched"
        dest = _archive_copy(root, p, rec.path)
        if dest is None:
            return (f"left {rec.path} untouched (could not archive a copy first); remove hearmemory's entries "
                    f"{rec.json_keys} by hand")
        if delete:
            p.unlink()
            _remove_empty_dirs(rec.created_parent_dirs)
            return f"removed {rec.path} (hearmemory created it; only an empty skeleton was left; copy in {dest})"
        M.write_json(p, new)
        return f"removed hearmemory entries from {rec.path} (other entries kept; previous copy in {dest})"

    if rec.action == "exclude_added":
        if p.exists():
            lines = p.read_text(encoding="utf-8").splitlines()
            lines = [l for l in lines if l.strip() != _git.EXCLUDE_LINE.strip()]
            p.write_text(("\n".join(lines) + "\n") if lines else "", encoding="utf-8")
        return f"removed exclude line from {rec.path}"

    return f"no undo rule for action={rec.action} path={rec.path}"
