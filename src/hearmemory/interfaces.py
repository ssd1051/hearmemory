"""hearmemory shared interfaces: the data model and contracts every module codes against.

Every module codes against the names in
this module. It holds ONLY:
  * version strings and closed vocabularies (tuples of allowed values),
  * frozen-ish dataclasses for every record that crosses a module boundary or
    is written to disk under .hearmemory/, each with to_dict()/from_dict(),
  * typing.Protocols for the cross-module services,
  * a few pure id / hash / relevance / actor / scope helpers that must be identical everywhere
    (including the small ActorMap resolver).

Importing it has no side effects: no file, network, env or clock access, and
no dependency outside the standard library. Changing a name or a field here is
an interface change: it is made in one place and checked by the integration tests.
See docs/DESIGN.md for the architecture.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from typing import (Any, Dict, Iterable, Iterator, List, Mapping, Optional, Protocol, Sequence, Tuple,
                    runtime_checkable)

# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------
INTERFACES_VERSION = "hearmemory-interfaces-1.1"
STORE_FORMAT = "hearmemory-store/1"             # written to .hearmemory/VERSION
OBS_SCHEMA = "hearmemory.obs/1"
CANDIDATE_SCHEMA = "hearmemory.cand/1"
JUDGMENT_SCHEMA = "hearmemory.judg/1"
EVENT_SCHEMA = "hearmemory.event/1"
LEDGER_SCHEMA = "hearmemory.ledger/1"
STATE_SCHEMA = "hearmemory.state/1"
MANIFEST_SCHEMA = "hearmemory.manifest/1"
EXTRACTOR_VERSION = "gx-1.0"               # generic extractor v1
JEV_MODEL_DEFAULT = "jev-1.13.0"
JEV_BASE_URL_DEFAULT = "https://api.typesafe.ai"
JEV_API_KEY_ENV = "TYPESAFE_API_KEY"       # the ONLY place the key is read from; never stored/logged
JEV_USD_PER_MILLION_INPUT = 0.042
HEARMEMORY_DIRNAME = ".hearmemory"
QUESTION_KEY = "decision"                  # Jev question key: never template-revealing (templates v1 global rule)

# ---------------------------------------------------------------------------
# Vocabularies
# ---------------------------------------------------------------------------
HOSTS: Tuple[str, ...] = ("claude", "codex", "cursor", "git", "cli", "mcp")
INSTALLABLE_HOSTS: Tuple[str, ...] = ("claude", "codex", "cursor", "git")

# Observation kinds. PRIMARY = tool output the agent did not author (evidence).
# ASSERTIVE = text an agent (or human) wrote (claims come from these).
OBS_KINDS: Tuple[str, ...] = (
    "command",            # shell command + (redacted, truncated) output; tests are commands with tool.test
    "file_edit",          # write/edit/patch of a project file; text = compact diff or new-content excerpt
    "file_read",          # read of a project file; path always, text only if capture.store_file_reads
    "search",             # grep/glob/search tool; text = truncated results
    "assistant_message",  # an agent's own conclusion / report text (final or intermediate)
    "subagent_result",    # a subagent's final report as returned to its parent
    "note",               # manual `hearmemory record` / hearmemory_record kind=note
    "claim",              # explicit claim recorded by an agent or a human (skips claim heuristics)
    "user_prompt",        # the human task text (truncated; context for relevance only, never evidence)
    "session_event",      # session start/stop/subagent start/stop, import markers, provenance links
)
PRIMARY_OBS_KINDS: Tuple[str, ...] = ("command", "file_edit", "file_read", "search")
ASSERTIVE_OBS_KINDS: Tuple[str, ...] = ("assistant_message", "subagent_result", "note", "claim")
NON_EVIDENCE_OBS_KINDS: Tuple[str, ...] = ("user_prompt", "session_event")
# "running": the command had not finished when it was recorded (Codex "Process running with
# session ID", Claude run_in_background) -- its outcome is unknown, never a pass.
TOOL_STATUSES: Tuple[str, ...] = ("ok", "error", "unknown", "running")

# Typed mentions. Only these kinds may become A1 endpoints (the A1 fix).
MENTION_KINDS: Tuple[str, ...] = ("file", "module", "symbol", "test", "service", "config_key")
# A1 may pair two mentions only if their kinds are in the same compatibility class.
MENTION_COMPAT: Mapping[str, str] = {"file": "path", "module": "path", "test": "test", "symbol": "symbol",
                                     "service": "service", "config_key": "config_key"}
CLAIM_CLASSES: Tuple[str, ...] = ("conclusion", "status", "premise", "other")

# The four judgment templates. B3 is folded into B1 (premise claims).
TEMPLATE_IDS: Tuple[str, ...] = ("A1", "A2", "A3", "B1")
TEMPLATE_VERSIONS: Mapping[str, str] = {"A1": "hm-A1.1", "A2": "hm-A2.1", "A3": "hm-A3.1", "B1": "hm-B1.1"}
TEMPLATE_LABELS: Mapping[str, Tuple[str, ...]] = {
    "A1": ("same", "different", "unresolved"),
    "A2": ("same_event", "different_events", "unresolved"),
    "A3": ("restates", "generalizes", "partial", "not_contained"),
    "B1": ("supports", "refutes", "both", "insufficient"),
}
# The conservative label a template falls back to when nothing better is known.
TEMPLATE_UNKNOWN_LABEL: Mapping[str, str] = {"A1": "unresolved", "A2": "unresolved", "A3": "not_contained",
                                             "B1": "insufficient"}
PROGRAM_RULE_TEMPLATES: Tuple[str, ...] = ("C1", "C2", "C4")      # program rules, never asked to a model
DEFERRED_TEMPLATES: Tuple[str, ...] = ("C3", "B4", "A4", "B2")

PROVIDERS: Tuple[str, ...] = ("jev", "rule", "cache", "manual")
JUDGMENT_OUTCOMES: Tuple[str, ...] = (
    "valid",              # a semantic label was obtained (from jev / rule / cache / manual)
    "transport_error",    # network / 5xx / timeout; retried later with backoff; writes nothing to memory
    "validation_error",   # malformed response; not retried with the same input
    "permission_denied",  # 401/403 WITH a key present (a missing key never produces a Judgment)
    "budget_blocked",     # daily/session cap reached before issuing the call
    "fallback_detected",  # returned model != pinned model: treated as no judgment
    "disabled",           # this candidate may not be sent (e.g. evidence under privacy.jev_exclude_globs).
                          # Process-local unavailability (JEV_LOCAL_REASONS) writes NO judgment at all.
)
CANDIDATE_STATUSES: Tuple[str, ...] = ("pending", "judged", "rule_judged", "skipped", "failed", "superseded")

# Memory entities
EDGE_RELATIONS: Tuple[str, ...] = ("same_object", "same_event", "restates", "generalizes")
EDGE_STATUSES: Tuple[str, ...] = ("provisional", "verified", "disputed", "retracted")
MARK_KINDS: Tuple[str, ...] = ("distinct_object", "distinct_event", "covered_by", "pending_alignment",
                               "pending_event_alignment")
CLAIM_STATUSES: Tuple[str, ...] = ("unjudged", "supported", "same_source_only", "refuted", "disputed",
                                   "insufficient", "outdated", "weak_support")
# "weak_support": Jev said supports, but with confidence below judge.b1_min_support_confidence and no
# program-checked run behind it. Rendered [WEAK SUPPORT]; never counted as supported (brief ranking, check).
# "outdated" (rule B1_test_status_changed) is a PROGRAM status, never a model label: the claim
# described a test/command result and a later run of the same target disagrees AFTER the code changed
# (HEAD moved, worktree state of a watched path changed, or a file_edit touched a watched path). It is NOT a
# refutation: it never triggers relies_on_refuted, never blocks, and is not shown as a current fact.
ISSUE_KINDS: Tuple[str, ...] = ("disputed_claim", "unverified_conclusion", "premise_gap", "manual",
                                "failing_check")
ISSUE_STATUSES: Tuple[str, ...] = ("open", "disputed", "resolved", "closed", "reopened")
OPEN_ISSUE_STATUSES: Tuple[str, ...] = ("open", "disputed", "reopened")
TIERS: Tuple[str, ...] = ("hot", "archive")
MEMORY_OP_KINDS: Tuple[str, ...] = ("edge_add", "edge_status_set", "mark_add", "claim_status_set",
                                    "group_merge", "issue_open", "issue_status_set", "tier_set")
EVENT_KINDS: Tuple[str, ...] = ("issue_close", "issue_reopen", "issue_open", "provenance_link",
                                "judgment_override", "archive_restore", "seen", "session_alias", "memory_shown")
# "session_alias": target = "<host>:<proxy session id>" (e.g. "codex:codex-1790255964-81139", the
# HEARMEMORY_SESSION_ID launch.sh gives the MCP server); provenance = the host-native session it IS (the Codex rollout
# session). Every PROXY record of that proxy session resolves to it, so one Codex session = one actor.
# "memory_shown": hearmemory delivered memory claims to a session (a brief, a recall result). target =
# the session key; data = {"claim_ids": [...], "via": "brief:<purpose>" | "recall"}; provenance = the requesting
# session (source "mcp"/"cli" when it came through hearmemory's own MCP server / CLI). The MemoryBuilder marks a later
# claim of that actor that restates one of those claims as derived (not independent).
# Every line hearmemory renders for an agent (brief, recall, check headers) starts with this marker; together with
# STATUS_TAGS it lets the extractor skip hearmemory output an agent quotes back (memory echo).
HEARMEMORY_OUTPUT_MARKER = "[hearmemory"

# Brief / precommit
BRIEF_TIERS: Tuple[str, ...] = ("P1", "P2", "P3")   # P1 refuted/disputed relevant > P2 open issues > P3 new facts
BRIEF_PURPOSES: Tuple[str, ...] = ("session_start", "subagent_start", "push", "manual")
BRIEF_ITEM_KINDS: Tuple[str, ...] = ("claim_status", "issue", "fact", "alignment")
CHECK_ACTIONS: Tuple[str, ...] = ("git_commit", "claim", "finish")
PRECOMMIT_MODES: Tuple[str, ...] = ("off", "warn", "hold_once", "block")
CHECK_DECISIONS: Tuple[str, ...] = ("allow", "warn", "hold", "block")
WARNING_KINDS: Tuple[str, ...] = ("relies_on_refuted", "relies_on_disputed", "unresolved_issue",
                                  "failing_check", "unseen_relevant_fact")
WARNING_RANK: Mapping[str, int] = {k: i for i, k in enumerate(WARNING_KINDS)}
BLOCKING_WARNING_KINDS: Tuple[str, ...] = ("relies_on_refuted", "failing_check")   # only these may block
LANGS: Tuple[str, ...] = ("en", "zh")
# Rendered status tags. "unjudged" renders only for conclusion claims shown as new facts.
STATUS_TAGS: Mapping[str, Mapping[str, str]] = {
    "en": {"supported": "[SUPPORTED]", "same_source_only": "[SAME-SOURCE ONLY]", "refuted": "[REFUTED]",
           "disputed": "[DISPUTED]", "insufficient": "[INSUFFICIENT]", "unjudged": "[UNVERIFIED]",
           "issue": "[ISSUE]", "archived": "[ARCHIVED]", "outdated": "[OUTDATED]", "weak_support": "[WEAK SUPPORT]",
           "addressed": "[ADDRESSED?]"},
    "zh": {"supported": "[有证据支持]", "same_source_only": "[仅同源转述]", "refuted": "[已被反驳]",
           "disputed": "[有争议]", "insufficient": "[证据不足]", "unjudged": "[待确认]",
           "issue": "[未决问题]", "archived": "[已归档]", "outdated": "[已过时]", "weak_support": "[弱支持]",
           "addressed": "[可能已处理]"},
}

# Relevance
REL_PATH_WEIGHT = 0.6
REL_IDENT_WEIGHT = 0.4
REL_IDENT_SATURATION = 2
BRIEF_FACT_MIN_REL = 0.2
RELIED_MIN_REL = 0.4
PRECOMMIT_MIN_REL = 0.4

# CLI and MCP
CLI_COMMANDS: Tuple[str, ...] = ("init", "status", "record", "recall", "check", "issues", "import", "worker",
                                 "doctor", "uninstall", "mcp", "hook", "host", "rebuild")
MCP_TOOLS: Tuple[str, ...] = ("hearmemory_recall", "hearmemory_record", "hearmemory_check", "hearmemory_issues", "hearmemory_status")
MCP_PROTOCOL_VERSIONS: Tuple[str, ...] = ("2025-06-18", "2025-03-26", "2024-11-05")

# Host hook events (normalised names used by `hearmemory hook <host> <event>`)
HOOK_EVENTS: Mapping[str, Tuple[str, ...]] = {
    "claude": ("SessionStart", "PostToolUse", "PostToolUseFailure", "PreToolUse", "SubagentStart",
               "SubagentStop", "Stop", "UserPromptSubmit"),
    "codex": ("SessionStart", "Stop", "PostToolUse", "PreToolUse"),
    "cursor": ("sessionStart", "beforeShellExecution", "afterShellExecution", "afterFileEdit",
               "afterAgentResponse", "stop", "beforeMCPExecution", "afterMCPExecution"),
    "git": ("pre-commit",),
}
INSTALL_ACTIONS: Tuple[str, ...] = ("created", "block_inserted", "json_merged", "chained", "exclude_added",
                                    "reused")   # reused: shared git hook dispatcher already present

# Claude Code fires PostToolUse only when a tool SUCCEEDS. A failing tool, including a Bash command that
# exits non-zero (e.g. a failing pytest run), fires PostToolUseFailure instead. Both events are registered
# with the SAME matcher; host generates both settings entries from these two constants.
CLAUDE_CAPTURE_EVENTS: Tuple[str, ...] = ("PostToolUse", "PostToolUseFailure")
CLAUDE_TOOL_MATCHER = ("Bash|Edit|MultiEdit|Write|NotebookEdit|Read|Grep|Glob|Task|Agent|WebFetch|WebSearch|"
                       "mcp__hearmemory__hearmemory_record")

# Every hook event runs under one budget profile (HOOK_STEP_BUDGETS_MS).
HOOK_PROFILES: Mapping[str, str] = {
    "claude:SessionStart": "session_start", "claude:SubagentStart": "push", "claude:UserPromptSubmit": "push",
    "claude:PreToolUse": "precommit", "claude:PostToolUse": "record", "claude:PostToolUseFailure": "record",
    "claude:SubagentStop": "record", "claude:Stop": "record",
    "codex:SessionStart": "session_start", "codex:Stop": "import", "codex:PostToolUse": "record",
    "codex:PreToolUse": "precommit",
    "cursor:sessionStart": "session_start", "cursor:beforeShellExecution": "precommit",
    "cursor:afterShellExecution": "record", "cursor:afterFileEdit": "record", "cursor:afterAgentResponse": "record",
    "cursor:stop": "record", "cursor:beforeMCPExecution": "record", "cursor:afterMCPExecution": "record",
    "git:pre-commit": "precommit",
}
# Per-step soft slices (ms). "startup" (interpreter + imports) and "render" (brief / check text) are RESERVED:
# optional steps only get min(slice, remaining - reserved) and are skipped when that is not enough, so a slow
# import can never swallow the check output. sum(slices) + HOOK_BUDGET_SLACK_MS <= total (hook_budget_fits).
HOOK_STEP_BUDGETS_MS: Mapping[str, Mapping[str, int]] = {
    "record": {"startup": 250, "normalize_append": 150, "link": 50, "spawn": 50},
    "import": {"startup": 250, "import_codex": 900, "spawn": 50},
    "push": {"startup": 250, "normalize_append": 100, "overlay": 150, "render": 300},
    "precommit": {"startup": 250, "import_codex": 350, "overlay": 200, "render": 350, "spawn": 50},
    "session_start": {"startup": 250, "import_codex": 700, "memory": 600, "render": 400, "spawn": 50},
}
HOOK_PROFILE_TOTAL_KEY: Mapping[str, str] = {"record": "timeout_ms", "import": "timeout_ms", "push": "timeout_ms",
                                             "precommit": "timeout_ms", "session_start": "session_start_budget_ms"}
HOOK_RESERVED_STEPS: Tuple[str, ...] = ("startup", "render")
HOOK_BUDGET_SLACK_MS = 150                  # the hard backstop (run_guarded) fires only at the full total

# Exit codes (hooks always exit 0, except the git hook in hold_once/block)
EXIT_OK = 0
EXIT_BLOCKED = 1            # `hearmemory check` decision hold/block; git hook refusing a commit
EXIT_USAGE = 2
EXIT_NOT_INITIALISED = 3

# Layout of <project>/.hearmemory (relative paths). Raw = append-only, never rewritten.
LAYOUT: Mapping[str, str] = {
    "version": "VERSION",
    "config": "config.toml",
    "observations": "observations.jsonl",    # raw
    "claims": "claims.jsonl",                # extraction log (append-only, derived-but-persisted)
    "candidates": "candidates.jsonl",        # raw queue definitions
    "judgments": "judgments.jsonl",          # raw; also the judgment cache
    "events": "events.jsonl",                # raw control events
    "ledger": "ledger/jev.jsonl",            # raw Jev call ledger
    "spool": "spool",                        # lock-free fallback files
    "state": "state",                        # derived; safe to delete
    "archive": "archive",                    # moved-in, never deleted
    "host": "host",                          # generated per-session host configs
    "locks": "locks",
    "logs": "logs",
    "manifest": "install_manifest.json",
}
RAW_FILES: Tuple[str, ...] = ("observations", "claims", "candidates", "judgments", "events", "ledger")
STATE_FILES: Mapping[str, str] = {
    "memory": "state/memory.json", "queue": "state/queue.json", "cursors": "state/cursors.json",
    "index": "state/index.json", "worker": "state/worker.json", "jev_health": "state/jev_health.json",
    "git_holds": "state/git_holds.json", "sessions_dir": "state/sessions",
    "extract_index": "state/extract_index.json",     # Incremental extractor index (bounded window)
    "codex_launches": "state/codex_launches.json",   # proxy session id -> {epoch, alias} (session_alias source)
}
LOCK_NAMES: Tuple[str, ...] = ("obs", "claims", "candidates", "judgments", "events", "ledger", "state", "worker",
                               "pipeline")
# "pipeline": held for ONE run of import/extract/rules/jev/rebuild (worker, CLI --wait, MCP rebuild).
# Hooks only try it NON-blocking and skip that work when it is held. Raw appends never need it.

# Default config.toml content as data (core renders/merges it; other modules read keys from it).
DEFAULT_CONFIG: Mapping[str, Mapping[str, Any]] = {
    "project": {"name": ""},                                   # "" -> directory name
    "hosts": {"enabled": ["claude", "codex", "git"]},          # cursor is opt-in
    "capture": {"max_text_chars": 4000, "store_file_reads": False, "store_user_prompts": True,
                "user_prompt_chars": 500, "unknown_tools": False, "fsync": False},
    "privacy": {"exclude_globs": [".env", ".env.*", "!.env.example", "!.env.sample", "*.pem", "*.key", "*.p12",
                                  "*.pfx", "id_rsa*", "id_ed25519*", "*secret*", "*credential*", "*.keystore",
                                  ".ssh/*", "*.kdbx", ".netrc", ".pgpass", ".npmrc", ".pypirc", "environ",
                                  ".git/**", ".hearmemory/**", "node_modules/**", ".venv/**", "venv/**"],
                "extra_redact_patterns": [], "send_to_jev": True, "jev_exclude_globs": [],
                "redact_env_values": True, "withhold_env_dumps": True, "redact_hex_min_len": 32},
    "jev": {"enabled": True, "model": "jev-1.13.0", "base_url": "https://api.typesafe.ai", "timeout_s": 8.0,
            "daily_call_cap": 200, "daily_usd_cap": 0.05, "max_calls_per_run": 40,
            "usd_per_million_input": 0.042, "max_concurrency": 4, "max_attempts": 5},
    "extract": {"max_candidates_per_run": 20, "a1_max_per_run": 4, "a2_max_per_run": 4, "a3_max_per_run": 6,
                "b1_max_per_run": 10, "a3_min_jaccard": 0.35, "a3_rule_restates_jaccard": 0.92,
                "a2_signature_jaccard": 0.5, "a2_max_gap_hours": 48, "a1_alias_token_jaccard": 0.67,
                "b1_max_evidence": 3, "b1_evidence_chars": 800, "claims_per_message": 5,
                "b1_rejudge_per_claim_per_day": 3, "max_mentions_per_obs": 12, "link_grace_s": 180,
                "history_window_days": 14, "history_max_obs": 5000},
    "brief": {"lang": "en", "session_start_tokens": 600, "subagent_tokens": 300, "push_tokens": 200,
              "push_on_prompt": True, "push_on_tool_use": False, "push_min_interval_s": 60,
              "p1_max": 3, "p2_max": 3, "p3_max": 5, "empty_context_days": 3},
    "precommit": {"claude_mode": "warn", "git_mode": "warn", "cursor_mode": "warn", "codex_mode": "warn",
                  "min_rel": 0.4, "max_tokens": 400, "hold_window_s": 900},
    "issues": {"from_unresolved_alignment": False, "from_insufficient_conclusion": True},
    "judge": {"b1_min_support_confidence": 0.65},     # a weaker model "supports" is [WEAK SUPPORT]
    "worker": {"spawn_from_hooks": True, "idle_exit_s": 600, "poll_s": 1.0, "run_deadline_s": 30.0,
               "import_codex_every_s": 60, "rebuild_min_interval_s": 5.0, "handoff_wait_s": 5.0,
               "stop_timeout_s": 3.0, "inline_judge_s": 2.5},
    "archive": {"min_age_days": 7, "keep_disputed_days": 30},
    "hooks": {"timeout_ms": 1500, "session_start_budget_ms": 2500, "hook_rebuild_max_obs": 1500},
    "mcp": {"rebuild_budget_s": 3.0},
    "import": {"codex_home": "", "codex_max_age_days": 14, "codex_max_line_bytes": 4194304},
}

# Robustness
HOOK_TIMEOUT_MS_DEFAULT = 1500
LOCK_TIMEOUT_S_DEFAULT = 0.5
MAX_TEXT_CHARS_DEFAULT = 4000
WORKER_MODES: Tuple[str, ...] = ("daemon", "single_pass", "once")

# Record echo and actor identity
RECORD_ECHO_PREFIX = "hearmemory: recorded "     # `hearmemory record` prints exactly "hearmemory: recorded <obs_id>"
OBS_ID_RE = r"\bo-[0-9a-f]{16}\b"
AGENT_HOSTS: Tuple[str, ...] = ("claude", "codex", "cursor")
PROXY_SOURCES: Tuple[str, ...] = ("cli", "mcp")   # provenance.source of records written via hearmemory's CLI / MCP
ACTOR_WILDCARD = "?"

# Reasons Jev is unavailable IN THIS PROCESS ONLY. Never written to state/jev_health.json: Codex strips
# *KEY*/*TOKEN*/*SECRET* env vars from shell commands, starts MCP servers with a minimal env, and its sandbox
# may block the network - a process started there says nothing about the other agents of the project.
JEV_LOCAL_REASONS: Tuple[str, ...] = ("no_key", "no_sdk", "config_disabled", "privacy_disabled",
                                      "sandbox_no_network")
SANDBOX_NO_NETWORK_ENV: Tuple[str, ...] = ("CODEX_SANDBOX_NETWORK_DISABLED",)   # value "1" -> sandbox_no_network

# Privacy vocabulary (core implements; pinned here so every module and test agrees).
_SECRET_NAME_CORE = (r"(?!\w*(?:error|exception|warning)\b)"
                     r"\w*(?:key|token|secret|pass(?!ed\b|es\b|ing\b)|pwd|credential|auth(?!or))\w*")
SECRET_NAME_RE = r"(?i)^" + _SECRET_NAME_CORE + r"$"          # an env/config NAME that looks secret-bearing
SECRET_ASSIGNMENT_RE = (r"(?i)\b(" + _SECRET_NAME_CORE + r")"  # NAME=VALUE, NAME: VALUE, "name": "value"
                        r"(\s*[\"']?\s*[:=]\s*[\"']?)([^\s\"',;]{4,})")   # group 3 = the value to redact
ENV_DUMP_WORDS: Tuple[str, ...] = ("env", "printenv", "export", "set", "declare", "typeset", "compgen")
HEX_SECRET_MIN_LEN = 32
# Applied to the (<= 40) chars on the same line BEFORE a long hex run: a match keeps the run (commit / digest).
HEX_SAFE_CONTEXT_RE = (r"(?i)\b(?:commit|sha|sha1|sha256|hash|digest|tree|blob|object|parent|head|rev|revision|"
                       r"merge|checksum|md5|etag)\b[^\n]{0,24}$")
# Model-visible state must never contain relative times (they would change input_hash over time).
RELATIVE_TIME_RE = (r"(?i)\b\d+\s*(?:s|secs?|seconds?|m|mins?|minutes?|h|hrs?|hours?|d|days?|w|weeks?)\s+ago\b"
                    r"|\bjust now\b|\byesterday\b|\d+\s*(?:秒|分钟|小时|天|周)前")


# ---------------------------------------------------------------------------
# Pure helpers (must be identical in every module)
# ---------------------------------------------------------------------------
def canonical_json(obj: Any) -> str:
    """Deterministic JSON used for every hash and id."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def stable_id(prefix: str, *parts: Any) -> str:
    """prefix + first 16 hex of sha256(canonical_json(parts))."""
    return prefix + hashlib.sha256(canonical_json(list(parts)).encode("utf-8")).hexdigest()[:16]


