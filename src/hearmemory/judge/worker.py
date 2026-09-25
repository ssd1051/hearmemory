"""Pipeline and background worker.

run_pipeline(store, cfg, deadline_s, use_jev, mode) runs ONE pass under the non-blocking "pipeline" lock:
  1 merge spool -> 2 (interval) Codex import -> 3 extract new observations -> 4 rule judgments ->
  5 Jev (cache first, budget, backoff) -> 6 memory rebuild -> stats.
Only one process runs a pass at a time; a busy lock returns {"skipped": "busy"} immediately.

The worker (`hearmemory worker --daemon`) is a singleton holding locks/worker.lock for its lifetime:
  * jev_capable: loop passes until idle for worker.idle_exit_s;
  * not capable (no key / no SDK / sandbox / disabled): mode "single_pass" - one pass without Jev, then
    exit; never idles, never writes jev_health, never changes queue state for missing Jev.
spawn_background() never raises and costs <= ~50 ms; a Jev-capable caller asks a non-capable worker to
yield (SIGUSR1) and starts a replacement with --wait-lock. stop_worker() only signals a pid it has
verified (lock held, pid alive, command line is a hearmemory worker). Hooks never call run_pipeline.
"""
from __future__ import annotations

import copy
import json
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from hearmemory import interfaces as I
from hearmemory.judge import _compat as C
from hearmemory.judge.extract import ExtractIndex, Extractor
from hearmemory.judge.jev import JevJudge, jev_capability
from hearmemory.judge.project_index import ProjectIndex
from hearmemory.judge.queue import Queue
from hearmemory.judge.rules import RuleJudge

MAX_NEW_OBS_PER_RUN = 2000
INDEX_REBUILD_TAIL_BYTES = 16 * 1024 * 1024
FETCH_WINDOW_BYTES = 256 * 1024
SPAWN_MIN_INTERVAL_S = 5.0
STOP_EVENT = threading.Event()            # set by SIGTERM / SIGUSR1 in a worker process
APPENDED_OUTCOMES = ("valid", "validation_error", "fallback_detected", "permission_denied", "disabled",
                     "transport_error")
_LAST_REBUILD: Dict[str, float] = {}


def _default_cfg() -> Dict[str, Any]:
    return copy.deepcopy({k: dict(v) for k, v in I.DEFAULT_CONFIG.items()})


def load_cfg(root: Any) -> Dict[str, Any]:
    try:
        from hearmemory.config import load_config
        cfg = load_config(root)
        if isinstance(cfg, Mapping):
            return dict(cfg)
    except Exception:
        pass
    return _default_cfg()


def open_store(root: Any) -> Any:
    try:
        from hearmemory.store import open_store as core_open
        return core_open(root, create=False)
    except Exception:
        return None


def _version_exists(root: Any) -> bool:
    return os.path.isfile(os.path.join(str(root), I.HEARMEMORY_DIRNAME, I.LAYOUT["version"]))


def _state_path(root: Any, name: str) -> str:
    return os.path.join(str(root), I.HEARMEMORY_DIRNAME, I.STATE_FILES[name])


def _read_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def _write_state(store: Any, name: str, data: Mapping[str, Any]) -> None:
    try:
        if store.is_initialised():
            store.write_state(name, data)
    except Exception:
        pass


def _safe_state(store: Any, name: str) -> Optional[Dict[str, Any]]:
    try:
        d = store.read_state(name)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


# =========================================================================================================
# pipeline
# =========================================================================================================
class _Timer:
    def __init__(self, deadline_s: float, stop: Optional[threading.Event]) -> None:
        self.end = time.monotonic() + max(0.0, float(deadline_s))
        self.stop = stop

    def left(self) -> float:
        return self.end - time.monotonic()

    def ok(self, need: float = 0.2) -> bool:
        return not (self.stop is not None and self.stop.is_set()) and self.left() > need


