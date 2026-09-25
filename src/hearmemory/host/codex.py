"""Codex host adapter. Codex gives us far less live signal than Claude Code (only
SessionStart/Stop hooks, experimental and version-dependent), so most of Codex's contribution to
project memory comes from `import_rollouts`: a READ-ONLY, incremental, idempotent import of Codex's
own `~/.codex/sessions/**/rollout-*.jsonl`, filtered to sessions whose cwd is this project.

install() never writes `~/.codex/config.toml` or any other file outside this project: it inserts a
marked block into (or creates) the project's own AGENTS.md, and writes `.hearmemory/host/codex/` files.
"""
from __future__ import annotations

import ast
import inspect
import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from hearmemory import interfaces as I
from hearmemory.host import _deps, manifest as M, snippets as S
from hearmemory.textutil import GIT_COMMIT_CMD_RE, hearmemory_only_command, new_commit_sha, strip_hearmemory_output

AGENTS_MD = "AGENTS.md"
_EXIT_CODE_RE = re.compile(r"(?i)\bexit code[:\s]+(-?\d+)\b|\bprocess exited with code\s+(-?\d+)\b")
_HEARMEMORY_CLI_RE = re.compile(r"(?:^|[;&|]\s*)(?:hearmemory|hmem)\b|(?:^|\s)-m\s+hearmemory\s")
_PATCH_FILE_RE = re.compile(r"^\*\*\*\s+(Add|Update|Delete) File:\s+(.+)$", re.MULTILINE)
# Apply_patch also arrives as an exec_command heredoc
# (`apply_patch <<'PATCH' ... PATCH`), not only as the custom tool. It is a FILE EDIT, never a command
# whose text is the patch (that made B1 evidence a head-truncated "- return a - b" = fake counter-evidence).
_APPLY_PATCH_CMD_RE = re.compile(r"^\s*(?:cd\s+\S+\s*&&\s*)?apply_patch\b")
# the Codex approval reviewer ("auto_review" / guardian) runs as its own rollout session whose
# prompt is a copy of the main session's transcript; it is not project work and must not be a second agent.
_SIDECAR_RE = re.compile(r"(?i)guardian|auto.?review|approv")
_SIDECAR_PROMPT_PREFIX = "The following is the Codex agent history"
_HEARMEMORY_RECORD_NAME_RE = re.compile(r"(?:^|[_.:/])hearmemory_record$")
_LAUNCH_ID_RE = re.compile(r"^codex-(\d{9,11})-\d+$")
_MCP_ID_RE = re.compile(r"^mcp-\d+-(\d{9,11})$")
LAUNCH_SLACK_S = 10.0             # a rollout may start a few seconds before its launch.sh/MCP time stamp
LAUNCH_MAX_GAP_S = 12 * 3600.0    # a launch's first rollout starts within this long after the launch
# a long exec_command returns early with this; the rest (and the exit code) comes back through
# later write_stdin calls on the same session id.
_RUNNING_RE = re.compile(r"(?i)\bProcess running with session ID\s+(\d+)")
_PROC_OUT_KEEP = 16 * 1024
_PATCH_KEEP = 1024 * 1024


_GIT_CMD_RE = re.compile(r"^\s*git\b")
_WITHHELD = "[hearmemory: withheld (could not redact)]"


def _redacted(text: Any, cfg: Optional[Mapping[str, Any]], *, git_cmd: str = "") -> str:
    """what the cursor file may keep of a pending call's command / output -- the same redaction
    every stored observation gets (fail closed: nothing, when redaction is unavailable)."""
    text = str(text or "")
    if not text:
        return text
    fn = _deps.redact
    if fn is None:
        return _WITHHELD
    try:
        return fn(text, cfg=cfg, git_output=bool(_GIT_CMD_RE.match(git_cmd or "")))[0]
    except Exception:
        return _WITHHELD


def _redacted_tail(output: str, cfg: Optional[Mapping[str, Any]], cmd: str) -> str:
    # cut generously first (bounded regex work), redact, then keep the last _PROC_OUT_KEEP chars
    return _redacted(output[-2 * _PROC_OUT_KEEP:], cfg, git_cmd=cmd)[-_PROC_OUT_KEEP:]


def _scrub_pending(pending: Dict[str, Dict[str, Any]], cfg: Optional[Mapping[str, Any]]) -> None:
    """Pending calls kept by an older version (raw command / output in the cursor file): redact
    them in place the first time the file is touched again."""
    for v in pending.values():
        if not isinstance(v, dict) or v.get("redacted"):
            continue
        cmd = str(v.get("cmd") or "")
        if "input" in v:
            v["input"] = _redacted(str(v.get("input") or "")[:_PATCH_KEEP], cfg)
        if "out" in v:
            v["out"] = _redacted_tail(str(v.get("out") or ""), cfg, cmd)
        if "cmd" in v:
            v["cmd"] = _redacted(cmd, cfg, git_cmd=cmd)
        v["redacted"] = True


def _workdir(arguments: Any, st: "_FileState") -> Optional[str]:
    """Where a Codex command ran: its `workdir` argument (relative = to the session cwd), else the
    session cwd (a bare `pytest` there is a run of THAT directory)."""
    wd = arguments.get("workdir") if isinstance(arguments, dict) else None
    if isinstance(wd, str) and wd.strip():
        wd = wd.strip()
        if not os.path.isabs(wd):
            wd = os.path.join(st.cwd, wd) if st.cwd else None
        return os.path.normpath(wd) if wd else None
    return st.cwd or None


def _is_hearmemory_cli(command: str) -> bool:
    return bool(_HEARMEMORY_CLI_RE.search(command or ""))


def _cli_record_ids(cmd: str, output: str) -> List[str]:
    """obs ids a `hearmemory record` inside `cmd` printed ("hearmemory: recorded o-..."); for a command that is nothing but
    hearmemory, any obs id in its output (the old rule)."""
    if not re.search(r"\b(?:hearmemory|hmem)\s+record\b", cmd or ""):
        return []
    ids = re.findall(re.escape(I.RECORD_ECHO_PREFIX) + r"(o-[0-9a-f]{16})", output or "")
    if not ids and hearmemory_only_command(cmd):
        oid = _extract_obs_id(output)
        ids = [oid] if oid else []
    return list(dict.fromkeys(ids))


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


def _import_obs(ts: Optional[str], *args: Any, **kw: Any) -> "I.Observation":
    """Every imported rollout event is HISTORICAL: it is stamped with the rollout line's
    own timestamp (not the import time, which made a days-old failure look "just now" and
    outrank a fresher pass), and it never gets the CURRENT worktree's dirty_state."""
    return _deps.build_observation(*args, event_ts=ts, historical=True, **kw)


# rollout lines above this size are not json-parsed whole (a 60 MB tool-output line made
# SessionStart take 10-30 s); only their head/tail is used, see _oversized_line_record.
MAX_LINE_BYTES_DEFAULT = 4 * 1024 * 1024
_OVERSIZE_KEEP_BYTES = 64 * 1024
_EXIT_SCAN_CHARS = 8192


def _unescape_json_fragment(frag: str) -> str:
    """Best-effort decode of a piece of a JSON string literal (cut at an arbitrary point)."""
    for cut in range(0, 8):
        piece = frag[: len(frag) - cut] if cut else frag
        try:
            return json.loads('"' + piece + '"')
        except Exception:
            continue
    return frag.replace("\\n", "\n").replace('\\"', '"')


