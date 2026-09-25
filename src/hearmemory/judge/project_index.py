"""ProjectIndex: the grounding lexicon of "what counts as an object" in this project.

Built from the project itself (git ls-files, light regex over code, a few manifest formats) and cached in
.hearmemory/state/index.json keyed by git HEAD + .git/index mtime. Only mentions that resolve here may become
A1 endpoints - this keeps field names, log words and ids from becoming bogus A1 questions.
"""
from __future__ import annotations

import json
import os
import posixpath
import re
import subprocess
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set

from hearmemory import interfaces as I
from hearmemory.judge import _compat as C
from hearmemory.judge._text import ident_tokens

INDEX_SCHEMA = "hearmemory.index/1"
MAX_FILES = 20000
MAX_SYMBOL_FILES = 2000
MAX_SYMBOL_FILE_BYTES = 256 * 1024
MAX_CONFIG_FILE_BYTES = 64 * 1024
BUILD_DEADLINE_S = 1.5
REBUILD_MIN_INTERVAL_S = 60.0
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "dist", "build", "__pycache__", ".hearmemory", ".mypy_cache",
             ".pytest_cache", ".tox", ".idea", ".vscode", "target", ".next", ".cache"}
CODE_EXTS = {".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rs", ".java", ".kt", ".kts",
             ".rb", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".swift", ".scala", ".php", ".sh", ".sql", ".proto"}
SYMBOL_STOPWORDS = {"main", "test", "tests", "init", "run", "get", "set", "setup", "teardown", "handler",
                    "helper", "helpers", "utils", "util", "self", "cls", "none", "true", "false", "data", "value",
                    "values", "item", "items", "name", "args", "kwargs", "config", "default", "load", "save",
                    "start", "stop", "close", "open", "read", "write", "update", "create", "delete", "call",
                    "func", "wrapper", "inner", "index", "build", "parse", "render", "result", "error", "errors",
                    "exception", "object", "type", "types", "base", "model", "models", "view", "views", "app",
                    "string", "number", "list", "dict", "file", "path", "time", "date", "info", "debug", "warn",
                    "__init__", "__main__", "__call__", "__repr__", "__str__", "constructor", "then", "this"}
_SYM_PY = [re.compile(r"^[ \t]*(?:async[ \t]+)?def[ \t]+([A-Za-z_]\w*)", re.M),
           re.compile(r"^[ \t]*class[ \t]+([A-Za-z_]\w*)", re.M)]
_SYM_JS = [re.compile(r"^[ \t]*(?:export[ \t]+)?(?:default[ \t]+)?(?:async[ \t]+)?function\*?[ \t]+([A-Za-z_$][\w$]*)", re.M),
           re.compile(r"^[ \t]*(?:export[ \t]+)?(?:default[ \t]+)?(?:abstract[ \t]+)?class[ \t]+([A-Za-z_$][\w$]*)", re.M),
           re.compile(r"^[ \t]*export[ \t]+(?:const|let|var|interface|type|enum)[ \t]+([A-Za-z_$][\w$]*)", re.M)]
_SYM_GO = [re.compile(r"^func[ \t]+(?:\([^)]*\)[ \t]*)?([A-Za-z_]\w*)", re.M),
           re.compile(r"^type[ \t]+([A-Za-z_]\w*)", re.M)]
_SYM_RS = [re.compile(r"^[ \t]*(?:pub(?:\([^)]*\))?[ \t]+)?(?:async[ \t]+)?(?:fn|struct|enum|trait)[ \t]+([A-Za-z_]\w*)", re.M)]
_SYM_JAVA = [re.compile(r"^[ \t]*(?:(?:public|private|protected|abstract|final|static|data|sealed|open|internal)[ \t]+)*"
                        r"(?:class|interface|object|enum)[ \t]+([A-Za-z_]\w*)", re.M)]
