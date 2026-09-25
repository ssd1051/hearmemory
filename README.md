# HearMemory

English | [简体中文](README.zh-CN.md)

**Shared memory for multi-agent coding: parallel subagents and agents handing work to each other (Codex, Claude Code, Cursor) share one project memory, and Jev, a small, fast judge model, checks every agent's claims against real evidence (test runs, diffs, commits), so the next agent knows what it can trust.**

<p align="center">
  <img src="docs/assets/overview.svg" alt="HearMemory overview: agents write to a per-project shared memory, Jev judges claims against evidence, and every session receives a checked brief" width="900">
</p>

## Overview

HearMemory records what coding agents do in a project (commands, test results, file edits, commits) and what they claim ("fixed X", "tests pass"). A background worker asks Jev a few narrow questions about these records: do two mentions refer to the same code object, are two records the same event, does one claim restate another, and does the recorded evidence support or refute a claim. Every new session and every new subagent then receives a short brief with the findings of the other agents and the status of each claim, and every `git commit` is checked against the shared memory.

## Features

- **Multi-agent coordination.** Parallel subagents see each other's results as they arrive. Sequential agents, including agents of different vendors, continue from where the previous one stopped.
- **Evidence-checked claims.** Each claim is labelled `SUPPORTED`, `DISPUTED`, `REFUTED`, `SAME-SOURCE ONLY`, and so on, based on independent test runs, diffs and commits, with full provenance (agent, session, subagent, commit).
- **Fast, low-cost judge.** Jev answers in about 0.5 s per call (benchmark median) and costs well under one cent per working session. Without an API key, HearMemory falls back to deterministic rules.
- **Pre-commit check.** Commits that rely on refuted claims, touch files with open issues, or follow a failing test run are flagged or blocked, depending on configuration.
- **Project-scoped.** Everything lives in the project directory. Global agent configuration is never modified, and `hearmemory uninstall` reverts every change.
- **No core dependencies.** The core uses only the Python standard library. The Jev client (`typesafe-sdk`) is optional.

## Supported hosts

| Host | Integration | Status |
|---|---|---|
| Claude Code | MCP server, hooks (session start, tool results, subagents, pre-commit) | Supported |
| Codex CLI | MCP server, `AGENTS.md` instructions, session log import | Supported |
| Cursor | MCP server, hooks, project rule | Experimental |
| git | pre-commit hook | Supported |
| Any MCP client / shell | MCP tools and CLI | Supported |

Requirements: macOS or Linux, Python 3.11 or later.

## Installation

Install into the virtual environment of the project that will use it:

```sh
pip install "hearmemory[jev] @ git+https://github.com/ssd1051/hearmemory"
```

Omit `[jev]` to install without the Jev client (rules-only mode). The package provides two equivalent commands: `hearmemory` and `hmem`.

## Quick start

```sh
cd your-project
python3 -m venv .venv
.venv/bin/pip install "hearmemory[jev] @ git+https://github.com/ssd1051/hearmemory"
.venv/bin/hearmemory init --hosts claude,codex,git
export TYPESAFE_API_KEY=...                  # optional; enables the Jev judge

sh .hearmemory/host/claude/launch.sh         # start Claude Code with HearMemory
sh .hearmemory/host/codex/launch.sh          # or start Codex with HearMemory
```

Inspect the shared memory at any time:

```sh
hearmemory recall --brief        # the brief a new session receives
hearmemory recall "div"          # search the memory
hearmemory status                # record counts, judge status, today's spend
```

## Host integration

### Claude Code

```sh
sh .hearmemory/host/claude/launch.sh     # equivalent to: claude --mcp-config ... --settings ...
hearmemory host claude-cmd               # print the full command
```

The launcher loads the MCP server and hooks for that session only. To load HearMemory whenever `claude` runs in the project, use `hearmemory init --claude-persist`, which adds entries to the project's `.mcp.json` and `.claude/settings.local.json`.

### Codex CLI

```sh
sh .hearmemory/host/codex/launch.sh      # extra arguments are passed through to codex
hearmemory host codex-cmd                # print the bare `codex -c ...` command
```

`hearmemory init` adds a marked block to `AGENTS.md` that instructs Codex to read the brief at start, record conclusions, and run `hearmemory check` before committing. Codex activity is imported read-only from Codex's session logs (`~/.codex/sessions`) when the launcher exits, periodically by the background worker, and before each HearMemory tool call. Sessions that ended before `hearmemory init` are skipped unless `hearmemory import codex --since <time>` or `--all-history` is used.

### Cursor (experimental)

```sh
hearmemory init --hosts cursor
```

