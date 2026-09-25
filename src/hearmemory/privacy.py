"""hearmemory.privacy -- path exclusion and text redaction.

Nothing here ever touches the network. redact() is called twice: once before
anything is written to disk, and again (on the already-redacted text) right
before a candidate's state is sent to Jev.
"""
from __future__ import annotations

import collections
import math
import os
import re
import shlex
from pathlib import PurePosixPath
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from .interfaces import (ENV_DUMP_WORDS, HEX_SAFE_CONTEXT_RE, HEX_SECRET_MIN_LEN, SECRET_ASSIGNMENT_RE,
                         SECRET_NAME_RE)

# ---------------------------------------------------------------------------
# Path exclusion (privacy.exclude_globs / privacy.jev_exclude_globs)
# ---------------------------------------------------------------------------
_GLOB_SPECIAL_RE = re.compile(r"[*?\[]")


def _fnmatch(name: str, pattern: str) -> bool:
    import fnmatch
    return fnmatch.fnmatchcase(name, pattern)


def _match_globs(candidates: Sequence[str], globs: Sequence[str]) -> bool:
    matched = False
    for pat in globs:
        neg = pat.startswith("!")
        p = pat[1:] if neg else pat
        hit = any(_fnmatch(c, p) for c in candidates)
        if hit:
            matched = not neg
    return matched


def _candidates_for(relpath: str) -> List[str]:
    norm = str(relpath).replace("\\", "/")
    is_outside = norm.startswith("/") or norm.startswith("..") or norm.startswith("~")
    if norm.startswith("~"):
        norm = os.path.expanduser(norm).replace("\\", "/")
    out: List[str] = []
    if is_outside:
        parts = [p for p in PurePosixPath(norm.lstrip("/")).parts if p not in (".", "")]
        for i in range(len(parts)):
            out.append("/".join(parts[i:]))
        if not out and parts:
            out.append(parts[-1])
    else:
        while norm.startswith("./"):
            norm = norm[2:]
        out.append(norm)
        out.append(PurePosixPath(norm).name)
    return [c for c in out if c]


def is_excluded(relpath: str, cfg: Mapping[str, Any]) -> bool:
    """True if `relpath` matches privacy.exclude_globs. Project-internal paths are matched
    by their relative path and basename; paths outside the project by every path suffix."""
    if not relpath:
        return False
    globs = ((cfg or {}).get("privacy") or {}).get("exclude_globs") or []
    return _match_globs(_candidates_for(relpath), list(globs))


def is_jev_excluded(relpath: str, cfg: Mapping[str, Any]) -> bool:
    if is_excluded(relpath, cfg):
        return True
    globs = ((cfg or {}).get("privacy") or {}).get("jev_exclude_globs") or []
    if not globs:
        return False
    return _match_globs(_candidates_for(relpath), list(globs))


# ---------------------------------------------------------------------------
# Command-level exclusion : `cat .env`, `source .env; env`, ...
# ---------------------------------------------------------------------------
_SEGMENT_SPLIT_RE = re.compile(r"&&|\|\||[;|\n]")


def _segments(command: str) -> List[str]:
    return [s.strip() for s in _SEGMENT_SPLIT_RE.split(command or "") if s.strip()]


def _tokens(segment: str) -> List[str]:
    try:
        return shlex.split(segment)
    except ValueError:
        return segment.split()


def command_touches_excluded(command: str, cfg: Mapping[str, Any]) -> bool:
    """True if any token (or the RHS of a `=`/`<`/`>`) in `command` names an excluded path."""
    if not command:
        return False
    globs = ((cfg or {}).get("privacy") or {}).get("exclude_globs") or []
    # The program a segment RUNS (`.venv/bin/pytest`, `node_modules/.bin/jest`) is not content being
    # read: directory-wide globs such as `.venv/**` do not withhold it (`.venv/bin/pytest x` was
    # silently dropped, so its passing run never counted). File-name globs (`*secret*`, ...) still do.
    prog_cfg = {"privacy": {"exclude_globs": [g for g in globs if not g.rstrip("*").endswith("/")]}}
    for segment in _segments(command):
        prog_seen = False
        for tok in _tokens(segment):
            is_prog = False
            if not prog_seen and not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tok):
                prog_seen = is_prog = True
            use_cfg = prog_cfg if is_prog else cfg
            for cand in ([tok] if is_prog else _assignment_values(tok)):
                cand = cand.strip().strip("'\"")
                if not cand:
                    continue
                if is_excluded(cand, use_cfg) or is_excluded(os.path.expanduser(cand), use_cfg):
                    return True
    return False


def _assignment_values(token: str) -> List[str]:
    out = [token]
    for sep in ("=", "<", ">"):
        if sep in token:
            out.append(token.split(sep, 1)[1])
    return out


