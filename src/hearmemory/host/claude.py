"""Claude Code host adapter. install() writes only inside .hearmemory/host/claude/ (plus,
opt-in via --claude-persist, a tracked merge into the project's own .mcp.json /
.claude/settings.local.json). normalize()/handle_hook() turn hook payloads into Observations and
provenance links; nothing here ever touches ~/.claude.
"""
from __future__ import annotations

import difflib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from hearmemory import interfaces as I
from hearmemory.host import _deps, manifest as M, snippets as S
from hearmemory.textutil import hearmemory_only_command, strip_hearmemory_output

_EXIT_CODE_RE = re.compile(r"(?i)\bexit(?:ed with)?\s*code[:\s]+(-?\d+)\b")
_HEARMEMORY_CLI_RE = re.compile(r"(?:^|[;&|]\s*)(?:hearmemory|hmem)\b|(?:^|\s)-m\s+hearmemory\s")
# Bash with run_in_background returns at once; the command is still running.
_BACKGROUND_RE = re.compile(r"(?i)\b(?:command )?running in (?:the )?background\b")


def _sid(payload: Mapping[str, Any]) -> str:
    return str(payload.get("session_id") or "")


def _agent_id(payload: Mapping[str, Any]) -> Optional[str]:
    return payload.get("agent_id")


def _provenance(payload: Mapping[str, Any], event: str) -> "I.Provenance":
    """every hook-sourced Observation needs real git_branch/git_commit/git_dirty on its
    Provenance (not just meta.dirty_state) - the judge's scope_facts()/b1_status_rule() compare
    provenance.git_commit across observations to decide "unchanged scope", exactly like the
    CLI record path already does via the core's capture_provenance. Falls back to a bare Provenance
    (git fields None -> worktree_known/commit stay unknown, never crashes) when core is not yet
    importable or there is no project root."""
    root = payload.get("_root")
    if root is not None and _deps.capture_provenance is not None:
        try:
            return _deps.capture_provenance(
                root, "claude", session_id=_sid(payload) or None, subagent_id=_agent_id(payload),
                subagent_type=payload.get("agent_type"), cwd=payload.get("cwd"), source=f"hook:{event}")
        except Exception:
            pass
    return I.Provenance(host="claude", session_id=_sid(payload) or None, subagent_id=_agent_id(payload),
                        subagent_type=payload.get("agent_type"), source=f"hook:{event}",
                        cwd=payload.get("cwd"))


def _event_key(payload: Mapping[str, Any], suffix: str = "") -> str:
    sid = _sid(payload)
    tool_use_id = payload.get("tool_use_id")
    if tool_use_id:
        return f"claude:{sid}:{tool_use_id}"
    return I.stable_id("claude-ek-", sid, suffix, payload.get("tool_input"))


def _is_hearmemory_cli(command: str) -> bool:
    return bool(_HEARMEMORY_CLI_RE.search(command or ""))


def _parse_exit_code(text: str) -> Optional[int]:
    m = _EXIT_CODE_RE.search(text or "")
    return int(m.group(1)) if m else None


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


def _link_event(obs_id: str, payload: Mapping[str, Any], event: str) -> "I.ControlEvent":
    prov = _provenance(payload, event)
    return I.ControlEvent(id=I.stable_id("ev-link-", obs_id, prov.session_id, prov.subagent_id),
                          ts=_deps.now_ts(), kind="provenance_link", target=obs_id,
                          data={}, provenance=prov)


def normalize(event: str, payload: Mapping[str, Any]) -> List[Any]:
    """Returns a list of I.Observation | I.ControlEvent (run_hook/hooks.py sorts them by type
    before appending to the store's two different files)."""
    cfg = payload.get("_cfg") or dict(I.DEFAULT_CONFIG)
    root = payload.get("_root")
    tool_name = payload.get("tool_name")
    sid = _sid(payload)

    if event in ("PostToolUse", "PostToolUseFailure"):
        return _normalize_tool_event(event, payload, cfg, root)
    if event == "SubagentStop":
        return _normalize_subagent_stop(payload, cfg, root)
    if event == "Stop":
        return _normalize_stop(payload, cfg, root)
    if event == "UserPromptSubmit":
        prompt = str(payload.get("prompt") or "")
        cap = (cfg.get("capture") or {})
        if not cap.get("store_user_prompts", True):
            return []
        n = int(cap.get("user_prompt_chars", 500))
        text = prompt[:n]
        ek = f"claude:{sid}:prompt:{I.sha256_text(prompt)}"
        return [_deps.build_observation(root, cfg, "user_prompt", text, _provenance(payload, event),
                                        event_key=ek)]
    if event in ("SessionStart", "SubagentStart"):
        ts = _deps.now_ts()
        ek = f"claude:{sid}:start:{ts}"
        meta = {"source_field": payload.get("source"), "agent_type": payload.get("agent_type")}
        text = f"session_event:{event}:{payload.get('source') or ''}"
        return [_deps.build_observation(root, cfg, "session_event", text, _provenance(payload, event),
                                        event_key=ek, meta=meta)]
    return []


