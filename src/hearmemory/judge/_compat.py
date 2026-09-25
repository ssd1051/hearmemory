"""judge-internal bridges to core helpers, with small stdlib fallbacks.

core (store, privacy, textutil, locks, budget) is built in parallel; judge only talks to it through the
names pinned in interfaces.ENTRY_POINTS and imports them lazily, so a missing or broken core module can
never break extraction or make a hook fail. Fallbacks are deliberately conservative (e.g. the fallback
redactor over-redacts; a missing Budget blocks every Jev call).
"""
from __future__ import annotations

import datetime as _dt
import errno
import fnmatch
import os
import posixpath
import re
import time
from typing import Any, Callable, Iterable, List, Mapping, Optional, Tuple

from hearmemory import interfaces as I

Clock = Callable[[], float]          # epoch seconds (time.time); injected by tests


# ---------------------------------------------------------------------------
# config / time
# ---------------------------------------------------------------------------
def cfg_get(cfg: Optional[Mapping[str, Any]], section: str, key: str, default: Any = None) -> Any:
    """Read cfg[section][key], falling back to interfaces.DEFAULT_CONFIG (and then `default`)."""
    try:
        sec = (cfg or {}).get(section)
        if isinstance(sec, Mapping) and key in sec and sec[key] is not None:
            return sec[key]
    except AttributeError:
        pass
    return I.DEFAULT_CONFIG.get(section, {}).get(key, default)


def ts_of(epoch: float) -> str:
    """UTC 'YYYY-MM-DDTHH:MM:SS.ffffffZ' (the store's timestamp format)."""
    return _dt.datetime.fromtimestamp(float(epoch), _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def now_ts(clock: Optional[Clock] = None) -> str:
    return ts_of((clock or time.time)())


_TS_RE = re.compile(r"^(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d)(?::(\d\d)(?:\.(\d{1,6})\d*)?)?")


def parse_ts(ts: Optional[str]) -> float:
    """Epoch seconds of a hearmemory timestamp; 0.0 when unparsable (sorts first, never raises)."""
    if not ts:
        return 0.0
    m = _TS_RE.match(str(ts))
    if not m:
        return 0.0
    y, mo, d, h, mi, s, frac = m.groups()
    try:
        dt = _dt.datetime(int(y), int(mo), int(d), int(h), int(mi), int(s or 0),
                          int((frac or "0").ljust(6, "0")), tzinfo=_dt.timezone.utc)
    except ValueError:
        return 0.0
    return dt.timestamp()


def utc_day(epoch: float) -> str:
    return _dt.datetime.fromtimestamp(float(epoch), _dt.timezone.utc).strftime("%Y-%m-%d")


def next_utc_day(epoch: float) -> float:
    d = _dt.datetime.fromtimestamp(float(epoch), _dt.timezone.utc).date() + _dt.timedelta(days=1)
    return _dt.datetime(d.year, d.month, d.day, tzinfo=_dt.timezone.utc).timestamp()


# ---------------------------------------------------------------------------
# privacy
# ---------------------------------------------------------------------------
_KNOWN_SECRET_RES = [
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END[A-Z ]*-----|$)")),
    ("aws_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("api_key", re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}")),
    ("stripe_key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{8,}")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{8,}")),
    ("google_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),
    ("jwt", re.compile(r"\beyJ[\w-]+\.eyJ[\w-]+\.[\w-]+")),
    ("bearer", re.compile(r"(?i)\bBearer\s+\S{16,}")),
    ("url_password", re.compile(r"(?<=://)[^\s/:@]+:[^\s/@]+(?=@)")),
    # kept in step with hearmemory.privacy; this fallback only runs when that module is not importable
    ("api_key", re.compile(r"\b(?:hf_[A-Za-z0-9]{20,}|glpat-[A-Za-z0-9_-]{20,}|npm_[A-Za-z0-9]{30,}|gsk_[A-Za-z0-9]{20,}"
                           r"|dckr_pat_[A-Za-z0-9_-]{20,}|pypi-[A-Za-z0-9_-]{30,}|xai-[A-Za-z0-9]{20,})")),
    ("secret", re.compile(r"(?i)(?<=\s)(?<![\w-])(?:(?<=--token )|(?<=--api-key )|(?<=--password )|(?<=--secret ))\S+")),
    ("secret", re.compile(r"(?i)(?<=authorization: basic )\S+|(?<=authorization: token )\S+")),
]
_ASSIGN_RE = re.compile(I.SECRET_ASSIGNMENT_RE)
_HEX_RUN_RE = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{%d,}(?![0-9A-Fa-f])" % I.HEX_SECRET_MIN_LEN)
_HEX_SAFE_RE = re.compile(I.HEX_SAFE_CONTEXT_RE)


