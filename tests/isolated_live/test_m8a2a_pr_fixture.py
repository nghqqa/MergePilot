#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M8-A2-a — isolated-stack read-only PR fixture tests.

Covers the opt-in ``MERGEPILOT_STUB_PR_FIXTURE`` extension of the
in-container upstream stub and the (already-existing, untouched) bind-first
success chain it is designed to drive:

  1. Default contract preserved verbatim: env unset (or any value other
     than the exact string '1') keeps the stub ZERO-TOOL — list_tools is
     empty and every call_tool raises, byte-for-byte the v3 Fix 3 message.
  2. Fixture mode (env == '1'): exactly one read-only tool; get/get_diff/
     get_files return fixed, mutually consistent, schema-valid payloads;
     unknown tools, unknown/write methods, wrong owner/repo/PR, and
     non-object arguments all fail closed.
  3. Determinism: repeated calls return byte-identical payloads; the
     fixture identity (repo/PR/base/head/diff/files) is self-consistent
     with the M4F_RUN + gateway_read_pr + build_skill_inputs contracts.
  4. The existing success chain ordering is verified WITHOUT a database:
     a fake connection records SQL; m4f_controller.stage_six_skill_run
     must call public.bind_revision → commit → enqueue_snapshot_job →
     commit → snapshot completion → enqueue_skill_job ×6 (SKILLS order)
     → commit; the only revision_bindings write is via the contract
     function, and no audit_events / mcp_calls write occurs.
  5. A1 failure semantics untouched (source-level): retryable errors still
     return to M4F_PENDING, M4FIngressError still reaches terminal ERROR
     on the 5th attempt; the fixture module never imports the controller.
  6. No frozen-truth promotion: NOT_VERIFIED / false constants unchanged.

No Docker, no network, no database, no skips.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import unittest
from pathlib import Path

_HERE = Path(os.path.dirname(os.path.abspath(__file__)))
ROOT = _HERE.parent.parent

import importlib.util as _ilu  # noqa: E402


def _load(name, relpath):
    """Load a module from an explicit file path WITHOUT touching
    sys.path — inserting tools/policy-gateway globally would shadow
    tools/workflow-controller/healthcheck.py for unrelated tests."""
    spec = _ilu.spec_from_file_location(name, ROOT / relpath)
    mod = _ilu.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class _Tool:
    def __init__(self, name=None, description=None, inputSchema=None):
        self.name = name
        self.description = description
        self.inputSchema = inputSchema


class _StubServer:
    """Captures the @server.list_tools()/@server.call_tool() handlers so
    tests can invoke them directly."""

    def __init__(self):
        self._handlers = {}

    def list_tools(self):
        def deco(fn):
            self._handlers["list"] = fn
            return fn
        return deco

    def call_tool(self):
        def deco(fn):
            self._handlers["call"] = fn
            return fn
        return deco

    def run(self, *a, **k):
        raise AssertionError("server.run must not execute in unit tests")


# The stub's runtime deps (mcp / starlette / uvicorn) exist only inside the
# gateway container image (python:3.12-slim). The host runs Python 3.9 which
# the mcp SDK does not support, so we install minimal placeholder modules
# for import resolution. The TextContent placeholder is a REAL Pydantic
# model with the same field contract as mcp.types.TextContent — NOT a mock
# — so type validation in tests exercises actual Pydantic constraints.
import types as _types
from typing import Literal as _Literal

from pydantic import BaseModel as _BaseModel


class _TextContentModel(_BaseModel):
    """Faithful structural twin of mcp.types.TextContent (Pydantic v2):
    ``type: Literal["text"] = "text"`` + ``text: str``. The real SDK type
    (in the container) has identical field names, types, and validation."""
    type: _Literal["text"] = "text"
    text: str


def _install(name):
    if name in sys.modules:
        return
    sys.modules[name] = _types.ModuleType(name)