def run_pipeline(store: Any, cfg: Optional[Mapping[str, Any]] = None, deadline_s: float = 30.0, use_jev: bool = True,
                 mode: str = "worker", *, environ: Optional[Mapping[str, str]] = None,
                 clock: Optional[C.Clock] = None, jev_judge: Optional[JevJudge] = None,
                 import_fn: Optional[Callable[..., Any]] = None, rebuild_fn: Optional[Callable[..., Any]] = None,
                 stop_event: Optional[threading.Event] = None) -> Dict[str, Any]:
    """One bounded pass (see module doc). Never raises; returns stats (or {"skipped": reason})."""
    try:
        if store is None or not store.is_initialised():
            return {"skipped": "not_initialised"}
    except Exception:
        return {"skipped": "not_initialised"}
    cfg = dict(cfg) if cfg else load_cfg(store.root)
    lock = C.FLock(str(store.hearmemory_dir), "pipeline")
    if not lock.acquire(0.0):
        return {"skipped": "busy"}
    try:
        return _pipeline_locked(store, cfg, _Timer(deadline_s, stop_event), use_jev, mode, environ,
                                clock or time.time, jev_judge, import_fn, rebuild_fn)
    except Exception as exc:              # a broken pass must never break the worker / CLI
        return {"error": type(exc).__name__}
    finally:
        lock.release()


def _pipeline_locked(store: Any, cfg: Dict[str, Any], timer: _Timer, use_jev: bool, mode: str,
                     environ: Optional[Mapping[str, str]], clock: C.Clock, jev_judge: Optional[JevJudge],
                     import_fn: Optional[Callable[..., Any]], rebuild_fn: Optional[Callable[..., Any]]) -> Dict[str, Any]:
    stats: Dict[str, Any] = {"mode": mode, "new_obs": 0, "claims": 0, "candidates": 0, "rule_judgments": 0,
                             "jev_judgments": 0, "rebuilt": False}
    now = clock()
    # 1. spool
    if hasattr(store, "merge_spool") and timer.ok():
        try:
            store.merge_spool()
        except Exception:
            stats["spool_error"] = True
    xindex = ExtractIndex(_safe_state(store, "extract_index"))
    # 2. codex import (interval)
    every = float(C.cfg_get(cfg, "worker", "import_codex_every_s", 60))
    if timer.ok(2.0) and now - float(xindex.meta.get("last_import_epoch", 0.0)) >= every:
        fn = import_fn
        if fn is None:
            try:
                from hearmemory.host.codex import import_rollouts as fn    # type: ignore
            except Exception:
                fn = None
        if fn is not None:
            try:
                stats["imported"] = fn(store, cfg)
            except Exception:
                stats["import_error"] = True
            if import_fn is None:
                # map launch.sh / MCP proxy session ids to the rollout sessions just imported
                try:
                    from hearmemory.host.codex import reconcile_sessions
                    reconcile_sessions(store, cfg)
                except Exception:
                    stats["reconcile_error"] = True
        xindex.meta["last_import_epoch"] = now
    # 3. extract
    queue = Queue.load(store)
    new_cands: List[I.Candidate] = []
    if timer.ok(1.0):
        new_cands = _extract_step(store, cfg, xindex, queue, timer, clock, stats)
    else:
        _write_state(store, "extract_index", xindex.to_dict())
    # 4. rules
    rule_cands = [c for c in new_cands if c.rule_hint]
    if rule_cands:
        js = RuleJudge(clock).judge(rule_cands)
        if js:
            store.append_judgments(js)
            ts = C.now_ts(clock)
            for j in js:
                queue.set(j.candidate_id, "rule_judged", ts)
        stats["rule_judgments"] = len(js)
    # 5. jev
    if use_jev and timer.ok(1.0):
        _jev_step(store, cfg, queue, timer, clock, environ, jev_judge, stats)
    queue.save(store)
    stats["queue"] = queue.counts()
    stats["waiting_for_jev"] = queue.waiting_for_jev()
    # 6. rebuild
    changed = bool(stats["new_obs"] or stats["rule_judgments"] or stats["jev_judgments"])
    key = str(store.root)
    min_iv = float(C.cfg_get(cfg, "worker", "rebuild_min_interval_s", 5.0))
    if changed and timer.ok(0.5) and time.monotonic() - _LAST_REBUILD.get(key, -1e9) >= min_iv:
        stats["rebuilt"] = _rebuild(store, cfg, timer, rebuild_fn)
        _LAST_REBUILD[key] = time.monotonic()
    return stats


