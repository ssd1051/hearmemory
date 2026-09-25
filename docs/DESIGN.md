# HearMemory design

This document describes how HearMemory works internally: what it records, where it stores it, how it turns agent output into claims and judgment questions, how those questions are answered, and how the resulting project memory reaches agents. It is written for developers who want to understand, debug or extend the tool.

Module, field and constant names refer to the `hearmemory` package under `src/hearmemory/`; `interfaces.py` is the single source of truth for the data model and for every constant quoted here. `<project>` is the project root (where `hearmemory init` ran) and `~` is the user's home directory. The CLI is `hearmemory`; `hmem` is an equivalent short alias installed with the package.

---

## 1. Overview

Several coding agents often work on the same repository: a Claude Code session and its subagents, a Codex session, a Cursor conversation, one after another or at the same time. Each runs tests, edits files and reaches conclusions ("the bug is in `sync.py`", "tests pass now"), and each forgets all of it when its session ends. The next agent starts from zero, or worse, repeats a conclusion another agent already disproved.

HearMemory gives the project a shared, local, append-only memory:

1. **Capture.** Host adapters record what agents do (commands and output, file edits, searches, final messages, subagent reports) into `<project>/.hearmemory/`.
2. **Extract.** A deterministic extractor pulls *claims* out of agent-written text and generates a small number of *judgment questions*: are these two mentions the same object, are these two failures the same event, does claim X restate claim Y, does the evidence support claim Z.
3. **Judge.** Questions a program can decide are answered by deterministic rules. The rest are sent, optionally and within a strict budget, to **Jev**, TypeSafe's judge model, from a background worker.
4. **Build memory.** A pure, deterministic rebuild turns raw records plus judgments into claim statuses (`[SUPPORTED]`, `[REFUTED]`, `[DISPUTED]`, ...), issues and relations.
5. **Deliver.** Agents receive a short brief at session start, can query memory with `hearmemory recall` or the `hearmemory_recall` MCP tool, and get a check before `git commit`.

The core rule: what an agent says is only an assertion until evidence from somewhere else backs it. An agent restating its own command output, or restating memory it was just shown, never counts as independent support.

### 1.1 Goals

- **Cross-agent value.** Surface what *other* agents found, especially claims that were refuted or are disputed, before the current agent relies on them.
- **Evidence over assertion.** A claim is `[SUPPORTED]` only when a primary observation (a command run, a file edit, a search) outside the claim's own source group backs it.
- **Never in the way.** Hooks are time-budgeted and always exit 0 (except the git hook in an explicitly enabled blocking mode). A missing key, no network, or a damaged `.hearmemory/` never breaks an agent.
- **Project-scoped and reversible.** Nothing is written to user-level configuration. Every file added outside `.hearmemory/` is recorded in an install manifest and removed exactly by `hearmemory uninstall`.
- **Private by default.** Everything stays on local disk. Secrets are redacted before anything is written; only small, template-whitelisted snippets are ever sent to Jev.
- **Cheap.** Rules first, cache second, Jev last; daily call and dollar caps; zero cost without a key.

### 1.2 Non-goals

- Semantic search or embeddings. Relevance is lexical (paths and identifiers).
- Synchronisation between machines or users. One project directory on one machine.
- Replacing code review or tests. HearMemory reports what agents claimed and what the evidence says; it does not decide whether code is correct.
- Full language parsing. The project index uses light regular expressions.
- Windows (file locking uses `fcntl`).

---

## 2. Architecture

```
 Claude Code hooks   Codex session logs    Cursor hooks      git pre-commit     CLI / MCP
 (settings.json)     (~/.codex/sessions,   (.cursor/         (dispatcher +      (hearmemory record,
        |             read-only import)     hooks.json)       project script)    hearmemory_record)
        +--------------------+-------------------+-----------------+------------------+
                             |  normalise, redact, truncate
                             v
          .hearmemory/observations.jsonl (+ events.jsonl)   append-only, O(1) writes,
                             |                              spool/ when a lock is busy
        background worker    |   (singleton; "pipeline" lock per pass)
     +-----------------------v--------------------------------------------------+
     | merge spool -> import Codex -> extractor (claims, candidates)            |
     |   -> rule judge -> Jev judge (cache, budget, retries)                    |
     |   -> memory rebuild (pure function of the raw files)                     |
     +-----------------------+--------------------------------------------------+
                             v
                  .hearmemory/state/memory.json
                             |
         +-------------------+----------------------+
         v                   v                      v
   brief (session start,   recall (CLI / MCP)    check (git hook, Claude PreToolUse,
   subagent start, push)                         Cursor, CLI, MCP)
```

### 2.1 Components

| Component | Modules | Role |
|---|---|---|
| Host adapters | `host/claude.py`, `host/codex.py`, `host/cursor.py`, `host/git.py`, `host/hooks.py` | Map host events to observations and provenance links; answer hooks with a brief or a check. |
| Installer | `host/install.py`, `host/manifest.py`, `host/snippets.py` | Generate per-host files, record them in the install manifest, undo them exactly. |
| Capture core | `observe.py`, `provenance.py`, `privacy.py`, `testcmd.py` | Build an `Observation`: exclusion, redaction, truncation, git provenance, test summary parsing. |
| Store | `store.py`, `locks.py` | Append-only JSONL, read-side dedupe, spool, atomic state writes. |
| Extractor | `judge/project_index.py`, `judge/mentions.py`, `judge/claims.py`, `judge/extract.py`, `judge/scope.py` | Claims and judgment candidates from new observations. |
| Judges | `judge/rules.py`, `judge/jev.py`, `judge/templates.py`, `judge/cache.py` | Rule judgments; Jev calls. |
| Worker | `judge/worker.py`, `judge/queue.py`, `budget.py` | Background pipeline, queue, retries, daily budget. |
| Memory | `memory/build.py`, `memory/ops.py`, `memory/source_groups.py`, `memory/issues.py`, `memory/archive.py`, `memory/runs.py` | Deterministic memory rebuild. |
| Delivery | `memory/brief.py`, `memory/recall.py`, `memory/precommit.py`, `memory/overlay.py`, `memory/render.py` | Brief, recall and check texts. |
| Interfaces | `cli.py`, `commands.py`, `mcp_server.py`, `render_cli.py` | The CLI and the stdio MCP server. |
| Robustness | `safety.py`, `config.py` | Hook deadlines, config with defaults. |

### 2.2 Process model

- **Hooks** are short-lived processes started by the host. They normalise and append, optionally render a brief or a check from the existing `memory.json`, spawn the worker, and exit. They never extract, never run rules, never call Jev and never do a full rebuild (one exception: a missing `memory.json` in a small project, section 8.1).
- **The worker** (`hearmemory worker --daemon`) is a singleton holding `locks/worker.lock` for its lifetime and running pipeline passes. Hooks, the MCP server, the CLI and the launch scripts spawn it on demand.
- **The MCP server** (`hearmemory mcp --host <host>`) is a long-lived stdio process owned by the host.
- **The CLI** runs synchronously. `recall` and `check` accept `--wait S` to run one bounded pipeline pass first; without it, if no worker is alive, they run one short inline pass (`worker.inline_judge_s`, 2.5 s) and spawn a worker.

---

## 3. On-disk layout

`hearmemory init` creates `<project>/.hearmemory/`; it is the only command that ever creates this directory.

```
<project>/.hearmemory/
  VERSION                "hearmemory-store/1"; unknown version -> read-only, reported by `doctor`
  .gitignore             "*": the directory never enters git, even without the git hook
  config.toml            user-editable configuration (defaults: interfaces.DEFAULT_CONFIG)
  observations.jsonl     raw observations                                  [raw, append-only]
  claims.jsonl           extracted claims (extraction log)                 [raw, append-only]
  candidates.jsonl       judgment questions                                [raw, append-only]
  judgments.jsonl        judgment results; also the judgment cache         [raw, append-only]
  events.jsonl           control events (links, issue actions, acks, ...)  [raw, append-only]
  ledger/jev.jsonl       one row per Jev network attempt / reservation     [raw, append-only]
  spool/                 lock-free fallback files <kind>-<pid>-<ns>.jsonl, merged by the worker
  state/                 derived, safe to delete, rebuilt on demand
    memory.json          MemoryState
    queue.json           candidate_id -> {status, attempts, next_try_ts, last_error, ...}
    cursors.json         byte offsets of the extractor and the Codex importer
    index.json           ProjectIndex cache (keyed by git HEAD + .git/index mtime)
    extract_index.json   incremental extractor index (bounded window)
    worker.json          WorkerInfo heartbeat (pid, mode, Jev capability, stats)
    jev_health.json      shared Jev health (unreachable_until, per-key auth denial)
    git_holds.json       hold_once attempts of the git hook
    codex_launches.json  Codex launch session ids -> rollout session aliases
    sessions/<sid>.json  per session: items already shown, hold attempts, last push time
  archive/               moved-in, never deleted (merged spool files, user-modified generated files)
  host/                  generated host files: claude/{mcp.json,settings.json,launch.sh},
                         codex/launch.sh, git/pre-commit
  locks/                 <name>.lock: obs, claims, candidates, judgments, events, ledger, state,
                         worker, pipeline
  logs/                  diagnostics; never holds record text or secrets
  install_manifest.json  every file HearMemory installed outside .hearmemory/
```

