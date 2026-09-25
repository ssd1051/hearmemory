"""Shared fixtures for the judge tests (no tests in this module).

FakeStore mirrors the core's Store semantics that judge relies on (append-only JSONL, first-wins dedupe on read,
iter_observations yields END byte offsets, read_observations_window starts at a line start, state/ writes
atomic, nothing is written unless .hearmemory/VERSION exists), so judge tests do not depend on the core's progress.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import textwrap
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from hearmemory import interfaces as I

T0 = 1790244000.0          # 2026-09-24T10:00:00Z


def ts(offset_s: float) -> str:
    from hearmemory.judge._compat import ts_of
    return ts_of(T0 + offset_s)


class Clock:
    def __init__(self, t: float = T0 + 3600 * 5) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


_CLS = {"observations": (I.Observation, "id"), "claims": (I.Claim, "claim_id"),
        "candidates": (I.Candidate, "candidate_id"), "judgments": (I.Judgment, "judgment_id"),
        "events": (I.ControlEvent, "id")}


class FakeStore:
    def __init__(self, root: Any) -> None:
        self.root = Path(root)
        self.hearmemory_dir = self.root / I.HEARMEMORY_DIRNAME

    @classmethod
    def init(cls, root: Any) -> "FakeStore":
        d = Path(root) / I.HEARMEMORY_DIRNAME
        for sub in ("", "state", "locks", "ledger", "spool", "archive"):
            (d / sub).mkdir(exist_ok=True)
        (d / "VERSION").write_text(I.STORE_FORMAT + "\n")
        return cls(root)

    def is_initialised(self) -> bool:
        return (self.hearmemory_dir / "VERSION").is_file()

    def _path(self, key: str) -> Path:
        return self.hearmemory_dir / I.LAYOUT[key]

    def _append(self, key: str, rows: Sequence[Any]) -> None:
        if not rows or not self.is_initialised():
            return
        with open(self._path(key), "a", encoding="utf-8") as fh:
            fh.write("".join(json.dumps(r.to_dict(), ensure_ascii=False) + "\n" for r in rows))

    def append_observations(self, obs):
        self._append("observations", obs)
        return [o.id for o in obs]

    def append_claims(self, xs):
        self._append("claims", xs)
        return len(xs)

    def append_candidates(self, xs):
        self._append("candidates", xs)
        return len(xs)

    def append_judgments(self, xs):
        self._append("judgments", xs)
        return len(xs)

    def append_events(self, xs):
        self._append("events", xs)
        return len(xs)

    def _iter(self, key: str, since: int = 0) -> Iterator[Tuple[int, Any]]:
        cls, idf = _CLS[key]
        p = self._path(key)
        if not p.exists():
            return
        seen = set()
        with open(p, "rb") as fh:
            fh.seek(since)
            off = since
            while True:
                line = fh.readline()
                if not line or not line.endswith(b"\n"):
                    break
                off += len(line)
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get(idf) in seen:
                    continue
                seen.add(d.get(idf))
                yield off, cls.from_dict(d)

    def iter_observations(self, since_offset: int = 0):
        yield from self._iter("observations", since_offset)

    def iter_claims(self):
        return (o for _, o in self._iter("claims"))

    def iter_candidates(self):
        return (o for _, o in self._iter("candidates"))

    def iter_judgments(self):
        return (o for _, o in self._iter("judgments"))

    def iter_events(self):
        return (o for _, o in self._iter("events"))

    def read_observations_window(self, from_offset: int, max_bytes: int):
        p = self._path("observations")
        out: List[I.Observation] = []
        if not p.exists():
            return from_offset, out
        off = from_offset
        with open(p, "rb") as fh:
            fh.seek(from_offset)
            used = 0
            while used < max_bytes:
                line = fh.readline()
                if not line or not line.endswith(b"\n"):
                    break
                used += len(line)
                off += len(line)
                try:
                    out.append(I.Observation.from_dict(json.loads(line)))
                except Exception:
                    continue
        return off, out

    def _state_path(self, name: str) -> Path:
        return self.hearmemory_dir / I.STATE_FILES.get(name, "state/%s.json" % name)

    def read_state(self, name: str):
        try:
            return json.loads(self._state_path(name).read_text())
        except (OSError, ValueError):
            return None

    def write_state(self, name: str, data) -> None:
        if not self.is_initialised():
            return
        p = self._state_path(name)
        tmp = p.with_suffix(".tmp%d" % os.getpid())
        tmp.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True))
        os.replace(tmp, p)

    def fingerprint(self) -> str:
        parts = []
        for k in I.RAW_FILES:
            p = self._path(k)
            parts.append("%s:%d" % (k, p.stat().st_size if p.exists() else -1))
        return "|".join(parts)

    def merge_spool(self):
        return {}

    def read_lines(self, key: str) -> List[Dict[str, Any]]:
        p = self._path(key)
        return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


# ------------------------------------------------------------------------------------------------ project
PROJECT_FILES: Dict[str, str] = {
    "src/shop/__init__.py": "",
    "src/shop/ledger/__init__.py": "",
    "src/shop/ledger/sync.py": textwrap.dedent('''\
        class LedgerSync:
            def sync_rows(self, rows):
                return [normalize_tz(r) for r in rows]

        class SyncWorker:
            retries = 3

        def normalize_tz(row):
            return row
        '''),
    "src/shop/jobs/__init__.py": "",
    "src/shop/jobs/sync.py": textwrap.dedent('''\
        class SyncWorker:
            def run_nightly_sync(self):
                pass
        '''),
    "src/shop/export/__init__.py": "",
    "src/shop/export/csv_export.py": "def export_orders_csv(orders):\n    return ''\n",
    "src/shop/recon.py": textwrap.dedent('''\
        class ReconcileError(Exception):
            pass

        def reconcile_day(day_window_utc):
            return day_window_utc
        '''),
    "tests/test_recon.py": "def test_reconcile_tz():\n    pass\n\ndef test_reconcile_totals():\n    pass\n",
    "tests/test_sync.py": "def test_sync_rows():\n    pass\n",
    "docker-compose.yml": "services:\n  nightly-recon:\n    image: shop\n  export-csv:\n    image: shop\n",
    "config/settings.yaml": "ledger:\n  tz_offset_hours: 8\n  retry_count: 3\nname: shop\n",
    ".env.example": "LEDGER_TZ=UTC\nSTRIPE_KEY=\n",
    ".env": "STRIPE_KEY=sk_live_abcdefghijklmnopqrstuvwxyz\nSECRET_ONLY_IN_DOTENV=1\n",
    "pyproject.toml": "[project]\nname='shop'\n[project.scripts]\nshop-cli = 'shop.cli:main'\n",
    "README.md": "# shop\n",
}


def make_project(files: Optional[Dict[str, str]] = None, git: bool = True) -> str:
    root = tempfile.mkdtemp(prefix="hearmemory-judge-")
    root = os.path.realpath(root)
    for rel, content in (files or PROJECT_FILES).items():
        p = Path(root) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    if git:
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@e")
        subprocess.run(["git", "init", "-q", root], check=True, env=env)
        (Path(root) / ".gitignore").write_text(".env\n.hearmemory/\n")
        subprocess.run(["git", "-C", root, "add", "-A"], check=True, env=env)
        subprocess.run(["git", "-C", root, "commit", "-qm", "init"], check=True, env=env)
    return root


# ------------------------------------------------------------------------------------------------ observations
COMMIT_A = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
COMMIT_B = "b1b2c3d4e5f60718293a4b5c6d7e8f9012345678"


def prov(host: str = "claude", session: str = "s1", sub: Optional[str] = None, sub_type: Optional[str] = None,
         source: Optional[str] = None, commit: Optional[str] = COMMIT_A, branch: str = "main") -> I.Provenance:
    return I.Provenance(host=host, session_id=session, subagent_id=sub, subagent_type=sub_type,
                        git_commit=commit, git_branch=branch, source=source or "hook:PostToolUse")


_N = [0]


def obs(kind: str, text: str, t: float, p: Optional[I.Provenance] = None, tool: Optional[I.ToolInfo] = None,
        key: Optional[str] = None, meta: Optional[Dict[str, Any]] = None, excluded: bool = False) -> I.Observation:
    _N[0] += 1
    k = key or "test:%d:%s" % (_N[0], kind)
    return I.Observation(id=I.obs_id_for(k), ts=ts(t), kind=kind, event_key=k, provenance=p or prov(), text=text,
                         tool=tool, meta=dict(meta or {}), excluded=excluded)


def run(cmd: str, output: str, t: float, p: Optional[I.Provenance] = None, exit_code: int = 0,
        passed: int = 0, failed: int = 0, failed_ids: Sequence[str] = (), paths: Sequence[str] = (),
        dirty: Optional[Dict[str, str]] = None, runner: str = "pytest") -> I.Observation:
    test = None
    if passed or failed:
        target = " ".join(x for x in cmd.split() if x not in ("-q", "-x", "-v"))
        test = I.RunnerSummary(runner=runner, passed=passed, failed=failed, failed_ids=list(failed_ids), target=target)
    tool = I.ToolInfo(name="Bash", command=cmd, paths=list(paths), exit_code=exit_code,
                      status="ok" if exit_code == 0 else "error", test=test)
    meta = {} if dirty is None else {"dirty_state": dict(dirty)}
    return obs("command", "$ %s\n%s" % (cmd, output), t, p, tool=tool, meta=meta)


def edit(path: str, diff: str, t: float, p: Optional[I.Provenance] = None) -> I.Observation:
    return obs("file_edit", diff, t, p, tool=I.ToolInfo(name="Edit", paths=[path], status="ok"))


def say(text: str, t: float, p: Optional[I.Provenance] = None, kind: str = "assistant_message") -> I.Observation:
    return obs(kind, text, t, p)


def link_event(obs_id: str, p: I.Provenance, t: float) -> I.ControlEvent:
    return I.ControlEvent(id=I.stable_id("e-", obs_id, "link"), ts=ts(t), kind="provenance_link", target=obs_id,
                          provenance=p)


def default_cfg() -> Dict[str, Any]:
    import copy
    return copy.deepcopy({k: dict(v) for k, v in I.DEFAULT_CONFIG.items()})