_install("mcp")
_install("mcp.server")
_install("mcp.server.sse")
_install("mcp.types")
_install("starlette")
_install("starlette.applications")
_install("starlette.requests")
_install("starlette.routing")
sys.modules["mcp.server"].Server = lambda name: _StubServer()
class _FakeSSE:
    handle_post_message = staticmethod(lambda *a, **k: None)

sys.modules["mcp.server.sse"].SseServerTransport = lambda p: _FakeSSE()
sys.modules["mcp.types"].Tool = _Tool
sys.modules["mcp.types"].TextContent = _TextContentModel
sys.modules["starlette.applications"].Starlette = lambda **kw: None
sys.modules["starlette.requests"].Request = lambda *a, **k: None
sys.modules["starlette.routing"].Mount = lambda *a, **k: None
sys.modules["starlette.routing"].Route = lambda *a, **k: None
sys.modules["uvicorn"] = _types.ModuleType("uvicorn")
sys.modules["uvicorn"].run = lambda *a, **k: None



stub = _load("upstream_stub",
            "tools/policy-gateway/upstream_stub.py")

STUB_SOURCE = (ROOT / "tools" / "policy-gateway" / "upstream_stub.py")\
    .read_text(encoding="utf-8")

_SHA40 = re.compile(r"^[0-9a-f]{40}$")


def _with_env(value):
    """Context manager setting the fixture env var; '' removes it."""
    import contextlib

    @contextlib.contextmanager
    def _cm():
        old = os.environ.get(stub.FIXTURE_ENV)
        try:
            if value == "":
                os.environ.pop(stub.FIXTURE_ENV, None)
            else:
                os.environ[stub.FIXTURE_ENV] = value
            yield
        finally:
            if old is None:
                os.environ.pop(stub.FIXTURE_ENV, None)
            else:
                os.environ[stub.FIXTURE_ENV] = old

    return _cm()


def _call(name, args):
    return asyncio.run(stub.call_tool(name, args))


def _list():
    return asyncio.run(stub.list_tools())


def _args(method, *, owner=None, repo=None, pr=None):
    return {
        "method": method,
        "owner": owner or stub.FIXTURE_OWNER,
        "repo": repo or stub.FIXTURE_REPO_NAME,
        "pullNumber": pr if pr is not None else stub.FIXTURE_PR_NUMBER,
    }


# ── 1: default zero-tool contract preserved ─────────────────────────────────

class TestDefaultZeroToolContract(unittest.TestCase):

    def test_env_unset_zero_tools_and_call_refused(self):
        with _with_env(""):
            self.assertEqual(_list(), [])
            with self.assertRaises(ValueError) as cm:
                _call("pull_request_read", _args("get"))
            self.assertIn("serves no tools", str(cm.exception))

    def test_only_exact_string_one_enables(self):
        for value in ("0", "true", "yes", "on", " 1", "1 "):
            with _with_env(value):
                self.assertEqual(_list(), [], value)
                with self.assertRaises(ValueError):
                    _call("pull_request_read", _args("get"))

    def test_default_refusal_message_unchanged(self):
        # byte-for-byte the v3 Fix 3 message (regression pin)
        self.assertIn(
            "isolated upstream stub serves no tools (call_tool %r refused)",
            STUB_SOURCE)

    def test_module_has_no_outbound_or_secret_surface(self):
        self.assertNotIn("import urllib", STUB_SOURCE)
        self.assertNotIn("import requests", STUB_SOURCE)
        self.assertNotIn("import httpx", STUB_SOURCE)
        self.assertNotIn("socket.socket", STUB_SOURCE)
        self.assertNotIn("GITHUB_TOKEN", STUB_SOURCE)
        self.assertNotIn("password", STUB_SOURCE.lower())


# ── 2: fixture mode — exact tool surface and fail-closed ────────────────────