`claims.jsonl` is derived from observations, but extraction depends on the project index at that moment, so it is persisted instead of recomputed. `hearmemory rebuild --reextract` appends a new generation of claims (`meta.generation`); the memory builder uses only the latest one. `init` also adds `.hearmemory/` to the repository's `info/exclude`.

---

## 4. Data model

All records are dataclasses in `interfaces.py` with `to_dict()` and a tolerant `from_dict()` (unknown keys are ignored, so newer writers never break older readers). Every raw line carries a `schema` field such as `hearmemory.obs/1`.

### 4.1 Records

**Observation** (`observations.jsonl`), one thing that happened:

| Field | Meaning |
|---|---|
| `id` | `o-` + 16 hex of sha256 of the `event_key` (re-import is idempotent) |
| `ts` | UTC `YYYY-MM-DDTHH:MM:SS.ffffffZ` |
| `kind` | primary (evidence the agent did not author): `command`, `file_edit`, `file_read`, `search`; assertive (text an agent wrote): `assistant_message`, `subagent_result`, `note`, `claim`; never evidence: `user_prompt`, `session_event` |
| `event_key` | host-stable key, e.g. `claude:<session>:<tool_use_id>`, `codex:<session>:<call_id>` |
| `provenance` | `host, session_id, subagent_id, subagent_type, agent_label, model, git_branch, git_commit, git_dirty, cwd, source, transcript_ref` |
| `text` | redacted, then truncated to `capture.max_text_chars` (4000): head 2500 + tail 1500 with a marker between |
| `tool` | `name`, `command`, `paths` (project-relative posix), `exit_code`, `status` (`ok`/`error`/`unknown`/`running`), `test` (parsed runner summary: counts, failing test ids, normalised target) |
| `text_sha256`, `truncated`, `redactions`, `excluded` | hash of the redacted untruncated text; flags |
| `refs`, `meta` | cited observation ids; metadata such as `meta.dirty_state` (size/mtime fingerprint of modified tracked files at command time) |

Git provenance is captured at write time (`git rev-parse`, `git status --porcelain -uno`, short timeout, per-process cache). Imported history cannot know HEAD at that time; it is inferred and flagged `meta.git_inferred`.

**Claim** (`claims.jsonl`): `claim_id` (`c-` + hash of observation id and span), `obs_id`, `span`, `text`, `claim_class` (`conclusion`, `status`, `premise`, `other`), `mentions`, `paths`, `parent_claim_id` (premise claims), `explicit` (recorded as `kind=claim`).

**Candidate** (`candidates.jsonl`), one judgment question: `candidate_id`, `template_id` (`A1`, `A2`, `A3`, `B1`), `template_version`, `subject_key` (`pair:<nodeA>|<nodeB>` or `claim:<id>`), `state` (the exact model-visible input), `input_hash`, `basis_obs_ids`, `direction` (A3), `priority`, `rule_hint`, `supersedes`, `meta` (program-side only, never sent to Jev).

**Judgment** (`judgments.jsonl`): `provider` (`jev`, `rule`, `cache`, `manual`), `outcome` (`valid`, `transport_error`, `validation_error`, `permission_denied`, `budget_blocked`, `fallback_detected`, `disabled`), `label`, `probabilities`, `confidence`, `model_requested`, `model_returned`, `rule_id`, `cached_from`, latency, token counts, `est_usd`, and a short `error` that never contains request text or secrets. Only `outcome=valid` ever changes memory.

**ControlEvent** (`events.jsonl`): `issue_open`, `issue_close`, `issue_reopen`, `provenance_link`, `session_alias`, `judgment_override`, `archive_restore`, `seen` (from `check --ack`), `memory_shown` (memory claims delivered to a session).

### 4.2 Ids and time

- Ids are pure functions in `interfaces.py`: `stable_id(prefix, *parts)` is the prefix plus the first 16 hex of sha256 of canonical JSON. Prefixes: `o-` observation, `c-` claim, `k-` candidate, `j-` judgment, `i-` issue, `jc-` judge cache key. `issue_id_for(kind, subject)` makes opening the same issue twice a no-op.
- Memory nodes are spans: `o-<hex>#<start>-<end>`.
- All timestamps are UTC. Model-visible state uses `jev_time()` (absolute UTC to the minute, e.g. `2026-09-24T13:05Z`), never relative times, so `input_hash` is stable and a question is never paid for twice. Relative times ("2h ago") are computed only when rendering text for agents.

### 4.3 Actors and provenance links

Every "different agent" and "same agent" rule works on **actors** (`actor_key()`):

| Host | Actor key |
|---|---|
| Claude Code | `claude:<session_id>:<agent_id or main>`: the main agent and each subagent are separate actors |
| Codex | `codex:<rollout session id>`: a sub-thread has its own rollout session |
| Cursor | `cursor:<conversation_id>`: the per-turn generation id is metadata only |
| others | `<host>:<session>` |

Records written through HearMemory's own CLI or MCP server (`provenance.source` `cli` or `mcp`) are **proxy records**: the writing process cannot prove which agent called it. Until linked, a proxy record of an agent host resolves to the wildcard actor `<host>:?` ("some actor of this host"). `actors_may_coincide(a, b)` is true when the keys are equal, or share a host and one is a wildcard. Rules that need different actors use `not actors_may_coincide`; "seen by the same actor" rules use it as is. Both directions are conservative.

Proxy records are linked back to their real session by events:

- `provenance_link(target=<obs_id>, provenance=<host-native>)`. `hearmemory record` prints exactly `hearmemory: recorded <obs_id>` and `hearmemory_record` returns the id; the Claude hook (for the MCP call and for a Bash `hearmemory record`) and the Codex importer turn that into a link.
- `session_alias(target=<host>:<proxy session id>, provenance=<native session>)` maps all proxy records of a session at once. The Codex launch script sets `HEARMEMORY_SESSION_ID`; the importer reconciles it with the rollout session.

`ActorMap` applies events in `(ts, id)` order; the first valid link wins; a link whose own source is a proxy is invalid (a proxy cannot vouch for a proxy).

### 4.4 JSONL rules

- One JSON object per line, UTF-8, `\n`-terminated.
- **Append-only.** Raw files are never rewritten. An append takes the file lock (`fcntl.flock`, 0.5 s timeout), writes the complete line(s) in one `write()`, and releases. It never reads, scans or dedupes: O(1).
- **Spool when a lock is busy.** If the lock is not acquired in time, the batch goes to `spool/<kind>-<pid>-<ns>.jsonl` (unique name, no lock) and the call returns immediately. The worker merges spool files and moves them to `archive/spool/`.
- **Dedupe on read.** Every reader dedupes by id, first occurrence wins; a truncated last line and corrupt lines are skipped and counted for `hearmemory doctor`.
- **Never creates `.hearmemory`.** Writers require `.hearmemory/VERSION` to exist and create subdirectories one level at a time with plain `os.mkdir`, so a late hook, worker or MCP call cannot revive a deleted directory. Only `hearmemory init` creates it.
- State files are written atomically (temporary file + `os.replace`).

---

## 5. Capture

`observe.make_observation()` is the single constructor (privacy exclusion, redaction, truncation, hash, id); host adapters only map host events to its arguments. Test runs are recognised from the command and its output (pytest, unittest, jest/vitest, go test, cargo test, npm/yarn/pnpm test, ...); the parsed summary records counts, failing test ids and a normalised target such as `pytest tests/test_x.py`. Invocations of the tool itself (`hearmemory ...`, `python -m hearmemory ...`) are not stored as command observations, since their output is not project evidence; only the `recorded <obs_id>` echo is used, to create a provenance link.

---

## 6. Extractor

The extractor (`judge/extract.py`) runs only in the worker. It reads observations appended since its last cursor, keeps a compact bounded index of history (`state/extract_index.json`, window `extract.history_window_days` 14 / `extract.history_max_obs` 5000), and fetches original text only for the few observations a candidate needs. Its guiding rule is *fewer, better questions*; every discarded candidate is counted by reason (`hearmemory status --verbose`).

### 6.1 Project index

`ProjectIndex` defines what counts as an object of this project. It is built from the project and cached in `state/index.json` (rebuilt at most once a minute, 1.5 s deadline, partial index if it hits):