def _normalize_tool_event(event: str, payload: Mapping[str, Any], cfg: Mapping[str, Any], root) -> List[Any]:
    tool_name = payload.get("tool_name") or ""
    sid = _sid(payload)

    if tool_name == "mcp__hearmemory__hearmemory_record":
        result_text = json.dumps(payload.get("tool_response")) if payload.get("tool_response") else ""
        obs_id = _extract_obs_id(result_text)
        return [_link_event(obs_id, payload, event)] if obs_id else []
    if tool_name.startswith("mcp__hearmemory__"):
        return []

    if tool_name == "Bash":
        command = (payload.get("tool_input") or {}).get("command", "")
        if _is_hearmemory_cli(command):
            out_text = _bash_output_text(payload, event)
            if hearmemory_only_command(command):
                obs_id = _extract_obs_id(out_text)
                return [_link_event(obs_id, payload, event)] if obs_id else []
            # `pytest -q && hearmemory record ...` is still the agent's command: observed without hearmemory's own
            # output lines; the record it made is linked (it is stored already)
            ids = re.findall(re.escape(I.RECORD_ECHO_PREFIX) + r"(o-[0-9a-f]{16})", out_text) \
                if re.search(r"\b(?:hearmemory|hmem)\s+record\b", command) else []
            links = [_link_event(oid, payload, event) for oid in dict.fromkeys(ids)]
            return links + _normalize_bash(event, _without_hearmemory_output(payload), cfg, root, command)
        return _normalize_bash(event, payload, cfg, root, command)

    if event == "PostToolUseFailure":
        # Edit/Write failure = file unchanged; Task failure has no conclusion: only counted.
        return []

    if tool_name in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
        return _normalize_edit(payload, cfg, root, tool_name)
    if tool_name == "Read":
        return _normalize_read(payload, cfg, root)
    if tool_name in ("Grep", "Glob", "WebFetch", "WebSearch"):
        return _normalize_search(payload, cfg, root, tool_name)
    if tool_name in ("Task", "Agent"):
        return _normalize_subagent_result(payload, cfg, root)
    if (cfg.get("capture") or {}).get("unknown_tools", False):
        return _normalize_search(payload, cfg, root, tool_name or "unknown")
    return []


def _without_hearmemory_output(payload: Mapping[str, Any]) -> Dict[str, Any]:
    out = dict(payload)
    resp = payload.get("tool_response")
    if isinstance(resp, dict):
        out["tool_response"] = {k: (strip_hearmemory_output(v) if k in ("stdout", "stderr") and isinstance(v, str) else v)
                                for k, v in resp.items()}
    if isinstance(payload.get("error"), str):
        out["error"] = strip_hearmemory_output(payload["error"])
    return out


def _bash_output_text(payload: Mapping[str, Any], event: str) -> str:
    if event == "PostToolUseFailure":
        return str(payload.get("error") or "")
    resp = payload.get("tool_response") or {}
    if isinstance(resp, dict):
        return f"{resp.get('stdout', '')}\n{resp.get('stderr', '')}"
    return str(resp)


def _normalize_bash(event: str, payload: Mapping[str, Any], cfg: Mapping[str, Any], root,
                    command: str) -> List[Any]:
    ek = _event_key(payload)
    if event == "PostToolUse":
        resp = payload.get("tool_response") or {}
        stdout = resp.get("stdout", "") if isinstance(resp, dict) else ""
        stderr = resp.get("stderr", "") if isinstance(resp, dict) else ""
        text = f"$ {command}\n{stdout}\n{stderr}"
        interrupted = bool(resp.get("interrupted")) if isinstance(resp, dict) else False
        exit_code = None
        if isinstance(resp, dict):
            exit_code = resp.get("exit_code") if resp.get("exit_code") is not None else resp.get("exitCode")
        if exit_code is None:
            exit_code = _parse_exit_code(stdout + "\n" + stderr)
        ti = payload.get("tool_input") or {}
        background = bool((isinstance(ti, dict) and ti.get("run_in_background"))
                          or (isinstance(resp, dict) and (resp.get("backgroundTaskId") or resp.get("background_task_id")))
                          or _BACKGROUND_RE.search(stdout[:500]))
        if background:
            # the result is not known yet -- never a pass (and never hides an earlier failure)
            status, exit_code = "running", None
            summary = None
        else:
            if exit_code is None and not interrupted and (stdout.strip() or stderr.strip()):
                # Claude Code sends PostToolUse only for a command that succeeded (a non-zero exit
                # goes to PostToolUseFailure). With no output at all nothing is known: stays None.
                exit_code = 0
            status = "error" if interrupted or (exit_code not in (None, 0)) else ("ok" if exit_code == 0 else "unknown")
            summary = _deps.get_test_summary(command, stdout + "\n" + stderr)
        tool = I.ToolInfo(name="Bash", command=command, exit_code=exit_code, status=status, test=summary)
        return [_deps.build_observation(root, cfg, "command", text, _provenance(payload, event),
                                        tool=tool, event_key=ek)]
    # PostToolUseFailure Bash
    error = str(payload.get("error") or "")
    text = f"$ {command}\n{error}"
    is_interrupt = bool(payload.get("is_interrupt"))
    exit_code = None if is_interrupt else _parse_exit_code(error)
    summary = _deps.get_test_summary(command, error)
    tool = I.ToolInfo(name="Bash", command=command, exit_code=exit_code, status="error", test=summary)
    return [_deps.build_observation(root, cfg, "command", text, _provenance(payload, event),
                                    tool=tool, event_key=ek)]