SYMBOL_RES: Mapping[str, List["re.Pattern[str]"]] = {
    ".py": _SYM_PY, ".pyi": _SYM_PY, ".js": _SYM_JS, ".jsx": _SYM_JS, ".ts": _SYM_JS, ".tsx": _SYM_JS,
    ".mjs": _SYM_JS, ".cjs": _SYM_JS, ".go": _SYM_GO, ".rs": _SYM_RS, ".java": _SYM_JAVA, ".kt": _SYM_JAVA,
    ".kts": _SYM_JAVA, ".scala": _SYM_JAVA,
}
_TEST_FILE_RE = re.compile(r"(?:^|/)(?:test_[^/]*\.py|[^/]*_test\.py|[^/]*\.(?:test|spec)\.[jt]sx?|[^/]*_test\.go)$")
_TEST_DIR_RE = re.compile(r"(?:^|/)tests?/")
_TEST_NAME_RES = [re.compile(r"^[ \t]*(?:async[ \t]+)?def[ \t]+(test_\w+)", re.M),
                  re.compile(r"^func[ \t]+(Test\w+)", re.M),
                  re.compile(r"""\b(?:it|test)\(\s*["']([^"'\n]{3,80})["']""")]
_MAKE_TARGET_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)[ \t]*:(?!=)", re.M)
_PROCFILE_RE = re.compile(r"^([A-Za-z0-9_-]+):", re.M)
_YAML_KEY_RE = re.compile(r"^([ ]*)(?:-[ ]+)?([A-Za-z_][\w.-]*)[ ]*:(?:[ ]|$)")
_ENV_EXAMPLE_RE = re.compile(r"^(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)[ \t]*=", re.M)
_CAMEL_INNER_RE = re.compile(r"[a-z][A-Z]")


def distinctive_key(key: str) -> bool:
    """a config key is kept only if it is >= 4 chars and contains _ . - or an inner camel hump."""
    return len(key) >= 4 and (any(ch in key for ch in "_.-") or bool(_CAMEL_INNER_RE.search(key)))


def is_test_file(rel: str) -> bool:
    return bool(_TEST_FILE_RE.search(rel)) or (bool(_TEST_DIR_RE.search(rel)) and rel.endswith(tuple(CODE_EXTS)))


def _module_of(rel: str) -> Optional[str]:
    stem, ext = posixpath.splitext(rel)
    if ext in (".py", ".pyi"):
        for pre in ("src/", "lib/"):
            if stem.startswith(pre):
                stem = stem[len(pre):]
                break
        parts = stem.split("/")
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if not parts or not all(re.match(r"^[A-Za-z_]\w*$", p) for p in parts):
            return None
        return ".".join(parts)
    if ext in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"):
        return stem
    return None


