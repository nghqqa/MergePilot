"""M8-GH-4B3-W3B-S2 tests: Gateway semantic health + E2E status."""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

HERE = Path(os.path.dirname(os.path.abspath(__file__)))
ROOT = HERE.parent.parent
for p in (str(HERE), str(ROOT), str(ROOT / "tools" / "cli")):
    if p not in sys.path:
        sys.path.insert(0, p)

import e2e_gateway_health as gwh            # noqa: E402
import e2e_lifecycle as el                  # noqa: E402


def _ok_transport(method, url, *, headers, body):
    """Fake transport returning valid MCP responses."""
    if method == "GET":
        return 200, {}, "event: message\ndata: {\"type\":\"initialize\"}"
    # POST = tools/list
    tools = [{"name": n} for n in gwh.FROZEN_READ_ONLY_TOOLS]
    return 200, {}, json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "result": {"tools": tools}})


class TestGatewaySemanticHealth(unittest.TestCase):

    def test_healthy_with_exact_tools(self):
        result = gwh.verify_gateway_mcp_health(
            upstream_url="http://172.31.0.34:8082/sse",
            transport=_ok_transport)
        self.assertTrue(result["healthy"])
        self.assertEqual(set(result["tools"]),
                         set(gwh.FROZEN_READ_ONLY_TOOLS))

    def test_zero_tools_rejected(self):
        def t(method, url, *, headers, body):
            if method == "GET":
                return 200, {}, "ok"
            return 200, {}, json.dumps(
                {"result": {"tools": []}})
        with self.assertRaises(gwh.GatewayHealthError) as ctx:
            gwh.verify_gateway_mcp_health(
                upstream_url="http://u/sse", transport=t)
        self.assertEqual(ctx.exception.code, "GATEWAY_ZERO_TOOLS")

    def test_missing_tools_rejected(self):
        def t(method, url, *, headers, body):
            if method == "GET":
                return 200, {}, "ok"
            # Only 3 of 4 tools
            tools = [{"name": n} for n in
                     ("get_pull_request",
                      "get_pull_request_files",
                      "get_file_contents")]
            return 200, {}, json.dumps(
                {"result": {"tools": tools}})
        with self.assertRaises(gwh.GatewayHealthError) as ctx:
            gwh.verify_gateway_mcp_health(
                upstream_url="http://u/sse", transport=t)
        self.assertEqual(ctx.exception.code, "GATEWAY_MISSING_TOOLS")

    def test_extra_tools_rejected(self):
        def t(method, url, *, headers, body):
            if method == "GET":
                return 200, {}, "ok"
            tools = [{"name": n} for n in
                     gwh.FROZEN_READ_ONLY_TOOLS]
            tools.append({"name": "create_pull_request"})
            return 200, {}, json.dumps(
                {"result": {"tools": tools}})
        with self.assertRaises(gwh.GatewayHealthError) as ctx:
            gwh.verify_gateway_mcp_health(
                upstream_url="http://u/sse", transport=t)
        self.assertEqual(ctx.exception.code, "GATEWAY_EXTRA_TOOLS")

    def test_write_tool_rejected(self):
        def t(method, url, *, headers, body):
            if method == "GET":
                return 200, {}, "ok"
            tools = [{"name": n} for n in
                     gwh.FROZEN_READ_ONLY_TOOLS]
            tools.append({"name": "merge_pull_request"})
            return 200, {}, json.dumps(
                {"result": {"tools": tools}})
        with self.assertRaises(gwh.GatewayHealthError) as ctx:
            gwh.verify_gateway_mcp_health(
                upstream_url="http://u/sse", transport=t)
        self.assertEqual(ctx.exception.code, "GATEWAY_EXTRA_TOOLS")

    def test_upstream_failure_rejected(self):
        def t(method, url, *, headers, body):
            raise ConnectionError("upstream down")
        with self.assertRaises(gwh.GatewayHealthError) as ctx:
            gwh.verify_gateway_mcp_health(
                upstream_url="http://u/sse", transport=t)
        self.assertEqual(ctx.exception.code,
                         "GATEWAY_UPSTREAM_UNREACHABLE")

    def test_initialize_http_error_rejected(self):
        def t(method, url, *, headers, body):
            return 503, {}, "unavailable"
        with self.assertRaises(gwh.GatewayHealthError) as ctx:
            gwh.verify_gateway_mcp_health(
                upstream_url="http://u/sse", transport=t)
        self.assertEqual(ctx.exception.code,
                         "GATEWAY_INITIALIZE_FAILED")

    def test_safe_wrapper_returns_false_on_error(self):
        def t(method, url, *, headers, body):
            raise ConnectionError()
        result = gwh.verify_gateway_mcp_health_safe(
            upstream_url="http://u/sse", transport=t)
        self.assertFalse(result["healthy"])
        self.assertEqual(result["error"],
                         "GATEWAY_UPSTREAM_UNREACHABLE")

    def test_running_port_not_sufficient(self):
        """Running + port open ≠ semantic health."""
        # The function only checks MCP protocol, not Running/port.
        # This is inherent in the design: if initialize or tools/list
        # fails, healthy=False even if container is Running.
        def port_only_transport(method, url, *, headers, body):
            if method == "GET":
                return 503, {}, ""  # Port open but MCP fails
            return 503, {}, ""
        result = gwh.verify_gateway_mcp_health_safe(
            upstream_url="http://u/sse",
            transport=port_only_transport)
        self.assertFalse(result["healthy"])


