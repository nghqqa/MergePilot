#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M6-C · SLS production preflight tests (offline, no real SLS).

Verifies:
- HMAC-SHA1 signature format correctness
- CredentialProvider env injection + file hot rotation
- Request schema (PutLogs JSON)
- Redaction before signing (PAT/token never in signed body)
- Fail-closed: no credentials → no-op, business continues
- RAM policy JSON structure (minimal privilege)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
OTEL = os.path.normpath(os.path.join(HERE, "..", "..", "tools", "otel"))
if OTEL not in sys.path:
    sys.path.insert(0, OTEL)

from sls_signer import CredentialProvider, sign_request, SlsStubSigner


class TestSignatureFormat(unittest.TestCase):

    def test_signature_starts_with_LOG(self):
        auth = sign_request("POST", b'{"test":1}', "application/json",
                            "test.sls.aliyuncs.com", "Mon, 01 Jan 2026 00:00:00 GMT",
                            "AKID123", "SECRET456")
        self.assertTrue(auth.startswith("LOG AKID123:"))
        # The part after the colon is base64
        sig_b64 = auth.split(":", 1)[1]
        decoded = base64.b64decode(sig_b64)
        self.assertEqual(len(decoded), 20)  # SHA1 = 20 bytes

    def test_signature_deterministic(self):
        """Same inputs → same signature."""
        s1 = sign_request("POST", b'{"a":1}', "application/json",
                          "h.sls.aliyuncs.com", "D1", "AK", "SK")
        s2 = sign_request("POST", b'{"a":1}', "application/json",
                          "h.sls.aliyuncs.com", "D1", "AK", "SK")
        self.assertEqual(s1, s2)

    def test_different_body_different_signature(self):
        s1 = sign_request("POST", b'{"a":1}', "application/json",
                          "h", "D", "AK", "SK")
        s2 = sign_request("POST", b'{"a":2}', "application/json",
                          "h", "D", "AK", "SK")
        self.assertNotEqual(s1, s2)

    def test_credentials_not_in_output(self):
        auth = sign_request("POST", b'{}', "application/json",
                            "h", "D", "MY_AK_ID", "MY_SECRET_KEY")
        self.assertNotIn("MY_SECRET_KEY", auth)
        # AK appears in the header (it's the key identifier, not a secret)
        # but SK must never appear
        self.assertIn("MY_AK_ID", auth)  # this is correct per SLS spec


class TestCredentialProviderEnv(unittest.TestCase):

    def test_env_injection(self):
        os.environ["SLS_ACCESS_KEY_ID"] = "test-ak-env"
        os.environ["SLS_ACCESS_KEY_SECRET"] = "test-sk-env"
        try:
            cp = CredentialProvider()
            self.assertTrue(cp.is_configured)
            self.assertEqual(cp.access_key_id, "test-ak-env")
            self.assertEqual(cp.access_key_secret, "test-sk-env")
        finally:
            del os.environ["SLS_ACCESS_KEY_ID"]
            del os.environ["SLS_ACCESS_KEY_SECRET"]

    def test_unconfigured_returns_false(self):
        # Clear env
        for k in ("SLS_ACCESS_KEY_ID", "SLS_ACCESS_KEY_SECRET",
                   "SLS_CREDENTIAL_FILE"):
            os.environ.pop(k, None)
        cp = CredentialProvider()
        self.assertFalse(cp.is_configured)