class TestFixtureMode(unittest.TestCase):

    def setUp(self):
        self._cm = _with_env("1")
        self._cm.__enter__()

    def tearDown(self):
        self._cm.__exit__(None, None, None)

    def test_exactly_one_readonly_tool(self):
        tools = _list()
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0].name, "pull_request_read")
        self.assertEqual(tools[0].inputSchema["properties"]["method"]["enum"],
                         ["get", "get_diff", "get_files"])

    def test_get_payload_strict_contract(self):
        text = _call("pull_request_read", _args("get"))
        d = json.loads(text[0].text if isinstance(text, list) else text)
        self.assertEqual(d["number"], stub.FIXTURE_PR_NUMBER)
        self.assertEqual(d["state"], "open")
        self.assertIs(d["merged"], False)
        self.assertRegex(d["head"]["sha"], _SHA40)
        self.assertRegex(d["base"]["sha"], _SHA40)
        self.assertNotEqual(d["head"]["sha"], d["base"]["sha"])
        self.assertEqual(d["head"]["repo"]["full_name"], stub.FIXTURE_REPO)
        self.assertIsInstance(d["head"]["ref"], str)
        self.assertIsInstance(d["base"]["ref"], str)

    def test_get_diff_is_unified_and_consistent(self):
        result = _call("pull_request_read", _args("get_diff"))
        text = result[0].text if isinstance(result, list) else result
        self.assertIsInstance(text, str)
        self.assertIn("diff --git a/%s b/%s" % (stub.FIXTURE_FILE_PATH,
                                                stub.FIXTURE_FILE_PATH), text)
        self.assertIn("@@", text)

    def test_get_files_shape_and_diff_consistency(self):
        result = _call("pull_request_read", _args("get_files"))
        files = json.loads(result[0].text if isinstance(result, list)
                           else result)
        self.assertIsInstance(files, list)
        self.assertEqual(len(files), 1)
        item = files[0]
        self.assertEqual(item["filename"], stub.FIXTURE_FILE_PATH)
        self.assertEqual(item["status"], "modified")
        self.assertIsInstance(item["additions"], int)
        self.assertIsInstance(item["deletions"], int)
        # same single change described by the diff
        diff_r = _call("pull_request_read", _args("get_diff"))
        diff = diff_r[0].text if isinstance(diff_r, list) else diff_r
        self.assertIn(item["filename"], diff)

    def test_deterministic_byte_identical_replay(self):
        for method in ("get", "get_diff", "get_files"):
            a = _call("pull_request_read", _args(method))
            b = _call("pull_request_read", _args(method))
            self.assertEqual(a, b, method)

    def test_unknown_tool_refused(self):
        with self.assertRaises(ValueError):
            _call("create_pull_request", _args("get"))
        with self.assertRaises(ValueError):
            _call("merge_pull_request", _args("get"))

    def test_write_and_unknown_methods_refused(self):
        for method in ("update", "merge", "create", "delete", "get_review",
                       "list", ""):
            with self.assertRaises(ValueError, msg=method):
                _call("pull_request_read", _args(method))

    def test_wrong_repo_owner_pr_refused(self):
        with self.assertRaises(ValueError):
            _call("pull_request_read", _args("get", repo="other-repo"))
        with self.assertRaises(ValueError):
            _call("pull_request_read", _args("get", owner="evil"))
        with self.assertRaises(ValueError):
            _call("pull_request_read", _args("get", pr=9002))
        with self.assertRaises(ValueError):
            _call("pull_request_read", _args("get", pr="9001"))  # str not int
        with self.assertRaises(ValueError):
            _call("pull_request_read", _args("get", pr=True))    # bool

    def test_non_object_arguments_refused(self):
        with self.assertRaises(ValueError):
            _call("pull_request_read", None)
        with self.assertRaises(ValueError):
            _call("pull_request_read", "get")

    def test_no_secrets_or_machine_paths_in_payloads(self):
        # Extract .text from the TextContent wrappers before scanning
        results = [_call("pull_request_read", _args(m))
                   for m in ("get", "get_diff", "get_files")]
        blob = "".join(
            r[0].text if isinstance(r, list) and r
            and hasattr(r[0], "text") else str(r)
            for r in results)
        for banned in ("password", "token", "postgresql://", "C:\\",
                       "/mnt/", "ghp_", "AKIA"):
            self.assertNotIn(banned, blob)


