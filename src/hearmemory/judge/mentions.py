"""Typed mentions: which words in an observation denote a real object of THIS project.

Only grounded mentions (resolved against the ProjectIndex) count, plus one narrow alias exception. Never
objects: words on log lines (except existing file paths, and those only as evidence locators), ids and
numbers, URLs / e-mails / out-of-project / privacy-excluded / .hearmemory paths, stdlib and builtin names,
exception class names, and ungrounded words. This keeps bogus A1 (same object) questions out.
"""
from __future__ import annotations

import builtins
import re
import sys
from dataclasses import dataclass
from typing import Any, List, Mapping, Optional, Sequence, Set, Tuple

from hearmemory import interfaces as I
from hearmemory.judge import _compat as C
from hearmemory.judge._text import ident_tokens, jaccard
from hearmemory.judge.project_index import SYMBOL_STOPWORDS, ProjectIndex

_EXTS = sorted(["py", "pyi", "js", "jsx", "ts", "tsx", "mjs", "cjs", "go", "rs", "java", "kt", "kts", "rb", "c",
                "h", "cc", "cpp", "hpp", "cs", "swift", "scala", "php", "sh", "sql", "proto", "toml", "yaml", "yml",
                "json", "cfg", "ini", "md", "txt", "csv", "lock", "html", "css", "scss", "vue", "svelte", "env",
                "example", "sample", "xml", "gradle", "mk", "dockerfile"], key=len, reverse=True)
PATH_RE = re.compile(r"(?<![\w./@+-])((?:~|\.\.?)?/?(?:[\w.@+-]+/)*[\w@+-][\w.@+-]*\.(?:%s))(?![\w-])"
                     r"(?:::([A-Za-z_][\w\[\].-]*))?" % "|".join(_EXTS))
