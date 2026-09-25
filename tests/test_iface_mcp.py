"""protocol-conformance tests for `hearmemory.mcp_server.serve`.

Drives the stdio JSON-RPC server exactly as a real MCP client would: write
JSON-RPC request lines into a StringIO "stdin", run `serve()` to EOF, and read
back whatever lines it wrote to a StringIO "stdout". Every response line must
be a single well-formed JSON-RPC 2.0 message; nothing else may ever appear on
stdout ("stdout 只写协议消息").
"""
from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tests"))

import hearmemory.interfaces as I  # noqa: E402
from hearmemory import mcp_server  # noqa: E402
from test_iface_support import FakeRegistry, InstalledFakeModules  # noqa: E402


def run_rpc(root, host: Optional[str], requests: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Feeds `requests` (already-encoded dicts, or raw strings for malformed-input
    tests) as newline-delimited JSON into `serve()` and returns the parsed
    response lines, in order."""
    lines = []
    for r in requests:
        lines.append(r if isinstance(r, str) else json.dumps(r))
    stdin = io.StringIO("\n".join(lines) + "\n")
    stdout = io.StringIO()
    rc = mcp_server.serve(root, host, stdin, stdout)
    raw = stdout.getvalue()
    responses = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        responses.append(json.loads(line))  # will raise if anything non-JSON leaked onto stdout
    return responses, rc, raw


class McpTestCase(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.registry = FakeRegistry()
        self.fakes = InstalledFakeModules(self.registry)
        self.fakes.__enter__()
        self.addCleanup(self.fakes.__exit__)
        self.addCleanup(self._tmp.cleanup)

    def init_project(self):
        return self.registry.open_store(self.root, create=True)


class InitializeTests(McpTestCase):
    def test_returns_requested_version_when_supported(self):
        version = I.MCP_PROTOCOL_VERSIONS[1]
        responses, rc, raw = run_rpc(self.root, "claude", [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": version}}])
        self.assertEqual(rc, 0)
        self.assertEqual(responses[0]["result"]["protocolVersion"], version)
        self.assertEqual(responses[0]["result"]["serverInfo"]["name"], "hearmemory")
        self.assertIn("tools", responses[0]["result"]["capabilities"])

    def test_falls_back_to_latest_when_unsupported(self):
        responses, rc, raw = run_rpc(self.root, "claude", [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}}])
        self.assertEqual(responses[0]["result"]["protocolVersion"], I.MCP_PROTOCOL_VERSIONS[0])

    def test_notifications_initialized_gets_no_response(self):
        responses, rc, raw = run_rpc(self.root, "claude", [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        ])
        self.assertEqual(len(responses), 1)  # only the initialize response

    def test_ping(self):
        responses, rc, raw = run_rpc(self.root, "claude", [{"jsonrpc": "2.0", "id": 7, "method": "ping"}])
        self.assertEqual(responses[0]["result"], {})
        self.assertEqual(responses[0]["id"], 7)


class ToolsListTests(McpTestCase):
    def test_lists_all_five_tools_with_schemas(self):
        responses, rc, raw = run_rpc(self.root, "claude", [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])
        tools = responses[0]["result"]["tools"]
        names = {t["name"] for t in tools}
        self.assertEqual(names, set(I.MCP_TOOLS))
        for t in tools:
            self.assertIn("inputSchema", t)
            self.assertEqual(t["inputSchema"]["type"], "object")
            self.assertFalse(t["inputSchema"].get("additionalProperties", True))
            self.assertIn("description", t)


class ProtocolErrorTests(McpTestCase):
    def test_unknown_method(self):
        responses, rc, raw = run_rpc(self.root, "claude", [{"jsonrpc": "2.0", "id": 1, "method": "bogus/method"}])
        self.assertEqual(responses[0]["error"]["code"], -32601)

    def test_bad_json_gets_parse_error(self):
        responses, rc, raw = run_rpc(self.root, "claude", ["{not json", '{"jsonrpc":"2.0","id":1,"method":"ping"}'])
        self.assertEqual(responses[0]["error"]["code"], -32700)
        self.assertEqual(responses[1]["result"], {})

    def test_missing_method_is_invalid_request(self):
        responses, rc, raw = run_rpc(self.root, "claude", [{"jsonrpc": "2.0", "id": 1}])
        self.assertEqual(responses[0]["error"]["code"], -32600)

    def test_notification_with_unknown_method_gets_no_response(self):
        responses, rc, raw = run_rpc(self.root, "claude", [{"jsonrpc": "2.0", "method": "bogus/method"}])
        self.assertEqual(responses, [])

    def test_tools_call_unknown_tool_is_invalid_params(self):
        self.init_project()
        responses, rc, raw = run_rpc(self.root, "claude", [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "not_a_tool", "arguments": {}}}])
        self.assertEqual(responses[0]["error"]["code"], -32602)

    def test_tools_call_missing_required_argument_is_invalid_params(self):
        self.init_project()
        responses, rc, raw = run_rpc(self.root, "claude", [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "hearmemory_record", "arguments": {}}}])
        self.assertEqual(responses[0]["error"]["code"], -32602)

    def test_tools_call_wrong_type_argument_is_invalid_params(self):
        self.init_project()
        responses, rc, raw = run_rpc(self.root, "claude", [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "hearmemory_recall", "arguments": {"query": 5}}}])
        self.assertEqual(responses[0]["error"]["code"], -32602)

    def test_tools_call_unknown_extra_argument_is_invalid_params(self):
        self.init_project()
        responses, rc, raw = run_rpc(self.root, "claude", [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "hearmemory_status", "arguments": {"bogus": 1}}}])
        self.assertEqual(responses[0]["error"]["code"], -32602)


class NotInitialisedTests(McpTestCase):
    """the server starts and answers even when `.hearmemory` does not exist; tools
    return isError instead of a protocol error, and write nothing."""

    def test_hearmemory_recall_iserror_when_missing(self):
        responses, rc, raw = run_rpc(self.root, "claude", [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "hearmemory_recall", "arguments": {"query": ""}}}])
        self.assertEqual(rc, 0)
        result = responses[0]["result"]
        self.assertTrue(result["isError"])
        self.assertIn("hearmemory init", result["content"][0]["text"])

    def test_hearmemory_status_iserror_when_missing(self):
        responses, rc, raw = run_rpc(self.root, "claude", [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "hearmemory_status", "arguments": {}}}])
        self.assertTrue(responses[0]["result"]["isError"])

    def test_stops_writing_after_version_deleted_mid_session(self):
        store = self.init_project()
        req = lambda i, name, args: {"jsonrpc": "2.0", "id": i, "method": "tools/call",
                                      "params": {"name": name, "arguments": args}}
        responses, rc, raw = run_rpc(self.root, "claude", [
            req(1, "hearmemory_record", {"text": "first note"}),
        ])
        self.assertFalse(responses[0]["result"].get("isError", False))
        self.assertEqual(len(store._obs), 1)

        self.registry.delete_version(self.root)
        responses2, rc2, raw2 = run_rpc(self.root, "claude", [
            req(1, "hearmemory_record", {"text": "second note, should not be written"}),
        ])
        self.assertTrue(responses2[0]["result"]["isError"])
        self.assertEqual(len(store._obs), 1, "a tool call after VERSION is gone must write nothing")


class ToolCallTests(McpTestCase):
    def test_hearmemory_record_then_recall_and_status(self):
        self.init_project()

        def call(i, name, args):
            responses, rc, raw = run_rpc(self.root, "codex", [
                {"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": name, "arguments": args}}])
            return responses[0]["result"]

        rec = call(1, "hearmemory_record", {"text": "root cause is a stale cache", "kind": "claim"})
        self.assertFalse(rec.get("isError", False))
        self.assertTrue(rec["content"][0]["text"].startswith(I.RECORD_ECHO_PREFIX))
        self.assertRegex(rec["structuredContent"]["obs_id"], I.OBS_ID_RE)

        recall = call(2, "hearmemory_recall", {"query": "cache"})
        self.assertFalse(recall.get("isError", False))
        self.assertIn("structuredContent", recall)

        status = call(3, "hearmemory_status", {})
        self.assertEqual(status["structuredContent"]["counts"]["observations"], 1)

    def test_hearmemory_record_issue_returns_issue_id(self):
        self.init_project()
        responses, rc, raw = run_rpc(self.root, "cursor", [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "hearmemory_record", "arguments": {"text": "flaky test", "kind": "issue"}}}])
        result = responses[0]["result"]
        self.assertIsNotNone(result["structuredContent"]["issue_id"])

    def test_hearmemory_check_always_advisory_warn_mode(self):
        self.init_project()
        responses, rc, raw = run_rpc(self.root, "claude", [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "hearmemory_check", "arguments": {"text": "TRIGGER_BLOCK", "action": "claim"}}}])
        result = responses[0]["result"]
        # MCP never blocks: it only reports the decision, there is no exit code concept here.
        self.assertFalse(result.get("isError", False))
        self.assertEqual(result["structuredContent"]["mode"], "warn")
        self.assertEqual(result["structuredContent"]["decision"], "block")

    def test_hearmemory_issues_list_and_open(self):
        self.init_project()

        def call(i, name, args):
            responses, rc, raw = run_rpc(self.root, "claude", [
                {"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": name, "arguments": args}}])
            return responses[0]["result"]

        empty = call(1, "hearmemory_issues", {"action": "list"})
        self.assertIn("no open issues", empty["content"][0]["text"])

        opened = call(2, "hearmemory_issues", {"action": "open", "title": "leaks memory"})
        self.assertIsNotNone(opened["structuredContent"]["issue_id"])


class StdoutHygieneTests(McpTestCase):
    def test_stdout_is_only_protocol_json_lines(self):
        self.init_project()
        stdin = io.StringIO(
            "not json at all\n"
            + json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n"
            + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
            + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n"
            + json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                          "params": {"name": "hearmemory_status", "arguments": {}}}) + "\n"
        )
        stdout = io.StringIO()
        rc = mcp_server.serve(self.root, "claude", stdin, stdout)
        self.assertEqual(rc, 0)
        for line in stdout.getvalue().splitlines():
            if not line.strip():
                continue
            doc = json.loads(line)  # raises if anything but JSON leaked onto stdout
            self.assertEqual(doc.get("jsonrpc"), "2.0")


if __name__ == "__main__":
    unittest.main()