def is_env_dump_command(command: str) -> bool:
    """True for `env`/`printenv` with nothing to execute, bare `export`/`set`, `declare -x|-p`,
    `typeset -x|-p`, `compgen -v` (these print the whole environment)."""
    if not command:
        return False
    for segment in _segments(command):
        toks = _tokens(segment)
        if not toks:
            continue
        head = os.path.basename(toks[0])
        rest = toks[1:]
        if head not in ENV_DUMP_WORDS:
            continue
        if head in ("env", "printenv"):
            # a trailing bare word is only a "print this var" argument when it looks like a
            # conventional ALL_CAPS env var name; a lowercase/mixed word is the command to run
            # (e.g. `env FOO=bar pytest` executes pytest, it does not print the environment).
            if all(t.startswith("-") or "=" in t or re.fullmatch(r"[A-Z_][A-Z0-9_]*", t) for t in rest):
                return True
        elif head == "export":
            if not rest or all(t == "-p" for t in rest):
                return True
        elif head == "set":
            if not rest:
                return True
        elif head in ("declare", "typeset"):
            if any(t in ("-x", "-p") for t in rest):
                return True
        elif head == "compgen":
            if "-v" in rest:
                return True
    return False


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------
_PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
                             re.S)
_KNOWN_KEY_RES = [
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bASIA[0-9A-Z]{16}\b"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{10,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\beyJ[\w-]+\.eyJ[\w-]+\.[\w-]+\b"),
    re.compile(r"\bBearer\s+\S{16,}"),
    # bare token prefixes (Hugging Face, GitLab, npm, Groq, Docker Hub, PyPI, xAI, Typesafe)
    re.compile(r"\bhf_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bnpm_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bgsk_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bdckr_pat_[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bpypi-[A-Za-z0-9_-]{30,}"),
    re.compile(r"\bxai-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bts_(?=[A-Za-z0-9]*\d)(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{24,}\b"),
]

# secrets passed as command-line arguments / headers. The flag (or header name) is kept, only
# the value is replaced, so the command stays readable.
_QVAL = r"(?:\"[^\"\n]*\"|'[^'\n]*'|[^\s\"'`;|&]+)"
_SECRET_FLAG_RE = re.compile(
    r"(?i)(?<![\w-])(--(?!no[-_])(?:[a-z0-9]+[-_])*(?:token|api[-_]?key|apikey|secret|client[-_]secret|"
    r"password|passwd|pass|pwd|access[-_]?key|secret[-_]?key|private[-_]?key|auth[-_]?token|credentials?))"
    r"(=|\s+)(?!-)(" + _QVAL + ")")
_AUTH_HEADER_RE = re.compile(
    r"(?i)\b((?:proxy-)?authorization\s*[:=]\s*(?:(?:basic|bearer|token|digest|bot|negotiate|apikey)\s+)?)"
    r"([^\s\"',;]+)")
# `mysql -pSECRET` (attached; a bare `-p` prompts), `sshpass -p SECRET`, `docker login -p SECRET`
_MYSQL_P_RE = re.compile(r"(?i)(\b(?:mysql|mysqldump|mysqladmin|mysqlimport|mariadb|mariadb-dump)\b[^\n|;&]*?\s-p)"
                         r"()(?![\s-])(" + _QVAL + ")")
_SSHPASS_P_RE = re.compile(r"(\bsshpass\b(?:\s+-[a-zA-Z](?:\s+\S+)?)*?\s+-p)(\s*)(?!-)(" + _QVAL + ")")
_LOGIN_P_RE = re.compile(r"(?i)(\b(?:docker|podman|nerdctl|buildah|helm\s+registry|skopeo)\s+login\b[^\n|;&]*?\s-p)"
                         r"(\s*)(?![\s-])(" + _QVAL + ")")
# quoted multi-word secret values: password="correct horse battery staple"
_QUOTED_SECRET_ASSIGN_RE = re.compile(
    r"(?i)\b(" + SECRET_NAME_RE[len("(?i)^"):-1] + r")([\"']?\s*[:=]\s*)([\"'])((?:(?!\3)[^\n\\]|\\.)+)\3")
_SECRET_ASSIGNMENT_RE = re.compile(SECRET_ASSIGNMENT_RE)
_SECRET_NAME_RE = re.compile(SECRET_NAME_RE)
_HEX_SAFE_CONTEXT_RE = re.compile(HEX_SAFE_CONTEXT_RE)
_URL_CRED_RE = re.compile(r"(://[^\s/:@]+):([^\s/@]+)@")
_HEX_RUN_RE = re.compile(r"\b[0-9a-fA-F]{%d,}\b" % HEX_SECRET_MIN_LEN)
_BASE64ISH_RE = re.compile(r"\b[A-Za-z0-9+/]{32,}={0,2}\b")


def _shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts = collections.Counter(s)
    length = len(s)
    return -sum((c / length) * math.log2(c / length) for c in counts.values())