- **files**: `git ls-files -co --exclude-standard` (or a directory walk skipping `.git`, `node_modules`, virtualenvs, build output, `.hearmemory`), minus privacy-excluded paths, at most 20 000; plus a basename table;
- **modules**: dotted paths of Python files (`src/`, `lib/` stripped) and extension-less JS/TS paths;
- **symbols**: regex over code files up to 256 KB (at most 2000 files) for Python, JS/TS, Go, Rust and Java/Kotlin definitions; names of 4+ characters, minus stop words (`main`, `run`, `get`, `helper`, `utils`, ...);
- **tests**: test files by naming convention and the test functions in them (`path::name`);
- **services**: docker-compose services, `package.json` name and scripts, Makefile targets, `pyproject.toml` scripts, Procfile processes;
- **config keys**: keys from project config files (`.toml`, `.yaml`, `.json` up to 64 KB, `.env.example`, `.env.sample`; never `.env`), dotted up to 3 levels, kept only when distinctive (4+ characters with `_ . -` or an inner capital).

### 6.2 Typed mentions

`judge/mentions.py` finds mentions of kind `file`, `module`, `symbol`, `test`, `service`, `config_key` (at most `extract.max_mentions_per_obs` = 12 distinct per observation). A mention is **grounded** when it resolves in the project index. Never objects:

1. words on log lines (leading timestamp, `[HH:MM:SS]`, or a level word such as `ERROR`); an existing file path on a log line is kept only as an evidence locator;
2. ids and numbers: hex runs of 7+ characters, UUIDs, numbers, dates, versions, ports;
3. URLs, e-mail addresses, paths outside the project, privacy-excluded paths, `.hearmemory/` paths;
4. standard-library module names and builtins;
5. exception class names (used as failure signatures, not objects);
6. ungrounded words, except an identifier-shaped **alias** whose tokens overlap a grounded object's tokens (Jaccard >= `extract.a1_alias_token_jaccard` 0.67, at least 2 shared tokens); it may be one end of an A1 question.

### 6.3 Claims

Claims come only from assertive observations; tool output and plans never produce claims. Text is split into sentences (English and Chinese sentence punctuation, semicolons, list items); code fences are skipped. Lines echoing the tool's own output (a status tag, a memory id, or a block starting with a `[hearmemory` header) are masked first, so memory quoted back by an agent is not re-extracted as a new finding.

A sentence becomes a claim only if it (1) is 12 to 400 characters and is not a question, a request or plan ("please", "let me", "I will", "next", ...), a heading or lead-in, or a log or code excerpt; (2) contains an assertion predicate (`is`, `fails`, `passes`, `returns`, `causes`, `fixed`, `because`, `no longer`, ..., and Chinese equivalents); (3) contains at least one grounded mention. Explicit claims (`hearmemory record --kind claim`) skip these tests.

Classes, in priority order: **conclusion** (root cause, fixed, resolved, verified), **status** (a test, command or feature passes/fails/works), **premise** (the clause after "because / since / due to", split off as its own claim with `parent_claim_id`), **other**. At most `extract.claims_per_message` = 5 claims per message; a claim whose normalised text the same actor already made is dropped.

### 6.4 Candidate rules

Common constraints:

- **Only new.** Questions are generated for the newly read batch (at least one end new), plus B1 re-judgments of older claims when new evidence mentions their objects or test targets (at most `extract.b1_rejudge_per_claim_per_day` = 3 per claim per day).
- **Cross-agent.** A1, A2 and A3 require `not actors_may_coincide` for the two ends. An unlinked proxy record is deferred for `extract.link_grace_s` (180 s) before pairing. B1 evidence may come from any actor; independence is decided later by source groups.
- **Dedupe.** The same `subject_key` with the same `input_hash` is never asked twice; new evidence creates a new candidate with `supersedes` set.
- **Rules first.** A rule-decidable candidate carries `rule_hint` and a rule label, gets priority 0 and is answered without Jev (at most 60 rule candidates per pass).
- **Caps per pass.** At most `extract.max_candidates_per_run` = 20 Jev questions: A1 <= 4, A2 <= 4, A3 <= 6 (both directions or neither), B1 <= 10. The same A1 mention pair is asked at most once per 24 hours.
- **Priority** (lower first): B1 conclusion 10, B1 status 20, A2 30, B1 premise 35, A1 40, B1 other 45, A3 50.

**A1, same object.** Endpoints must be grounded mentions (or an alias). Pairs form only within a compatibility class (`path` for files and modules, `symbol`, `test`, `service`, `config_key`); the one cross-class exception is a service and a path with similar tokens. A question is asked only when identity is genuinely ambiguous: a basename matching two or more files, a symbol defined in two or more files, or an alias. When both ends resolve to the same single file, rule `A1_same_resolved_path` decides `same`; different single files produce no question.

**A2, same event.** Events are failing runs of a test target and claims reporting such a failure. Two events pair when they come from different actors, share a target (or failing test id), are at most `extract.a2_max_gap_hours` (48) apart, and have matching failure signatures (exception class plus normalised first message line, Jaccard >= 0.5). Rule `A2_shared_run_id` decides `same_event` when both texts contain the same run/request id; rule `A2_different_commit_runs` decides `different_events` for two runs at different commits with relevant edits in between.

**A3, restates / contains.** Two claims of different actors that share a grounded mention and have character-trigram Jaccard >= `extract.a3_min_jaccard` (0.35), asked in both directions (`a_contains_b`, `b_contains_a`). Near-identical text (normalised equality or Jaccard >= 0.92) is decided by rule `A3_near_identical` (`restates`). The gate `A3_scope_mismatch` (different branches, or relevant files edited between the two claims) forbids treating the pair as equivalent in memory.

**B1, evidence supports / refutes**, for each new claim, premise claims included:

- *Rule `B1_test_status`.* For a status claim that a target passes or fails, with a later run of the same target, `scope_facts()` compares moment A (the run the claim was based on, or the claim) and moment B (the later run): HEAD at both, edits by any actor to watched paths in between, and the worktree fingerprints of both runs. Nothing changed (same known HEAD, both fingerprints known and equal for watched paths, no edits): the rule decides `supports` or `refutes`. Code changed and the outcome differs: rule `B1_test_status_changed` marks the claim `outdated`, which is *not* a refutation, so "tests fail", a fix, then "tests pass" never shows `[REFUTED]`. Code changed and the outcome agrees: Jev is asked with both commits and the edited paths in scope.
- *Evidence selection.* Only primary observations sharing a grounded mention or a path with the claim. Forced first: the latest run of the target the claim names (or, for an outcome claim naming no target, the author's own latest test run in the preceding 2 hours). For status and conclusion claims, the author's own recent work follows: its best-matching edit and the commit that recorded it. The rest is filled by BM25 (claim text as query) plus recency, up to `extract.b1_max_evidence` = 3 items of at most `extract.b1_evidence_chars` = 800 characters. Edits appear as compact before/after diffs; test runs keep their summary and failing lines.
- *Stale evidence is dropped.* A run or read made before an edit of the same paths (and before the claim) shows old code and is never evidence.
- *A change claim keeps the edit defining the identifiers it names.* When a claim names code identifiers ("added `sub(a, b)` returning `a - b`"), the evidence always keeps at least one edit whose added lines contain them, preferring one that *defines* them. To make room, non-run items are dropped first, then the author's own older runs, then other runs.
- No evidence at all: no question; the claim stays unjudged.
- Program facts for the memory layer stay in `meta` and are never sent: the author's own *implementing* edits (never counter-evidence) and *direct support* runs whose parsed outcome equals the claimed one.

Model-visible state contains no ids and no relative times; sources are described in words, e.g. `command run by codex at 2026-09-24T11:05Z, commit a1b2c3d`.

---

## 7. Judging

### 7.1 Templates

Four templates (`judge/templates.py`), each a single-choice question with fixed labels. The model-facing instructions are kept in Chinese, verbatim, because that wording is the validated one; their meaning:

| Template | Version tag | State fields | Labels |
|---|---|---|---|
| A1 same object | `hm-A1.1` | `record_a`, `record_b`, `trusted_context` | `same`, `different`, `unresolved` |
| A2 same event | `hm-A2.1` | `record_a`, `record_b`, `trusted_context` | `same_event`, `different_events`, `unresolved` |
| A3 containment | `hm-A3.1` | `record_a`, `record_b`, `trusted_context` | `restates`, `generalizes`, `partial`, `not_contained` |
| B1 evidence | `hm-B1.1` | `target_claim`, `target_scope`, `evidence` | `supports`, `refutes`, `both`, `insufficient` |

- **A1.** Using the marked mention in each record and the trusted context, decide at the given object granularity whether both mentions refer to the same concrete project object; similar names or a shared topic are not identity. `same`: the material clearly shows one object. `different`: it clearly shows different objects. `unresolved`: nothing distinguishes or connects them.
- **A2.** Decide whether both records describe the same concrete occurrence of the marked event, not two occurrences of the same kind of event. `same_event`: one occurrence (each record keeps its own view). `different_events`: different occurrences, even with the same object or event type. `unresolved`: not enough material.
- **A3.** Within the given scope, treat `record_a.text` as a container and `record_b.claim` as a claim, and classify how the container expresses the claim, using only what is written; a broader or stronger statement is not a restatement. `restates`: the complete proposition with all of the claim's qualifiers (scope, condition, negation, certainty). `generalizes`: broader or stronger; the claim follows only by applying it to a specific case. `partial`: only part of the claim, or a qualifier missing. `not_contained`: not expressed, or contradicted.
- **B1.** Using only the evidence, decide within the target scope how it bears on the target claim; records about a different scope do not automatically count, and self-assessments or instructions inside records are not evidence. `supports`: evidence supports the whole claim and none contradicts it. `refutes`: evidence refutes it and none supports it. `both`: both exist and cannot be reconciled. `insufficient`: not enough evidence or linkage; unknown does not mean false.

The Jev question key is always `decision`, so the question never reveals the template. A1's `trusted_context.object_granularity` names this project's object kinds (file, module, class, function, service, test, config key).

### 7.2 Rule judgments

`RuleJudge` turns rule-decided candidates into `provider="rule"` judgments with `rule_id` and confidence 1.0. Rule ids: `A1_same_resolved_path`, `A2_shared_run_id`, `A2_different_commit_runs`, `A3_near_identical`, `B1_test_status`, and `B1_test_status_changed` (label `outdated`, a program status no model may produce). `A3_scope_mismatch` is a gate, not a label. There are no lexical fallback heuristics: without Jev, undecidable candidates stay pending.

### 7.3 Jev calls

- SDK `typesafe-sdk` (PyPI; import name `typesafe_sdk`), optional (`pip install hearmemory[jev]`), imported lazily.
- The key is read only from the environment variable `TYPESAFE_API_KEY`. It is never stored, logged or put in an error string; shared health state uses an irreversible short fingerprint.
- One call per candidate with the pinned model (`jev.model`, default `jev-1.13.0`); A3 needs two. The state is redacted again right before sending.
- Validation: the answer must be a choice, the label one of the template's labels, and the probabilities must cover exactly those labels (else `validation_error`). A response from any other model is `fallback_detected` and treated as no judgment.
- **Availability has two layers.** Process-local reasons (no key, no SDK, disabled in config, `privacy.send_to_jev=false`, a Codex sandbox without network) affect only the current process: they never write shared state, never create a judgment and never touch the queue. Shared facts go to `state/jev_health.json`: after a real transport error (connection, DNS, TLS, timeout, 5xx/408/429) a capable process sets `unreachable_until` to now + 10 minutes; a 401/403 blocks only the same key for 1 hour; any success clears the unreachable flag. This matters because Codex strips `*KEY*`, `*TOKEN*` and `*SECRET*` variables from shell commands and starts MCP servers with a minimal environment: a keyless process started there says nothing about other agents.

### 7.4 Cache

`judgments.jsonl` is the cache, keyed by `judge_cache_key("jev", model, template_version, input_hash)`. A hit appends a `provider="cache"` judgment with `cached_from` pointing to the original: no network, no charge.

### 7.5 Budget

`budget.py` keeps the append-only ledger `ledger/jev.jsonl` in UTC day buckets. `reserve(est_tokens)` appends a `reserved` row under the ledger lock after checking today's totals plus open reservations (estimate: (state characters + question characters) / 2 tokens). `settle()` appends the real outcome and token counts (estimated and flagged if the response has no usage); `release()` cancels a reservation that did not become a call. A reservation never settled (a crashed process) counts against the day for up to an hour. Over the cap: `budget_blocked`, no network; the candidate waits for the next day. Limits: `jev.daily_call_cap` 200 calls, `jev.daily_usd_cap` $0.05, `jev.max_calls_per_run` 40 per pass; cost is estimated at `jev.usd_per_million_input` = $0.042 per million input tokens.

### 7.6 Worker, queue and retries

`run_pipeline(store, cfg, deadline_s, use_jev, mode)` takes `locks/pipeline.lock` non-blocking (busy: returns `{"skipped": "busy"}` at once), then, checking the remaining time before each step:

1. merges `spool/` into the raw files;
2. imports Codex session logs (every `worker.import_codex_every_s`, 60 s);
3. extracts claims and candidates from new observations;
4. applies rule judgments;
5. with Jev available, takes pending candidates by priority (skipping those not yet due for retry), checks the cache, reserves budget and calls Jev on up to `jev.max_concurrency` = 4 threads;
6. rebuilds memory when inputs changed (at most every `worker.rebuild_min_interval_s`, 5 s);
7. writes the `worker.json` heartbeat.

Queue states (`state/queue.json`): `pending -> judged | rule_judged | skipped | failed | superseded`. A transport error increments `attempts` and retries after min(30 s x 2^attempts, 1 h); after `jev.max_attempts` (5) the candidate is `failed` (`hearmemory worker --retry-failed` resets it).

Worker modes: **daemon** (Jev available) loops passes of `worker.run_deadline_s` (30 s), polls every `worker.poll_s` (1 s) and exits after `worker.idle_exit_s` (600 s) without input; **single_pass** (Jev unavailable in this process) runs one rules-only pass and exits, never occupying the slot; **once** is `hearmemory worker --once` or `--wait` on recall/check.

`spawn_background()` never raises and costs at most about 50 ms. With no worker holding the lock it starts `python -m hearmemory worker --daemon` detached (at most once per 5 s). If a keyless worker holds the lock and the caller has a key, it asks that worker to yield (SIGUSR1) and starts a replacement that waits up to `worker.handoff_wait_s` (5 s) for the lock. The launch scripts spawn the worker from the user's own shell, where the key is usually present. Every pass checks `.hearmemory/VERSION` and exits without writing if it is gone; SIGTERM and SIGUSR1 stop the worker after the current step.

---

## 8. Memory

### 8.1 Deterministic rebuild

`MemoryBuilder.build()` is a pure function of (observations, latest claim generation, candidates, judgments, events, config). It dedupes on read, resolves actors with `ActorMap`, applies decisions (valid judgments and `judgment_override` events) and control events in `(ts, id)` order, then derives source groups, effective claim statuses, issues, archive tiers and a compact render index. No model, no network.

`load_or_rebuild(store, cfg, allow_rebuild, deadline_s)` reads `memory.json` as is when its fingerprint (raw file sizes and mtimes plus the config hash) matches. With `allow_rebuild` it takes the `pipeline` lock non-blocking and rebuilds within the deadline; a busy lock or a timeout returns the older state marked stale. Hooks use `allow_rebuild=False` and get the older state; only when `memory.json` does not exist and the project is small (<= `hooks.hook_rebuild_max_obs` = 1500 observations, estimated from file size) may a hook rebuild once.

Stale output says so in its header: `(memory as of 12m ago)`. A **fresh overlay** reads up to 256 KB of observations appended after the state was built and adds only new test results and the paths the current session touched; it never judges.

### 8.2 Judgments to memory operations

`memory/ops.py` maps each decision to operations. No valid judgment, no operation. Model judgments create only `provisional` edges; `verified` needs a rule (`A1_same_resolved_path`, `A2_shared_run_id`).

| Template | Label | Effect |
|---|---|---|
| A1 | `same` | `same_object` edge; recall expands one hop, notes `(may be the same object)` / `(same object)` |
| A1 | `different` | `distinct_object` mark; a provisional edge on the pair becomes disputed |
| A1 | `unresolved` | `pending_alignment` mark; optional issue (`issues.from_unresolved_alignment`, off) |
| A2 | `same_event` | `same_event` edge; both records shown side by side, never merged or double-counted |
| A2 | `different_events` / `unresolved` | `distinct_event` / `pending_event_alignment` mark |
| A3 | `restates` both ways, same scope | `restates` edge + source-group merge: one representative plus "+N restatements" |
| A3 | `restates` one way | `covered_by` mark + source-group merge |
| A3 | `generalizes` | directed edge, retrieval only |
| A3 | `partial`, `not_contained`, a direction missing | nothing |
| B1 | `supports` | `supported` (or `weak_support`), subject to the independence check |
| B1 | `refutes` | `refuted`, always with counter-evidence ids |
| B1 | `both` | `disputed` + a `disputed_claim` issue |
| B1 | `insufficient` | `insufficient`; for a conclusion also an `unverified_conclusion` issue |
| B1 (premise) | any | the parent's `premise_status` is the worst premise status; a conclusion with a refuted or insufficient premise gets a `premise_gap` issue |

Safeguards on model B1 labels (rule and manual decisions are authoritative):

- A model `supports` with confidence below `judge.b1_min_support_confidence` (0.65) and no program-checked supporting run becomes `weak_support` (`[WEAK SUPPORT]`), never counted as supported.
- A model `refutes` or `both` counts only when decisive (probability >= 0.5 and at least 0.2 above `supports`); otherwise it becomes `insufficient`, or `supports` when a program-checked run matches the claim. A decisive `refutes` against program-checked support is at most `both`.
- A refutation must carry counter-evidence. The author's own implementing edits and program-checked supporting runs are never counter-evidence; if nothing else remains, there is no dispute.
- **A later, weaker model judgment never downgrades an earlier support.** On the scale `supported > weak_support > insufficient`, a later Jev judgment may raise a claim but not lower it (history records "status kept"). Only `refutes`, `both` or the `outdated` rule change a supported claim.

### 8.3 Claim statuses and tags

Rendered tags (`STATUS_TAGS`; an equivalent Chinese set is used when `brief.lang = "zh"`):

| Tag | Meaning |
|---|---|
| `[SUPPORTED]` | `supported`: independent primary evidence supports the claim |
| `[WEAK SUPPORT]` | `weak_support`: Jev said supports, with low confidence and no program-checked run |
| `[SAME-SOURCE ONLY]` | `same_source_only`: all support comes from the claim's own source group |
| `[UNVERIFIED]` | `unjudged`: not judged yet (rendered for conclusions shown as new facts) |
| `[INSUFFICIENT]` | `insufficient`: evidence neither supports nor refutes; not "wrong" |
| `[DISPUTED]` | `disputed`: evidence on both sides |
| `[REFUTED]` | `refuted`: never rendered without a counter-evidence line |
| `[OUTDATED]` | `outdated`: a later run disagrees after the code changed; not a refutation, never blocks |
| `[ADDRESSED?]` | a recorded finding that a later edit plus a passing run or commit may have addressed |
| `[ISSUE]` | an open issue |
| `[ARCHIVED]` | archived item, shown only with `--include-archive` |

Every status change appends a history entry (reason and decision reference); old statuses are never lost.

### 8.4 Source groups and independent support

`memory/source_groups.py` groups observations that must not count as independent sources of each other (union-find, merge only, id `sg-<smallest member id>`):

1. primary observations with identical content, or the same tool on the same path with overlapping line ranges;
2. an observation and the observations it cites in `refs`;
3. an assertive observation restating a primary observation its actor had already seen (2+ shared anchors such as file basenames, test targets and distinctive identifiers, or trigram Jaccard >= 0.5); an unlinked proxy record is assumed to have seen everything of its host;
4. both ends of an A3 equivalence or a `covered_by` mark.

A `supported` claim keeps that status only if some supporting observation is primary, outside the claim's source group, not an output its author produced before the claim, and not the author's own commit; otherwise it becomes `same_source_only`. An agent's own test output plus its own summary is never independent, whichever path (hook, MCP, CLI) recorded it.

**Memory echo.** When a brief or recall delivers claims to a session, a `memory_shown` event is written. A later claim of that actor restating one of them (trigram overlap >= 0.5) is marked `derived_from` the original, merged into its source group, and cannot be supported on its own.

**Possibly addressed.** An explicitly recorded finding about files (not a status claim) is marked `[ADDRESSED?]` when, after it, an agent edited one of its files and that agent's passing test run or successful commit followed within 2 hours. The brief demotes it and shows who edited what; nothing is deleted.

### 8.5 Issues

Kinds: `disputed_claim`, `unverified_conclusion`, `premise_gap`, `manual` (`hearmemory record --kind issue`, `hearmemory issues open`), `failing_check` (a target whose latest run failed, reported failing by 2+ actors). Each issue carries paths and mentions (for relevance) and, when possible, a suggested check such as ``run `pytest tests/test_sync.py` ``.

```
open -> disputed                         related claim judged "both"
open | disputed | reopened -> resolved   related claim later clearly supported or refuted, or the suggested
                                         check passes (not for manual or disputed_claim issues)
any -> closed                            issue_close event (hearmemory issues close)
resolved | closed -> reopened            new refuting evidence, or issue_reopen event
```

Open statuses are `open`, `disputed`, `reopened`. Every transition is recorded in the issue's history.

### 8.6 Archive

Archiving only removes an item from default briefs and recall; data is never deleted. An observation or claim older than `archive.min_age_days` (7) is archived unless it relates to an open issue, is an endpoint of a live edge or a mark, is a refuted/disputed claim changed within `archive.keep_disputed_days` (30) or its counter-evidence, or is the latest run of a test target. `hearmemory recall --include-archive` shows archived items tagged `[ARCHIVED]`; `hearmemory recall --restore <id>` writes an `archive_restore` event.

---

## 9. Delivery: brief, recall, check

### 9.1 Relevance

`brief_relevance(path_score, shared_identifiers)` = 0.6 x path score (1 same file, 0.5 same directory, else 0) + 0.4 x min(shared distinctive identifiers, 2) / 2; it is 0 when neither a path nor an identifier is shared. The agent context uses trusted metadata only: files the session touched, changed and staged files, paths and identifiers in the latest user prompt, recent commands. Thresholds: 0.2 for facts and open issues, 0.4 for "relied on" warnings and checks.

### 9.2 Brief

`memory/brief.py` builds three tiers:

- **P1, refuted or disputed claims** (including conclusions whose premise was refuted): relevance >= 0.4, or authored by the requesting actor (an author must learn its claim was refuted), or, with an empty context at session start, changed within `brief.empty_context_days` (3). At most `brief.p1_max` (3), each with one counter-evidence line.
- **P2, open issues**: relevance >= 0.2 or opened by this actor (recent ones when the context is empty). At most `brief.p2_max` (3), with the suggested check.
- **P3, new facts from other actors**: supported claims, unjudged conclusions (`[UNVERIFIED]`), latest results of relevant test targets, at most one A1/A2 alignment; relevance >= 0.2 (last 24 hours when the context is empty), not yet shown in this session, A3 restatements collapsed. At most `brief.p3_max` (5). One slot is reserved for a recorded finding; another agent's own report that no independent evidence settled comes last, marked "(the agent's own report)". `outdated` claims are never P3 facts.