Writes `.cursor/mcp.json`, `.cursor/hooks.json` and `.cursor/rules/hearmemory.mdc`.

### git pre-commit

The `git` host installs a pre-commit hook that checks staged changes against the shared memory. The default mode is `warn`; per-host modes (`off`, `warn`, `hold_once`, `block`) are set as `<host>_mode` under `[precommit]` in `.hearmemory/config.toml`. An existing pre-commit hook is preserved and runs first. `HEARMEMORY_DISABLE=1` bypasses all hooks for one command.

### CLI and MCP tools

| CLI | MCP tool | Purpose |
|---|---|---|
| `hearmemory recall [query] [--brief]` | `hearmemory_recall` | Brief or search |
| `hearmemory record --kind claim\|issue\|note "..."` | `hearmemory_record` | Record a conclusion, issue or note |
| `hearmemory check --staged` | `hearmemory_check` | Pre-commit check |
| `hearmemory issues` | `hearmemory_issues` | Open issues |
| `hearmemory status` | `hearmemory_status` | Store, judge and worker status |

## Example: Codex → Claude Code subagents → Codex

The following is a recorded session on a small Python project. Output is abridged.

**1. Codex adds `sub()` and commits.** HearMemory imports the session: the test run before the commit, the edits, and the commit itself.

**2. Claude Code starts and receives the brief:**

```text
[hearmemory] Shared project memory — 2 items. Format: status · claim · source.
New from other agents:
- test `pytest` passed (12 passed) — codex · session 01a0d476 · 2m ago · 1dcbbe0+dirty → b1d916a
- [SAME-SOURCE ONLY] "Added sub(a, b) returning a - b with a test; python -m pytest -q passes all 12 tests." (the agent's own report) — codex · session 01a0d476 · 2m ago · 1dcbbe0+dirty → b1d916a
```

Claude Code starts two parallel subagents: A re-runs the test suite, B reviews test coverage of `sub()`. Both record their results: A's run is stored as independent evidence for Codex's claim, and B records that negative, zero and floating-point cases are missing.

**3. A second Codex session receives B's finding, adds the missing tests (16 passed) and commits.** The brief for the next session:

```text
[hearmemory] Shared project memory — 3 items. Format: status · claim · source.
New from other agents:
- [SUPPORTED] "python -m pytest -q: 12 passed, 0 failed at b1d916a …" — claude · session 0f684169 · subagent general-purpose · 6m ago · b1d916a
- [ADDRESSED?] "sub test coverage is insufficient. tests/test_calc.py has exactly one sub test, test_sub (sub(5, 3) == 2) …" → codex session 01a0d47d edited tests/test_calc.py, 16 passed, 1696a2b — claude · session 0f684169 · subagent general-purpose · 6m ago · b1d916a
```

Seven Jev calls were made across the three sessions, at a total cost of $0.0004.

### Status tags

| Tag | Meaning |
|---|---|
| `[SUPPORTED]` | Backed by evidence from a source other than the claim's author. |
| `[WEAK SUPPORT]` | Judged as supported with low confidence (below 0.65 by default). |
| `[SAME-SOURCE ONLY]` | Supported only by the author's own output. |
| `[UNVERIFIED]` | Not yet judged, or no comparable evidence. |
| `[INSUFFICIENT]` | Judged; the evidence neither supports nor refutes the claim. |
| `[DISPUTED]` | Conflicting evidence; an issue is opened. |
| `[REFUTED]` | Contradicted by evidence; the counter-evidence is shown. |
| `[OUTDATED]` | A test-result claim superseded by later code changes and results. |
| `[ADDRESSED?]` | A reported problem that a later edit and passing run may have resolved. |
| `[ISSUE …]` | An open issue, recorded manually or opened by a dispute. |

Provenance `abc1234+dirty → def5678` denotes a run on `abc1234` with uncommitted changes that were then committed as `def5678`. Set `brief.lang = "zh"` for Chinese labels.

## Performance

### Judgment quality

67 judgment cases across the twelve judgment templates HearMemory is designed around, labelled blind by a human annotator. Chinese originals, with an English translation of every case.

| Judge | Accuracy (zh) | Accuracy (en) | Dangerous errors¹ (zh / en) |
|---|---|---|---|
| **Jev** (jev-1.13.0) | 93.9% (62/66) | 94.0% (63/67) | 1 / 1 |
| Model A, reasoning LLM | 92.5% (62/67) | 95.5% (64/67) | 1 / 0 |
| Model B, reasoning LLM | 100% (67/67) | 98.5% (66/67) | 0 / 0 |
| Deterministic rules | 58.2% (39/67) | 58.2% (39/67) | 4 / 4 |

¹ Errors with harmful downstream effects, such as merging two different objects or accepting a refuted claim.