class ProjectIndex:
    """Grounding lexicon. `resolve(kind, surface)` returns the project paths / names a mention may denote
    (empty = ungrounded). Serialisable (to_dict / from_dict) for the state/index.json cache."""

    def __init__(self, root: str = "", files: Sequence[str] = (), symbols: Optional[Mapping[str, Sequence[str]]] = None,
                 tests: Optional[Mapping[str, str]] = None, services: Iterable[str] = (),
                 config_keys: Iterable[str] = (), partial: bool = False, key: str = "", built_epoch: float = 0.0,
                 project: str = "") -> None:
        self.root = str(root)
        self.project = project or (os.path.basename(self.root.rstrip("/")) if self.root else "")
        self._files: List[str] = sorted(set(files))
        self.symbols: Dict[str, List[str]] = {k: sorted(set(v)) for k, v in (symbols or {}).items()}
        self.tests: Dict[str, str] = dict(tests or {})              # test id "path::name" -> path
        self.services: Set[str] = set(services)
        self.config_keys: Set[str] = set(config_keys)
        self.partial = bool(partial)
        self.key = key
        self.built_epoch = float(built_epoch)
        self._derive()

    # -- derived lookup tables ------------------------------------------------------------------------
    def _derive(self) -> None:
        self.file_set: Set[str] = set(self._files)
        self.basenames: Dict[str, List[str]] = {}
        for f in self._files:
            self.basenames.setdefault(posixpath.basename(f), []).append(f)
        self.modules: Dict[str, str] = {}
        packages: Dict[str, str] = {}
        for f in self._files:
            mod = _module_of(f)
            if not mod:
                continue
            if f.endswith("__init__.py"):
                packages[mod] = f
            self.modules.setdefault(mod, f)
        for mod, f in packages.items():
            self.modules[mod] = f
        self.test_names: Dict[str, List[str]] = {}
        for tid in sorted(self.tests):
            self.test_names.setdefault(tid.split("::", 1)[-1], []).append(tid)
        self.test_files: Set[str] = {f for f in self._files if is_test_file(f)} | set(self.tests.values())
        # token index for the alias rule (mention rule 6)
        self.object_tokens: Dict[str, Set[str]] = {}
        for name in self.symbols:
            self.object_tokens["symbol:" + name] = ident_tokens(name)
        for s in self.services:
            self.object_tokens["service:" + s] = ident_tokens(s)
        for m, f in self.modules.items():
            if "." in m and not m.endswith("__init__"):
                self.object_tokens["path:" + f] = ident_tokens(m)
        self.token_objects: Dict[str, List[str]] = {}
        for norm in sorted(self.object_tokens):
            for t in self.object_tokens[norm]:
                self.token_objects.setdefault(t, []).append(norm)

    # -- ProjectIndexAPI ----------------------------------------------------------------------------------
    def files(self) -> Sequence[str]:
        return list(self._files)

    def resolve(self, kind: str, surface: str) -> List[str]:
        s = (surface or "").strip()
        if not s:
            return []
        if kind == "file":
            rel = C.norm_relpath(s, self.root)
            if not rel:
                return []
            if rel in self.file_set:
                return [rel]
            if "/" in rel:
                suf = "/" + rel
                return [f for f in self._files if f.endswith(suf)]
            return list(self.basenames.get(rel, []))
        if kind == "module":
            f = self.modules.get(s)
            return [f] if f else []
        if kind == "symbol":
            return list(self.symbols.get(s, []))
        if kind == "test":
            if s in self.tests:
                return [s]
            if "::" in s:
                path, name = s.split("::", 1)
                rel = C.norm_relpath(path, self.root) or path
                return [t for t in self.test_names.get(name.split("[", 1)[0], [])
                        if t.split("::", 1)[0] == rel or t.split("::", 1)[0].endswith("/" + rel)]
            return list(self.test_names.get(s, []))
        if kind == "service":
            return [s] if s in self.services else []
        if kind == "config_key":
            return [s] if s in self.config_keys else []
        return []

    def is_test_file(self, rel: str) -> bool:
        return rel in self.test_files

    # -- (de)serialisation ------------------------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {"schema": INDEX_SCHEMA, "root": self.root, "project": self.project, "key": self.key,
                "built_epoch": self.built_epoch, "partial": self.partial, "files": self._files,
                "symbols": self.symbols, "tests": self.tests, "services": sorted(self.services),
                "config_keys": sorted(self.config_keys)}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "ProjectIndex":
        return cls(root=d.get("root", ""), files=d.get("files") or [], symbols=d.get("symbols") or {},
                   tests=d.get("tests") or {}, services=d.get("services") or [],
                   config_keys=d.get("config_keys") or [], partial=bool(d.get("partial")), key=d.get("key", ""),
                   built_epoch=float(d.get("built_epoch") or 0.0), project=d.get("project", ""))

    # -- building -----------------------------------------------------------------------------------------
    @classmethod
    def build(cls, root: Any, cfg: Optional[Mapping[str, Any]] = None, deadline_s: float = BUILD_DEADLINE_S,
              clock: Optional[C.Clock] = None) -> "ProjectIndex":
        root = os.path.realpath(str(root))
        t_end = time.monotonic() + max(0.05, float(deadline_s))
        partial = False
        files = _list_files(root)
        if len(files) > MAX_FILES:
            files, partial = files[:MAX_FILES], True
        files = [f for f in files if not f.startswith(I.HEARMEMORY_DIRNAME + "/") and not C.is_excluded(f, cfg)]
        symbols: Dict[str, Set[str]] = {}
        tests: Dict[str, str] = {}
        services: Set[str] = set()
        config_keys: Set[str] = set()
        n_scanned = 0
        for rel in files:
            if time.monotonic() > t_end:
                partial = True
                break
            ext = posixpath.splitext(rel)[1].lower()
            base = posixpath.basename(rel)
            full = os.path.join(root, rel)
            if ext in CODE_EXTS and n_scanned < MAX_SYMBOL_FILES:
                src = _read_small(full, MAX_SYMBOL_FILE_BYTES)
                if src is None:
                    continue
                n_scanned += 1
                for rx in SYMBOL_RES.get(ext, ()):
                    for m in rx.finditer(src):
                        name = m.group(1)
                        if len(name) >= 4 and name.lower() not in SYMBOL_STOPWORDS and not name.startswith("__"):
                            symbols.setdefault(name, set()).add(rel)
                if is_test_file(rel):
                    for rx in _TEST_NAME_RES:
                        for m in rx.finditer(src):
                            tests[rel + "::" + m.group(1)] = rel
                if base == "Makefile":
                    pass
            if base.startswith("docker-compose") and ext in (".yml", ".yaml"):
                services |= _compose_services(_read_small(full, MAX_CONFIG_FILE_BYTES) or "")
            elif base == "package.json":
                services |= _package_json_services(_read_small(full, MAX_CONFIG_FILE_BYTES) or "")
            elif base in ("Makefile", "makefile", "GNUmakefile"):
                services |= {t for t in _MAKE_TARGET_RE.findall(_read_small(full, MAX_CONFIG_FILE_BYTES) or "")
                             if not t.startswith(".") and len(t) >= 3}
            elif base == "Procfile":
                services |= set(_PROCFILE_RE.findall(_read_small(full, MAX_CONFIG_FILE_BYTES) or ""))
            if base == "pyproject.toml":
                services |= _pyproject_scripts(_read_small(full, MAX_CONFIG_FILE_BYTES) or "")
            if base in (".env.example", ".env.sample"):
                config_keys |= {k for k in _ENV_EXAMPLE_RE.findall(_read_small(full, MAX_CONFIG_FILE_BYTES) or "")
                                if distinctive_key(k)}
            elif ext in (".toml", ".yaml", ".yml", ".json") and base != "package-lock.json":
                src = _read_small(full, MAX_CONFIG_FILE_BYTES)
                if src:
                    config_keys |= {k for k in _config_keys(ext, src) if distinctive_key(k)}
        return cls(root=root, files=files, symbols={k: sorted(v) for k, v in symbols.items()}, tests=tests,
                   services={s for s in services if len(s) >= 3}, config_keys=config_keys, partial=partial,
                   key=cache_key(root), built_epoch=(clock or time.time)())

    @classmethod
    def load_or_build(cls, store: Any, cfg: Optional[Mapping[str, Any]] = None, clock: Optional[C.Clock] = None,
                      deadline_s: float = BUILD_DEADLINE_S) -> "ProjectIndex":
        """Cached index (state/index.json); rebuilt when the git key changed, at most once a minute."""
        now = (clock or time.time)()
        root = os.path.realpath(str(getattr(store, "root", ".")))
        cached = None
        try:
            d = store.read_state("index")
            if d and d.get("schema") == INDEX_SCHEMA:
                cached = cls.from_dict(d)
        except Exception:
            cached = None
        key = cache_key(root)
        if cached is not None and (cached.key == key or now - cached.built_epoch < REBUILD_MIN_INTERVAL_S):
            return cached
        idx = cls.build(root, cfg, deadline_s=deadline_s, clock=clock)
        try:
            store.write_state("index", idx.to_dict())
        except Exception:
            pass
        return idx


