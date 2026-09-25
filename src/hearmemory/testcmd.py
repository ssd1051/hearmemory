"""hearmemory.testcmd -- the ONE canonical form of a test command.

Agents spell the same test run in many ways: `cd <project> && pytest tests/x.py`, `python -m pytest
-q tests/x.py`, `.venv/bin/pytest tests/x.py`, `timeout 60 pytest tests/x.py 2>&1 | tail -20`, Codex's
legacy `["bash","-lc","..."]`. Every place that keys test runs by their "target" (the run index behind
the brief / pre-commit check, the extractor's A2/B1 keys, RunnerSummary.target and backticked commands
in claims) goes through `canonical_target()` so these spellings meet under one key, and
`target_covers()` lets a wider passing run (the whole file, or a bare `pytest`) supersede an older
failure of a narrower target inside it (one test of that file).

only a command that REALLY ran the tests may cover anything. Runner invocations that run no
tests (`pytest --version/-h/--co/--fixtures/...`) are not test runs at all; a bare `pytest` is
placed in the directory it ran in (`cd tests/unit && pytest` -> `pytest tests/unit`; a directory
outside the project stays an absolute path and never matches a project target; an unknown one --
`cd $X`, `cd ~`, `cd -` -- makes the segment unplaceable, so it enters no index); unknown `--options`
are kept (so the target cannot cover others); and `exit_owned()` tells whether the WHOLE command's
exit status really is the test run's own (`pytest x; echo`, `pytest x || echo`, `pytest x | head`,
`pytest x &` are not -- only the runner's own summary counts then).

Pure string functions, stdlib only; never raises (any parse problem falls back to a conservative
whitespace normalisation).
"""
from __future__ import annotations

import functools
import os
import posixpath
import re
import shlex
import subprocess
import time
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

__all__ = ["canonical_target", "exit_owned", "is_test_command", "linked_worktrees", "placed_target", "project_relpath",
           "stored_target", "strip_worktree", "target_covers", "test_outcome", "MAX_TARGET_CHARS"]

MAX_TARGET_CHARS = 500

# A canonical segment (runner already unified, see _unify_runner) that runs tests.
_RUNNER_RE = re.compile(
    r"^(?:pytest|python -m (?:unittest|nose2?|tox|nox)|nosetests|nose2|tox|nox|jest|vitest|mocha|ava|karma|"
    r"go test|cargo (?:test|nextest)|(?:npm|yarn|pnpm|bun) (?:run )?test|make (?:test|check)|bun test|deno test|"
    r"rspec|phpunit|ctest|mvn test|gradlew? test|dotnet test|mix test|swift test)(?:\b|$)")
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
_SHELL_C_RE = re.compile(r"^-[a-z]*c[a-z]*$")
_RAW_SHELL_C_RE = re.compile(r"^\s*(?:\S*/)?(?:ba|z|da|k)?sh\s+-[a-z]*c[a-z]*\s+(.+)$", re.S)
_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PY_RE = re.compile(r"^python(?:\d+(?:\.\d+)?)?$")
_PYTEST_BIN_RE = re.compile(r"^(?:pytest|py\.test)(?:-?\d+(?:\.\d+)?)?$")
_REDIR_ALONE_RE = re.compile(r"^\d*(?:>>?|>&|&>>?|<|<<<?)$")
_REDIR_ATTACHED_RE = re.compile(r"^\d*(?:>>?|&>>?|<)\S+$|^\d*>&\d+-?$")
# cheap pre-filter (on the raw command): anything that can possibly be a test run
_ANY_RUNNER_RE = re.compile(
    r"(?:pytest|py\.test|unittest|nose|tox|nox|jest|vitest|mocha|\bava\b|karma|go\s+test|cargo\s+(?:test|nextest)|"
    r"\btest\b|\bcheck\b|rspec|phpunit|ctest)")

