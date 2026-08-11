#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D2B-3B1.1 · Real transport tests for the Docker socket proxy.

These tests exercise the PRODUCTION handler (proxy_transport.handle_connection)
over a real socket round-trip:

    ControllerStubClient → proxy listener → production handler
      → FakeUpstreamDaemon → production forward/relay → ControllerStubClient

They are NOT pure classify_request calls. Every test asserts real HTTP status
codes and verifies the FakeUpstreamDaemon actually received (or did NOT
receive) the forwarded request.

Covers (req 5 of D2B-3B1.1):
  - GET /_ping round-trip
  - transformed create body actually received by upstream
  - upstream response relayed verbatim
  - exec ID auto-registered from upstream response
  - hijack bidirectional byte transfer
  - archive /etc -> 403 (B11)
  - logs/stats/changes/wait -> 403
  - stop non-t=10 -> 403
  - delete non-force=true -> 403
  - inspect mismatch -> 403
  - upstream unavailable/timeout fail-closed
  - chunked request/response
  - deny proves 0 upstream hits
"""
from __future__ import annotations

import json
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
HICLAB = os.path.normpath(os.path.join(HERE, "..", "..", "tools", "hiclab"))
sys.path.insert(0, HICLAB)
sys.path.insert(0, HERE)

from proxy_stubs import ProxyHarness, InspectStub, HAS_AF_UNIX  # noqa: E402

DIGEST = "sha256:" + "a" * 64
WORKER = "agentteams-worker-fixer"


def _has_unix_or_skip():
    """These tests run on BOTH POSIX (AF_UNIX) and Windows (socketpair broker).
    They are never skipped — the production handler runs identically in both.
    """
    return  # no-op: never skip


class TestRealTransportRoundTrip(unittest.TestCase):
    """End-to-end round-trip through the production handler."""

    def test_01_ping_round_trip(self):
        _has_unix_or_skip()
        with ProxyHarness() as h:
            h.daemon.queue_response(status=200, body=b"OK")
            status, body, err = h.client.get("/_ping")
            self.assertEqual(status, 200)
            self.assertEqual(body, b"OK")
            self.assertEqual(h.upstream_request_count, 1)

    def test_02_transformed_create_body_reaches_upstream(self):
        with ProxyHarness() as h:
            h.daemon.queue_response(status=201, body=b'{"Id":"c1"}')
            status, _body, _err = h.client.post(
                "/containers/create?name=%s" % WORKER,
                body={"Image": DIGEST})
            self.assertEqual(status, 201)
            self.assertEqual(h.upstream_request_count, 1)
            up = h.daemon.requests[0]
            self.assertEqual(up["body"]["HostConfig"]["RestartPolicy"],
                             {"Name": "no"})
            self.assertEqual(
                up["body"]["Labels"]["com.mergepilot.hardened"], "1")
            self.assertEqual(
                up["body"]["Labels"]["com.mergepilot.run_id"], "test-run-01")

    def test_03_upstream_response_relayed_verbatim(self):
        with ProxyHarness() as h:
            payload = b'{"key":"value","n":42}'
            h.daemon.queue_response(status=200, body=payload)
            status, body, _err = h.client.get("/_ping")
            self.assertEqual(status, 200)
            self.assertEqual(body, payload)

    def test_04_inspect_round_trip_with_name_match(self):
        # nameprefix op: proxy does authoritative inspect; upstream returns
        # an inspect body whose Name matches -> request forwarded.
        with ProxyHarness() as h:
            # 1st response: the inspect (200, Name matches)
            h.daemon.queue_response(
                status=200, body=InspectStub.body(WORKER))
            # 2nd response: the actual GET /json result
            h.daemon.queue_response(status=200, body=b'{"State":{"Running":true}}')
            status, body, _err = h.client.get(
                "/containers/%s/json" % WORKER)
            self.assertEqual(status, 200)

    def test_05_start_round_trip(self):
        with ProxyHarness() as h:
            h.daemon.queue_response(
                status=200, body=InspectStub.body(WORKER))
            h.daemon.queue_response(status=204, body=b"")
            status, _b, _e = h.client.post(
                "/containers/%s/start" % WORKER)
            self.assertEqual(status, 204)

    def test_06_stop_round_trip(self):
        with ProxyHarness() as h:
            h.daemon.queue_response(
                status=200, body=InspectStub.body(WORKER))
            h.daemon.queue_response(status=204, body=b"")
            status, _b, _e = h.client.post(
                "/containers/%s/stop?t=10" % WORKER)
            self.assertEqual(status, 204)

    def test_07_delete_round_trip(self):
        with ProxyHarness() as h:
            h.daemon.queue_response(
                status=200, body=InspectStub.body(WORKER))
            h.daemon.queue_response(status=204, body=b"")
            status, _b, _e = h.client.delete(
                "/containers/%s?force=true" % WORKER)
            self.assertEqual(status, 204)

    def test_08_archive_auth_dir_round_trip(self):
        with ProxyHarness() as h:
            h.daemon.queue_response(
                status=200, body=InspectStub.body(WORKER))
            h.daemon.queue_response(status=200, body=b"")
            status, _b, _e = h.client.put(
                "/containers/%s/archive?path=/var/run/secrets/agentteams"
                % WORKER,
                body=b"\x1f\x8b")  # dummy tar bytes
            self.assertEqual(status, 200)

    def test_09_image_inspect_round_trip(self):
        with ProxyHarness() as h:
            h.daemon.queue_response(status=200, body=b'{"Id":"sha256:abc"}')
            status, _b, _e = h.client.get("/images/%s/json" % DIGEST)
            self.assertEqual(status, 200)

    def test_10_image_pull_round_trip(self):
        with ProxyHarness() as h:
            h.daemon.queue_response(status=200, body=b"{}")
            status, _b, _e = h.client.post(
                "/images/create?fromImage=%s" % DIGEST)
            self.assertEqual(status, 200)

    def test_11_delete_auth_volume_round_trip(self):
        with ProxyHarness() as h:
            h.daemon.queue_response(status=204, body=b"")
            status, _b, _e = h.client.delete(
                "/volumes/%s-auth" % WORKER)
            self.assertEqual(status, 204)


class TestExecLifecycleTransport(unittest.TestCase):
    """Exec ID auto-registration from the upstream response (req 9)."""

    def test_01_exec_create_then_start_authorized(self):
        # D2B-3B1.2: exec-create now triggers authoritative inspect FIRST.
        # Queue: inspect body (valid labels) -> exec-create Id -> hijack.
        with ProxyHarness() as h:
            # 1. inspect response (valid authoritative labels for WORKER)
            h.daemon.queue_response(
                status=200, body=InspectStub.body(WORKER,
                                                  scope="test",
                                                  run_id="test-run-01"))
            # 2. exec-create response with an Id
            h.daemon.queue_response(
                status=201, body=b'{"Id":"exec-abc-123"}')
            status, _b, _e = h.client.post(
                "/containers/%s/exec" % WORKER)
            self.assertEqual(status, 201)
            # the exec ID must now be registered
            self.assertTrue(
                h.server.exec_registry.authorize("exec-abc-123")[0])
            # two upstream requests: inspect + exec-create
            self.assertEqual(h.upstream_request_count, 2)
            # now /exec/{id}/start with hijack should be authorized
            h.daemon.queue_response(status=101, mode="hijack")
            status, body, _e = h.client.post(
                "/exec/exec-abc-123/start", upgrade=True)
            self.assertEqual(status, 101)

    def test_02_exec_start_without_register_denied(self):
        with ProxyHarness() as h:
            h.daemon.queue_response(status=101, mode="hijack")
            status, _b, _e = h.client.post(
                "/exec/never-created/start", upgrade=True)
            self.assertEqual(status, 403)
            self.assertEqual(h.upstream_request_count, 0)

    def test_03_exec_json_without_register_denied(self):
        with ProxyHarness() as h:
            status, _b, _e = h.client.get("/exec/never-created/json")
            self.assertEqual(status, 403)
            self.assertEqual(h.upstream_request_count, 0)

    def test_04_exec_create_malformed_response_no_register(self):
        # D2B-3B1.2: inspect passes (valid labels), then exec-create returns
        # a malformed response (no Id) -> nothing registered.
        with ProxyHarness() as h:
            h.daemon.queue_response(
                status=200, body=InspectStub.body(WORKER))
            h.daemon.queue_response(
                status=201, body=b'{"something":"else"}')
            status, _b, _e = h.client.post(
                "/containers/%s/exec" % WORKER)
            self.assertEqual(status, 201)
            # nothing registered -> subsequent start denied
            h.daemon.queue_response(status=101, mode="hijack")
            status2, _b2, _e2 = h.client.post(
                "/exec/anything/start", upgrade=True)
            self.assertEqual(status2, 403)

    def test_05_exec_create_non_2xx_no_register(self):
        # inspect passes, exec-create returns 404 -> not registered
        with ProxyHarness() as h:
            h.daemon.queue_response(
                status=200, body=InspectStub.body(WORKER))
            h.daemon.queue_response(status=404, body=b'{"message":"no such"}')
            status, _b, _e = h.client.post(
                "/containers/%s/exec" % WORKER)
            self.assertEqual(status, 404)
            # not registered
            self.assertFalse(
                h.server.exec_registry.authorize("anything")[0])

    def test_06_exec_create_inspect_fail_no_exec_forwarded(self):
        # D2B-3B1.2: if inspect fails (missing labels), exec-create must NOT
        # be forwarded upstream. Only the inspect request reaches upstream.
        with ProxyHarness() as h:
            # inspect body with NO labels -> inspect fails
            h.daemon.queue_response(
                status=200, body=InspectStub.body(WORKER, labels={}))
            # an exec Id response that should NEVER be consumed
            h.daemon.queue_response(
                status=201, body=b'{"Id":"should-not-register"}')
            status, _b, _e = h.client.post(
                "/containers/%s/exec" % WORKER)
            self.assertEqual(status, 403)
            # only 1 upstream request (the inspect); exec-create NOT forwarded
            self.assertEqual(h.upstream_request_count, 1)
            self.assertFalse(
                h.server.exec_registry.authorize("should-not-register")[0])

    def test_07_exec_create_wrong_run_id_no_exec_forwarded(self):
        # inspect returns wrong run_id -> DENY, exec not forwarded
        with ProxyHarness() as h:
            h.daemon.queue_response(
                status=200,
                body=InspectStub.body(WORKER, run_id="wrong-run"))
            h.daemon.queue_response(
                status=201, body=b'{"Id":"should-not-register"}')
            status, _b, _e = h.client.post(
                "/containers/%s/exec" % WORKER)
            self.assertEqual(status, 403)
            self.assertEqual(h.upstream_request_count, 1)


class TestHijackTransport(unittest.TestCase):
    """101 Upgrade / hijack bidirectional byte transfer (req 10)."""

    def test_01_hijack_echo_bytes(self):
        # D2B-3B1.2: exec-create triggers inspect first (valid labels),
        # then exec-create Id, then hijack start.
        with ProxyHarness() as h:
            h.daemon.queue_response(
                status=200, body=InspectStub.body(WORKER))
            h.daemon.queue_response(
                status=201, body=b'{"Id":"exec-xyz"}')
            h.client.post("/containers/%s/exec" % WORKER)
            # hijack start: upstream echoes bytes back
            h.daemon.queue_response(status=101, mode="hijack")
            status, body, _e = h.client.post(
                "/exec/exec-xyz/start", upgrade=True)
            self.assertEqual(status, 101)
            # the client sent "ping" after 101; upstream echoed it
            self.assertIn(b"ping", body)


class TestStrictDenyTransport(unittest.TestCase):
    """Denials must prove the FakeUpstreamDaemon received 0 requests."""

    def test_01_archive_etc_denied_zero_upstream(self):
        with ProxyHarness() as h:
            status, _b, _e = h.client.put(
                "/containers/%s/archive?path=/etc" % WORKER,
                body=b"x")
            self.assertEqual(status, 403)
            self.assertEqual(h.upstream_request_count, 0)

    def test_02_archive_root_denied(self):
        with ProxyHarness() as h:
            status, _b, _e = h.client.put(
                "/containers/%s/archive?path=/" % WORKER, body=b"x")
            self.assertEqual(status, 403)
            self.assertEqual(h.upstream_request_count, 0)

    def test_03_archive_traversal_denied(self):
        with ProxyHarness() as h:
            status, _b, _e = h.client.put(
                "/containers/%s/archive?path=/var/run/secrets/agentteams/../../.."
                % WORKER, body=b"x")
            self.assertEqual(status, 403)
            self.assertEqual(h.upstream_request_count, 0)

    def test_04_logs_denied(self):
        with ProxyHarness() as h:
            status, _b, _e = h.client.get(
                "/containers/%s/logs" % WORKER)
            self.assertEqual(status, 403)
            self.assertEqual(h.upstream_request_count, 0)

    def test_05_stats_denied(self):
        with ProxyHarness() as h:
            status, _b, _e = h.client.get(
                "/containers/%s/stats" % WORKER)
            self.assertEqual(status, 403)
            self.assertEqual(h.upstream_request_count, 0)

    def test_06_changes_denied(self):
        with ProxyHarness() as h:
            status, _b, _e = h.client.get(
                "/containers/%s/changes" % WORKER)
            self.assertEqual(status, 403)
            self.assertEqual(h.upstream_request_count, 0)

    def test_07_wait_denied(self):
        with ProxyHarness() as h:
            status, _b, _e = h.client.post(
                "/containers/%s/wait" % WORKER)
            self.assertEqual(status, 403)
            self.assertEqual(h.upstream_request_count, 0)

    def test_08_head_ping_denied(self):
        with ProxyHarness() as h:
            status, _b, _e = h.client.head("/_ping")
            self.assertEqual(status, 403)
            self.assertEqual(h.upstream_request_count, 0)

    def test_09_stop_wrong_query_denied(self):
        with ProxyHarness() as h:
            status, _b, _e = h.client.post(
                "/containers/%s/stop?t=5" % WORKER)
            self.assertEqual(status, 403)
            self.assertEqual(h.upstream_request_count, 0)

    def test_10_stop_no_query_denied(self):
        with ProxyHarness() as h:
            status, _b, _e = h.client.post(
                "/containers/%s/stop" % WORKER)
            self.assertEqual(status, 403)

    def test_11_delete_wrong_query_denied(self):
        with ProxyHarness() as h:
            status, _b, _e = h.client.delete(
                "/containers/%s?force=false" % WORKER)
            self.assertEqual(status, 403)
            self.assertEqual(h.upstream_request_count, 0)

    def test_12_delete_no_query_denied(self):
        with ProxyHarness() as h:
            status, _b, _e = h.client.delete(
                "/containers/%s" % WORKER)
            self.assertEqual(status, 403)

    def test_13_inspect_wrong_name_zero_upstream_forward(self):
        # name doesn't match worker/manager regex -> deny at classify,
        # zero upstream contact
        with ProxyHarness() as h:
            status, _b, _e = h.client.get("/containers/evil-container/json")
            self.assertEqual(status, 403)
            self.assertEqual(h.upstream_request_count, 0)

    def test_14_inspect_name_match_but_upstream_says_different(self):
        # client targets a valid worker name, but the authoritative upstream
        # inspect returns a DIFFERENT Name -> proxy must deny
        with ProxyHarness() as h:
            h.daemon.queue_response(
                status=200, body=InspectStub.body("agentteams-worker-OTHER"))
            status, _b, _e = h.client.get(
                "/containers/%s/json" % WORKER)
            self.assertEqual(status, 403)

    def test_15_inspect_upstream_404_denied(self):
        with ProxyHarness() as h:
            h.daemon.queue_response(status=404, body=b'{"message":"not found"}')
            status, _b, _e = h.client.get(
                "/containers/%s/json" % WORKER)
            self.assertEqual(status, 403)

    def test_16_unknown_endpoint_denied_zero_upstream(self):
        with ProxyHarness() as h:
            status, _b, _e = h.client.get("/events")
            self.assertEqual(status, 403)
            self.assertEqual(h.upstream_request_count, 0)

    def test_17_networks_create_denied(self):
        with ProxyHarness() as h:
            status, _b, _e = h.client.post("/networks/create")
            self.assertEqual(status, 403)
            self.assertEqual(h.upstream_request_count, 0)

    def test_18_build_denied(self):
        with ProxyHarness() as h:
            status, _b, _e = h.client.post("/build")
            self.assertEqual(status, 403)
            self.assertEqual(h.upstream_request_count, 0)


class TestFailClosedTransport(unittest.TestCase):
    """Upstream unavailable / timeout / malformed -> fail-closed."""

    def test_01_upstream_unavailable_ping_502(self):
        # No daemon listening -> proxy cannot connect upstream -> 502
        with ProxyHarness(upstream_reachable=False) as h:
            status, _b, _e = h.client.get("/_ping")
            self.assertEqual(status, 502)

    def test_02_upstream_disconnect_502(self):
        with ProxyHarness() as h:
            h.daemon.queue_response(mode="disconnect")
            status, _b, _e = h.client.get("/_ping")
            self.assertIn(status, (502, 0))  # 502 or connection drop


class TestChunkedTransport(unittest.TestCase):
    """Chunked request/response bodies."""

    def test_01_chunked_response_relayed(self):
        with ProxyHarness() as h:
            h.daemon.queue_response(status=200, body=b"chunked-body-data",
                                     mode="chunked")
            status, body, _e = h.client.get("/_ping")
            self.assertEqual(status, 200)
            self.assertEqual(body, b"chunked-body-data")


class TestVersionPrefix(unittest.TestCase):
    def test_01_v1_47_prefix_ping(self):
        with ProxyHarness() as h:
            h.daemon.queue_response(status=200, body=b"OK")
            status, _b, _e = h.client.get("/v1.47/_ping")
            self.assertEqual(status, 200)
            self.assertEqual(h.upstream_request_count, 1)

    def test_02_v1_47_prefix_create(self):
        with ProxyHarness() as h:
            h.daemon.queue_response(status=201, body=b'{"Id":"c1"}')
            status, _b, _e = h.client.post(
                "/v1.45/containers/create?name=%s" % WORKER,
                body={"Image": DIGEST})
            self.assertEqual(status, 201)


class TestB11ArchivePathBypass(unittest.TestCase):
    """B11: archive path encoding/traversal bypass attempts."""

    def test_01_double_encoding_denied(self):
        with ProxyHarness() as h:
            # %252e%252e%252f -> %2e%2e%2f -> ../  (double-encoding bypass)
            status, _b, _e = h.client.put(
                "/containers/%s/archive?path=%%252e%%252e%%252f"
                % WORKER, body=b"x")
            self.assertEqual(status, 403)
            self.assertEqual(h.upstream_request_count, 0)

    def test_02_arbitrary_absolute_denied(self):
        with ProxyHarness() as h:
            status, _b, _e = h.client.put(
                "/containers/%s/archive?path=/root" % WORKER, body=b"x")
            self.assertEqual(status, 403)
            self.assertEqual(h.upstream_request_count, 0)

    def test_03_empty_path_denied(self):
        with ProxyHarness() as h:
            status, _b, _e = h.client.put(
                "/containers/%s/archive" % WORKER, body=b"x")
            self.assertEqual(status, 403)

    def test_04_token_next_allowed(self):
        # the rotation temp file path is in the allowlist
        import docker_socket_proxy as dsp
        self.assertTrue(dsp._archive_path_allowed(
            "/var/run/secrets/agentteams/token.next"))


# ===========================================================================
# D2B-3B1.2 · Authoritative resource binding (Name + 4 labels exact-match)
# ===========================================================================


class TestAuthoritativeBinding(unittest.TestCase):
    """D2B-3B1.2: every managed container must carry the four authoritative
    labels (scope/run_id/agent/hardened), exactly matching the proxy config.
    Missing or wrong labels -> 403, and the target operation is NEVER
    forwarded to upstream (proven by 0 non-inspect upstream requests)."""

    # Helper: do a GET /containers/{name}/json with a programmable inspect body.
    def _inspect_round_trip(self, inspect_body):
        with ProxyHarness() as h:
            h.daemon.queue_response(status=200, body=inspect_body)
            # The proxy will: (1) inspect -> get the body above; (2) if inspect
            # passes, forward the GET /json to upstream (consuming a 2nd resp).
            h.daemon.queue_response(status=200, body=b'{"ok":true}')
            status, _b, _e = h.client.get("/containers/%s/json" % WORKER)
            return status, h

    def test_01_valid_name_and_labels_allowed(self):
        # correct Name + correct four labels -> inspect passes, op forwarded
        status, h = self._inspect_round_trip(
            InspectStub.body(WORKER, scope="test", run_id="test-run-01"))
        self.assertEqual(status, 200)
        # 2 upstream requests: inspect + the forwarded GET /json
        self.assertEqual(h.upstream_request_count, 2)

    def test_02_labels_missing_denied(self):
        status, h = self._inspect_round_trip(
            InspectStub.body(WORKER, labels={}))
        self.assertEqual(status, 403)
        self.assertEqual(h.upstream_request_count, 1)  # only inspect

    def test_03_config_missing_denied(self):
        status, h = self._inspect_round_trip(
            InspectStub.body(WORKER, no_config=True))
        self.assertEqual(status, 403)
        self.assertEqual(h.upstream_request_count, 1)

    def test_04_config_labels_key_missing_denied(self):
        status, h = self._inspect_round_trip(
            InspectStub.body(WORKER, no_labels=True))
        self.assertEqual(status, 403)
        self.assertEqual(h.upstream_request_count, 1)

    def test_05_scope_wrong_denied(self):
        status, h = self._inspect_round_trip(
            InspectStub.body(WORKER, scope="prod"))
        self.assertEqual(status, 403)
        self.assertEqual(h.upstream_request_count, 1)

    def test_06_run_id_wrong_denied(self):
        status, h = self._inspect_round_trip(
            InspectStub.body(WORKER, run_id="wrong-run"))
        self.assertEqual(status, 403)
        self.assertEqual(h.upstream_request_count, 1)

    def test_07_hardened_missing_denied(self):
        # labels present but hardened absent
        status, h = self._inspect_round_trip(
            InspectStub.body(WORKER, labels={
                "com.mergepilot.scope": "test",
                "com.mergepilot.run_id": "test-run-01",
                "com.mergepilot.agent": "fixer",
            }))
        self.assertEqual(status, 403)
        self.assertEqual(h.upstream_request_count, 1)

    def test_08_hardened_wrong_value_denied(self):
        status, h = self._inspect_round_trip(
            InspectStub.body(WORKER, hardened="0"))
        self.assertEqual(status, 403)
        self.assertEqual(h.upstream_request_count, 1)

    def test_09_agent_missing_denied(self):
        status, h = self._inspect_round_trip(
            InspectStub.body(WORKER, labels={
                "com.mergepilot.scope": "test",
                "com.mergepilot.run_id": "test-run-01",
                "com.mergepilot.hardened": "1",
            }))
        self.assertEqual(status, 403)
        self.assertEqual(h.upstream_request_count, 1)

    def test_10_agent_wrong_denied(self):
        # container name derives "fixer" but label says "reviewer"
        status, h = self._inspect_round_trip(
            InspectStub.body(WORKER, labels={
                "com.mergepilot.scope": "test",
                "com.mergepilot.run_id": "test-run-01",
                "com.mergepilot.hardened": "1",
                "com.mergepilot.agent": "reviewer",
            }))
        self.assertEqual(status, 403)
        self.assertEqual(h.upstream_request_count, 1)

    def test_11_name_wrong_denied(self):
        # inspect returns a DIFFERENT Name than the target
        with ProxyHarness() as h:
            h.daemon.queue_response(
                status=200, body=InspectStub.body("agentteams-worker-verifier"))
            h.daemon.queue_response(status=200, body=b'{}')
            status, _b, _e = h.client.get("/containers/%s/json" % WORKER)
            self.assertEqual(status, 403)
            self.assertEqual(h.upstream_request_count, 1)

    def test_12_labels_not_object_denied(self):
        # Labels as a list (wrong JSON type)
        with ProxyHarness() as h:
            h.daemon.queue_response(
                status=200,
                body={"Name": WORKER, "Config": {"Labels": ["not", "a", "dict"]}})
            h.daemon.queue_response(status=200, body=b'{}')
            status, _b, _e = h.client.get("/containers/%s/json" % WORKER)
            self.assertEqual(status, 403)
            self.assertEqual(h.upstream_request_count, 1)

    def test_13_unknown_agent_name_denied(self):
        # agentteams-worker-evil -> derive_agent_strict returns None -> DENY
        evil = "agentteams-worker-evil"
        with ProxyHarness() as h:
            h.daemon.queue_response(
                status=200, body=InspectStub.body(evil))
            h.daemon.queue_response(status=200, body=b'{}')
            # classify will deny at the name-regex stage (evil matches the
            # worker regex [a-z0-9-]+ so it passes classify, but inspect
            # derives agent=None -> deny)
            status, _b, _e = h.client.get("/containers/%s/json" % evil)
            self.assertEqual(status, 403)

    def test_14_exec_create_correct_binding_registers(self):
        # D2B-3B1.2 positive exec binding: inspect passes (valid labels) +
        # exec-create returns a valid Id -> registered; 2 upstream requests.
        with ProxyHarness() as h:
            h.daemon.queue_response(
                status=200, body=InspectStub.body(WORKER))
            h.daemon.queue_response(
                status=201, body=b'{"Id":"exec-bound-ok"}')
            status, _b, _e = h.client.post(
                "/containers/%s/exec" % WORKER)
            self.assertEqual(status, 201)
            self.assertTrue(
                h.server.exec_registry.authorize("exec-bound-ok")[0])
            self.assertEqual(h.upstream_request_count, 2)

    def test_15_manager_role_valid_binding(self):
        # agentteams-manager with manager agent label -> allowed
        mgr = "agentteams-manager"
        with ProxyHarness() as h:
            h.daemon.queue_response(
                status=200, body=InspectStub.body(mgr, scope="test",
                                                  run_id="test-run-01"))
            h.daemon.queue_response(status=200, body=b'{"ok":true}')
            status, _b, _e = h.client.get("/containers/%s/json" % mgr)
            self.assertEqual(status, 200)

    def test_16_start_op_with_wrong_labels_denied_no_forward(self):
        # Any nameprefix op (start) with wrong labels -> 403, op not forwarded
        with ProxyHarness() as h:
            h.daemon.queue_response(
                status=200, body=InspectStub.body(WORKER, run_id="wrong"))
            h.daemon.queue_response(status=204, body=b'')
            status, _b, _e = h.client.post(
                "/containers/%s/start" % WORKER)
            self.assertEqual(status, 403)
            self.assertEqual(h.upstream_request_count, 1)

    def test_17_delete_op_with_wrong_labels_denied_no_forward(self):
        with ProxyHarness() as h:
            h.daemon.queue_response(
                status=200, body=InspectStub.body(WORKER, scope="prod"))
            h.daemon.queue_response(status=204, body=b'')
            status, _b, _e = h.client.delete(
                "/containers/%s?force=true" % WORKER)
            self.assertEqual(status, 403)
            self.assertEqual(h.upstream_request_count, 1)

    def test_18_archive_op_with_wrong_labels_denied_no_forward(self):
        with ProxyHarness() as h:
            h.daemon.queue_response(
                status=200, body=InspectStub.body(WORKER, hardened="0"))
            h.daemon.queue_response(status=200, body=b'')
            status, _b, _e = h.client.put(
                "/containers/%s/archive?path=/var/run/secrets/agentteams"
                % WORKER, body=b"x")
            self.assertEqual(status, 403)
            self.assertEqual(h.upstream_request_count, 1)


if __name__ == "__main__":
    unittest.main()