def _oversized_line_record(raw_b: bytes) -> Optional[Dict[str, Any]]:
    """A synthetic, SMALL rollout record for a huge line, built from its first/last 64 KB only:
    enough to keep a tool call's result (Codex puts "Exit code: N" first and a test runner's
    summary last) without parsing or holding the megabytes in between. None for anything that
    is not a function_call_output / custom_tool_call_output (then the line is just skipped)."""
    head = raw_b[:_OVERSIZE_KEEP_BYTES].decode("utf-8", errors="ignore")
    tail = raw_b[-_OVERSIZE_KEEP_BYTES:].decode("utf-8", errors="ignore")
    m_type = re.search(r'"type"\s*:\s*"(function_call_output|custom_tool_call_output)"', head)
    m_call = re.search(r'"call_id"\s*:\s*"([^"]+)"', head) or re.search(r'"call_id"\s*:\s*"([^"]+)"', tail)
    if not (m_type and m_call):
        return None
    m_ts = re.search(r'"timestamp"\s*:\s*"([^"]+)"', head)
    m_out = re.search(r'"output"\s*:\s*"', head)
    out_head = _unescape_json_fragment(head[m_out.end():]) if m_out else ""
    t = tail.rstrip()
    t = t.rstrip("}").rstrip()
    t = re.sub(r',\s*"call_id"\s*:\s*"[^"]*"\s*$', "", t)  # call_id after output, if any
    if t.endswith('"'):
        t = t[:-1]
    out_tail = _unescape_json_fragment(t)
    output = (f"{out_head}\n…[hearmemory: oversized rollout line ({len(raw_b)} bytes); middle not "
              f"imported]…\n{out_tail}")
    return {"timestamp": m_ts.group(1) if m_ts else None, "type": "response_item",
            "payload": {"type": m_type.group(1), "call_id": m_call.group(1), "output": output,
                        "_hearmemory_oversized_bytes": len(raw_b)}}


def _parse_exit_code(text: str) -> Optional[int]:
    text = text or ""
    if len(text) > 2 * _EXIT_SCAN_CHARS:  # the exit line is at the start (or end)
        text = text[:_EXIT_SCAN_CHARS] + "\n" + text[-_EXIT_SCAN_CHARS:]
    m = _EXIT_CODE_RE.search(text)
    if not m:
        return None
    return int(m.group(1) or m.group(2))


def _cmd_text(arguments: Mapping[str, Any]) -> str:
    cmd = arguments.get("cmd") if "cmd" in arguments else arguments.get("command")
    if isinstance(cmd, list):
        parts = [str(c) for c in cmd]
        # legacy `shell` tool: ["bash", "-lc", "<script>"] -> the script itself
        if (len(parts) == 3 and os.path.basename(parts[0]) in ("bash", "sh", "zsh", "dash")
                and re.fullmatch(r"-[a-z]*c[a-z]*", parts[1])):
            return parts[2]
        return shlex.join(parts)
    return str(cmd or "")


def _infer_git_commit(root: Path, ts: Optional[str]) -> Optional[str]:
    if not ts:
        return None
    try:
        out = subprocess.run(["git", "rev-list", "-1", f"--before={ts}", "HEAD"], cwd=str(root),
                             capture_output=True, text=True, timeout=0.3)
        return out.stdout.strip() or None if out.returncode == 0 else None
    except Exception:
        return None


class _FileState:
    def __init__(self) -> None:
        self.saw_agent_message = False
        self.session_id: Optional[str] = None
        self.model: Optional[str] = None
        self.cwd: Optional[str] = None
        self.matched = False
        self.start_ts: Optional[str] = None
        self.main = True                   # a user-facing thread (not a sub-agent / reviewer thread)
        self.sidecar = False               # the approval reviewer: never imported
        self.subagent_type: Optional[str] = None
        # files this session edited and has not committed yet (a test run meanwhile ran on a
        # dirty tree: the commit that was HEAD then is not what was tested)
        self.uncommitted: List[str] = []


def _make_provenance(session_id: Optional[str], model: Optional[str], ts: Optional[str], root: Path,
                     source: str = "import:codex_rollout", cwd: Optional[str] = None,
                     subagent_type: Optional[str] = None) -> "I.Provenance":
    return I.Provenance(host="codex", session_id=session_id, model=model, source=source,
                        git_commit=_infer_git_commit(root, ts), cwd=cwd, subagent_type=subagent_type)


def _subagent_label(src: Any) -> Optional[str]:
    """session_meta.source for a sub-thread is an object such as {"subagent": "review"} or
    {"subagent": {"other": "guardian"}}; a plain string ("cli", "exec", "vscode") is a user thread."""
    if not isinstance(src, dict):
        return None
    sub = src.get("subagent", src.get("sub_agent", src))
    if isinstance(sub, str):
        return sub
    try:
        return json.dumps(sub, sort_keys=True)[:80]
    except Exception:
        return "subagent"


def _become_sidecar(st: "_FileState", obs_batch: List[Any], events_batch: List[Any]) -> None:
    st.sidecar = True
    st.matched = False
    st.main = False
    obs_batch.clear()
    events_batch.clear()


def _within_project(cwd: Optional[str], root: Path) -> bool:
    if not cwd:
        return False
    try:
        return Path(cwd).resolve() == root.resolve() or root.resolve() in Path(cwd).resolve().parents
    except Exception:
        return False


IMPORT_FLOOR_SLACK_S = 5.0        # file mtimes come from a coarse clock; a file written just after init may lag


def import_floor(store: Any, since: Optional[str] = None, all_history: bool = False) -> Optional[float]:
    """the epoch before which Codex history is NOT imported. Default: the store's `hearmemory init` time
    (state/init.json, written by create_store) -- a fresh store must not fill up with sessions that happened
    before it existed (an old echo claim became [SUPPORTED] and was shown to the next agent). `since` (ISO time)
    overrides it; `all_history` (or a store created before init.json existed) imports everything, as before."""
    if all_history:
        return None
    if since:
        ep = _parse_epoch(since)
        if ep is None:
            raise ValueError(f"--since: not an ISO date/time: {since!r}")
        return ep
    try:
        info = store.read_state("init") or {}
    except Exception:
        info = {}
    return _parse_epoch(info.get("init_ts"))


def import_rollouts(store: Any, cfg: Mapping[str, Any], since: Optional[str] = None,
                    session: Optional[str] = None, *, codex_home: Optional[str] = None,
                    dry_run: bool = False, max_lines: Optional[int] = None,
                    deadline_s: Optional[float] = None, all_history: bool = False) -> int:
    """`store` follows StoreAPI (duck-typed: append_observations/append_events/read_state/
    write_state/root). Returns the number of observations appended (or that WOULD be appended,
    when dry_run=True).

    `deadline_s`: wall-clock budget in seconds. Hooks pass their `import_codex` slice so a
    large Codex history (hundreds of MB) never makes SessionStart/Stop/pre-commit late; the import
    stops between lines, keeps the cursor exactly before the first unprocessed line, and resumes
    there next time. Each file is read from its saved byte offset (seek, never the whole file), and
    the cursor is saved after EVERY file, so progress survives a caller that gives up on us.

    a rollout last written before import_floor() (a session that ended before `hearmemory init`, or
    before `since`) is skipped; `all_history=True` backfills everything."""
    t_end = (time.monotonic() + max(0.0, float(deadline_s))) if deadline_s is not None else None
    floor = import_floor(store, since, all_history)
    root = Path(getattr(store, "root"))
    imp_cfg = (cfg.get("import") or {})
    home = Path(codex_home or imp_cfg.get("codex_home") or os.environ.get("CODEX_HOME") or
                os.path.expanduser("~/.codex"))
    sessions_dir = home / "sessions"
    if not sessions_dir.exists():
        return 0
    max_age_days = float(imp_cfg.get("codex_max_age_days", 14))
    cutoff = time.time() - max_age_days * 86400.0

    cursors = store.read_state("cursors") or {}
    codex_cursors: Dict[str, Any] = cursors.setdefault("import", {}).setdefault("codex", {})

    total_appended = 0
    for path in sorted(sessions_dir.glob("**/rollout-*.jsonl")):
        if t_end is not None and time.monotonic() >= t_end:
            break
        try:
            st_ = path.stat()
        except OSError:
            continue
        if st_.st_mtime < cutoff:
            continue
        if floor is not None and st_.st_mtime < floor - IMPORT_FLOOR_SLACK_S:
            continue    # the whole session happened before this store existed (or before --since)
        key = str(path)
        entry = codex_cursors.get(key) or {"offset": 0, "session_id": None, "match": None}
        if int(entry.get("offset") or 0) == st_.st_size and key in codex_cursors:
            continue  # fully consumed and unchanged: no need to even open it
        before = dict(entry)
        appended = _import_one_file(store, cfg, path, entry, root, session=session, dry_run=dry_run,
                                    max_lines=max_lines, t_end=t_end, size=st_.st_size)
        total_appended += appended
        if not dry_run and entry != before:
            codex_cursors[key] = entry
            store.write_state("cursors", cursors)
    return total_appended


