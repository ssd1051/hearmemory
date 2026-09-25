"""hearmemory.observe -- build a redacted, truncated Observation.

make_observation() is the single place every host adapter, the CLI and the MCP
server go through to turn "something an agent did" into an Observation: it
decides exclusion, redacts, truncates, hashes and assigns the id. Hosts only
have to map their own event payloads into its arguments.
"""
from __future__ import annotations

import dataclasses
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .interfaces import OBS_SCHEMA, Observation, Provenance, RunnerSummary, ToolInfo, obs_id_for, sha256_text
from .privacy import command_touches_excluded, is_env_dump_command, is_excluded, redact
from .provenance import dirty_state as _dirty_state
from .testcmd import (TARGET_META_KEY, TARGET_META_V, TARGET_META_V_KEY, canonical_target, is_test_command,
                      placed_target, project_relpath)
from .textutil import normalize_ts, now_ts

_HEAD_CHARS = 2500
_TAIL_CHARS = 1500
# redaction is regex work over the whole text (about 1 s/MB on a loaded machine, holding the
# GIL so no thread timeout can interrupt it). Only the kept head/tail is ever stored, so the text is
# first cut to that head + tail plus this much context on each side -- every single-line secret
# pattern that overlaps the kept part then still lies wholly inside what is redacted -- and only
# then redacted. A multi-line PEM private key cut by the pre-cut is removed by _PEM_* below.
PRECUT_MARGIN_CHARS = 2048
_PRECUT_SEP = "\n…[hearmemory: pre-cut]…\n"
_PEM_OPEN_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----(?![\s\S]*-----END [A-Z0-9 ]*PRIVATE KEY-----)[\s\S]*\Z")
_PEM_CLOSE_RE = re.compile(r"\A(?:(?!-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----)[\s\S])*?-----END [A-Z0-9 ]*PRIVATE KEY-----")


def _kept_head(max_chars: int) -> int:
    return _HEAD_CHARS if max_chars >= _HEAD_CHARS + _TAIL_CHARS else max_chars


def _precut(text: str, max_chars: int) -> "tuple[str, int, int]":
    """-> (text to redact, chars dropped before redaction, partial-PEM redactions). No-op for any
    text _truncate would keep whole or that is barely longer than what it keeps."""
    head_n = _kept_head(max_chars) + PRECUT_MARGIN_CHARS
    tail_n = _TAIL_CHARS + PRECUT_MARGIN_CHARS
    if len(text) <= max(max_chars, head_n + tail_n + len(_PRECUT_SEP)):
        return text, 0, 0
    head, tail = text[:head_n], text[-tail_n:]
    n = 0
    if "PRIVATE KEY-----" in head:
        head, k = _PEM_OPEN_RE.subn("[REDACTED:private_key]", head)
        n += k
    if "PRIVATE KEY-----" in tail:
        tail, k = _PEM_CLOSE_RE.subn("[REDACTED:private_key]", tail)
        n += k
    return head + _PRECUT_SEP + tail, len(text) - head_n - tail_n, n


def _truncate(text: str, max_chars: int, dropped: int = 0) -> "tuple[str, bool]":
    if text is None or (len(text) <= max_chars and not dropped):
        return text or "", False
    if dropped and len(text) <= max_chars:  # cannot happen with the margins above; be safe
        return text, True
    head = text[:_HEAD_CHARS] if max_chars >= _HEAD_CHARS + _TAIL_CHARS else text[:max_chars]
    if max_chars >= _HEAD_CHARS + _TAIL_CHARS:
        tail = text[-_TAIL_CHARS:]
        omitted = len(text) - len(head) - len(tail) + dropped
        if dropped:
            omitted -= len(_PRECUT_SEP)
        return f"{head}…[hearmemory: {omitted} chars truncated]…{tail}", True
    return head, True


# free-text meta fields hosts copy from agent payloads (the Task prompt excerpt, a fetched URL, ...)
_REDACT_META_KEYS = ("prompt_excerpt", "url", "path", "description")


