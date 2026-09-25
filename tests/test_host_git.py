"""git pre-commit adapter tests: dispatcher install (created/chained/reused), worktree
isolation, and warn/hold_once/block behaviour."""
import json
import os
import stat
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from test_host_common import FakeStore, make_project  # noqa: E402

import hearmemory.interfaces as I  # noqa: E402
from hearmemory.host import git as hgit  # noqa: E402
from hearmemory.host import snippets as S  # noqa: E402


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True, check=True)


class GitCommitRegex(unittest.TestCase):
    def test_matches_plain_and_flagged_commit(self):
        for cmd in ("git commit -m x", "git -C . commit --amend", "echo hi && git commit -m x",
                    "cd foo; git commit"):
            self.assertTrue(hgit.is_git_commit_command(cmd), cmd)

    def test_does_not_match_other_git_subcommands(self):
        for cmd in ("git status", "git commit-graph write", "git log --oneline"):
            self.assertFalse(hgit.is_git_commit_command(cmd), cmd)


class Install(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_project(Path(self._tmp.name), git=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_creates_dispatcher_and_project_script_and_exclude_line(self):
        records = hgit.install(self.root, "/usr/bin/python3", dict(I.DEFAULT_CONFIG))
        actions = {(r.path.split("/")[-1], r.action) for r in records}
        self.assertIn(("pre-commit", "created"), actions)
        self.assertIn(("exclude", "exclude_added"), actions)
        dispatcher = hgit.hooks_dir(self.root) / "pre-commit"
        self.assertTrue(os.access(dispatcher, os.X_OK))
        proj_script = self.root / ".hearmemory" / "host" / "git" / "pre-commit"
        self.assertTrue(proj_script.exists())
        exclude = (self.root / ".git" / "info" / "exclude").read_text()
        self.assertIn(".hearmemory/", exclude)

    def test_chains_pre_existing_pre_commit_hook(self):
        hooks_dir = hgit.hooks_dir(self.root)
        hooks_dir.mkdir(parents=True, exist_ok=True)
        existing = hooks_dir / "pre-commit"
        existing.write_text("#!/bin/sh\necho existing-hook\n", encoding="utf-8")
        existing.chmod(existing.stat().st_mode | stat.S_IEXEC)
        records = hgit.install(self.root, "/usr/bin/python3", dict(I.DEFAULT_CONFIG))
        rec = next(r for r in records if r.action == "chained")
        self.assertTrue((hooks_dir / "pre-commit.hearmemory-orig").exists())
        self.assertIn("existing-hook", (hooks_dir / "pre-commit.hearmemory-orig").read_text())
        self.assertIn(S.SH_BEGIN_MARKER, (hooks_dir / "pre-commit").read_text())

    def test_reinstall_reports_reused(self):
        hgit.install(self.root, "/usr/bin/python3", dict(I.DEFAULT_CONFIG))
        records = hgit.install(self.root, "/usr/bin/python3", dict(I.DEFAULT_CONFIG))
        rec = next(r for r in records if r.path.endswith("pre-commit") and r.host == "git")
        self.assertEqual(rec.action, "reused")


class WorktreeIsolation(unittest.TestCase):
    """A sibling worktree without its own hearmemory init never runs hearmemory."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_project(Path(self._tmp.name), git=True)
        hgit.install(self.root, sys.executable, dict(I.DEFAULT_CONFIG))
        self.sibling = self.root.parent / "sibling-wt"
        _git(self.root, "branch", "feature")
        _git(self.root, "worktree", "add", "-q", str(self.sibling), "feature")

    def tearDown(self):
        self._tmp.cleanup()

    def test_sibling_worktree_without_init_is_a_noop(self):
        (self.sibling / "f.txt").write_text("x\n", encoding="utf-8")
        _git(self.sibling, "add", "f.txt")
        # Run the actual generated (shared) dispatcher as git would, from the SIBLING worktree,
        # which never ran `hearmemory init`: it must produce no output and exit 0.
        dispatcher = hgit.hooks_dir(self.sibling) / "pre-commit"  # shared dir, same file as root's
        result = subprocess.run([str(dispatcher)], cwd=str(self.sibling), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        # Also verify the ROOT project's own check script bails when run from a different TOP
        # (double guard: dispatcher looks at the CURRENT worktree, the project script re-checks).
        proj_script = self.root / ".hearmemory" / "host" / "git" / "pre-commit"
        r2 = subprocess.run([str(proj_script)], cwd=str(self.sibling), capture_output=True, text=True)
        self.assertEqual(r2.returncode, 0)
        self.assertEqual(r2.stdout, "")

    def test_second_worktree_init_is_reused(self):
        records = hgit.install(self.sibling, sys.executable, dict(I.DEFAULT_CONFIG))
        rec = next(r for r in records if r.path.endswith("pre-commit") and r.host == "git" and r.shared)
        self.assertEqual(rec.action, "reused")

    def test_dispatcher_kept_when_other_worktree_still_has_project_script(self):
        hgit.install(self.sibling, sys.executable, dict(I.DEFAULT_CONFIG))
        # simulate: the sibling worktree has its own .hearmemory/host/git/pre-commit as well
        (self.sibling / I.HEARMEMORY_DIRNAME / "host" / "git").mkdir(parents=True, exist_ok=True)
        (self.sibling / I.HEARMEMORY_DIRNAME / "host" / "git" / "pre-commit").write_text("#!/bin/sh\nexit 0\n")
        from hearmemory.host.install import _shared_dispatcher_still_needed
        self.assertTrue(_shared_dispatcher_still_needed(self.root))


class HandleHookModes(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_project(Path(self._tmp.name), git=True)
        self.store = FakeStore(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def _patch(self, decision, text, mode):
        import hearmemory.host._deps as deps
        cfg = dict(I.DEFAULT_CONFIG)
        cfg["precommit"] = dict(cfg["precommit"], git_mode=mode)
        old = (deps.open_store, deps.load_memory, deps.mem_check, deps.load_config)
        deps.open_store = lambda root: self.store
        deps.load_memory = lambda *a, **k: object()
        deps.mem_check = lambda *a, **k: I.CheckResult(decision=decision, text=text, mode=mode)
        deps.load_config = lambda root: cfg
        return old

    def _restore(self, old):
        import hearmemory.host._deps as deps
        deps.open_store, deps.load_memory, deps.mem_check, deps.load_config = old

    def test_warn_mode_exits_zero_with_text_on_stderr(self):
        old = self._patch("block", "relies on a refuted claim", "warn")
        try:
            result = hgit.handle_hook("pre-commit", {"project": str(self.root)})
            self.assertEqual(result.exit_code, 0)
            self.assertIn("refuted", result.stderr)
        finally:
            self._restore(old)

    def test_hold_once_blocks_first_then_allows_same_tree(self):
        old = self._patch("block", "relies on a refuted claim", "hold_once")
        try:
            r1 = hgit.handle_hook("pre-commit", {"project": str(self.root)})
            self.assertEqual(r1.exit_code, 1)
            r2 = hgit.handle_hook("pre-commit", {"project": str(self.root)})
            self.assertEqual(r2.exit_code, 0)
        finally:
            self._restore(old)

    def test_block_mode_blocks_on_block_decision(self):
        old = self._patch("block", "relies on a refuted claim", "block")
        try:
            result = hgit.handle_hook("pre-commit", {"project": str(self.root)})
            self.assertEqual(result.exit_code, 1)
        finally:
            self._restore(old)

    def test_off_mode_never_runs_check(self):
        cfg = dict(I.DEFAULT_CONFIG)
        cfg["precommit"] = dict(cfg["precommit"], git_mode="off")
        import hearmemory.host._deps as deps
        old = deps.load_config
        deps.load_config = lambda root: cfg
        try:
            result = hgit.handle_hook("pre-commit", {"project": str(self.root)})
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.stderr, "")
        finally:
            deps.load_config = old

    def test_no_core_mem_available_never_blocks(self):
        # Simulates a partial install: core / memory modules not importable -> must degrade to allow, never crash.
        cfg = dict(I.DEFAULT_CONFIG)
        cfg["precommit"] = dict(cfg["precommit"], git_mode="block")
        import hearmemory.host._deps as deps
        old = deps.load_config
        deps.load_config = lambda root: cfg
        try:
            result = hgit.handle_hook("pre-commit", {"project": str(self.root)})
            self.assertEqual(result.exit_code, 0)
        finally:
            deps.load_config = old


if __name__ == "__main__":
    unittest.main()