def _import_one_file(store: Any, cfg: Mapping[str, Any], path: Path, cursor_entry: Dict[str, Any],
                     root: Path, *, session: Optional[str], dry_run: bool,
                     max_lines: Optional[int], t_end: Optional[float] = None,
                     size: Optional[int] = None) -> int:
    offset = int(cursor_entry.get("offset") or 0)
    if size is None:
        try:
            size = path.stat().st_size
        except OSError:
            return 0
    if offset > size:
        # file was rotated/truncated: restart (worst case: a few duplicate lines, deduped on read)
        offset = 0
        for k in ("session_id", "match", "_meta_seen", "_line_no", "_model", "_cwd",
                  "_saw_agent_message", "_pending_call", "_start_ts", "_main", "_sidecar", "_subagent_type",
                  "_uncommitted"):
            cursor_entry.pop(k, None)
    # A single Codex session's rollout file is imported across MULTIPLE calls (each hook/worker
    # tick has its own time slice); the per-file scan state must therefore survive
    # between calls exactly like the byte offset does, or a batch boundary falling between
    # session_meta and the lines that need it would silently orphan every later observation.
    st = _FileState()
    st.matched = bool(cursor_entry.get("match")) if cursor_entry.get("match") is not None else False
    st.session_id = cursor_entry.get("session_id")
    st.model = cursor_entry.get("_model")
    st.cwd = cursor_entry.get("_cwd")
    st.saw_agent_message = bool(cursor_entry.get("_saw_agent_message"))
    st.start_ts = cursor_entry.get("_start_ts")
    st.main = bool(cursor_entry.get("_main", True))
    st.sidecar = bool(cursor_entry.get("_sidecar"))
    st.subagent_type = cursor_entry.get("_subagent_type")
    st.uncommitted = [str(p) for p in cursor_entry.get("_uncommitted") or []]
    if st.sidecar:
        st.matched = False
    meta_seen = bool(cursor_entry.get("_meta_seen"))
    known_session = cursor_entry.get("session_id")
    # function_call / function_call_output (and custom_tool_call / ..._output) are always TWO
    # separate lines; a batch boundary between them must not drop the call, so this too survives
    # across calls (JSON-safe: only str/None values).
    pending_call: Dict[str, Dict[str, Any]] = dict(cursor_entry.get("_pending_call") or {})
    _scrub_pending(pending_call, cfg)
    obs_batch: List[Any] = []
    events_batch: List[Any] = []
    line_no_base = int(cursor_entry.get("_line_no", 0) or 0)

    consumed_bytes = offset
    lines_consumed = 0
    n_processed = 0
    try:
        max_line_bytes = int((cfg.get("import") or {}).get("codex_max_line_bytes")
                             or MAX_LINE_BYTES_DEFAULT)
    except (TypeError, ValueError):
        max_line_bytes = MAX_LINE_BYTES_DEFAULT
    try:
        fh = path.open("rb")
    except OSError:
        return 0
    with fh:
        if meta_seen and not st.matched:
            # session_meta (always the first line) already told us this rollout belongs to another
            # project: skip the rest of the file without reading or parsing it.
            consumed_bytes = size
        else:
            fh.seek(offset)
            while True:
                if t_end is not None and time.monotonic() >= t_end:
                    break  # out of time: cursor stays before this line, resume here next call
                raw_b = fh.readline()
                if not raw_b or not raw_b.endswith(b"\n"):
                    # EOF, or a partial line the writer has not finished yet: not consumed.
                    break
                oversized = len(raw_b) > max_line_bytes
                raw = "x" if oversized else raw_b.decode("utf-8", errors="ignore")
                if raw.strip():
                    if max_lines is not None and n_processed >= max_lines:
                        # Stop WITHOUT consuming this line's bytes: the cursor stays before it, so
                        # a timed-out batch never loses a line, it just resumes here next time.
                        break
                    n_processed += 1
                    if oversized:
                        rec = _oversized_line_record(raw_b)  # never parse it whole
                    else:
                        try:
                            rec = json.loads(raw)
                        except Exception:
                            rec = None
                    if isinstance(rec, dict):
                        line_no = line_no_base + lines_consumed
                        _handle_rollout_line(rec, st, path, line_no, root, session, obs_batch,
                                             events_batch, pending_call, cfg)
                        if rec.get("type") == "session_meta":
                            meta_seen = True
                consumed_bytes += len(raw_b)
                lines_consumed += 1
                if meta_seen and not st.matched:
                    consumed_bytes = size  # another project's session: skip the remainder
                    break

    if known_session and session and known_session != session:
        pass  # caller asked for a specific session; still advance the cursor below.

    if not dry_run:
        if obs_batch:
            store.append_observations(obs_batch)
        if events_batch:
            store.append_events(events_batch)
        cursor_entry["offset"] = consumed_bytes
        cursor_entry["session_id"] = st.session_id or known_session
        cursor_entry["match"] = False if st.sidecar else (st.matched or bool(cursor_entry.get("match")))
        cursor_entry["_meta_seen"] = meta_seen
        cursor_entry["_start_ts"] = st.start_ts
        cursor_entry["_main"] = st.main
        cursor_entry["_sidecar"] = st.sidecar
        cursor_entry["_subagent_type"] = st.subagent_type
        cursor_entry["_line_no"] = line_no_base + lines_consumed
        cursor_entry["_model"] = st.model
        cursor_entry["_cwd"] = st.cwd
        cursor_entry["_saw_agent_message"] = st.saw_agent_message
        cursor_entry["_pending_call"] = pending_call
        cursor_entry["_uncommitted"] = st.uncommitted[-50:]
    return len(obs_batch)


