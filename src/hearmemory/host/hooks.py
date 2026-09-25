"""Central hook entry point. `hearmemory hook <host> <event>` (CLI/MCP CLI) calls
`run_hook(host, event, stdin_bytes, root)`.

This function must NEVER raise and NEVER block past its budget: a missing/deleted
`.hearmemory`, an event whose cwd is outside the project, an unknown host/event, a broken core/memory/judge
dependency, a malformed stdin payload, or a slow step all degrade to `HookResult(exit_code=0)`
rather than surfacing as an error to the agent. The single non-zero exit is git's own hold_once /
block, which is expressed by whoever calls this (the git dispatcher script checks the CLI's
own exit code, not this function's return value's JSON).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from hearmemory import interfaces as I
from hearmemory.host import _deps


def _load_payload(stdin_bytes: Any) -> Dict[str, Any]:
    if stdin_bytes is None:
        return {}
    if isinstance(stdin_bytes, (bytes, bytearray)):
        try:
            stdin_bytes = stdin_bytes.decode("utf-8")
        except Exception:
            return {}
    if not stdin_bytes:
        return {}
    try:
        data = json.loads(stdin_bytes)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def run_hook(host: str, event: str, stdin_bytes: Any, root: Optional[str] = None) -> "I.HookResult":
    if os.environ.get("HEARMEMORY_DISABLE") == "1":
        return I.HookResult(exit_code=0)
    payload = _load_payload(stdin_bytes)

    try:
        proj_root = Path(root).resolve() if root else Path(payload.get("cwd") or ".").resolve()
    except Exception:
        return I.HookResult(exit_code=0)

    try:
        version_ok = (proj_root / I.HEARMEMORY_DIRNAME / "VERSION").is_file()
    except OSError:
        version_ok = False
    if not version_ok:
        return I.HookResult(exit_code=0)  # < 50 ms, silent, never creates .hearmemory

    payload_cwd = payload.get("cwd")
    if payload_cwd:
        try:
            cwd_resolved = Path(payload_cwd).resolve()
            if proj_root != cwd_resolved and proj_root not in cwd_resolved.parents:
                return I.HookResult(exit_code=0)  # event happened outside this project
        except Exception:
            pass

    from hearmemory.host import ADAPTERS
    adapter = ADAPTERS.get(host)
    if adapter is None or event not in I.HOOK_EVENTS.get(host, ()):
        return I.HookResult(exit_code=0)

    try:
        cfg = _deps.load_config(proj_root) if _deps.load_config else dict(I.DEFAULT_CONFIG)
    except Exception:
        cfg = dict(I.DEFAULT_CONFIG)

    payload = dict(payload)
    payload["_root"] = proj_root
    payload["_cfg"] = cfg
    payload["project"] = str(proj_root)

    try:
        return adapter.handle_hook(event, payload)
    except Exception:
        return I.HookResult(exit_code=0)


REFUSAL_DECISIONS = ("hold", "block")


def is_refusal(decision: Optional[str]) -> bool:
    """Only a `hold` or `block` CheckResult refuses a commit; `warn` (e.g. a non-blocking open
    issue) is advisory in every mode, exactly like `hearmemory check` reports it ("Warning only")."""
    return decision in REFUSAL_DECISIONS


def _deadline_for(host: str, event: str, hooks_cfg: Mapping[str, Any]):
    profile = I.HOOK_PROFILES.get(f"{host}:{event}")
    if profile is None:
        return None, None
    cls = _deps.Deadline or _deps.FallbackDeadline
    return profile, cls(profile, hooks_cfg)


def generic_handle_hook(host: str, event: str, payload: Mapping[str, Any]) -> "I.HookResult":
    """Shared time-budgeted orchestration for the events that are not git's own pre-commit (which
    git.py handles end to end itself). Used by claude.py/codex.py/cursor.py."""
    root = payload["_root"]
    cfg = payload["_cfg"]
    profile, deadline = _deadline_for(host, event, cfg.get("hooks", {}))
    if profile == "precommit":
        return _handle_precommit(host, event, payload, deadline)
    if profile == "session_start":
        return _handle_session_start(host, event, payload, deadline)
    if profile in ("record", "push", "import"):
        return _handle_record(host, event, payload, deadline, push=(profile == "push"))
    return I.HookResult(exit_code=0)


def _split_items(items):
    obs = [x for x in items if isinstance(x, I.Observation)]
    evs = [x for x in items if isinstance(x, I.ControlEvent)]
    return obs, evs


def _append_and_spawn(root, host: str, event: str, obs, evs) -> None:
    store = _deps.open_store(root) if _deps.open_store else None
    if store is not None:
        try:
            if obs:
                store.append_observations(obs)
            if evs:
                store.append_events(evs)
        except Exception:
            pass
    if _deps.spawn_worker:
        try:
            _deps.spawn_worker(root, launched_by=f"hook:{host}:{event}")
        except Exception:
            pass


def _handle_record(host: str, event: str, payload: Mapping[str, Any], deadline, *, push: bool) -> "I.HookResult":
    from hearmemory.host import ADAPTERS
    root, cfg = payload["_root"], payload["_cfg"]
    try:
        items = ADAPTERS[host].normalize(event, payload)
    except Exception:
        items = []
    obs, evs = _split_items(items)
    _append_and_spawn(root, host, event, obs, evs)

    stdout = ""
    if push and host == "claude" and event == "UserPromptSubmit":
        if (cfg.get("brief") or {}).get("push_on_prompt", True):
            text = _maybe_push_text(root, cfg, payload)
            if text:
                stdout = json.dumps({"hookSpecificOutput": {"hookEventName": event,
                                                            "additionalContext": text}})
    elif push and host == "claude" and event == "SubagentStart":
        # a starting subagent gets the ~300-token subagent brief as additionalContext,
        # so parallel subagents share what the project memory already knows.
        text = _maybe_push_text(root, cfg, payload, purpose="subagent_start")
        if text:
            stdout = json.dumps({"hookSpecificOutput": {"hookEventName": event,
                                                        "additionalContext": text}})
    return I.HookResult(exit_code=0, stdout=stdout, observations=[o.id for o in obs])


def _maybe_push_text(root, cfg, payload: Mapping[str, Any], purpose: str = "push") -> str:
    if _deps.open_store is None or _deps.load_memory is None or _deps.build_brief is None:
        return ""
    try:
        bcfg = cfg.get("brief") or {}
        if purpose == "subagent_start":
            max_tokens = int(bcfg.get("subagent_tokens", 300))
        else:
            max_tokens = int(bcfg.get("push_tokens", 200))
        store = _deps.open_store(root)
        state = _deps.load_memory(store, cfg, allow_rebuild=False)
        ctx = I.AgentContext(host="claude", session_id=payload.get("session_id"),
                             subagent_id=payload.get("agent_id") if purpose == "subagent_start" else None)
        req = I.BriefRequest(context=ctx, purpose=purpose, max_tokens=max_tokens,
                             lang=bcfg.get("lang", "en"))
        brief = _deps.build_brief(state, store, req, cfg)
        return brief.text
    except Exception:
        return ""


def _handle_session_start(host: str, event: str, payload: Mapping[str, Any], deadline) -> "I.HookResult":
    from hearmemory.host import ADAPTERS
    root, cfg = payload["_root"], payload["_cfg"]
    try:
        items = ADAPTERS[host].normalize(event, payload)
    except Exception:
        items = []
    obs, evs = _split_items(items)
    store = _deps.open_store(root) if _deps.open_store else None
    if store is not None:
        try:
            if obs:
                store.append_observations(obs)
            if evs:
                store.append_events(evs)
        except Exception:
            pass
        if host in ("claude", "codex"):
            # bounded to the profile's import_codex slice (700 ms by default); a large Codex
            # history is imported a slice at a time here and finished by the background worker,
            # so the brief below is always produced well inside the host's hook timeout.
            try:
                slice_ms = deadline.slice_ms("import_codex") if deadline is not None else 0.0
                if slice_ms > 0:
                    from hearmemory.host.codex import bounded_import
                    bounded_import(store, cfg, slice_ms / 1000.0)
            except Exception:
                pass

    brief_text = ""
    if store is not None and _deps.load_memory is not None and _deps.build_brief is not None:
        try:
            state = _deps.load_memory(store, cfg, allow_rebuild=False)
            ctx = I.AgentContext(host=host, session_id=payload.get("session_id") or
                                 payload.get("conversation_id"))
            req = I.BriefRequest(context=ctx, purpose="session_start",
                                 max_tokens=int((cfg.get("brief") or {}).get("session_start_tokens", 600)),
                                 lang=(cfg.get("brief") or {}).get("lang", "en"))
            brief = _deps.build_brief(state, store, req, cfg)
            brief_text = brief.text
        except Exception:
            brief_text = ""

    if _deps.spawn_worker:
        try:
            _deps.spawn_worker(root, launched_by=f"hook:{host}:{event}")
        except Exception:
            pass

    stdout = ""
    if brief_text:
        if host == "claude":
            stdout = json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                        "additionalContext": brief_text}})
        elif host == "cursor":
            # Cursor's documented sessionStart output field is snake_case `additional_context`.
            stdout = json.dumps({"additional_context": brief_text})
        else:
            stdout = brief_text
    return I.HookResult(exit_code=0, stdout=stdout, observations=[o.id for o in obs])


def _handle_precommit(host: str, event: str, payload: Mapping[str, Any], deadline) -> "I.HookResult":
    from hearmemory.host.git import is_git_commit_command
    root, cfg = payload["_root"], payload["_cfg"]
    precommit_cfg = cfg.get("precommit") or {}

    if host == "claude":
        if payload.get("tool_name") != "Bash":
            return I.HookResult(exit_code=0)
        command = (payload.get("tool_input") or {}).get("command", "")
        if not is_git_commit_command(command):
            return I.HookResult(exit_code=0)
        mode = precommit_cfg.get("claude_mode", "warn")
    else:  # codex PreToolUse (experimental)
        ti = payload.get("tool_input")
        command = ti.get("command", "") if isinstance(ti, dict) else str(ti or "")
        if not is_git_commit_command(command):
            return I.HookResult(exit_code=0)
        mode = precommit_cfg.get("codex_mode", "warn")

    if mode == "off":
        return I.HookResult(exit_code=0)

    decision, text = "allow", ""
    if _deps.open_store and _deps.load_memory and _deps.mem_check:
        try:
            store = _deps.open_store(root)
            state = _deps.load_memory(store, cfg, allow_rebuild=False)
            ctx = I.AgentContext(host=host, session_id=payload.get("session_id"))
            req = I.CheckRequest(context=ctx, action="git_commit", payload_text=command, mode=mode)
            result = _deps.mem_check(state, store, req, cfg)
            decision, text = result.decision, result.text
        except Exception:
            decision, text = "allow", ""

    if host == "claude":
        if mode == "warn" or not is_refusal(decision):
            # allow / warn (incl. non-blocking open issues under hold_once|block): context only.
            stdout = (json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                          "additionalContext": text}}) if text else "")
            return I.HookResult(exit_code=0, stdout=stdout)
        # decision hold/block: NEVER emit permissionDecision: allow; express refusal only.
        return I.HookResult(exit_code=0, stdout=json.dumps(
            {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                    "permissionDecisionReason": text}}))
    return I.HookResult(exit_code=0, stderr=text)
