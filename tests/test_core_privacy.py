"""privacy.py -- exclude globs, command/env-dump withholding, redaction."""
from __future__ import annotations

import unittest

from hearmemory.interfaces import DEFAULT_CONFIG
from hearmemory.privacy import (command_touches_excluded, is_env_dump_command, is_excluded, redact)


def _cfg():
    return {"privacy": dict(DEFAULT_CONFIG["privacy"])}


class TestExcludeGlobs(unittest.TestCase):
    def test_default_excludes_env_and_keys(self):
        cfg = _cfg()
        self.assertTrue(is_excluded(".env", cfg))
        self.assertTrue(is_excluded("config/.env", cfg))
        self.assertTrue(is_excluded("id_rsa", cfg))
        self.assertTrue(is_excluded(".ssh/id_rsa", cfg))
        self.assertFalse(is_excluded("src/main.py", cfg))

    def test_negation_exception_overrides_earlier_match(self):
        cfg = _cfg()
        self.assertTrue(is_excluded(".env.local", cfg))
        self.assertFalse(is_excluded(".env.example", cfg))

    def test_outside_project_path_matches_by_suffix(self):
        cfg = _cfg()
        self.assertTrue(is_excluded("/home/u/.ssh/id_rsa", cfg))
        self.assertTrue(is_excluded("~/.ssh/id_rsa", cfg))


class TestCommandExclusion(unittest.TestCase):
    def test_cat_env_file(self):
        self.assertTrue(command_touches_excluded("cat .env", _cfg()))

    def test_source_env_semicolon_env(self):
        self.assertTrue(command_touches_excluded("source .env; env", _cfg()))

    def test_dot_env_local_and_run(self):
        self.assertTrue(command_touches_excluded(". ./.env.local && run", _cfg()))

    def test_cat_ssh_key(self):
        self.assertTrue(command_touches_excluded("cat ~/.ssh/id_rsa", _cfg()))

    def test_cp_id_ed25519(self):
        self.assertTrue(command_touches_excluded("cp id_ed25519 /tmp", _cfg()))

    def test_proc_environ(self):
        self.assertTrue(command_touches_excluded("cat /proc/1/environ", _cfg()))

    def test_harmless_command_not_excluded(self):
        self.assertFalse(command_touches_excluded("pytest tests/test_x.py -q", _cfg()))


class TestEnvDumpDetection(unittest.TestCase):
    def test_printenv_bare(self):
        self.assertTrue(is_env_dump_command("printenv"))

    def test_env_bare(self):
        self.assertTrue(is_env_dump_command("env"))

    def test_env_with_command_is_not_a_dump(self):
        self.assertFalse(is_env_dump_command("env FOO=bar pytest"))

    def test_export_bare(self):
        self.assertTrue(is_env_dump_command("export"))

    def test_export_with_assignment_is_not_a_dump(self):
        self.assertFalse(is_env_dump_command("export FOO=bar"))

    def test_declare_dash_x(self):
        self.assertTrue(is_env_dump_command("declare -x"))

    def test_compgen_v(self):
        self.assertTrue(is_env_dump_command("compgen -v"))


class TestRedact(unittest.TestCase):
    def test_stripe_key_assignment_redacted(self):
        out, n = redact("STRIPE_KEY=sk_live_abcdefghijklmnopqrstuv")
        self.assertNotIn("abcdefghijklmnopqrstuv", out)
        self.assertGreaterEqual(n, 1)
        self.assertIn("STRIPE_KEY=", out)

    def test_openai_key_assignment_redacted(self):
        out, n = redact('OPENAI_KEY: "sk-abcdefghijklmnopqrstuvwx"')
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwx", out)
        self.assertGreaterEqual(n, 1)

    def test_db_pass_assignment_redacted(self):
        out, n = redact("DB_PASS=hunter2verysecret")
        self.assertNotIn("hunter2verysecret", out)
        self.assertGreaterEqual(n, 1)

    def test_bare_64_hex_redacted(self):
        hexstr = "a" * 64
        out, n = redact(f"token seen: {hexstr}")
        self.assertNotIn(hexstr, out)
        self.assertGreaterEqual(n, 1)

    def test_commit_prefixed_hex_preserved(self):
        hexstr = "b" * 40
        out, n = redact(f"commit {hexstr} looks fine")
        self.assertIn(hexstr, out)
        self.assertEqual(n, 0)

    def test_git_rev_parse_head_output_preserved_via_git_output_flag(self):
        hexstr = "c" * 40
        out, n = redact(hexstr, git_output=True)
        self.assertEqual(out, hexstr)
        self.assertEqual(n, 0)

    def test_keyerror_and_passed_count_not_redacted(self):
        text = "KeyError: x\n3 passed, 1 failed in 0.12s"
        out, n = redact(text)
        self.assertEqual(out, text)
        self.assertEqual(n, 0)

    def test_private_key_block_redacted(self):
        block = "-----BEGIN RSA PRIVATE KEY-----\nMIIB...==\n-----END RSA PRIVATE KEY-----"
        out, n = redact(block)
        self.assertNotIn("MIIB", out)
        self.assertGreaterEqual(n, 1)

    def test_bearer_token_redacted(self):
        out, n = redact("Authorization: Bearer abcdEFGH12345678ijkl")
        self.assertNotIn("abcdEFGH12345678ijkl", out)
        self.assertGreaterEqual(n, 1)

    def test_url_credentials_redacted(self):
        out, n = redact("https://user:hunter2@example.com/path")
        self.assertNotIn("hunter2", out)
        self.assertGreaterEqual(n, 1)

    def test_env_value_redaction(self):
        out, n = redact("the token is xVerySecretValue123", environ={"TYPESAFE_API_KEY": "xVerySecretValue123"})
        self.assertNotIn("xVerySecretValue123", out)
        self.assertGreaterEqual(n, 1)

    def test_extra_redact_patterns(self):
        out, n = redact("ticket JIRA-1234 filed", extra_redact_patterns=[r"JIRA-\d+"])
        self.assertNotIn("JIRA-1234", out)
        self.assertEqual(n, 1)

    def test_empty_text_noop(self):
        self.assertEqual(redact(""), ("", 0))


if __name__ == "__main__":
    unittest.main()
