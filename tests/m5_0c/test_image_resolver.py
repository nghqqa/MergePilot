#!/usr/bin/env python3
"""Formal regression gate for tests/m5_0c/deploy_test_stack.sh.

Two test surfaces, both driven by a fake ``docker`` that records every call:

  * resolve_pinned_image — sourced verbatim from the deploy script and invoked
    through the fake docker. No real daemon, no pulls, no network.
  * action independence — the FULL deploy script run with mp_guard stubbed,
    proving down/status/health never touch image existence, RUN_KEY validation
    precedes the resolver, and up fails before any resource is created.

All bash is fed to ``bash -s`` as raw UTF-8 bytes. Windows text-mode I/O would
translate "\\n"->"\\r\\n" and corrupt parsing (``pipefail\\r`` -> "invalid
option"; ``func() {\\r`` -> syntax error); raw bytes avoid that. The fake
docker is a bash FUNCTION (inherited by ``$(...)`` subshells, always wins over
PATH), loaded from a quoted heredoc so the helper may use both quote styles.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
import unittest
from collections import namedtuple

SCRIPT_PATH = os.path.join("tests", "m5_0c", "deploy_test_stack.sh")

REAL_DIGEST = (
    "higress-registry.cn-hangzhou.cr.aliyuncs.com/higress/hiclaw-embedded"
    "@sha256:5f8b42fd6c4160b40eb7c3b26c5617edc78fe24d2fcb00f918ff6d742aaa2d2c"
)
REAL_TAG = (
    "higress-registry.cn-hangzhou.cr.aliyuncs.com/higress/hiclaw-embedded:v1.1.2"
)
REAL_ID = "sha256:44a29e0d1f8d6522e69f4837729eb297911a7cc6b46c4e1203424e289979ced2"

ResolverResult = namedtuple("ResolverResult", ["rc", "method", "img_id", "payload", "calls"])
ActionResult = namedtuple("ActionResult", ["rc", "stdout", "stderr", "calls"])

# Fake docker. Models a small world (containers/networks/volumes with labels +
# image) plus image responses for the resolver. Logging is bash-side. Commands
# are an explicit ALLOWLIST; anything else (pull/tag/build/push/save/load/rmi and
# any unknown subcommand) FAILS CLOSED with rc=97 — no silent success.
# Image inspect is ref-keyed via data["images"] when present (so the 3 deploy
# images can resolve to distinct IDs); flat data["digest"]/data["tag"] remain as
# a backward-compatible fallback for the resolver-only tests.
FAKE_DOCKER_PY = r'''import json, os, re, sys
args = sys.argv[1:]
try:
    data = json.loads(os.environ.get("M5C_FAKE_RESP", "{}"))
except Exception:
    data = {}

def eval_fmt(fmt, res):
    if fmt == "{{.Image}}":
        return str(res.get("image", ""))
    if fmt == "{{.Id}}":
        return str(res.get("id", ""))
    if fmt == "{{.State.Status}}":
        return str(res.get("state", "running"))
    if fmt == "{{.SizeRw}}":
        return str(res.get("size_rw", "0"))
    if fmt == "{{json .RepoDigests}}":
        if "repo_digests_raw" in res:
            return str(res["repo_digests_raw"])
        return json.dumps(res.get("repo_digests", []))
    m = re.match(r'\{\{index \.Config\.Labels "([^"]+)"\}\}', fmt)
    if m:
        return str(res.get("labels", {}).get(m.group(1), ""))
    m = re.match(r'\{\{index \.Labels "([^"]+)"\}\}', fmt)
    if m:
        return str(res.get("labels", {}).get(m.group(1), ""))
    return ""

def ref_fmt(rest):
    refs = [a for a in rest if not a.startswith("-") and "{{" not in a]
    fmt = ""
    for a in rest:
        if "{{" in a:
            fmt = a
            break
    return (refs[0] if refs else ""), fmt

if len(args) >= 2 and args[0] == "image" and args[1] == "inspect":
    ref, fmt = ref_fmt(args[2:])
    images = data.get("images", {})
    if ref in images:
        entry = images[ref]
    elif "@sha256:" in ref:
        entry = data.get("digest", {})
    else:
        entry = data.get("tag", {})
    if entry.get("_missing"):
        sys.exit(1)
    if fmt:
        sys.stdout.write(eval_fmt(fmt, entry))
    sys.exit(0)

if len(args) >= 1 and args[0] == "inspect":
    name, fmt = ref_fmt(args[1:])
    res = data.get("containers", {}).get(name)
    if res is None:
        sys.exit(1)
    if fmt:
        sys.stdout.write(eval_fmt(fmt, res))
    sys.exit(0)

if len(args) >= 2 and args[0] == "network" and args[1] == "inspect":
    name, fmt = ref_fmt(args[2:])
    res = data.get("networks", {}).get(name)
    if res is None:
        sys.exit(1)
    if fmt:
        sys.stdout.write(eval_fmt(fmt, res))
    sys.exit(0)

if len(args) >= 2 and args[0] == "volume" and args[1] == "inspect":
    name, fmt = ref_fmt(args[2:])
    res = data.get("volumes", {}).get(name)
    if res is None:
        sys.exit(1)
    if fmt:
        sys.stdout.write(eval_fmt(fmt, res))
    sys.exit(0)

if len(args) >= 2 and args[0] == "network" and args[1] == "create":
    sys.exit(0)
if len(args) >= 2 and args[0] == "volume" and args[1] == "create":
    sys.exit(0)
if len(args) >= 1 and args[0] == "run":
    sys.exit(0)
if len(args) >= 1 and args[0] == "rm":
    sys.exit(0)
if len(args) >= 2 and args[0] == "network" and args[1] == "rm":
    sys.exit(0)
if len(args) >= 2 and args[0] == "volume" and args[1] == "rm":
    sys.exit(0)
if len(args) >= 1 and args[0] == "ps":
    sys.exit(0)
if len(args) >= 2 and args[0] == "network" and args[1] == "ls":
    sys.exit(0)
if len(args) >= 2 and args[0] == "volume" and args[1] == "ls":
    sys.exit(0)
if len(args) >= 1 and args[0] == "exec":
    sys.exit(1)
if len(args) >= 1 and args[0] == "logs":
    sys.exit(0)

# FAIL CLOSED: pull/tag/build/push/save/load/rmi and ANY unknown subcommand
sys.exit(97)
'''


# ── helpers ──

def _read_script():
    with open(SCRIPT_PATH, encoding="utf-8") as f:
        return f.read()


def _new_log_path():
    fd, path = tempfile.mkstemp(suffix=".fakelog", prefix="m5c_")
    os.close(fd)
    if os.path.exists(path):
        os.unlink(path)  # fake docker recreates it via append
    return path


def _to_bash_path(p):
    """Convert a Windows path to the WSL POSIX path so bash redirection works.

    Python's subprocess ``bash`` here is WSL bash (drives under /mnt/). bash cannot
    open `C:\\Users\\...` via `>>`, but `/mnt/c/Users/...` works. Python reads the
    original Windows path — same physical file.
    """
    p = p.replace("\\", "/")
    if len(p) >= 2 and p[1] == ":":
        return "/mnt/" + p[0].lower() + p[2:]
    return p


def _read_calls(log_path):
    """Each logged call is one line of TAB-joined args (written by the bash wrapper)."""
    calls = []
    try:
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\r\n")
                if not line:
                    continue
                calls.append(line.split("\t"))
    except Exception:
        pass
    return calls


def _safe_unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _bash_prefix(responses, log_path):
    """Shared prologue: fake docker installed as a PATH script under WSL /tmp so
    that BOTH bash `docker` calls AND `subprocess.run(["docker",...])` inside the
    deploy script's python heredocs resolve to it. A bash function is NOT inherited
    by python subprocess (execvp ignores functions), so the health/idempotency
    heredocs would otherwise bypass the fake. The wrapper logs each call (tab-joined
    args) to M5C_FAKE_LOG, then dispatches to the python helper. Temp files are
    trap-cleaned on exit."""
    return [
        "set -uo pipefail",
        "export M5C_FAKE_RESP=" + shlex.quote(json.dumps(responses)),
        "export M5C_FAKE_LOG=" + shlex.quote(_to_bash_path(log_path)),
        "_M5C_FD=\"$(mktemp /tmp/m5c_fd.XXXXXX.py)\"",
        "_M5C_BIN=\"$(mktemp -d /tmp/m5c_bin.XXXXXX)\"",
        "trap 'rm -f \"$_M5C_FD\"; rm -rf \"$_M5C_BIN\"' EXIT",
        "cat > \"$_M5C_FD\" <<'M5C_FD_EOF'",
        FAKE_DOCKER_PY.rstrip("\n"),
        "M5C_FD_EOF",
        "cat > \"$_M5C_BIN/docker\" <<'M5C_BIN_EOF'",
        "#!/usr/bin/env bash",
        'if [ -n "${M5C_FAKE_LOG:-}" ]; then ( IFS=$(printf "\\t"); printf "%s\\n" "$*" >> "$M5C_FAKE_LOG" ); fi',
        'python3 "$_M5C_FD" "$@"',
        "M5C_BIN_EOF",
        "chmod +x \"$_M5C_BIN/docker\"",
        "export _M5C_FD",
        "export PATH=\"$_M5C_BIN:$PATH\"",
    ]


def _run_resolver(responses, digest_ref=REAL_DIGEST, tag_ref=REAL_TAG):
    """Source resolve_pinned_image and invoke it once. Returns ResolverResult."""
    src = _read_script()
    func_code = src[src.index("resolve_pinned_image()"):src.index("# ── RUN_KEY validation")]
    log_path = _new_log_path()
    try:
        script = "\n".join(
            _bash_prefix(responses, log_path) + [
                func_code.rstrip("\n"),
                "OUT=$(resolve_pinned_image " + shlex.quote(digest_ref)
                + " " + shlex.quote(tag_ref) + ")",
                "RC=$?",
                "printf '%s\\n' \"$OUT\"",
                "printf 'RC=%d\\n' \"$RC\"",
            ]
        ) + "\n"
        proc = subprocess.run(["bash", "-s"], input=script.encode("utf-8"),
                              capture_output=True, timeout=15, cwd=os.getcwd())
        calls = _read_calls(log_path)
    finally:
        _safe_unlink(log_path)
    out = proc.stdout.decode("utf-8", errors="replace")
    m = re.search(r"RC=(\d+)\s*$", out)
    rc = int(m.group(1)) if m else proc.returncode
    payload = (out[: m.start()] if m else out).rstrip("\n")
    parts = payload.split("\n") if payload else []
    method = parts[0] if len(parts) >= 1 else ""
    img_id = parts[1] if len(parts) >= 2 else ""
    return ResolverResult(rc, method, img_id, payload, calls)


def _run_action(action, run_key=None, responses=None):
    """Run the full deploy script (mp_guard stubbed) with a fake docker.

    M5C_RUN_KEY is injected INTO the script: MSYS bash does not reliably inherit
    custom env vars set via subprocess ``env=``, so passing it out-of-band silently
    fails (the script then auto-generates a RUN_KEY and reaches the resolver).
    """
    src = _read_script()
    guard = 'source "$ROOT_WSL/tools/test-env/mp_guard.sh"'
    if guard not in src:
        raise RuntimeError("mp_guard source line not found; deploy script changed")
    src = src.replace(guard, ": # mp_guard stubbed for action-independence unit test")
    log_path = _new_log_path()
    try:
        prefix = _bash_prefix(responses or {}, log_path)
        if run_key is not None:
            prefix.append("export M5C_RUN_KEY=" + shlex.quote(run_key))
        script = "\n".join(prefix + [src.rstrip("\n")]) + "\n"
        proc = subprocess.run(["bash", "-s", action], input=script.encode("utf-8"),
                              capture_output=True, timeout=30, cwd=os.getcwd())
        calls = _read_calls(log_path)
    finally:
        _safe_unlink(log_path)
    return ActionResult(proc.returncode,
                        proc.stdout.decode("utf-8", errors="replace"),
                        proc.stderr.decode("utf-8", errors="replace"),
                        calls)


def _parse_constants(src):
    consts = {}
    for n in ("EMBEDDED_IMG", "EMBEDDED_TAG", "MANAGER_IMG", "MANAGER_TAG",
              "WORKER_IMG", "WORKER_TAG"):
        m = re.search(rf'^{n}="([^"]+)"', src, re.MULTILINE)
        if m:
            consts[n] = m.group(1)
    return consts


def _has_image_inspect(calls):
    return any(len(c) >= 2 and c[0] == "image" and c[1] == "inspect" for c in calls)


def _has_pull(calls):
    return any(c[:1] == ["pull"] for c in calls)


def _has_network_create(calls):
    return any(c[:2] == ["network", "create"] for c in calls)


def _has_run(calls):
    return any(c[:1] == ["run"] for c in calls)


# ── idempotency world-state helpers ──

EMB_ID = "sha256:" + "e" * 64
MGR_ID = "sha256:" + "d" * 64
WRK_ID = "sha256:" + "c" * 64


def _labels(rk):
    return {"com.mergepilot.scope": "test",
            "com.mergepilot.phase": "m5-0c",
            "com.mergepilot.run_key": rk}


def _idem_world(rk, consts):
    """Full correct world state for an idempotent already_up (all 5 resources)."""
    return {
        "images": {
            consts["EMBEDDED_IMG"]: {"id": EMB_ID, "repo_digests": [consts["EMBEDDED_IMG"]]},
            consts["MANAGER_IMG"]: {"id": MGR_ID, "repo_digests": [consts["MANAGER_IMG"]]},
            consts["WORKER_IMG"]: {"id": WRK_ID, "repo_digests": [consts["WORKER_IMG"]]},
        },
        "containers": {
            "m5c-controller-" + rk: {"image": EMB_ID, "labels": _labels(rk), "state": "running"},
            "m5c-manager-" + rk: {"image": MGR_ID, "labels": _labels(rk), "state": "running"},
            "m5c-worker-" + rk: {"image": WRK_ID, "labels": _labels(rk), "state": "running"},
        },
        "networks": {"m5c-net-" + rk: {"labels": _labels(rk)}},
        "volumes": {"m5c-data-" + rk: {"labels": _labels(rk)}},
    }


def _docker_rc(args, responses=None):
    """Run the fake docker with the given argv; return its exit code."""
    log_path = _new_log_path()
    try:
        parts = _bash_prefix(responses or {}, log_path) + [
            "docker " + " ".join(shlex.quote(a) for a in args),
            "printf 'DOCKER_RC=%d\\n' \"$?\"",
        ]
        script = "\n".join(parts) + "\n"
        proc = subprocess.run(["bash", "-s"], input=script.encode("utf-8"),
                              capture_output=True, timeout=15, cwd=os.getcwd())
    finally:
        _safe_unlink(log_path)
    out = proc.stdout.decode("utf-8", errors="replace")
    m = re.search(r"DOCKER_RC=(\d+)", out)
    return int(m.group(1)) if m else proc.returncode


# ── resolver tests ──

class TestResolvePinnedImage(unittest.TestCase):
    """resolve_pinned_image: success, strict rejection, and output contract."""

    def _run(self, responses, digest_ref=REAL_DIGEST, tag_ref=REAL_TAG):
        return _run_resolver(responses, digest_ref, tag_ref)

    # ── success paths ──
    def test_01_digest_direct_success(self):
        r = self._run({"digest": {"id": REAL_ID, "repo_digests": [REAL_DIGEST]},
                       "tag": {"id": REAL_ID, "repo_digests": [REAL_DIGEST]}})
        self.assertEqual(r.rc, 0)
        self.assertEqual(r.method, "digest_direct")
        self.assertEqual(r.img_id, REAL_ID)

    def test_02_tag_fallback_success(self):
        r = self._run({"digest": {"_missing": True},
                       "tag": {"id": REAL_ID, "repo_digests": [REAL_DIGEST]}})
        self.assertEqual(r.rc, 0)
        self.assertEqual(r.method, "verified_tag_fallback")
        self.assertEqual(r.img_id, REAL_ID)

    # ── strict rejection (cases grep would falsely pass) ──
    def test_03_substring_rejected(self):
        prefix, _, hexpart = REAL_DIGEST.partition("@sha256:")
        truncated = prefix + "@sha256:" + hexpart[:16]
        self.assertNotIn(truncated, [REAL_DIGEST])
        r = self._run({"digest": {"_missing": True},
                       "tag": {"id": REAL_ID, "repo_digests": [REAL_DIGEST]}},
                      digest_ref=truncated)
        self.assertEqual(r.rc, 6)

    def test_04_repo_mismatch_rejected(self):
        wrong_repo = ("different.registry.io/higress/hiclaw-embedded@"
                      + REAL_DIGEST.split("@", 1)[1])
        r = self._run({"digest": {"_missing": True},
                       "tag": {"id": REAL_ID, "repo_digests": [REAL_DIGEST]}},
                      digest_ref=wrong_repo)
        self.assertEqual(r.rc, 6)

    def test_05_digest_mismatch_rejected(self):
        wrong = REAL_DIGEST.split("@", 1)[0] + "@sha256:" + "0" * 64
        r = self._run({"digest": {"_missing": True},
                       "tag": {"id": REAL_ID, "repo_digests": [wrong]}})
        self.assertEqual(r.rc, 6)

    def test_06_empty_repodigests_rejected(self):
        r = self._run({"digest": {"_missing": True},
                       "tag": {"id": REAL_ID, "repo_digests": []}})
        self.assertEqual(r.rc, 6)

    def test_07_both_missing_rejected(self):
        r = self._run({"digest": {"_missing": True}, "tag": {"_missing": True}})
        self.assertEqual(r.rc, 6)

    # ── malformed RepoDigests (section 二.1-4) ──
    def test_08_repodigests_null_rejected(self):
        r = self._run({"digest": {"_missing": True},
                       "tag": {"id": REAL_ID, "repo_digests_raw": "null"}})
        self.assertEqual(r.rc, 6)

    def test_09_repodigests_non_array_rejected(self):
        r = self._run({"digest": {"_missing": True},
                       "tag": {"id": REAL_ID, "repo_digests_raw": '"a-string-not-array"'}})
        self.assertEqual(r.rc, 6)

    def test_10_repodigests_invalid_json_rejected(self):
        r = self._run({"digest": {"_missing": True},
                       "tag": {"id": REAL_ID, "repo_digests_raw": "{broken-json"}})
        self.assertEqual(r.rc, 6)

    def test_11_repodigests_non_string_element_safe(self):
        # expected (string) present alongside junk -> accept, junk ignored
        r = self._run({"digest": {"_missing": True},
                       "tag": {"id": REAL_ID, "repo_digests": [REAL_DIGEST, 123, None]}})
        self.assertEqual(r.rc, 0)
        self.assertEqual(r.method, "verified_tag_fallback")
        # only non-string elements, expected absent -> reject
        r2 = self._run({"digest": {"_missing": True},
                        "tag": {"id": REAL_ID, "repo_digests": [123, None]}})
        self.assertEqual(r2.rc, 6)

    # ── safety / contract (section 二.5,7,8,9) ──
    def test_12_no_docker_pull_ever(self):
        r = self._run({"digest": {"id": REAL_ID, "repo_digests": [REAL_DIGEST]},
                       "tag": {"id": REAL_ID, "repo_digests": [REAL_DIGEST]}})
        self.assertEqual(r.rc, 0)
        self.assertGreater(len(r.calls), 0, "fake docker must have been called")
        self.assertFalse(_has_pull(r.calls), f"docker pull must never happen: {r.calls}")

    def test_13_empty_image_id_rejected(self):
        r = self._run({"digest": {"_missing": True},
                       "tag": {"id": "", "repo_digests": [REAL_DIGEST]}})
        self.assertEqual(r.rc, 6)

    def test_14_non_sha256_image_id_rejected(self):
        """Defense in depth: a non-sha256 {{.Id}} must be rejected on BOTH paths."""
        r = self._run({"digest": {"id": "not-a-sha256", "repo_digests": [REAL_DIGEST]},
                       "tag": {"id": "not-a-sha256", "repo_digests": [REAL_DIGEST]}})
        self.assertEqual(r.rc, 6)
        r2 = self._run({"digest": {"_missing": True},
                        "tag": {"id": "registry/repo:v1.0", "repo_digests": [REAL_DIGEST]}})
        self.assertEqual(r2.rc, 6)

    def test_15_stdout_exact_format(self):
        """stdout is exactly '<method>\\n<sha256-id>' — no diagnostic leakage."""
        r = self._run({"digest": {"id": REAL_ID, "repo_digests": [REAL_DIGEST]},
                       "tag": {"id": REAL_ID, "repo_digests": [REAL_DIGEST]}})
        self.assertEqual(r.payload, "digest_direct" + "\n" + REAL_ID)
        self.assertEqual(r.payload.count("\n"), 1)

    def test_16_three_constants_have_tag_fallback(self):
        src = _read_script()
        consts = _parse_constants(src)
        for n in ("EMBEDDED_IMG", "EMBEDDED_TAG", "MANAGER_IMG", "MANAGER_TAG",
                  "WORKER_IMG", "WORKER_TAG"):
            self.assertIn(n, consts, f"{n} missing in deploy script")
        for base in ("EMBEDDED", "MANAGER", "WORKER"):
            self.assertIn("@sha256:", consts[base + "_IMG"])
            self.assertIn(":", consts[base + "_TAG"])
            self.assertNotIn("@sha256:", consts[base + "_TAG"])

    def test_17_three_images_same_resolver_path(self):
        consts = _parse_constants(_read_script())
        ids = {
            "EMBEDDED": "sha256:" + "1" * 64,
            "MANAGER": "sha256:" + "2" * 64,
            "WORKER": "sha256:" + "3" * 64,
        }
        for base in ("EMBEDDED", "MANAGER", "WORKER"):
            dr, tr = consts[base + "_IMG"], consts[base + "_TAG"]
            r = _run_resolver({"digest": {"id": ids[base], "repo_digests": [dr]},
                               "tag": {"id": ids[base], "repo_digests": [dr]}},
                              digest_ref=dr, tag_ref=tr)
            self.assertEqual(r.rc, 0, f"{base} resolve rc")
            self.assertEqual(r.method, "digest_direct", f"{base} method")
            self.assertEqual(r.img_id, ids[base], f"{base} id")

    def test_18_digest_and_fallback_consistent(self):
        direct = self._run({"digest": {"id": REAL_ID, "repo_digests": [REAL_DIGEST]},
                            "tag": {"id": REAL_ID, "repo_digests": [REAL_DIGEST]}})
        fallback = self._run({"digest": {"_missing": True},
                              "tag": {"id": REAL_ID, "repo_digests": [REAL_DIGEST]}})
        self.assertEqual(direct.rc, 0)
        self.assertEqual(fallback.rc, 0)
        self.assertEqual(direct.img_id, fallback.img_id)
        self.assertEqual(direct.img_id, REAL_ID)
        self.assertEqual(direct.method, "digest_direct")
        self.assertEqual(fallback.method, "verified_tag_fallback")


# ── action independence tests (section 三) ──

class TestActionIndependence(unittest.TestCase):
    """With images absent, down/status/health never inspect images; RUN_KEY
    validation precedes the resolver; up fails before creating any resource."""

    def test_a01_down_no_image_inspect(self):
        r = _run_action("down", run_key="act-down-1")
        self.assertEqual(r.rc, 0)
        self.assertGreater(len(r.calls), 0, "down must call docker for cleanup")
        self.assertFalse(_has_image_inspect(r.calls),
                         f"down must not inspect images: {r.calls}")

    def test_a02_down_still_cleans_up(self):
        r = _run_action("down", run_key="act-down-2")
        self.assertEqual(r.rc, 0)
        kinds = set()
        for c in r.calls:
            if c[:1] == ["rm"]:
                kinds.add("rm")
            if c[:2] == ["network", "rm"]:
                kinds.add("network_rm")
            if c[:2] == ["network", "ls"]:
                kinds.add("network_ls")
        self.assertIn("rm", kinds, "down must rm containers")
        self.assertIn("network_rm", kinds, "down must rm network")
        self.assertIn("network_ls", kinds, "down must count residue")

    def test_a03_status_no_resolver(self):
        r = _run_action("status", run_key="act-status-1")
        self.assertEqual(r.rc, 0)
        self.assertGreater(len(r.calls), 0, "status must call docker (log non-empty)")
        self.assertFalse(_has_image_inspect(r.calls),
                         f"status must not inspect images: {r.calls}")
        self.assertIn("RUN_KEY=act-status-1", r.stdout)
        self.assertNotIn('"error"', r.stdout)

    def test_a04_health_no_resolver_nonexistent_rc1(self):
        r = _run_action("health", run_key="act-health-1")
        self.assertGreater(len(r.calls), 0, "health must call docker (log non-empty)")
        self.assertEqual(r.rc, 1)
        self.assertFalse(_has_image_inspect(r.calls),
                         f"health must not inspect images: {r.calls}")
        self.assertIn('"all_passed"', r.stdout)

    def test_a05_empty_runkey_rc4(self):
        r = _run_action("up", run_key="")
        self.assertEqual(r.rc, 4)
        self.assertFalse(_has_image_inspect(r.calls),
                         "RUN_KEY validation must precede resolver")

    def test_a06_illegal_runkey_rc4(self):
        r = _run_action("up", run_key="bad/key")
        self.assertEqual(r.rc, 4)
        self.assertFalse(_has_image_inspect(r.calls))

    def test_a07_unknown_action_rc64(self):
        r = _run_action("bogus", run_key="act-unknown-1")
        self.assertEqual(r.rc, 64)
        self.assertFalse(_has_image_inspect(r.calls))

    def test_a08_up_missing_images_rc6_no_resources(self):
        r = _run_action("up", run_key="act-up-missing",
                        responses={"digest": {"_missing": True}, "tag": {"_missing": True}})
        self.assertEqual(r.rc, 6)
        self.assertTrue(_has_image_inspect(r.calls), "resolver must attempt image inspect")
        self.assertFalse(_has_network_create(r.calls),
                         f"no network create on resolve failure: {r.calls}")
        self.assertFalse(_has_run(r.calls),
                         f"no container run on resolve failure: {r.calls}")

    def test_a09_resolver_before_resource_creation(self):
        r = _run_action("up", run_key="act-up-order",
                        responses={"digest": {"_missing": True}, "tag": {"_missing": True}})
        self.assertEqual(r.rc, 6)
        first_inspect = next((i for i, c in enumerate(r.calls) if _has_image_inspect([c])), None)
        first_resource = next((i for i, c in enumerate(r.calls)
                               if c[:2] == ["network", "create"] or c[:1] == ["run"]), None)
        self.assertIsNotNone(first_inspect, "resolver must run before any resource op")
        self.assertIsNone(first_resource,
                          f"resolve failure must preempt all resource creation: {r.calls}")


# ── idempotency completeness (P2-2) ──

class TestIdempotencyCompleteness(unittest.TestCase):
    """already_up requires ALL 5 resources present with correct scope/phase/run_key
    labels AND each container .Image equal to its resolved ID. Any defect → rc=5."""

    def setUp(self):
        self.consts = _parse_constants(_read_script())

    def _up(self, rk, world):
        return _run_action("up", run_key=rk, responses=world)

    def _full(self, rk):
        return _idem_world(rk, self.consts)

    def test_i01_manager_missing_rc5(self):
        rk = "idem-mgr-missing"
        w = self._full(rk)
        del w["containers"]["m5c-manager-" + rk]
        self.assertEqual(self._up(rk, w).rc, 5)

    def test_i02_worker_missing_rc5(self):
        rk = "idem-wrk-missing"
        w = self._full(rk)
        del w["containers"]["m5c-worker-" + rk]
        self.assertEqual(self._up(rk, w).rc, 5)

    def test_i03_manager_runkey_label_wrong_rc5(self):
        rk = "idem-mgr-rk"
        w = self._full(rk)
        w["containers"]["m5c-manager-" + rk]["labels"]["com.mergepilot.run_key"] = "WRONG"
        self.assertEqual(self._up(rk, w).rc, 5)

    def test_i04_worker_scope_label_wrong_rc5(self):
        rk = "idem-wrk-scope"
        w = self._full(rk)
        w["containers"]["m5c-worker-" + rk]["labels"]["com.mergepilot.scope"] = "production"
        self.assertEqual(self._up(rk, w).rc, 5)

    def test_i05_manager_image_wrong_rc5(self):
        rk = "idem-mgr-img"
        w = self._full(rk)
        w["containers"]["m5c-manager-" + rk]["image"] = "sha256:" + "9" * 64
        self.assertEqual(self._up(rk, w).rc, 5)

    def test_i06_worker_image_wrong_rc5(self):
        rk = "idem-wrk-img"
        w = self._full(rk)
        w["containers"]["m5c-worker-" + rk]["image"] = "sha256:" + "9" * 64
        self.assertEqual(self._up(rk, w).rc, 5)

    def test_i07_volume_missing_rc5(self):
        rk = "idem-vol-missing"
        w = self._full(rk)
        del w["volumes"]["m5c-data-" + rk]
        self.assertEqual(self._up(rk, w).rc, 5)

    def test_i08_volume_label_wrong_rc5(self):
        rk = "idem-vol-label"
        w = self._full(rk)
        w["volumes"]["m5c-data-" + rk]["labels"]["com.mergepilot.run_key"] = "WRONG"
        self.assertEqual(self._up(rk, w).rc, 5)

    def test_i09_all_complete_already_up_rc0(self):
        rk = "idem-all-ok"
        r = self._up(rk, self._full(rk))
        self.assertEqual(r.rc, 0)
        self.assertIn('"status":"already_up"', r.stdout.replace(" ", ""))
        self.assertIn('"idempotent":true', r.stdout.replace(" ", ""))

    def test_i10_exited_but_present_already_up_rc0(self):
        """manager/worker exited but container objects present with correct metadata."""
        rk = "idem-exited-ok"
        w = self._full(rk)
        w["containers"]["m5c-manager-" + rk]["state"] = "exited"
        w["containers"]["m5c-worker-" + rk]["state"] = "exited"
        r = self._up(rk, w)
        self.assertEqual(r.rc, 0)
        self.assertIn('"status":"already_up"', r.stdout.replace(" ", ""))


# ── health JSON compatibility (P2-1) ──

class TestHealthJsonCompat(unittest.TestCase):
    def test_h01_health_json_has_http_code_fields(self):
        """health JSON must carry matrix_http/minio_http/element_http + the booleans."""
        rk = "health-fields"
        w = {"images": {},
             "containers": {"m5c-controller-" + rk: {"image": EMB_ID, "labels": _labels(rk), "state": "running"}},
             "networks": {}, "volumes": {}}
        r = _run_action("health", run_key=rk, responses=w)
        compact = r.stdout.replace(" ", "")
        for field in ('"matrix_http"', '"minio_http"', '"element_http"',
                      '"matrix_6167"', '"minio_9000"', '"element_8080"', '"secret_hits"'):
            self.assertIn(field, compact, f"health JSON missing {field}: {r.stdout}")


# ── fake docker fail-closed (P3-1) ──

class TestFakeDockerFailClosed(unittest.TestCase):
    """Unknown / forbidden subcommands must NOT succeed (default deny)."""

    def test_f01_unknown_subcommand_nonzero(self):
        self.assertNotEqual(_docker_rc(["bogus-subcmd", "x"]), 0)

    def test_f02_pull_nonzero(self):
        self.assertNotEqual(_docker_rc(["pull", "img:latest"]), 0)

    def test_f03_tag_nonzero(self):
        self.assertNotEqual(_docker_rc(["tag", "a", "b"]), 0)

    def test_f04_push_nonzero(self):
        self.assertNotEqual(_docker_rc(["push", "a"]), 0)

    def test_f05_supported_commands_still_succeed(self):
        rc = _docker_rc(["image", "inspect", "ref@sha256:aa", "--format", "{{.Id}}"],
                        {"digest": {"id": "sha256:x", "repo_digests": []}})
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