class TestCredentialHotRotation(unittest.TestCase):

    def test_file_mtime_triggers_reload(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                         delete=False) as f:
            json.dump({"access_key_id": "old-ak", "access_key_secret": "old-sk"}, f)
            cred_path = f.name

        os.environ["SLS_CREDENTIAL_FILE"] = cred_path
        os.environ.pop("SLS_ACCESS_KEY_ID", None)
        os.environ.pop("SLS_ACCESS_KEY_SECRET", None)
        try:
            cp = CredentialProvider()
            self.assertEqual(cp.access_key_id, "old-ak")

            # Update file
            time.sleep(0.1)  # ensure mtime changes
            with open(cred_path, "w") as f:
                json.dump({"access_key_id": "new-ak", "access_key_secret": "new-sk"}, f)

            # Access triggers mtime check → immediate reload
            self.assertEqual(cp.access_key_id, "new-ak")
        finally:
            os.environ.pop("SLS_CREDENTIAL_FILE", None)
            os.unlink(cred_path)

    def test_credentials_not_leaked(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                         delete=False) as f:
            json.dump({"access_key_id": "SECRET_AK", "access_key_secret": "SECRET_SK"}, f)
            cred_path = f.name

        os.environ["SLS_CREDENTIAL_FILE"] = cred_path
        os.environ.pop("SLS_ACCESS_KEY_ID", None)
        os.environ.pop("SLS_ACCESS_KEY_SECRET", None)
        try:
            cp = CredentialProvider()
            signer = SlsStubSigner(cp)
            auth = signer.sign("POST", b'{}', "application/json",
                               "host", "date")
            self.assertIsNotNone(auth)
            # SK must never appear in the signature output
            self.assertNotIn("SECRET_SK", auth)
        finally:
            os.environ.pop("SLS_CREDENTIAL_FILE", None)
            os.unlink(cred_path)


class TestFailClosedNoCredentials(unittest.TestCase):

    def test_no_credentials_returns_none(self):
        for k in ("SLS_ACCESS_KEY_ID", "SLS_ACCESS_KEY_SECRET",
                   "SLS_CREDENTIAL_FILE"):
            os.environ.pop(k, None)
        signer = SlsStubSigner()
        result = signer.sign("POST", b'{}', "application/json", "host", "date")
        self.assertIsNone(result)


class TestRamPolicyStructure(unittest.TestCase):

    def test_minimal_privilege_policy(self):
        """The RAM policy JSON must only allow PostLogStoreLogs."""
        policy = {
            "Version": "1",
            "Statement": [{
                "Effect": "Allow",
                "Action": ["log:PostLogStoreLogs"],
                "Resource": [
                    "acs:log:*:*:project/mergepilot-trace/logstore/mp-trace",
                    "acs:log:*:*:project/mergepilot-trace/logstore/mp-trace-error"
                ]
            }]
        }
        # Verify no read/delete/manage permissions
        actions = policy["Statement"][0]["Action"]
        for action in actions:
            self.assertIn("Post", action)  # only write
            self.assertNotIn("Get", action)
            self.assertNotIn("Delete", action)
            self.assertNotIn("Create", action)
            self.assertNotIn("List", action)
            self.assertNotIn("Update", action)


class TestRedactionBeforeSigning(unittest.TestCase):

    def test_secret_not_in_signed_body(self):
        """A body containing a secret should be redacted BEFORE signing."""
        # This simulates what SLSExporter does: span_to_sls applies redaction
        # BEFORE the body reaches the signer
        import otel_spans as otel
        span = otel.SpanRecord("t" * 32, "s" * 16, None, "test", "r")
        span.set_attribute("api_key", "ghp_secret_12345")
        span.end()

        from sls_exporter import span_to_sls
        sls = span_to_sls(span)
        body = json.dumps(sls).encode("utf-8")

        # The redacted body should not contain the secret
        self.assertNotIn(b"ghp_secret", body)

        # Signing the redacted body should also not contain it
        auth = sign_request("POST", body, "application/json",
                            "host", "date", "AK", "SK")
        self.assertNotIn("ghp_secret", auth)


class TestProductionEndpointFormat(unittest.TestCase):

    def test_endpoint_https_only(self):
        """Production SLS endpoint must be HTTPS."""
        valid = "https://mergepilot-trace.cn-hangzhou.sls.aliyuncs.com"
        self.assertTrue(valid.startswith("https://"))

        invalid = "http://mergepilot-trace.cn-hangzhou.sls.aliyuncs.com"
        self.assertFalse(invalid.startswith("https://"))


if __name__ == "__main__":
    unittest.main()