def _handle_rollout_line(rec: Dict[str, Any], st: _FileState, path: Path, line_no: int, root: Path,
                         want_session: Optional[str], obs_batch: List[Any], events_batch: List[Any],
                         pending_call: Dict[str, Dict[str, Any]],
                         cfg: Optional[Mapping[str, Any]] = None) -> None:
    # cfg is the project's real config: build_observation applies its exclude_globs / redaction
    # (privacy) exactly like the live Claude/Cursor hooks do.
    cfg = cfg if cfg is not None else dict(I.DEFAULT_CONFIG)
    rtype = rec.get("type")
    payload = rec.get("payload") or {}
    ts = rec.get("timestamp") or payload.get("timestamp")
    transcript_ref = f"{path.name}:{line_no}"

    if rtype == "session_meta":
        st.session_id = payload.get("id")
        st.cwd = payload.get("cwd")
        st.matched = _within_project(st.cwd, root)
        st.start_ts = payload.get("timestamp") or ts
        sub = _subagent_label(payload.get("source"))
        thread_source = payload.get("thread_source")
        st.subagent_type = sub
        st.main = sub is None and thread_source in (None, "user")
        # source {"subagent": {"other": "guardian"}}, thread_source "guardian_review": the approval reviewer
        if (sub is not None and _SIDECAR_RE.search(sub)) or _SIDECAR_RE.search(str(thread_source or "")):
            _become_sidecar(st, obs_batch, events_batch)
        return
    if not st.matched:
        return
    if want_session and st.session_id != want_session:
        return

    if rtype == "turn_context":
        st.cwd = payload.get("cwd") or st.cwd
        st.model = payload.get("model") or st.model
        if st.model and "auto-review" in str(st.model):
            _become_sidecar(st, obs_batch, events_batch)
        return

    if rtype == "response_item":
        item_type = payload.get("type")
        if item_type in ("custom_tool_call", "custom_tool_call_output"):
            # Real Codex rollouts: apply_patch is a custom tool nested under response_item.
            _handle_custom_tool(item_type, payload, st, ts, root, transcript_ref, obs_batch,
                                pending_call, cfg, events_batch)
            return
        if item_type in ("function_call", "shell"):
            name = payload.get("name")
            call_id = payload.get("call_id")
            arguments = payload.get("arguments") or {}
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except Exception:
                    arguments = {}
            if name in ("exec_command", "shell", None) and call_id:
                # the cursor file keeps only the redacted command (it is redacted again when
                # stored, which is a no-op on an already redacted string)
                raw_cmd = _cmd_text(arguments)
                pending_call[call_id] = {"kind": "exec", "cmd": _redacted(raw_cmd, cfg, git_cmd=raw_cmd), "ts": ts,
                                         "cwd": _workdir(arguments, st), "redacted": True}
            elif _is_hearmemory_record_call(name, payload) and call_id:
                pending_call[call_id] = {"kind": "hearmemory_mcp", "ts": ts}
            elif name == "write_stdin" and call_id:
                sid = arguments.get("session_id") if isinstance(arguments, dict) else None
                if sid is not None and f"proc:{sid}" in pending_call:
                    pending_call[call_id] = {"kind": "stdin", "proc": str(sid), "ts": ts}
            return
        if item_type == "function_call_output":
            call_id = payload.get("call_id")
            info = pending_call.pop(call_id, None)
            raw_output = payload.get("output")
            output = raw_output
            if isinstance(output, dict):
                output = output.get("output") or output.get("content") or json.dumps(output)
            if isinstance(output, list):
                output = json.dumps(output, ensure_ascii=False)
            output = str(output or "")
            if info is None:
                return
            if info["kind"] == "hearmemory_mcp":
                obs_id = _extract_obs_id(output) or _extract_obs_id(json.dumps(raw_output, ensure_ascii=False))
                if obs_id:
                    events_batch.append(_link_event(obs_id, st, ts))
                return
            if info["kind"] == "stdin":
                _finish_running(info, output, call_id, st, ts, root, cfg, obs_batch, pending_call,
                                transcript_ref)
                return
            cmd = info["cmd"]
            if _APPLY_PATCH_CMD_RE.match(cmd):
                patch = _patch_from_command(cmd)
                if patch:
                    code = _parse_exit_code(output)
                    failed = (code is not None and code != 0) or bool(
                        re.search(r"(?i)\bfailed to apply|\berror:", output[:_EXIT_SCAN_CHARS]))
                    _emit_patch_edits(patch, call_id, "error" if failed else "ok", st, ts, root,
                                      transcript_ref, obs_batch, cfg)
                    return
            if _is_hearmemory_cli(cmd):
                # hearmemory's own record is linked to this session (it is stored already: never recorded twice).
                # A command that only CONTAINS hearmemory calls (`hearmemory record .. && git add .. && hearmemory check
                # --staged && git commit ..`) is still observed -- without hearmemory's own output lines
                for oid in _cli_record_ids(cmd, output):
                    events_batch.append(_link_event(oid, st, ts))
                if hearmemory_only_command(cmd):
                    return
                output = strip_hearmemory_output(output)
            exit_code = _parse_exit_code(output)
            running = _RUNNING_RE.search(output[:_EXIT_SCAN_CHARS]) if exit_code is None else None
            if running:
                # still running -- recorded as such (never a pass); the final result is filled
                # in from the write_stdin output that reports its exit code (see _finish_running).
                procs = [k for k in pending_call if k.startswith("proc:")]
                for k in procs[:max(0, len(procs) - 31)]:     # bounded cursor state (never-finished runs)
                    pending_call.pop(k, None)
                pending_call[f"proc:{running.group(1)}"] = {"kind": "proc", "cmd": cmd, "call_id": call_id,
                                                            "ts": ts, "out": _redacted_tail(output, cfg, cmd),
                                                            "cwd": info.get("cwd"), "redacted": True}
                summary = None
                status = "running"
            else:
                summary = _deps.get_test_summary(cmd, output)
                status = "unknown" if exit_code is None else ("ok" if exit_code == 0 else "error")
            tool = I.ToolInfo(name="exec_command", command=cmd, exit_code=exit_code, status=status,
                              test=summary)
            ek = f"codex:{st.session_id}:{call_id}"
            text = f"$ {cmd}\n{output}"
            _command_obs(st, ts, root, cfg, tool, text, output, info.get("cwd"), ek,
                         {"transcript_ref": transcript_ref}, obs_batch)
            return
        if item_type == "message" and payload.get("role") == "assistant" and not st.saw_agent_message:
            text = _message_text(payload)
            if text:
                prov = _make_provenance(st.session_id, st.model, ts, root)
                ek = f"codex:{st.session_id}:msg:{line_no}"
                obs_batch.append(_import_obs(ts, root, cfg, "assistant_message", text, prov,
                                                          event_key=ek,
                                                          meta={"transcript_ref": transcript_ref}))
            return
        obs_id = _mcp_record_obs_id(payload)
        if obs_id:
            events_batch.append(_link_event(obs_id, st, ts))
        return

    if rtype in ("custom_tool_call", "custom_tool_call_output"):
        # Legacy/flat shape (kept for tolerance); real rollouts nest these under response_item.
        _handle_custom_tool(rtype, payload, st, ts, root, transcript_ref, obs_batch, pending_call, cfg,
                            events_batch)
        return

    if rtype == "event_msg":
        etype = payload.get("type")
        if etype == "agent_message":
            st.saw_agent_message = True
            text = str(payload.get("message") or "")
            if text:
                prov = _make_provenance(st.session_id, st.model, ts, root)
                ek = f"codex:{st.session_id}:msg:{line_no}"
                obs_batch.append(_import_obs(ts, root, cfg, "assistant_message", text, prov,
                                                          event_key=ek,
                                                          meta={"transcript_ref": transcript_ref}))
        elif etype == "task_complete":
            text = str(payload.get("last_agent_message") or "")
            if text and not st.saw_agent_message:
                prov = _make_provenance(st.session_id, st.model, ts, root)
                ek = f"codex:{st.session_id}:msg:{line_no}"
                obs_batch.append(_import_obs(ts, root, cfg, "assistant_message", text, prov,
                                                          event_key=ek,
                                                          meta={"transcript_ref": transcript_ref}))
        elif etype == "item_completed" and isinstance(payload.get("item"), Mapping) \
                and payload["item"].get("type") == "CommandExecution":
            _note_exec_exit(payload["item"], pending_call, cfg)
        elif etype == "user_message":
            text = str(payload.get("message") or "")[:500]
            if text.startswith(_SIDECAR_PROMPT_PREFIX):
                _become_sidecar(st, obs_batch, events_batch)
                return
            if text:
                prov = _make_provenance(st.session_id, st.model, ts, root)
                ek = f"codex:{st.session_id}:prompt:{I.sha256_text(text)}"
                obs_batch.append(_import_obs(ts, root, cfg, "user_prompt", text, prov,
                                                          event_key=ek))
        else:
            # mcp_tool_call_end {invocation:{server, tool}, result}, item_completed {item:{McpToolCall ...}}, ...
            obs_id = _mcp_record_obs_id(payload)
            if obs_id:
                events_batch.append(_link_event(obs_id, st, ts))
        return
    # reasoning / token_count / anything else: ignored


def _finish_running(info: Dict[str, Any], output: str, call_id: Any, st: "_FileState", ts: Optional[str],
                    root: Path, cfg: Mapping[str, Any], obs_batch: List[Any],
                    pending_call: Dict[str, Dict[str, Any]], transcript_ref: str) -> None:
    """a write_stdin result for a command that was still running. Its output is appended to
    the command's; once it reports the exit code, ONE command observation for the ORIGINAL command
    is recorded with the real exit code and runner summary (the earlier "running" one says nothing
    about the outcome)."""
    key = f"proc:{info.get('proc')}"
    proc = pending_call.get(key)
    if not proc:
        return
    exit_code = _parse_exit_code(output)
    body = _strip_chunk_header(output)
    cmd = str(proc.get("cmd") or "")
    proc["out"] = (str(proc.get("out") or "") + "\n" + _redacted_tail(body, cfg, cmd))[-_PROC_OUT_KEEP:]
    if exit_code is None:
        return
    pending_call.pop(key, None)
    full = str(proc.get("out") or "")
    summary = _deps.get_test_summary(cmd, full)
    tool = I.ToolInfo(name="exec_command", command=cmd, exit_code=exit_code,
                      status="ok" if exit_code == 0 else "error", test=summary)
    ek = f"codex:{st.session_id}:{proc.get('call_id')}:exit"
    text = f"$ {cmd}\n{full}\nProcess exited with code {exit_code}"
    _command_obs(st, ts, root, cfg, tool, text, full, proc.get("cwd"), ek,
                 {"transcript_ref": transcript_ref, "completed_by": str(call_id)}, obs_batch)


