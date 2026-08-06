#!/usr/bin/env python3
"""Negative test client for the M5-0C gateway gate.

Lives in the repo (tests/m5_0c/) so it is visible at /workspace/tests/m5_0c/...
inside the client container via the harness's `-v $ROOT_WSL:/workspace:ro`
mount — NO host-/tmp dependency. Behavior is selected by M5C_NEGATIVE_MODE:

  rc1_true      -> write a VALID JSON payload with all_passed=true, flush, exit 1
  empty         -> write nothing, flush, exit 0
  invalid_json  -> write a clearly-invalid JSON string, flush, exit 0

Used (via the harness M5C_CLIENT_SCRIPT injection) to PROVE the gate's
fail-closed logic per mode — not to drive the real Gateway. The gate must
classify each output precisely (client_output_state) and fail-closed regardless
of any payload all_passed claim.
"""
from __future__ import annotations

import json
import os
import sys

MODE = os.environ.get("M5C_NEGATIVE_MODE", "").strip()


def _emit(s: str) -> None:
    sys.stdout.write(s)
    sys.stdout.flush()


if MODE == "rc1_true":
    _emit(
        json.dumps(
            {"all_passed": True, "scenarios": 17, "passed": 17, "failed": 0, "results": []}
        )
        + "\n"
    )
    sys.exit(1)
elif MODE == "empty":
    sys.stdout.flush()
    sys.exit(0)
elif MODE == "invalid_json":
    _emit('{"all_passed": true, "scenarios": 17, oops this is not closed\n')
    sys.exit(0)
else:
    sys.stderr.write(
        "negative client: unknown/empty M5C_NEGATIVE_MODE=%r "
        "(expected rc1_true|empty|invalid_json)\n" % MODE
    )
    sys.exit(2)
