"""Cursor host adapter. Opt-in only (`hearmemory init --hosts cursor`): install() writes
only inside the project's own `.cursor/` directory, never `~/.cursor`. Field names are Cursor's
documented hook payload shape; exact presence/absence may vary between Cursor versions and every normalize() function here tolerates missing fields.
"""
from __future__ import annotations

import difflib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from hearmemory import interfaces as I
from hearmemory.host import _deps, manifest as M, snippets as S
from hearmemory.host.git import is_git_commit_command

_DISCARD_FIELDS = ("user_email", "user_id", "email")


def _sid(payload: Mapping[str, Any]) -> Optional[str]:
    return payload.get("conversation_id")


def _provenance(payload: Mapping[str, Any], event: str) -> "I.Provenance":
    # generation_id changes every turn: it is meta only, never part of identity/actor.
    return I.Provenance(host="cursor", session_id=_sid(payload), subagent_id=None,
                        source=f"hook:{event}", cwd=(payload.get("workspace_roots") or [None])[0])


def _meta(payload: Mapping[str, Any]) -> Dict[str, Any]:
    return {"generation_id": payload.get("generation_id")}


def _event_key(payload: Mapping[str, Any], kind: str, extra: str = "", *more: str) -> str:
    # `more` is appended only when given, so keys of events that pass none stay as before.
    return I.stable_id("cursor-ek-", _sid(payload), kind, extra, payload.get("generation_id"), *more)


def _extract_obs_id(text: str) -> Optional[str]:
    if not text:
        return None
    try:
        data = json.loads(text)
        if isinstance(data, dict) and data.get("obs_id"):
            return str(data["obs_id"])
    except Exception:
        pass
    m = re.search(I.OBS_ID_RE, text)
    return m.group(0) if m else None


def normalize(event: str, payload: Mapping[str, Any]) -> List[Any]:
    cfg = payload.get("_cfg") or dict(I.DEFAULT_CONFIG)
    root = payload.get("_root")
    payload = {k: v for k, v in payload.items() if k not in _DISCARD_FIELDS}

    if event == "afterShellExecution":
        command = str(payload.get("command") or "")
        output = str(payload.get("output") or "")
        text = f"$ {command}\n{output}"
        exit_code = payload.get("exit_code")
        status = "unknown" if exit_code is None else ("ok" if exit_code == 0 else "error")
        summary = _deps.get_test_summary(command, output)
        tool = I.ToolInfo(name="afterShellExecution", command=command, exit_code=exit_code,
                          status=status, test=summary)
        # Cursor has no per-call id, and generation_id is shared by every call in one
        # turn, so a command re-run in the same turn (fail -> fix -> pass) must be told apart by
        # its result: output hash + exit code + duration are part of the key.
        ek = _event_key(payload, "shell", command, I.sha256_text(output), str(exit_code),
                        str(payload.get("duration") if payload.get("duration") is not None else ""))
        return [_deps.build_observation(root, cfg, "command", text, _provenance(payload, event), tool=tool,
                                        event_key=ek, meta=_meta(payload))]

    if event == "afterFileEdit":
        path = payload.get("file_path") or ""
        edits = payload.get("edits") or []
        old = "\n".join(e.get("old_string", "") for e in edits)
        new = "\n".join(e.get("new_string", "") for e in edits)
        diff = "\n".join(list(difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm=""))[:60])
        tool = I.ToolInfo(name="afterFileEdit", paths=[path] if path else [], status="ok")
        # repeated edits to one path in one turn get distinct keys (edits content hash).
        ek = _event_key(payload, "edit", path,
                        I.sha256_text(json.dumps(edits, sort_keys=True, ensure_ascii=False, default=str)))
        return [_deps.build_observation(root, cfg, "file_edit", diff, _provenance(payload, event), tool=tool,
                                        event_key=ek, meta=_meta(payload))]

    if event == "afterAgentResponse":
        text = str(payload.get("text") or "")
        ek = _event_key(payload, "msg", I.sha256_text(text))
        return [_deps.build_observation(root, cfg, "assistant_message", text, _provenance(payload, event),
                                        event_key=ek, meta=_meta(payload))]

    if event == "afterMCPExecution":
        if payload.get("tool_name") == "hearmemory_record":
            result = payload.get("result_json") or payload.get("result") or ""
            obs_id = _extract_obs_id(result if isinstance(result, str) else json.dumps(result))
            if obs_id:
                ev = I.ControlEvent(id=I.stable_id("ev-link-", obs_id, _sid(payload)), ts=_deps.now_ts(),
                                    kind="provenance_link", target=obs_id, data={},
                                    provenance=_provenance(payload, event))
                return [ev]
        return []

    if event == "sessionStart":
        ek = _event_key(payload, "start", _deps.now_ts())
        return [_deps.build_observation(root, cfg, "session_event", "session_event:sessionStart",
                                        _provenance(payload, event), event_key=ek, meta=_meta(payload))]

    if event == "stop":
        ek = _event_key(payload, "stop", str(payload.get("status") or ""))
        return [_deps.build_observation(root, cfg, "session_event",
                                        f"session_event:stop:{payload.get('status') or ''}",
                                        _provenance(payload, event), event_key=ek, meta=_meta(payload))]

    if event == "beforeShellExecution":
        return []  # not recorded; only used to gate the precommit check

    return []


