#!/usr/bin/env python3
"""Detect whether Docker ``--storage-opt size=`` actually works.

DESIGN (P1 fix): the default is UNSUPPORTED. The probe NEVER statically
declares support based on the backing filesystem name (ext4 is NOT
assumed to support quota -- quota must be enabled and established, which
is deployment-specific). Support is established ONLY by running a real
disposable container with ``--storage-opt size=1g`` and observing success.

The disposable probe container:
  * ``--rm`` (auto-cleanup on exit)
  * ``--network none`` (no network access, cannot touch production nets)
  * ``--label com.mergepilot.scope=storageopt-probe`` (precisely identifiable
    for a belt-and-suspenders cleanup sweep)
  * a precise generated name ``mp-storageopt-probe-<nonce>``
  * a trivial command (``true``) that exits immediately
  * a tiny/fallback image (caller-supplied; falls back to a dummy name)

The probe NEVER touches production containers. If the probe cannot run
(daemon unreachable, image missing, timeout), the result is UNSUPPORTED
(fail-safe: never enable a quota mechanism we could not prove works).

Probes are injectable (runner callback) for host-side unit testing.
"""
from __future__ import annotations

import os
import subprocess
import sys

PROBE_LABEL = "com.mergepilot.scope=storageopt-probe"
PROBE_NAME_PREFIX = "mp-storageopt-probe"
PROBE_STORAGE_OPT = "size=1g"


def _default_runner(argv):
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    return proc.returncode, proc.stdout, proc.stderr


def _default_nonce():
    import secrets
    return secrets.token_hex(6)


def probe_with_disposable_container(runner=None, image=None, rng_fn=None,
                                    storage_opt=PROBE_STORAGE_OPT):
    """Run a disposable container to test ``--storage-opt`` support.

    Returns True if the probe succeeded (supported), False if it failed
    (unsupported), and None on a probe-infrastructure error (daemon/image
    unreachable -- treated as unsupported by the caller).
    """
    runner = runner or _default_runner
    rng_fn = rng_fn or _default_nonce
    img = image or os.environ.get("MP_STORAGE_OPT_PROBE_IMAGE", "")
    name = "%s-%s" % (PROBE_NAME_PREFIX, rng_fn())
    argv = [
        "docker", "run", "--rm",
        "--name", name,
        "--network", "none",
        "--label", PROBE_LABEL,
        "--storage-opt", storage_opt,
    ]
    if img:
        argv.append(img)
    else:
        argv.append("scratch-no-image")
    argv.append("true")
    try:
        rc, _out, _err = runner(argv)
    except Exception:
        return None
    return rc == 0


def cleanup_probe_residue(runner=None):
    """Belt-and-suspenders: remove any leftover probe containers.

    ``--rm`` should auto-clean, but if the daemon killed the probe before
    cleanup, this sweep removes containers by the precise probe label.
    NEVER matches production containers (label is probe-specific).
    """
    runner = runner or _default_runner
    try:
        rc, out, _err = runner(
            ["docker", "ps", "-a", "--filter", "label=" + PROBE_LABEL,
             "--format", "{{.Names}}"])
    except Exception:
        return []
    removed = []
    for line in (out or "").splitlines():
        name = line.strip()
        if name.startswith(PROBE_NAME_PREFIX):
            try:
                runner(["docker", "rm", "-f", name])
                removed.append(name)
            except Exception:
                pass
    return removed


def detect(runner=None, image=None, enable_real_probe=True):
    """Return a dict: {supported, reason, probed}.

    Default (no probe / probe disabled / probe error): supported=False.
    Only a successful disposable-container probe sets supported=True.
    """
    result = {"supported": False, "reason": "", "probed": False,
              "probe_image": image or ""}
    if not enable_real_probe:
        result["reason"] = "real probe disabled; default unsupported"
        return result
    outcome = probe_with_disposable_container(runner=runner, image=image)
    result["probed"] = outcome is not None
    if outcome is None:
        result["reason"] = (
            "probe could not run (daemon/image unreachable); "
            "default unsupported (fail-safe)")
        return result
    if outcome:
        result["supported"] = True
        result["reason"] = "disposable-container probe succeeded"
    else:
        result["reason"] = "disposable-container probe failed (unsupported)"
    return result


def main():
    r = detect()
    if r["supported"]:
        sys.stdout.write("storage_opt SUPPORTED %s\n" % r["reason"])
        return 0
    sys.stdout.write("storage_opt NOT_SUPPORTED %s\n" % r["reason"])
    return 1  # non-fatal: caller skips storage-opt


if __name__ == "__main__":
    sys.exit(main())
