"""Small deterministic text helpers used by the extractor (tokenize, char trigrams, Jaccard, BM25).

Kept inside judge so candidate ids and thresholds do not move when the core's textutil changes; semantics
are fixed (ASCII lower-case words + CJK bigrams)."""
from __future__ import annotations

import math
import re
from typing import Dict, Iterable, List, Sequence, Set

_WORD_RE = re.compile(r"[A-Za-z0-9_]+|[一-鿿]+")
_CJK_RE = re.compile(r"[一-鿿]")
_NORM_RE = re.compile(r"[^0-9a-z一-鿿]+")
_CAMEL_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z]|\d|\b)|[A-Z]?[a-z]+|[A-Z]+|\d+")


def tokenize(text: str) -> List[str]:
    out: List[str] = []
    for w in _WORD_RE.findall(text or ""):
        if _CJK_RE.match(w):
            if len(w) == 1:
                out.append(w)
            else:
                out.extend(w[i:i + 2] for i in range(len(w) - 1))
        else:
            out.append(w.lower())
    return out


def normalize_text(text: str) -> str:
    return _NORM_RE.sub(" ", (text or "").lower()).strip()


def char_trigrams(text: str) -> Set[str]:
    t = normalize_text(text)
    if len(t) < 3:
        return {t} if t else set()
    return {t[i:i + 3] for i in range(len(t) - 2)}


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 0.0
    return len(sa & sb) / float(len(sa | sb))


def trigram_jaccard(a: str, b: str) -> float:
    return jaccard(char_trigrams(a), char_trigrams(b))


def ident_tokens(name: str) -> Set[str]:
    """Split an identifier / dotted path / service name into lower-case word tokens
    (camelCase, snake_case, kebab-case, dots)."""
    toks: Set[str] = set()
    for part in re.split(r"[_.\-/:\s]+", name or ""):
        for t in _CAMEL_RE.findall(part):
            t = t.lower()
            if t and not t.isdigit():
                toks.add(t)
    return toks


def bm25_scores(query: str, docs: Sequence[str], k1: float = 1.5, b: float = 0.75) -> List[float]:
    q = tokenize(query)
    toks = [tokenize(d) for d in docs]
    n = len(docs)
    if not n or not q:
        return [0.0] * n
    avg = sum(len(t) for t in toks) / float(n) or 1.0
    df: Dict[str, int] = {}
    for t in toks:
        for w in set(t):
            df[w] = df.get(w, 0) + 1
    scores = []
    for t in toks:
        tf: Dict[str, int] = {}
        for w in t:
            tf[w] = tf.get(w, 0) + 1
        s = 0.0
        for w in set(q):
            f = tf.get(w, 0)
            if not f:
                continue
            idf = math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
            s += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * len(t) / avg))
        scores.append(round(s, 6))
    return scores