# ── 2b: MCP SDK TextContent return contract ────────────────────────────────

class TestTextContentReturnContract(unittest.TestCase):
    """The call_tool handler must return a list of exactly one TextContent
    (Pydantic model with type='text' and text=<deterministic JSON>), never
    a bare string — a bare str fails the SDK's Pydantic validation and the
    gateway proxies the validation error instead of the fixture payload."""

    def setUp(self):
        self._cm = _with_env("1")
        self._cm.__enter__()

    def tearDown(self):
        self._cm.__exit__(None, None, None)

    def _call_and_check(self, method):
        result = _call("pull_request_read", _args(method))
        self.assertIsInstance(result, list,
                              "%s: must return a list" % method)
        self.assertEqual(len(result), 1,
                         "%s: exactly one content item" % method)
        item = result[0]
        self.assertIsInstance(item, _TextContentModel,
                              "%s: must be a TextContent instance" % method)
        self.assertEqual(item.type, "text")
        self.assertIsInstance(item.text, str)
        return item.text

    def test_get_returns_text_content_list(self):
        text = self._call_and_check("get")
        d = json.loads(text)
        self.assertEqual(d["number"], stub.FIXTURE_PR_NUMBER)
        self.assertEqual(d["state"], "open")

    def test_get_diff_returns_text_content_list(self):
        text = self._call_and_check("get_diff")
        self.assertIn("diff --git", text)

    def test_get_files_returns_text_content_list(self):
        text = self._call_and_check("get_files")
        files = json.loads(text)
        self.assertEqual(len(files), 1)

    def test_pydantic_validation_passes(self):
        # The returned item passes actual Pydantic model re-validation
        result = _call("pull_request_read", _args("get"))
        item = result[0]
        revalidated = _TextContentModel.model_validate(
            {"type": item.type, "text": item.text})
        self.assertEqual(revalidated.text, item.text)

    def test_pydantic_rejects_bare_string(self):
        # A bare string would fail TextContent validation (the original bug)
        with self.assertRaises(Exception):
            _TextContentModel.model_validate("not a dict")

    def test_text_content_json_roundtrip(self):
        # model_dump produces a dict that can be JSON-serialized (the
        # serialization path the gateway uses for its own responses)
        result = _call("pull_request_read", _args("get"))
        item = result[0]
        dumped = item.model_dump()
        self.assertEqual(dumped["type"], "text")
        json.dumps(dumped)  # must not raise
        parsed = json.loads(dumped["text"])
        self.assertEqual(parsed["number"], stub.FIXTURE_PR_NUMBER)

    def test_all_three_methods_unpack_to_old_contract(self):
        """Unwrapping .text yields EXACTLY the payloads the pre-fix tests
        asserted (backward compatibility of fixture data)."""
        get_text = self._call_and_check("get")
        diff_text = self._call_and_check("get_diff")
        files_text = self._call_and_check("get_files")

        # get payload matches _fixture_pr_json() output
        self.assertEqual(get_text, stub._fixture_pr_json())

        # diff payload matches FIXTURE_DIFF constant
        self.assertEqual(diff_text, stub.FIXTURE_DIFF)

        # files payload matches json.dumps(FIXTURE_FILES, sort_keys=True)
        self.assertEqual(files_text,
                         json.dumps(stub.FIXTURE_FILES, sort_keys=True))

    def test_default_closed_still_raises_original_message(self):
        with _with_env(""):
            with self.assertRaises(ValueError) as cm:
                _call("pull_request_read", _args("get"))
            self.assertIn(
                "isolated upstream stub serves no tools", str(cm.exception))

    def test_unknown_tool_in_fixture_mode_still_rejected(self):
        with self.assertRaises(ValueError):
            _call("unknown_tool", _args("get"))

    def test_write_method_in_fixture_mode_still_rejected(self):
        with self.assertRaises(ValueError):
            _call("pull_request_read", _args("merge"))

    def test_stub_source_uses_text_content_import(self):
        self.assertIn("TextContent", STUB_SOURCE)
        self.assertIn("from mcp.types import TextContent", STUB_SOURCE)
        self.assertIn("return [TextContent(type=\"text\", text=payload)]",
                      STUB_SOURCE)