def obs_id_for(event_key: str) -> str:
    """Observation id from its host event key, so re-import is idempotent."""
    return stable_id("o-", event_key)


def claim_id_for(obs_id: str, span: Sequence[int]) -> str:
    return stable_id("c-", obs_id, list(span))


def input_hash(template_id: str, template_version: str, state: Mapping[str, Any]) -> str:
    """Hash of exactly what a judge sees (cache key part)."""
    return hashlib.sha256(canonical_json([template_id, template_version, state]).encode("utf-8")).hexdigest()


def candidate_id_for(template_id: str, template_version: str, subject_key: str, direction: Optional[str],
                     state_hash: str) -> str:
    return stable_id("k-", template_id, template_version, subject_key, direction or "", state_hash)


def judge_cache_key(provider: str, model: Optional[str], template_version: str, in_hash: str) -> str:
    return stable_id("jc-", provider, model or "", template_version, in_hash)


def issue_id_for(kind: str, subject_key: str) -> str:
    """Idempotent issue id: same kind + same subject -> same issue."""
    return stable_id("i-", kind, subject_key)


def span_node(obs_id: str, span: Optional[Sequence[int]]) -> str:
    """Span-level memory node ("obs_id" or "obs_id#start-end")."""
    if not span:
        return obs_id
    return f"{obs_id}#{int(span[0])}-{int(span[1])}"


