"""hearmemory.textutil -- pure text helpers shared by core, memory and judge.

No file/network access. Deterministic. Kept dependency-free (stdlib only).
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Mapping, Optional, Sequence

_WORD_RE = re.compile(r"[A-Za-z0-9_]+")
_CAMEL_SPLIT_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|[_\-.]+")
_STOPWORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being", "to", "of", "and", "or",
    "in", "on", "for", "with", "this", "that", "it", "as", "by", "at", "from", "but", "not", "no",
    "do", "does", "did", "has", "have", "had", "will", "would", "can", "could", "should", "may",
    "main", "test", "init", "run", "get", "set", "setup", "handler", "helper", "utils", "util",
})


def now_ts() -> str:
    """UTC ISO-8601 with microseconds, e.g. '2026-09-24T13:05:12.345678Z'."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def normalize_ts(ts: object) -> "str | None":
    """Any ISO-8601 timestamp (e.g. a Codex rollout's '2026-09-19T08:01:02.345Z' or one with an
    offset) -> the canonical hearmemory format above, in UTC. None when missing/unparsable. A time in
    the future (clock skew) is clamped to now so it can never outrank genuinely fresh events."""
    if not ts or not isinstance(ts, str):
        return None
    s = ts.strip()
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    # fromisoformat (3.8+) wants 0, 3 or 6 fraction digits: pad/cut whatever Codex wrote.
    m = re.match(r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})(?:\.(\d+))?(.*)$", s)
    if not m:
        return None
    frac = (m.group(2) or "0")[:6].ljust(6, "0")
    try:
        dt = datetime.fromisoformat(f"{m.group(1)}.{frac}{m.group(3)}")
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    now = datetime.now(timezone.utc)
    if dt > now:
        dt = now
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def tokenize(text: str) -> List[str]:
    """Lowercase word tokens (alnum + underscore runs)."""
    if not text:
        return []
    return [t.lower() for t in _WORD_RE.findall(text)]


def _split_identifier(token: str) -> List[str]:
    parts = [p for p in _CAMEL_SPLIT_RE.split(token) if p]
    out: List[str] = []
    for p in parts:
        out.extend(x for x in re.split(r"(?<=[a-z0-9])(?=[A-Z])", p) if x)
    return [p.lower() for p in (out or parts)]


def distinctive_identifiers(text: str, min_len: int = 4) -> List[str]:
    """Identifier-looking tokens (camelCase / snake_case / dotted), excluding stopwords/short tokens.
    Order-preserving, de-duplicated."""
    if not text:
        return []
    seen: Dict[str, None] = {}
    for raw in re.findall(r"[A-Za-z_][A-Za-z0-9_.]*", text):
        low = raw.lower()
        if len(raw) < min_len or low in _STOPWORDS:
            continue
        if low in seen:
            continue
        seen[low] = None
    return list(seen.keys())


def char_trigrams(text: str) -> set:
    """Character 3-grams of the normalised (lowercased, whitespace-collapsed) text."""
    if not text:
        return set()
    norm = re.sub(r"\s+", " ", text.strip().lower())
    if len(norm) < 3:
        return {norm} if norm else set()
    return {norm[i:i + 3] for i in range(len(norm) - 2)}