# ── 3: success-chain ordering (fake connection, no DB) ─────────────────────

class _FakeCursor:
    def __init__(self, sink, results):
        self._sink = sink
        self._results = results  # list of return values for fetchone

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._sink.append(("sql", " ".join(str(sql).split())))

    def fetchone(self):
        return self._results.pop(0)


class _FakeConn:
    def __init__(self, sink):
        self._sink = sink

    def cursor(self):
        return _FakeCursor(self._sink, [])

    def commit(self):
        self._sink.append(("commit", None))

    def rollback(self):
        self._sink.append(("rollback", None))


class TestSuccessChainOrdering(unittest.TestCase):
    """Drive m4f_controller.stage_six_skill_run with recording fakes and
    assert the bind-first sequence and the absence of forbidden writes."""

    def test_bind_first_then_snapshot_then_six_skills(self):
        wc = str(ROOT / "tools" / "workflow-controller")
        sys.path.insert(0, wc)
        try:
            import m4f_controller
        finally:
            sys.path.remove(wc)

        events = []
        ctrl = _FakeConn(events)
        snap = _FakeConn(events)

        digests = {skill: "d-%s" % skill for skill in m4f_controller.SKILLS}

        calls = []

        def fake_put_requests(conn, run_id, trace_id, skill_inputs):
            calls.append("put_requests")
            return dict(digests), None

        def fake_complete_snapshot(conn, *, snapshot_job_id, run_id,
                                   trace_id, base_sha, head_sha,
                                   request_digests, worker_id, observer):
            calls.append(("complete_snapshot", snapshot_job_id))
            return "snap-fixed"

        # Program bind/enqueue results through a cursor that actually
        # returns fetchone values: patch _FakeCursor usage by monkeypatching
        # the module-level SQL result sequence.
        bind_results = [("rev-fixed",), ("snapjob-run",)] + \
            [("sj-%d" % i,) for i in range(len(m4f_controller.SKILLS))]

        real_cursor = _FakeCursor

        class _ResultConn(_FakeConn):
            def __init__(self, sink):
                super().__init__(sink)
                self._results = list(bind_results)  # SHARED sequence

            def cursor(self):
                return _FakeCursor(self._sink, self._results)

        ctrl = _ResultConn(events)
        snap = _FakeConn(events)

        old_put = m4f_controller._put_requests
        old_complete = m4f_controller._complete_snapshot
        m4f_controller._put_requests = fake_put_requests
        m4f_controller._complete_snapshot = fake_complete_snapshot
        try:
            staged = m4f_controller.stage_six_skill_run(
                ctrl, snap,
                run_id="run-fixture", trace_id="trace-fixture",
                repo=stub.FIXTURE_REPO, pr_number=stub.FIXTURE_PR_NUMBER,
                base_sha=stub.FIXTURE_BASE_SHA,
                head_sha=stub.FIXTURE_HEAD_SHA,
                source_call_id="mcp-fixture-001",
                source_evidence_digest="a" * 64,
                skill_inputs={"diff-parse": {"placeholder": True}},
                snapshot_worker_id="fixture-worker",
            )
        finally:
            m4f_controller._put_requests = old_put
            m4f_controller._complete_snapshot = old_complete

        self.assertEqual(staged.revision_binding_id, "rev-fixed")
        self.assertEqual(staged.snapshot_job_id, "snapjob-run")
        self.assertEqual(staged.snapshot_id, "snap-fixed")
        self.assertEqual(len(staged.skill_job_ids),
                         len(m4f_controller.SKILLS))

        sqls = [s for kind, s in events if kind == "sql"]
        self.assertTrue(sqls, "expected SQL calls")
        # bind FIRST
        self.assertIn("SELECT public.bind_revision", sqls[0])
        # snapshot job enqueued after bind
        idx_bind = 0
        idx_snap = next(i for i, s in enumerate(sqls)
                        if "public.enqueue_snapshot_job" in s)
        self.assertGreater(idx_snap, idx_bind)
        # six skill jobs after snapshot completion (fake order marker)
        self.assertIn("complete_snapshot", [c if isinstance(c, str) else c[0]
                                            for c in calls])
        skill_calls = [i for i, s in enumerate(sqls)
                       if "public.enqueue_skill_job" in s]
        self.assertEqual(len(skill_calls), len(m4f_controller.SKILLS))
        self.assertTrue(all(i > idx_snap for i in skill_calls))
        # commit boundaries present
        commits = [e for kind, e in events if kind == "commit"]
        self.assertGreaterEqual(len(commits), 3)

    def test_no_direct_writes_to_forbidden_tables(self):
        # the staging path must only touch contract functions (no raw
        # INSERT into revision_bindings / audit_events / mcp_calls)
        src = (ROOT / "tools" / "workflow-controller" / "m4f_controller.py")\
            .read_text(encoding="utf-8")
        for table in ("INSERT INTO public.revision_bindings",
                      "INSERT INTO public.audit_events",
                      "INSERT INTO public.mcp_calls"):
            self.assertNotIn(table, src, table)
        self.assertIn("SELECT public.bind_revision", src)


