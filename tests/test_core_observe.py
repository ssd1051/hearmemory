"""observe.py -- make_observation and parse_test_summary."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import init_project, make_provenance  # noqa: E402

from hearmemory.interfaces import DEFAULT_CONFIG, ToolInfo
from hearmemory.observe import make_observation, parse_test_summary


def _cfg():
    return {"capture": dict(DEFAULT_CONFIG["capture"]), "privacy": dict(DEFAULT_CONFIG["privacy"])}


class TestParseTestSummary(unittest.TestCase):
    def test_pytest_summary(self):
        out = "F.\n=== FAILURES ===\nFAILED tests/test_x.py::test_y - AssertionError\n" \
              "=== 1 failed, 3 passed in 0.12s ==="
        summary = parse_test_summary("pytest -q", out)
        self.assertIsNotNone(summary)
        self.assertEqual(summary.runner, "pytest")
        self.assertEqual(summary.failed, 1)
        self.assertEqual(summary.passed, 3)
        self.assertIn("tests/test_x.py::test_y", summary.failed_ids)

    def test_unittest_summary_ok(self):
        out = "...\n----------------------------------------------------------------------\n" \
              "Ran 3 tests in 0.01s\n\nOK"
        summary = parse_test_summary("python -m unittest", out)
        self.assertEqual(summary.runner, "unittest")
        self.assertEqual(summary.passed, 3)
        self.assertEqual(summary.failed, 0)

    def test_unittest_summary_failed(self):
        out = "Ran 5 tests in 0.02s\n\nFAILED (failures=2, errors=1)"
        summary = parse_test_summary("python -m unittest", out)
        self.assertEqual(summary.failed, 2)
        self.assertEqual(summary.errors, 1)
        self.assertEqual(summary.passed, 2)

    def test_jest_summary(self):
        out = "Tests:       2 failed, 1 skipped, 7 passed, 10 total"
        summary = parse_test_summary("jest", out)
        self.assertEqual(summary.runner, "jest")
        self.assertEqual(summary.failed, 2)
        self.assertEqual(summary.passed, 7)

    def test_go_test_summary(self):
        out = "--- FAIL: TestFoo (0.00s)\nFAIL\nFAIL\tpkg/foo\t0.003s"
        summary = parse_test_summary("go test ./...", out)
        self.assertEqual(summary.runner, "go")
        self.assertEqual(summary.failed, 1)
        self.assertIn("TestFoo", summary.failed_ids)

    def test_cargo_summary(self):
        out = "test result: FAILED. 4 passed; 1 failed; 0 ignored"
        summary = parse_test_summary("cargo test", out)
        self.assertEqual(summary.runner, "cargo")
        self.assertEqual(summary.passed, 4)
        self.assertEqual(summary.failed, 1)

    def test_unrecognised_output_returns_none(self):
        self.assertIsNone(parse_test_summary("echo hi", "hi"))
        self.assertIsNone(parse_test_summary(None, ""))


class TestMakeObservation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        init_project(self.root, git=True)
        (self.root / "a.txt").write_text("hi")
        subprocess.run(["git", "add", "a.txt"], cwd=self.root, check=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_basic_note_observation(self):
        obs = make_observation(self.root, _cfg(), "note", "the bug is in sync.py", make_provenance())
        self.assertEqual(obs.kind, "note")
        self.assertFalse(obs.excluded)
        self.assertEqual(obs.text, "the bug is in sync.py")
        self.assertTrue(obs.id.startswith("o-"))

    def test_truncation_head_tail(self):
        cfg = _cfg()
        cfg["capture"]["max_text_chars"] = 4000
        text = "H" * 3000 + "MIDDLE" + "T" * 3000
        obs = make_observation(self.root, cfg, "command", text, make_provenance(),
                               tool=ToolInfo(name="Bash", command="echo x"))
        self.assertTrue(obs.truncated)
        self.assertIn("chars truncated", obs.text)
        self.assertTrue(obs.text.startswith("H" * 100))
        self.assertTrue(obs.text.endswith("T" * 100))

    def test_secret_redacted_before_storage(self):
        obs = make_observation(self.root, _cfg(), "command", "STRIPE_KEY=sk_live_abcdefghijklmnop",
                               make_provenance(), tool=ToolInfo(name="Bash", command="echo $STRIPE_KEY"))
        self.assertNotIn("abcdefghijklmnop", obs.text)
        self.assertGreater(obs.redactions, 0)

    def test_env_dump_command_withheld(self):
        obs = make_observation(self.root, _cfg(), "command", "SECRET=abc\nOTHER=def", make_provenance(),
                               tool=ToolInfo(name="Bash", command="env"))
        self.assertTrue(obs.excluded)
        self.assertEqual(obs.meta.get("withheld"), "env_dump")
        self.assertIn("withheld", obs.text)

    def test_excluded_path_withholds_text(self):
        obs = make_observation(self.root, _cfg(), "command", "SECRET_TOKEN=xyz", make_provenance(),
                               tool=ToolInfo(name="Bash", command="cat .env"))
        self.assertTrue(obs.excluded)
        self.assertEqual(obs.meta.get("withheld"), "path")

    def test_event_key_stable_id(self):
        obs1 = make_observation(self.root, _cfg(), "note", "x", make_provenance(), event_key="fixed-1")
        obs2 = make_observation(self.root, _cfg(), "note", "y", make_provenance(), event_key="fixed-1")
        self.assertEqual(obs1.id, obs2.id)

    def test_command_observation_records_dirty_state(self):
        (self.root / "a.txt").write_text("changed")
        obs = make_observation(self.root, _cfg(), "command", "pytest", make_provenance(),
                               tool=ToolInfo(name="Bash", command="pytest"))
        self.assertIn("dirty_state", obs.meta)
        self.assertIn("a.txt", obs.meta["dirty_state"])

    def test_git_output_hex_not_redacted(self):
        commit_hex = "d" * 40
        obs = make_observation(self.root, _cfg(), "command", commit_hex, make_provenance(),
                               tool=ToolInfo(name="Bash", command="git rev-parse HEAD"))
        self.assertIn(commit_hex, obs.text)


if __name__ == "__main__":
    unittest.main()