def jaccard(a: Iterable, b: Iterable) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb)
    return inter / union if union else 0.0


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token), same rule the Jev budget estimator uses."""
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def bm25_scores(query: str, docs: Sequence[str], k1: float = 1.5, b: float = 0.75) -> List[float]:
    """Minimal BM25 over a small in-memory document list (evidence ranking, recall)."""
    q_terms = tokenize(query)
    if not docs or not q_terms:
        return [0.0 for _ in docs]
    doc_tokens = [tokenize(d) for d in docs]
    doc_lens = [len(t) for t in doc_tokens]
    avgdl = (sum(doc_lens) / len(doc_lens)) if doc_lens else 0.0
    n = len(docs)
    df: Dict[str, int] = {}
    for toks in doc_tokens:
        for term in set(toks):
            df[term] = df.get(term, 0) + 1
    scores = []
    for toks, dl in zip(doc_tokens, doc_lens):
        tf: Dict[str, int] = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        score = 0.0
        for term in q_terms:
            f = tf.get(term, 0)
            if f == 0:
                continue
            n_q = df.get(term, 0)
            idf = math.log(1 + (n - n_q + 0.5) / (n_q + 0.5))
            denom = f + k1 * (1 - b + b * (dl / avgdl if avgdl else 0.0))
            score += idf * (f * (k1 + 1)) / (denom if denom else 1.0)
        scores.append(score)
    return scores


# ---------------------------------------------------------------------------------------------------------
# Edit / patch rendering. A file edit shown to Jev or to an agent must keep BOTH sides of a
# change: the old head-truncated `apply_patch <<'PATCH' ... - re…` showed only the removed line, which read
# as counter-evidence to the very claim the edit implemented.
_DIFF_HEADER_RE = re.compile(r"^(?:---|\+\+\+|diff --git|index [0-9a-f]|\*\*\* (?:Begin|End) Patch|\*\*\* (?:Add|Update|Delete) File:)")


def _diff_blocks(text: str) -> List[Dict[str, object]]:
    """Hunks of a unified diff / Codex patch section: {header, blocks: [{ctx, minus: [...], plus: [...]}]}."""
    hunks: List[Dict[str, object]] = []
    cur: Dict[str, object] = {"header": "", "blocks": []}
    last_ctx = ""
    block: Dict[str, object] = {}

    def close_block() -> None:
        nonlocal block
        if block and (block["minus"] or block["plus"]):
            cur["blocks"].append(block)          # type: ignore[union-attr]
        block = {}

    for raw in (text or "").splitlines():
        if _DIFF_HEADER_RE.match(raw):
            continue
        if raw.startswith("@@"):
            close_block()
            if cur["blocks"] or cur["header"]:
                hunks.append(cur)
            cur = {"header": raw.strip("@ ").strip(), "blocks": []}
            last_ctx = ""
            continue
        if raw.startswith("-") or raw.startswith("+"):
            if not block:
                block = {"ctx": last_ctx, "minus": [], "plus": []}
            (block["minus"] if raw.startswith("-") else block["plus"]).append(raw[1:])   # type: ignore[union-attr]
            continue
        close_block()
        if raw.strip():
            last_ctx = raw[1:] if raw.startswith(" ") else raw
    close_block()
    if cur["blocks"] or cur["header"]:
        hunks.append(cur)
    return [h for h in hunks if h["blocks"]]


# a `git commit` command (same shape as host/git.py's detector) and the "[<branch> <sha>] <msg>"
# line git prints for the commit it created.
GIT_COMMIT_CMD_RE = re.compile(r"(^|[;&|]\s*)git(\s+-[^\s]+(\s+[^\s-][^\s]*)?)*\s+commit(?![\w-])")
_NEW_COMMIT_RE = re.compile(r"^\[[^\]\n]*?\s([0-9a-f]{7,40})\]\s", re.MULTILINE)


def new_commit_sha(output: str) -> Optional[str]:
    """The sha of the commit a `git commit` output reports it created ("[main 8761b49] add div"), else None."""
    m = _NEW_COMMIT_RE.search(output or "")
    return m.group(1) if m else None


def is_amend_commit(command: str) -> bool:
    """`git commit --amend` replaces the commit HEAD pointed at (the new sha is in its output)."""
    return bool(GIT_COMMIT_CMD_RE.search(command or "")) and bool(re.search(r"(?:^|\s)--amend\b", command or ""))


# a command that CONTAINS a hearmemory call (`hearmemory record ... && git add src && hearmemory check --staged && git
# commit ...`) is still the agent's command -- its git commit / test output must be seen. Only a command made of
# hearmemory calls alone is hearmemory's own business; inside a mixed one, hearmemory's own output lines are stripped.
_SHELL_SPLIT_RE = re.compile(r"&&|\|\||[;|\n]")
_HEARMEMORY_SEG_RE = re.compile(r"^(?:\w+=\S*\s+)*(?:\S*/)?(?:hearmemory|hmem|python3?\s+-m\s+hearmemory)(?:\s|$)")
_NEUTRAL_SEG_RE = re.compile(r"^(?:cd|export|true|:)(?:\s|$)")
_HEARMEMORY_OUT_LINE_RE = re.compile(r"^\s*hearmemory(?: [a-z]+)?: ")          # "hearmemory: recorded o-..", "hearmemory check: allow"
_HEARMEMORY_OUT_HEADER_RE = re.compile(r"^[\s>*_`#-]*\[hearmemory[\] ]")          # "[hearmemory] Shared project memory ..."
_HEARMEMORY_OUT_FOOTERS = ("Details: hearmemory_recall", "详情：hearmemory_recall", "(Warning only", "（只是提醒", "Run the same command",
                      "再执行一次同样的命令", "Resolve the items or run", "请先处理这些条目")


def _shell_segments(command: str) -> List[str]:
    """`a && b; c | d` -> ["a", "b", "c", "d"] (quoted operators are not split on)."""
    masked = re.sub(r"'[^']*'|\"(?:\\.|[^\"\\])*\"", lambda m: "x" * len(m.group(0)), command or "")
    out, start = [], 0
    for m in _SHELL_SPLIT_RE.finditer(masked):
        out.append(command[start:m.start()].strip())
        start = m.end()
    out.append(command[start:].strip())
    return [s for s in out if s]


def hearmemory_only_command(command: str) -> bool:
    """True when every part of the shell command is a hearmemory call (or a neutral `cd` / `export`)."""
    segs = [s for s in _shell_segments(command) if not _NEUTRAL_SEG_RE.match(s)]
    return bool(segs) and all(_HEARMEMORY_SEG_RE.match(s) for s in segs)


def strip_hearmemory_output(output: str) -> str:
    """`output` without hearmemory's own lines: "hearmemory: recorded o-..", "hearmemory check: allow", and a block that starts
    with hearmemory's header ("[hearmemory] ...", "[hearmemory pre-commit check] ...") up to its footer or the next blank line."""
    if not output or "hearmemory" not in output:
        return output or ""
    out: List[str] = []
    in_block = False
    for line in output.splitlines(keepends=True):
        body = line.strip()
        if in_block:
            if not body:
                in_block = False
            elif body.startswith(_HEARMEMORY_OUT_FOOTERS):
                in_block = False
                continue
            else:
                continue
        if _HEARMEMORY_OUT_HEADER_RE.match(line):
            in_block = True
            continue
        if _HEARMEMORY_OUT_LINE_RE.match(line):
            continue
        out.append(line)
    return "".join(out)