class _StatusCP:
    def __init__(self, rc=0, out=b"id123 running"):
        self.returncode = rc
        self.stdout = out


class _FakeDocker:
    def __init__(self, existing=None):
        self.calls = []
        self.existing = existing or {}

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        if argv[0] == "inspect":
            name = argv[1]
            info = self.existing.get(name)
            if info:
                return _StatusCP(0,
                                 ("%s %s" % (info["id"],
                                             info["state"])).encode())
            return _StatusCP(1, b"")
        return _StatusCP(0, b"")


class TestE2EStatus(unittest.TestCase):

    def test_status_reports_11_services(self):
        session = {"e2e_stage": "complete",
                   "e2e_container_ids": {},
                   "e2e_runtime_journal": {},
                   "firewall_sid": "ab12cd34",
                   "firewall_state": "installed",
                   "prerequisite_summary": {"verified": True}}
        fd = _FakeDocker()
        result = el.run_e2e_status(
            docker_executor=fd, session=session)
        # 11 services + 4 metadata entries
        service_count = sum(1 for k in result if not k.startswith("_"))
        self.assertEqual(service_count, 11)

    def test_status_sanitized_no_secrets(self):
        session = {"e2e_stage": "complete",
                   "e2e_container_ids": {},
                   "e2e_runtime_journal": {},
                   "firewall_sid": "ab12cd34"}
        fd = _FakeDocker()
        result = el.run_e2e_status(
            docker_executor=fd, session=session)
        blob = str(result)
        for forbidden in ("ghp_", "password=", "postgresql://",
                          "BEGIN PRIVATE", "Bearer ", "eyJ"):
            self.assertNotIn(forbidden, blob)

    def test_status_reports_absent_containers(self):
        session = {"e2e_stage": "init",
                   "e2e_container_ids": {},
                   "e2e_runtime_journal": {}}
        fd = _FakeDocker()  # nothing exists
        result = el.run_e2e_status(
            docker_executor=fd, session=session)
        for svc in ("postgres", "gh-proxy-r", "mcp-bridge"):
            self.assertFalse(result[svc]["exists"])

    def test_status_reports_stage(self):
        session = {"e2e_stage": "firewall",
                   "e2e_container_ids": {},
                   "e2e_runtime_journal": {}}
        fd = _FakeDocker()
        result = el.run_e2e_status(
            docker_executor=fd, session=session)
        self.assertEqual(result["_stage"], "firewall")


if __name__ == "__main__":
    unittest.main()