def _actor_map(store: Any) -> I.ActorMap:
    am = I.ActorMap()
    try:
        evs = [e for e in store.iter_events() if e.kind in ("provenance_link", "session_alias")]
    except Exception:
        return am
    for e in sorted(evs, key=lambda e: (e.ts or "", e.id)):
        am.add_event(e)
    return am


def make_fetcher(store: Any) -> Callable[[str, Mapping[str, Any]], Optional[I.Observation]]:
    """Fetch one observation back by its start offset (extract index `off`); tolerant of stale offsets."""
    def fetch(obs_id: str, summary: Mapping[str, Any]) -> Optional[I.Observation]:
        off = summary.get("off")
        if off is None:
            return None
        try:
            _, objs = store.read_observations_window(int(off), FETCH_WINDOW_BYTES)
        except Exception:
            return None
        for o in objs[:4]:
            if o.id == obs_id:
                return o
        return None
    return fetch


def _extract_step(store: Any, cfg: Dict[str, Any], xindex: ExtractIndex, queue: Queue, timer: _Timer,
                  clock: C.Clock, stats: Dict[str, Any]) -> List[I.Candidate]:
    cursors = _safe_state(store, "cursors") or {}
    since = int(((cursors.get("extract") or {}).get("obs_offset")) or 0)
    history: List[I.Observation] = []
    offsets: Dict[str, int] = {}
    if not xindex.obs and since > 0:
        # index lost or corrupt: rebuild history from the file tail (worker only, never a hook)
        start = max(0, since - INDEX_REBUILD_TAIL_BYTES)
        try:
            _, history = store.read_observations_window(start, since - start)
        except Exception:
            history = []
        stats["index_rebuilt_from_tail"] = len(history)
    new: List[I.Observation] = []
    prev = since
    end = since
    for off, o in store.iter_observations(since):
        offsets[o.id] = prev                       # core yields END offsets; the line starts at the previous one
        prev = off
        end = off
        new.append(o)
        if len(new) >= MAX_NEW_OBS_PER_RUN or not timer.ok(1.0):
            break
    stats["new_obs"] = len(new)
    if not new and not xindex.deferred and not history:
        _write_state(store, "extract_index", xindex.to_dict())
        return []
    index = ProjectIndex.load_or_build(store, cfg, clock=clock)
    ex = Extractor(index, cfg, actor_map=_actor_map(store), xindex=xindex, clock=clock, fetch=make_fetcher(store))
    res = ex.extract(new, history, None, offsets=offsets)
    if res.claims:
        store.append_claims(res.claims)
    if res.candidates:
        store.append_candidates(res.candidates)
        ts = C.now_ts(clock)
        for c in res.candidates:
            queue.add(c, ts)
    stats["claims"] = len(res.claims)
    stats["candidates"] = len(res.candidates)
    stats["dropped"] = res.dropped
    _write_state(store, "extract_index", xindex.to_dict())
    cursors = _safe_state(store, "cursors") or {}         # re-read: the import step may have advanced its part
    cursors.setdefault("extract", {})
    cursors["extract"]["obs_offset"] = end
    _write_state(store, "cursors", cursors)
    return list(res.candidates)