_APPLY_PATCH_CMD_RE = re.compile(r"^\s*(?:\$\s+)?(?:cd\s+\S+\s*&&\s*)?apply_patch\b")
_PATCH_FILE_LINE_RE = re.compile(r"^\*\*\*\s+(?:Add|Update|Delete) File:\s+(.+?)\s*$", re.MULTILINE)


def apply_patch_paths(command: str) -> List[str]:
    """Files a Codex `apply_patch <<'PATCH' ...` command edits ([] when it is not such a command). Records
    imported by older versions stored these as commands; readers treat them as the file edits they are."""
    if not command or not _APPLY_PATCH_CMD_RE.match(command):
        return []
    out: List[str] = []
    for m in _PATCH_FILE_LINE_RE.finditer(command):
        p = m.group(1).strip()
        if p and p not in out:
            out.append(p)
    return out


def diff_counts(text: str) -> Dict[str, int]:
    hunks = _diff_blocks(text)
    minus = sum(len(b["minus"]) for h in hunks for b in h["blocks"])   # type: ignore[union-attr]
    plus = sum(len(b["plus"]) for h in hunks for b in h["blocks"])     # type: ignore[union-attr]
    return {"hunks": len(hunks), "removed": minus, "added": plus}


def _clip_line(s: str, n: int) -> str:
    s = s.rstrip()
    return s if len(s) <= n else s[: max(0, n - 1)] + "…"


