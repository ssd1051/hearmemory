"""plain-text rendering for the CLI's non-`--json` output.

memory already renders `RecallResult.text`, `Brief.text` and `CheckResult.text`
for the cases it owns; the functions here are the CLI/MCP layer's own formatting for
everything memory does not render (status, issues, doctor, worker, host) and are
the fallback for recall/check when the memory-rendered text is empty (e.g. a
fake/partial store in tests, or a genuinely empty result).

Every function here is a pure `data -> str` mapping so it can be unit-tested
without a project, a store or any other module.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional, Sequence


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def worker_line(worker: Optional[Mapping[str, Any]]) -> str:
    """Honest worker line. `worker` is the judge's worker_liveness() dict ("state" key); a bare
    worker.json dict (no "state") is only trusted when it names a pid and has no exit mark."""
    if not worker:
        return "not running"
    state = worker.get("state")
    if state is None:
        pid = int(worker.get("pid") or 0)
        if pid and not worker.get("exited_ts"):
            return f"unverified (worker.json names pid {pid})"
        return "not running"
    pid = worker.get("pid")
    if state == "running":
        jev = worker.get("jev_capable")
        extra = f", mode {worker.get('mode')}" if worker.get("mode") else ""
        extra += ", with Jev" if jev else (", without Jev" if jev is False else "")
        return f"running (pid {pid}{extra})"
    if state == "starting":
        return f"starting (pid {pid})"
    if state == "busy":
        return "lock held by a process that is not a live hearmemory worker yet"
    last = worker.get("last_pid")
    if last:
        when = worker.get("exited_ts") or worker.get("last_beat_ts")
        return f"not running (last worker pid {last}" + (f", last seen {when})" if when else ")")
    return "not running"


def jev_line(jev: Mapping[str, Any]) -> str:
    """the live worker's Jev state when there is one (that is where the judging happens); this
    shell's own check otherwise, labelled as such."""
    if jev.get("source") == "worker":
        shell = jev.get("this_shell") or {}
        bits = [f"worker pid {jev.get('worker_pid')}"]
        if jev.get("model"):
            bits.append(str(jev["model"]))
        if jev.get("capable"):
            if jev.get("calls_today") is not None:
                bits.append(f"{jev.get('calls_today')} calls today")
            out = "available (" + ", ".join(bits) + ")"
        else:
            out = f"unavailable ({jev.get('reason') or 'unknown'}; " + ", ".join(bits) + ")"
        if shell and not shell.get("capable"):
            out += f"; this shell: {shell.get('reason') or 'unavailable'}"
        return out
    if jev.get("capable"):
        return "available (this shell; no live worker)"
    return f"unavailable (this shell: {jev.get('reason', 'unknown')}; no live worker)"


def render_status(info: Mapping[str, Any]) -> str:
    counts = info.get("counts", {}) or {}
    lines = [
        "hearmemory status",
        f"  observations: {counts.get('observations', 0)}",
        f"  claims:       {counts.get('claims', 0)}",
        f"  events:       {counts.get('events', 0)}",
    ]
    cbs = counts.get("candidates_by_status") or {}
    if cbs:
        lines.append("  candidates:   " + ", ".join(f"{k}={v}" for k, v in sorted(cbs.items())))
    jbp = counts.get("judgments_by_provider") or {}
    if jbp:
        lines.append("  judgments:    " + ", ".join(f"{k}={v}" for k, v in sorted(jbp.items())))
    lines.append(f"  open issues:  {info.get('issues_open', 0)}")
    lines.append(f"  jev:          {jev_line(info.get('jev') or {})}")
    budget = info.get("budget") or {}
    if budget:
        lines.append(f"  jev spend today: {budget.get('calls', 0)}/{budget.get('call_cap', '?')} calls, "
                      f"${budget.get('usd', 0):.4f}/${budget.get('usd_cap', '?')}")
    lines.append(f"  worker:       {worker_line(info.get('worker'))}")
    if info.get("memory_as_of"):
        lines.append(f"  memory as of: {info['memory_as_of']}")
    dropped = info.get("extract_dropped")
    if dropped:
        lines.append("  extraction dropped: " + ", ".join(f"{k}={v}" for k, v in sorted(dropped.items())))
    return "\n".join(lines)


