# HearMemory

English | [简体中文](README.zh-CN.md)

**Shared memory for multi-agent coding: parallel subagents and agents handing work to each other (Codex, Claude Code, Cursor) share one project memory, and Jev, a small, fast judge model, checks every agent's claims against real evidence (test runs, diffs, commits), so the next agent knows what it can trust.**

HearMemory gives the coding agents that work on one project a shared memory. It helps in two cases.
First, when several subagents work in parallel and cannot see what the others found. Second, when
one agent hands work to another, for example Codex writes the code and Claude Code reviews it
later. HearMemory records what the agents did (commands, test results, file edits) and what they said
("I fixed X", "tests pass"). It then checks each claim against the recorded evidence and gives every
new session a short brief: what other agents found, which claims are backed by evidence, and which
are disputed or wrong.

**Why the name:** what an agent says about its own work is just hearsay until evidence backs it.
HearMemory keeps the memory and tells you which parts are backed, so the next agent knows what it
can rely on.

Status: alpha (0.1.0). Works on macOS and Linux. Python 3.11 or newer.

## Contents

- [2-minute quickstart](#2-minute-quickstart)
- [Install](#install)
- [Use it with your agents](#use-it-with-your-agents)
- [What the brief looks like](#what-the-brief-looks-like)
- [The Jev judge and your API key](#the-jev-judge-and-your-api-key)
- [What HearMemory writes](#what-hearmemory-writes)
- [Uninstall](#uninstall)
- [Limitations](#limitations)
- [How it was tested](#how-it-was-tested)
- [License](#license)

## 2-minute quickstart

```sh
cd your-project                       # must be a git repository for the pre-commit check
python3 -m venv .venv                 # or use the project's existing virtual environment
.venv/bin/pip install "hearmemory[jev] @ git+https://github.com/ssd1051/hearmemory"
.venv/bin/hearmemory init --hosts claude,codex,git

sh .hearmemory/host/claude/launch.sh     # start Claude Code with HearMemory (this session only)
sh .hearmemory/host/codex/launch.sh      # or start Codex with HearMemory
```

Work as usual. In a new session the agent gets a brief automatically (Claude Code) or reads it
first (Codex, told to by `AGENTS.md`). You can look at the memory yourself at any time:

```sh
.venv/bin/hearmemory recall --brief      # the brief a new session would get
.venv/bin/hearmemory recall "div by zero"  # search the memory
.venv/bin/hearmemory status              # counts, judge status, today's spend
```

Without an API key everything still works; only the model judgments are skipped (see
[The Jev judge](#the-jev-judge-and-your-api-key)).

## Install

Install HearMemory into a virtual environment of the project you want to use it in:

```sh
.venv/bin/pip install "hearmemory[jev] @ git+https://github.com/ssd1051/hearmemory"
```

- Install from GitHub for now. A release on PyPI may follow.
- The package installs two commands: `hearmemory` and the short alias `hmem`. They are the same.
- `[jev]` adds `typesafe-sdk` (from PyPI), the client for the Jev judge. Leave it out if you only want
  the rule-based mode: `pip install "hearmemory @ git+https://github.com/ssd1051/hearmemory"`.
- The core has no other dependencies (standard library only).
- `hearmemory init` writes the full path of this Python into the generated hooks and scripts. If you move
  or delete the virtual environment, run `hearmemory init` again.

## Use it with your agents

`hearmemory init --hosts ...` sets up one or more "hosts". The default is `claude,codex,git`. `cursor`
must be asked for by name. Everything is set up inside the project only (see
[What HearMemory writes](#what-hearmemory-writes)).

### Claude Code

```sh
sh .hearmemory/host/claude/launch.sh     # same as: claude --mcp-config ... --settings ... (extra args are passed on)
hearmemory host claude-cmd               # prints the full command if you prefer to run claude yourself
```

The launch script loads HearMemory's MCP server and hooks for this one session. It does not change
your Claude Code settings. The hooks record tool results (commands, edits, subagent results), put a
brief into each new session and each new subagent, and check `git commit` commands before they run.
If you want plain `claude` to always load HearMemory in this project, use `hearmemory init --claude-persist`;
it adds entries to the project's own `.mcp.json` and `.claude/settings.local.json`.

### Codex

```sh
sh .hearmemory/host/codex/launch.sh      # starts codex with HearMemory's MCP server (extra args are passed on)
hearmemory host codex-cmd                # prints the bare `codex -c ...` command instead
```

- `hearmemory init` adds a short, marked block to `AGENTS.md` that tells Codex to read the brief at the
  start, record conclusions, and run `hearmemory check` before committing.
- Codex has no per-tool hooks that work without changing your user config. So HearMemory reads Codex's
  own session logs (`~/.codex/sessions`, read-only) to learn what Codex did. It does this when the
  launch script exits, about every minute from the background worker, and before each HearMemory tool
  call. `hearmemory import codex` does it by hand. Sessions that ended before `hearmemory init` are
  skipped, unless you pass `--since <time>` or `--all-history`.
- Note: when Codex runs in `workspace-write` mode in a folder it has not seen before, Codex itself adds
  a `[projects."<path>"]` trust entry to `~/.codex/config.toml`. HearMemory never writes there. Remove the
  entry by hand if you want.

### Cursor (experimental)

```sh
hearmemory init --hosts cursor           # can be combined: --hosts claude,codex,cursor,git
```

This writes `.cursor/mcp.json`, `.cursor/hooks.json` and `.cursor/rules/hearmemory.mdc` in the project.
**The Cursor adapter is written and unit-tested but has not yet been tested in real use.** Hook field
names may differ between Cursor versions. Reports are welcome.

### git pre-commit (any agent, and you)

The `git` host installs a pre-commit hook. Before each commit it checks the staged change against
the memory: does it rely on a claim that was refuted, is there a related open issue, did a related
test fail last time it ran? By default it only prints a warning and the commit goes ahead. You can
change this per host in `.hearmemory/config.toml` (`[precommit]`, modes `off`, `warn`, `hold_once`,
`block`). An existing pre-commit hook is kept and still runs first. Set `HEARMEMORY_DISABLE=1` to turn
all hooks off for one command.

### By hand, or from any other tool

```sh
hearmemory record --kind claim "src/calc.py div() raises ValueError when b == 0"
hearmemory record --kind issue "tests/test_calc.py has no test for negative numbers"
hearmemory recall "div"                  # search
hearmemory check --staged                # the same check the git hook runs
hearmemory issues                        # open issues
```

Agents with MCP support get the same functions as tools: `hearmemory_recall`, `hearmemory_record`,
`hearmemory_check`, `hearmemory_issues`, `hearmemory_status`.

## What the brief looks like

A new session gets a short brief like this (it is kept to about 600 tokens):

```text
[hearmemory] Shared project memory — 5 items. Format: status · claim · source.
Refuted / disputed:
- [REFUTED] "tests/test_calc.py passes with the new div()" — codex · session 01a0d40a · 2h ago · 8761b49
    counter-evidence: test `pytest tests/test_calc.py` failed (1 failed, 5 passed) (claude · session 11f0b146 · subagent general-purpose · 1h ago · 8761b49)
Open issues:
- [ISSUE i-6f82] tests/test_calc.py has no test for negative numbers
New from other agents:
- test `pytest` passed (6 passed) — codex · session 01a0d411 · 2m ago · 76860d9+dirty → cf15004
- [SUPPORTED] "Added div(a, b); it raises ValueError when b == 0; python -m pytest -q passes (3 passed)." — codex · session 01a0d40a · 10m ago · 8761b49
- [ADDRESSED?] "Tests for div only cover positive numbers." → codex session 01a0d411 edited tests/test_calc.py, 6 passed, cf15004 — claude · session 11f0b146 · subagent general-purpose · 6m ago · 8761b49
Details: hearmemory_recall (MCP) or `hearmemory recall <query>`. Before committing: hearmemory_check / `hearmemory check --staged`.
```

Each line ends with where it came from: tool, session, subagent, how long ago, and the git commit
(`abc1234+dirty → def5678` means "uncommitted changes on abc1234, later committed as def5678").

What the tags mean:

| Tag | Meaning |
|---|---|
| `[SUPPORTED]` | Evidence from outside the claim's own source backs it (for example a test run by another agent). |
| `[WEAK SUPPORT]` | The judge leaned towards "supported", but with low confidence (below 0.65 by default). |
| `[SAME-SOURCE ONLY]` | The only support is the author's own output. Plausible, but nobody checked it independently. |
| `[UNVERIFIED]` | Not judged yet (no key, not processed yet, or nothing to check it against). |
| `[INSUFFICIENT]` | Judged, but the evidence neither backs nor refutes it. This does not mean it is wrong. |
| `[DISPUTED]` | There is evidence both for and against it. An issue is opened. |
| `[REFUTED]` | Evidence contradicts it. The counter-evidence is always shown next to it. |
| `[OUTDATED]` | It described a test result, and the code changed and the result is different now. |
| `[ADDRESSED?]` | A reported problem that another agent may have fixed since (it edited the files and the tests passed). This is a rule-based guess; check it. |
| `[ISSUE ...]` | An open problem: recorded by hand, or opened by a dispute. |
| `[ARCHIVED]` | Old and no longer relevant. Only shown by `hearmemory recall --include-archive`. Nothing is ever deleted. |

"(the agent's own report)" after a claim means the claim was written by the agent itself. Set
`brief.lang = "zh"` in `.hearmemory/config.toml` for Chinese tags and labels.

## The Jev judge and your API key

HearMemory asks a small, fast judge model, **Jev** by TypeSafe, four narrow questions: are two mentions
the same code object, are two records the same event, does one claim restate another, and does the
recorded evidence support or refute a claim. Everything else is decided by plain rules.

- **Key:** set `TYPESAFE_API_KEY` in the shell where you start the agent (the launch scripts start a
  background worker from that shell). The key is only read from the environment. It is never written
  to disk.
- **No key:** HearMemory still works. Deterministic rules decide what they can (for example a test that
  now fails refutes "tests pass"); the other questions wait, and claims show as `[UNVERIFIED]`.
  Nothing breaks and nothing is charged.
- **What is sent to Jev:** only the few short snippets one question needs: the claim sentence and up
  to 3 pieces of evidence of at most 800 characters each (a diff excerpt, a test summary), plus labels
  like "codex session at 2026-09-24T10:00Z, commit a1b2c3d". Never whole files, never full logs.
  Secrets are redacted before anything is stored and again before anything is sent. Files matching
  `privacy.exclude_globs` (for example `.env`, `*.pem`, `*secret*`) are never stored.
  `privacy.jev_exclude_globs` keeps chosen paths away from Jev. `privacy.send_to_jev = false` turns Jev
  off completely.
- **Cost caps (defaults, in `.hearmemory/config.toml` under `[jev]`):** at most 200 calls and $0.05 per day
  (UTC), and 40 calls per worker run. One call is a few hundred tokens. The same question is never paid
  for twice (answers are cached). `hearmemory status` shows today's spend.
- Judging runs in a background worker, never inside the agent's turn, so the agent is never slowed
  down.

## What HearMemory writes

HearMemory only writes inside the project. It never writes to `~/.claude`, `~/.codex`, `~/.cursor`,
your global git config, or any other user-level location.

| Host | Files |
|---|---|
| all | `.hearmemory/` (the memory, config, logs; added to `.git/info/exclude` so git ignores it) |
| claude | `.hearmemory/host/claude/{mcp.json,settings.json,launch.sh}`; with `--claude-persist` also `.mcp.json` and `.claude/settings.local.json` |
| codex | a marked block in `AGENTS.md` (the file is created if missing), `.hearmemory/host/codex/launch.sh` |
| cursor | `.cursor/mcp.json`, `.cursor/hooks.json`, `.cursor/rules/hearmemory.mdc` |
| git | `.git/hooks/pre-commit` (an existing hook is kept and chained), `.hearmemory/host/git/pre-commit` |

Every change is listed in `.hearmemory/install_manifest.json`, so it can be undone exactly. One
exception that is not HearMemory's doing: Codex may add a trust entry to `~/.codex/config.toml` (see
[Codex](#codex)).

## Uninstall

```sh
.venv/bin/hearmemory uninstall                 # remove the hooks and files init added; keep the memory
.venv/bin/hearmemory uninstall --purge --yes   # also delete .hearmemory/ (the memory itself)
.venv/bin/pip uninstall hearmemory
```

`uninstall` stops the background worker first, removes the `AGENTS.md` block, merged JSON entries and
the git hook, and restores a pre-commit hook that was there before. A generated file that you edited
by hand is moved to `.hearmemory/archive/` instead of being deleted.

## Limitations

- **macOS and Linux only.** HearMemory uses `fcntl` file locks. Windows is not supported.
- **Cursor support is experimental** (written, not yet tested in real use).
- **Codex integration reads Codex's session logs.** A change in that log format can break the import
  until HearMemory is updated.
- **`[ADDRESSED?]` is a rule-based guess**, not a judgment. It only means "someone edited the
  mentioned files after the report and the tests passed".
- Finding claims and deciding what to ask the judge is heuristic. Some claims are missed and some
  questions are not useful.
- Relevance is based on shared file paths and identifiers, not on meaning. The brief can miss an item
  that is worded differently; `hearmemory recall` can find it.
- Memory is shared per project folder on one machine. It is not synced between machines.
- Secret redaction is pattern-based. A secret that looks like nothing known (and is not in an
  excluded file) can get through.
- The memory can lag a few seconds behind the latest actions; the brief then says "memory as of ...".

## How it was tested

- An offline test suite of about 630 tests: unit tests for each part, end-to-end tests that run the
  real CLI, MCP server, hooks and git hook in throwaway git repositories, and replay tests. The suite
  needs no network and no API key; a small live Jev test runs only when a key is set.
- Five real multi-agent runs on a small test project: Claude Code desktop with parallel subagents
  (including subagents in git worktrees), Codex CLI, and handoffs such as Codex → Claude Code → Codex.
  Every problem seen in a run was fixed and pinned by a replay test built from the sanitised recording
  of that run (`tests/test_replay_*.py`).

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
```

See [docs/DESIGN.md](docs/DESIGN.md) for the architecture, and [CHANGELOG.md](CHANGELOG.md).

## License

MIT. See [LICENSE](LICENSE).