# pytest options: which ones change WHICH tests run (kept, with their value), which ones only change
# how the run is reported / scheduled (dropped, together with their value when they take one), and
# which ones make pytest run NO tests at all (such a command is not a test run). Any other
# `--option` is kept verbatim, so a target carrying it never covers a target without it.
_PYTEST_KEEP_VAL = {"-k", "-m", "--deselect", "--ignore", "--ignore-glob"}
_PYTEST_KEEP_FLAG = {"--lf", "--last-failed", "--sw", "--stepwise", "--sw-skip", "--stepwise-skip"}
_PYTEST_DROP_VAL = {"-n", "--numprocesses", "--maxfail", "-p", "--tb", "--durations", "--timeout", "-W",
                    "-c", "--rootdir", "--basetemp", "-o", "--override-ini", "--junitxml", "--junit-xml",
                    "--cov-report", "--cov-config", "--log-level", "--log-cli-level", "--log-file", "-r", "--dist",
                    "--color", "--capture", "--import-mode", "--confcutdir", "--html", "--reruns", "--count",
                    "--random-order-seed", "--durations-min", "--max-worker-restart", "--tx", "--cache-clear",
                    "--cov-fail-under", "--reruns-delay", "--code-highlight", "--show-capture",
                    "--log-format", "--log-date-format", "--log-cli-format", "--log-file-level", "--junit-prefix"}
_PYTEST_NO_VALUE = {"--cache-clear"}
_PYTEST_DROP_FLAG = {"--cov", "--no-cov", "--cov-append", "--cov-branch", "--quiet", "--verbose", "--exitfirst",
                     "--showlocals", "--full-trace", "--strict-markers", "--strict-config", "--strict",
                     "--disable-warnings", "--disable-pytest-warnings", "--no-header", "--no-summary",
                     "--ff", "--failed-first", "--nf", "--new-first", "--runxfail", "--pdb", "--trace",
                     "--forked", "--benchmark-disable", "--benchmark-skip", "--color=yes", "--color=no",
                     "--force-sugar", "--no-cov-on-fail", "--random-order", "--pyargs", "--doctest-modules",
                     "--last-failed-no-failures", "--lfnf", "--setup-show", "--no-print-logs", "--cache-clear"}
_PYTEST_INFO_ONLY = {"--version", "-V", "-VV", "-h", "--help", "--co", "--collect-only", "--collectonly",
                     "--fixtures", "--funcargs", "--fixtures-per-test", "--markers", "--setup-plan",
                     "--setup-only", "--cache-show", "--trace-config-only"}
# other runners: invocations that list / build / describe instead of running the tests
_GENERIC_INFO_ONLY = {"--help", "-h", "--version", "--no-run", "--listTests", "--list-tests", "--list", "-list",
                      "--showConfig", "--collect-only"}
# generic noise flags (any runner)
_NOISE_FLAGS = {"-q", "-qq", "-v", "-vv", "-vvv", "-x", "-s", "--quiet", "--verbose", "-rA", "--no-header",
                "--color=no", "--color=yes", "--tb=short", "--tb=no", "--tb=line", "--tb=long", "--tb=auto"}