def _command_obs(st: "_FileState", ts: Optional[str], root: Path, cfg: Mapping[str, Any], tool: "I.ToolInfo",
                 text: str, output: str, cwd: Optional[str], ek: str, meta: Dict[str, Any],
                 obs_batch: List[Any]) -> None:
    """One imported command observation. A command run while this session had edited files it had
    not committed yet ran on a DIRTY tree (provenance.git_dirty=True, meta.dirty_inferred = those files): the
    commit that was HEAD then is not what it tested. A successful `git commit` clears the session's edits."""
    prov = _make_provenance(st.session_id, st.model, ts, root, cwd=cwd or st.cwd)
    cmd = tool.command or ""
    committed = bool(GIT_COMMIT_CMD_RE.search(cmd)) and (
        tool.exit_code == 0 or (tool.exit_code is None and new_commit_sha(output) is not None))
    if committed:
        st.uncommitted = []
    elif st.uncommitted:
        prov.git_dirty = True
        meta = dict(meta, dirty_inferred=list(st.uncommitted[:20]))
    obs_batch.append(_import_obs(ts, root, cfg, "command", text, prov, tool=tool, event_key=ek, meta=meta))


def _strip_chunk_header(output: str) -> str:
    """Drop Codex's `Chunk ID / Wall time / Process ... / Original token count / Output:` header."""
    head, sep, rest = output.partition("\nOutput:\n")
    if sep and len(head) < 400 and "Chunk ID" in head:
        return rest
    return output


def _patch_path(fpath: str, root: Path) -> str:
    """apply_patch paths are usually relative to the session cwd; an absolute one inside the
    project is stored project-relative so it matches the Claude/Cursor file_edit paths."""
    fpath = fpath.strip()
    try:
        pp = Path(fpath)
        if pp.is_absolute():
            return pp.resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        pass
    return fpath


def _handle_custom_tool(kind: str, payload: Dict[str, Any], st: _FileState, ts: Optional[str],
                        root: Path, transcript_ref: str, obs_batch: List[Any],
                        pending_call: Dict[str, Dict[str, Any]], cfg: Mapping[str, Any],
                        events_batch: Optional[List[Any]] = None) -> None:
    events_batch = events_batch if events_batch is not None else []
    if kind == "custom_tool_call":
        name = payload.get("name")
        call_id = payload.get("call_id")
        if not call_id:
            return
        if name == "apply_patch":
            # may sit in the cursor file until its output line is read -> redacted
            patch = _relative_patch(str(payload.get("input") or "")[:_PATCH_KEEP], root)
            pending_call[call_id] = {"kind": "patch", "ts": ts, "redacted": True, "input": _redacted(patch, cfg)}
        elif name == "exec":
            # code mode -- the real tool calls are inside the JS snippet
            calls = []
            for c in _code_mode_calls(str(payload.get("input") or "")[:_PATCH_KEEP]):
                if "cmd" in c:
                    c["cmd"] = _redacted(c["cmd"], cfg, git_cmd=c["cmd"])
                    c["cwd"] = _workdir({"workdir": c.pop("workdir", None)}, st)
                if "patch" in c:
                    c["patch"] = _redacted(_relative_patch(c["patch"][:_PATCH_KEEP], root), cfg)
                calls.append(c)
            if calls:
                pending_call[call_id] = {"kind": "code", "ts": ts, "redacted": True, "calls": calls[:32]}
        elif isinstance(name, str) and name:
            pending_call[call_id] = {"kind": "custom", "name": name[:80], "ts": ts, "redacted": True}
        return
    call_id = payload.get("call_id")
    info = pending_call.pop(call_id, None)
    if info is None:
        return
    try:
        if info.get("kind") == "code":
            _finish_code_mode(info, payload.get("output"), call_id, st, ts, root, cfg, obs_batch, events_batch,
                              transcript_ref)
            return
        if info.get("kind") == "custom":
            # an unknown custom tool: one generic tool observation, never a crash
            body, failed = _code_mode_output(payload.get("output"))
            tool = I.ToolInfo(name=str(info.get("name") or "custom"), status="error" if failed else "ok")
            prov = _make_provenance(st.session_id, st.model, ts, root)
            obs_batch.append(_import_obs(ts, root, cfg, "search", f"{tool.name}: {body}"[:1500], prov, tool=tool,
                                         event_key=f"codex:{st.session_id}:custom:{call_id}",
                                         meta={"transcript_ref": transcript_ref}))
            return
    except Exception:
        return
    if info.get("kind") != "patch":
        return
    output = payload.get("output")
    out_text = output if isinstance(output, str) else json.dumps(output or "")
    status = "error" if re.search(r'(?i)"exit_code"\s*:\s*[1-9]|\bfailed to apply|\berror:', out_text) else "ok"
    _emit_patch_edits(info["input"], call_id, status, st, ts, root, transcript_ref, obs_batch, cfg)


# --------------------------------------------------------------------------- code mode
# codex-cli 0.154 "code mode": the model calls ONE custom tool `exec` whose input is a JS snippet that calls the
# real tools -- `await tools.exec_command({cmd:"...", workdir:"..."})`, `tools.apply_patch(patch)`,
# `tools.mcp__hearmemory__hearmemory_recall({...})` -- and the custom_tool_call_output is a list of {type: "input_text",
# text} parts starting with "Script completed\nWall time ...\nOutput:\n". Before this, such sessions produced no
# command / file_edit observations at all. The snippet is parsed best-effort (string literals, arrays of them,
# and `const x = "..."` variables); an inner exec_command's exit code comes from the CommandExecution
# item_completed event Codex writes between the call and its output (_note_exec_exit).
_CODE_EXEC_TOOLS = ("exec_command", "shell", "local_shell", "container.exec")
_JS_CALL_RE = re.compile(r"\btools\s*\.\s*([A-Za-z_$][\w$]*)\s*\(")
_JS_KEY_FMT = r"(?:^|[{,\s])[\"']?%s[\"']?\s*:\s*"
_SCRIPT_HEAD_RE = re.compile(r"^\s*Script (\w+)[^\n]*\n(?:Wall time[^\n]*\n)?(?:Output:\n)?\n?")
_JS_ESC = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}


def _js_string(src: str, i: int):
    """(value, end index) of the JS string literal starting at src[i] ('..', ".." or `..`; a template's ${..}
    stays literal). (None, len) when unterminated."""
    q = src[i]
    out: List[str] = []
    j = i + 1
    while j < len(src):
        ch = src[j]
        if ch == "\\" and j + 1 < len(src):
            nx = src[j + 1]
            try:
                if nx == "u" and src[j + 2:j + 3] == "{":
                    k = src.index("}", j + 3)
                    out.append(chr(int(src[j + 3:k], 16)))
                    j = k + 1
                    continue
                if nx in "ux":
                    n = 4 if nx == "u" else 2
                    out.append(chr(int(src[j + 2:j + 2 + n], 16)))
                    j += 2 + n
                    continue
            except ValueError:
                pass
            if nx != "\n":                              # "\<newline>" is a line continuation
                out.append(_JS_ESC.get(nx, nx))
            j += 2
            continue
        if ch == q:
            return "".join(out), j + 1
        out.append(ch)
        j += 1
    return None, len(src)


def _js_close(src: str, i: int) -> int:
    """Index of the bracket closing the one just before src[i] (string-aware), or len(src)."""
    depth = 1
    j = i
    while j < len(src):
        ch = src[j]
        if ch in "'\"`":
            j = _js_string(src, j)[1]
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return len(src)