def brief_relevance(path_score: float, n_shared_identifiers: int) -> float:
    """Absolute relevance in [0,1] . path_score is 1 (same path),
    0.5 (same directory) or 0. Gate: 0 unless a path or an identifier is shared."""
    if path_score not in (0, 0.0, 0.5, 1, 1.0):
        raise ValueError("path_score must be 0, 0.5 or 1")
    n = max(0, int(n_shared_identifiers))
    ident = min(n, REL_IDENT_SATURATION) / REL_IDENT_SATURATION
    return round(REL_PATH_WEIGHT * float(path_score) + REL_IDENT_WEIGHT * ident, 6)


def estimate_usd(input_tokens: int, usd_per_million_input: float = JEV_USD_PER_MILLION_INPUT) -> float:
    return round(max(0, int(input_tokens)) * float(usd_per_million_input) / 1_000_000, 8)


def jev_time(ts: Optional[str]) -> Optional[str]:
    """Model-visible time: absolute UTC to the minute ("2026-09-24T13:05Z"). Never relative ("2h ago"):
    relative times would make input_hash / candidate_id depend on when extraction ran."""
    if not ts:
        return None
    return ts[:16] + "Z" if len(ts) >= 16 else ts


def key_fingerprint(api_key: str) -> str:
    """Non-reversible short fingerprint that scopes JevHealth.auth_denied to one key. Never log the key."""
    return "kf-" + hashlib.sha256(("hearmemory-key-fp:" + (api_key or "")).encode("utf-8")).hexdigest()[:12]


