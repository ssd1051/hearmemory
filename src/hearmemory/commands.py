"""CLI command handlers.

`COMMANDS` maps every `hearmemory` subcommand *except* `hook` (owned by host,
dispatched directly by the core's cli.py to `hearmemory.host.hooks.run_hook`) to a
handler `(args, ctx) -> int` exit code.

`ctx` is built by the core's `cli.py`::

    ctx = {"root": Path, "store": StoreAPI | None, "config": dict,
           "json": bool, "quiet": bool}

`ctx["store"]` is `None` when `<root>/.hearmemory` does not exist (or is not a
recognised STORE_FORMAT) for every command except `init` (which creates it)
and `mcp` (which must start even without `.hearmemory`).

Cross-module dependency rule
---------------------------------------------------
This module must import cleanly even when none of those modules exist yet
(a partial install or a broken environment). So it never imports them at
module scope; every call goes through `_entry(name)`, which resolves
`interfaces.ENTRY_POINTS[name]` ("pkg.mod:attr") via `importlib` *at call
time*. Tests substitute fake implementations by putting a fake module object
into `sys.modules["pkg.mod"]` before invoking a handler (see
`tests/test_iface_commands.py`); production code, once the other modules
land, needs no changes here.

The one exception is `hearmemory.mcp_server`, which is the CLI/MCP layer's own module: it is
imported normally.
"""
from __future__ import annotations

import argparse
import shlex
import importlib
import json as _json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from hearmemory.interfaces import (
    ACTOR_WILDCARD,
    AgentContext,
    BriefRequest,
    CHECK_ACTIONS,
    CheckRequest,
    ControlEvent,
    EVENT_SCHEMA,
    EXIT_BLOCKED,
    EXIT_NOT_INITIALISED,
    EXIT_OK,
    EXIT_USAGE,
    ENTRY_POINTS,
    ISSUE_KINDS,
    LAYOUT,
    RAW_FILES,
    OPEN_ISSUE_STATUSES,
    PRECOMMIT_MODES,
    RECORD_ECHO_PREFIX,
    RecallQuery,
    canonical_json,
    issue_id_for,
    stable_id,
)
from hearmemory import render_cli

__all__ = ["COMMANDS", "do_record", "do_recall", "do_check", "do_issues", "do_status", "do_doctor"]


# ---------------------------------------------------------------------------
# Cross-module lazy resolution (see module docstring)
# ---------------------------------------------------------------------------
def _entry(name: str):
    """Resolve `interfaces.ENTRY_POINTS[name]` ("pkg.mod:attr") on demand."""
    target = ENTRY_POINTS[name]
    mod_name, _, attr = target.partition(":")
    mod = importlib.import_module(mod_name)
    return getattr(mod, attr)


def _entry_or(name: str, default):
    """Like `_entry` but returns `default` instead of raising when the
    owning module is not built yet (used for genuinely optional steps, e.g.
    `store.merge_spool()` in `doctor --repair`, never for a command's core
    behaviour)."""
    try:
        return _entry(name)
    except (ImportError, AttributeError, KeyError):
        return default


def _now_iso() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


# ---------------------------------------------------------------------------
# manual-record provenance guessing (the CLI/MCP layer's job, not the host layer's: this is
# `hearmemory record` / `hearmemory_record` specifically, not a host hook).
# ---------------------------------------------------------------------------
def guess_cli_host(environ: Mapping[str, str]) -> str:
    """Best-effort guess of which agent host a bare `hearmemory` CLI invocation is
    running under. Wrong guesses only affect whether a wildcard actor
    is used; they never merge two real agents into one."""
    sid = environ.get("HEARMEMORY_SESSION_ID", "") or ""
    if sid.startswith("codex-"):
        # set by hearmemory's Codex launch.sh; Codex started from a Claude Code terminal also inherits
        # CLAUDECODE=1, which made a Codex `hearmemory record` a "claude" record
        return "codex"
    if environ.get("CLAUDECODE") == "1":
        return "claude"
    if any(k.startswith("CODEX_SANDBOX") for k in environ):
        return "codex"
    if environ.get("CURSOR_TRACE_ID") or environ.get("CURSOR_AGENT"):
        return "cursor"
    return "cli"


def cli_session_id(explicit: Optional[str], environ: Mapping[str, str]) -> str:
    if explicit:
        return explicit
    if environ.get("HEARMEMORY_SESSION_ID"):
        return environ["HEARMEMORY_SESSION_ID"]
    return f"cli-{uuid.uuid4()}"


def mcp_session_id(environ: Mapping[str, str]) -> str:
    if environ.get("HEARMEMORY_SESSION_ID"):
        return environ["HEARMEMORY_SESSION_ID"]
    return f"mcp-{os.getpid()}-{int(time.time())}"


