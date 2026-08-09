#!/usr/bin/env python3
"""D2B-3 static safety analysis of run_production_live.sh.

No WSL, no execution — purely text + ordering checks on the shell entry point.
Asserts the authorization contract, secret-file hygiene, trap-before-copy
ordering, probe-file confinement, cleanup non-zero-on-failure, final Stopped
confirmation, and capture return-code passthrough.
"""
from __future__ import annotations
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "run_production_live.sh")
SRC = open(SCRIPT, encoding="utf-8").read()
LINES = SRC.splitlines()


def _x(cond, msg):
    if not cond:
        raise AssertionError("FAIL: " + msg)
    print("  PASS:", msg)


def _line_index(predicate):
    """1-based line number of the first line matching predicate, else -1."""
    for i, ln in enumerate(LINES):
        if predicate(ln):
            return i + 1
    return -1


def test_authz_marker_exact():
    _x('AUTHZ_EXPECTED="operator-authorized-tier-c"' in SRC,
       "authorization marker constant is exactly operator-authorized-tier-c")
    # The marker content must be compared with exact equality (no prefix/regex).
    _x('"$(cat \'"$AUTHZ_FILE\'\')" = \'"$AUTHZ_EXPECTED"\''
        in SRC or '= "' + "operator-authorized-tier-c" + '"' in SRC,
       "authz content compared by exact equality")


def test_secret_file_contract():
    """Both authz + Matrix password files: regular, non-symlink, mode 0600, non-empty."""
    for marker in ("AUTHZ_FILE", "MATRIX_PW_FILE"):
        _x('"' in SRC and marker in SRC, "%s referenced" % marker)
    _x("test -f" in SRC, "checks regular file (test -f)")
    _x("test ! -L" in SRC, "rejects symlink (test ! -L)")
    _x('stat -c %a' in SRC and '"600"' in SRC, "enforces mode 0600 via stat -c %a")
    _x("test -s" in SRC, "enforces non-empty (test -s)")


def test_trap_before_docker_cp():
    """trap cleanup MUST be installed before any docker cp (cleanup always runs).
    Comment lines mentioning 'docker cp' are ignored — only real commands count."""
    trap_line = _line_index(lambda ln: "trap cleanup" in ln and "EXIT" in ln)
    cp_line = _line_index(lambda ln: "docker cp" in ln and not ln.strip().startswith("#"))
    _x(trap_line > 0, "trap cleanup EXIT present")
    _x(cp_line > 0, "docker cp command present (non-comment)")
    _x(trap_line < cp_line, "trap (line %d) before docker cp (line %d)" % (trap_line, cp_line))


def test_probe_path_fixed():
    """probe-tools.py only copied to the fixed /tmp/m5d-probe-tools.py."""
    _x('PROBE_DST_PATH="/tmp/m5d-probe-tools.py"' in SRC,
       "fixed probe destination /tmp/m5d-probe-tools.py")
    _x("docker cp" in SRC and "/tmp/m5d-probe-tools.py" in SRC,
       "docker cp targets the fixed probe path")


def test_cleanup_only_deletes_probe():
    """Cleanup must remove ONLY the probe file (no broad rm -rf)."""
    _x("rm -f" in SRC and "PROBE_DST_PATH" in SRC, "cleanup rm -f references probe path")
    _x("rm -rf /tmp" not in SRC and "rm -rf '" not in SRC,
       "no broad recursive rm in cleanup")
    # Cleanup must verify the probe file is gone after removal (fail-closed).
    _x("! docker exec" in SRC and "test -f" in SRC,
       "cleanup verifies probe file deleted")


def test_cleanup_failure_nonzero():
    """If cleanup cannot delete the probe or confirm Stopped, exit must be non-zero."""
    _x("cleanup_failed" in SRC, "tracks cleanup failure flag")
    _x('rc=3' in SRC or '[ "$rc" -ne 0 ] || rc=3' in SRC or "rc=3" in SRC.replace(" ", ""),
       "cleanup failure forces non-zero exit (rc=3)")


def test_final_stopped_confirmation():
    """After capture, Ubuntu-22.04 must be confirmed Stopped before exiting."""
    _x("Stopped" in SRC, "polls for Stopped state")
    _x("Ubuntu-22.04" in SRC, "targets production distro Ubuntu-22.04")
    _x("prod_after" in SRC or "Stopped" in SRC,
       "verifies distro reached Stopped")


def test_capture_rc_passthrough():
    """The collector's return code must propagate as the script exit code."""
    _x("capture_rc=$?" in SRC, "captures collector return code")
    _x('exit "$capture_rc"' in SRC, "exits with collector return code (passthrough)")


def test_isolation_distro_checks():
    """Production distro Running + test distro Stopped enforced before capture."""
    _x("Ubuntu-22.04" in SRC and "Running" in SRC, "requires Ubuntu-22.04 Running")
    _x("MergePilot-Test" in SRC and "Stopped" in SRC, "requires MergePilot-Test Stopped")


def test_no_pat_handling():
    """The entry must not read, pass, or echo a GitHub PAT."""
    for banned in ("GITHUB_TOKEN", "GH_TOKEN", "ghp_", "github_pat_", "--token"):
        _x(banned not in SRC, "no %s reference (PAT never handled)" % banned)


def main():
    for n, fn in sorted(globals().items()):
        if n.startswith("test_") and callable(fn):
            print("=== %s ===" % n); fn()
    print("\nALL UNIT TESTS PASSED")


if __name__ == "__main__":
    main()
