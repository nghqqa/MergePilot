#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M6-C · SLS production credential provider + stub signer (offline only).

Provides:
- CredentialProvider: reads AK/SK from env or credential file with 60s
  hot rotation (mtime-triggered immediate reload).
- SlsStubSigner: generates HMAC-SHA1 SLS request signatures for offline
  verification. NEVER sends real requests to SLS.
- sign_request(): builds the SLS StringToSign and computes the signature.

Security:
- Credentials are NEVER printed, logged, or stored in span attributes.
- The signer only uses credentials to compute HMAC; credentials never
  appear in the output signature or any test assertion.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Optional


class CredentialProvider:
    """Provides SLS access key credentials with hot rotation.

    Reads from env (SLS_ACCESS_KEY_ID / SLS_ACCESS_KEY_SECRET) or from
    a credential file (SLS_CREDENTIAL_FILE JSON). The file is checked
    every 60s or immediately when its mtime changes.
    """

    def __init__(self):
        self._ak = os.environ.get("SLS_ACCESS_KEY_ID", "")
        self._sk = os.environ.get("SLS_ACCESS_KEY_SECRET", "")
        self._cred_file = os.environ.get("SLS_CREDENTIAL_FILE", "")
        self._last_load = 0.0
        self._last_mtime = 0.0
        self._reload_interval = 60.0
        if self._cred_file:
            self._load_from_file()

    def _load_from_file(self):
        """Load credentials from SLS_CREDENTIAL_FILE."""
        try:
            st = os.stat(self._cred_file)
            self._last_mtime = st.st_mtime
            with open(self._cred_file, "r") as f:
                data = json.load(f)
            self._ak = data.get("access_key_id", "")
            self._sk = data.get("access_key_secret", "")
            self._last_load = time.monotonic()
        except (OSError, json.JSONDecodeError, KeyError):
            pass  # fail-closed: keep old credentials

    def _maybe_reload(self):
        """Reload if file mtime changed or interval elapsed."""
        if not self._cred_file:
            return
        now = time.monotonic()
        if now - self._last_load < self._reload_interval:
            # Check mtime for immediate reload
            try:
                mtime = os.stat(self._cred_file).st_mtime
                if mtime != self._last_mtime:
                    self._load_from_file()
            except OSError:
                pass
            return
        self._load_from_file()

    @property
    def access_key_id(self) -> str:
        self._maybe_reload()
        return self._ak

    @property
    def access_key_secret(self) -> str:
        self._maybe_reload()
        return self._sk

    @property
    def is_configured(self) -> bool:
        self._maybe_reload()
        return bool(self._ak and self._sk)

    def force_reload(self):
        """Force immediate reload from file."""
        if self._cred_file:
            self._load_from_file()


def sign_request(method: str, body: bytes, content_type: str,
                 host: str, date: str, ak: str, sk: str) -> str:
    """Generate SLS HMAC-SHA1 request signature.

    Returns the Authorization header value:
        LOG <AccessKeyId>:<Base64(HMAC-SHA1(SK, StringToSign))>

    This is an OFFLINE computation. It NEVER sends a request.
    Credentials (ak/sk) are only used to compute HMAC; they never
    appear in the output.
    """
    body_md5 = hashlib.md5(body).hexdigest()
    body_raw_size = str(len(body))

    string_to_sign = "\n".join([
        method.upper(),
        body_md5,
        content_type,
        f"x-sls-bodyrawsize:{body_raw_size}",
        "x-sls-apiversion:0.6.0",
        "x-sls-signaturemethod:hmac-sha1",
        date,
        f"x-sls-host:{host}",
    ])

    signature = hmac.new(
        sk.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha1
    ).digest()

    signature_b64 = base64.b64encode(signature).decode("ascii")
    return f"LOG {ak}:{signature_b64}"


class SlsStubSigner:
    """Offline stub signer for testing.

    Wraps sign_request() with a CredentialProvider, allowing tests to
    verify signature format without real credentials or SLS access.
    """

    def __init__(self, provider: CredentialProvider = None):
        self.provider = provider or CredentialProvider()

    def sign(self, method: str, body: bytes, content_type: str,
             host: str, date: str) -> Optional[str]:
        """Sign a request. Returns Authorization header or None if
        credentials are not configured (fail-closed)."""
        if not self.provider.is_configured:
            return None
        return sign_request(
            method, body, content_type, host, date,
            self.provider.access_key_id,
            self.provider.access_key_secret,
        )