def _normalize_title(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


# ---------------------------------------------------------------------------
# Shared core logic (used by both the CLI handlers below and mcp_server.py)
# ---------------------------------------------------------------------------
def _load_state(store, cfg, *, allow_rebuild: bool = True, deadline_s: Optional[float] = None):
    load_memory = _entry("load_memory")
    return load_memory(store, cfg, allow_rebuild=allow_rebuild, deadline_s=deadline_s)


def _maybe_run_pipeline(store, cfg, wait_s: Optional[float], root=None) -> None:
    """`--wait S`: best-effort synchronous judging before recall/check render,
    never raises, never blocks longer than `wait_s`.

    Without `--wait`: when NO worker is alive (the launch.sh-spawned one exited, or never
    started), run one bounded pass here (`worker.inline_judge_s`, default 2.5 s) and spawn a worker, so a
    recall/check never silently shows a memory that nobody is judging."""
    if wait_s and wait_s > 0:
        try:
            run_pipeline = _entry("run_pipeline")
            run_pipeline(store, cfg, deadline_s=float(wait_s), use_jev=True, mode="once")
        except Exception:
            pass
        return
    if root is None:
        return
    status_fn = _entry_or("worker_status", None)
    if status_fn is None:
        return
    try:
        if status_fn(root).get("state") != "stopped":
            return
        budget = float((cfg.get("worker") or {}).get("inline_judge_s", 2.5) or 0)
        if budget > 0:
            _entry("run_pipeline")(store, cfg, deadline_s=budget, use_jev=True, mode="once")
        _entry("spawn_worker")(root, launched_by="inline:recall")
    except Exception:
        pass


def _codex_sync(store, cfg, host: str, session_id: Optional[str], budget_s: float = 1.0) -> None:
    """Codex has no per-tool hooks usable without global config: before a Codex agent's
    record / recall / check, import its pending rollout lines (bounded, skipped while the worker holds the
    pipeline) and reconcile its proxy session id (launch.sh HEARMEMORY_SESSION_ID) with the rollout session."""
    if host != "codex":
        return
    fn = _entry_or("codex_sync", None)
    if fn is None:
        return
    try:
        fn(store, cfg, session_id, budget_s=budget_s)
    except Exception:
        pass


def native_session_id(store, host: Optional[str], session_id: Optional[str]) -> Optional[str]:
    """The host-native session a proxy session id is an alias of (session_alias events), else unchanged.
    Used for the requester identity so a Codex agent's own imported runs are never 'from other agents'."""
    if not host or not session_id:
        return session_id
    target = f"{host}:{session_id}"
    try:
        for ev in store.iter_events():
            if ev.kind == "session_alias" and ev.target == target and ev.provenance is not None \
                    and ev.provenance.session_id:
                return ev.provenance.session_id
    except Exception:
        pass
    return session_id


def do_record(root, store, cfg, *, text: str, kind: str = "note", paths: Optional[Sequence[str]] = None,
              refs: Optional[Sequence[str]] = None, host: str, session_id: str, source: str,
              agent_label: Optional[str] = None, key: Optional[str] = None) -> Dict[str, Any]:
    """manual `note`/`claim`/`issue` record. Returns {"obs_id": ..., "issue_id": Optional[str]}."""
    if kind not in ("note", "claim", "issue"):
        raise ValueError(f"invalid record kind: {kind!r}")
    make_observation = _entry("make_observation")
    capture_provenance = _entry("capture_provenance")
    # import what this Codex session did BEFORE the record, so its evidence precedes the claim
    _codex_sync(store, cfg, host, session_id)
    prov = capture_provenance(root, host, session_id=session_id, agent_label=agent_label, source=source)
    obs_kind = "note" if kind == "issue" else kind
    event_key = f"cli:{key}" if key else f"cli:{uuid.uuid4()}"
    meta = {"paths": list(paths)} if paths else None
    obs = make_observation(root, cfg, obs_kind, text, prov, event_key=event_key, refs=list(refs or []), meta=meta)
    store.append_observations([obs])

    issue_id: Optional[str] = None
    if kind == "issue":
        issue_id = issue_id_for("manual", _normalize_title(text))
        ev = ControlEvent(id=stable_id("e-", "issue_open", issue_id, obs.id), ts=obs.ts, kind="issue_open",
                           target=issue_id, data={"obs_id": obs.id, "title": text.strip(), "paths": list(paths or [])},
                           provenance=prov)
        store.append_events([ev])

    try:
        _entry("spawn_worker")(root, launched_by=f"cli:record")
    except Exception:
        pass
    return {"obs_id": obs.id, "issue_id": issue_id}


def do_recall(root, store, cfg, *, query: str = "", limit: int = 8, include_archive: bool = False,
              brief: bool = False, paths: Optional[Sequence[str]] = None, host: str, session_id: Optional[str] = None,
              purpose: str = "manual", lang: Optional[str] = None, wait_s: Optional[float] = None,
              deadline_s: Optional[float] = None, source: Optional[str] = None):
    """`source`: "mcp" / "cli" when called through hearmemory's own MCP server / CLI (the memory_shown
    event then names a proxy session, resolved like a proxy record)."""
    _codex_sync(store, cfg, host, session_id)
    session_id = native_session_id(store, host, session_id)
    _maybe_run_pipeline(store, cfg, wait_s, root=root)
    state = _load_state(store, cfg, allow_rebuild=True, deadline_s=deadline_s)
    actx = AgentContext(host=host, session_id=session_id, paths=list(paths or []), query_text=query or "")
    if brief:
        req = BriefRequest(context=actx, purpose=purpose, max_tokens=int(cfg["brief"]["session_start_tokens"]),
                            lang=lang or cfg["brief"]["lang"])
        build_brief = _entry("build_brief")
        try:
            return build_brief(state, store, req, cfg, shown_source=source)
        except TypeError:
            return build_brief(state, store, req, cfg)
    q = RecallQuery(query=query or "", context=actx, limit=int(limit), include_archive=bool(include_archive))
    recall = _entry("recall")
    out = recall(state, store, q, cfg)
    try:
        from hearmemory.memory.session import record_shown_event
        record_shown_event(store, actx, [cid for it in (getattr(out, "items", None) or []) for cid in it.claim_ids],
                           "recall", source)
    except Exception:
        pass
    return out


def do_restore(root, store, cfg, *, target: str, host: str, session_id: Optional[str] = None) -> None:
    capture_provenance = _entry("capture_provenance")
    prov = capture_provenance(root, host, session_id=session_id, source="cli")
    ev = ControlEvent(id=stable_id("e-", "archive_restore", target, _now_iso()), ts=_now_iso(),
                       kind="archive_restore", target=target, provenance=prov)
    store.append_events([ev])


def do_check(root, store, cfg, *, text: Optional[str] = None, staged: bool = False, message: Optional[str] = None,
             paths: Optional[Sequence[str]] = None, mode: Optional[str] = None, host: str,
             session_id: Optional[str] = None, action: Optional[str] = None, attempt_key: Optional[str] = None,
             wait_s: Optional[float] = None, deadline_s: Optional[float] = None):
    if action is None:
        action = "git_commit" if staged else ("claim" if text else "finish")
    if action not in CHECK_ACTIONS:
        raise ValueError(f"invalid check action: {action!r}")
    if mode is None:
        mode = "warn"
    if mode not in PRECOMMIT_MODES:
        raise ValueError(f"invalid check mode: {mode!r}")
    payload_text = text or ""
    if staged:
        diff = _staged_diff_excerpt(root)
        payload_text = "\n".join(p for p in (message, diff) if p)
    _codex_sync(store, cfg, host, session_id)
    session_id = native_session_id(store, host, session_id)
    _maybe_run_pipeline(store, cfg, wait_s, root=root)
    state = _load_state(store, cfg, allow_rebuild=True, deadline_s=deadline_s)
    actx = AgentContext(host=host, session_id=session_id, paths=list(paths or []))
    req = CheckRequest(context=actx, action=action, payload_text=payload_text, paths=list(paths or []), mode=mode,
                        attempt_key=attempt_key)
    check = _entry("check")
    return check(state, store, req, cfg)


def _staged_diff_excerpt(root, max_chars: int = 4000) -> str:
    try:
        import subprocess
        out = subprocess.run(["git", "diff", "--cached"], cwd=str(root), capture_output=True, text=True, timeout=3)
        text = out.stdout or ""
    except Exception:
        return ""
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n...[hearmemory: {len(text) - max_chars} chars truncated]..."
    return text


def do_issues(root, store, cfg, *, action: str = "list", issue_id: Optional[str] = None,
              title: Optional[str] = None, reason: Optional[str] = None, show_all: bool = False,
              paths: Optional[Sequence[str]] = None, host: str, session_id: Optional[str] = None):
    if action == "list":
        state = _load_state(store, cfg, allow_rebuild=True)
        items = list(state.issues.values())
        if not show_all:
            items = [i for i in items if i.status in OPEN_ISSUE_STATUSES]
        return {"issues": items}
    if action == "show":
        state = _load_state(store, cfg, allow_rebuild=True)
        issue = state.issues.get(issue_id)
        return {"issue": issue}
    if action == "open":
        return do_record(root, store, cfg, text=title or "", kind="issue", paths=paths, host=host,
                          session_id=session_id, source="cli")
    if action in ("close", "reopen"):
        capture_provenance = _entry("capture_provenance")
        prov = capture_provenance(root, host, session_id=session_id, source="cli")
        kind = "issue_close" if action == "close" else "issue_reopen"
        data = {"reason": reason} if reason else {}
        ev = ControlEvent(id=stable_id("e-", kind, issue_id, _now_iso()), ts=_now_iso(), kind=kind,
                           target=issue_id, data=data, provenance=prov)
        store.append_events([ev])
        return {"issue_id": issue_id, "action": action}
    raise ValueError(f"invalid issues action: {action!r}")


def do_status(root, store, cfg, *, verbose: bool = False) -> Dict[str, Any]:
    counts = {"observations": 0, "claims": 0, "candidates_by_status": {}, "judgments_by_provider": {}, "events": 0}
    for _, _obs in store.iter_observations():
        counts["observations"] += 1
    for _c in store.iter_claims():
        counts["claims"] += 1
    # Candidate itself carries no status (interfaces.Candidate has none); per-candidate status
    # lives in the judge worker's queue (state/queue.json). Fall back to "pending" for any
    # candidate id the queue does not (yet) know about, and never let a missing/corrupt queue
    # crash `hearmemory status` (status must never break the agent).
    try:
        queue_state = store.read_state("queue") or {}
    except Exception:
        queue_state = {}
    queue_entries = queue_state.get("entries") or {}
    n_candidates = 0
    for cand in store.iter_candidates():
        n_candidates += 1
        entry = queue_entries.get(cand.candidate_id) or {}
        status = entry.get("status", "pending")
        counts["candidates_by_status"][status] = counts["candidates_by_status"].get(status, 0) + 1
    counts["candidates"] = n_candidates
    for j in store.iter_judgments():
        counts["judgments_by_provider"][j.provider] = counts["judgments_by_provider"].get(j.provider, 0) + 1
    for _e in store.iter_events():
        counts["events"] += 1

    issues_open = 0
    try:
        state = _load_state(store, cfg, allow_rebuild=False)
        issues_open = sum(1 for i in state.issues.values() if i.status in OPEN_ISSUE_STATUSES)
    except Exception:
        state = None

    budget_info: Dict[str, Any] = {}
    try:
        Budget = _entry("budget")
        b = Budget(store, cfg)
        bs = b.status()
        budget_info = bs.to_dict() if hasattr(bs, "to_dict") else dict(bs)
    except Exception:
        pass

    jev_capable, jev_reason = False, "no_sdk"
    try:
        jev_capability = _entry("jev_capability")
        jev_capable, jev_reason = jev_capability(cfg, os.environ)
    except Exception:
        pass

    worker_info: Optional[Dict[str, Any]] = None
    status_fn = _entry_or("worker_status", None)
    try:
        if status_fn is not None:
            # liveness is checked (lock + pid + command line), never inferred from worker.json
            worker_info = status_fn(root).get("liveness")
        else:
            worker_info = store.read_state("worker")
    except Exception:
        worker_info = None

    # Jev runs in the WORKER, not in this shell. A shell without the key used to print
    # "unavailable (no_key)" while the live worker was making real judgments; report the live worker's own state
    # (worker.json, refreshed every beat) and this shell's only as a labelled fallback / extra.
    jev_info: Dict[str, Any] = {"capable": bool(jev_capable), "reason": jev_reason, "source": "this_shell"}
    if isinstance(worker_info, dict) and worker_info.get("state") == "running" \
            and worker_info.get("jev_capable") is not None:
        wj = worker_info.get("jev") if isinstance(worker_info.get("jev"), dict) else {}
        w_capable = bool(worker_info.get("jev_capable"))
        jev_info = {"capable": w_capable,
                    "reason": None if w_capable else (wj.get("reason") or worker_info.get("jev_unavailable_reason")),
                    "source": "worker", "worker_pid": worker_info.get("pid"), "model": wj.get("model"),
                    "calls_today": wj.get("calls_today"), "last_call_ts": wj.get("last_call_ts"),
                    "this_shell": {"capable": bool(jev_capable), "reason": jev_reason}}

    result: Dict[str, Any] = {
        "initialised": True,
        "counts": counts,
        "issues_open": issues_open,
        "budget": budget_info,
        "jev": jev_info,
        "worker": worker_info,
        "memory_as_of": getattr(state, "built_ts", None) if state is not None else None,
    }
    if verbose:
        try:
            extract_index = store.read_state("extract_index") or {}
            result["extract_dropped"] = extract_index.get("dropped", {})
        except Exception:
            result["extract_dropped"] = {}
    return result


def do_doctor(root, store, cfg, *, repair: bool = False) -> Dict[str, Any]:
    report: Dict[str, Any] = {"initialised": store is not None, "problems": [], "repairs": []}
    if store is None:
        report["problems"].append("`.hearmemory` is not initialised or its VERSION is unrecognised; run `hearmemory init`.")
        return report
    try:
        report["is_initialised"] = bool(store.is_initialised())
    except Exception as e:
        report["problems"].append(f"is_initialised() failed: {e}")
    try:
        jev_capability = _entry("jev_capability")
        capable, reason = jev_capability(cfg, os.environ)
        report["jev"] = {"capable": capable, "reason": reason}
    except Exception:
        report["jev"] = {"capable": False, "reason": "unknown"}
    try:
        status_fn = _entry_or("worker_status", None)
        report["worker"] = status_fn(root).get("liveness") if status_fn is not None else store.read_state("worker")
    except Exception:
        report["worker"] = None
    # corrupt/duplicate raw lines are skipped-and-counted on read, never surfaced anywhere
    # except here. Fully draining each raw iterator is what populates the real Store's
    # `last_read_stats` (see hearmemory.store._iter_raw); a store implementation without it (e.g. a
    # test fake) is simply skipped, never a crash.
    try:
        for _ in store.iter_observations():
            pass
        for _ in store.iter_claims():
            pass
        for _ in store.iter_candidates():
            pass
        for _ in store.iter_judgments():
            pass
        for _ in store.iter_events():
            pass
        read_stats = getattr(store, "last_read_stats", None) or {}
        for key, stats in read_stats.items():
            corrupt = int((stats or {}).get("corrupt_lines", 0) or 0)
            dup = int((stats or {}).get("duplicate_ids", 0) or 0)
            if corrupt:
                report["problems"].append(f"{LAYOUT.get(key, key)}: {corrupt} corrupt/unreadable line(s) skipped")
            if dup:
                report["problems"].append(f"{LAYOUT.get(key, key)}: {dup} duplicate id(s) skipped (first wins)")
    except Exception:
        pass
    # no rotation, just a size hint past 64 MB.
    try:
        hearmemory_dir = getattr(store, "hearmemory_dir", None)
        if hearmemory_dir is not None:
            for key in RAW_FILES:
                p = Path(hearmemory_dir) / LAYOUT[key]
                try:
                    size = p.stat().st_size
                except OSError:
                    continue
                if size > 64 * 1024 * 1024:
                    report["problems"].append(f"{LAYOUT[key]} is {size // (1024 * 1024)} MB (no rotation yet)")
    except Exception:
        pass
    if repair:
        merge_spool = getattr(store, "merge_spool", None)
        if callable(merge_spool):
            try:
                n = merge_spool()
                report["repairs"].append(f"merged {n} spooled row(s)")
            except Exception as e:
                report["problems"].append(f"merge_spool failed: {e}")
        try:
            load_memory = _entry("load_memory")
            load_memory(store, cfg, allow_rebuild=True, deadline_s=None)
            report["repairs"].append("rebuilt derived state")
        except Exception as e:
            report["problems"].append(f"rebuild failed: {e}")
    return report


# ---------------------------------------------------------------------------
# CLI output helpers
# ---------------------------------------------------------------------------
def _print(ctx: Mapping[str, Any], text: str) -> None:
    if not ctx.get("quiet"):
        print(text)


def _emit(ctx: Mapping[str, Any], obj: Any, text: Optional[str] = None) -> None:
    """Print either the JSON form of `obj` (a Record, dict, list of Records) or `text`.
    `--json` output is never suppressed by `-q/--quiet` (quiet only trims human text;
    a caller using `--json` wants the machine-readable result regardless)."""
    if ctx.get("json"):
        print(canonical_json(_jsonable(obj)))
    elif text is not None:
        _print(ctx, text)


def _jsonable(obj: Any) -> Any:
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if isinstance(obj, (list, tuple)):
        return [_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    return obj


def _err(ctx: Mapping[str, Any], msg: str) -> None:
    print(f"hearmemory: {msg}", file=sys.stderr)


def _need_store(args: argparse.Namespace, ctx: Dict[str, Any]) -> bool:
    """Returns True (and prints a not-initialised message) when this command
    needs a store but `.hearmemory` is missing/unrecognised (EXIT_NOT_INITIALISED)."""
    if ctx.get("store") is None:
        if ctx.get("json"):
            print(canonical_json({"error": "not_initialised", "message": "run `hearmemory init` first"}))
        else:
            _err(ctx, "not initialised; run `hearmemory init` first")
        return True
    return False


# ---------------------------------------------------------------------------
# Command handlers: (args, ctx) -> exit code
# ---------------------------------------------------------------------------
def cmd_init(args: argparse.Namespace, ctx: Dict[str, Any]) -> int:
    root = ctx["root"]
    store = ctx.get("store")
    hearmemory_dir = Path(root) / ".hearmemory"
    from hearmemory.host.snippets import UnsafePathError, check_safe_path
    try:
        # refuse (before creating anything) a path that cannot be quoted safely into the
        # generated hook commands / launch scripts / TOML -- never report "initialised" for hooks
        # that cannot run.
        check_safe_path(str(root), "project path")
        check_safe_path(str(getattr(args, "python", None) or sys.executable), "Python interpreter path")
    except UnsafePathError as e:
        _err(ctx, str(e))
        return EXIT_USAGE
    if os.path.lexists(hearmemory_dir):
        real_c, real_r = os.path.realpath(hearmemory_dir), os.path.realpath(root)
        if not real_c.startswith(real_r.rstrip(os.sep) + os.sep):
            # Scope rule: a `.hearmemory` symlink pointing outside the project would make every
            # store/hook/config write land outside it. Refuse instead of writing through it.
            _err(ctx, f"{hearmemory_dir} resolves to {real_c}, outside the project {real_r}; "
                      f"hearmemory only writes inside the project. Remove the symlink and re-run.")
            return EXIT_USAGE
    if store is None or not store.is_initialised():
        # `open_store(create=True)` deliberately returns a bare *uninitialised* Store
        # for a not-yet-`hearmemory init`-ed root (only `create_store` may
        # actually create `.hearmemory`). `hearmemory init` is that one caller, so it must go
        # through `create_store` explicitly rather than assume `open_store` did it.
        create_store = _entry("create_store")
        store = create_store(root)
        if store is None or not store.is_initialised():
            _err(ctx, "failed to create .hearmemory (is the path writable?)")
            return EXIT_USAGE
        ctx["store"] = store
    load_config = _entry_or("load_config", None)
    cfg = load_config(root) if load_config else dict(ctx.get("config") or {})
    ctx["config"] = cfg

    hosts_arg = getattr(args, "hosts", None)
    if not hosts_arg or hosts_arg == "all":
        from hearmemory.interfaces import INSTALLABLE_HOSTS
        hosts_list = list(INSTALLABLE_HOSTS) if hosts_arg == "all" else list(cfg.get("hosts", {}).get("enabled", []))
    else:
        hosts_list = [h.strip() for h in hosts_arg.split(",") if h.strip()]
    if getattr(args, "no_git_hook", False) and "git" in hosts_list:
        hosts_list = [h for h in hosts_list if h != "git"]

    python = getattr(args, "python", None) or sys.executable
    install = _entry("install")
    kwargs = dict(python=python, force=bool(getattr(args, "force", False)),
                  claude_persist=bool(getattr(args, "claude_persist", False)),
                  force_hooks_path=bool(getattr(args, "force_hooks_path", False)))
    try:
        manifest = install(root, hosts_list, cfg, **kwargs)
    except TypeError:
        manifest = install(root, hosts_list, cfg)

    # [hosts] enabled must say what was actually installed (it used to keep the default
    # claude/codex/git while `--hosts claude,codex,cursor` had installed cursor files).
    installed = [h for h in hosts_list if any(getattr(r, "host", None) == h
                                             for r in (getattr(manifest, "records", []) or []))]
    set_value = _entry_or("set_config_value", None)
    if set_value is not None and installed:
        try:
            set_value(root, "hosts", "enabled", installed)
            cfg = load_config(root) if load_config else cfg
            ctx["config"] = cfg
        except Exception:
            pass

    n = len(getattr(manifest, "records", []) or [])
    _emit(ctx, manifest, text=f"hearmemory: initialised {root} ({n} file(s) installed for hosts: {', '.join(hosts_list)})")
    return EXIT_OK


def cmd_status(args: argparse.Namespace, ctx: Dict[str, Any]) -> int:
    if _need_store(args, ctx):
        return EXIT_NOT_INITIALISED
    info = do_status(ctx["root"], ctx["store"], ctx["config"], verbose=bool(getattr(args, "verbose", False)))
    _emit(ctx, info, text=render_cli.render_status(info))
    return EXIT_OK


def cmd_record(args: argparse.Namespace, ctx: Dict[str, Any]) -> int:
    if _need_store(args, ctx):
        return EXIT_NOT_INITIALISED
    host = guess_cli_host(os.environ)
    session_id = cli_session_id(getattr(args, "session", None), os.environ)
    result = do_record(ctx["root"], ctx["store"], ctx["config"], text=args.text,
                        kind=getattr(args, "kind", None) or "note", paths=getattr(args, "paths", None),
                        refs=getattr(args, "refs", None), host=host, session_id=session_id, source="cli",
                        agent_label=getattr(args, "agent_label", None), key=getattr(args, "key", None))
    if ctx.get("json"):
        print(canonical_json(result))
    else:
        _print(ctx, RECORD_ECHO_PREFIX + result["obs_id"])
    return EXIT_OK


def cmd_recall(args: argparse.Namespace, ctx: Dict[str, Any]) -> int:
    if _need_store(args, ctx):
        return EXIT_NOT_INITIALISED
    if getattr(args, "restore", None):
        do_restore(ctx["root"], ctx["store"], ctx["config"], target=args.restore,
                   host=guess_cli_host(os.environ), session_id=cli_session_id(getattr(args, "session", None), os.environ))
        _emit(ctx, {"restored": args.restore}, text=f"hearmemory: restored {args.restore}")
        return EXIT_OK
    host = guess_cli_host(os.environ)
    session_id = cli_session_id(getattr(args, "session", None), os.environ)
    out = do_recall(ctx["root"], ctx["store"], ctx["config"], query=getattr(args, "query", None) or "",
                     limit=getattr(args, "limit", None) or 8,
                     include_archive=bool(getattr(args, "include_archive", False)),
                     brief=bool(getattr(args, "brief", False)), paths=getattr(args, "paths", None), host=host,
                     session_id=session_id, wait_s=getattr(args, "wait", None), source="cli")
    _emit(ctx, out, text=getattr(out, "text", "") or render_cli.render_recall(out))
    return EXIT_OK


def cmd_check(args: argparse.Namespace, ctx: Dict[str, Any]) -> int:
    if _need_store(args, ctx):
        return EXIT_NOT_INITIALISED
    host = guess_cli_host(os.environ)
    session_id = cli_session_id(getattr(args, "session", None), os.environ)
    cfg = ctx["config"]
    mode = getattr(args, "mode", None) or cfg.get("precommit", {}).get(f"{host}_mode") if host in (
        "claude", "codex", "cursor", "git") else None
    mode = mode or "warn"
    result = do_check(ctx["root"], ctx["store"], cfg, text=getattr(args, "text", None),
                       staged=bool(getattr(args, "staged", False)), message=getattr(args, "message", None),
                       paths=getattr(args, "paths", None), mode=mode, host=host, session_id=session_id,
                       attempt_key=getattr(args, "ack", None), wait_s=getattr(args, "wait", None))
    _emit(ctx, result, text=result.text or render_cli.render_check(result))
    return EXIT_BLOCKED if result.decision in ("hold", "block") else EXIT_OK


def cmd_issues(args: argparse.Namespace, ctx: Dict[str, Any]) -> int:
    if _need_store(args, ctx):
        return EXIT_NOT_INITIALISED
    host = guess_cli_host(os.environ)
    session_id = cli_session_id(getattr(args, "session", None), os.environ)
    action = getattr(args, "issues_action", None) or "list"
    try:
        out = do_issues(ctx["root"], ctx["store"], ctx["config"], action=action,
                         issue_id=getattr(args, "id", None), title=getattr(args, "title", None),
                         reason=getattr(args, "reason", None), show_all=bool(getattr(args, "all", False)),
                         paths=getattr(args, "paths", None), host=host, session_id=session_id)
    except ValueError as e:
        _err(ctx, str(e))
        return EXIT_USAGE
    if action == "show" and out.get("issue") is None:
        _emit(ctx, {"error": "not_found", "id": getattr(args, "id", None)}, text=f"hearmemory: no such issue {args.id!r}")
        return EXIT_USAGE
    if action == "list":
        _emit(ctx, out["issues"], text=render_cli.render_issue_list(out["issues"]))
    elif action == "show":
        _emit(ctx, out["issue"], text=render_cli.render_issue(out["issue"]))
    elif action == "open":
        if ctx.get("json"):
            print(canonical_json(out))
        else:
            _print(ctx, RECORD_ECHO_PREFIX + out["obs_id"])
    else:
        _emit(ctx, out, text=f"hearmemory: issue {out.get('issue_id')} -> {action}")
    return EXIT_OK


def cmd_import(args: argparse.Namespace, ctx: Dict[str, Any]) -> int:
    if _need_store(args, ctx):
        return EXIT_NOT_INITIALISED
    source = getattr(args, "source", None) or "codex"
    if source != "codex":
        _err(ctx, f"unknown import source {source!r} (only 'codex' is supported)")
        return EXIT_USAGE
    import_codex = _entry("import_codex")
    kwargs: Dict[str, Any] = {}
    if getattr(args, "since", None):
        kwargs["since"] = args.since
    if getattr(args, "all_history", False):
        kwargs["all_history"] = True
    if getattr(args, "session", None):
        kwargs["session"] = args.session
    if getattr(args, "codex_home", None):
        kwargs["codex_home"] = args.codex_home
    if getattr(args, "dry_run", False):
        kwargs["dry_run"] = True
    dry_run = bool(kwargs.get("dry_run"))
    try:
        n = import_codex(ctx["store"], ctx["config"], **kwargs)
    except ValueError as e:                 # an unparsable --since
        _err(ctx, str(e))
        return EXIT_USAGE
    except TypeError:
        if dry_run:
            # an importer without dry-run support would really import: refuse instead of lying
            _err(ctx, "this build's codex importer does not support --dry-run")
            return EXIT_USAGE
        kwargs.pop("codex_home", None)
        n = import_codex(ctx["store"], ctx["config"], **kwargs)
    launch = getattr(args, "launch_session", None)
    aliased = None
    if launch and not dry_run:
        sync = _entry_or("codex_sync", None)
        if sync is not None:
            try:
                aliased = (sync(ctx["store"], ctx["config"], launch, budget_s=0.0, ended=True) or {}).get("alias")
            except Exception:
                aliased = None
    if dry_run:
        # a dry run must never claim it imported anything
        _emit(ctx, {"would_import": n, "dry_run": True},
              text=f"hearmemory: would import {n} observation(s) from codex (dry run, nothing written)")
        return EXIT_OK
    out: Dict[str, Any] = {"imported": n}
    text = f"hearmemory: imported {n} observation(s) from codex"
    if launch:
        out["launch_session"] = launch
        out["alias"] = aliased
        if aliased:
            text += f"; session {launch} = codex rollout session {aliased}"
    _emit(ctx, out, text=text)
    return EXIT_OK


def cmd_worker(args: argparse.Namespace, ctx: Dict[str, Any]) -> int:
    if _need_store(args, ctx):
        return EXIT_NOT_INITIALISED
    root, store, cfg = ctx["root"], ctx["store"], ctx["config"]
    if getattr(args, "stop", False):
        stop_worker = _entry("stop_worker")
        timeout_s = getattr(args, "timeout", None) or cfg.get("worker", {}).get("stop_timeout_s", 3.0)
        ok = stop_worker(root, timeout_s)
        _emit(ctx, {"stopped": bool(ok)}, text=f"hearmemory: worker {'stopped' if ok else 'was not running'}")
        return EXIT_OK
    if getattr(args, "spawn", False):
        spawn_worker = _entry("spawn_worker")
        ok = spawn_worker(root, launched_by=getattr(args, "launched_by", None) or "cli")
        _emit(ctx, {"spawned": bool(ok)}, text=f"hearmemory: worker {'spawned' if ok else 'not spawned'}")
        return EXIT_OK
    if getattr(args, "status", False):
        status_fn = _entry_or("worker_status", None)
        if status_fn is not None:
            try:
                live = status_fn(root).get("liveness") or {}
            except Exception:
                live = {}
            _emit(ctx, live, text=render_cli.render_worker_status(live))
            return EXIT_OK
        info = store.read_state("worker")
        _emit(ctx, info or {}, text=render_cli.render_worker_status(info))
        return EXIT_OK
    if getattr(args, "daemon", False):
        # `worker --daemon` (what spawn_background starts) used to run ONE pipeline pass and exit
        # without ever taking the worker lock or writing its pid ("worker: running (pid 0)", no judging).
        run_worker = _entry_or("run_worker", None)
        if run_worker is not None:
            return int(run_worker(root, "daemon", launched_by=getattr(args, "launched_by", None),
                                  use_jev=not bool(getattr(args, "no_jev", False)),
                                  timeout_s=getattr(args, "timeout", None),
                                  wait_lock_s=float(getattr(args, "wait_lock", None) or 0.0)) or 0)

    run_pipeline = _entry("run_pipeline")
    deadline_s = getattr(args, "timeout", None) or cfg.get("worker", {}).get("run_deadline_s", 30.0)
    use_jev = not bool(getattr(args, "no_jev", False))
    mode = "daemon" if getattr(args, "daemon", False) else "once"
    wait_lock_s = getattr(args, "wait_lock", None)
    deadline = time.monotonic() + float(wait_lock_s) if wait_lock_s else None
    while True:
        stats = run_pipeline(store, cfg, deadline_s=float(deadline_s), use_jev=use_jev, mode=mode)
        if not (isinstance(stats, dict) and stats.get("skipped") == "busy" and deadline is not None
                and time.monotonic() < deadline):
            break
        time.sleep(0.05)
    _emit(ctx, stats, text=f"hearmemory: worker ran ({stats})")
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace, ctx: Dict[str, Any]) -> int:
    if _need_store(args, ctx):
        return EXIT_NOT_INITIALISED
    report = do_doctor(ctx["root"], ctx["store"], ctx["config"], repair=bool(getattr(args, "repair", False)))
    _emit(ctx, report, text=render_cli.render_doctor(report))
    return EXIT_OK


def cmd_uninstall(args: argparse.Namespace, ctx: Dict[str, Any]) -> int:
    root = ctx["root"]
    purge = bool(getattr(args, "purge", False))
    if purge and not getattr(args, "yes", False):
        if ctx.get("quiet") or ctx.get("json"):
            _err(ctx, "uninstall --purge requires --yes in non-interactive mode")
            return EXIT_USAGE
        try:
            answer = input(f"This will permanently delete {root}/.hearmemory. Type 'yes' to continue: ")
        except EOFError:
            answer = ""
        if answer.strip().lower() != "yes":
            _err(ctx, "aborted")
            return EXIT_USAGE
    uninstall = _entry("uninstall")
    notes = uninstall(root, purge=purge)
    _emit(ctx, {"notes": list(notes)}, text="\n".join(["hearmemory: uninstalled"] + list(notes)))
    ctx["store"] = None
    return EXIT_OK


def cmd_rebuild(args: argparse.Namespace, ctx: Dict[str, Any]) -> int:
    if _need_store(args, ctx):
        return EXIT_NOT_INITIALISED
    store, cfg = ctx["store"], ctx["config"]
    if getattr(args, "reextract", False):
        try:
            run_pipeline = _entry("run_pipeline")
            run_pipeline(store, cfg, deadline_s=cfg.get("worker", {}).get("run_deadline_s", 30.0), use_jev=True,
                         mode="once")
        except Exception:
            pass
    load_memory = _entry("load_memory")
    state = load_memory(store, cfg, allow_rebuild=True, deadline_s=None)
    _emit(ctx, state, text=f"hearmemory: rebuilt memory (as of {getattr(state, 'built_ts', '?')}, "
                            f"{getattr(state, 'observation_count', '?')} observation(s))")
    return EXIT_OK


def cmd_mcp(args: argparse.Namespace, ctx: Dict[str, Any]) -> int:
    from hearmemory.mcp_server import serve
    host = getattr(args, "host", None)
    return serve(ctx["root"], host, sys.stdin, sys.stdout)


def cmd_host(args: argparse.Namespace, ctx: Dict[str, Any]) -> int:
    root = Path(ctx["root"])
    action = getattr(args, "host_action", None) or getattr(args, "action", None)
    if action == "claude-cmd":
        return _print_launch_script(ctx, root / ".hearmemory" / "host" / "claude" / "launch.sh", "claude")
    if action == "codex-cmd":
        return _print_launch_script(ctx, root / ".hearmemory" / "host" / "codex" / "launch.sh", "codex")
    if action == "cursor-status":
        mcp_json = root / ".cursor" / "mcp.json"
        hooks_json = root / ".cursor" / "hooks.json"
        info = {"mcp_json": mcp_json.exists(), "hooks_json": hooks_json.exists()}
        _emit(ctx, info, text=render_cli.render_cursor_status(info))
        return EXIT_OK
    _err(ctx, f"unknown host action {action!r} (expected claude-cmd, codex-cmd or cursor-status)")
    return EXIT_USAGE


def _print_launch_script(ctx: Dict[str, Any], path: Path, host: str) -> int:
    """print ONE complete, pasteable command line (shlex.join of the real argv) -- never the
    `exec` line of launch.sh (that replaced the user's shell, and for Codex it was only the first of
    three backslash-continued lines). `sh .hearmemory/host/<host>/launch.sh` does the same plus starts the
    background worker."""
    if not path.exists():
        _err(ctx, f"no generated launch script for {host}; run `hearmemory init --hosts {host}` first")
        return EXIT_USAGE
    from hearmemory.host import snippets as S
    root = Path(ctx["root"])
    python = sys.executable
    try:
        from hearmemory.host.manifest import read_manifest
        m = read_manifest(root)
        if m is not None and getattr(m, "python", None):
            python = m.python
    except Exception:
        pass
    argv = S.codex_cmd_argv(python, str(root)) if host == "codex" else S.claude_cmd_argv(str(root))
    command = shlex.join(argv)
    rel = path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path)
    _emit(ctx, {"launch_sh": str(path), "argv": argv, "command": command,
                "launch_command": shlex.join(["sh", rel])}, text=command)
    return EXIT_OK


# ---------------------------------------------------------------------------
COMMANDS: Dict[str, Callable[[argparse.Namespace, Dict[str, Any]], int]] = {
    "init": cmd_init,
    "status": cmd_status,
    "record": cmd_record,
    "recall": cmd_recall,
    "check": cmd_check,
    "issues": cmd_issues,
    "import": cmd_import,
    "worker": cmd_worker,
    "doctor": cmd_doctor,
    "uninstall": cmd_uninstall,
    "rebuild": cmd_rebuild,
    "mcp": cmd_mcp,
    "host": cmd_host,
}