def _finish_tool(root: Any, cfg: Mapping[str, Any], tool: ToolInfo, provenance: Optional[Provenance],
                 meta: Dict[str, Any], is_git_output: bool) -> "tuple[ToolInfo, Dict[str, Any], int]":
    """ToolInfo.command is stored too, so it is redacted like the text (it used to be written
    verbatim, e.g. `--token hf_...`). A test command's target is canonicalised HERE, with the
    project root and the command's cwd, so `cd <project> && pytest x`, `pytest <abs path>/x` and
    `pytest x` share one key (meta.test_target when there is no runner summary)."""
    n = 0
    command = tool.command
    if command:
        command, n = redact(command, cfg=cfg, git_output=is_git_output)
    test = tool.test
    cwd = getattr(provenance, "cwd", None) if provenance is not None else None
    root_s = str(root) if root is not None else None
    try:
        # the target is decided HERE, once, with the root and cwd (and marked as such): a
        # runner asked only for information, or run in an unknown directory, gets "" (no run index).
        target = placed_target(command, root_s, cwd) if command else None
        if target is not None:
            meta = dict(meta)
            meta[TARGET_META_KEY] = target
            meta[TARGET_META_V_KEY] = TARGET_META_V
            if test is not None:
                test = dataclasses.replace(test, target=target or (redact(test.target, cfg=cfg)[0]
                                                                  if test.target else test.target))
        elif test is not None and test.target:
            test = dataclasses.replace(test, target=redact(test.target, cfg=cfg)[0])
    except Exception:
        pass
    paths = list(tool.paths or [])
    if root_s and paths:
        # an absolute path inside the project -- or inside a linked git worktree of it
        # (<root>/.claude/worktrees/<name>/src/x.py) -- is stored project-relative (src/x.py)
        try:
            paths = [(project_relpath(p, root_s) or p) if isinstance(p, str) and p.startswith("/") else p
                     for p in paths]
        except Exception:
            paths = list(tool.paths or [])
    if command != tool.command or test is not tool.test or paths != list(tool.paths or []):
        tool = dataclasses.replace(tool, command=command, test=test, paths=paths)
    return tool, meta, n


def _capture_cfg(cfg: Mapping[str, Any]) -> Dict[str, Any]:
    return dict((cfg or {}).get("capture") or {})


def make_observation(root: Any, cfg: Mapping[str, Any], kind: str, text: str, provenance: Provenance,
                     *, tool: Optional[ToolInfo] = None, event_key: Optional[str] = None,
                     refs: Sequence[str] = (), meta: Optional[Mapping[str, Any]] = None,
                     is_command_kind: Optional[bool] = None, event_ts: Optional[str] = None,
                     historical: bool = False) -> Observation:
    """`event_ts`: when the event happened, if that is not "now" -- e.g. the timestamp of
    an imported Codex rollout line. It becomes Observation.ts (ordering, "N min ago", freshness)
    and the import time is kept in meta.recorded_ts. `historical=True` marks an event from the
    past: the CURRENT worktree's dirty_state says nothing about it, so none is recorded."""
    capture_cfg = _capture_cfg(cfg)
    meta_out: Dict[str, Any] = dict(meta or {})
    text = text or ""
    recorded_ts = now_ts()
    obs_ts = normalize_ts(event_ts) if event_ts else None
    if obs_ts is not None and obs_ts != recorded_ts:
        meta_out.setdefault("recorded_ts", recorded_ts)
    obs_ts = obs_ts or recorded_ts

    excluded = False
    withheld_reason: Optional[str] = None

    paths = list(tool.paths) if tool else list(meta_out.get("paths") or [])
    for p in paths:
        if is_excluded(p, cfg):
            excluded = True
            withheld_reason = "path"
            break

    command = tool.command if tool else None
    if not excluded and command:
        if command_touches_excluded(command, cfg):
            excluded = True
            withheld_reason = "path"
        elif capture_cfg.get("withhold_env_dumps", True) and is_env_dump_command(command):
            excluded = True
            withheld_reason = "env_dump"

    is_git_output = bool(command and re.match(r"^\s*git\b", command))
    redactions = 0
    max_chars = int(capture_cfg.get("max_text_chars", 4000))
    dropped = 0
    if excluded:
        meta_out["withheld"] = withheld_reason
        stored_text = "[hearmemory: content withheld by privacy rules]"
    else:
        cut_text, dropped, pem_cut = _precut(text, max_chars)  # cut BEFORE the regex work
        if dropped:
            meta_out["original_chars"] = len(text)
        stored_text, redactions = redact(cut_text, cfg=cfg, git_output=is_git_output)
        redactions += pem_cut

    # For pre-cut text this hashes the redacted head+tail (never the raw, unredacted text).
    text_sha256 = sha256_text(stored_text)
    truncated_text, truncated = _truncate(stored_text, max_chars, dropped)

    if event_key is None:
        event_key = f"local:{sha256_text((command or text or '') + now_ts())}"

    if tool is not None:
        tool, meta_out, k = _finish_tool(root, cfg, tool, provenance, meta_out, is_git_output)
        redactions += k
    for key in _REDACT_META_KEYS:
        v = meta_out.get(key)
        if isinstance(v, str) and v:
            meta_out[key], k = redact(v, cfg=cfg)
            redactions += k

    if (kind == "command" and tool is not None and not excluded and not historical
            and capture_cfg.get("record_dirty_state", True)):
        try:
            dstate, dtrunc = _dirty_state(root)
            if dstate is not None:
                meta_out["dirty_state"] = dstate
                if dtrunc:
                    meta_out["dirty_state_truncated"] = True
        except Exception:
            pass

    obs_id = obs_id_for(event_key)
    return Observation(id=obs_id, ts=obs_ts, kind=kind, event_key=event_key, provenance=provenance,
                        text=truncated_text, tool=tool, text_sha256=text_sha256, truncated=truncated,
                        redactions=redactions, excluded=excluded, refs=list(refs), meta=meta_out,
                        schema=OBS_SCHEMA)


