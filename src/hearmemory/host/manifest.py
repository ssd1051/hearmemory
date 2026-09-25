"""InstallManifest persistence + the small generic helpers every host adapter's install()/
uninstall() share: atomic file writes, marker-block text insert/remove, and a tracked JSON merge
. Pure-ish: only touches files under the paths the caller gives it.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

from hearmemory import interfaces as I
from hearmemory.host import _deps


def manifest_path(root: Path) -> Path:
    return Path(root) / I.HEARMEMORY_DIRNAME / I.LAYOUT["manifest"]


def sha256_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_manifest(root: Path) -> "I.InstallManifest | None":
    p = manifest_path(root)
    if not p.exists():
        return None
    try:
        return I.InstallManifest.from_dict(json.loads(p.read_text(encoding="utf-8")))
    except Exception:
        return None


def write_manifest(root: Path, manifest: "I.InstallManifest") -> None:
    p = manifest_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    os.replace(tmp, p)


def is_within_real(path: Any, base: Any) -> bool:
    """True when `path`, with EVERY symlink along it resolved (including a dangling one and the
    last component itself), lies inside the real path of `base`. Uses os.path.realpath so a
    symlinked AGENTS.md / .cursor / .claude / .hearmemory pointing at a user-level location (e.g.
    ~/.cursor) is seen for what it is before anything is written through it."""
    try:
        real = os.path.realpath(os.fspath(path))
        real_base = os.path.realpath(os.fspath(base))
    except (OSError, ValueError, TypeError):
        return False
    if real == real_base:
        return True
    return real.startswith(real_base.rstrip(os.sep) + os.sep)


def target_ok(path: Path, bases: "Any", what: str = "") -> bool:
    """Scope guard every install() write goes through. `bases` is one directory or a
    sequence of them (the project root; for git files the repo git dir). Returns True when the
    real target path is inside one of them; otherwise warns on stderr and returns False so the
    caller SKIPS that file instead of writing through a symlink into user-level config."""
    if isinstance(bases, (str, os.PathLike)):
        bases = [bases]
    bases = [b for b in (bases or []) if b is not None]
    if any(is_within_real(path, b) for b in bases):
        return True
    try:
        real = os.path.realpath(os.fspath(path))
    except Exception:
        real = str(path)
    warn(f"skipped {what or path}: {path} resolves to {real}, which is outside "
         f"{', '.join(os.path.realpath(os.fspath(b)) for b in bases)} (a symlink?). hearmemory only "
         f"writes inside the project it was initialised in; nothing was written there.")
    return False


def rel_or_abs(p: Path, root: Path) -> str:
    """Project-relative posix path when `p` is inside `root`, else its absolute real path (never
    raises -- a relative_to() ValueError used to crash init half-way)."""
    try:
        return Path(os.path.realpath(p)).relative_to(os.path.realpath(root)).as_posix()
    except ValueError:
        return Path(os.path.realpath(p)).as_posix()


def write_file(path: Path, content: str, *, executable: bool = False) -> Tuple[bool, List[str]]:
    """Write `content` to `path`, creating parent dirs. Returns (created_new_file, created_parent_dirs)."""
    created_new = not path.exists()
    created_dirs: List[str] = []
    parent = path.parent
    to_make = []
    p = parent
    while not p.exists():
        to_make.append(p)
        p = p.parent
    for p in reversed(to_make):
        p.mkdir(exist_ok=False)
        created_dirs.append(str(p))
    path.write_text(content, encoding="utf-8")
    if executable:
        mode = path.stat().st_mode
        path.chmod(mode | 0o111)
    return created_new, created_dirs


def insert_marker_block(path: Path, block: str, begin: str, end: str) -> Tuple[bool, bool]:
    """Insert `block` (which itself starts with `begin` and ends with `end`, each on its own
    line) into `path`, replacing any existing hearmemory block found between the same markers.
    Returns (created_new_file, already_present_unchanged)."""
    created_new = not path.exists()
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if begin in existing and end in existing:
        pre, _, rest = existing.partition(begin)
        old_block_and_after = begin + rest
        _, _, after = old_block_and_after.partition(end)
        old_block = old_block_and_after[: len(old_block_and_after) - len(after)]
        if old_block.rstrip("\n") == block.rstrip("\n"):
            return created_new, True
        new_text = pre + block + after
    else:
        sep = "" if (not existing or existing.endswith("\n")) else "\n"
        new_text = existing + sep + ("\n" if existing else "") + block
    path.write_text(new_text, encoding="utf-8")
    return created_new, False


def remove_marker_block(path: Path, begin: str, end: str) -> bool:
    """Remove the hearmemory block from `path`. Returns True if the file is now empty (caller may want
    to delete it if hearmemory created it)."""
    if not path.exists():
        return True
    text = path.read_text(encoding="utf-8")
    if begin not in text or end not in text:
        return text.strip() == ""
    pre, _, rest = text.partition(begin)
    _, _, after = rest.partition(end)
    new_text = (pre.rstrip("\n") + ("\n" if pre.strip() else "") + after.lstrip("\n"))
    new_text = new_text if new_text.strip() else ""
    path.write_text(new_text, encoding="utf-8")
    return new_text.strip() == ""


HEARMEMORY_KEY = "hearmemory"
_HEARMEMORY_CMD_RE = re.compile(r"(?:^|[\s'\"])-m\s+hearmemory\s+--project(?:\s|$)")


def deep_merge_tracked(base: Dict[str, Any], add: Dict[str, Any], prefix: Tuple[str, ...] = ()) -> List[List[str]]:
    """Merge `add` into `base` in place; returns the LEAF key paths hearmemory added: e.g.
    ["mcpServers", "hearmemory"] and ["hooks", "PreToolUse"] -- never a whole container like ["mcpServers"]
    or ["hooks"], even when hearmemory creates the file, so that whatever the user later adds under those
    containers is never mistaken for hearmemory's. A dict is descended into (created empty when missing)
    except hearmemory's own named entry (`hearmemory`), which is recorded as one leaf. Existing keys are left
    untouched, so re-running install() is idempotent."""
    added: List[List[str]] = []
    for k, v in add.items():
        path = prefix + (k,)
        if k not in base:
            if isinstance(v, dict) and v and k != HEARMEMORY_KEY:
                base[k] = {}
                added.extend(deep_merge_tracked(base[k], v, path))
            else:
                base[k] = copy.deepcopy(v)
                added.append(list(path))
        elif isinstance(v, dict) and isinstance(base[k], dict) and k != HEARMEMORY_KEY:
            added.extend(deep_merge_tracked(base[k], v, path))
        # else: key already present -> leave the user's value alone.
    return added


def _strings(v: Any):
    if isinstance(v, str):
        yield v
    elif isinstance(v, dict):
        for x in v.values():
            yield from _strings(x)
    elif isinstance(v, (list, tuple)):
        for x in v:
            yield from _strings(x)


def is_hearmemory_entry(entry: Any) -> bool:
    """A hook / server entry hearmemory generated: its command runs `<python> -m hearmemory --project ...`
    (a hook command string) or its args are ["-m", "hearmemory", "--project", ...] (an MCP server)."""
    for s_ in _strings(entry):
        if _HEARMEMORY_CMD_RE.search(s_):
            return True

    def _args_lists(v: Any):
        if isinstance(v, list):
            if all(isinstance(x, str) for x in v):
                yield v
            for x in v:
                yield from _args_lists(x)
        elif isinstance(v, dict):
            for x in v.values():
                yield from _args_lists(x)

    for lst in _args_lists(entry):
        for i in range(len(lst) - 2):
            if lst[i] == "-m" and lst[i + 1] == "hearmemory" and lst[i + 2] == "--project":
                return True
    return False


def without_hearmemory(entries: List[Any]) -> List[Any]:
    """`entries` minus hearmemory's own. A Claude-style matcher entry
    ({"matcher": "Bash", "hooks": [...]}) is judged hook by hook: only hearmemory's hooks leave its
    `hooks` array -- a hook the user added to the same matcher entry stays -- and the entry itself goes
    only when no hook is left in it. Any other entry (a Cursor hook, an MCP server, ...) is judged whole."""
    kept: List[Any] = []
    for e in entries:
        hooks = e.get("hooks") if isinstance(e, dict) else None
        if isinstance(hooks, list):
            inner = [h for h in hooks if not is_hearmemory_entry(h)]
            if len(inner) == len(hooks):
                kept.append(e)
            elif inner:
                e2 = dict(e)
                e2["hooks"] = inner
                kept.append(e2)
            continue
        if not is_hearmemory_entry(e):
            kept.append(e)
    return kept


def _strip_hearmemory_in(d: Dict[str, Any]) -> None:
    """Legacy manifests recorded whole containers (["mcpServers"], ["hooks"]): remove only hearmemory's own
    entries inside them (the `hearmemory` server, hook entries running hearmemory), keep everything else."""
    if HEARMEMORY_KEY in d:
        del d[HEARMEMORY_KEY]
    for k in list(d):
        v = d[k]
        if isinstance(v, list):
            kept = without_hearmemory(v)
            if kept != v:
                if kept:
                    d[k] = kept
                else:
                    del d[k]
        elif isinstance(v, dict):
            _strip_hearmemory_in(v)
            if not v:
                del d[k]


def _node(base: Dict[str, Any], path: List[str]) -> Any:
    node: Any = base
    for part in path:
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def remove_json_keys(base: Dict[str, Any], key_paths: List[List[str]]) -> None:
    """Undo a tracked merge, in place. Per recorded path:
    - `...hearmemory` (hearmemory's named entry) -> deleted;
    - a list (a hook event hearmemory created) -> only hearmemory's own entries are removed -- inside a matcher
      entry only hearmemory's own hooks --; the key goes only when nothing else is left (a hook the
      user appended there, or added to hearmemory's matcher entry, survives);
    - a dict (legacy whole-container record) -> only hearmemory entries inside it are removed;
    - a scalar (e.g. Cursor's "version": 1) -> removed only when the file is otherwise an empty
      skeleton, since the user's own entries may depend on it.
    Containers left empty along those paths are pruned."""
    scalars: List[List[str]] = []
    for path in key_paths:
        if not path:
            continue
        parent = _node(base, list(path[:-1]))
        if not isinstance(parent, dict) or path[-1] not in parent:
            continue
        key = path[-1]
        val = parent[key]
        if key == HEARMEMORY_KEY:
            del parent[key]
        elif isinstance(val, list):
            kept = without_hearmemory(val)
            if kept:
                parent[key] = kept
            else:
                del parent[key]
        elif isinstance(val, dict):
            _strip_hearmemory_in(val)
            if not val:
                del parent[key]
        else:
            scalars.append(list(path))
    _prune_empty(base, key_paths)
    if scalars and is_empty_skeleton(base, scalars):
        for path in scalars:
            parent = _node(base, path[:-1])
            if isinstance(parent, dict):
                parent.pop(path[-1], None)
        _prune_empty(base, scalars)


def _without_empty(v: Any) -> Any:
    if isinstance(v, dict):
        out = {k: _without_empty(x) for k, x in v.items()}
        return {k: x for k, x in out.items() if x not in ({}, [])}
    if isinstance(v, list):
        return [x for x in (_without_empty(y) for y in v) if x not in ({}, [])]
    return v


def is_empty_skeleton(data: Dict[str, Any], ignore: List[List[str]] = ()) -> bool:
    """True when `data` holds nothing but empty containers (plus the `ignore` scalar paths), e.g.
    {"mcpServers": {}} or {"version": 1, "hooks": {}}: only then may uninstall delete a file hearmemory
    created."""
    d = copy.deepcopy(data)
    for path in ignore or ():
        parent = _node(d, list(path[:-1]))
        if isinstance(parent, dict):
            parent.pop(path[-1], None)
    return _without_empty(d) == {}


def _prune_empty(base: Dict[str, Any], key_paths: List[List[str]]) -> None:
    for path in key_paths:
        for depth in range(len(path) - 1, 0, -1):
            node = base
            ok = True
            for part in path[:depth]:
                if not isinstance(node, dict) or part not in node:
                    ok = False
                    break
                node = node[part]
            if ok and isinstance(node, dict) and not node:
                parent = base
                for part in path[: depth - 1]:
                    parent = parent[part]
                del parent[path[depth - 1]]


class UnparsableJSON(Exception):
    """A user-owned JSON config file exists but is not a plain JSON object (comments, trailing
    commas, a syntax error, or a non-object top level). hearmemory must never overwrite such a file."""


def read_json(path: Path) -> Dict[str, Any]:
    """{} for a missing file; the parsed object otherwise. Raises UnparsableJSON when the file
    exists but cannot be parsed as a JSON object -- callers must then leave it untouched (returning
    {} here used to make install()/uninstall() overwrite a user's hand-edited config)."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise UnparsableJSON(f"{path}: {type(e).__name__}: {e}") from e
    if not isinstance(data, dict):
        raise UnparsableJSON(f"{path}: top level is {type(data).__name__}, not an object")
    return data


def warn(msg: str) -> None:
    """Install-time warning for the user (stderr; install() has no other channel to the CLI)."""
    try:
        sys.stderr.write(f"hearmemory: warning: {msg}\n")
    except Exception:
        pass


def missing_parent_dirs(path: Path) -> List[str]:
    """Parent directories of `path` that do not exist yet (outermost first)."""
    out: List[str] = []
    p = path.parent
    while not p.exists():
        out.append(str(p))
        p = p.parent
    return list(reversed(out))


def write_json(path: Path, data: Dict[str, Any]) -> List[str]:
    """Write `data` as JSON, creating parent dirs. Returns the parent dirs it created (outermost
    first) so uninstall can remove them again."""
    created_dirs = missing_parent_dirs(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return created_dirs


def merge_json_file(path: Path, add: Dict[str, Any], root: Path, host: str) -> "I.InstallRecord | None":
    """Tracked merge of `add` into the JSON object file `path` (created when missing). Returns the
    json_merged InstallRecord, or None -- after warning the user -- when the existing file is not
    parseable JSON: such a file (e.g. JSONC with comments / trailing commas) is left byte-for-byte
    untouched and the user is told what to add by hand. A target whose real path is outside the
    project (symlinked `.cursor`/`.claude`/`.mcp.json`) is skipped the same way."""
    if not target_ok(path, Path(root), f"{host} config {path.name}"):
        return None
    created_file = not path.exists()
    try:
        base = read_json(path)
    except UnparsableJSON as e:
        warn(f"{e}. Left it untouched (not valid JSON, e.g. comments or trailing commas); "
             f"add these entries by hand if you want hearmemory there: {json.dumps(add, ensure_ascii=False)}")
        return None
    added = deep_merge_tracked(base, add)
    created_dirs: List[str] = []
    if added:
        created_dirs = write_json(path, base)
    rel = rel_or_abs(path, Path(root))
    return I.InstallRecord(path=rel, action="json_merged", host=host, json_keys=added,
                           created_file=created_file and bool(added), created_parent_dirs=created_dirs)


def new_manifest(root: Path, python: str) -> "I.InstallManifest":
    return I.InstallManifest(root=str(root), python=python, created_ts=_deps.now_ts(), records=[])
