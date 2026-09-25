"""textutil.py -- pure text helpers used by memory/judge recall & relevance."""
from __future__ import annotations

import re
import unittest

from hearmemory.textutil import bm25_scores, char_trigrams, distinctive_identifiers, estimate_tokens, jaccard, now_ts, tokenize


class TestTextutil(unittest.TestCase):
    def test_now_ts_format(self):
        ts = now_ts()
        self.assertRegex(ts, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")

    def test_tokenize_lowercases_words(self):
        self.assertEqual(tokenize("Hello, World! foo_bar"), ["hello", "world", "foo_bar"])

    def test_jaccard_basic(self):
        self.assertEqual(jaccard({1, 2, 3}, {2, 3, 4}), 2 / 4)
        self.assertEqual(jaccard(set(), set()), 0.0)
        self.assertEqual(jaccard({1}, {1}), 1.0)

    def test_char_trigrams(self):
        grams = char_trigrams("abcd")
        self.assertIn("abc", grams)
        self.assertIn("bcd", grams)
        self.assertEqual(char_trigrams(""), set())

    def test_distinctive_identifiers_excludes_stopwords_and_short(self):
        ids = distinctive_identifiers("run the LedgerSyncWorker and setup test_reconcile")
        self.assertIn("ledgersyncworker", ids)
        self.assertIn("test_reconcile", ids)
        self.assertNotIn("run", ids)
        self.assertNotIn("the", ids)

    def test_estimate_tokens_monotonic(self):
        self.assertLess(estimate_tokens("a" * 4), estimate_tokens("a" * 400))
        self.assertEqual(estimate_tokens(""), 0)

    def test_bm25_scores_ranks_relevant_doc_higher(self):
        docs = ["the sync worker fails with KeyError", "completely unrelated text about cooking"]
        scores = bm25_scores("sync worker KeyError", docs)
        self.assertGreater(scores[0], scores[1])

    def test_bm25_scores_empty_query_or_docs(self):
        self.assertEqual(bm25_scores("", ["a", "b"]), [0.0, 0.0])
        self.assertEqual(bm25_scores("x", []), [])


if __name__ == "__main__":
    unittest.main()