# ---------------------------------------------------------------------------------------------------------
def _git(root: str, *args: str, timeout: float = 1.0) -> Optional[bytes]:
    try:
        p = subprocess.run(["git", "-C", root] + list(args), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           stdin=subprocess.DEVNULL, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


def cache_key(root: str) -> str:
    head = _git(root, "rev-parse", "HEAD", timeout=0.5)
    if head is not None:
        try:
            mt = os.stat(os.path.join(root, ".git", "index")).st_mtime_ns
        except OSError:
            mt = 0
        return "git:%s:%d" % (head.decode("ascii", "replace").strip(), mt)
    try:
        return "dir:%d" % os.stat(root).st_mtime_ns
    except OSError:
        return "dir:0"


def _list_files(root: str) -> List[str]:
    out = _git(root, "ls-files", "-co", "--exclude-standard", "-z", timeout=1.0)
    if out is not None:
        files = [f for f in out.decode("utf-8", "replace").split("\0") if f]
        return sorted({f for f in files if not any(p in SKIP_DIRS for p in f.split("/")[:-1])})
    files: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        rel_dir = os.path.relpath(dirpath, root)
        for fn in sorted(filenames):
            files.append(fn if rel_dir == "." else posixpath.join(rel_dir.replace(os.sep, "/"), fn))
            if len(files) > MAX_FILES:
                return files
    return files


def _read_small(path: str, limit: int) -> Optional[str]:
    try:
        if os.path.getsize(path) > limit:
            return None
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _compose_services(src: str) -> Set[str]:
    out: Set[str] = set()
    in_services = False
    indent = None
    for line in src.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if re.match(r"^services\s*:\s*$", line):
            in_services, indent = True, None
            continue
        if in_services:
            m = re.match(r"^(\s+)([A-Za-z0-9_.-]+)\s*:\s*$", line)
            lead = len(line) - len(line.lstrip())
            if lead == 0:
                in_services = False
                continue
            if m and (indent is None or lead == indent):
                indent = lead
                out.add(m.group(2))
    return out


def _package_json_services(src: str) -> Set[str]:
    try:
        d = json.loads(src)
    except ValueError:
        return set()
    out: Set[str] = set()
    if isinstance(d, dict):
        if isinstance(d.get("name"), str):
            out.add(d["name"].split("/")[-1])
        if isinstance(d.get("scripts"), dict):
            out |= {k for k in d["scripts"] if isinstance(k, str) and ":" not in k}
    return out


def _pyproject_scripts(src: str) -> Set[str]:
    try:
        import tomllib
        d = tomllib.loads(src)
    except Exception:
        return set()
    scripts = ((d.get("project") or {}).get("scripts") or {}) if isinstance(d, dict) else {}
    return {k for k in scripts if isinstance(k, str)}


def _nested_keys(obj: Any, prefix: str = "", depth: int = 0) -> Iterable[str]:
    if depth >= 3 or not isinstance(obj, dict):
        return
    for k, v in obj.items():
        if not isinstance(k, str):
            continue
        full = prefix + "." + k if prefix else k
        yield k
        if prefix:
            yield full
        yield from _nested_keys(v, full, depth + 1)


def _config_keys(ext: str, src: str) -> Set[str]:
    if ext == ".json":
        try:
            return set(_nested_keys(json.loads(src)))
        except ValueError:
            return set()
    if ext == ".toml":
        try:
            import tomllib
            return set(_nested_keys(tomllib.loads(src)))
        except Exception:
            return set()
    # yaml: indentation-based key scan (no PyYAML dependency)
    out: Set[str] = set()
    stack: List[tuple] = []
    for line in src.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _YAML_KEY_RE.match(line)
        if not m:
            continue
        lead = len(m.group(1))
        while stack and stack[-1][0] >= lead:
            stack.pop()
        key = m.group(2)
        parents = [k for _, k in stack][-2:]
        out.add(key)
        if parents:
            out.add(".".join(parents + [key]))
        stack.append((lead, key))
    return out
