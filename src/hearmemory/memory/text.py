"""Pure text helpers used by memory .

Kept local to the memory package so memory is deterministic and independent of the core's internals:
tokenize + BM25 (ASCII lower-case words + CJK bigrams), character trigrams, Jaccard,
distinctive identifiers and the token estimate.
No I/O, no clock except now_ts()."""
from __future__ import annotations

import math
import re
from collections import Counter
from datetime import datetime, timezone
from typing import AbstractSet, Dict, Iterable, List, Optional, Sequence, Tuple

_ASCII = re.compile(r"[A-Za-z0-9_]+")
_CJK_RUN = re.compile(r"[一-鿿]+")
_CJK_CHAR = re.compile(r"[　-〿一-鿿＀-￯]")
_WS = re.compile(r"\s+")


def tokenize(text: str) -> List[str]:
    """ASCII words lower-cased + CJK bigrams (single CJK chars kept)."""
    toks = [t.lower() for t in _ASCII.findall(text or "")]
    for run in _CJK_RUN.findall(text or ""):
        if len(run) == 1:
            toks.append(run)
        else:
            toks.extend(run[i:i + 2] for i in range(len(run) - 1))
    return toks


def bm25_tokens(query_tokens: Sequence[str], docs: Sequence[Tuple[str, Sequence[str]]],
                k1: float = 1.2, b: float = 0.75) -> Dict[str, float]:
    """BM25 over pre-tokenised docs [(doc_id, tokens)]."""
    n = len(docs)
    if n == 0:
        return {}
    avg = (sum(len(t) for _, t in docs) / n) or 1.0
    df: Counter = Counter()
    for _, t in docs:
        df.update(set(t))
    q = list(dict.fromkeys(query_tokens))
    out: Dict[str, float] = {}
    for doc_id, t in docs:
        if not q:
            out[doc_id] = 0.0
            continue
        tf = Counter(t)
        s = 0.0
        dl = len(t)
        for term in q:
            f = tf.get(term)
            if f:
                idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
                s += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * dl / avg))
        out[doc_id] = s
    return out


def bm25_scores(query: str, docs: Sequence[Tuple[str, str]], k1: float = 1.2, b: float = 0.75) -> Dict[str, float]:
    return bm25_tokens(tokenize(query), [(d, tokenize(t)) for d, t in docs], k1, b)


def normalize_text(s: str) -> str:
    return _WS.sub(" ", (s or "").strip().lower())


def char_trigrams(s: str, max_chars: int = 1200) -> frozenset:
    s = f"  {normalize_text(s[: max_chars * 2] if s else '')[:max_chars]} "
    return frozenset([s[i:i + 3] for i in range(len(s) - 2)])


def jaccard(a: Iterable, b: Iterable) -> float:
    if not isinstance(a, (set, frozenset)):
        a = set(a)
    if not isinstance(b, (set, frozenset)):
        b = set(b)
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


_IDENT_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.:-]*[A-Za-z0-9_]|\d{3,}")
_IDENT_MARK_CHARS = frozenset("0123456789_.:-")


def distinctive_identifiers(text: str, exclude: AbstractSet[str] = frozenset(), limit: Optional[int] = None) -> frozenset:
    """Lower-cased tokens of >= 3 chars that contain a digit or one of _ . : -, are ALL CAPS,
    or are camelCase; plus bare numbers of >= 3 digits. Plain words never count."""
    out = set()
    marks = _IDENT_MARK_CHARS
    for tok in _IDENT_TOKEN_RE.findall(text or ""):
        if len(tok) < 3:
            continue
        tok = tok.rstrip(".:-")
        if len(tok) < 3:
            continue
        low = tok.lower()
        if not marks.isdisjoint(tok) or (tok.isupper() and tok.isalpha()) or tok[1:] != low[1:]:
            if low not in exclude:
                out.add(low)
                if limit is not None and len(out) >= limit:
                    break
    return frozenset(out)


def _estimate_tokens_local(text: str) -> int:
    if not text:
        return 0
    cjk = len(_CJK_CHAR.findall(text))
    other = len(text) - cjk
    return int(math.ceil(cjk / 1.5) + math.ceil(other / 4))


_CORE_ESTIMATE = None   # resolved once: core textutil.estimate_tokens, else the local formula


def estimate_tokens(text: str) -> int:
    """ASCII ~4 chars/token, Chinese ~1.5 chars/token. Uses core textutil when present."""
    global _CORE_ESTIMATE
    if _CORE_ESTIMATE is None:
        try:
            from hearmemory.textutil import estimate_tokens as core_estimate  # noqa: WPS433
            core_estimate("probe")
            _CORE_ESTIMATE = core_estimate
        except Exception:
            _CORE_ESTIMATE = _estimate_tokens_local
    try:
        return int(_CORE_ESTIMATE(text))
    except Exception:
        return _estimate_tokens_local(text)


TS_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"


def now_ts() -> str:
    return datetime.now(timezone.utc).strftime(TS_FMT)


def ts_seconds(ts: Optional[str]) -> Optional[float]:
    """UTC ISO timestamp -> epoch seconds (tolerant: with/without fraction, 'Z' or offset)."""
    if not ts:
        return None
    s = str(ts).strip()
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def clip(text: str, n: int) -> str:
    text = _WS.sub(" ", (text or "").strip())
    return text if len(text) <= n else text[: max(0, n - 1)].rstrip() + "…"