def _js_value(src: str, i: int, whole: str, depth: int = 0) -> Any:
    """The string / array-of-strings value at src[i]: a literal, an array of literals, or a variable the
    snippet declares with one. None otherwise (computed values are not evaluated)."""
    while i < len(src) and src[i].isspace():
        i += 1
    if i >= len(src) or depth > 3:
        return None
    ch = src[i]
    if ch in "'\"`":
        return _js_string(src, i)[0]
    if ch == "[":
        end = _js_close(src, i + 1)
        items, j = [], i + 1
        while j < end:
            if src[j] in "'\"`":
                v, j = _js_string(src, j)
                if v is not None:
                    items.append(v)
                continue
            j += 1
        return items
    m = re.match(r"[A-Za-z_$][\w$]*", src[i:])
    if m:
        d = re.search(r"\b(?:const|let|var)\s+%s\s*=\s*" % re.escape(m.group(0)), whole)
        if d:
            return _js_value(whole, d.end(), whole, depth + 1)
    return None


def _js_key(args: str, key: str, whole: str) -> Any:
    m = re.search(_JS_KEY_FMT % re.escape(key), args)
    return _js_value(args, m.end(), whole) if m else None


def _code_mode_calls(src: str) -> List[Dict[str, Any]]:
    """The `tools.<name>(...)` calls of a code-mode snippet, in order: {"tool", "cmd", "workdir"} for commands,
    {"tool", "patch"} for apply_patch, {"tool"} otherwise."""
    out: List[Dict[str, Any]] = []
    for m in _JS_CALL_RE.finditer(src):
        name = m.group(1)
        args = src[m.end():_js_close(src, m.end())].strip()
        call: Dict[str, Any] = {"tool": name}
        if name in _CODE_EXEC_TOOLS:
            cmd = _js_key(args, "cmd", src)
            if cmd is None:
                cmd = _js_key(args, "command", src)
            call["cmd"] = _cmd_text({"cmd": cmd}) if isinstance(cmd, (str, list)) else ""
            wd = _js_key(args, "workdir", src)
            call["workdir"] = wd if isinstance(wd, str) else None
        elif name == "apply_patch":
            v = (_js_key(args, "input", src) or _js_key(args, "patch", src)) if args.startswith("{") \
                else _js_value(args, 0, src)
            call["patch"] = v if isinstance(v, str) and "*** Begin Patch" in v else (_patch_from_command(src) or "")
        out.append(call)
    return out


def _output_parts(output: Any) -> List[str]:
    if isinstance(output, list):
        return [p for x in output for p in _output_parts(x)]
    if isinstance(output, dict):
        v = output.get("text", output.get("output", output.get("content")))
        return _output_parts(v) if v is not None else []
    if isinstance(output, str):
        s = output.strip()
        if s.startswith("[{") and s.endswith("}]"):
            # the output is sometimes a stringified list of {'type': 'input_text', 'text': ...} parts
            for parse in (json.loads, ast.literal_eval):
                try:
                    v = parse(s)
                except Exception:
                    continue
                if isinstance(v, list):
                    return _output_parts(v)
        return [output]
    return [] if output is None else [str(output)]


def _code_mode_output(output: Any):
    """(body, script_failed): the snippet's output without the "Script completed / Wall time / Output:" head."""
    text = "\n".join(_output_parts(output))
    m = _SCRIPT_HEAD_RE.match(text)
    if not m:
        return text, False
    return text[m.end():], m.group(1).lower() != "completed"


def _note_exec_exit(item: Mapping[str, Any], pending_call: Dict[str, Dict[str, Any]],
                    cfg: Optional[Mapping[str, Any]]) -> None:
    """A CommandExecution item_completed event: its exit code belongs to the matching inner exec_command of the
    newest pending code-mode call (code mode's own output never states it)."""
    if item.get("exit_code") is None:
        return
    raw = _cmd_text({"cmd": item.get("command")})
    red = _redacted(raw, cfg, git_cmd=raw)
    for info in reversed(list(pending_call.values())):
        if isinstance(info, dict) and info.get("kind") == "code":
            for c in info.get("calls") or ():
                if c.get("tool") in _CODE_EXEC_TOOLS and "exit" not in c and c.get("cmd") in (red, raw):
                    try:
                        c["exit"] = int(item["exit_code"])
                    except (TypeError, ValueError):
                        pass
                    return
            return


def _finish_code_mode(info: Mapping[str, Any], output: Any, call_id: Any, st: "_FileState", ts: Optional[str],
                      root: Path, cfg: Mapping[str, Any], obs_batch: List[Any], events_batch: List[Any],
                      transcript_ref: str) -> None:
    """The observations of one code-mode snippet, one per inner call, as the function_call path makes them. The
    snippet's outputs cannot be told apart: the LAST call carries the combined output (meta.combined_output); a
    test command that is the snippet's only one gets its runner summary from it wherever it stands."""
    body, failed = _code_mode_output(output)
    calls = [c for c in info.get("calls") or () if isinstance(c, dict)]
    hearmemory_cmds = [str(c.get("cmd") or "") for c in calls
                  if c.get("tool") in _CODE_EXEC_TOOLS and _is_hearmemory_cli(str(c.get("cmd") or ""))]
    if hearmemory_cmds:
        # link what `hearmemory record` recorded; hearmemory's own output is never part of a command's output
        for oid in dict.fromkeys(i for cmd in hearmemory_cmds for i in _cli_record_ids(cmd, body)):
            events_batch.append(_link_event(oid, st, ts))
        body = strip_hearmemory_output(body)
    n = len(calls)
    test_idx = [i for i, c in enumerate(calls) if c.get("tool") in _CODE_EXEC_TOOLS
                and _deps.get_test_summary(c.get("cmd") or "", body) is not None]
    for i, c in enumerate(calls):
        last = i == n - 1
        sub = str(call_id) if n == 1 else f"{call_id}:{i}"
        meta: Dict[str, Any] = {"transcript_ref": transcript_ref, "code_mode": True}
        if n > 1:
            meta.update({"code_mode_calls": n, "combined_output": last})
        name = str(c.get("tool") or "")
        out_i = body if last else ""
        if name in _CODE_EXEC_TOOLS:
            cmd = str(c.get("cmd") or "")
            if not cmd:
                continue
            exit_code = c.get("exit")
            if _APPLY_PATCH_CMD_RE.match(cmd) and _patch_from_command(cmd):
                bad = (exit_code not in (None, 0)) or (last and failed)
                _emit_patch_edits(_patch_from_command(cmd) or "", sub, "error" if bad else "ok", st, ts, root,
                                  transcript_ref, obs_batch, cfg)
                continue
            if _is_hearmemory_cli(cmd) and hearmemory_only_command(cmd):
                continue
            summary = _deps.get_test_summary(cmd, body) if (last or test_idx == [i]) else None
            if summary is not None and not last:
                meta["test_summary_from"] = "combined_output"
            if exit_code is not None:
                status = "ok" if exit_code == 0 else "error"
            else:
                status = "error" if (last and failed) else "unknown"
            tool = I.ToolInfo(name=name, command=cmd, exit_code=exit_code, status=status, test=summary)
            text = f"$ {cmd}\n{out_i}" if (last or not body) else \
                f"$ {cmd}\n[hearmemory: output combined with the snippet's last call]"
            _command_obs(st, ts, root, cfg, tool, text, out_i, c.get("cwd"),
                         f"codex:{st.session_id}:{sub}", meta, obs_batch)
        elif name == "apply_patch":
            patch = str(c.get("patch") or "")
            if patch:
                bad = failed or bool(re.search(r"(?i)\bfailed to apply|\berror:", body[:_EXIT_SCAN_CHARS]))
                _emit_patch_edits(patch, sub, "error" if bad else "ok", st, ts, root, transcript_ref, obs_batch, cfg)
        elif name.startswith("mcp__hearmemory__") or name.startswith("hearmemory"):
            # hearmemory's own tools: a record is linked to this session; recall / check / issues output is hearmemory's
            # own text and is never imported (memory echo)
            if _HEARMEMORY_RECORD_NAME_RE.search(name):
                ids = re.findall(re.escape(I.RECORD_ECHO_PREFIX) + r"(o-[0-9a-f]{16})", body) \
                    + re.findall(r'"obs_id"\s*:\s*"(o-[0-9a-f]{16})"', body)
                for oid in dict.fromkeys(ids):
                    events_batch.append(_link_event(oid, st, ts))
        else:
            tool = I.ToolInfo(name=name[:80], status="error" if (last and failed) else "ok")
            prov = _make_provenance(st.session_id, st.model, ts, root)
            obs_batch.append(_import_obs(ts, root, cfg, "search", f"{name}: {out_i}"[:1500], prov, tool=tool,
                                         event_key=f"codex:{st.session_id}:custom:{sub}", meta=meta))