DOTTED_RE = re.compile(r"(?<![\w./@$-])([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+)(?![\w/$-])")
IDENT_RE = re.compile(r"(?<![\w.$/@-])([A-Za-z_][A-Za-z0-9_]*)(?![\w$/-])")
URL_RE = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s`'\")\]>]+")
EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
# Rule 1: a log line (leading timestamp, [HH:MM:SS], or an upper-case level word).
LOG_LINE_RE = re.compile(r"^\s*(?:\[?\d{4}-\d\d-\d\d[T ]\d\d:\d\d|\[\d\d:\d\d:\d\d(?:[.,]\d+)?\])"
                         r"|\b(?:DEBUG|INFO|WARN|WARNING|ERROR|TRACE|FATAL|CRITICAL)\b")
HEXISH_RE = re.compile(r"^(?=[0-9a-fA-F]*\d)[0-9a-fA-F]{7,}$")
EXCEPTION_NAME_RE = re.compile(r"(?:Error|Exception|Warning)$")
_CAMEL_RE = re.compile(r"[a-z0-9][A-Z]|[A-Z]{2,}[a-z]")
GENERIC_SERVICE_WORDS = {"test", "tests", "build", "lint", "start", "dev", "clean", "install", "all", "check",
                         "serve", "docs", "release", "deploy", "format", "fmt", "run", "watch", "help", "default",
                         "prepare", "setup", "coverage", "typecheck", "preview", "web", "app", "main", "prod"}
STDLIB_NAMES: Set[str] = set(getattr(sys, "stdlib_module_names", ())) | set(dir(builtins))


@dataclass
class Hit:
    """A mention plus extractor-side flags (not persisted in Claim.mentions)."""
    mention: I.Mention
    a1_ok: bool = True           # False: found on a log line (evidence locator only, never an A1 endpoint)
    alias: bool = False          # ungrounded alias of a grounded object (rule 6)

    @property
    def norm(self) -> str:
        return self.mention.norm


def _log_line_spans(text: str) -> List[Tuple[int, int]]:
    spans = []
    pos = 0
    for line in text.split("\n"):
        if LOG_LINE_RE.search(line):
            spans.append((pos, pos + len(line)))
        pos += len(line) + 1
    return spans


def _in_spans(pos: int, spans: Sequence[Tuple[int, int]]) -> bool:
    return any(a <= pos < b for a, b in spans)


def _mask(text: str, rx: "re.Pattern[str]") -> str:
    return rx.sub(lambda m: " " * len(m.group(0)), text)


def _excluded_id(tok: str) -> bool:
    return bool(HEXISH_RE.match(tok)) or tok.isdigit()


def _is_identifier_form(tok: str, text: str, start: int, end: int) -> bool:
    """camelCase / snake_case / `name(` call form / backticked."""
    if "_" in tok.strip("_") or _CAMEL_RE.search(tok):
        return True
    after = text[end:end + 2]
    if after.startswith("(") or after.startswith(" ("):
        return True
    return start > 0 and text[start - 1] == "`" and text[end:end + 1] == "`"


def _config_context(text: str, start: int, end: int) -> bool:
    before = text[start - 1:start] if start else ""
    after = text[end:end + 1]
    if before in ("`", "'", '"') and after == before:
        return True
    return bool(re.match(r"\s*[=:](?!:)", text[end:end + 4]))


def scan(obs_id: str, text: str, index: ProjectIndex, cfg: Optional[Mapping[str, Any]] = None,
         max_mentions: Optional[int] = None) -> List[Hit]:
    """All grounded (and alias) mentions of one observation text, deduped by norm, capped."""
    text = text or ""
    cap = int(max_mentions or C.cfg_get(cfg, "extract", "max_mentions_per_obs", 12))
    alias_thr = float(C.cfg_get(cfg, "extract", "a1_alias_token_jaccard", 0.67))
    masked = _mask(_mask(text, URL_RE), EMAIL_RE)
    logs = _log_line_spans(masked)
    consumed = bytearray(len(masked))
    hits: List[Hit] = []
    aliases: List[Hit] = []

    def free(a: int, b: int) -> bool:
        return not any(consumed[a:b])

    def take(a: int, b: int) -> None:
        consumed[a:b] = b"\x01" * (b - a)

    def add(kind: str, surface: str, norm: str, span: Tuple[int, int], resolved: Sequence[str], grounded: bool = True,
            a1_ok: bool = True, alias: bool = False) -> None:
        m = I.Mention(kind=kind, surface=surface, norm=norm, obs_id=obs_id, span=[span[0], span[1]],
                      grounded=grounded, resolved=list(resolved))
        (aliases if alias else hits).append(Hit(m, a1_ok=a1_ok, alias=alias))

    # 1. paths (and path::test ids); allowed on log lines but never as A1 endpoints there
    for m in PATH_RE.finditer(masked):
        surface, test_name = m.group(1), m.group(2)
        a, b = m.span(1)
        rel = C.norm_relpath(surface, index.root)
        take(a, m.end())
        if not rel or rel.startswith(I.HEARMEMORY_DIRNAME + "/") or C.is_excluded(rel, cfg):
            continue
        res = index.resolve("file", rel)
        if not res:
            continue
        on_log = _in_spans(a, logs)
        norm = "path:" + res[0] if len(res) == 1 else "base:" + rel.rsplit("/", 1)[-1]
        add("file", surface, norm, (a, b), res, a1_ok=not on_log)
        if test_name and not on_log:
            tres = index.resolve("test", rel + "::" + test_name)
            if tres:
                add("test", surface + "::" + test_name, "test:" + tres[0], (a, m.end()), tres)
    # 2. dotted names: modules or dotted config keys
    for m in DOTTED_RE.finditer(masked):
        a, b = m.span(1)
        if not free(a, b) or _in_spans(a, logs):
            continue
        tok = m.group(1)
        if tok.split(".", 1)[0] in STDLIB_NAMES and not index.resolve("module", tok):
            continue
        res = index.resolve("module", tok)
        if res:
            take(a, b)
            add("module", tok, "path:" + res[0], (a, b), res)
            continue
        if index.resolve("config_key", tok) and _config_context(masked, a, b):
            take(a, b)
            add("config_key", tok, "config:" + tok, (a, b), [tok])
            continue
        # Class.method / module.attr chains: every segment that is a known symbol is a symbol mention
        seg_hits = []
        pos = a
        for seg in tok.split("."):
            if len(seg) >= 4 and seg.lower() not in SYMBOL_STOPWORDS and seg not in STDLIB_NAMES \
                    and not EXCEPTION_NAME_RE.search(seg) and index.resolve("symbol", seg):
                seg_hits.append((seg, pos, pos + len(seg)))
            pos += len(seg) + 1
        if seg_hits:
            take(a, b)
            for seg, sa, sb in seg_hits:
                add("symbol", seg, "symbol:" + seg, (sa, sb), index.resolve("symbol", seg))
            continue
        if len(ident_tokens(tok)) >= 2:
            src = _alias_for(tok, index, alias_thr)
            if src:
                add(_alias_kind(src[0]), tok, "alias:" + tok, (a, b), src, grounded=False, alias=True)
                take(a, b)
    # 3. identifiers: tests, symbols, config keys, aliases
    for m in IDENT_RE.finditer(masked):
        a, b = m.span(1)
        if not free(a, b) or _in_spans(a, logs):
            continue
        tok = m.group(1)
        if len(tok) < 4 or _excluded_id(tok) or tok in STDLIB_NAMES or EXCEPTION_NAME_RE.search(tok):
            continue
        if tok.lower() in SYMBOL_STOPWORDS:
            continue
        if tok.startswith("test_") or re.match(r"^Test[A-Z_]", tok):
            tres = index.resolve("test", tok)
            if tres:
                add("test", tok, "test:" + tres[0] if len(tres) == 1 else "test:*::" + tok, (a, b), tres)
                continue
        ident_form = _is_identifier_form(tok, masked, a, b)
        if ident_form:
            res = index.resolve("symbol", tok)
            if res:
                add("symbol", tok, "symbol:" + tok, (a, b), res)
                continue
        if index.resolve("config_key", tok) and _config_context(masked, a, b):
            add("config_key", tok, "config:" + tok, (a, b), [tok])
            continue
        if ident_form and len(ident_tokens(tok)) >= 2 and tok not in index.services:
            src = _alias_for(tok, index, alias_thr)
            if src:
                add(_alias_kind(src[0]), tok, "alias:" + tok, (a, b), src, grounded=False, alias=True)
    # 4. services: exact word-boundary matches of distinctive service names
    for s in sorted(index.services):
        if s.lower() in GENERIC_SERVICE_WORDS or s.lower() in SYMBOL_STOPWORDS or len(s) < 3:
            continue
        for m in re.finditer(r"(?<![\w./-])%s(?![\w/-])" % re.escape(s), masked):
            a, b = m.span()
            if _in_spans(a, logs) or not free(a, b):
                continue
            take(a, b)
            add("service", s, "service:" + s, (a, b), [s])
    hits.sort(key=lambda h: (h.mention.span[0], h.mention.span[1]))
    aliases.sort(key=lambda h: (h.mention.span[0], h.mention.span[1]))
    # cap on DISTINCT norms (grounded first), but keep every occurrence of a kept norm so each claim
    # sentence sees its own mentions
    keep: List[str] = []
    for h in hits + aliases:
        if h.norm not in keep and len(keep) < cap:
            keep.append(h.norm)
    kept = set(keep)
    out: List[Hit] = []
    seen_span: Set[Tuple[str, int, int]] = set()
    for h in hits + aliases:
        k = (h.norm, h.mention.span[0], h.mention.span[1])
        if h.norm in kept and k not in seen_span:
            seen_span.add(k)
            out.append(h)
    out.sort(key=lambda h: (h.mention.span[0], h.mention.span[1]))
    return out


def _alias_kind(norm: str) -> str:
    return {"symbol": "symbol", "service": "service"}.get(norm.split(":", 1)[0], "module")


def _alias_for(tok: str, index: ProjectIndex, thr: float) -> List[str]:
    """Grounded object norms whose token set is Jaccard >= thr with tok's and shares >= 2 tokens."""
    toks = ident_tokens(tok)
    if len(toks) < 2:
        return []
    counts = {}
    for t in toks:
        for norm in index.token_objects.get(t, ()):
            counts[norm] = counts.get(norm, 0) + 1
    best: List[Tuple[float, str]] = []
    for norm, shared in counts.items():
        if shared < 2:
            continue
        if norm == "symbol:" + tok or norm == "service:" + tok:
            continue
        j = jaccard(toks, index.object_tokens.get(norm, set()))
        if round(j, 2) >= thr:
            best.append((-j, norm))
    best.sort()
    return [n for _, n in best[:3]]


def find_mentions(obs_id: str, text: str, index: ProjectIndex, cfg: Optional[Mapping[str, Any]] = None) -> List[I.Mention]:
    return [h.mention for h in scan(obs_id, text, index, cfg)]


def exception_signature(text: str) -> Optional[str]:
    """A2 failure signature: first 'SomeError: message' (numbers/hex -> #, paths -> basename)."""
    m = re.search(r"\b([A-Z]\w*(?:Error|Exception|Failure))\b(?::[ \t]*([^\n]{0,160}))?", text or "")
    if not m:
        return None
    msg = (m.group(2) or "").strip()
    msg = re.split(r"\s(?:in|at|when|because|after|since|while|from|for|on|so|but|and)\s|[,;，；]", msg, 1)[0]
    msg = msg.strip().rstrip(".;,。； ")
    msg = re.sub(r"(?:[\w.-]+/)+([\w.-]+)", r"\1", msg)
    msg = re.sub(r"\b[0-9a-fA-F]{7,}\b", "#", msg)
    msg = re.sub(r"\d+", "#", msg)
    return (m.group(1) + (": " + msg if msg else ""))[:160]


def signatures_match(a: Optional[str], b: Optional[str], thr: float) -> bool:
    """Same exception class, and (when both carry a message) message char-trigram Jaccard >= thr."""
    from hearmemory.judge._text import normalize_text, trigram_jaccard
    ca, _, ma = (a or "").partition(":")
    cb, _, mb = (b or "").partition(":")
    if ca.strip() != cb.strip():
        return False
    ma, mb = normalize_text(ma), normalize_text(mb)
    if not ma or not mb:
        return True
    return ma == mb or trigram_jaccard(ma, mb) >= thr


RUN_ID_RE = re.compile(r"\b(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|"
                       r"(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{8,})\b")
_HEX_SAFE = re.compile(I.HEX_SAFE_CONTEXT_RE)


def run_ids(text: str, exclude: Sequence[str] = ()) -> List[str]:
    """Run / request ids (>= 8 hex with a digit and a letter, or UUIDs) that are not commit-like (A2)."""
    out = []
    for m in RUN_ID_RE.finditer(text or ""):
        tok = m.group(0)
        line_start = text.rfind("\n", 0, m.start()) + 1
        if _HEX_SAFE.search(text[max(line_start, m.start() - 40):m.start()]):
            continue
        if any(e and (tok.startswith(e[:len(tok)]) or e.startswith(tok)) for e in exclude):
            continue
        out.append(tok)
    return C.uniq(out)