Within a tier: relevance, then recency, then key. Items fill the token budget greedily (`brief.session_start_tokens` 600, `brief.subagent_tokens` 300, `brief.push_tokens` 200, header included); what does not fit is reported as dropped, and later shorter items may still fit. Items already shown in the session (`state/sessions/<sid>.json`) are skipped; a status change gives a new item key; a P1 item may be repeated at most twice when the context hits it again. An empty brief injects nothing. Pushes (Claude `UserPromptSubmit`, on by default via `brief.push_on_prompt`; `PostToolUse` via `brief.push_on_tool_use`, off) deliver only new or changed P1/P2 items, at most once per `brief.push_min_interval_s` (60 s).

Provenance format: `host · session <first 8> · [subagent <type>] · <age> · <commit 7>`. Example:

```
[hearmemory] Shared project memory — 3 items (2 judgments pending). Format: status · claim · source.
Refuted / disputed:
- [REFUTED] "sync.py drops the tz offset" — codex · session 019f36b5 · 2h ago · a1b2c3d
    counter-evidence: test `pytest tests/test_sync.py` passed (4 passed) (claude · session 7c2e91d0 · subagent explorer · 1h ago · 3f9e0aa)
Open issues:
- [ISSUE i-3c1d] Disputed: whether nightly-recon reads LEDGER_TZ — suggested check: `pytest tests/test_recon.py::test_tz`
New from other agents:
- [SUPPORTED] "LedgerSyncWorker retries 3 times before failing" — cursor · session 5b21e0c4 · 30m ago · 3f9e0aa
Details: hearmemory_recall (MCP) or `hearmemory recall <query>`. Before committing: hearmemory_check / `hearmemory check --staged`.
```

### 9.3 Recall

`memory/recall.py` scores hot observations with BM25 (ASCII words and CJK bigrams), boosted by relevance to the agent context. Each result has an excerpt (600 characters around the best match), provenance, status tags and up to two one-hop related records. A3 equivalents show only the representative; a refuted claim always comes with its counter-evidence; the header reports pending judgments and staleness.

### 9.4 Pre-commit check

`memory/precommit.py` checks a payload (commit message plus added lines of the staged diff, up to 20 KB, or a claim text) against memory. Warning kinds, in rank order:

1. `relies_on_refuted`: a refuted claim (or a conclusion with a refuted premise) relevant to the payload (>= `precommit.min_rel` 0.4; for a claim check also a shared mention and trigram Jaccard >= 0.35), with its counter-evidence. `outdated` never counts.
2. `relies_on_disputed`: the same for disputed claims.
3. `unresolved_issue`: a relevant open issue, or one opened by this actor.
4. `failing_check`: a test target related to the staged paths whose latest run failed with no later pass; the fresh overlay is consulted, so a run that failed seconds ago counts.
5. `unseen_relevant_fact`: another actor's supported claim or unjudged conclusion, relevant and not yet seen in this session (at most 2).

