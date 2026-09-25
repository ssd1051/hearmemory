"""cli.py -- argparse skeleton, root resolution, graceful "not implemented"."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import init_project  # noqa: E402

import argparse

from hearmemory.cli import build_parser, main, resolve_root
from hearmemory.interfaces import CLI_COMMANDS, EXIT_NOT_INITIALISED, EXIT_USAGE


class TestParserSkeleton(unittest.TestCase):
    def test_all_cli_commands_have_a_subparser(self):
        parser = build_parser()
        sub_action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        self.assertEqual(set(sub_action.choices.keys()), set(CLI_COMMANDS))

    def test_no_command_prints_help_and_usage_exit(self):
        code = main([])
        self.assertEqual(code, EXIT_USAGE)


class TestRootResolution(unittest.TestCase):
    def test_explicit_project_wins(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(resolve_root(d), Path(d).resolve())

    def test_env_var_used_when_no_explicit(self):
        import os
        with tempfile.TemporaryDirectory() as d:
            old = os.environ.get("HEARMEMORY_PROJECT")
            os.environ["HEARMEMORY_PROJECT"] = d
            try:
                self.assertEqual(resolve_root(None), Path(d).resolve())
            finally:
                if old is None:
                    os.environ.pop("HEARMEMORY_PROJECT", None)
                else:
                    os.environ["HEARMEMORY_PROJECT"] = old

    def test_walks_up_for_initialised_hearmemory(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            init_project(root)
            sub = root / "pkg" / "mod"
            sub.mkdir(parents=True)
            old_cwd = Path.cwd()
            try:
                import os
                os.chdir(sub)
                self.assertEqual(resolve_root(None), root)
            finally:
                os.chdir(old_cwd)


class TestDispatch(unittest.TestCase):
    def test_status_outside_initialised_project_returns_not_initialised(self):
        with tempfile.TemporaryDirectory() as d:
            code = main(["--project", d, "status"])
            self.assertEqual(code, EXIT_NOT_INITIALISED)

    def test_unimplemented_command_does_not_crash(self):
        """A command missing from `hearmemory.commands.COMMANDS` (whether the module doesn't exist yet,
        or simply hasn't implemented that one) must still exit cleanly instead of raising."""
        import types
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            init_project(root)
            fake_commands = types.SimpleNamespace(COMMANDS={})
            with mock.patch("hearmemory.cli._commands_module", return_value=fake_commands):
                code = main(["--project", str(d), "status"])
            self.assertEqual(code, EXIT_USAGE)

    def test_handler_exception_is_caught_and_reported(self):
        import types
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            init_project(root)

            def boom(args, ctx):
                raise RuntimeError("kaboom")

            fake_commands = types.SimpleNamespace(COMMANDS={"status": boom})
            with mock.patch("hearmemory.cli._commands_module", return_value=fake_commands):
                code = main(["--project", str(d), "status"])
            self.assertEqual(code, EXIT_USAGE)

    def test_real_status_dispatch_smoke(self):
        """Smoke test only: once the CLI/MCP layer's `hearmemory.commands` exists, `status` should dispatch through
        without the core-side plumbing (root/config/store wiring) raising."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            init_project(root)
            code = main(["--project", str(d), "status"])
            self.assertIsInstance(code, int)

    def test_hook_dispatch_never_raises_even_without_host_adapters(self):
        with tempfile.TemporaryDirectory() as d:
            import subprocess
            proc = subprocess.run([sys.executable, "-m", "hearmemory", "--project", d, "hook", "git", "pre-commit"],
                                  input=b"{}", capture_output=True, timeout=10)
            self.assertEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