def hook_budget_fits(profile: str, hooks_config: Mapping[str, Any]) -> bool:
    """True when the profile's step slices + slack fit in its configured total (hooks.* in config)."""
    steps = HOOK_STEP_BUDGETS_MS[profile]
    total = int(hooks_config.get(HOOK_PROFILE_TOTAL_KEY[profile], 0) or 0)
    return sum(steps.values()) + HOOK_BUDGET_SLACK_MS <= total


def actor_key(p: Any) -> str:
    """Actor identity: the unit of every "different agent" / "same agent" rule.
    claude -> "claude:<session>:<agent_id|main>" (the main agent and each subagent are separate actors);
    codex  -> "codex:<rollout session>" (a Codex sub-thread has its own rollout session);
    cursor -> "cursor:<conversation_id>" (generation_id changes every turn: meta only, never the actor);
    others -> "<host>:<session>".
    A PROXY record (source in PROXY_SOURCES, host an agent host) without a provenance_link resolves to the
    wildcard "<host>:?" = "some actor of this host". Apply ActorMap first to use links."""
    host = getattr(p, "host", None) or ACTOR_WILDCARD
    if getattr(p, "source", None) in PROXY_SOURCES and host in AGENT_HOSTS:
        return f"{host}:{ACTOR_WILDCARD}"
    sid = getattr(p, "session_id", None) or ACTOR_WILDCARD
    if host == "claude":
        return f"claude:{sid}:{getattr(p, 'subagent_id', None) or 'main'}"
    return f"{host}:{sid}"


def session_alias_target(host: str, session_id: str) -> str:
    """ControlEvent(kind="session_alias").target for a proxy session (e.g. launch.sh's HEARMEMORY_SESSION_ID)."""
    return f"{host}:{session_id}"


def actors_may_coincide(a: str, b: str) -> bool:
    """True if two actor keys are, or may be, the same actor (equal, or same host and one is a wildcard).
    "Different actor" rules use `not actors_may_coincide`; "seen by the same actor" rules use it as is."""
    if a == b:
        return True
    ha, _, ra = a.partition(":")
    hb, _, rb = b.partition(":")
    if ha != hb:
        return False
    return ra.split(":", 1)[0] == ACTOR_WILDCARD or rb.split(":", 1)[0] == ACTOR_WILDCARD


# ---------------------------------------------------------------------------
# Dataclass (de)serialisation
# ---------------------------------------------------------------------------
def _to_jsonable(v: Any) -> Any:
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return {f.name: _to_jsonable(getattr(v, f.name)) for f in dataclasses.fields(v)}
    if isinstance(v, (list, tuple)):
        return [_to_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _to_jsonable(x) for k, x in v.items()}
    return v


