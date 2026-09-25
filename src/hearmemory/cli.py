"""hearmemory.cli -- argparse skeleton and dispatch (core provides the skeleton;
CLI/MCP fills in `hearmemory.commands.COMMANDS` and `hearmemory.host.hooks.run_hook`).

Root resolution: --project > $HEARMEMORY_PROJECT > walking up from cwd for an
initialised .hearmemory/ > walking up from cwd for a git root. Every command except
`init`/`hook` fails with EXIT_NOT_INITIALISED when no store is found.
`hook` never raises past this module: it always goes through safety.run_guarded.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .config import load_config
from .interfaces import CLI_COMMANDS, EXIT_NOT_INITIALISED, EXIT_OK, EXIT_USAGE, HOOK_EVENTS, HookResult
from .store import Store, open_store

PROG = "hearmemory"


def _find_git_root(start: Path) -> Optional[Path]:
    cur = start
    while True:
        if (cur / ".git").exists():
            return cur
        if cur.parent == cur:
            return None
        cur = cur.parent


def resolve_root(explicit: Optional[str]) -> Path:
    if explicit:
        return Path(explicit).resolve()
    env = os.environ.get("HEARMEMORY_PROJECT")
    if env:
        return Path(env).resolve()
    start = Path.cwd()
    store = open_store(start, create=False)
    if store is not None:
        return store.root
    git_root = _find_git_root(start)
    if git_root is not None:
        return git_root
    return start


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG, description="Shared project memory for coding agents.")
    parser.add_argument("--project", default=None, help="project root (default: autodetect)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("-q", "--quiet", action="store_true", help="suppress non-essential output")
    sub = parser.add_subparsers(dest="command")

    p_init = sub.add_parser("init")
    p_init.add_argument("--hosts", default=None, help="comma-separated: claude,codex,cursor,git,all")
    p_init.add_argument("--no-git-hook", action="store_true")
    p_init.add_argument("--claude-persist", action="store_true")
    p_init.add_argument("--force-hooks-path", action="store_true")
    p_init.add_argument("--python", default=None)
    p_init.add_argument("--force", action="store_true")

    p_status = sub.add_parser("status")
    p_status.add_argument("--verbose", action="store_true")

    p_record = sub.add_parser("record")
    p_record.add_argument("text")
    p_record.add_argument("--kind", default="note", choices=["note", "claim", "issue"])
    p_record.add_argument("--paths", nargs="*", default=[])
    p_record.add_argument("--refs", nargs="*", default=[])
    p_record.add_argument("--session", default=None)
    p_record.add_argument("--agent-label", default=None)
    p_record.add_argument("--key", default=None)

    p_recall = sub.add_parser("recall")
    p_recall.add_argument("query", nargs="?", default="")
    p_recall.add_argument("--brief", action="store_true")
    p_recall.add_argument("--limit", type=int, default=8)
    p_recall.add_argument("--include-archive", action="store_true")
    p_recall.add_argument("--paths", nargs="*", default=[])
    p_recall.add_argument("--session", default=None)
    p_recall.add_argument("--wait", type=float, default=None)
    p_recall.add_argument("--restore", default=None)

    p_check = sub.add_parser("check")
    p_check.add_argument("--staged", action="store_true")
    p_check.add_argument("--text", default=None)
    p_check.add_argument("--message", default=None)
    p_check.add_argument("--paths", nargs="*", default=[])
    p_check.add_argument("--mode", default=None)
    p_check.add_argument("--session", default=None)
    p_check.add_argument("--wait", type=float, default=None)
    p_check.add_argument("--ack", default=None)

    p_issues = sub.add_parser("issues")
    p_issues.add_argument("action", nargs="?", default="list", choices=["list", "show", "close", "reopen", "open"])
    p_issues.add_argument("target", nargs="?", default=None)
    p_issues.add_argument("--all", action="store_true")
    p_issues.add_argument("--reason", default=None)
    p_issues.add_argument("--paths", nargs="*", default=[])

    p_import = sub.add_parser("import")
    p_import.add_argument("source", nargs="?", default="codex", choices=["codex"])
    # by default only sessions after `hearmemory init` are imported; --since / --all-history backfill
    p_import.add_argument("--since", default=None)
    p_import.add_argument("--all-history", action="store_true")
    p_import.add_argument("--session", default=None)
    p_import.add_argument("--codex-home", default=None)
    p_import.add_argument("--dry-run", action="store_true")
    # launch.sh passes its HEARMEMORY_SESSION_ID after Codex exits, so the MCP-recorded records of
    # that launch are reconciled with the Codex rollout session (one Codex session = one actor).
    p_import.add_argument("--launch-session", default=None)

    p_worker = sub.add_parser("worker")
    group = p_worker.add_mutually_exclusive_group()
    group.add_argument("--once", action="store_true")
    group.add_argument("--daemon", action="store_true")
    group.add_argument("--spawn", action="store_true")
    group.add_argument("--stop", action="store_true")
    p_worker.add_argument("--timeout", type=float, default=None)
    p_worker.add_argument("--no-jev", action="store_true")
    p_worker.add_argument("--wait-lock", type=float, default=None)
    p_worker.add_argument("--launched-by", default=None)
    p_worker.add_argument("--retry-failed", action="store_true")
    p_worker.add_argument("--status", action="store_true")

    p_doctor = sub.add_parser("doctor")
    p_doctor.add_argument("--repair", action="store_true")

    p_uninstall = sub.add_parser("uninstall")
    p_uninstall.add_argument("--purge", action="store_true")
    p_uninstall.add_argument("--yes", action="store_true")

    p_rebuild = sub.add_parser("rebuild")
    p_rebuild.add_argument("--reextract", action="store_true")

    p_mcp = sub.add_parser("mcp")
    p_mcp.add_argument("--host", default=None, choices=["claude", "codex", "cursor"])

    p_hook = sub.add_parser("hook")
    p_hook.add_argument("host", choices=sorted(HOOK_EVENTS.keys()))
    p_hook.add_argument("event")

    p_host = sub.add_parser("host")
    # dest must be `host_action` (what commands.cmd_host reads); metavar keeps the usage text.
    p_host.add_argument("host_action", metavar="action", choices=["claude-cmd", "codex-cmd", "cursor-status"])
    p_host.add_argument("--hooks", action="store_true")

    assert set(sub.choices.keys()) == set(CLI_COMMANDS), (
        f"cli.py subcommands out of sync with interfaces.CLI_COMMANDS: "
        f"{set(sub.choices.keys()) ^ set(CLI_COMMANDS)}")
    return parser


def _print(args: argparse.Namespace, obj: Any, text: Optional[str] = None) -> None:
    if getattr(args, "json", False):
        if hasattr(obj, "to_dict"):
            obj = obj.to_dict()
        print(json.dumps(obj, ensure_ascii=False, sort_keys=True))
    elif text is not None:
        print(text)
    elif not getattr(args, "quiet", False):
        print(obj)


def _dispatch_hook(args: argparse.Namespace, root: Path) -> int:
    from . import safety
    try:
        from .host.hooks import run_hook
    except ImportError:
        def run_hook(host, event, stdin_bytes, root=None):  # type: ignore[misc]
            return HookResult(exit_code=0, stdout="", stderr="hearmemory: host adapters not installed yet")

    payload = sys.stdin.buffer.read() if not sys.stdin.isatty() else b"{}"

    def _run() -> HookResult:
        return run_hook(args.host, args.event, payload, root=root)

    result = safety.run_guarded(_run, total_ms=5000)
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr.rstrip("\n") + "\n")
    return result.exit_code


def _commands_module():
    try:
        from . import commands
        return commands
    except ImportError:
        return None


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return EXIT_USAGE

    root = resolve_root(args.project)

    if args.command == "hook":
        return _dispatch_hook(args, root)

    if args.command == "init":
        commands = _commands_module()
        if commands is None or "init" not in getattr(commands, "COMMANDS", {}):
            print(f"hearmemory: 'init' not implemented in this build", file=sys.stderr)
            return EXIT_USAGE
        store = open_store(root, create=True)
        ctx = {"root": root, "store": store, "config": None, "json": args.json, "quiet": args.quiet}
        try:
            return commands.COMMANDS["init"](args, ctx)
        except Exception as exc:  # noqa: BLE001
            print(f"hearmemory: init failed: {exc}", file=sys.stderr)
            return EXIT_USAGE

    store = open_store(root, create=False)
    if store is None:
        print(f"hearmemory: not initialised (run `hearmemory init`) in {root}", file=sys.stderr)
        return EXIT_NOT_INITIALISED

    config = load_config(store.root)
    ctx = {"root": store.root, "store": store, "config": config, "json": args.json, "quiet": args.quiet}

    commands = _commands_module()
    handler = None
    if commands is not None:
        handler = getattr(commands, "COMMANDS", {}).get(args.command)
    if handler is None:
        print(f"hearmemory: '{args.command}' is not implemented in this build yet", file=sys.stderr)
        return EXIT_USAGE
    try:
        return handler(args, ctx)
    except Exception as exc:  # noqa: BLE001
        print(f"hearmemory: {args.command} failed: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