def handle_hook(event: str, payload: Mapping[str, Any]) -> "I.HookResult":
    if event == "beforeShellExecution":
        return _handle_before_shell(payload)
    from hearmemory.host import hooks as _hooks
    return _hooks.generic_handle_hook("cursor", event, payload)


def _handle_before_shell(payload: Mapping[str, Any]) -> "I.HookResult":
    command = str(payload.get("command") or "")
    if not is_git_commit_command(command):
        return I.HookResult(exit_code=0, stdout=json.dumps({}))
    root = Path(payload.get("_root") or payload.get("project") or ".")
    cfg = payload.get("_cfg") or dict(I.DEFAULT_CONFIG)
    mode = (cfg.get("precommit") or {}).get("cursor_mode", "warn")
    if mode == "off":
        return I.HookResult(exit_code=0, stdout=json.dumps({}))

    store = _deps.open_store(root) if _deps.open_store else None
    decision, text = "allow", ""
    if store is not None and _deps.load_memory is not None and _deps.mem_check is not None:
        try:
            state = _deps.load_memory(store, cfg, allow_rebuild=False)
            ctx = I.AgentContext(host="cursor", session_id=_sid(payload))
            req = I.CheckRequest(context=ctx, action="git_commit", payload_text=command, mode=mode)
            result = _deps.mem_check(state, store, req, cfg)
            decision, text = result.decision, result.text
        except Exception:
            decision, text = "allow", ""

    from hearmemory.host.hooks import is_refusal
    if mode == "warn" or not is_refusal(decision):
        return I.HookResult(exit_code=0, stdout=json.dumps({}), stderr=text)
    # decision hold/block: never emit an "allow" decision ourselves (never bypass Cursor's own
    # approval); a blocking outcome is expressed with permission=deny.
    return I.HookResult(exit_code=0, stdout=json.dumps({"permission": "deny", "user_message": text,
                                                        "agent_message": text}))


# --------------------------------------------------------------------------- install
def install(root: Path, python: str, cfg: Mapping[str, Any], *,
            records: Optional[List["I.InstallRecord"]] = None) -> List["I.InstallRecord"]:
    """Writes only inside the project's own `.cursor/`. A `.cursor` (or any file in it) whose real
    path is outside the project -- e.g. a symlink to ~/.cursor -- is skipped with a warning: hearmemory
    never creates global Cursor hooks that would record OTHER projects into this one.
    `records` is appended to as each file is written (see claude.install)."""
    root = Path(root)
    records = [] if records is None else records

    for jpath, add in ((root / ".cursor" / "mcp.json",
                        {"mcpServers": {"hearmemory": S.cursor_mcp_entry(python, str(root))}}),
                       (root / ".cursor" / "hooks.json", S.cursor_hooks_json(python, str(root)))):
        rec = M.merge_json_file(jpath, add, root, "cursor")
        if rec is not None:
            records.append(rec)

    rules_path = root / ".cursor" / "rules" / "hearmemory.mdc"
    if M.target_ok(rules_path, root, "cursor rules/hearmemory.mdc"):
        content = S.cursor_rules_mdc()
        created_new, created_dirs = M.write_file(rules_path, content)
        records.append(I.InstallRecord(path=_rel(rules_path, root), action="created", host="cursor",
                                        sha256_after=M.sha256_of(content), created_file=created_new,
                                        created_parent_dirs=created_dirs))
    return records


def _rel(p: Path, root: Path) -> str:
    return M.rel_or_abs(p, root)
