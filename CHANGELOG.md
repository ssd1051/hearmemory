# Changelog

## 0.1.0 (2026-09-25)

First public release of HearMemory.

- Shared, project-local memory for coding agents: records commands, test results, file edits and
  agent claims into an append-only store in `.hearmemory/`.
- Host adapters: Claude Code (session-scoped MCP server and hooks, optional persistent setup),
  Codex (MCP server, `AGENTS.md` block, import of Codex session logs), git pre-commit (any agent),
  Cursor (experimental, not yet tested in real use).
- Claims are checked against recorded evidence: deterministic rules, plus the optional Jev judge
  (`pip install "hearmemory[jev] @ git+https://github.com/ssd1051/hearmemory"`, key in `TYPESAFE_API_KEY`).
  Four narrow judgment templates: same object, same event, restatement, evidence supports/refutes.
- Session-start brief with status tags ([SUPPORTED], [WEAK SUPPORT], [SAME-SOURCE ONLY],
  [UNVERIFIED], [INSUFFICIENT], [DISPUTED], [REFUTED], [OUTDATED], [ADDRESSED?]), recall search,
  issues, and a pre-commit check with modes off / warn / hold_once / block. English and Chinese output.
- Privacy: path exclusion, secret redaction before storing and again before sending, only small
  snippets are sent to Jev, `privacy.send_to_jev = false` to turn it off.
- Cost controls: daily call and dollar caps (200 calls, $0.05 per day by default), per-run cap,
  answer cache.
- Exact uninstall from an install manifest; `hearmemory uninstall --purge` also removes the memory.
- Offline test suite with replay tests of real multi-agent runs; macOS and Linux, Python 3.11+.