class Record:
    """Mixin: to_dict() (JSON-able, field order preserved) and tolerant from_dict()
    (unknown keys ignored so newer writers never break older readers; nested
    dataclass fields are rebuilt from NESTED)."""

    NESTED: Mapping[str, Any] = {}          # field name -> Record subclass (or (list, cls))

    def to_dict(self) -> Dict[str, Any]:
        return _to_jsonable(self)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]):
        if d is None:
            return None
        names = {f.name for f in dataclasses.fields(cls)}
        kw: Dict[str, Any] = {}
        for k, v in dict(d).items():
            if k not in names:
                continue
            spec = cls.NESTED.get(k)
            if spec is not None and v is not None:
                if isinstance(spec, tuple) and spec[0] is list:
                    v = [spec[1].from_dict(x) if isinstance(x, Mapping) else x for x in v]
                elif isinstance(v, Mapping):
                    v = spec.from_dict(v)
            kw[k] = v
        return cls(**kw)


# ---------------------------------------------------------------------------
# observations
# ---------------------------------------------------------------------------
@dataclass
class Provenance(Record):
    host: str                                   # one of HOSTS
    session_id: Optional[str] = None            # host session / conversation id
    subagent_id: Optional[str] = None           # Claude agent_id, Cursor generation id, ...
    subagent_type: Optional[str] = None
    agent_label: Optional[str] = None           # human label, e.g. "codex", "claude:explorer"
    model: Optional[str] = None
    git_branch: Optional[str] = None
    git_commit: Optional[str] = None            # full sha of HEAD at capture time
    git_dirty: Optional[bool] = None
    cwd: Optional[str] = None                   # relative to project root ("." = root)
    source: Optional[str] = None                # "hook:PostToolUse", "import:codex_rollout", "cli", "mcp"
    transcript_ref: Optional[str] = None        # host transcript path + line (never copied wholesale)


@dataclass
class RunnerSummary(Record):
    runner: str                                 # pytest | unittest | jest | go | cargo | npm | other
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    failed_ids: List[str] = field(default_factory=list)   # e.g. tests/test_x.py::test_y (<= 20)
    target: Optional[str] = None                # normalised command target (e.g. "pytest tests/test_x.py")


@dataclass
class ToolInfo(Record):
    name: str                                   # host tool name (Bash, exec_command, Edit, apply_patch, ...)
    command: Optional[str] = None               # redacted shell command
    paths: List[str] = field(default_factory=list)   # project-relative, posix
    exit_code: Optional[int] = None
    status: str = "unknown"                     # TOOL_STATUSES
    test: Optional[RunnerSummary] = None
    NESTED = {"test": RunnerSummary}


@dataclass
class Observation(Record):
    """One append-only raw record in .hearmemory/observations.jsonl."""
    id: str
    ts: str                                     # UTC ISO-8601 "YYYY-MM-DDTHH:MM:SS.ffffffZ"
    kind: str                                   # OBS_KINDS
    event_key: str                              # host-stable dedupe key, e.g. "claude:<sid>:<tool_use_id>"
    provenance: Provenance
    text: str = ""                              # redacted + truncated
    tool: Optional[ToolInfo] = None
    text_sha256: str = ""                       # of the redacted, untruncated text
    truncated: bool = False
    redactions: int = 0
    excluded: bool = False                      # touched a privacy-excluded path: text withheld
    refs: List[str] = field(default_factory=list)   # obs ids this record cites / links
    meta: Dict[str, Any] = field(default_factory=dict)
    schema: str = OBS_SCHEMA
    NESTED = {"provenance": Provenance, "tool": ToolInfo}

    @property
    def is_primary(self) -> bool:
        return self.kind in PRIMARY_OBS_KINDS

    @property
    def is_assertive(self) -> bool:
        return self.kind in ASSERTIVE_OBS_KINDS

    @property
    def paths(self) -> List[str]:
        return list(self.tool.paths) if self.tool else list(self.meta.get("paths") or [])


@dataclass
class ControlEvent(Record):
    """Append-only control record in .hearmemory/events.jsonl (issue close/reopen, links, seen marks)."""
    id: str
    ts: str
    kind: str                                   # EVENT_KINDS
    target: str                                 # issue id / obs id / claim id / candidate id
    data: Dict[str, Any] = field(default_factory=dict)
    provenance: Optional[Provenance] = None
    schema: str = EVENT_SCHEMA
    NESTED = {"provenance": Provenance}


class ActorMap:
    """Effective provenance / actor after provenance_link events. Pure and deterministic: feed events
    in (ts, id) order; the FIRST valid link for an observation wins. A link is valid only when its own
    provenance is host-native (source not in PROXY_SOURCES): a proxy can never vouch for a proxy.
    The effective provenance takes identity fields from the link and git/cwd fields from the record."""

    def __init__(self) -> None:
        self._links: Dict[str, Provenance] = {}
        self._aliases: Dict[str, Provenance] = {}

    def add_event(self, ev: ControlEvent) -> bool:
        if ev.provenance is None:
            return False
        if ev.kind == "session_alias":
            return self.add_alias(ev.target, ev.provenance)
        if ev.kind != "provenance_link":
            return False
        return self.add_link(ev.target, ev.provenance)

    def add_link(self, obs_id: str, link: Provenance) -> bool:
        if not obs_id or obs_id in self._links or not link.host or link.source in PROXY_SOURCES:
            return False
        self._links[obs_id] = link
        return True

    def add_alias(self, target: str, native: Provenance) -> bool:
        """Session-level link: every PROXY record whose (host, session_id) is `target`
        ("<host>:<proxy session id>") resolves to `native`. First valid alias wins; a per-record
        provenance_link still takes precedence."""
        if not target or target in self._aliases or not native.host or native.source in PROXY_SOURCES \
                or not native.session_id:
            return False
        self._aliases[target] = native
        return True

    def native_session(self, host: Optional[str], session_id: Optional[str]) -> Optional[Provenance]:
        """The host-native session a proxy session id is an alias of (None when unknown)."""
        if not host or not session_id:
            return None
        return self._aliases.get(session_alias_target(host, session_id))

    def _link_for(self, obs: "Observation") -> Optional[Provenance]:
        link = self._links.get(obs.id)
        if link is not None:
            return link
        o = obs.provenance
        if self._aliases and getattr(o, "source", None) in PROXY_SOURCES:
            return self.native_session(getattr(o, "host", None), getattr(o, "session_id", None))
        return None

    def linked(self, obs_id: str) -> bool:
        return obs_id in self._links

    def resolved(self, obs: "Observation") -> bool:
        """True when a provenance_link or a session_alias gives this record a host-native identity."""
        return self._link_for(obs) is not None

    def provenance_of(self, obs: "Observation") -> Provenance:
        link = self._link_for(obs)
        o = obs.provenance
        if link is None:
            return o
        return dataclasses.replace(o, host=link.host, session_id=link.session_id, subagent_id=link.subagent_id,
                                   subagent_type=link.subagent_type or o.subagent_type,
                                   agent_label=link.agent_label or o.agent_label, model=link.model or o.model,
                                   source=link.source or "link", transcript_ref=link.transcript_ref or o.transcript_ref)

    def actor_of(self, obs: "Observation") -> str:
        return actor_key(self.provenance_of(obs))


# ---------------------------------------------------------------------------
# extraction
# ---------------------------------------------------------------------------
@dataclass
class Mention(Record):
    kind: str                                   # MENTION_KINDS
    surface: str                                # as written
    norm: str                                   # normalised key, e.g. "path:src/a/sync.py", "symbol:LedgerSync"
    obs_id: str
    span: List[int]                             # [start, end) in the observation text
    grounded: bool                              # resolved against the ProjectIndex
    resolved: List[str] = field(default_factory=list)   # candidate project paths / qualified names


@dataclass
class Claim(Record):
    claim_id: str
    obs_id: str
    span: List[int]
    text: str
    claim_class: str                            # CLAIM_CLASSES
    mentions: List[Mention] = field(default_factory=list)
    paths: List[str] = field(default_factory=list)
    parent_claim_id: Optional[str] = None       # set for premise claims (B3 folded into B1)
    explicit: bool = False                      # recorded as kind=claim (heuristics skipped)
    NESTED = {"mentions": (list, Mention)}