def _normalize_edit(payload: Mapping[str, Any], cfg: Mapping[str, Any], root, tool_name: str) -> List[Any]:
    ti = payload.get("tool_input") or {}
    path = ti.get("file_path") or ti.get("notebook_path") or ""
    if tool_name in ("Edit", "MultiEdit"):
        old = ti.get("old_string", "") if tool_name == "Edit" else "\n".join(
            e.get("old_string", "") for e in (ti.get("edits") or []))
        new = ti.get("new_string", "") if tool_name == "Edit" else "\n".join(
            e.get("new_string", "") for e in (ti.get("edits") or []))
        diff = "\n".join(list(difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm=""))[:60])
        text = diff
    else:
        content = ti.get("content", "") or ti.get("new_source", "")
        text = "\n".join(content.splitlines()[:40])
    tool = I.ToolInfo(name=tool_name, paths=[path] if path else [], status="ok")
    ek = _event_key(payload)
    return [_deps.build_observation(root, cfg, "file_edit", text, _provenance(payload, "PostToolUse"),
                                    tool=tool, event_key=ek)]


def _normalize_read(payload: Mapping[str, Any], cfg: Mapping[str, Any], root) -> List[Any]:
    ti = payload.get("tool_input") or {}
    path = ti.get("file_path") or ""
    store_reads = (cfg.get("capture") or {}).get("store_file_reads", False)
    resp = payload.get("tool_response")
    text = "" if not store_reads else str(resp)[:4000]
    tool = I.ToolInfo(name="Read", paths=[path] if path else [], status="ok")
    ek = _event_key(payload)
    return [_deps.build_observation(root, cfg, "file_read", text, _provenance(payload, "PostToolUse"),
                                    tool=tool, event_key=ek, meta={"path": path})]


def _normalize_search(payload: Mapping[str, Any], cfg: Mapping[str, Any], root, tool_name: str) -> List[Any]:
    resp = payload.get("tool_response")
    text = str(resp if resp is not None else "")[:1500]
    ti = payload.get("tool_input") or {}
    meta: Dict[str, Any] = {}
    if "url" in ti:
        meta["url"] = ti["url"]
    tool = I.ToolInfo(name=tool_name, status="ok")
    ek = _event_key(payload)
    return [_deps.build_observation(root, cfg, "search", text, _provenance(payload, "PostToolUse"),
                                    tool=tool, event_key=ek, meta=meta)]


def _subagent_result_text(resp: Any) -> str:
    """the real Task/Agent tool_response is an OBJECT ({status, prompt, agentId, content:[{type:
    "text", text}], usage, totalTokens, ...}). Only the subagent's own answer -- the `text` of
    content[] blocks of type "text" -- is its result; `prompt` is the PARENT's instruction (its
    hypotheses must never be recorded as the subagent's conclusion) and the rest is bookkeeping.
    A plain string response is used as is."""
    if resp is None:
        return ""
    if isinstance(resp, str):
        return resp
    content = resp.get("content") if isinstance(resp, dict) else resp if isinstance(resp, list) else None
    if isinstance(content, str):
        return content
    parts: List[str] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
    return "\n".join(p for p in parts if p)


def _normalize_subagent_result(payload: Mapping[str, Any], cfg: Mapping[str, Any], root) -> List[Any]:
    resp = payload.get("tool_response")
    text = _subagent_result_text(resp)
    ti = payload.get("tool_input") or {}
    meta = {"subagent_type": ti.get("subagent_type"), "prompt_excerpt": str(ti.get("prompt") or "")[:200]}
    tool = I.ToolInfo(name=payload.get("tool_name") or "Task", status="ok")
    ek = _event_key(payload)
    return [_deps.build_observation(root, cfg, "subagent_result", text, _provenance(payload, "PostToolUse"),
                                    tool=tool, event_key=ek, meta=meta)]