def _relative_patch(patch: str, root: Path) -> str:
    """code mode patches name files by ABSOLUTE path; made project-relative BEFORE redaction
    (redaction would otherwise turn a project path equal to an env value into "[REDACTED:env]...")."""
    return _PATCH_FILE_RE.sub(lambda m: m.group(0)[:m.start(2) - m.start(0)] + _patch_path(m.group(2), root), patch)


def _patch_from_command(cmd: str) -> Optional[str]:
    """The `*** Begin Patch ... *** End Patch` body of an `apply_patch <<'EOF' ... EOF` command."""
    i = cmd.find("*** Begin Patch")
    if i < 0:
        return None
    j = cmd.find("*** End Patch", i)
    return cmd[i:(j + len("*** End Patch")) if j >= 0 else len(cmd)]


def _emit_patch_edits(patch: str, call_id: Any, status: str, st: "_FileState", ts: Optional[str], root: Path,
                      transcript_ref: str, obs_batch: List[Any], cfg: Mapping[str, Any]) -> None:
    """One file_edit observation per `*** Add/Update/Delete File:` section (text = that section, <= 60
    lines, with both the removed and the added lines)."""
    prov = _make_provenance(st.session_id, st.model, ts, root, subagent_type=st.subagent_type)
    for m in _PATCH_FILE_RE.finditer(patch):
        action, raw_path = m.group(1), m.group(2).strip()
        fpath = _patch_path(raw_path, root)
        start = m.end()
        next_m = _PATCH_FILE_RE.search(patch, start)
        end = next_m.start() if next_m else len(patch)
        seg_lines = [ln for ln in patch[start:end].splitlines() if not ln.startswith("*** End Patch")]
        segment = "\n".join(seg_lines[:60])
        ek = f"codex:{st.session_id}:patch:{call_id}:{fpath}"
        tool = I.ToolInfo(name="apply_patch", command=action, paths=[fpath], status=status)
        obs_batch.append(_import_obs(ts, root, cfg, "file_edit", segment, prov, tool=tool,
                                                  event_key=ek, meta={"transcript_ref": transcript_ref}))
        if status == "ok" and fpath not in st.uncommitted:
            st.uncommitted.append(fpath)


def _message_text(payload: Dict[str, Any]) -> str:
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict) and c.get("text"):
                parts.append(c["text"])
            elif isinstance(c, str):
                parts.append(c)
        return "\n".join(parts)
    return ""


def _link_event(obs_id: str, st: _FileState, ts: Optional[str]) -> "I.ControlEvent":
    prov = I.Provenance(host="codex", session_id=st.session_id, source="import:codex_rollout")
    return I.ControlEvent(id=I.stable_id("ev-link-", obs_id, st.session_id, ts), ts=_deps.now_ts(),
                          kind="provenance_link", target=obs_id, data={}, provenance=prov)


def _is_hearmemory_record_call(name: Any, payload: Mapping[str, Any]) -> bool:
    """A Codex MCP call of hearmemory's hearmemory_record, whatever the naming scheme of this Codex version:
    `mcp__hearmemory__hearmemory_record`, `hearmemory__hearmemory_record`, `hearmemory.hearmemory_record`, or `hearmemory_record` with a
    separate namespace / server field (with only the two old names recognised, the real
    codex-cli 0.154 session produced no link for its hearmemory_record call)."""
    if not isinstance(name, str) or not _HEARMEMORY_RECORD_NAME_RE.search(name):
        return False
    if name == "hearmemory_record":
        ns = str(payload.get("namespace") or payload.get("server") or payload.get("server_name") or "hearmemory")
        return "hearmemory" in ns
    return True


def _mcp_record_obs_id(payload: Any) -> Optional[str]:
    """obs_id from an MCP tool-call record of hearmemory_record in any of the shapes Codex writes
    (event_msg mcp_tool_call_end {invocation:{server,tool}, result}, item_completed {item:{type:
    "McpToolCall", server, tool, result}}, response_item mcp_* items). None for anything else."""
    if not isinstance(payload, Mapping):
        return None
    for inv in (payload.get("invocation"), payload.get("item"), payload):
        if not isinstance(inv, Mapping):
            continue
        server = inv.get("server") or inv.get("server_name")
        tool = inv.get("tool") or inv.get("tool_name") or inv.get("name")
        if server == "hearmemory" and isinstance(tool, str) and _HEARMEMORY_RECORD_NAME_RE.search(tool):
            for res in (payload.get("result"), inv.get("result"), payload.get("output"), inv.get("output")):
                if res is None:
                    continue
                text = res if isinstance(res, str) else json.dumps(res, ensure_ascii=False)
                oid = _extract_obs_id(text)
                if oid:
                    return oid
    return None


# --------------------------------------------------------------------------- session identity
# launch.sh gives the MCP server HEARMEMORY_SESSION_ID="codex-<epoch>-<pid>"; the rollout has its own session id.
# Every launch (proxy session) is registered in state/codex_launches.json; after an import, each MAIN
# rollout of this project is assigned to the latest launch that started before it, and a launch with
# exactly one such rollout gets a ControlEvent(kind="session_alias"): from then on all of its MCP / CLI
# records are that rollout session's own records (one Codex session = one actor).
def launch_epoch(session_id: Optional[str], now: Optional[float] = None) -> Optional[float]:
    if not session_id:
        return None
    m = _LAUNCH_ID_RE.match(session_id) or _MCP_ID_RE.match(session_id)
    if m:
        return float(m.group(1))
    return now


def register_launch(store: Any, session_id: Optional[str], *, ended: bool = False,
                    now: Optional[float] = None) -> bool:
    """Remember a Codex proxy session id (idempotent; `ended` marks that launch.sh saw Codex exit)."""
    if not session_id:
        return False
    now = time.time() if now is None else float(now)
    reg = (store.read_state("codex_launches") or {}) if hasattr(store, "read_state") else {}
    launches = reg.setdefault("launches", {})
    ent = launches.get(session_id)
    changed = False
    if ent is None:
        epoch = launch_epoch(session_id, now)
        ent = launches[session_id] = {"epoch": epoch, "alias": None}
        changed = True
    if ended and not ent.get("ended"):
        ent["ended"] = now
        changed = True
    if changed:
        if len(launches) > 500:                      # bounded: keep the newest launches
            for k in sorted(launches, key=lambda k: launches[k].get("epoch") or 0)[:len(launches) - 500]:
                launches.pop(k, None)
        store.write_state("codex_launches", reg)
    return changed


def assign_launches(launches: Mapping[str, Mapping[str, Any]], rollouts: List[Dict[str, Any]]
                    ) -> Dict[str, List[Dict[str, Any]]]:
    """Pure: {launch id: [main rollouts that belong to it]}. A rollout belongs to the latest launch whose
    epoch <= rollout start + LAUNCH_SLACK_S, started within LAUNCH_MAX_GAP_S of it, and (when the launch
    is known to have ended) started before it ended."""
    order = sorted(((float(v.get("epoch")), k) for k, v in launches.items() if v.get("epoch") is not None))
    out: Dict[str, List[Dict[str, Any]]] = {k: [] for _, k in order}
    for r in rollouts:
        start = r.get("start")
        if start is None:
            continue
        best = None
        for epoch, k in order:
            if epoch <= start + LAUNCH_SLACK_S:
                best = (epoch, k)
        if best is None:
            continue
        epoch, k = best
        ended = launches[k].get("ended")
        if start - epoch > LAUNCH_MAX_GAP_S or (ended is not None and start > float(ended) + LAUNCH_SLACK_S):
            continue
        out[k].append(r)
    return out