# --------------------------------------------------------------------------- splitting
def _split_ops(cmd: str, keep_empty: bool = False) -> List[Tuple[str, Optional[str]]]:
    """Quote-aware split on the shell list/pipe operators && || ; | & and newlines.
    Returns [(segment, operator_before_it)]. `2>&1` / `&>file` are redirections, not operators.
    `keep_empty` keeps empty segments, so a trailing `&` (backgrounded) is still visible."""
    segs: List[Tuple[str, Optional[str]]] = []
    buf: List[str] = []
    quote: Optional[str] = None
    op_before: Optional[str] = None
    i, n = 0, len(cmd)

    def flush(op: str) -> None:
        nonlocal buf, op_before
        segs.append(("".join(buf), op_before))
        buf = []
        op_before = op

    while i < n:
        c = cmd[i]
        if quote:
            buf.append(c)
            if c == "\\" and quote == '"' and i + 1 < n:
                buf.append(cmd[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            buf.append(c)
            buf.append(cmd[i + 1])
            i += 2
            continue
        if c in "'\"":
            quote = c
            buf.append(c)
            i += 1
            continue
        two = cmd[i:i + 2]
        if two in ("&&", "||"):
            flush(two)
            i += 2
            continue
        if c in ";\n":
            flush(";")
            i += 1
            continue
        if c == "|":
            flush("|")
            i += 1
            continue
        if c == "&":
            prev = buf[-1] if buf else ""
            nxt = cmd[i + 1] if i + 1 < n else ""
            if prev in ("<", ">") or nxt == ">":
                buf.append(c)
                i += 1
                continue
            flush("&")
            i += 1
            continue
        buf.append(c)
        i += 1
    segs.append(("".join(buf), op_before))
    return [(s.strip(), op) for s, op in segs if keep_empty or s.strip()]


def _tokens(segment: str) -> List[str]:
    try:
        return shlex.split(segment)
    except ValueError:
        return segment.split()


def _strip_redirections(toks: List[str]) -> List[str]:
    out: List[str] = []
    skip = False
    for t in toks:
        if skip:
            skip = False
            continue
        if _REDIR_ALONE_RE.match(t):
            skip = True
            continue
        if _REDIR_ATTACHED_RE.match(t):
            continue
        out.append(t)
    return out


def _unwrap_shell(cmd: str, depth: int = 0) -> str:
    """`bash -lc '<script>'` / `sh -c "<script>"` -> <script> (also Codex's legacy argv joined with
    plain spaces, `bash -lc cd x && pytest`, whose script part is unquoted)."""
    if depth > 3:
        return cmd
    toks = _tokens(cmd)
    if len(toks) >= 3 and posixpath.basename(toks[0]) in _SHELLS and _SHELL_C_RE.match(toks[1]):
        m = _RAW_SHELL_C_RE.match(cmd)
        rest = m.group(1).lstrip() if m else ""
        if rest[:1] in ("'", '"'):
            return _unwrap_shell(toks[2], depth + 1)
        if rest:
            return _unwrap_shell(rest, depth + 1)
    return cmd


def _strip_prefixes(toks: List[str]) -> List[str]:
    """Drop env assignments and wrappers that do not change what runs: `FOO=1`, `env [-u X] A=1`,
    `timeout [-s SIG] 60`, `time`, `nice [-n N]`, `nohup`, `command`, `exec`, `uv|poetry|pdm|pipenv|hatch
    run`, `npx`, `xvfb-run`."""
    for _ in range(12):
        if not toks:
            return toks
        t0 = toks[0]
        b = posixpath.basename(t0)
        if _ASSIGN_RE.match(t0):
            toks = toks[1:]
            continue
        if b == "env":
            i = 1
            while i < len(toks) and (toks[i].startswith("-") or _ASSIGN_RE.match(toks[i])):
                i += 2 if toks[i] in ("-u", "--unset", "-C", "--chdir", "-S") else 1
            toks = toks[i:]
            continue
        if b == "timeout":
            i = 1
            while i < len(toks) and toks[i].startswith("-"):
                i += 2 if toks[i] in ("-s", "--signal", "-k", "--kill-after") else 1
            toks = toks[i + 1:]           # the duration
            continue
        if b in ("time", "nohup", "command", "exec", "builtin", "unbuffer"):
            i = 1
            while i < len(toks) and toks[i].startswith("-"):
                i += 1
            toks = toks[i:]
            continue
        if b == "nice":
            i = 1
            if i < len(toks) and toks[i] in ("-n", "--adjustment"):
                i += 2
            elif i < len(toks) and re.fullmatch(r"-\d+|--adjustment=\S+", toks[i]):
                i += 1
            toks = toks[i:]
            continue
        if b == "stdbuf":
            i = 1
            while i < len(toks) and toks[i].startswith("-"):
                i += 1
            toks = toks[i:]
            continue
        if b in ("uv", "poetry", "pdm", "pipenv", "hatch", "rye") and len(toks) > 1 and toks[1] == "run":
            i = 2
            while i < len(toks) and toks[i].startswith("-"):
                i += 2 if toks[i] in ("--with", "--python", "-p", "--extra", "--group", "--package", "--project",
                                      "--directory", "--env-file", "--env", "-e") else 1
            toks = toks[i:]
            continue
        if b in ("npx", "bunx", "pnpx", "xvfb-run"):
            i = 1
            while i < len(toks) and toks[i].startswith("-"):
                i += 1
            toks = toks[i:]
            continue
        return toks
    return toks


def _unify_runner(toks: List[str]) -> List[str]:
    """`python3.11 -m pytest`, `/x/.venv/bin/python -m pytest`, `.venv/bin/pytest`, `py.test` -> `pytest`;
    `/x/bin/python -m unittest` -> `python -m unittest`; `node_modules/.bin/jest` -> `jest`."""
    if not toks:
        return toks
    b = posixpath.basename(toks[0])
    if _PY_RE.match(b):
        i = 1
        while i < len(toks) and toks[i].startswith("-") and toks[i] != "-m":
            i += 2 if toks[i] in ("-X", "-W") else 1
        if i + 1 < len(toks) and toks[i] == "-m":
            mod = toks[i + 1]
            if mod in ("pytest", "py.test"):
                return ["pytest"] + toks[i + 2:]
            return ["python", "-m", mod] + toks[i + 2:]
        return ["python"] + toks[1:]
    if _PYTEST_BIN_RE.match(b):
        return ["pytest"] + toks[1:]
    return [b] + toks[1:]


# --------------------------------------------------------------------------- worktrees
# Claude Code desktop runs a session in a linked git worktree of the SAME repository, under
# <root>/.claude/worktrees/<name>/ (same files, same HEAD). A cwd or a path in there is the project's own
# path: `<root>/.claude/worktrees/w/tests/x.py` is `tests/x.py`, and a bare `pytest` run at the worktree
# root is the whole suite (it used to become the target `pytest .claude/worktrees/w`, which matched no
# other run). The prefix rule below is pure (hermetic tests); linked worktrees anywhere else are found
# with `git worktree list --porcelain` (cached, short timeout, () without git).
_WORKTREE_REL_RE = re.compile(r"^\.claude/worktrees/[^/]+(?:/|$)")
_WORKTREE_ANY_RE = re.compile(r"/\.claude/worktrees/[^/]+(?:/|$)")
_WORKTREE_TTL_S = 30.0
_worktree_cache: Dict[str, Tuple[float, Tuple[str, ...]]] = {}


def strip_worktree(rel: str) -> str:
    """Project-relative path `rel` with a leading `.claude/worktrees/<name>/` removed ("" = the worktree
    root = the project root). Anything else is returned unchanged."""
    m = _WORKTREE_REL_RE.match(rel or "")
    return rel[m.end():].rstrip("/") if m else rel


def linked_worktrees(root: Optional[str]) -> Tuple[str, ...]:
    """Absolute paths of the OTHER worktrees of the git repository at `root` (never `root` itself).
    Cached per root for a few seconds; () when git is missing, slow or `root` is not a repository."""
    if not root:
        return ()
    now = time.monotonic()
    hit = _worktree_cache.get(root)
    if hit is not None and now - hit[0] < _WORKTREE_TTL_S:
        return hit[1]
    out: List[str] = []
    try:
        r = subprocess.run(["git", "-C", root, "worktree", "list", "--porcelain"], capture_output=True, text=True,
                           timeout=0.5)
        if r.returncode == 0:
            me = {posixpath.normpath(root), os.path.realpath(root)}
            for line in r.stdout.splitlines():
                if line.startswith("worktree "):
                    p = posixpath.normpath(line[len("worktree "):].strip())
                    if p and p not in me and os.path.realpath(p) not in me:
                        out.append(p)
    except Exception:
        out = []
    res = tuple(out)
    _worktree_cache[root] = (now, res)
    return res


def _under(p: str, r: str) -> Optional[str]:
    if p == r:
        return ""
    if p.startswith(r.rstrip("/") + "/"):
        return p[len(r.rstrip("/")) + 1:]
    return None


# --------------------------------------------------------------------------- paths
def _inside(path: str, root: str) -> Optional[str]:
    """Project-relative posix form of absolute `path` when it lies inside `root` -- or inside a linked
    git worktree of the same repository --, else None."""
    for p, r in ((posixpath.normpath(path), posixpath.normpath(root)),
                 (os.path.realpath(path), os.path.realpath(root))):
        rel = _under(p, r)
        if rel is not None:
            return strip_worktree(rel)
    for wt in linked_worktrees(root):
        for p, r in ((posixpath.normpath(path), wt), (os.path.realpath(path), os.path.realpath(wt))):
            rel = _under(p, r)
            if rel is not None:
                return rel
    return None


def project_relpath(path: str, root: Optional[str]) -> Optional[str]:
    """Project-relative posix path of a file path an agent used (absolute, or relative to the project
    root), mapping linked worktrees of the repository onto the project. None when the
    path lies outside the project. Without a root only the pure worktree-prefix rule applies."""
    if not path:
        return None
    p = path.replace("\\", "/")
    if posixpath.isabs(p):
        if root:
            rel = _inside(p, root)
            return None if rel is None else (rel or ".")
        m = _WORKTREE_ANY_RE.search(p)
        return (p[m.end():].rstrip("/") or ".") if m else None
    rel = strip_worktree(posixpath.normpath(p))
    return rel or "."


def _start_dir(root: Optional[str], cwd: Optional[str]) -> Optional[str]:
    """Where the command starts. With a known root this is ABSOLUTE; without one it is relative to
    the project root ("" = the root; an absolute cwd is then assumed to be the project itself)."""
    if root:
        if cwd:
            return posixpath.normpath(cwd if posixpath.isabs(cwd) else posixpath.join(root, cwd))
        return root
    if cwd and not posixpath.isabs(cwd):
        c = strip_worktree(posixpath.normpath(cwd))
        return "" if c == "." else c
    return ""


def _chdir(cur: Optional[str], toks: Sequence[str]) -> Optional[str]:
    """`cd` / `pushd` / `popd`: the new current directory, or None when it cannot be known."""
    if posixpath.basename(toks[0]) == "popd":
        return None
    args = list(toks[1:])
    while args and args[0] in ("-L", "-P", "-e", "-@", "--"):
        args = args[1:]
    target = args[0] if args else "~"
    if not target or target.startswith(("~", "-")) or "$" in target or "`" in target:
        return None
    if posixpath.isabs(target):
        return posixpath.normpath(target)
    if cur is None:
        return None
    new = posixpath.normpath(posixpath.join(cur, target)) if cur else posixpath.normpath(target)
    return "" if new == "." else new


def _place(cur: Optional[str], root: Optional[str]) -> Optional[str]:
    """The directory a test ran in as it appears in a target: "" = the project root, "a/b" = a
    project directory, "/abs/x" = a directory OUTSIDE the project (never equal to a project path),
    None = unknown (the run cannot be placed and enters no index)."""
    if cur is None:
        return None
    if root:
        rel = _inside(cur, root)
        return cur if rel is None else rel
    if posixpath.isabs(cur):
        return ""          # no root known: an absolute `cd` is (almost always) `cd <project>`
    return strip_worktree(cur)


def _norm_test_path(p: str, cur: str, root: Optional[str]) -> str:
    path, sep, node = p.partition("::")
    if not path:
        return p
    if root:
        ap = posixpath.normpath(path if posixpath.isabs(path) else posixpath.join(cur, path))
        rel = _inside(ap, root)
        path = ap if rel is None else rel
    else:
        base = _place(cur, None)
        if posixpath.isabs(path):
            path = posixpath.normpath(path)
        elif base:
            path = strip_worktree(posixpath.normpath(posixpath.join(base, path)))
        else:
            path = strip_worktree(posixpath.normpath(path))
    if path in (".", ""):
        path = "."
    return path + sep + node


# --------------------------------------------------------------------------- canonical forms
def _info_only(args: Sequence[str], table: Any) -> bool:
    """True when a runner invocation only prints information and runs no test."""
    for a in args:
        if a == "--":
            break
        if a in table or a.partition("=")[0] in table:
            return True
    return False


def _canon_pytest(args: Sequence[str], cur: str, root: Optional[str]) -> str:
    opts: List[Tuple[str, str]] = []
    flags: List[str] = []
    paths: List[str] = []
    i = 0
    args = list(args)
    while i < len(args):
        a = args[i]
        i += 1
        if a == "--":
            continue
        if a.startswith("--"):
            name, eq, val = a.partition("=")
            if name in _PYTEST_KEEP_FLAG:
                opts.append((name, ""))
            elif name in _PYTEST_KEEP_VAL:
                if not eq:
                    val = args[i] if i < len(args) else ""
                    i += 1
                opts.append((name, val))
            elif a in _PYTEST_DROP_FLAG or (name in _PYTEST_DROP_FLAG and name != "--color"):
                pass
            elif name in _PYTEST_DROP_VAL:
                if not eq and name not in _PYTEST_NO_VALUE:
                    i += 1
            else:
                # an option we do not know may change what runs; keep it (never covers others)
                if a not in flags:
                    flags.append(a)
            continue
        if a.startswith("-") and len(a) > 1:
            if a[:2] in ("-k", "-m"):
                val = a[2:]
                if not val:
                    val = args[i] if i < len(args) else ""
                    i += 1
                opts.append((a[:2], val))
            elif a in _PYTEST_DROP_VAL:
                i += 1
            continue            # -q -x -v -s -rA -xvs -n4 ...
        np_ = _norm_test_path(a, cur, root)
        if np_ != "." and np_ not in paths:
            paths.append(np_)
    if not paths:
        # a bare `pytest` runs the directory it was started in, not the whole project
        place = _place(cur, root)
        if place:
            paths.append(place)
    out = ["pytest"]
    for name, val in sorted(set(opts)):
        out.append(name)
        if val or name not in _PYTEST_KEEP_FLAG:
            out.append(val)
    out.extend(sorted(flags))
    out.extend(sorted(paths))
    return shlex.join(out)


def _canon_generic(toks: Sequence[str], place: str) -> str:
    keep = [t for t in toks if not (t in _NOISE_FLAGS or t.startswith("--tb") or t.startswith("--color")
                                    or t.startswith("--durations"))]
    s = shlex.join(keep)
    # `cd frontend && npm test` is not the project's `npm test` (target_covers never matches
    # a target with a `cd` part to anything but itself)
    return f"cd {shlex.quote(place)} && {s}" if place else s


def _runner_str(toks: Sequence[str]) -> str:
    return " ".join(toks[:4])


def _legacy_normalize(cmd: str) -> str:
    """Non-test commands: the pre-R3 whitespace/noise-flag normalisation (A2 `cmd:` keys)."""
    toks = _tokens(cmd)
    keep = [t for t in toks if not (t in _NOISE_FLAGS or t.startswith("--tb") or t.startswith("--color")
                                    or t.startswith("--durations"))]
    return " ".join(keep)[:MAX_TARGET_CHARS]


class _Analysis(NamedTuple):
    targets: Tuple[str, ...]    # canonical targets of the test runs that could be placed
    n_runs: int                 # test-runner segments that run tests (placed or not)
    n_info: int                 # runner segments that run NO tests (--version, --collect-only, -h, ...)
    owned: bool                 # the whole command's exit status is (all of) those runs' own


_NO_ANALYSIS = _Analysis((), 0, 0, False)


def _owns_exit(segs: Sequence[Tuple[str, Optional[str]]], i: int) -> bool:
    """Does the exit status of the whole command tell how segment i ended? Only when it really ran
    (not after `||` or inside a pipe) and nothing after it can replace its status: everything after it
    is joined with `&&` (so a zero status means segment i succeeded too). `; echo`, `|| echo`,
    `| head`, a trailing `&` all break this."""
    if segs[i][1] in ("||", "|"):
        return False
    for seg, op in segs[i + 1:]:
        if op == "&&" and seg:
            continue
        if not seg and op == ";":
            continue            # `pytest x;` -- an empty statement after it
        return False
    return True


@functools.lru_cache(maxsize=4096)
def _analyze(cmd: str, root: Optional[str], cwd: Optional[str]) -> _Analysis:
    cmd = _unwrap_shell((cmd or "").strip())
    root = posixpath.normpath(root) if root else None
    cur: Optional[str] = _start_dir(root, cwd)
    segs = _split_ops(cmd, keep_empty=True)
    targets: List[str] = []
    runs: List[int] = []
    n_info = 0
    exported_addopts: List[str] = []
    after_pipe_of_test = False
    for idx, (seg, op) in enumerate(segs):
        if not seg:
            continue
        if op == "|" and after_pipe_of_test:
            continue            # `| tail -20`, `| tee log`, `| grep x` after the test run
        after_pipe_of_test = False
        raw = _strip_redirections(_tokens(seg))
        if not raw:
            continue
        if raw[0] == "export":
            for t in raw[1:]:
                if t.startswith("PYTEST_ADDOPTS="):
                    exported_addopts = _tokens(t.partition("=")[2])
            continue
        addopts = exported_addopts
        for t in raw:
            if t.startswith("PYTEST_ADDOPTS="):
                addopts = _tokens(t.partition("=")[2])
            elif not (_ASSIGN_RE.match(t) or posixpath.basename(t) == "env" or t.startswith("-")):
                break
        toks = _strip_prefixes(raw)
        if not toks:
            continue
        if posixpath.basename(toks[0]) in ("cd", "pushd", "popd"):
            cur = _chdir(cur, toks)
            continue
        uni = _unify_runner(toks)
        if not _RUNNER_RE.match(_runner_str(uni)):
            continue
        after_pipe_of_test = True
        is_pytest = uni[0] == "pytest"
        args = list(addopts) + uni[1:] if is_pytest else uni[1:]
        if _info_only(args, _PYTEST_INFO_ONLY if is_pytest else _GENERIC_INFO_ONLY):
            n_info += 1
            continue
        runs.append(idx)
        place = _place(cur, root)
        if place is None or cur is None:
            continue            # ran in an unknown directory: no target (enters no index)
        if not is_pytest and not root and posixpath.isabs(cur):
            place = cur         # keeps `cd /abs && npm test` idempotent without a root
        targets.append(_canon_pytest(args, cur, root) if is_pytest else _canon_generic(uni, place))
    owned = bool(runs) and all(_owns_exit(segs, i) for i in runs)
    return _Analysis(tuple(dict.fromkeys(targets)), len(runs), n_info, owned)


def _analysis(cmd: Optional[str], root: Optional[str] = None, cwd: Optional[str] = None) -> _Analysis:
    try:
        cmd = (cmd or "").strip()
        if not cmd or not _ANY_RUNNER_RE.search(cmd):
            return _NO_ANALYSIS
        return _analyze(cmd, root or None, cwd or None)
    except Exception:
        return _NO_ANALYSIS


def _test_segments(cmd: str, root: Optional[str], cwd: Optional[str]) -> List[str]:
    return list(_analysis(cmd, root, cwd).targets)


def canonical_target(cmd: Optional[str], root: Optional[str] = None, cwd: Optional[str] = None) -> str:
    """The canonical test target of shell command `cmd` (idempotent: canonical(canonical(x)) ==
    canonical(x)). `root` (project root) and `cwd` (where the command ran) make absolute / `cd`-relative
    test paths project-relative. A command that runs no recognised test runner (or runs one only for
    information, or in an unknown directory) gets the legacy noise-flag normalisation, so non-test
    command keys are unchanged."""
    try:
        cmd = (cmd or "").strip()
        if not cmd:
            return ""
        segs = _analysis(cmd, root, cwd).targets
        if segs:
            return " && ".join(segs)[:MAX_TARGET_CHARS]
        return _legacy_normalize(cmd)
    except Exception:
        return " ".join((cmd or "").split())[:MAX_TARGET_CHARS]


def is_test_command(cmd: Optional[str]) -> bool:
    """True when `cmd` really runs tests somewhere we can name (`pytest --version`,
    `pytest --collect-only`, `cd $X && pytest` are not)."""
    return bool(_analysis(cmd).targets)


# meta["test_target_v"] on an observation: its meta["test_target"] was computed at capture time with
# the project root and the command's cwd, and is used as-is ("" = the command runs a test runner but
# no test we can place: info only, or an unknown directory -> it enters no run index).
TARGET_META_KEY = "test_target"
TARGET_META_V_KEY = "test_target_v"
TARGET_META_V = 2


def placed_target(cmd: Optional[str], root: Optional[str] = None, cwd: Optional[str] = None) -> Optional[str]:
    """Capture-time target: None when `cmd` involves no test runner at all; "" when it does but
    runs no test we can place (`pytest --version`, `cd $X && pytest`); the canonical target otherwise."""
    try:
        a = _analysis(cmd, root, cwd)
        if not a.n_runs and not a.n_info:
            return None
        return " && ".join(a.targets)[:MAX_TARGET_CHARS]
    except Exception:
        return None


def stored_target(meta: Any) -> Optional[str]:
    """The capture-time target of an observation (see TARGET_META_V), or None when it was stored by
    an older version (callers then re-canonicalise what they have)."""
    if isinstance(meta, dict) and meta.get(TARGET_META_V_KEY) == TARGET_META_V:
        v = meta.get(TARGET_META_KEY)
        if isinstance(v, str) and ".claude/worktrees/" in v:
            return canonical_target(v)      # stored before worktree paths were mapped
        return v if isinstance(v, str) else ""
    return None


def exit_owned(cmd: Optional[str]) -> Optional[bool]:
    """None when `cmd` runs no recognised test runner at all; False when it only asks a runner for
    information (runs no test) or when the command's exit status is not the test run's own;
    True otherwise."""
    a = _analysis(cmd)
    if not a.n_runs and not a.n_info:
        return None
    return bool(a.n_runs) and a.owned


# --------------------------------------------------------------------------- coverage
@functools.lru_cache(maxsize=8192)
def _pytest_parts(target: str) -> Optional[Tuple[Tuple[Tuple[str, str], ...], Tuple[str, ...]]]:
    toks = _tokens(target)
    if not toks or toks[0] != "pytest":
        return None
    opts: List[Tuple[str, str]] = []
    paths: List[str] = []
    i = 1
    while i < len(toks):
        t = toks[i]
        i += 1
        if t in _PYTEST_KEEP_FLAG or (t.startswith("--") and t.partition("=")[0] not in _PYTEST_KEEP_VAL):
            opts.append((t, ""))        # a selection flag, or an unknown option kept verbatim
        elif t.startswith("-"):
            opts.append((t, toks[i] if i < len(toks) else ""))
            i += 1
        else:
            paths.append(t)
    return tuple(sorted(opts)), tuple(paths)


def _path_covers(w: str, n: str) -> bool:
    if w == n:
        return True
    if "::" not in w:
        wp = w.rstrip("/")
        if n.startswith(wp + "::") or n.startswith(wp + "/"):
            return True
    else:
        # tests/x.py::TestA covers tests/x.py::TestA::test_b
        if n.startswith(w + "::"):
            return True
    return False


@functools.lru_cache(maxsize=65536)
def target_covers(wide: str, narrow: str) -> bool:
    """True when a PASSING run of canonical target `wide` also ran (and therefore passed) everything
    canonical target `narrow` selects: the same target; a file covers its tests (`x.py` covers
    `x.py::test_f`); a directory covers the files under it; a bare `pytest` covers every pytest
    target. Selection options (-k/-m/--deselect/...) and unknown options on `wide` must be absent or
    identical. Pure and cached (the run index calls it for many pairs)."""
    if not wide or not narrow:
        return False
    if wide == narrow:
        return True
    w_parts = wide.split(" && ")
    n_parts = narrow.split(" && ")
    if any(p.startswith("cd ") for p in w_parts) or any(p.startswith("cd ") for p in n_parts):
        return False            # a non-pytest run placed in a directory: only itself
    if len(w_parts) > 1 or len(n_parts) > 1:
        return all(any(target_covers(w, n) for w in w_parts) for n in n_parts)
    pw, pn = _pytest_parts(wide), _pytest_parts(narrow)
    if pw is None or pn is None:
        return False
    w_opts, w_paths = pw
    n_opts, n_paths = pn
    if w_opts and w_opts != n_opts:
        return False
    if not w_paths:
        return True
    if not n_paths:
        return False
    return all(any(_path_covers(w, n) for w in w_paths) for n in n_paths)


# --------------------------------------------------------------------------- outcome
def test_outcome(tool: Any) -> Optional[str]:
    """"pass" / "fail" / None (unknown) for a command's ToolInfo. Unknown whenever the run has
    not finished (status "running": Codex "Process running with session ID", Claude run_in_background)
    or nothing says how it ended: no runner summary AND no exit code is NEVER a pass.
    a runner asked only for information (`pytest --version/--co/-h/...`) ran no test (None);
    when the command's exit status is not the test run's own (`pytest x; echo`, `pytest x || echo`,
    `pytest x | head`), only the runner's own summary counts."""
    if tool is None:
        return None
    status = getattr(tool, "status", None)
    if status == "running":
        return None
    exit_code = getattr(tool, "exit_code", None)
    test = getattr(tool, "test", None)
    a = _analysis(getattr(tool, "command", None))
    if a.n_info and not a.n_runs:
        return None
    trust_exit = not (a.n_runs or a.n_info) or a.owned
    if test is not None:
        if (getattr(test, "failed", 0) or 0) > 0 or (getattr(test, "errors", 0) or 0) > 0:
            return "fail"
        if trust_exit and (status == "error" or exit_code not in (None, 0)):
            return "fail"
        if (getattr(test, "passed", 0) or 0) > 0:
            # one summary cannot vouch for several runs whose statuses we cannot see
            return "pass" if (trust_exit or a.n_runs <= 1) else None
        return None
    if not trust_exit:
        return None
    if status == "error" or exit_code not in (None, 0):
        return "fail"
    if exit_code == 0:
        return "pass"
    return None