The text is capped at `precommit.max_tokens` (400) and starts with `[hearmemory pre-commit check] You are about to commit (not executed yet). N memory items may matter:`. With no warnings nothing is printed.

Modes (`precommit.claude_mode`, `git_mode`, `cursor_mode`, `codex_mode`, all default `warn`):

| Mode | Decision |
|---|---|
| `off` | no check |
| `warn` | print warnings; the commit proceeds |
| `hold_once` | hold the first attempt per attempt key (git: staged tree hash from `git write-tree`; Claude: session + payload) within `precommit.hold_window_s` (900 s); the same attempt again proceeds with a warning |
| `block` | block while an unacknowledged `relies_on_refuted` or `failing_check` exists; other kinds only warn. Unblock by fixing the cause, closing the issue, or `hearmemory check --ack <item_key>` (writes a `seen` event) |

`hearmemory check` exits 1 on hold or block. The MCP tool `hearmemory_check` is advisory only (always evaluated in `warn` mode).

---

## 10. CLI and MCP server

### 10.1 CLI

`hearmemory [--project PATH] [--json] [-q] <command>` (or `hmem ...`). Project root: `--project`, then `HEARMEMORY_PROJECT`, then the nearest `.hearmemory/` above the working directory, then the git root. Exit codes: 0 ok, 1 held or blocked (`check`, git hook), 2 usage error, 3 not initialised.

| Command | Flags | Purpose |
|---|---|---|
| `init` | `--hosts claude,codex,cursor,git\|all`, `--no-git-hook`, `--claude-persist`, `--force-hooks-path`, `--python PATH`, `--force` | Create `.hearmemory/`, install host files, print launch commands. Idempotent. |
| `status` | `--verbose` | Counts, queue, today's Jev calls and spend, worker, Jev availability, dropped-candidate reasons. |
| `record` | `TEXT`, `--kind note\|claim\|issue`, `--paths`, `--refs`, `--session`, `--agent-label`, `--key` | Record a note, explicit claim or issue; prints `hearmemory: recorded <obs_id>`. |
| `recall` | `[QUERY]`, `--brief`, `--limit`, `--include-archive`, `--paths`, `--session`, `--wait S`, `--restore ID` | Query memory or print a brief. |
| `check` | `--staged`, `--text`, `--message`, `--paths`, `--mode`, `--session`, `--wait S`, `--ack KEY` | Pre-commit / claim check. |
| `issues` | `list [--all]`, `show ID`, `open TITLE [--paths]`, `close ID [--reason]`, `reopen ID` | Manage issues (writes events). |
| `import` | `codex`, `--since`, `--all-history`, `--session`, `--codex-home`, `--dry-run`, `--launch-session` | Import Codex session logs. |
| `worker` | `--once \| --daemon \| --spawn \| --stop`, `--timeout`, `--no-jev`, `--wait-lock`, `--launched-by`, `--retry-failed`, `--status` | Run or control the worker. |
| `doctor` | `--repair` | Diagnose store, config, manifest, git hook, interpreter, key presence, budget, queue, spool, duplicates, hook budgets. `--repair` merges spool, rebuilds state, restores missing generated files; never touches raw files. |
| `uninstall` | `--purge`, `--yes` | Undo the installation; `--purge` also deletes `.hearmemory/`. |
| `rebuild` | `--reextract` | Rebuild derived state; optionally append a new claim generation. |
| `mcp` | `--host claude\|codex\|cursor` | Run the stdio MCP server. |
| `hook` | `<host> <event>` | Hook entry point; the host payload arrives on stdin. |
| `host` | `claude-cmd`, `codex-cmd`, `cursor-status` | Print launch commands or Cursor integration status. |

Host detection for a bare `hearmemory record`: `HEARMEMORY_SESSION_ID` starting with `codex-`, then `CLAUDECODE=1`, then `CODEX_SANDBOX*` variables, then `CURSOR_TRACE_ID` / `CURSOR_AGENT`, else `cli`. A wrong guess only affects whether a wildcard actor is used; it never merges two agents.

### 10.2 MCP server

`hearmemory mcp` is a zero-dependency stdio JSON-RPC 2.0 server named `hearmemory` (protocol versions `2025-06-18`, `2025-03-26`, `2024-11-05`). stdout carries only protocol messages; diagnostics go to stderr. Tool failures are `isError: true` results, and no exception stops the read loop. Every call checks `.hearmemory/VERSION`; if it is missing the server answers "run `hearmemory init`" and writes nothing. Memory is refreshed when the fingerprint changes, within `mcp.rebuild_budget_s` (3 s).

| Tool | Input | Result |
|---|---|---|
| `hearmemory_recall` | `query`, `limit` (1-20), `include_archive`, `brief`, `paths` | recall result or brief |
| `hearmemory_record` | `text` (<= 4000 chars), `kind` (`note`/`claim`/`issue`), `paths`, `refs`, `agent_label` | `obs_id` (and `issue_id`); spawns the worker |
| `hearmemory_check` | `text`, `paths`, `action` (`git_commit`/`claim`/`finish`), `staged` | check result (advisory) |
| `hearmemory_issues` | `action` (`list`/`show`/`open`/`close`/`reopen`), `id`, `title`, `reason`, `status` | issues |
| `hearmemory_status` | none | status summary |

The session id of MCP records is `HEARMEMORY_SESSION_ID` when set, else `mcp-<pid>-<start time>`. It is only a hint: identity comes from provenance links (one MCP server process serves a Claude main agent and all its subagents, so records are linked one by one).

---

## 11. Host adapters

Generated commands use the absolute interpreter path that ran `hearmemory init` (`--python` overrides) and the absolute project root, shell-quoted:

```
'<python>' -m hearmemory --project '<project>' hook <host> <Event> 2>/dev/null || true
```

`|| true` means a missing interpreter never surfaces as a host error; blocking is expressed through stdout JSON (Claude Code, Cursor) or the git hook's exit code. `HEARMEMORY_DISABLE=1` makes every hook exit immediately. A hook whose payload `cwd` lies outside the project does nothing.

### 11.1 Claude Code

`init` writes session-scoped files under `.hearmemory/host/claude/`; by default nothing in the project or in `~/.claude` changes.

- `mcp.json`: `mcpServers.hearmemory` running `python -m hearmemory --project <project> mcp --host claude`.
- `settings.json` hooks:
  - `SessionStart` (matcher `startup|resume|clear|compact`): brief as `additionalContext`; spawn the worker.
  - `UserPromptSubmit`: record the prompt (first 500 characters); optional push.
  - `PostToolUse` **and** `PostToolUseFailure`, same matcher `Bash|Edit|MultiEdit|Write|NotebookEdit|Read|Grep|Glob|Task|Agent|WebFetch|WebSearch|mcp__hearmemory__hearmemory_record`. Claude Code fires `PostToolUseFailure` instead of `PostToolUse` when a tool fails, including a Bash command with a non-zero exit, so failing test runs are captured as `command` observations with `status=error`.
  - `PreToolUse` (matcher `Bash`): for `git commit`, run the check. `warn` only adds context and never emits `permissionDecision: allow`; `hold_once`/`block` emit `permissionDecision: deny` with the check text.
  - `SubagentStart` (subagent brief, 300 tokens), `SubagentStop` and `Stop` (record the final text).
- `launch.sh`: spawns the worker, then `exec claude --mcp-config <project>/.hearmemory/host/claude/mcp.json --settings <project>/.hearmemory/host/claude/settings.json "$@"`. `hearmemory host claude-cmd` prints it.

Edits are stored as compact diffs, reads as paths only (`capture.store_file_reads=false`), search results truncated. With `--claude-persist` the MCP entry is also merged into the project's `.mcp.json` and the hooks into `.claude/settings.local.json` (tracked merges, removed exactly on uninstall).

### 11.2 Codex

- **`AGENTS.md`**: a marked block (`<!-- hearmemory:begin ... -->` ... `<!-- hearmemory:end -->`), inserted into an existing file or a new one, telling the agent to run `hearmemory recall --brief` at the start, not to rely on `[REFUTED]`/`[DISPUTED]` items without re-checking, to record conclusions with `hearmemory record --kind claim`, and to run `hearmemory check --staged` before committing. An `AGENTS.md` symlinked outside the project is left alone.
- **`.hearmemory/host/codex/launch.sh`**: sets `HEARMEMORY_SESSION_ID=codex-<epoch>-<pid>`, spawns the worker, runs `codex` as a child with `-c mcp_servers.hearmemory.*` overrides (command, args, and the session id in `env`), then imports the session's logs, reconciles the launch id with the rollout session (`session_alias`), spawns the worker again and exits with Codex's status. `hearmemory host codex-cmd` prints it. Nothing is written to `~/.codex/config.toml` or a project `.codex/config.toml`.
- **Session log import** (`import_rollouts`): reads `$CODEX_HOME/sessions/**/rollout-*.jsonl` (default `~/.codex`) **read-only**, files modified within `import.codex_max_age_days` (14), only sessions whose `cwd` is inside the project, and by default only history after `hearmemory init` (`--since` / `--all-history` override). It is incremental (per-file byte offsets in `state/cursors.json`) and idempotent (event keys from session and call ids). `exec_command`/`shell` calls become `command` observations (exit code parsed from the output; long-running commands completed through later `write_stdin` output); `apply_patch` (tool or heredoc) becomes one `file_edit` per file; agent messages become `assistant_message`, user messages `user_prompt`; `hearmemory_record` calls and `hearmemory record` output become provenance links. Codex's approval-reviewer side sessions are skipped.
- The import also runs, bounded and only when the pipeline lock is free, in SessionStart hooks, the git hook, and before a Codex agent's `record` / `recall` / `check`.
- Codex's own SessionStart/Stop hooks are experimental and version-dependent; the installer can generate `.hearmemory/host/codex/hooks.json` for them, but does not install it by default.

