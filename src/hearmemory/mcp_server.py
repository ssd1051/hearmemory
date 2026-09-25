"""the hearmemory MCP server.

Zero-dependency stdio JSON-RPC 2.0 server exposing five tools (`hearmemory_recall`,
`hearmemory_record`, `hearmemory_check`, `hearmemory_issues`, `hearmemory_status`). One JSON
object per line on stdin, one JSON object per line on stdout; nothing else
ever touches stdout (logs/diagnostics go to stderr). No exception may ever
reach `serve()`'s caller or stop the read loop: every failure is
turned into either a JSON-RPC error object or, for `tools/call`, an
`isError: true` tool result.

`serve(root, host, stdin, stdout) -> int` is the ENTRY_POINTS `mcp_serve`.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Mapping, Optional, TextIO

from hearmemory.interfaces import (
    MCP_PROTOCOL_VERSIONS,
    RECORD_ECHO_PREFIX,
    canonical_json,
)
from hearmemory import commands
from hearmemory import render_cli

try:
    import hearmemory as _hearmemory_pkg
    _SERVER_VERSION = getattr(_hearmemory_pkg, "__version__", "0.1.0")
except Exception:  # pragma: no cover - hearmemory/__init__.py always exists
    _SERVER_VERSION = "0.1.0"

_INSTRUCTIONS = (
    "hearmemory: shared project memory. Call hearmemory_recall with brief=true at the start of a task and read the "
    "result. Record conclusions with hearmemory_record. Run hearmemory_check before finishing a task or committing."
)


class _RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class _ParamsError(_RpcError):
    def __init__(self, message: str):
        super().__init__(-32602, message)


class _ToolError(Exception):
    """A tool-level failure: rendered as an `isError: true` tool RESULT, never a JSON-RPC error."""


class _NotInitialised(_ToolError):
    def __init__(self):
        super().__init__("hearmemory: this project is not initialised (or was uninstalled); run `hearmemory init`.")


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------
def _schema(properties: Mapping[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    s: Dict[str, Any] = {"type": "object", "properties": dict(properties), "additionalProperties": False}
    if required:
        s["required"] = list(required)
    return s


TOOLS: List[Dict[str, Any]] = [
    {
        "name": "hearmemory_recall",
        "description": "Recall relevant project memory (or, with brief=true, the session-start briefing): "
                        "refuted/disputed claims, open issues and new facts from other agents.",
        "inputSchema": _schema(
            {
                "query": {"type": "string", "description": "free-text query; \"\" is fine when brief=true"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                "include_archive": {"type": "boolean"},
                "brief": {"type": "boolean"},
                "paths": {"type": "array", "items": {"type": "string"}},
            },
            required=["query"],
        ),
    },
    {
        "name": "hearmemory_record",
        "description": "Record a note, an explicit claim, or open an issue in the shared project memory.",
        "inputSchema": _schema(
            {
                "text": {"type": "string", "maxLength": 4000},
                "kind": {"type": "string", "enum": ["note", "claim", "issue"]},
                "paths": {"type": "array", "items": {"type": "string"}},
                "refs": {"type": "array", "items": {"type": "string"}},
                "agent_label": {"type": "string"},
            },
            required=["text"],
        ),
    },
    {
        "name": "hearmemory_check",
        "description": "Check a conclusion or a commit against project memory for refuted claims and open "
                        "issues (advisory only over MCP: this never blocks).",
        "inputSchema": _schema(
            {
                "text": {"type": "string"},
                "paths": {"type": "array", "items": {"type": "string"}},
                "action": {"type": "string", "enum": ["git_commit", "claim", "finish"]},
                "staged": {"type": "boolean"},
            }
        ),
    },
    {
        "name": "hearmemory_issues",
        "description": "List, show, open, close or reopen project issues.",
        "inputSchema": _schema(
            {
                "action": {"type": "string", "enum": ["list", "show", "open", "close", "reopen"]},
                "id": {"type": "string"},
                "title": {"type": "string"},
                "reason": {"type": "string"},
                "status": {"type": "string", "enum": ["open", "all"]},
            }
        ),
    },
    {
        "name": "hearmemory_status",
        "description": "Summarise this project's hearmemory memory: counts, open issues, Jev availability, worker.",
        "inputSchema": _schema({}),
    },
]
_TOOL_NAMES = {t["name"] for t in TOOLS}


def _check_type(value: Any, expected: str, path: str) -> None:
    ok = {"string": isinstance(value, str), "integer": isinstance(value, int) and not isinstance(value, bool),
          "boolean": isinstance(value, bool), "array": isinstance(value, list),
          "object": isinstance(value, dict)}.get(expected, True)
    if not ok:
        raise _ParamsError(f"{path}: expected {expected}")


def _validate(name: str, arguments: Mapping[str, Any]) -> None:
    schema = next(t["inputSchema"] for t in TOOLS if t["name"] == name)
    props = schema["properties"]
    for k in arguments:
        if k not in props:
            raise _ParamsError(f"unknown argument {k!r} for {name}")
    for req in schema.get("required", []):
        if req not in arguments:
            raise _ParamsError(f"missing required argument {req!r} for {name}")
    for k, v in arguments.items():
        spec = props[k]
        _check_type(v, spec.get("type", "any"), k)
        if spec.get("type") == "array":
            for i, item in enumerate(v):
                if not isinstance(item, str):
                    raise _ParamsError(f"{k}[{i}]: expected string")
        if "enum" in spec and v not in spec["enum"]:
            raise _ParamsError(f"{k}: must be one of {spec['enum']}")
        if spec.get("type") == "string" and "maxLength" in spec and len(v) > spec["maxLength"]:
            raise _ParamsError(f"{k}: longer than {spec['maxLength']} characters")


# ---------------------------------------------------------------------------
# Server state: re-checked on every call so `.hearmemory` being deleted mid-session
# is noticed immediately, never re-created by this process.
# ---------------------------------------------------------------------------
class _Server:
    def __init__(self, root: Any, host: Optional[str]):
        self.root = root
        self.host = host or "cli"
        self.session_id = commands.mcp_session_id(os.environ)

    def _ready(self):
        """Returns (store, cfg) or raises `_NotInitialised`. Never writes anything, never creates `.hearmemory`."""
        try:
            open_store = commands._entry("open_store")
            store = open_store(self.root, create=False)
        except Exception:
            store = None
        if store is None:
            raise _NotInitialised()
        is_init = getattr(store, "is_initialised", None)
        if callable(is_init) and not is_init():
            raise _NotInitialised()
        try:
            load_config = commands._entry("load_config")
            cfg = load_config(self.root)
        except Exception:
            cfg = {}
        return store, cfg

    # -- tools -------------------------------------------------------------
    def tool_recall(self, args: Mapping[str, Any]) -> Dict[str, Any]:
        store, cfg = self._ready()
        deadline_s = float((cfg.get("mcp") or {}).get("rebuild_budget_s", 3.0))
        out = commands.do_recall(self.root, store, cfg, query=args.get("query", ""),
                                  limit=int(args.get("limit", 8)), include_archive=bool(args.get("include_archive",
                                                                                                   False)),
                                  brief=bool(args.get("brief", False)), paths=args.get("paths"), host=self.host,
                                  session_id=self.session_id, deadline_s=deadline_s, source="mcp")
        text = getattr(out, "text", "") or render_cli.render_recall(out)
        return {"content": [{"type": "text", "text": text}], "structuredContent": out.to_dict()}

    def tool_record(self, args: Mapping[str, Any]) -> Dict[str, Any]:
        store, cfg = self._ready()
        result = commands.do_record(self.root, store, cfg, text=args["text"], kind=args.get("kind", "note"),
                                     paths=args.get("paths"), refs=args.get("refs"), host=self.host,
                                     session_id=self.session_id, source="mcp", agent_label=args.get("agent_label"))
        return {"content": [{"type": "text", "text": RECORD_ECHO_PREFIX + result["obs_id"]}],
                "structuredContent": result}

    def tool_check(self, args: Mapping[str, Any]) -> Dict[str, Any]:
        store, cfg = self._ready()
        deadline_s = float((cfg.get("mcp") or {}).get("rebuild_budget_s", 3.0))
        result = commands.do_check(self.root, store, cfg, text=args.get("text"), staged=bool(args.get("staged",
                                                                                                        False)),
                                    paths=args.get("paths"), mode="warn", host=self.host,
                                    session_id=self.session_id, action=args.get("action"), deadline_s=deadline_s)
        text = result.text or render_cli.render_check(result)
        return {"content": [{"type": "text", "text": text}], "structuredContent": result.to_dict()}

    def tool_issues(self, args: Mapping[str, Any]) -> Dict[str, Any]:
        store, cfg = self._ready()
        action = args.get("action", "list")
        out = commands.do_issues(self.root, store, cfg, action=action, issue_id=args.get("id"),
                                  title=args.get("title"), reason=args.get("reason"),
                                  show_all=(args.get("status") == "all"), host=self.host,
                                  session_id=self.session_id)
        if action == "list":
            text = render_cli.render_issue_list(out["issues"])
            structured = {"issues": [i.to_dict() for i in out["issues"]]}
        elif action == "show":
            text = render_cli.render_issue(out["issue"])
            structured = {"issue": out["issue"].to_dict() if out["issue"] is not None else None}
        elif action == "open":
            text = RECORD_ECHO_PREFIX + out["obs_id"]
            structured = out
        else:
            text = f"hearmemory: issue {out.get('issue_id')} -> {action}"
            structured = out
        return {"content": [{"type": "text", "text": text}], "structuredContent": structured}

    def tool_status(self, args: Mapping[str, Any]) -> Dict[str, Any]:
        store, cfg = self._ready()
        info = commands.do_status(self.root, store, cfg, verbose=False)
        return {"content": [{"type": "text", "text": render_cli.render_status(info)}], "structuredContent": info}


_TOOL_METHODS = {
    "hearmemory_recall": _Server.tool_recall,
    "hearmemory_record": _Server.tool_record,
    "hearmemory_check": _Server.tool_check,
    "hearmemory_issues": _Server.tool_issues,
    "hearmemory_status": _Server.tool_status,
}


# ---------------------------------------------------------------------------
# JSON-RPC dispatch
# ---------------------------------------------------------------------------
def _initialize(server: _Server, params: Mapping[str, Any]) -> Dict[str, Any]:
    requested = params.get("protocolVersion") if isinstance(params, Mapping) else None
    version = requested if requested in MCP_PROTOCOL_VERSIONS else MCP_PROTOCOL_VERSIONS[0]
    return {
        "protocolVersion": version,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": "hearmemory", "version": _SERVER_VERSION},
        "instructions": _INSTRUCTIONS,
    }


def _ping(server: _Server, params: Mapping[str, Any]) -> Dict[str, Any]:
    return {}


def _tools_list(server: _Server, params: Mapping[str, Any]) -> Dict[str, Any]:
    return {"tools": TOOLS}


def _tools_call(server: _Server, params: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(params, Mapping):
        raise _ParamsError("params must be an object")
    name = params.get("name")
    if not isinstance(name, str) or name not in _TOOL_NAMES:
        raise _ParamsError(f"unknown tool: {name!r}")
    arguments = params.get("arguments", {})
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, Mapping):
        raise _ParamsError("arguments must be an object")
    _validate(name, arguments)
    try:
        return _TOOL_METHODS[name](server, arguments)
    except _ToolError as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    except Exception as e:  # never let a tool crash the server
        return {"content": [{"type": "text", "text": f"hearmemory: internal error running {name}: {e}"}],
                "isError": True}


_METHODS = {
    "initialize": _initialize,
    "ping": _ping,
    "tools/list": _tools_list,
    "tools/call": _tools_call,
}
_NOTIFICATIONS = {"notifications/initialized"}   # acknowledged silently: no response is ever sent


def _error_obj(msg_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _handle(server: _Server, msg: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(msg, dict):
        return _error_obj(None, -32600, "invalid request: expected a JSON object")
    has_id = "id" in msg
    msg_id = msg.get("id")
    method = msg.get("method")
    params = msg.get("params", {})
    if not isinstance(method, str):
        return _error_obj(msg_id, -32600, "invalid request: missing method") if has_id else None
    if method in _NOTIFICATIONS:
        return None
    handler = _METHODS.get(method)
    if handler is None:
        return _error_obj(msg_id, -32601, f"method not found: {method}") if has_id else None
    try:
        result = handler(server, params if isinstance(params, Mapping) else {})
    except _RpcError as e:
        return _error_obj(msg_id, e.code, e.message) if has_id else None
    except Exception as e:  # a bug in a METHOD handler still must not kill the server
        return _error_obj(msg_id, -32603, f"internal error: {e}") if has_id else None
    if not has_id:
        return None
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def serve(root: Any, host: Optional[str] = None, stdin: Optional[TextIO] = None,
          stdout: Optional[TextIO] = None) -> int:
    """ENTRY_POINTS `mcp_serve`. Reads one JSON-RPC message per line from `stdin`
    until EOF, writes one JSON-RPC message per line to `stdout`. Always returns 0;
    no exception ever propagates."""
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    server = _Server(root, host)
    for raw_line in stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            _write(stdout, _error_obj(None, -32700, "parse error: invalid JSON"))
            continue
        try:
            response = _handle(server, msg)
        except Exception as e:  # absolute backstop: never exit the loop
            response = _error_obj(msg.get("id") if isinstance(msg, dict) else None, -32603,
                                   f"internal error: {e}")
        if response is not None:
            _write(stdout, response)
    return 0


def _write(stdout: TextIO, obj: Mapping[str, Any]) -> None:
    stdout.write(canonical_json(obj) + "\n")
    stdout.flush()