### Latency

Same 67 cases, one call at a time, alternating between judges.

| Judge | Median | p90 | p95 | Median output tokens |
|---|---|---|---|---|
| **Jev** | **0.55 s** | **0.66 s** | **0.78 s** | 41 |
| Model A | 4.39 s | 15.4 s | 18.9 s | 234 |
| Model B | 6.17 s | 14.2 s | 16.3 s | 414 |

On paired cases, Jev is a median 7.9× faster than Model A and 11.1× faster than Model B.

### Multi-agent collaboration

Three LLM coding agents working together on scripted multi-step engineering tasks with hidden acceptance tests. Two scenario families, fresh episodes and resumed branches, five seeds each (20 runs per configuration).

| Shared memory configuration | Tasks completed within budget | Runs that used a refuted claim² | Judge cost per episode | Median judge latency |
|---|---|---|---|---|
| None | 9 / 20 | 0 / 5 | — | — |
| Raw shared log, no judge | 11 / 20 | 3 / 5 | — | — |
| Memory + deterministic rules | 12 / 20 | 2 / 5 | — | — |
| **Memory + Jev** | **12 / 20** | **0 / 5** | **$0.01–0.02** | **0.3 s** |
| Memory + reasoning LLM judge (Model A) | 13 / 20 | 0 / 5 | ≈ $0.3 | 6.3 s |

² Branch runs of the scenario family that contains refuted claims.

In the first scenario family, fresh episodes completed 4/5 with Jev versus 1/5 without shared memory. Jev matches the reasoning-LLM judge on task success at roughly 1/20 of the per-call latency and 1/15–1/30 of the judge cost, and episodes finished faster (550 s vs 670 s mean).

### Cost in practice

Measured on real sessions (Codex CLI, then Claude Code with two parallel subagents, then Codex CLI again): 7–26 Jev calls per session sequence, **$0.0004–0.0008 in total**, median 1.35 s per call over a residential connection. Jev pricing at the time of writing: $0.042 per million input tokens, output tokens free. Default caps: 200 calls and $0.05 per day.

## Configuration

Settings live in `.hearmemory/config.toml`.

| Key | Default | Description |
|---|---|---|
| `jev.daily_call_cap` / `jev.daily_usd_cap` | `200` / `0.05` | Daily Jev limits (UTC) |
| `jev.max_calls_per_run` | `40` | Calls per worker pass |
| `judge.b1_min_support_confidence` | `0.65` | Below this, support is shown as `WEAK SUPPORT` |
| `privacy.send_to_jev` | `true` | Set to `false` to disable Jev entirely |
| `privacy.exclude_globs` | `.env`, `*.pem`, `*secret*`, … | Files whose content is never stored |
| `privacy.jev_exclude_globs` | empty | Paths whose content is never sent to Jev |
| `precommit.<host>_mode` | `warn` | `off`, `warn`, `hold_once` or `block` (hosts: `claude`, `codex`, `cursor`, `git`) |
| `brief.lang` | `en` | `en` or `zh` |

The API key is read only from the `TYPESAFE_API_KEY` environment variable and is never written to disk. Each Jev request contains the claim and at most three redacted evidence excerpts of up to 800 characters each; whole files and full logs are never sent. Judgments run in the background worker and never block the agent.

### Files written

| Host | Files |
|---|---|
| all | `.hearmemory/` (memory, configuration, logs; added to `.git/info/exclude`) |
| claude | `.hearmemory/host/claude/{mcp.json,settings.json,launch.sh}`; with `--claude-persist`, also `.mcp.json` and `.claude/settings.local.json` |
| codex | a marked block in `AGENTS.md`, `.hearmemory/host/codex/launch.sh` |
| cursor | `.cursor/mcp.json`, `.cursor/hooks.json`, `.cursor/rules/hearmemory.mdc` |
| git | `.git/hooks/pre-commit` (chains any existing hook), `.hearmemory/host/git/pre-commit` |

Every change is recorded in `.hearmemory/install_manifest.json`.

## Uninstall

```sh
hearmemory uninstall                 # remove hooks and generated files, keep the memory
hearmemory uninstall --purge --yes   # also delete .hearmemory/
pip uninstall hearmemory
```

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
```

The suite (about 630 tests) runs offline and needs no API key. It includes unit tests, end-to-end tests of the CLI, MCP server and hooks in temporary git repositories, and replay tests built from recorded multi-agent sessions (`tests/test_replay_*.py`). Architecture: [docs/DESIGN.md](docs/DESIGN.md). Changes: [CHANGELOG.md](CHANGELOG.md).

## License

MIT. See [LICENSE](LICENSE).