# ---------------------------------------------------------------------------
# Test-result recognition
# ---------------------------------------------------------------------------
_PYTEST_SUMMARY_RE = re.compile(
    r"={3,}.*?(?P<counts>(?:\d+ \w+(?:, )?)+)\s*(?:in [\d.]+s)?\s*={3,}")
_PYTEST_COUNT_RE = re.compile(r"(\d+) (passed|failed|error(?:s)?|skipped)")
_PYTEST_FAILED_LINE_RE = re.compile(r"^FAILED (\S+)", re.M)
_UNITTEST_RAN_RE = re.compile(r"Ran (\d+) tests?")
_UNITTEST_FAILURES_RE = re.compile(r"FAILED \(([^)]*)\)")
_JEST_RE = re.compile(r"Tests:\s*(?:(\d+) failed, )?(?:(\d+) skipped, )?(\d+) passed")
_GO_FAIL_LINE_RE = re.compile(r"^--- FAIL: (\S+)", re.M)
_GO_RESULT_RE = re.compile(r"^(ok|FAIL)\s+(\S+)", re.M)
_CARGO_RE = re.compile(r"test result: (ok|FAILED)\. (\d+) passed; (\d+) failed")

SUMMARY_SCAN_CHARS = 32 * 1024

_ARG_NOISE_RE = re.compile(r"\s-[a-zA-Z]{1,3}\b(?!\S)")


def _normalize_target(command: str) -> str:
    """RunnerSummary.target: the shared canonical test target (hearmemory.testcmd)."""
    if not command:
        return command
    return canonical_target(command)


def parse_test_summary(command: Optional[str], output: str) -> Optional[RunnerSummary]:
    """Best-effort test-runner summary recognition (pytest/unittest/jest/go/cargo)."""
    if not output:
        return None
    if len(output) > 2 * SUMMARY_SCAN_CHARS:
        # runner summaries live at the start/end of the output; scanning a multi-MB (or
        # 60 MB single-line) output with these regexes can take seconds inside a hook.
        output = output[:SUMMARY_SCAN_CHARS] + "\n" + output[-SUMMARY_SCAN_CHARS:]
    target = _normalize_target(command) if command else None

    # Runners with a distinctive marker are checked first so a stray "N passed" in their output
    # (jest, cargo) can never be mistaken for a pytest summary.
    m = _UNITTEST_RAN_RE.search(output)
    if m and ("OK" in output or "FAILED (" in output):
        ran = int(m.group(1))
        fail_m = _UNITTEST_FAILURES_RE.search(output)
        failures = errors = 0
        if fail_m:
            for part in fail_m.group(1).split(","):
                part = part.strip()
                if "=" in part:
                    k, v = part.split("=", 1)
                    if k.strip() == "failures":
                        failures = int(v)
                    elif k.strip() == "errors":
                        errors = int(v)
        passed = ran - failures - errors
        return RunnerSummary(runner="unittest", passed=max(0, passed), failed=failures, errors=errors,
                             target=target)

    m = _JEST_RE.search(output)
    if m:
        failed = int(m.group(1) or 0)
        passed = int(m.group(3) or 0)
        return RunnerSummary(runner="jest", passed=passed, failed=failed, target=target)

    go_fails = _GO_FAIL_LINE_RE.findall(output)
    go_result = _GO_RESULT_RE.findall(output)
    if go_fails or go_result:
        failed = len(go_fails)
        passed = sum(1 for status, _ in go_result if status == "ok")
        return RunnerSummary(runner="go", passed=passed, failed=failed, failed_ids=go_fails[:20],
                             target=target)

    m = _CARGO_RE.search(output)
    if m:
        passed = int(m.group(2))
        failed = int(m.group(3))
        return RunnerSummary(runner="cargo", passed=passed, failed=failed, target=target)

    # pytest last, and only with a pytest-specific signal (command name, node ids, or the
    # "===== ... in Xs =====" summary banner) -- a bare "N passed" is not enough on its own.
    looks_like_pytest = ("pytest" in (command or "") or "::" in output
                        or _PYTEST_SUMMARY_RE.search(output) is not None)
    m = _PYTEST_COUNT_RE.findall(output)
    if m and looks_like_pytest:
        counts = {k: 0 for k in ("passed", "failed", "error", "errors", "skipped")}
        for n, k in m:
            counts[k] = counts.get(k, 0) + int(n)
        failed_ids = _PYTEST_FAILED_LINE_RE.findall(output)[:20]
        return RunnerSummary(runner="pytest", passed=counts.get("passed", 0),
                             failed=counts.get("failed", 0),
                             errors=counts.get("error", 0) + counts.get("errors", 0),
                             skipped=counts.get("skipped", 0), failed_ids=failed_ids, target=target)

    return None