def reconcile_sessions(store: Any, cfg: Optional[Mapping[str, Any]] = None) -> Dict[str, str]:
    """Emit session_alias events for launches that now map to exactly one main rollout. Returns
    {launch id: rollout session id} for every aliased launch (old and new)."""
    reg = store.read_state("codex_launches") or {}
    launches: Dict[str, Dict[str, Any]] = reg.get("launches") or {}
    done = {k: v["alias"] for k, v in launches.items() if v.get("alias")}
    if not launches or len(done) == len(launches):
        return done
    cursors = store.read_state("cursors") or {}
    rollouts = []
    for path, e in ((cursors.get("import") or {}).get("codex") or {}).items():
        if not isinstance(e, dict) or not e.get("match") or e.get("_sidecar") or not e.get("_main", True):
            continue
        start = _parse_epoch(e.get("_start_ts"))
        if start is None or not e.get("session_id"):
            continue
        rollouts.append({"session_id": e["session_id"], "start": start, "path": os.path.basename(path)})
    assigned = assign_launches(launches, rollouts)
    events = []
    changed = False
    for k, rs in assigned.items():
        ent = launches[k]
        if ent.get("alias"):
            continue
        sids = sorted({r["session_id"] for r in rs})
        if len(sids) == 1:
            r = next(r for r in rs if r["session_id"] == sids[0])
            prov = I.Provenance(host="codex", session_id=sids[0], source="import:codex_rollout")
            target = I.session_alias_target("codex", k)
            events.append(I.ControlEvent(id=I.stable_id("ev-alias-", target, sids[0]), ts=_deps.now_ts(),
                                         kind="session_alias", target=target, provenance=prov,
                                         data={"reason": "launch_window", "rollout": r["path"],
                                               "launch_epoch": ent.get("epoch"), "rollout_start": r["start"]}))
            ent["alias"] = sids[0]
            ent.pop("ambiguous", None)
            done[k] = sids[0]
            changed = True
        elif len(sids) > 1 and ent.get("ambiguous") != sids:
            ent["ambiguous"] = sids          # several rollouts in one launch (/new): per-record links only
            changed = True
    if events:
        store.append_events(events)
    if changed:
        store.write_state("codex_launches", reg)
    return done


def _parse_epoch(ts: Any) -> Optional[float]:
    if not ts:
        return None
    try:
        from datetime import datetime
        s = str(ts).replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


def sync_session(store: Any, cfg: Mapping[str, Any], proxy_session_id: Optional[str] = None, *,
                 budget_s: float = 1.0, ended: bool = False) -> Dict[str, Any]:
    """Called before a Codex agent's record / recall / check (and by launch.sh after Codex exits):
    register the proxy session, import pending rollout lines within `budget_s` (only when the pipeline
    lock is free -- a busy worker is importing already), then reconcile launches with rollouts.
    Never raises; returns {"alias": <rollout session id or None>, "imported": n}."""
    out: Dict[str, Any] = {"alias": None, "imported": 0}
    try:
        if proxy_session_id:
            register_launch(store, proxy_session_id, ended=ended)
        if budget_s and budget_s > 0:
            lock_fn = _deps.file_lock
            lock_path = Path(str(getattr(store, "hearmemory_dir", Path(store.root) / I.HEARMEMORY_DIRNAME))) / "locks" / "pipeline.lock"
            if lock_fn is not None and lock_path.parent.is_dir():
                with lock_fn(str(lock_path), 0.0) as got:
                    if got:
                        out["imported"] = import_rollouts(store, cfg, deadline_s=budget_s) or 0
        aliases = reconcile_sessions(store, cfg)
        out["alias"] = aliases.get(proxy_session_id) if proxy_session_id else None
    except Exception:
        pass
    return out


def bounded_import(store: Any, cfg: Mapping[str, Any], budget_s: float) -> None:
    """Hook-side import: cooperative deadline inside import_rollouts (stops between lines,
    saves the cursor per file) PLUS a hard wall-clock backstop via run_with_timeout, so a hook
    never waits more than ~budget_s on Codex history no matter how large it is. Whatever is left
    is picked up by the next hook or by the background worker."""
    if budget_s <= 0:
        return

    fn = import_rollouts  # module global, so a monkeypatched stand-in is honoured
    try:
        takes_deadline = "deadline_s" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        takes_deadline = False

    def _do() -> None:
        if takes_deadline:
            fn(store, cfg, deadline_s=budget_s)
        else:  # a stand-in without the deadline keyword (tests / older builds)
            fn(store, cfg)
    _deps.run_with_timeout(_do, budget_s + 0.05)


def normalize(event: str, payload: Mapping[str, Any]) -> List[Any]:
    # Live Codex hooks are experimental; the durable path is import_rollouts above.
    return []


def handle_hook(event: str, payload: Mapping[str, Any]) -> "I.HookResult":
    root = Path(payload.get("_root") or payload.get("project") or ".")
    cfg = payload.get("_cfg") or dict(I.DEFAULT_CONFIG)
    if event == "Stop":
        store = _deps.open_store(root) if _deps.open_store else None
        if store is not None:
            DeadlineCls = _deps.Deadline or _deps.FallbackDeadline
            try:
                slice_ms = DeadlineCls("import", cfg.get("hooks", {})).slice_ms("import_codex")
            except Exception:
                slice_ms = float(I.HOOK_STEP_BUDGETS_MS["import"]["import_codex"])
            bounded_import(store, cfg, slice_ms / 1000.0)
        if _deps.spawn_worker:
            try:
                _deps.spawn_worker(root, launched_by="hook:codex:Stop")  # finishes a partial import
            except Exception:
                pass
        return I.HookResult(exit_code=0)
    if event == "SessionStart":
        from hearmemory.host import hooks as _hooks
        return _hooks.generic_handle_hook("codex", event, payload)
    return I.HookResult(exit_code=0)


# --------------------------------------------------------------------------- install
def install(root: Path, python: str, cfg: Mapping[str, Any], *, with_hooks: bool = False,
            records: Optional[List["I.InstallRecord"]] = None) -> List["I.InstallRecord"]:
    """AGENTS.md block + `.hearmemory/host/codex/` files. An AGENTS.md that is a symlink to a file
    outside the project (e.g. a user-global AGENTS.md) is skipped with a warning, never edited
    through the link. `records` is appended to as each file is written."""
    root = Path(root)
    records = [] if records is None else records
    agents_path = root / AGENTS_MD
    if M.target_ok(agents_path, root, AGENTS_MD):
        block = S.codex_agents_block()
        created_new, _unchanged = M.insert_marker_block(agents_path, block, S.BEGIN_MARKER, S.END_MARKER)
        records.append(I.InstallRecord(path=AGENTS_MD, action="block_inserted", host="codex",
                                        sha256_after=M.sha256_of(agents_path.read_text(encoding="utf-8")),
                                        created_file=created_new))

    host_dir = root / I.HEARMEMORY_DIRNAME / "host" / "codex"
    launch = S.codex_launch_sh(python, str(root))
    path = host_dir / "launch.sh"
    if M.target_ok(path, root, "codex launch.sh"):
        created_new, created_dirs = M.write_file(path, launch, executable=True)
        records.append(I.InstallRecord(path=_rel(path, root), action="created", host="codex",
                                        sha256_after=M.sha256_of(launch), created_file=created_new,
                                        created_parent_dirs=created_dirs))
    if with_hooks:
        hooks_json = S.codex_hooks_json(python, str(root))
        text = json.dumps(hooks_json, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        hpath = host_dir / "hooks.json"
        if M.target_ok(hpath, root, "codex hooks.json"):
            created_new, created_dirs = M.write_file(hpath, text)
            records.append(I.InstallRecord(path=_rel(hpath, root), action="created", host="codex",
                                            sha256_after=M.sha256_of(text), created_file=created_new,
                                            created_parent_dirs=created_dirs))
    return records


def _rel(p: Path, root: Path) -> str:
    return M.rel_or_abs(p, root)