def compact_diff(text: str, max_chars: int = 800, focus: Iterable[str] = (), path: str = "") -> str:
    """A before -> after summary of an edit: hunk headers, one context line, and BOTH the removed (-)
    and added (+) lines of each change. Hunks that mention a `focus` term come first; when it does not
    fit, whole hunks are dropped (and counted), and a long change keeps its first lines of EACH side --
    never a head-cut that loses the + side. Text without any -/+ lines is returned clipped as is."""
    hunks = _diff_blocks(text)
    if not hunks:
        t = (text or "").strip()
        return t if len(t) <= max_chars else t[: max(0, max_chars - 1)] + "…"
    focus_l = [f.lower() for f in focus if f]

    def score(h: Dict[str, object]) -> int:
        blob = (str(h["header"]) + "\n" + "\n".join(
            str(b["ctx"]) + "\n" + "\n".join(b["minus"]) + "\n" + "\n".join(b["plus"])   # type: ignore[union-attr]
            for b in h["blocks"])).lower()                                                  # type: ignore[union-attr]
        return sum(1 for f in focus_l if f in blob)

    order = sorted(range(len(hunks)), key=lambda i: (-score(hunks[i]), i))
    head = f"edit {path} (before -> after):" if path else "edit (before -> after):"

    def render(h: Dict[str, object], per_side: int) -> List[str]:
        out = ["@@ " + _clip_line(str(h["header"]), 120)] if h["header"] else ["@@"]
        for b in h["blocks"]:                                                           # type: ignore[union-attr]
            if b["ctx"]:
                out.append("  " + _clip_line(str(b["ctx"]), 160))
            minus, plus = list(b["minus"]), list(b["plus"])
            for s in minus[:per_side]:
                out.append("- " + _clip_line(s, 160))
            if len(minus) > per_side:
                out.append(f"  … ({len(minus) - per_side} more removed line(s))")
            for s in plus[:per_side]:
                out.append("+ " + _clip_line(s, 160))
            if len(plus) > per_side:
                out.append(f"  … ({len(plus) - per_side} more added line(s))")
        return out

    for per_side in (40, 12, 4, 2, 1):
        lines = [head]
        used = len(head) + 1
        kept = 0
        for i in order:
            chunk = render(hunks[i], per_side)
            size = sum(len(x) + 1 for x in chunk)
            if used + size > max_chars and kept:
                continue
            lines += chunk
            used += size
            kept += 1
        if kept < len(hunks):
            lines.append(f"… ({len(hunks) - kept} more hunk(s) not shown)")
        out = "\n".join(lines)
        if len(out) <= max_chars or per_side == 1:
            return out if len(out) <= max_chars else out[: max(0, max_chars - 1)] + "…"
    return out


def edit_summary(text: str, path: str = "", max_chars: int = 160) -> str:
    """One line: `src/calc.py: "return a - b" -> "return a + b"` (+N more changes)."""
    hunks = _diff_blocks(text)
    blocks = [b for h in hunks for b in h["blocks"]]                                     # type: ignore[union-attr]
    prefix = f"{path}: " if path else ""
    if not blocks:
        return (prefix + "edited").strip()
    b = blocks[0]
    minus = [s.strip() for s in b["minus"] if s.strip()]                                # type: ignore[union-attr]
    plus = [s.strip() for s in b["plus"] if s.strip()]                                  # type: ignore[union-attr]
    more = len(blocks) - 1
    tail = f" (+{more} more change(s))" if more else ""
    budget = max(20, (max_chars - len(prefix) - len(tail) - 8) // 2)
    if minus and plus:
        body = f'"{_clip_line(minus[0], budget)}" -> "{_clip_line(plus[0], budget)}"'
    elif plus:
        body = f'added {len(plus)} line(s): "{_clip_line(plus[0], budget * 2)}"'
    elif minus:
        body = f'removed {len(minus)} line(s): "{_clip_line(minus[0], budget * 2)}"'
    else:
        body = "edited"
    return prefix + body + tail