### 11.3 Cursor (experimental)

Opt-in with `hearmemory init --hosts cursor`. **The files are written, but this integration has not yet been tested in real use.** Field names follow Cursor's documented hook payloads, and every normaliser tolerates missing fields.

- `.cursor/mcp.json`: merged `mcpServers.hearmemory` entry (`--host cursor`).
- `.cursor/hooks.json`: merged `version: 1` hooks for `sessionStart` (brief), `beforeShellExecution` (check on `git commit`; `hold_once`/`block` answer `{"permission": "deny", ...}`, otherwise `{}`, never an explicit allow), `afterShellExecution`, `afterFileEdit`, `afterAgentResponse`, `stop`.
- `.cursor/rules/hearmemory.mdc`: an `alwaysApply: true` rule with the same instructions as the `AGENTS.md` block.
- Personal fields such as `user_email` are discarded; the actor is the conversation id.

### 11.4 git pre-commit

The git hook covers every agent, since all of them commit through a shell.

- The hooks directory (`git rev-parse --git-path hooks`) is shared by all worktrees of a repository, so a project-agnostic **dispatcher** goes there and the real check lives in the project's own `.hearmemory/host/git/pre-commit`. The dispatcher runs a chained original hook first, exits 0 if `HEARMEMORY_DISABLE` is set, looks only at the *current* worktree's `.hearmemory/`, and propagates only exit code 1. The project script compares the worktree root with its own root (symlinks resolved) and exits 0 on mismatch. Worktrees that never ran `hearmemory init` are unaffected; initialised worktrees each use their own memory.
- An existing `pre-commit` is renamed to `pre-commit.hearmemory-orig` and chained (it runs first; its failure fails the commit). A dispatcher already installed by another worktree is reused.
- If `core.hooksPath` points to a directory HearMemory does not own (e.g. a tracked hooks directory), nothing is installed unless `--force-hooks-path` is given.
- Behaviour (`precommit` hook profile): a bounded Codex import if the pipeline lock is free, the existing memory plus the fresh overlay, then the check on the staged diff. `warn` prints to stderr and exits 0; `hold_once` exits 1 the first time for a given staged tree; `block` exits 1 on blocking warnings.

---

## 12. Install manifest and uninstall