def redact(text: str, *, cfg: Optional[Mapping[str, Any]] = None,
           extra_redact_patterns: Optional[Sequence[str]] = None,
           redact_hex_min_len: Optional[int] = None, redact_env_values: Optional[bool] = None,
           environ: Optional[Mapping[str, str]] = None, git_output: bool = False) -> Tuple[str, int]:
    """Redact secrets in `text`; returns (redacted_text, n_redactions). `git_output=True` (the text
    is the output of a `git ...` command) disables hex redaction entirely (git hashes are fine)."""
    if not text:
        return text, 0
    privacy_cfg = ((cfg or {}).get("privacy")) or {}
    if extra_redact_patterns is None:
        extra_redact_patterns = privacy_cfg.get("extra_redact_patterns") or []
    if redact_hex_min_len is None:
        redact_hex_min_len = int(privacy_cfg.get("redact_hex_min_len", HEX_SECRET_MIN_LEN) or HEX_SECRET_MIN_LEN)
    if redact_env_values is None:
        redact_env_values = bool(privacy_cfg.get("redact_env_values", True))

    n = 0
    out = text

    def _sub(pattern: "re.Pattern[str]", repl, s: str) -> str:
        nonlocal n
        s2, k = pattern.subn(repl, s)
        n += k
        return s2

    out = _sub(_PRIVATE_KEY_RE, "[REDACTED:private_key]", out)
    for pat in _KNOWN_KEY_RES:
        out = _sub(pat, "[REDACTED:api_key]", out)

    def _value_repl(prefix_groups: Sequence[int], value_group: int, label: str = "secret"):
        def repl(m: "re.Match[str]") -> str:
            nonlocal n
            val = m.group(value_group)
            if val.strip("\"'").startswith("[REDACTED"):
                return m.group(0)
            n += 1
            q = val[0] if val[:1] in ("'", '"') and val[-1:] == val[:1] and len(val) >= 2 else ""
            return "".join(m.group(g) for g in prefix_groups) + q + f"[REDACTED:{label}]" + q
        return repl

    # CLI flags, auth headers, `-p` passwords of mysql / sshpass / docker login, quoted values
    out = _SECRET_FLAG_RE.sub(_value_repl((1, 2), 3), out)
    out = _AUTH_HEADER_RE.sub(_value_repl((1,), 2), out)
    for pat in (_MYSQL_P_RE, _SSHPASS_P_RE, _LOGIN_P_RE):
        out = pat.sub(_value_repl((1, 2), 3, "password"), out)

    def _quoted_assign_repl(m: "re.Match[str]") -> str:
        nonlocal n
        if m.group(4).startswith("[REDACTED"):
            return m.group(0)
        n += 1
        return m.group(1) + m.group(2) + m.group(3) + "[REDACTED:secret]" + m.group(3)

    out = _QUOTED_SECRET_ASSIGN_RE.sub(_quoted_assign_repl, out)

    def _assign_repl(m: "re.Match[str]") -> str:
        nonlocal n
        if m.group(3).startswith("[REDACTED"):
            n -= 1              # already redacted above; _sub counted this match
            return m.group(0)
        return m.group(1) + m.group(2) + "[REDACTED:secret]"

    out = _sub(_SECRET_ASSIGNMENT_RE, _assign_repl, out)
    out = _sub(_URL_CRED_RE, lambda m: m.group(1) + ":[REDACTED:password]@", out)

    hex_min_len = max(HEX_SECRET_MIN_LEN, redact_hex_min_len)
    hex_re = _HEX_RUN_RE if hex_min_len == HEX_SECRET_MIN_LEN else re.compile(r"\b[0-9a-fA-F]{%d,}\b" % hex_min_len)
    if not git_output:
        snapshot = out

        def _hex_repl(m: "re.Match[str]") -> str:
            nonlocal n
            start = m.start()
            nl = snapshot.rfind("\n", 0, start)
            line_prefix = snapshot[nl + 1:start]
            if _HEX_SAFE_CONTEXT_RE.search(line_prefix):
                return m.group(0)
            n += 1
            return "[REDACTED:hex]"

        out = hex_re.sub(_hex_repl, out)

    def _b64_repl(m: "re.Match[str]") -> str:
        nonlocal n
        s = m.group(0)
        if not (any(c.isupper() for c in s) and any(c.islower() for c in s) and any(c.isdigit() for c in s)):
            return s
        if _shannon_entropy(s) < 4.0:
            return s
        n += 1
        return "[REDACTED:token]"

    out = _BASE64ISH_RE.sub(_b64_repl, out)

    if redact_env_values:
        env = environ if environ is not None else os.environ
        for name, value in env.items():
            if not value or len(value) < 8:
                continue
            if not _SECRET_NAME_RE.match(name):
                continue
            if value in out:
                count = out.count(value)
                out = out.replace(value, "[REDACTED:env]")
                n += count

    for pat in extra_redact_patterns:
        try:
            compiled = re.compile(pat)
        except re.error:
            continue
        out, k = compiled.subn("[REDACTED:custom]", out)
        n += k

    return out, n