def _fallback_redact(text: str) -> Tuple[str, int]:
    n = 0
    out = text or ""
    for kind, rx in _KNOWN_SECRET_RES:
        out, k = rx.subn("[REDACTED:%s]" % kind, out)
        n += k

    def _assign(m: "re.Match[str]") -> str:
        nonlocal n
        if m.group(3).startswith("[REDACTED"):
            return m.group(0)
        n += 1
        return m.group(1) + m.group(2) + "[REDACTED:assignment]"
    out = _ASSIGN_RE.sub(_assign, out)

    def _hex(m: "re.Match[str]") -> str:
        nonlocal n
        line_start = out.rfind("\n", 0, m.start()) + 1
        before = out[max(line_start, m.start() - 40):m.start()]
        if _HEX_SAFE_RE.search(before):
            return m.group(0)
        n += 1
        return "[REDACTED:hex]"
    out = _HEX_RUN_RE.sub(_hex, out)
    return out, n


def redact(text: str, cfg: Optional[Mapping[str, Any]] = None) -> Tuple[str, int]:
    """core privacy.redact when importable, else a conservative local redactor."""
    try:
        from hearmemory.privacy import redact as core_redact    # type: ignore
        res = core_redact(text or "", cfg=cfg) if cfg is not None else core_redact(text or "")
        if isinstance(res, tuple) and len(res) == 2:
            return str(res[0]), int(res[1])
    except Exception:
        pass
    return _fallback_redact(text or "")


def _glob_match(rel: str, globs: Iterable[str]) -> bool:
    rel = (rel or "").replace("\\", "/").lstrip("/")
    while rel.startswith("./"):
        rel = rel[2:]
    parts = rel.split("/")
    suffixes = ["/".join(parts[i:]) for i in range(len(parts))]
    hit = False
    for g in globs or ():
        neg = g.startswith("!")
        pat = g[1:] if neg else g
        if any(fnmatch.fnmatchcase(s, pat) for s in suffixes):
            hit = not neg
    return hit


def is_excluded(rel: str, cfg: Optional[Mapping[str, Any]]) -> bool:
    """privacy.exclude_globs check (core when importable)."""
    try:
        from hearmemory.privacy import is_excluded as core_is_excluded    # type: ignore
        return bool(core_is_excluded(rel, cfg))
    except Exception:
        pass
    return _glob_match(rel, cfg_get(cfg, "privacy", "exclude_globs", []) or [])


def jev_excluded(rel: str, cfg: Optional[Mapping[str, Any]]) -> bool:
    """privacy.jev_exclude_globs: evidence from these paths is never sent to Jev."""
    try:
        from hearmemory.privacy import is_jev_excluded as core_jev_excluded    # type: ignore
        return bool(core_jev_excluded(rel, cfg))
    except Exception:
        pass
    globs = cfg_get(cfg, "privacy", "jev_exclude_globs", []) or []
    return is_excluded(rel, cfg) or (bool(globs) and _glob_match(rel, globs))


# ---------------------------------------------------------------------------
# locks (fcntl.flock: interoperates with core file_lock on the same path)
# ---------------------------------------------------------------------------
try:
    import fcntl
except ImportError:          # pragma: no cover - hearmemory targets POSIX
    fcntl = None             # type: ignore[assignment]


class FLock:
    """A non-reentrant flock on <root>/.hearmemory/locks/<name>.lock. Never creates .hearmemory itself: the
    locks/ subdirectory is created with a plain os.mkdir only when .hearmemory/VERSION exists."""

    def __init__(self, hearmemory_dir: str, name: str) -> None:
        self.hearmemory_dir = str(hearmemory_dir)
        self.path = os.path.join(self.hearmemory_dir, "locks", name + ".lock")
        self._fd: Optional[int] = None

    def acquire(self, timeout_s: float = 0.0, poll_s: float = 0.02) -> bool:
        if fcntl is None or self._fd is not None:
            return False
        if not os.path.isfile(os.path.join(self.hearmemory_dir, I.LAYOUT["version"])):
            return False
        lock_dir = os.path.dirname(self.path)
        if not os.path.isdir(lock_dir):
            try:
                os.mkdir(lock_dir)
            except OSError as e:
                if e.errno != errno.EEXIST:
                    return False
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError:
            return False
        end = time.monotonic() + max(0.0, float(timeout_s))
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._fd = fd
                return True
            except OSError:
                if time.monotonic() >= end:
                    os.close(fd)
                    return False
                time.sleep(poll_s)

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass

    @property
    def held(self) -> bool:
        return self._fd is not None

    def __enter__(self) -> "FLock":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


# ---------------------------------------------------------------------------
# misc
# ---------------------------------------------------------------------------
def norm_relpath(path: str, root: Optional[str] = None) -> Optional[str]:
    """Project-relative posix path, or None when the path is outside the project."""
    p = (path or "").strip().strip("'\"`").replace("\\", "/")
    if not p:
        return None
    if p.startswith("~"):
        return None
    if p.startswith("/"):
        if not root:
            return None
        r = str(root).rstrip("/") + "/"
        if not p.startswith(r):
            return None
        p = p[len(r):]
    p = posixpath.normpath(p)
    try:
        from hearmemory.testcmd import strip_worktree
        p = strip_worktree(p)       # `.claude/worktrees/<name>/src/x.py` is `src/x.py`
    except Exception:
        pass
    if p in (".", "") or p.startswith("../") or p == "..":
        return None
    return p


def uniq(seq: Iterable[Any]) -> List[Any]:
    seen = set()
    out = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out