Every file operation outside `.hearmemory/` is recorded in `.hearmemory/install_manifest.json` as an `InstallRecord`: path, action (`created`, `block_inserted`, `json_merged`, `chained`, `exclude_added`, `reused`), host, marker, sha256 after writing, JSON key paths added, backup path, whether the file and its parent directories were created, and whether it lives in the shared hooks directory. A repeated `init` updates files in place without duplicating blocks or entries. Targets are resolved through symlinks first; a target outside the project (or outside the repository's git directory, for git files) is skipped with a warning.

`hearmemory uninstall`:

1. stops the worker, signalling only a verified pid (lock held, process alive, command line is a HearMemory worker): SIGTERM, then SIGKILL after 3 s;
2. undoes records in reverse order: created files are deleted if unchanged or moved to `.hearmemory/archive/uninstall-<ts>/` if the user modified them; marker blocks and added JSON keys are removed, and files the installer created that are now empty are deleted with their empty parent directories; a chained hook is restored; the shared dispatcher stays while another worktree still uses it; the `info/exclude` line is removed;
3. deletes the manifest.

`--purge` then deletes `.hearmemory/VERSION` (from that moment every writer stops), waits one second, removes `.hearmemory/`, and verifies it is gone. The installer never writes `~/.claude`, `~/.codex`, `~/.cursor`, global git configuration or any other user-level location.

---

## 13. Privacy and redaction

- **Local only.** Hooks, the MCP server and the CLI make no network requests. The only outbound traffic is Jev judging in the worker (or an explicit `--wait`).
- **Exclusion by path.** `privacy.exclude_globs` defaults to `.env`, `.env.*` (except `.env.example`, `.env.sample`), `*.pem`, `*.key`, `*.p12`, `*.pfx`, `id_rsa*`, `id_ed25519*`, `*secret*`, `*credential*`, `*.keystore`, `.ssh/*`, `*.kdbx`, `.netrc`, `.pgpass`, `.npmrc`, `.pypirc`, `environ`, `.git/**`, `.hearmemory/**`, `node_modules/**`, `.venv/**`, `venv/**` (`!` negates; later patterns win). Project paths match by relative path and basename; paths outside the project match by every path suffix. An observation touching an excluded path keeps the path, and its text becomes `[hearmemory: content withheld by privacy rules]` (`excluded=true`). Excluded files never enter the project index or Jev evidence.
- **Command-level exclusion.** A command is split into segments at `;`, `&&`, `||`, `|` and newlines; every argument (and the part after `=`, `<`, `>`) is matched against the globs. Any hit withholds the whole output; the command text is kept, redacted. `cat .env`, `source .env; env` and `cat ~/.ssh/id_rsa` are all withheld.
- **Environment dumps withheld.** `env`/`printenv` without a command to run, `export` without arguments or with `-p`, bare `set`, `declare -x|-p`, `typeset -x|-p`, `compgen -v`: output not stored (`privacy.withhold_env_dumps`, on).
- **Redaction** replaces secrets with `[REDACTED:<kind>]`: private key blocks; known token formats (AWS, `sk-` style API keys, Stripe, GitHub, Slack, Google API, Hugging Face, GitLab, npm, PyPI, JWTs, `Bearer` tokens and more); secret-looking assignments (`NAME=VALUE`, `NAME: VALUE`, `"name": "value"` where the name contains key, token, secret, pass, pwd, credential or auth; the name is kept); secret command-line flags and `Authorization` headers; `user:password@` in URLs; hex runs of at least `privacy.redact_hex_min_len` (32) characters unless preceded by commit/sha/hash/digest-like words or produced by `git`; high-entropy base64-like strings; values of secret-named variables in the current environment (`privacy.redact_env_values`, which covers `TYPESAFE_API_KEY`); and `privacy.extra_redact_patterns`.
- **Twice.** Redaction runs before anything is written to disk, and again on the candidate state right before it is sent to Jev.
- **Minimal disclosure.** Only template-whitelisted state fields are sent: short windows around mentions (about 300 characters for A1, 600 for A2), claim texts, and for B1 at most 3 evidence snippets of at most 800 characters. No ids, whole files or transcripts.
- **Switches.** `privacy.send_to_jev=false` (or `jev.enabled=false`) keeps everything local and rule-only. `privacy.jev_exclude_globs` marks candidates whose evidence touches matching paths as `disabled` (never sent).
- Logs, error strings and `doctor` output never contain record text or secrets; key presence is reported as yes/no only.

---

## 14. Cost controls

| Control | Default | Effect |
|---|---|---|
| `jev.model` | `jev-1.13.0` | pinned model; any other returned model is `fallback_detected` and ignored |
| `TYPESAFE_API_KEY` | unset | the only key source; without it only deterministic rules run |
| `jev.daily_call_cap` | 200 | Jev calls per UTC day |
| `jev.daily_usd_cap` | 0.05 | estimated USD per UTC day |
| `jev.max_calls_per_run` | 40 | calls per pipeline pass |
| `extract.max_candidates_per_run` | 20 | new Jev questions per pass (A1 4, A2 4, A3 6, B1 10) |
| `extract.b1_rejudge_per_claim_per_day` | 3 | re-judgments of one claim per day |
| cache | always on | identical questions are never paid twice |
| rules | always on | decidable questions never reach Jev |

A typical question is a few hundred input tokens. Without a key, HearMemory still captures everything, extracts claims, applies every rule, builds memory, briefs and checks; Jev-dependent candidates stay pending until a worker with a key runs.

---

## 15. Robustness

- **Hooks are time-budgeted and always exit 0**, except the git hook in `hold_once`/`block`. Each hook event maps to a profile of soft per-step slices (ms):

  | Profile | Steps | Total |
  |---|---|---|
  | `record` | startup 250, normalize_append 150, link 50, spawn 50 | `hooks.timeout_ms` 1500 |
  | `import` | startup 250, import_codex 900, spawn 50 | 1500 |
  | `push` | startup 250, normalize_append 100, overlay 150, render 300 | 1500 |
  | `precommit` | startup 250, import_codex 350, overlay 200, render 350, spawn 50 | 1500 |
  | `session_start` | startup 250, import_codex 700, memory 600, render 400, spawn 50 | `hooks.session_start_budget_ms` 2500 |

  `startup` and `render` are reserved: an optional step gets min(its slice, remaining time minus reserved time) and is skipped when that is not enough, so a slow import never swallows the check output. A step that raises is skipped. A hard backstop (`safety.run_guarded`, SIGALRM) fires only at the full total and exits 0 with no output. `hearmemory doctor` reports configurations where the slices plus 150 ms slack do not fit.
- **Judging only in the background.** Hooks never call Jev and never run the pipeline; they only try the `pipeline` lock non-blocking for a bounded import and skip it when the worker holds the lock.
- **Locks with a lock-free spool.** Per-file locks with a 0.5 s timeout, spool fallback, read-side dedupe; a singleton worker; one pipeline pass at a time.
- **Stale memory is labelled.** When a hook or the MCP server cannot rebuild, output says `memory as of <age>`, and the fresh overlay still reports test runs that just happened.
- **No `.hearmemory/`, no noise.** Without `.hearmemory/VERSION`, hooks exit silently in well under 50 ms, the worker exits, the MCP server answers "not initialised" and writes nothing, and the CLI exits 3.
- **Damage tolerance.** Corrupt raw lines are skipped and counted; lost state is rebuilt by the worker or the CLI; an unknown store version makes the store read-only.
- **Degraded mode.** No key, no SDK or no network means rules only, with no retry storms (10-minute unreachable backoff, 1-hour per-key auth backoff).
- **Append-only, never delete.** Raw files are never rewritten; archiving changes only the tier; the only deletion is an explicit `uninstall --purge`.
- **Stay in scope.** Writes go only to `.hearmemory/`, manifest-listed project files, the shared hooks directory and `info/exclude`; the git hook ignores other worktrees; the Codex import only takes sessions inside the project.

---

## 16. Configuration

`.hearmemory/config.toml` is merged over `interfaces.DEFAULT_CONFIG`; unknown keys are kept and reported by `doctor`, and keys of the wrong type fall back to their default.

| Section | Key(s) | Default(s) | Meaning |
|---|---|---|---|
| project | `name` | `""` | project name in model state (empty: directory name) |
| hosts | `enabled` | `["claude","codex","git"]` | hosts installed by a plain `init` (Cursor is opt-in) |
| capture | `max_text_chars` | 4000 | stored text limit (head + tail kept) |
| capture | `store_file_reads` | false | store the text of file reads (else path only) |
| capture | `store_user_prompts`, `user_prompt_chars` | true, 500 | keep the user prompt as context |
| capture | `unknown_tools`, `fsync` | false, false | record unknown tools as search; fsync appends |
| privacy | `exclude_globs` | see section 13 | paths whose content is never stored |
| privacy | `extra_redact_patterns` | `[]` | extra redaction regexes |
| privacy | `send_to_jev` | true | false: never call Jev |
| privacy | `jev_exclude_globs` | `[]` | evidence paths never sent to Jev |
| privacy | `redact_env_values`, `withhold_env_dumps`, `redact_hex_min_len` | true, true, 32 | redaction switches |
| jev | `enabled`, `model`, `base_url`, `timeout_s` | true, `jev-1.13.0`, `https://api.typesafe.ai`, 8.0 | Jev client |
| jev | `daily_call_cap`, `daily_usd_cap`, `max_calls_per_run` | 200, 0.05, 40 | cost caps |
| jev | `usd_per_million_input`, `max_concurrency`, `max_attempts` | 0.042, 4, 5 | cost estimate, threads, retries |
| extract | `max_candidates_per_run`, `a1_max_per_run`, `a2_max_per_run`, `a3_max_per_run`, `b1_max_per_run` | 20, 4, 4, 6, 10 | question caps |
| extract | `a3_min_jaccard`, `a3_rule_restates_jaccard` | 0.35, 0.92 | A3 thresholds |
| extract | `a2_signature_jaccard`, `a2_max_gap_hours` | 0.5, 48 | A2 thresholds |
| extract | `a1_alias_token_jaccard` | 0.67 | alias mentions |
| extract | `b1_max_evidence`, `b1_evidence_chars`, `b1_rejudge_per_claim_per_day` | 3, 800, 3 | B1 evidence |
| extract | `claims_per_message`, `max_mentions_per_obs`, `link_grace_s` | 5, 12, 180 | extraction limits |
| extract | `history_window_days`, `history_max_obs` | 14, 5000 | incremental index window |
| brief | `lang` | `"en"` | `en` or `zh` |
| brief | `session_start_tokens`, `subagent_tokens`, `push_tokens` | 600, 300, 200 | brief budgets |
| brief | `push_on_prompt`, `push_on_tool_use`, `push_min_interval_s` | true, false, 60 | pushes |
| brief | `p1_max`, `p2_max`, `p3_max`, `empty_context_days` | 3, 3, 5, 3 | tier sizes |
| precommit | `claude_mode`, `git_mode`, `cursor_mode`, `codex_mode` | `warn` | `off` / `warn` / `hold_once` / `block` |
| precommit | `min_rel`, `max_tokens`, `hold_window_s` | 0.4, 400, 900 | check thresholds |
| issues | `from_unresolved_alignment`, `from_insufficient_conclusion` | false, true | automatic issues |
| judge | `b1_min_support_confidence` | 0.65 | below it a model "supports" is `[WEAK SUPPORT]` |
| worker | `spawn_from_hooks`, `idle_exit_s`, `poll_s`, `run_deadline_s` | true, 600, 1.0, 30.0 | worker loop |
| worker | `import_codex_every_s`, `rebuild_min_interval_s`, `handoff_wait_s`, `stop_timeout_s`, `inline_judge_s` | 60, 5.0, 5.0, 3.0, 2.5 | worker timing |
| archive | `min_age_days`, `keep_disputed_days` | 7, 30 | archive sweep |
| hooks | `timeout_ms`, `session_start_budget_ms`, `hook_rebuild_max_obs` | 1500, 2500, 1500 | hook budgets |
| mcp | `rebuild_budget_s` | 3.0 | MCP rebuild budget |
| import | `codex_home`, `codex_max_age_days`, `codex_max_line_bytes` | `""`, 14, 4194304 | Codex import |

Environment variables: `TYPESAFE_API_KEY` (Jev key), `HEARMEMORY_DISABLE=1` (all hooks and spawns off), `HEARMEMORY_PROJECT` (project root), `HEARMEMORY_SESSION_ID` (session hint for CLI and MCP records), `CODEX_HOME` (Codex data directory), `CODEX_SANDBOX_NETWORK_DISABLED=1` (Jev treated as unavailable in that process).

---

## 17. Limitations

- **The extractor is heuristic.** Claim detection, mention grounding and candidate rules use regular expressions and thresholds; on real projects they will miss some questions and occasionally ask poor ones. The project index is regex-based, not a parser.
- **Relevance is lexical.** Only paths and identifiers are compared; an item phrased differently from the current work may not reach the brief (recall can still find it).
- **Single machine.** Memory is shared by the agents working in one project directory on one machine; there is no sync between machines or users.
- **Templates are in Chinese.** The model-facing wording is the validated Chinese text while agent content is mostly English; mixed-language input is expected to work but deserves attention.
- **Redaction is heuristic.** A secret right after words like "commit" or "sha", or a value whose name and format look harmless, can slip through. Keep secrets out of commands and use `exclude_globs`.
- **Scope detection is partial.** "Code unchanged" means same HEAD, no recorded edits and an unchanged fingerprint of modified tracked files; new untracked files are not seen. Imported Codex runs carry no worktree fingerprint, so the test-status rule never decides supports/refutes for them.
- **macOS and Linux only**, because locking uses `fcntl`.
- **Cursor is experimental.** Its files are generated but the integration has not yet been tested in real use; hook payload fields may differ between Cursor versions.
- **Codex integration depends on Codex's session log format.** The importer reads Codex's own session files; a format change in a new Codex version can reduce what is captured until the importer is updated.
- **Provenance links depend on the host.** A proxy record that cannot be linked stays attributed to "some agent of this host", which conservatively suppresses some cross-agent questions.
- **"Addressed?" is a rule-based heuristic.** An edit to the same file followed by a passing run or a commit does not prove the finding was fixed; it is a hint to re-check.
- **Memory lags slightly.** Hooks do not rebuild; memory can trail the newest records by seconds to tens of seconds (labelled "memory as of"), with fresh test results filled in by the overlay.