def _jev_step(store: Any, cfg: Dict[str, Any], queue: Queue, timer: _Timer, clock: C.Clock,
              environ: Optional[Mapping[str, str]], jev_judge: Optional[JevJudge], stats: Dict[str, Any]) -> None:
    judge = jev_judge or JevJudge(cfg, store=store, environ=environ, clock=clock)
    if not judge.capable:
        stats["jev_unavailable"] = judge.reason       # process-local: nothing is written anywhere
        return
    why = judge.blocked()
    if why:
        stats["jev_unavailable"] = why
        return
    now = clock()
    due = queue.due(now)
    if not due:
        return
    if judge.budget_exhausted():
        until = C.next_utc_day(now)
        for cid in due:
            queue.defer(cid, until, "budget")
        stats["jev_unavailable"] = "budget"
        return
    limit = int(C.cfg_get(cfg, "jev", "max_calls_per_run", 40))
    want = set(due[:limit])
    by_id: Dict[str, I.Candidate] = {}
    for c in store.iter_candidates():
        if c.candidate_id in want:
            by_id[c.candidate_id] = c
    batch = [by_id[cid] for cid in due[:limit] if cid in by_id]
    for cid in due[:limit]:
        if cid not in by_id:
            queue.set(cid, "skipped", C.now_ts(clock), "candidate row missing")
    js = judge.judge(batch, max(0.5, timer.left() - 0.5))
    ts = C.now_ts(clock)
    max_attempts = int(C.cfg_get(cfg, "jev", "max_attempts", 5))
    to_append = [j for j in js if j.outcome in APPENDED_OUTCOMES]
    if to_append:
        store.append_judgments(to_append)
    for j in js:
        if j.outcome == "valid":
            queue.set(j.candidate_id, "judged", ts)
        elif j.outcome in ("validation_error", "fallback_detected", "disabled"):
            queue.set(j.candidate_id, "skipped", ts, j.outcome)
        elif j.outcome == "transport_error":
            queue.transport_error(j.candidate_id, clock(), max_attempts, j.error or "transport_error")
        elif j.outcome == "budget_blocked":
            queue.defer(j.candidate_id, C.next_utc_day(clock()), "budget")
        # permission_denied: stays pending for a process with a working key
    stats["jev_judgments"] = sum(1 for j in js if j.outcome == "valid")
    stats["jev_outcomes"] = dict(judge.stats)
    if judge.stop_reason:
        stats["jev_unavailable"] = judge.stop_reason


def _rebuild(store: Any, cfg: Dict[str, Any], timer: _Timer, rebuild_fn: Optional[Callable[..., Any]]) -> bool:
    try:
        if rebuild_fn is not None:
            rebuild_fn(store, cfg, max(0.2, timer.left()))
            return True
        from hearmemory.memory.build import load_or_rebuild
        try:
            load_or_rebuild(store, cfg, allow_rebuild=True, deadline_s=max(0.2, timer.left()), pipeline_locked=True)
        except TypeError:
            load_or_rebuild(store, cfg, allow_rebuild=True, deadline_s=max(0.2, timer.left()))
        return True
    except Exception:
        return False


# =========================================================================================================
# worker process
# =========================================================================================================
def _install_signal_handlers() -> None:
    def _h(signum: int, frame: Any) -> None:
        STOP_EVENT.set()
    for sig in (signal.SIGTERM, signal.SIGUSR1):
        try:
            signal.signal(sig, _h)
        except (ValueError, OSError):      # not in the main thread (tests)
            pass


def _write_info(store: Any, info: I.WorkerInfo) -> None:
    if store is not None and _version_exists(store.root):
        _write_state(store, "worker", info.to_dict())


