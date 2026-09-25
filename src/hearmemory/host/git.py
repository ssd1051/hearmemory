"""git pre-commit integration. Works for every agent (Claude runs `git commit` through
Bash, Codex/Cursor through their shell tools) because it is the one hook every host shares.

Key property: the hooks directory returned by `git rev-parse --git-path hooks` is SHARED by every
worktree of the same repository. We therefore install a project-agnostic "dispatcher" there (safe
for worktrees that never ran `hearmemory init`) and keep the real, per-project check script inside this
project's own `.hearmemory/host/git/pre-commit`.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any, List, Mapping, Optional

from hearmemory import interfaces as I
from hearmemory.host import _deps, manifest as M, snippets as S

EXCLUDE_LINE = ".hearmemory/"
GIT_COMMIT_CMD_RE = re.compile(
    r"(^|[;&|]\s*)git(\s+-[^\s]+(\s+[^\s-][^\s]*)?)*\s+commit(?![\w-])")
# (?![\w-]) instead of a plain \b: \b alone also matches inside
# "commit-graph" (a hyphen is already a word boundary), which is a different git subcommand.


def is_git_commit_command(command: Optional[str]) -> bool:
    return bool(GIT_COMMIT_CMD_RE.search(command or ""))


def _run(args: List[str], cwd: Path, timeout: float = 3.0) -> Optional[str]:
    try:
        out = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


def has_git(root: Path) -> bool:
    return _run(["git", "rev-parse", "--git-dir"], root) is not None


def toplevel(cwd: Path) -> Optional[Path]:
    out = _run(["git", "rev-parse", "--show-toplevel"], cwd)
    return Path(out).resolve() if out else None


def hooks_dir(root: Path) -> Optional[Path]:
    out = _run(["git", "rev-parse", "--git-path", "hooks"], root)
    if out is None:
        return None
    p = Path(out)
    return p if p.is_absolute() else (root / p)


def core_hooks_path(root: Path) -> Optional[str]:
    return _run(["git", "config", "--get", "core.hooksPath"], root)


def git_common_dir(root: Path) -> Optional[Path]:
    """The repo's COMMON git dir (same for every worktree; unlike `.git`, which is a plain
    directory in the main worktree but a `gitdir: <path>` pointer FILE in any other worktree).
    `info/exclude` lives here, so this must be used instead of assuming `root/.git` is a directory."""
    out = _run(["git", "rev-parse", "--git-common-dir"], root)
    if out is None:
        return None
    p = Path(out)
    return p if p.is_absolute() else (root / p)


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError, RuntimeError):
        return False


def hooks_dir_is_repo_private(root: Path, hdir: Optional[Path]) -> bool:
    """True when the hooks dir git will use lies inside this repo's own git dir (`<project>/.git`,
    or, for a linked worktree, the common git dir all its worktrees share). Anything else is a
    core.hooksPath redirect to a directory hearmemory does not own (user-level or tracked in the tree)."""
    if hdir is None:
        return False
    common = git_common_dir(Path(root)) or (Path(root) / ".git")
    return _is_within(hdir, common)


def worktree_pre_commit_paths(root: Path) -> List[Path]:
    """All worktrees of this repo that have their own `.hearmemory/host/git/pre-commit` (uninstall
    rule: the shared dispatcher must stay if any OTHER worktree still needs it)."""
    out = _run(["git", "worktree", "list", "--porcelain"], root)
    if out is None:
        return []
    found: List[Path] = []
    for line in out.splitlines():
        if line.startswith("worktree "):
            wt = Path(line[len("worktree "):].strip())
            candidate = wt / I.HEARMEMORY_DIRNAME / "host" / "git" / "pre-commit"
            if candidate.exists():
                found.append(wt)
    return found


def install(root: Path, python: str, cfg: Mapping[str, Any], *, force_hooks_path: bool = False,
            no_git_hook: bool = False, records: Optional[List["I.InstallRecord"]] = None
            ) -> List["I.InstallRecord"]:
    """Every write is scope-checked on its REAL path: the dispatcher, its backup and
    info/exclude must resolve inside this repo's git dir (or, with --force-hooks-path, inside the
    chosen hooks dir), and the per-project script inside the project. A symlinked hook or exclude
    file pointing elsewhere is skipped with a warning. `records` is appended to as files are
    written (see claude.install)."""
    root = Path(root)
    records = [] if records is None else records
    if no_git_hook or not has_git(root):
        return records
    hdir = hooks_dir(root)
    if hdir is None:
        return records
    if not force_hooks_path and not hooks_dir_is_repo_private(root, hdir):
        # Scope rule: hearmemory only ever writes git hooks into THIS repo's
        # own git dir (`<project>/.git/hooks`, or the common dir shared by its worktrees). A
        # core.hooksPath that points anywhere else -- a work-tree-tracked dir (husky et al.) or a
        # user-level dir from the global git config (e.g. ~/.githooks, shared by every repo of the
        # user) -- is left alone by default; `--force-hooks-path` overrides this guard.
        sys.stderr.write(
            f"hearmemory: git hook NOT installed: hooks dir {hdir} (core.hooksPath={core_hooks_path(root)!r}) "
            f"is outside this repository's git dir; hearmemory never writes user-level or shared hook "
            f"directories. Re-run `hearmemory init --force-hooks-path` to install there anyway.\n")
        return records

    common_dir = git_common_dir(root) or (root / ".git")
    hook_base = hdir if force_hooks_path else common_dir
    dispatcher = hdir / "pre-commit"
    orig_backup = hdir / "pre-commit.hearmemory-orig"
    dispatcher_script = S.git_dispatcher_script()
    if not (M.target_ok(dispatcher, hook_base, "git pre-commit dispatcher")
            and M.target_ok(orig_backup, hook_base, "git pre-commit backup")):
        return records
    if dispatcher.exists():
        existing = dispatcher.read_text(encoding="utf-8", errors="replace") if dispatcher.is_file() else ""
        if S.SH_BEGIN_MARKER in existing:
            action = "reused"
        else:
            if not orig_backup.exists():
                dispatcher.replace(orig_backup)
                orig_backup.chmod(orig_backup.stat().st_mode | 0o111)
            M.write_file(dispatcher, dispatcher_script, executable=True)
            action = "chained"
    else:
        M.write_file(dispatcher, dispatcher_script, executable=True)
        action = "created"
    records.append(I.InstallRecord(path=_relposix(dispatcher, root, allow_outside=True), action=action,
                                    host="git", marker="hearmemory",
                                    sha256_after=M.sha256_of(dispatcher_script) if action != "reused" else None,
                                    backup_path=_relposix(orig_backup, root, allow_outside=True)
                                    if action == "chained" else None,
                                    shared=True))

    proj_script = root / I.HEARMEMORY_DIRNAME / "host" / "git" / "pre-commit"
    if M.target_ok(proj_script, root, "git project pre-commit script"):
        content = S.git_project_pre_commit_script(str(root), python)
        created_new, created_dirs = M.write_file(proj_script, content, executable=True)
        records.append(I.InstallRecord(path=_relposix(proj_script, root, allow_outside=True),
                                        action="created", host="git",
                                        sha256_after=M.sha256_of(content), created_file=created_new,
                                        created_parent_dirs=created_dirs))

    exclude_path = common_dir / "info" / "exclude"
    if not M.target_ok(exclude_path, common_dir, "git info/exclude"):
        return records
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
    lines = existing.splitlines()
    if EXCLUDE_LINE not in [l.strip() for l in lines]:
        sep = "" if (not existing or existing.endswith("\n")) else "\n"
        exclude_path.write_text(existing + sep + EXCLUDE_LINE + "\n", encoding="utf-8")
    # Always report the record, even when the line was already there (idempotent re-install, or a
    # second worktree that shares this same common dir): uninstall must keep knowing about it.
    records.append(I.InstallRecord(path=_relposix(exclude_path, root, allow_outside=True),
                                   action="exclude_added", host="git"))
    return records


def _relposix(p: Path, root: Path, allow_outside: bool = False) -> str:
    try:
        return p.resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        if allow_outside:
            return p.resolve().as_posix()
        raise


def normalize(event: str, payload: Mapping[str, Any]) -> List["I.Observation"]:
    # git pre-commit never produces observations; it only runs the pre-commit check.
    return []


def _staged_diff(root: Path) -> tuple[str, List[str]]:
    diff = _run(["git", "diff", "--cached", "--unified=0", "--"], root, timeout=2.0) or ""
    names = _run(["git", "diff", "--cached", "--name-only"], root, timeout=2.0) or ""
    paths = [l.strip() for l in names.splitlines() if l.strip()]
    return diff, paths


def _staged_tree_hash(root: Path) -> Optional[str]:
    return _run(["git", "write-tree"], root, timeout=2.0)


def handle_hook(event: str, payload: Mapping[str, Any]) -> "I.HookResult":
    """Precommit flow. `payload` carries {"project": <root>} (set by run_hook); everything
    else about the commit is read from git itself (staged diff/paths), never from stdin."""
    root = Path(payload.get("project") or payload.get("root") or ".").resolve()
    if event != "pre-commit":
        return I.HookResult(exit_code=0)
    top = toplevel(root)
    if top is None or top != root.resolve():
        # A sibling worktree, or not a git repo at all: hearmemory must not read or write anything here.
        return I.HookResult(exit_code=0)

    cfg = _deps.load_config(root) if _deps.load_config else dict(I.DEFAULT_CONFIG)
    mode = ((cfg.get("precommit") or {}).get("git_mode", "warn"))
    if mode == "off":
        return I.HookResult(exit_code=0)

    DeadlineCls = _deps.Deadline or _deps.FallbackDeadline
    deadline = DeadlineCls("precommit", cfg.get("hooks", {}))

    diff_text, paths = _staged_diff(root)
    tree_hash = _staged_tree_hash(root)

    store = _deps.open_store(root) if _deps.open_store else None
    if store is not None:
        import_slice_ms = deadline.slice_ms("import_codex")
        if import_slice_ms > 0:
            # Bounded and best-effort: a slow/hanging import never makes the commit
            # wait past its budgeted slice; the check below runs on whatever is in the store by then.
            try:
                from hearmemory.host.codex import bounded_import
                bounded_import(store, cfg, import_slice_ms / 1000.0)
            except Exception:
                pass

    warnings_text = ""
    decision = "allow"
    if store is not None and _deps.load_memory is not None and _deps.mem_check is not None:
        try:
            state = _deps.load_memory(store, cfg, allow_rebuild=False)
            ctx = I.AgentContext(host="git", paths=paths)
            req = I.CheckRequest(context=ctx, action="git_commit", payload_text=diff_text[:8000],
                                  paths=paths, mode=mode)
            result = _deps.mem_check(state, store, req, cfg)
            warnings_text = result.text
            decision = result.decision
        except Exception:
            decision, warnings_text = "allow", ""
    # else: core/memory not available yet -> nothing to check against; behave as "allow" (never
    # block just because a dependency is missing).

    if decision == "allow" or mode == "warn":
        if warnings_text:
            return I.HookResult(exit_code=0, stderr=warnings_text)
        return I.HookResult(exit_code=0)

    if mode == "hold_once":
        holds = (store.read_state("git_holds") or {}) if store is not None else {}
        if tree_hash and holds.get(tree_hash):
            return I.HookResult(exit_code=0, stderr=warnings_text)
        if tree_hash and store is not None:
            holds[tree_hash] = _deps.now_ts()
            store.write_state("git_holds", holds)
        return I.HookResult(exit_code=1, stderr=warnings_text)

    if mode == "block" and decision == "block":
        return I.HookResult(exit_code=1, stderr=warnings_text)
    return I.HookResult(exit_code=0, stderr=warnings_text)