@dataclass
class Candidate(Record):
    """A judgment question waiting in the queue (.hearmemory/candidates.jsonl)."""
    candidate_id: str
    template_id: str                            # TEMPLATE_IDS
    template_version: str
    subject_key: str                            # what is judged: "pair:<nodeA>|<nodeB>", "claim:<id>", ...
    state: Dict[str, Any]                       # EXACT model-visible state (whitelisted, redacted, capped)
    input_hash: str
    basis_obs_ids: List[str]                    # every obs the state was built from
    created_ts: str
    direction: Optional[str] = None             # A3: "a_contains_b" | "b_contains_a"
    priority: int = 50                          # 0 = most urgent
    rule_hint: Optional[str] = None             # rule id that could decide it deterministically
    supersedes: Optional[str] = None            # older candidate id for the same subject (new evidence)
    meta: Dict[str, Any] = field(default_factory=dict)   # program-side only, NEVER sent to Jev
    extractor_version: str = EXTRACTOR_VERSION
    schema: str = CANDIDATE_SCHEMA


@dataclass
class ExtractResult(Record):
    claims: List[Claim] = field(default_factory=list)
    candidates: List[Candidate] = field(default_factory=list)
    dropped: Dict[str, int] = field(default_factory=dict)   # reason -> count (explains conservatism)
    NESTED = {"claims": (list, Claim), "candidates": (list, Candidate)}


# ---------------------------------------------------------------------------
# judgments
# ---------------------------------------------------------------------------
@dataclass
class Judgment(Record):
    """Append-only row in .hearmemory/judgments.jsonl."""
    judgment_id: str
    candidate_id: str
    template_id: str
    template_version: str
    input_hash: str
    provider: str                               # PROVIDERS
    outcome: str                                # JUDGMENT_OUTCOMES
    ts: str
    label: Optional[str] = None                 # only when outcome == "valid"
    probabilities: Dict[str, float] = field(default_factory=dict)
    confidence: Optional[float] = None
    model_requested: Optional[str] = None
    model_returned: Optional[str] = None
    rule_id: Optional[str] = None               # provider == "rule"
    cached_from: Optional[str] = None           # provider == "cache": original judgment id
    latency_s: Optional[float] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    est_usd: Optional[float] = None
    error: Optional[str] = None                 # short, never contains request text or secrets
    schema: str = JUDGMENT_SCHEMA


@dataclass
class LedgerRow(Record):
    """.hearmemory/ledger/jev.jsonl: one row per Jev network attempt (billable or not)."""
    ts: str
    day: str                                    # UTC YYYY-MM-DD (budget bucket)
    candidate_id: Optional[str]
    template_id: Optional[str]
    outcome: str
    input_tokens: int = 0
    output_tokens: int = 0
    input_tokens_estimated: bool = False
    est_usd: float = 0.0
    latency_s: Optional[float] = None
    model: Optional[str] = None
    session_id: Optional[str] = None
    schema: str = LEDGER_SCHEMA


@dataclass
class BudgetStatus(Record):
    day: str
    calls: int
    usd: float
    call_cap: int
    usd_cap: float

    @property
    def exhausted(self) -> bool:
        return self.calls >= self.call_cap or self.usd >= self.usd_cap


@dataclass
class JevHealth(Record):
    """.hearmemory/state/jev_health.json. SHARED by every process of the project, so it holds only facts that are
    true for all of them. Process-local conditions (JEV_LOCAL_REASONS: no key, no SDK, sandbox without
    network, config/privacy off) are NEVER written here."""
    unreachable_until: Optional[str] = None     # set only by a jev_capable process after a REAL transport error
    unreachable_reporter_pid: Optional[int] = None
    auth_denied: Dict[str, str] = field(default_factory=dict)   # key_fingerprint(key) -> until (401/403 with it)
    last_ok_ts: Optional[str] = None            # any success clears unreachable_until
    last_error_kind: Optional[str] = None
    schema: str = "hearmemory.jev_health/1"


@dataclass
class WorkerInfo(Record):
    """.hearmemory/state/worker.json."""
    pid: int
    started_ts: str
    jev_capable: bool                           # False -> mode "single_pass": one pass, then exit (never idles)
    jev_unavailable_reason: Optional[str] = None   # one of JEV_LOCAL_REASONS when not capable
    mode: str = "daemon"                        # WORKER_MODES
    launched_by: Optional[str] = None           # "hook:claude:SessionStart", "mcp:codex", "cli", "launch.sh", ...
    last_beat_ts: Optional[str] = None
    last_spawn_ts: Optional[str] = None
    stats: Dict[str, Any] = field(default_factory=dict)
    exited_ts: Optional[str] = None             # set when the worker process ends (status never trusts a stale file)
    # the WORKER's own Jev state, refreshed every beat, so `hearmemory status` run from a shell without the
    # key reports what the worker can do: {capable, reason, model, day, calls_today, last_call_ts}
    jev: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ScopeFacts(Record):
    """Program facts: did the code change between moment A (the claim's evidence run, or the claim) and
    moment B (a later run of the same target)? Built by the judge's scope_facts() from observations of ANY actor."""
    ts_a: str
    ts_b: str
    commit_a: Optional[str] = None
    commit_b: Optional[str] = None
    watched_paths: List[str] = field(default_factory=list)          # claim paths + target paths
    edited_paths_between: List[str] = field(default_factory=list)   # file_edit paths in (ts_a, ts_b] & watched
    dirty_changed_paths: List[str] = field(default_factory=list)    # watched paths whose meta.dirty_state differs
    worktree_known: bool = False                # both runs carry meta.dirty_state (imports never do)
    edit_obs_ids: List[str] = field(default_factory=list)

    @property
    def unchanged(self) -> bool:
        return (bool(self.commit_a) and self.commit_a == self.commit_b and self.worktree_known
                and not self.edited_paths_between and not self.dirty_changed_paths)


RUN_OUTCOMES: Tuple[str, ...] = ("pass", "fail")


def b1_status_rule(claimed: str, observed: str, facts: ScopeFacts) -> Optional[str]:
    """Rule B1_test_status for a status claim "target passes/fails" and a later run of that target.
    unchanged scope: same outcome -> "supports", opposite -> "refutes" (rule judgment, authoritative);
    changed scope:   opposite -> "outdated" (program status, NOT a refutation, never blocks),
                     same -> None (no rule; the extractor may ask Jev with both commits + edits in scope)."""
    if claimed not in RUN_OUTCOMES or observed not in RUN_OUTCOMES:
        raise ValueError("claimed/observed must be 'pass' or 'fail'")
    if facts.unchanged:
        return "supports" if claimed == observed else "refutes"
    return None if claimed == observed else "outdated"


# ---------------------------------------------------------------------------
# derived memory state (.hearmemory/state/memory.json; rebuildable)
# ---------------------------------------------------------------------------
@dataclass
class HistoryEntry(Record):
    ts: str
    change: str                                 # e.g. "status provisional->disputed"
    reason: str
    decision_ref: Optional[str] = None          # judgment id / event id / rule id


@dataclass
class MemoryOp(Record):
    """Output of a consumer (judgment -> operation). Applied by the memory builder."""
    op_id: str
    kind: str                                   # MEMORY_OP_KINDS
    target: Dict[str, Any]
    basis_obs_ids: List[str]
    decision_ref: str                           # judgment id, event id, or "rule:<id>"
    template_id: Optional[str] = None
    reason: str = ""


@dataclass
class Edge(Record):
    edge_id: str
    relation: str                               # EDGE_RELATIONS
    a: str                                      # span node (span_node())
    b: str
    status: str                                 # EDGE_STATUSES
    basis_obs_ids: List[str] = field(default_factory=list)
    decision_refs: List[str] = field(default_factory=list)
    directed: bool = False
    history: List[HistoryEntry] = field(default_factory=list)
    NESTED = {"history": (list, HistoryEntry)}


@dataclass
class Mark(Record):
    mark_id: str
    kind: str                                   # MARK_KINDS
    a: str
    b: Optional[str] = None
    decision_ref: str = ""