def run_worker(root: Any, mode: str = "daemon", *, launched_by: Optional[str] = None, use_jev: bool = True,
               timeout_s: Optional[float] = None, wait_lock_s: float = 0.0, store: Any = None,
               cfg: Optional[Mapping[str, Any]] = None, environ: Optional[Mapping[str, str]] = None,
               clock: Optional[C.Clock] = None, stop_event: Optional[threading.Event] = None,
               install_signals: bool = True, pipeline_fn: Optional[Callable[..., Dict[str, Any]]] = None) -> int:
    """`hearmemory worker --daemon|--once`. Returns an exit code (0; nothing to do is not an error)."""
    root = os.path.realpath(str(root))
    if not _version_exists(root):
        return 0
    store = store if store is not None else open_store(root)
    if store is None:
        return 0
    cfg = dict(cfg) if cfg else load_cfg(root)
    env = os.environ if environ is None else environ
    stop = stop_event or STOP_EVENT
    if install_signals:
        _install_signal_handlers()
    capable, reason = jev_capability(cfg, env)
    if not use_jev:
        capable, reason = False, reason or "config_disabled"
    wl = C.FLock(str(store.hearmemory_dir), "worker")
    if not wl.acquire(max(0.0, float(wait_lock_s))):
        return 0                                   # another worker is running: nothing to do
    clock = clock or time.time
    pipeline_fn = pipeline_fn or run_pipeline
    info: Optional[I.WorkerInfo] = None
    try:
        eff_mode = "once" if mode == "once" else ("daemon" if capable else "single_pass")
        info = I.WorkerInfo(pid=os.getpid(), started_ts=C.now_ts(clock), jev_capable=bool(capable),
                            jev_unavailable_reason=None if capable else reason, mode=eff_mode,
                            launched_by=launched_by, last_beat_ts=C.now_ts(clock),
                            last_spawn_ts=(_read_json(_state_path(root, "worker")) or {}).get("last_spawn_ts"),
                            jev={"capable": bool(capable), "reason": None if capable else reason,
                                 "model": str(C.cfg_get(cfg, "jev", "model", I.JEV_MODEL_DEFAULT)),
                                 "day": C.utc_day(clock()), "calls_today": 0, "last_call_ts": None})
        _write_info(store, info)
        run_deadline = float(timeout_s or C.cfg_get(cfg, "worker", "run_deadline_s", 30.0))
        if eff_mode in ("once", "single_pass"):
            st = pipeline_fn(store, cfg, run_deadline, bool(capable), eff_mode, environ=env, clock=clock,
                             stop_event=stop)
            info.stats = _small_stats(st)
            info.last_beat_ts = C.now_ts(clock)
            _note_jev_calls(info, st, clock)
            _write_info(store, info)
            return 0
        poll = float(C.cfg_get(cfg, "worker", "poll_s", 1.0))
        idle_exit = float(C.cfg_get(cfg, "worker", "idle_exit_s", 600))
        last_activity = time.monotonic()
        while not stop.is_set():
            if not _version_exists(root):
                return 0                           # purged: exit without writing anything
            st = pipeline_fn(store, cfg, run_deadline, True, "daemon", environ=env, clock=clock, stop_event=stop)
            if not _version_exists(root):
                return 0
            info.last_beat_ts = C.now_ts(clock)
            info.stats = _small_stats(st)
            _note_jev_calls(info, st, clock)
            _write_info(store, info)
            busy = bool(st.get("new_obs") or st.get("jev_judgments") or st.get("candidates"))
            if busy:
                last_activity = time.monotonic()
            elif time.monotonic() - last_activity >= idle_exit:
                return 0
            stop.wait(poll)
        return 0
    finally:
        # never leave a worker.json that still looks alive after this process is gone
        if info is not None:
            try:
                info.exited_ts = C.now_ts(clock)
                _write_info(store, info)
            except Exception:
                pass
        wl.release()


def _note_jev_calls(info: I.WorkerInfo, st: Mapping[str, Any], clock: C.Clock) -> None:
    """keep the worker's Jev tally (this UTC day) in worker.json for `hearmemory status`."""
    jev = info.jev if isinstance(info.jev, dict) else {}
    day = C.utc_day(clock())
    if jev.get("day") != day:
        jev["day"], jev["calls_today"] = day, 0
    n = int(st.get("jev_judgments") or 0)
    if n > 0:
        jev["calls_today"] = int(jev.get("calls_today") or 0) + n
        jev["last_call_ts"] = C.now_ts(clock)
    info.jev = jev


def _small_stats(st: Mapping[str, Any]) -> Dict[str, Any]:
    keep = ("new_obs", "claims", "candidates", "rule_judgments", "jev_judgments", "rebuilt", "waiting_for_jev",
            "jev_unavailable", "skipped", "error")
    return {k: st[k] for k in keep if k in st}