def render_recall(result: Any) -> str:
    text = _get(result, "text")
    if text:
        return text
    items = _get(result, "items", []) or []
    if not items:
        return "hearmemory: nothing relevant found."
    lines = [f"hearmemory recall: {len(items)} item(s)"]
    for it in items:
        tags = "".join(_get(it, "tags", []) or [])
        excerpt = (_get(it, "excerpt", "") or "").splitlines()[0][:200] if _get(it, "excerpt", "") else ""
        lines.append(f"  {tags} {_get(it, 'kind', '')} {_get(it, 'obs_id', '')} :: {excerpt}")
    pending = _get(result, "pending_judgments", 0)
    if pending:
        lines.append(f"  ({pending} judgment(s) still pending)")
    return "\n".join(lines)


def render_check(result: Any) -> str:
    text = _get(result, "text")
    if text:
        return text
    decision = _get(result, "decision", "allow")
    warnings = _get(result, "warnings", []) or []
    lines = [f"hearmemory check: {decision}"]
    for w in warnings:
        lines.append(f"  [{_get(w, 'kind', '')}] {_get(w, 'text', '')}")
    if _get(result, "stale", False):
        lines.append(f"  (memory as of {_get(result, 'memory_as_of', '?')}, may be stale)")
    return "\n".join(lines)


def render_issue_list(issues: Sequence[Any]) -> str:
    if not issues:
        return "hearmemory: no open issues."
    lines = [f"hearmemory issues: {len(issues)} open"]
    for i in issues:
        lines.append(f"  [{_get(i, 'issue_id', '')}] ({_get(i, 'status', '')}) {_get(i, 'title', '')}")
    return "\n".join(lines)


def render_issue(issue: Any) -> str:
    if issue is None:
        return "hearmemory: issue not found."
    lines = [
        f"issue {_get(issue, 'issue_id', '')} ({_get(issue, 'status', '')})",
        f"  kind:  {_get(issue, 'kind', '')}",
        f"  title: {_get(issue, 'title', '')}",
    ]
    paths = _get(issue, "paths", []) or []
    if paths:
        lines.append(f"  paths: {', '.join(paths)}")
    suggestion = _get(issue, "suggestion", None)
    if suggestion:
        lines.append(f"  suggestion: {suggestion}")
    return "\n".join(lines)


def render_doctor(report: Mapping[str, Any]) -> str:
    lines = ["hearmemory doctor"]
    if not report.get("initialised", True):
        lines.append("  NOT INITIALISED: run `hearmemory init`.")
        return "\n".join(lines)
    jev = report.get("jev") or {}
    lines.append(f"  jev: {'available' if jev.get('capable') else 'unavailable (' + str(jev.get('reason')) + ')'}")
    lines.append(f"  worker: {worker_line(report.get('worker'))}")
    problems = report.get("problems") or []
    if problems:
        lines.append(f"  {len(problems)} problem(s):")
        for p in problems:
            lines.append(f"    - {p}")
    else:
        lines.append("  no problems found.")
    repairs = report.get("repairs") or []
    for r in repairs:
        lines.append(f"  repaired: {r}")
    return "\n".join(lines)


def render_worker_status(info: Optional[Mapping[str, Any]]) -> str:
    if not info:
        return "hearmemory: worker not running."
    if "state" in info:
        return f"hearmemory: worker {worker_line(info)}"
    return (f"hearmemory: worker pid={info.get('pid')} mode={info.get('mode')} "
            f"jev_capable={info.get('jev_capable')} started={info.get('started_ts')}")


def render_cursor_status(info: Mapping[str, Any]) -> str:
    mcp = "present" if info.get("mcp_json") else "missing"
    hooks = "present" if info.get("hooks_json") else "missing"
    return f"hearmemory: cursor .cursor/mcp.json {mcp}, .cursor/hooks.json {hooks}"