@dataclass
class ClaimView(Record):
    claim: Claim
    status: str = "unjudged"                    # CLAIM_STATUSES (effective, after source-group check)
    judged_label: Optional[str] = None          # raw B1 label
    support_ids: List[str] = field(default_factory=list)
    counter_ids: List[str] = field(default_factory=list)
    premise_claim_ids: List[str] = field(default_factory=list)
    premise_status: Optional[str] = None        # worst status among premise claims
    decision_refs: List[str] = field(default_factory=list)
    equivalents: List[str] = field(default_factory=list)   # A3 restates-both claim ids
    covered_by: Optional[str] = None
    derived_from: Optional[str] = None          # restates this memory claim its author had been shown
    addressed: Optional[Dict[str, Any]] = None  # a finding a later edit + passing run / commit may address
    tier: str = "hot"
    status_ts: Optional[str] = None
    history: List[HistoryEntry] = field(default_factory=list)
    NESTED = {"claim": Claim, "history": (list, HistoryEntry)}


@dataclass
class Issue(Record):
    issue_id: str
    kind: str                                   # ISSUE_KINDS
    title: str
    status: str = "open"                        # ISSUE_STATUSES
    claim_id: Optional[str] = None
    paths: List[str] = field(default_factory=list)
    mentions: List[str] = field(default_factory=list)      # Mention.norm keys
    source_obs_ids: List[str] = field(default_factory=list)
    suggestion: Optional[str] = None            # program rule "C2": e.g. "run `pytest tests/test_x.py`"
    opened_by: Optional[Provenance] = None
    opened_ts: Optional[str] = None
    history: List[HistoryEntry] = field(default_factory=list)
    NESTED = {"history": (list, HistoryEntry), "opened_by": Provenance}


@dataclass
class MemoryState(Record):
    schema: str = STATE_SCHEMA
    fingerprint: str = ""                       # of the raw inputs it was built from
    built_ts: str = ""
    observation_count: int = 0
    obs_offset: int = 0                         # byte length of observations.jsonl this state covers (overlay start)
    claims: Dict[str, ClaimView] = field(default_factory=dict)
    edges: Dict[str, Edge] = field(default_factory=dict)
    marks: Dict[str, Mark] = field(default_factory=dict)
    source_groups: Dict[str, str] = field(default_factory=dict)   # obs id -> group id ("sg-<min obs id>")
    issues: Dict[str, Issue] = field(default_factory=dict)
    tiers: Dict[str, str] = field(default_factory=dict)           # obs/claim id -> "archive" (absent = hot)
    stats: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]):
        base = super().from_dict({k: v for k, v in d.items() if k not in ("claims", "edges", "marks", "issues")})
        base.claims = {k: ClaimView.from_dict(v) for k, v in (d.get("claims") or {}).items()}
        base.edges = {k: Edge.from_dict(v) for k, v in (d.get("edges") or {}).items()}
        base.marks = {k: Mark.from_dict(v) for k, v in (d.get("marks") or {}).items()}
        base.issues = {k: Issue.from_dict(v) for k, v in (d.get("issues") or {}).items()}
        return base


# ---------------------------------------------------------------------------
# Recall, brief and pre-commit check
# ---------------------------------------------------------------------------
@dataclass
class AgentContext(Record):
    """What the requesting agent is working on (trusted metadata only)."""
    host: str
    session_id: Optional[str] = None
    subagent_id: Optional[str] = None
    paths: List[str] = field(default_factory=list)       # recently touched / staged / changed files
    identifiers: List[str] = field(default_factory=list)
    query_text: str = ""
    git_branch: Optional[str] = None
    git_commit: Optional[str] = None


@dataclass
class RecallQuery(Record):
    query: str = ""
    context: Optional[AgentContext] = None
    limit: int = 8
    include_archive: bool = False
    kinds: List[str] = field(default_factory=list)       # restrict to OBS_KINDS; empty = all
    NESTED = {"context": AgentContext}


@dataclass
class RecallItem(Record):
    obs_id: str
    kind: str
    excerpt: str
    score: float
    provenance_text: str
    tags: List[str] = field(default_factory=list)        # rendered status tags, e.g. "[REFUTED]"
    claim_ids: List[str] = field(default_factory=list)
    related: List[str] = field(default_factory=list)     # one-hop edge expansions (obs ids)
    archived: bool = False


@dataclass
class RecallResult(Record):
    query: str
    items: List[RecallItem] = field(default_factory=list)
    text: str = ""
    pending_judgments: int = 0
    NESTED = {"items": (list, RecallItem)}


@dataclass
class BriefRequest(Record):
    context: AgentContext
    purpose: str = "session_start"              # BRIEF_PURPOSES
    max_tokens: int = 600
    lang: str = "en"
    since_ts: Optional[str] = None              # only items changed after this (push)
    NESTED = {"context": AgentContext}


@dataclass
class BriefItem(Record):
    tier: str                                   # BRIEF_TIERS
    kind: str                                   # BRIEF_ITEM_KINDS
    item_key: str                               # dedupe key; a status change makes a new key
    text: str
    refs: List[str] = field(default_factory=list)
    provenance_text: str = ""
    relevance: float = 0.0
    tokens: int = 0


@dataclass
class Brief(Record):
    text: str                                   # "" means inject nothing
    items: List[BriefItem] = field(default_factory=list)
    dropped_for_budget: List[str] = field(default_factory=list)
    token_estimate: int = 0
    pending_judgments: int = 0
    memory_as_of: Optional[str] = None          # MemoryState.built_ts the brief was rendered from
    stale: bool = False                         # rendered from an older memory.json: header says "as of"
    NESTED = {"items": (list, BriefItem)}


@dataclass
class CheckRequest(Record):
    context: AgentContext
    action: str = "git_commit"                  # CHECK_ACTIONS
    payload_text: str = ""                      # commit message + staged diff excerpt, or the claim text
    paths: List[str] = field(default_factory=list)
    mode: str = "warn"                          # PRECOMMIT_MODES
    attempt_key: Optional[str] = None           # hold_once: same key twice -> second attempt allowed
    NESTED = {"context": AgentContext}


@dataclass
class CheckWarning(Record):
    kind: str                                   # WARNING_KINDS
    item_key: str
    text: str
    refs: List[str] = field(default_factory=list)
    relevance: float = 0.0


@dataclass
class CheckResult(Record):
    decision: str                               # CHECK_DECISIONS
    warnings: List[CheckWarning] = field(default_factory=list)
    text: str = ""
    mode: str = "warn"
    memory_as_of: Optional[str] = None
    stale: bool = False
    NESTED = {"warnings": (list, CheckWarning)}

    @property
    def ok(self) -> bool:
        return self.decision in ("allow", "warn")


# ---------------------------------------------------------------------------
# host integration
# ---------------------------------------------------------------------------
@dataclass
class HookResult(Record):
    """What `hearmemory hook <host> <event>` prints/returns. exit_code is 0 unless an
    explicit opt-in blocking mode decided to block."""
    exit_code: int = 0
    stdout: str = ""                            # exact bytes for the host (JSON or text); "" = nothing
    stderr: str = ""                            # short note for humans (never secrets)
    observations: List[str] = field(default_factory=list)   # obs ids written


@dataclass
class InstallRecord(Record):
    path: str                                   # project-relative posix path
    action: str                                 # INSTALL_ACTIONS
    host: str
    marker: str = "hearmemory"                       # block marker / json key marker
    sha256_after: Optional[str] = None          # created files: uninstall deletes only if unchanged
    json_keys: List[List[str]] = field(default_factory=list)   # json_merged: key paths hearmemory added
    backup_path: Optional[str] = None           # chained: where the original hook was moved
    created_file: bool = False                  # block/json written into a file hearmemory itself created
    created_parent_dirs: List[str] = field(default_factory=list)
    shared: bool = False                        # lives in the git hooks dir shared by all worktrees


@dataclass
class InstallManifest(Record):
    """.hearmemory/install_manifest.json"""
    root: str
    python: str                                 # interpreter used in generated commands
    created_ts: str
    records: List[InstallRecord] = field(default_factory=list)
    schema: str = MANIFEST_SCHEMA
    NESTED = {"records": (list, InstallRecord)}