# =========================================================================================================
# spawn / stop
# =========================================================================================================
def _pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    # A killed child of THIS process stays a zombie until reaped: it is not a running worker.
    return not _zombie(pid)


def _zombie(pid: int) -> bool:
    """True if `pid` is a zombie. Reaps it when it is our own child (portable: Linux and macOS)."""
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
        if done == pid:
            return True
    except (ChildProcessError, OSError):
        pass
    try:
        with open("/proc/%d/stat" % pid, "r") as fh:
            return fh.read().rsplit(")", 1)[-1].split()[0] == "Z"
    except (OSError, IndexError):
        pass
    try:                                              # no /proc (macOS): ask ps for the state
        p = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, timeout=0.5)
        return p.stdout.decode("utf-8", "replace").strip().startswith("Z")
    except (OSError, subprocess.SubprocessError):
        return False


def _cmdline(pid: int) -> str:
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as fh:
            return fh.read().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        pass
    try:
        p = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, timeout=0.5)
        return p.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return ""


def is_hearmemory_worker(pid: int) -> bool:
    cmd = _cmdline(pid)
    return _pid_alive(pid) and "hearmemory" in cmd and "worker" in cmd


def worker_argv(root: str, launched_by: Optional[str], wait_lock_s: Optional[float] = None,
                python: Optional[str] = None) -> List[str]:
    # NOTE: `--project` is a top-level `hearmemory` option (parsed before the subcommand is chosen);
    # it must precede "worker" in argv or the real CLI parser rejects it as an unrecognized
    # argument to the `worker` subcommand.
    argv = [python or sys.executable, "-m", "hearmemory", "--project", root, "worker", "--daemon",
            "--launched-by", launched_by or "spawn"]
    if wait_lock_s:
        argv += ["--wait-lock", str(wait_lock_s)]
    return argv


def _popen(argv: Sequence[str]) -> Any:
    return subprocess.Popen(list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)


def _touch_spawn(root: str, now: float) -> None:
    path = _state_path(root, "worker")
    if not _version_exists(root) or not os.path.isdir(os.path.dirname(path)):
        return
    d = _read_json(path) or {"pid": 0, "started_ts": "", "jev_capable": False, "mode": "daemon"}
    d["last_spawn_ts"] = C.ts_of(now)
    tmp = "%s.tmp-%d-%d" % (path, os.getpid(), time.time_ns())
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _record_spawn_pid(root: str, proc: Any) -> None:
    """Remember the pid of the worker process just started (worker.json `spawn_pid`). Until that
    process takes the worker lock and writes its own pid, stop_worker() can only find it through this
    (the uninstall / test-cleanup race: a worker spawned a moment ago, not yet holding the lock, kept
    writing .hearmemory/state while the directory was being removed)."""
    pid = int(getattr(proc, "pid", 0) or 0)
    if pid <= 0:
        return
    path = _state_path(root, "worker")
    if not _version_exists(root) or not os.path.isdir(os.path.dirname(path)):
        return
    d = _read_json(path) or {"pid": 0, "started_ts": "", "jev_capable": False, "mode": "daemon"}
    d["spawn_pid"] = pid
    tmp = "%s.tmp-%d-%d" % (path, os.getpid(), time.time_ns())
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _terminate(pid: int, timeout_s: float) -> bool:
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return not _pid_alive(pid)
    end = time.monotonic() + max(0.1, float(timeout_s))
    while time.monotonic() < end:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    time.sleep(0.1)
    return not _pid_alive(pid)


def _is_worker_of(pid: int, root: str) -> bool:
    return bool(pid) and pid != os.getpid() and is_hearmemory_worker(pid) and root in _cmdline(pid)