# ── 4: A1 semantics untouched + no promotion ────────────────────────────────

class TestA1SemanticsAndBoundaries(unittest.TestCase):

    def test_controller_retry_terminal_logic_unchanged(self):
        src = (ROOT / "tools" / "workflow-controller" / "controller.py")\
            .read_text(encoding="utf-8")
        self.assertIn(
            'terminal = permanent or attempt >= M4F_EVENT_MAX_ATTEMPTS', src)
        self.assertIn('"ERROR" if terminal else "M4F_PENDING"', src)
        self.assertIn('M4F_EVENT_MAX_ATTEMPTS = int(os.environ.get('
                      '"M4F_EVENT_MAX_ATTEMPTS", "5"))', src)

    def test_stub_does_not_import_controller_or_m4f(self):
        for mod in ("controller", "m4f_ingress", "m4f_controller",
                    "gateway_client"):
            self.assertNotIn("import %s" % mod, STUB_SOURCE, mod)

    def test_frozen_truth_constants_unchanged(self):
        em = (ROOT / "tools" / "demo_console" / "evidence_manifest.py")\
            .read_text(encoding="utf-8")
        self.assertIn("revision_producer_contract=NOT_VERIFIED", em)
        self.assertIn("audit_producer_contract=NOT_VERIFIED", em)
        for banned in ("application_integration_verified=true",
                       "database_verified=true",
                       "production_verified=true",
                       "revision_producer_contract=VERIFIED",
                       "audit_producer_contract=VERIFIED"):
            self.assertNotIn(banned, STUB_SOURCE + em, banned)

    def test_fixture_disclosed_as_non_production(self):
        self.assertIn("NOT production data", STUB_SOURCE)
        self.assertIn("does NOT verify any", STUB_SOURCE)
        self.assertIn("read-only", STUB_SOURCE)


if __name__ == "__main__":
    unittest.main()