# ---------------------------------------------------------------------------
# Protocols (cross-module services). Concrete modules are listed in ENTRY_POINTS.
# ---------------------------------------------------------------------------
@runtime_checkable
class TokenCounter(Protocol):
    def count(self, text: str) -> int: ...


@runtime_checkable
class StoreAPI(Protocol):
    """hearmemory.store.Store. All raw files append-only; state/ writes atomic.
    append_* are O(1): take the file lock, append, release (or write spool/) - they never read, scan or
    dedupe. Every iter_* / read_* dedupes by id, FIRST occurrence wins. No method ever creates
    .hearmemory itself; writes happen only while .hearmemory/VERSION exists."""
    root: Any                                   # pathlib.Path of the project root
    hearmemory_dir: Any                              # pathlib.Path of <root>/.hearmemory

    def append_observations(self, obs: Sequence[Observation]) -> List[str]: ...
    def iter_observations(self, since_offset: int = 0) -> Iterator[Tuple[int, Observation]]: ...
    def append_candidates(self, cands: Sequence[Candidate]) -> int: ...
    def iter_candidates(self) -> Iterator[Candidate]: ...
    def append_judgments(self, js: Sequence[Judgment]) -> int: ...
    def iter_judgments(self) -> Iterator[Judgment]: ...
    def append_claims(self, claims: Sequence[Claim]) -> int: ...
    def iter_claims(self) -> Iterator[Claim]: ...
    def append_events(self, evs: Sequence[ControlEvent]) -> int: ...
    def iter_events(self) -> Iterator[ControlEvent]: ...
    def read_state(self, name: str) -> Optional[Dict[str, Any]]: ...
    def write_state(self, name: str, data: Mapping[str, Any]) -> None: ...
    def fingerprint(self) -> str: ...
    def is_initialised(self) -> bool: ...
    def read_observations_window(self, from_offset: int, max_bytes: int) -> Tuple[int, List[Observation]]: ...


@runtime_checkable
class ProjectIndexAPI(Protocol):
    """grounding lexicon of the project (files, modules, symbols, tests, services, config keys)."""
    def resolve(self, kind: str, surface: str) -> List[str]: ...
    def files(self) -> Sequence[str]: ...


@runtime_checkable
class ExtractorAPI(Protocol):
    def extract(self, new_obs: Sequence[Observation], history: Sequence[Observation],
                state: Optional[MemoryState]) -> ExtractResult: ...


@runtime_checkable
class JudgeAPI(Protocol):
    name: str                                   # PROVIDERS

    def judge(self, cands: Sequence[Candidate], deadline_s: float) -> List[Judgment]: ...


@runtime_checkable
class MemoryBuilderAPI(Protocol):
    def build(self, store: StoreAPI, config: Mapping[str, Any]) -> MemoryState: ...


@runtime_checkable
class HostAdapterAPI(Protocol):
    name: str                                   # INSTALLABLE_HOSTS

    def install(self, root: Any, python: str, config: Mapping[str, Any]) -> List[InstallRecord]: ...
    def normalize(self, event: str, payload: Mapping[str, Any]) -> List[Observation]: ...
    def handle_hook(self, event: str, payload: Mapping[str, Any]) -> HookResult: ...


# Module-level entry points each module must provide (checked by the integration tests).
ENTRY_POINTS: Mapping[str, str] = {
    # core
    "open_store": "hearmemory.store:open_store",               # (start: Path|str|None, create=False) -> Store|None
    "create_store": "hearmemory.store:create_store",           # (root) -> Store; the only call that may create .hearmemory
                                                          #   itself. `open_store(create=True)` on a
                                                          #   not-yet-initialised root returns a bare *uninitialised*
                                                          #   Store (see its docstring/tests) - callers that actually
                                                          #   need `.hearmemory` to exist (i.e. `hearmemory init`) must call
                                                          #   `create_store`, not rely on `open_store`'s side effects.
    "load_config": "hearmemory.config:load_config",            # (root) -> dict (defaults merged)
    "set_config_value": "hearmemory.config:set_config_value",  # (root, section, key, value) -> bool (line-preserving)
    "file_lock": "hearmemory.locks:file_lock",                 # contextmanager (path, timeout_s) -> bool acquired
    "capture_provenance": "hearmemory.provenance:capture",     # (root, host, **ids) -> Provenance
    "make_observation": "hearmemory.observe:make_observation",  # (root, cfg, kind, text, provenance, ...) -> Observation
    "redact": "hearmemory.privacy:redact",                     # (text) -> (text, n)
    "is_excluded": "hearmemory.privacy:is_excluded",           # (relpath, cfg) -> bool
    "budget": "hearmemory.budget:Budget",                      # class: status(), reserve(), settle(), release()
    "cli_main": "hearmemory.cli:main",                         # (argv=None) -> int
    "deadline": "hearmemory.safety:Deadline",                  # (profile, hooks_cfg) -> .slice_ms(step), .remaining_ms()
    # judge
    "project_index": "hearmemory.judge.project_index:ProjectIndex",
    "extractor": "hearmemory.judge.extract:Extractor",
    "templates": "hearmemory.judge.templates:TEMPLATES",       # dict template id -> spec (wording, labels, fields)
    "jev_judge": "hearmemory.judge.jev:JevJudge",
    "rule_judge": "hearmemory.judge.rules:RuleJudge",
    "run_pipeline": "hearmemory.judge.worker:run_pipeline",    # (store, cfg, deadline_s, use_jev, mode) -> stats;
                                                          #   takes "pipeline" non-blocking, else {"skipped": "busy"}
    "spawn_worker": "hearmemory.judge.worker:spawn_background",  # (root, launched_by=None) -> bool (never raises)
    "run_worker": "hearmemory.judge.worker:run_worker",        # (root, mode, launched_by=..., ...) -> exit code
    "worker_status": "hearmemory.judge.worker:worker_status",  # (root) -> {"state": running|starting|stopped|stale, ...}
    "stop_worker": "hearmemory.judge.worker:stop_worker",      # (root, timeout_s) -> bool (verifies pid + lock first)
    "jev_capability": "hearmemory.judge.jev:jev_capability",   # (cfg, environ) -> (capable: bool, reason|None)
    "scope_facts": "hearmemory.judge.scope:scope_facts",       # (observations, run_a, run_b, watched_paths) -> ScopeFacts
    # memory
    "memory_builder": "hearmemory.memory.build:MemoryBuilder",
    "load_memory": "hearmemory.memory.build:load_or_rebuild",  # (store, cfg, allow_rebuild=True, deadline_s=None)
                                                          #   -> MemoryState (older state when it cannot rebuild)
    "recall": "hearmemory.memory.recall:recall",               # (state, store, RecallQuery, cfg) -> RecallResult
    "build_brief": "hearmemory.memory.brief:build_brief",      # (state, store, BriefRequest, cfg) -> Brief
    "check": "hearmemory.memory.precommit:check",              # (state, store, CheckRequest, cfg) -> CheckResult
    # CLI/MCP
    "commands": "hearmemory.commands:COMMANDS",                # dict name -> handler(args, ctx) -> int
    "mcp_serve": "hearmemory.mcp_server:serve",                # (root, host, stdin, stdout) -> int
    # host
    "host_adapters": "hearmemory.host:ADAPTERS",               # dict host -> HostAdapterAPI instance
    "hook_main": "hearmemory.host.hooks:run_hook",             # (host, event, stdin_bytes, root=None) -> HookResult
    "install": "hearmemory.host.install:install",              # (root, hosts, cfg) -> InstallManifest
    "uninstall": "hearmemory.host.install:uninstall",          # (root, purge=False) -> List[str] notes
    "import_codex": "hearmemory.host.codex:import_rollouts",   # (store, cfg, since=None, session=None) -> int
    "codex_sync": "hearmemory.host.codex:sync_session",        # (store, cfg, proxy_session_id, budget_s) -> dict
                                                          #   bounded import + launch/rollout session reconcile
}