def spawn_background(root: Any, launched_by: Optional[str] = None, *, environ: Optional[Mapping[str, str]] = None,
                     cfg: Optional[Mapping[str, Any]] = None, popen: Optional[Callable[[Sequence[str]], Any]] = None,
                     clock: Optional[C.Clock] = None, python: Optional[str] = None) -> bool:
    """Start (or hand over to) a background worker. Never raises; ~<= 50 ms; True when a process was started."""
    try:
        root = os.path.realpath(str(root))
        if not _version_exists(root):
            return False
        env = os.environ if environ is None else environ
        if str(env.get("HEARMEMORY_DISABLE", "")).strip() not in ("", "0"):
            return False
        if cfg is None:
            cfg = load_cfg(root)
        if not C.cfg_get(cfg, "worker", "spawn_from_hooks", True):
            return False
        now = (clock or time.time)()
        popen = popen or _popen
        hearmemory_dir = os.path.join(root, I.HEARMEMORY_DIRNAME)
        wl = C.FLock(hearmemory_dir, "worker")
        if wl.acquire(0.0):
            wl.release()
            info = _read_json(_state_path(root, "worker")) or {}
            last = C.parse_ts(info.get("last_spawn_ts"))
            if last and 0 <= now - last < SPAWN_MIN_INTERVAL_S:
                return False
            _touch_spawn(root, now)
            _record_spawn_pid(root, popen(worker_argv(root, launched_by, python=python)))
            return True
        # a worker holds the lock: hand over only if it cannot use Jev and we can
        capable, _ = jev_capability(cfg, env)
        if not capable:
            return False
        info = _read_json(_state_path(root, "worker")) or {}
        pid = int(info.get("pid") or 0)
        if info.get("jev_capable") is not False or not is_hearmemory_worker(pid):
            return False
        last = C.parse_ts(info.get("last_spawn_ts"))
        if last and 0 <= now - last < SPAWN_MIN_INTERVAL_S:
            return False
        try:
            os.kill(pid, signal.SIGUSR1)
        except OSError:
            return False
        _touch_spawn(root, now)
        _record_spawn_pid(root, popen(worker_argv(root, launched_by,
                                                  wait_lock_s=float(C.cfg_get(cfg, "worker", "handoff_wait_s", 5.0)),
                                                  python=python)))
        return True
    except Exception:
        return False


def stop_worker(root: Any, timeout_s: float = 3.0) -> bool:
    """Stop the running worker (uninstall step 1). True when no worker is running afterwards.
    Signals only a verified pid: lock held + pid alive + command line is a hearmemory worker."""
    try:
        root = os.path.realpath(str(root))
        hearmemory_dir = os.path.join(root, I.HEARMEMORY_DIRNAME)
        if not os.path.isdir(hearmemory_dir):
            return True
        info = _read_json(_state_path(root, "worker")) or {}
        spawn_pid = int(info.get("spawn_pid") or 0)
        if os.path.isfile(os.path.join(hearmemory_dir, I.LAYOUT["version"])):
            wl = C.FLock(hearmemory_dir, "worker")
            if wl.acquire(0.0):
                wl.release()
                # Nobody holds the lock -- but a worker spawned a moment ago may not have taken it
                # yet (it would then start writing .hearmemory/ after we return).
                if _is_worker_of(spawn_pid, root):
                    return _terminate(spawn_pid, timeout_s)
                return True
        pid = int(info.get("pid") or 0)
        if pid == os.getpid() or not is_hearmemory_worker(pid):
            # lock held but worker.json not (yet) naming the holder: the just-spawned process
            if _is_worker_of(spawn_pid, root):
                return _terminate(spawn_pid, timeout_s)
            return False
        ok = _terminate(pid, timeout_s)
        if spawn_pid != pid and _is_worker_of(spawn_pid, root):
            ok = _terminate(spawn_pid, timeout_s) and ok
        return ok
    except Exception:
        return False


# =========================================================================================================
# helpers for CLI/MCP (`hearmemory worker --retry-failed / --status`, `hearmemory status`)
# =========================================================================================================
def retry_failed(store: Any, clock: Optional[C.Clock] = None) -> int:
    q = Queue.load(store)
    n = q.reset_failed(C.now_ts(clock))
    if n:
        q.save(store)
    return n