def _read_transcript_tail(path: Optional[str], max_bytes: int = 256 * 1024) -> List[dict]:
    if not path:
        return []
    try:
        p = Path(path)
        data = p.read_bytes()[-max_bytes:]
        text = data.decode("utf-8", errors="ignore")
        lines = text.splitlines()
        out = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out
    except Exception:
        return []


def _assistant_text(entry: dict) -> str:
    msg = entry.get("message") or {}
    content = msg.get("content") or []
    parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
    return "\n".join(p for p in parts if p)


def _normalize_subagent_stop(payload: Mapping[str, Any], cfg: Mapping[str, Any], root) -> List[Any]:
    entries = _read_transcript_tail(payload.get("agent_transcript_path") or payload.get("transcript_path"))
    text = ""
    for entry in reversed(entries):
        if entry.get("type") == "assistant" and (entry.get("isSidechain") or payload.get("agent_transcript_path")):
            text = _assistant_text(entry)
            if text:
                break
    agent_id = _agent_id(payload) or "unknown"
    ek = f"claude:{_sid(payload)}:subagent:{agent_id}"
    tool = I.ToolInfo(name="Task", status="ok")
    return [_deps.build_observation(root, cfg, "subagent_result", text, _provenance(payload, "SubagentStop"),
                                    tool=tool, event_key=ek)]


def _normalize_stop(payload: Mapping[str, Any], cfg: Mapping[str, Any], root) -> List[Any]:
    entries = _read_transcript_tail(payload.get("transcript_path"))
    text, uuid = "", ""
    for entry in reversed(entries):
        if entry.get("type") == "assistant" and not entry.get("isSidechain"):
            text = _assistant_text(entry)
            uuid = str(entry.get("uuid") or "")
            if text:
                break
    ek = f"claude:{_sid(payload)}:msg:{uuid or I.sha256_text(text)[:16]}"
    return [_deps.build_observation(root, cfg, "assistant_message", text, _provenance(payload, "Stop"),
                                    event_key=ek)]


# --------------------------------------------------------------------------- install
def install(root: Path, python: str, cfg: Mapping[str, Any], *, persist: bool = False,
            records: Optional[List["I.InstallRecord"]] = None) -> List["I.InstallRecord"]:
    """`records`, when given, is appended to AS EACH FILE IS WRITTEN (install.py passes the
    manifest's list so a crash half-way still leaves an undoable manifest). Every target is
    checked with M.target_ok first: a path whose real location is outside the project (e.g. a
    `.claude` symlink to ~/.claude) is skipped with a warning, never written through."""
    root = Path(root)
    records = [] if records is None else records
    host_dir = root / I.HEARMEMORY_DIRNAME / "host" / "claude"

    mcp = S.claude_mcp_json(python, str(root))
    settings = S.claude_settings_json(python, str(root))
    launch = S.claude_launch_sh(python, str(root))

    for fname, content_obj, is_json in (("mcp.json", mcp, True), ("settings.json", settings, True)):
        path = host_dir / fname
        if not M.target_ok(path, root, f"claude {fname}"):
            continue
        text = json.dumps(content_obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        created_new, created_dirs = M.write_file(path, text)
        records.append(I.InstallRecord(path=_rel(path, root), action="created", host="claude",
                                        sha256_after=M.sha256_of(text), created_file=created_new,
                                        created_parent_dirs=created_dirs))
    path = host_dir / "launch.sh"
    if M.target_ok(path, root, "claude launch.sh"):
        created_new, created_dirs = M.write_file(path, launch, executable=True)
        records.append(I.InstallRecord(path=_rel(path, root), action="created", host="claude",
                                        sha256_after=M.sha256_of(launch), created_file=created_new,
                                        created_parent_dirs=created_dirs))

    if persist:
        # Tracked merges into the user's own project files; created_file/created_parent_dirs are
        # computed BEFORE writing so uninstall can remove exactly what hearmemory created.
        # merge_json_file applies the same realpath scope guard.
        for jpath, add in ((root / ".mcp.json", mcp),
                           (root / ".claude" / "settings.local.json", settings)):
            rec = M.merge_json_file(jpath, add, root, "claude")
            if rec is not None:
                records.append(rec)
    return records


def _rel(p: Path, root: Path) -> str:
    return M.rel_or_abs(p, root)


def handle_hook(event: str, payload: Mapping[str, Any]) -> "I.HookResult":
    from hearmemory.host import hooks as _hooks
    return _hooks.generic_handle_hook("claude", event, payload)