def worker_liveness(root: Any) -> Dict[str, Any]:
    """Honest worker state for `hearmemory status` / `doctor` (status used to print
    "running (pid 0)" whenever state/worker.json merely existed).

    state: "running"  -- the worker lock is held and the pid in worker.json is a live hearmemory worker;
           "starting" -- a worker was just spawned (spawn_pid alive) and has not taken the lock yet;
           "busy"     -- the lock is held but worker.json does not (yet) name a live worker;
           "stopped"  -- no live worker (worker.json, if any, describes a past one: `last_*` fields);
    """
    root = os.path.realpath(str(root))
    info = _read_json(_state_path(root, "worker")) or {}
    hearmemory_dir = os.path.join(root, I.HEARMEMORY_DIRNAME)
    held = False
    if _version_exists(root):
        wl = C.FLock(hearmemory_dir, "worker")
        if wl.acquire(0.0):
            wl.release()
        else:
            held = True
    pid = int(info.get("pid") or 0)
    spawn_pid = int(info.get("spawn_pid") or 0)
    out: Dict[str, Any] = {"pid": None, "mode": info.get("mode"), "jev_capable": info.get("jev_capable"),
                           "jev_unavailable_reason": info.get("jev_unavailable_reason"),
                           "jev": info.get("jev") if isinstance(info.get("jev"), dict) else {},
                           "launched_by": info.get("launched_by"), "started_ts": info.get("started_ts"),
                           "last_beat_ts": info.get("last_beat_ts"), "exited_ts": info.get("exited_ts"),
                           "last_pid": pid or None}
    if held and pid and not info.get("exited_ts") and is_hearmemory_worker(pid):
        out.update(state="running", pid=pid)
    elif held:
        out.update(state="busy")
    elif spawn_pid and _is_worker_of(spawn_pid, root):
        out.update(state="starting", pid=spawn_pid)
    else:
        out.update(state="stopped")
    return out


def worker_status(root: Any, environ: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """Plain facts for `hearmemory status` / `doctor` (never the key itself)."""
    root = os.path.realpath(str(root))
    info = _read_json(_state_path(root, "worker")) or {}
    live = worker_liveness(root)
    running = live["state"] in ("running", "busy", "starting")
    capable, reason = jev_capability(load_cfg(root), os.environ if environ is None else environ)
    q = _read_json(_state_path(root, "queue")) or {}
    entries = q.get("entries") or {}
    counts: Dict[str, int] = {}
    for e in entries.values():
        counts[e.get("status", "?")] = counts.get(e.get("status", "?"), 0) + 1
    xi = _read_json(_state_path(root, "extract_index")) or {}
    return {"running": running, "state": live["state"], "liveness": live, "worker": info,
            "this_process_jev_capable": capable,
            "this_process_jev_reason": reason, "queue": counts,
            "waiting_for_jev": sum(1 for e in entries.values() if e.get("status") == "pending" and not e.get("rule")),
            "jev_health": _read_json(_state_path(root, "jev_health")) or {},
            "dropped_total": (xi.get("stats") or {}).get("dropped_total", {})}


def main(argv: Optional[Sequence[str]] = None) -> int:
    """`python -m hearmemory.judge.worker` (usable without the CLI/MCP CLI)."""
    import argparse
    ap = argparse.ArgumentParser(prog="hearmemory-worker")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--daemon", action="store_true")
    g.add_argument("--once", action="store_true")
    g.add_argument("--spawn", action="store_true")
    g.add_argument("--stop", action="store_true")
    ap.add_argument("--project", default=".")
    ap.add_argument("--launched-by", default=None)
    ap.add_argument("--timeout", type=float, default=None)
    ap.add_argument("--wait-lock", type=float, default=0.0)
    ap.add_argument("--no-jev", action="store_true")
    a = ap.parse_args(argv)
    if a.spawn:
        spawn_background(a.project, a.launched_by)
        return 0
    if a.stop:
        return 0 if stop_worker(a.project) else 1
    return run_worker(a.project, "once" if a.once else "daemon", launched_by=a.launched_by, use_jev=not a.no_jev,
                      timeout_s=a.timeout, wait_lock_s=a.wait_lock)


if __name__ == "__main__":
    sys.exit(main())
