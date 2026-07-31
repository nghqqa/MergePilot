"""TestRunner core -- isolated test execution with a deploy-owned trust boundary.

Hardening (this round):
* env allowlist is fail-closed: effective_env = allowlist ∩ caller_values −
  sensitive keys; an EMPTY allowlist allows ZERO vars (case-insensitive).
* trusted config is fully validated (fail-closed -> DENIED/TRUSTED_CONFIG_MISSING,
  never a business FAIL); v1 container network is fixed denied.
* image must be deploy-provided as repository@sha256:<64hex> matching the profile
  repository; no tag fallback.
* exit classification: pytest 0->PASS, 1->FAIL(exit10), 5->no-tests(FAIL,flagged),
  2/3/4->runner ERROR; docker 125/126/127 -> DEPENDENCY_UNAVAILABLE.
* container cleanup is fail-closed (rm rc checked; residue queried by label;
  failure -> ERROR/INTERNAL_ERROR).
* per-run artifact dir under deploy-provided root (containment-checked); no
  fallback tmp creation.
* workspace copy rejects ANY symlink/junction/reparse/socket/device -> ERROR.
* subprocess timeout bounded below ctx.deadline (cleanup budget reserved).
* total output budget (stdout+stderr+metadata) hard-capped < 1 MiB envelope.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil

SCHEMA_VERSION = "1"
SUPPORTED_PROFILES_MAJOR = 1

PASS, FAIL, TIMEOUT, ERROR = "PASS", "FAIL", "TIMEOUT", "ERROR"

INPUT_INVALID = "TEST_RUNNER_INPUT_INVALID"
INVALID_COMMAND = "TEST_RUNNER_INVALID_COMMAND"
PATH_ESCAPE = "TEST_RUNNER_PATH_ESCAPE"
NO_TRUSTED_EXECUTOR = "TEST_RUNNER_NO_TRUSTED_EXECUTOR"
NETWORK_DENIED = "TEST_RUNNER_NETWORK_DENIED"
TRUSTED_CONFIG_MISSING = "TEST_RUNNER_TRUSTED_CONFIG_MISSING"
EXEC_UNAVAILABLE = "TEST_RUNNER_EXEC_UNAVAILABLE"
CONTAINER_UNAVAILABLE = "TEST_RUNNER_CONTAINER_UNAVAILABLE"
TIMEOUT_SUB = "TEST_RUNNER_TIMEOUT"
INTERNAL = "TEST_RUNNER_INTERNAL"

# frozen hard limits
CLEANUP_BUDGET_MS = 3000
MIN_EXEC_MS = 100
HARD_MAX_TIMEOUT_MS = 300000
HARD_MAX_OUTPUT_BYTES = 512 * 1024  # total budget (stdout+stderr), < 1 MiB envelope
DEFAULT_MAX_OUTPUT_BYTES = 256 * 1024
DEFAULT_SANDBOX_BYTES = 256 * 1024 * 1024
MAX_ENV_KEYS = 32
MAX_ENV_KEY_LEN = 64
MAX_ENV_VAL_LEN = 4096
MAX_ARTIFACT_FILES = 100
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024

_EXCLUDED_DIRS = {".git", "__pycache__", ".pytest_cache", "node_modules", ".venv", "venv"}
_EXCLUDED_FILE_RE = re.compile(r"(^\.env|^.*\.(pem|key|pfx|p12|keystore)$|\.pyc$)", re.IGNORECASE)
_DENY_SUB = ("TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "COOKIE", "DSN")
_DENY_EXACT = {"PG_PASS", "PG_PASSWORD", "PG_DSN", "MERGEPILOT_APPROVER_PASS"}
_DIGEST_RE = re.compile(r"^([A-Za-z0-9_.\-/:]+)@sha256:([0-9a-f]{64})$")
_DISTRO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\- ]{0,63}$")
_CPUS_RE = re.compile(r"^[0-9]+(\.[0-9]+)?$")
_MEM_RE = re.compile(r"^([0-9]+)([kmg]?)$", re.IGNORECASE)

# resource bounds (reject 0/negative/unbounded)
MIN_CPUS = 0.1
MAX_CPUS = 16
MIN_MEM_BYTES = 4 * 1024 * 1024
MAX_MEM_BYTES = 4 * 1024 * 1024 * 1024
MIN_UID = 1
MAX_UID = 65534  # non-root; 0 forbidden
MIN_GID = 1
MAX_GID = 65534
DEFAULT_UID = 1000
DEFAULT_GID = 1000

import threading as _threading
_RUN_SEQ = 0
_RUN_LOCK = _threading.Lock()

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROFILES_SCHEMA_PATH = os.path.join(_HERE, "schema", "runner-profiles.schema.json")
DEFAULT_PROFILES_PATH = os.path.join(_HERE, "config", "runner-profiles.v1.json")
_SCHEMA_VALIDATOR = None


class TestRunnerError(Exception):
    def __init__(self, subcode, detail=""):
        super().__init__(subcode)
        self.subcode = subcode
        self.detail = detail


def _is_sensitive_key(key):
    k = key.upper()
    if k.startswith("MERGEPILOT_TR_") or k in _DENY_EXACT:
        return True
    if any(s in k for s in _DENY_SUB):
        return True
    for seg in re.split(r"[^A-Z0-9]+", k):
        if seg == "KEY" or seg == "AUTH" or seg.startswith("AUTH"):
            return True
    return False


def _schema_validator():
    global _SCHEMA_VALIDATOR
    if _SCHEMA_VALIDATOR is None:
        import jsonschema
        with open(_PROFILES_SCHEMA_PATH, encoding="utf-8") as fh:
            _SCHEMA_VALIDATOR = jsonschema.Draft202012Validator(json.load(fh))
    return _SCHEMA_VALIDATOR


def _dep_rule_id_unused():
    pass


def load_profiles(path, expected_version=None):
    if not path or not os.path.isfile(path):
        raise TestRunnerError(INTERNAL, "profiles file not found: %s" % path)
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (ValueError, OSError) as exc:
        raise TestRunnerError(INTERNAL, "profiles not valid JSON: %s" % exc)
    errs = sorted(_schema_validator().iter_errors(doc), key=lambda e: list(e.absolute_path))
    if errs:
        p = "/".join(str(x) for x in errs[0].absolute_path) or "<root>"
        raise TestRunnerError(INTERNAL, "%s: %s" % (p, errs[0].message))
    version = doc["profiles_version"]
    if int(version.split(".")[0]) != SUPPORTED_PROFILES_MAJOR:
        raise TestRunnerError(INTERNAL, "profiles_version %s unsupported" % version)
    if expected_version is not None and expected_version != version:
        raise TestRunnerError(INPUT_INVALID, "expected_profiles_version %s != %s" % (expected_version, version))
    by_key = {}
    for p in doc["profiles"]:
        if p["runner_key"] in by_key:
            raise TestRunnerError(INTERNAL, "duplicate runner_key %s" % p["runner_key"])
        by_key[p["runner_key"]] = p
    return version, by_key


def _safe_rel(rel):
    if not isinstance(rel, str) or not rel:
        raise TestRunnerError(INPUT_INVALID, "empty test_path")
    norm = rel.replace("\\", "/")
    if os.path.isabs(rel) or norm.startswith("/") or norm.startswith("~"):
        raise TestRunnerError(PATH_ESCAPE, "absolute/home test_path rejected")
    if any(p == ".." for p in norm.split("/")):
        raise TestRunnerError(PATH_ESCAPE, "'..' in test_path rejected")
    return "/".join(p for p in norm.split("/") if p)


def _is_reparse(path):
    import stat as _stat
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if _stat.S_ISLNK(st.st_mode):
        return True
    return bool(getattr(st, "st_file_attributes", 0) & 0x400)


def _assert_no_reparse_chain(path):
    """Reject symlink/junction/reparse point at the path OR any ancestor.
    Uses ``os.path.dirname`` iteration (not manual string building) to correctly
    handle POSIX /a/b, Windows C:\\a\\b, and UNC paths. Checks each existing
    ancestor via lstat BEFORE realpath, so link attributes are not lost."""
    p = os.path.abspath(path)
    ancestors = []
    while True:
        ancestors.append(p)
        parent = os.path.dirname(p)
        if parent == p:  # root/anchor reached
            break
        p = parent
    for a in reversed(ancestors):  # root → leaf
        if os.path.lexists(a) and _is_reparse(a):
            raise TestRunnerError(PATH_ESCAPE, "symlink/reparse in path chain: %s" % a)


def _new_run_id():
    """Globally unique per-call run id (process counter + full uuid4 + time).
    The full uuid4 gives cross-process uniqueness; the counter/time prefix aids
    ordering and diagnostics. We claim only the tested concurrency range, not
    mathematical global uniqueness."""
    import time as _time
    import uuid as _uuid
    global _RUN_SEQ
    with _RUN_LOCK:
        _RUN_SEQ += 1
        seq = _RUN_SEQ
    return "run-%d-%d-%s" % (seq, int(_time.time() * 1000), _uuid.uuid4().hex)


def _parse_cpus(s):
    if not isinstance(s, str) or not _CPUS_RE.match(s):
        raise TestRunnerError(TRUSTED_CONFIG_MISSING, "cpus invalid")
    v = float(s)
    if v < MIN_CPUS or v > MAX_CPUS:
        raise TestRunnerError(TRUSTED_CONFIG_MISSING, "cpus out of range [%g,%g]" % (MIN_CPUS, MAX_CPUS))
    return s


def _parse_memory_bytes(s):
    if not isinstance(s, str):
        raise TestRunnerError(TRUSTED_CONFIG_MISSING, "memory invalid")
    m = _MEM_RE.match(s)
    if not m:
        raise TestRunnerError(TRUSTED_CONFIG_MISSING, "memory invalid")
    n = int(m.group(1))
    unit = (m.group(2) or "").lower()
    mult = {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}[unit]
    bytes_ = n * mult
    if bytes_ < MIN_MEM_BYTES or bytes_ > MAX_MEM_BYTES:
        raise TestRunnerError(TRUSTED_CONFIG_MISSING, "memory out of range")
    return bytes_, s


def _validate_trusted(tc, profile, executor):
    """Fail-closed validation of deploy-owned trusted config."""
    ws = tc.get("workspace")
    if not ws or not os.path.isdir(ws):
        raise TestRunnerError(TRUSTED_CONFIG_MISSING, "trusted workspace missing or not a dir")
    _assert_no_reparse_chain(ws)  # reject symlink/junction/reparse in ws root or ancestors
    if executor not in ("container", "process"):
        raise TestRunnerError(NO_TRUSTED_EXECUTOR, "executor must be container|process")
    if executor == "process" and tc.get("trusted_dev") is not True:
        raise TestRunnerError(NO_TRUSTED_EXECUTOR, "process executor requires trusted_dev=true")
    np = tc.get("network_policy")
    if executor == "container":
        if np != "denied":
            raise TestRunnerError(NETWORK_DENIED, "v1 container requires network_policy=denied")
    else:
        if np != "allowed":
            raise TestRunnerError(NETWORK_DENIED, "process executor cannot enforce network=denied")
    if tc.get("transport") not in ("native", "wsl"):
        raise TestRunnerError(TRUSTED_CONFIG_MISSING, "transport must be native|wsl")
    if not _DISTRO_RE.match(tc.get("wsl_distro") or ""):
        raise TestRunnerError(TRUSTED_CONFIG_MISSING, "wsl_distro not a safe string")
    _parse_cpus(tc.get("cpus"))
    _parse_memory_bytes(tc.get("memory"))
    uid = tc.get("uid")
    if not isinstance(uid, int) or uid < MIN_UID or uid > MAX_UID:
        raise TestRunnerError(TRUSTED_CONFIG_MISSING, "uid must be a non-root integer [%d,%d]" % (MIN_UID, MAX_UID))
    gid = tc.get("gid")
    if not isinstance(gid, int) or gid < MIN_GID or gid > MAX_GID:
        raise TestRunnerError(TRUSTED_CONFIG_MISSING, "gid must be a non-root integer [%d,%d]" % (MIN_GID, MAX_GID))
    for key, lo, hi in (("pids_limit", 1, 4096), ("max_timeout_ms", 1000, HARD_MAX_TIMEOUT_MS),
                        ("max_output_bytes", 1, HARD_MAX_OUTPUT_BYTES)):
        v = tc.get(key)
        if not isinstance(v, int) or v < lo or v > hi:
            raise TestRunnerError(TRUSTED_CONFIG_MISSING, "%s out of range" % key)
    ar = tc.get("artifact_root")
    if not ar:
        raise TestRunnerError(TRUSTED_CONFIG_MISSING, "artifact_root must be deploy-provided")
    _assert_no_reparse_chain(ar)
    ar_real = os.path.realpath(ar)
    if not os.path.isdir(ar_real):
        raise TestRunnerError(TRUSTED_CONFIG_MISSING, "artifact_root not a dir")
    if executor == "container":
        _validate_image(tc.get("image"), profile.get("image_repository"))
    return ar_real


def _validate_image(image, repo):
    if not image:
        raise TestRunnerError(TRUSTED_CONFIG_MISSING, "image must be deploy-provided (repository@sha256:digest)")
    m = _DIGEST_RE.match(image)
    if not m:
        raise TestRunnerError(TRUSTED_CONFIG_MISSING, "image must be repository@sha256:<64hex>; tag-only rejected")
    if repo and m.group(1) != repo:
        raise TestRunnerError(TRUSTED_CONFIG_MISSING, "image repository %r != profile %r" % (m.group(1), repo))


def _trusted_config():
    def _int(name, default):
        try:
            return int(os.environ.get(name, str(default)))
        except ValueError:
            return -1  # invalid -> caught by validator
    tc = {
        "workspace": os.environ.get("MERGEPILOT_TR_WORKSPACE"),
        "executor": os.environ.get("MERGEPILOT_TR_EXECUTOR", "container"),
        "network_policy": os.environ.get("MERGEPILOT_TR_NETWORK_POLICY", "denied"),
        "env_allowlist": os.environ.get("MERGEPILOT_TR_ENV_ALLOWLIST", ""),
        "image": os.environ.get("MERGEPILOT_TR_IMAGE"),
        "trusted_dev": os.environ.get("MERGEPILOT_TR_TRUSTED_DEV", "") == "true",
        "transport": os.environ.get("MERGEPILOT_TR_DOCKER_TRANSPORT", "auto"),
        "wsl_distro": os.environ.get("MERGEPILOT_TR_WSL_DISTRO", "Ubuntu-22.04"),
        "artifact_root": os.environ.get("MERGEPILOT_TR_ARTIFACT_ROOT"),
        "cpus": os.environ.get("MERGEPILOT_TR_CPUS", "1.0"),
        "memory": os.environ.get("MERGEPILOT_TR_MEMORY", "512m"),
        "uid": _int("MERGEPILOT_TR_UID", DEFAULT_UID),
        "gid": _int("MERGEPILOT_TR_GID", DEFAULT_GID),
        "pids_limit": _int("MERGEPILOT_TR_PIDS", 64),
        "max_timeout_ms": _int("MERGEPILOT_TR_MAX_TIMEOUT_MS", 60000),
        "max_output_bytes": _int("MERGEPILOT_TR_MAX_OUTPUT_BYTES", DEFAULT_MAX_OUTPUT_BYTES),
    }
    if tc["transport"] == "auto":
        from shutil import which
        tc["transport"] = "native" if which("docker") else "wsl"
    allow = set()
    for k in (tc["env_allowlist"] or "").split(","):
        k = k.strip().upper()
        if k:
            allow.add(k)
    tc["env_allowlist"] = allow
    return tc


def _effective_env(env_values, allowlist):
    """allowlist ∩ caller_values − sensitive keys. Empty allowlist -> {}."""
    out = {}
    for k, v in (env_values or {}).items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        if len(k) > MAX_ENV_KEY_LEN or len(v) > MAX_ENV_VAL_LEN:
            continue
        if k.upper() not in allowlist:
            continue
        if _is_sensitive_key(k):
            continue
        out[k] = v
    if len(out) > MAX_ENV_KEYS:
        out = dict(list(out.items())[:MAX_ENV_KEYS])
    return out


def _baseline_env():
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"}
    if os.name == "nt":
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")
    return env


def _copy_workspace(src_root, dst_root, max_bytes, deadline=None):
    """One-shot copy. ANY symlink/junction/reparse/socket/device -> raise.
    Cooperative deadline check in the bounded file loop."""
    copied = [0]
    for dirpath, dirnames, filenames in os.walk(src_root, followlinks=False):
        for d in list(dirnames):
            full = os.path.join(dirpath, d)
            if _is_reparse(full):
                raise TestRunnerError(PATH_ESCAPE, "symlink/reparse dir rejected: %s" % d)
            if d in _EXCLUDED_DIRS:
                dirnames.remove(d)
        for f in filenames:
            if deadline is not None:
                deadline.check()
            full = os.path.join(dirpath, f)
            if _is_reparse(full):
                raise TestRunnerError(PATH_ESCAPE, "symlink/reparse file rejected: %s" % f)
            if _EXCLUDED_FILE_RE.match(f) or f in _EXCLUDED_DIRS:
                continue
            if not os.path.isfile(full):
                raise TestRunnerError(INTERNAL, "non-regular file rejected: %s" % f)
            rel = os.path.relpath(full, src_root)
            dst = os.path.join(dst_root, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            try:
                sz = os.path.getsize(full)
            except OSError as exc:
                raise TestRunnerError(INTERNAL, "stat failed for %s: %s" % (f, exc))
            if copied[0] + sz > max_bytes:
                raise TestRunnerError(INPUT_INVALID, "workspace copy exceeds sandbox byte cap")
            shutil.copy2(full, dst)
            copied[0] += sz


def _sha_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_pytest_summary(text):
    out = {}
    for key in ("passed", "failed", "skipped", "errors"):
        mm = re.search(r"(\d+)\s+%s" % key, text)
        if mm:
            out[key] = int(mm.group(1))
    return out


def _classify(executor, started, timed_out, exit_code, cleanup_ok, phase_error=False):
    """Return (verdict, runtime_error_subcode)."""
    # Phase 1 exception (after container/process may have started) -> INTERNAL,
    # NOT CONTAINER_UNAVAILABLE (the executor was reachable; it failed internally).
    if phase_error:
        return ERROR, INTERNAL
    if not started:
        return ERROR, CONTAINER_UNAVAILABLE if executor == "container" else EXEC_UNAVAILABLE
    if not cleanup_ok:
        return ERROR, INTERNAL
    if timed_out:
        return TIMEOUT, TIMEOUT_SUB
    if executor == "container":
        if exit_code in (126, 127):
            return ERROR, EXEC_UNAVAILABLE
        if exit_code == 125:
            return ERROR, CONTAINER_UNAVAILABLE
    if exit_code == 0:
        return PASS, None
    if exit_code == 1 or exit_code == 5:
        return FAIL, None
    return ERROR, INTERNAL


def _ensure_artifact_dir(artifact_root_real, run_id):
    target = os.path.realpath(os.path.join(artifact_root_real, run_id))
    if not (target == artifact_root_real or target.startswith(artifact_root_real.rstrip(os.sep) + os.sep)):
        raise TestRunnerError(PATH_ESCAPE, "artifact dir escapes root")
    if _is_reparse(target):
        raise TestRunnerError(PATH_ESCAPE, "artifact dir is symlink/reparse")
    # unique run_id -> target must not exist; if it does (collision), make unique
    if os.path.exists(target):
        target = os.path.realpath(os.path.join(artifact_root_real, run_id + "-" + _new_run_id()))
    os.makedirs(target)
    return target


def _collect_artifacts(artifact_dir):
    """Recursive collection; symlink/socket/device -> fail-closed (raise); enforce
    MAX_ARTIFACT_FILES and MAX_ARTIFACT_BYTES. Returns (artifacts_list)."""
    out = []
    total = 0
    for dirpath, dirnames, filenames in os.walk(artifact_dir, followlinks=False):
        for d in list(dirnames):
            full = os.path.join(dirpath, d)
            if _is_reparse(full):
                raise TestRunnerError(INTERNAL, "symlink/reparse artifact dir rejected: %s" % d)
        for f in filenames:
            full = os.path.join(dirpath, f)
            if _is_reparse(full):
                raise TestRunnerError(INTERNAL, "symlink/reparse artifact rejected: %s" % f)
            if not os.path.isfile(full):
                raise TestRunnerError(INTERNAL, "non-regular artifact rejected: %s" % f)
            try:
                sz = os.path.getsize(full)
            except OSError as exc:
                raise TestRunnerError(INTERNAL, "artifact stat failed: %s" % exc)
            total += sz
            if total > MAX_ARTIFACT_BYTES:
                raise TestRunnerError(INTERNAL, "artifacts exceed MAX_ARTIFACT_BYTES")
            if len(out) >= MAX_ARTIFACT_FILES:
                raise TestRunnerError(INTERNAL, "artifacts exceed MAX_ARTIFACT_FILES")
            rel = os.path.relpath(full, artifact_dir).replace(os.sep, "/")
            out.append({"name": rel, "rel_path": rel, "size": sz, "digest": _sha_file(full)})
    out.sort(key=lambda a: a["rel_path"])
    return out


def _try_rmtree(path):
    if path and os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=False)


def _is_timeout_exc(exc):
    """True if exc is the common runtime's cooperative deadline timeout."""
    try:
        from skills.common.runtime import errors as _e
        return isinstance(exc, _e.SkillTimeout)
    except Exception:  # noqa: BLE001
        return False


def run(inp, profiles_path=None, trusted=None, expected_profiles_version=None, deadline=None):
    from skills.test_runner.executors import subprocess_executor, container_executor
    import tempfile

    profiles_version, profiles = load_profiles(profiles_path or DEFAULT_PROFILES_PATH, expected_profiles_version)
    runner_key = inp.get("runner_key")
    if runner_key not in profiles:
        raise TestRunnerError(INVALID_COMMAND, "runner_key not a known profile: %r" % runner_key)
    profile = profiles[runner_key]

    test_paths = inp.get("test_paths") or []
    safe_paths = [_safe_rel(p) for p in test_paths]
    if not safe_paths:
        raise TestRunnerError(INPUT_INVALID, "test_paths required")

    trusted = trusted or _trusted_config()
    executor = trusted["executor"]
    artifact_root_real = _validate_trusted(trusted, profile, executor)

    workspace_real = os.path.realpath(trusted["workspace"])
    for rel in safe_paths:
        joined = os.path.join(workspace_real, rel)
        if _is_reparse(joined):
            raise TestRunnerError(PATH_ESCAPE, "symlink test_path rejected")
        real = os.path.realpath(joined)
        if not (real == workspace_real or real.startswith(workspace_real.rstrip(os.sep) + os.sep)):
            raise TestRunnerError(PATH_ESCAPE, "test_path escapes workspace")
        if not os.path.isfile(real):
            raise TestRunnerError(INPUT_INVALID, "test_path missing or excluded: %s" % rel)

    caller_env = _effective_env(inp.get("env_values"), trusted["env_allowlist"])
    requested_timeout = min(int(inp.get("timeout_ms") or 60000), trusted["max_timeout_ms"])
    total_output_budget = min(int(inp.get("max_output_bytes") or DEFAULT_MAX_OUTPUT_BYTES),
                              trusted["max_output_bytes"], HARD_MAX_OUTPUT_BYTES)
    per_stream = total_output_budget // 2  # total budget split; budget=1 -> 0 per stream
    np_ = trusted["network_policy"]

    def _deadline_remaining():
        return deadline.remaining_ms() if deadline is not None else requested_timeout

    # per-run artifact dir (unique run id; no fallback tmp)
    run_id = _new_run_id()
    artifact_dir = _ensure_artifact_dir(artifact_root_real, run_id)

    # sandbox copy (one-shot; reject any symlink/reparse/socket/device; cooperative deadline)
    sandbox = tempfile.mkdtemp(prefix="mp-sandbox-")
    try:
        _copy_workspace(workspace_real, sandbox, DEFAULT_SANDBOX_BYTES, deadline=deadline)
    except Exception as exc:
        _try_rmtree(sandbox)
        _try_rmtree(artifact_dir)
        if _is_timeout_exc(exc):
            return _timeout_output(profiles_version, runner_key, executor, trusted, requested_timeout, total_output_budget, np_)
        if isinstance(exc, TestRunnerError):
            raise
        raise TestRunnerError(INTERNAL, "sandbox copy failed: %s" % exc)

    # recompute remaining AFTER copy; exec timeout must finish before skill deadline
    exec_timeout = min(requested_timeout, _deadline_remaining() - CLEANUP_BUDGET_MS)
    if exec_timeout < MIN_EXEC_MS:
        _try_rmtree(sandbox)
        _try_rmtree(artifact_dir)
        return _timeout_output(profiles_version, runner_key, executor, trusted, requested_timeout, total_output_budget, np_)

    argv = [profile["executable"]]
    if profile.get("module"):
        argv += ["-m", profile["module"]]
    argv += list(profile.get("fixed_args", []))
    argv += safe_paths

    plan = {
        "argv": argv, "cwd": sandbox,
        "env": dict(_baseline_env(), **caller_env) if executor == "process" else caller_env,
        "timeout_ms": exec_timeout, "max_output_bytes": per_stream, "run_id": run_id,
        "memory": trusted["memory"], "cpus": trusted["cpus"], "pids_limit": trusted["pids_limit"],
        "host_uid": trusted["uid"],
        "host_gid": trusted["gid"],
        "transport": trusted["transport"], "wsl_distro": trusted["wsl_distro"],
        "image": trusted["image"], "artifact_dir": artifact_dir,
    }

    try:
        if executor == "container":
            res = container_executor.run(plan)
        else:
            res = subprocess_executor.run(plan)
    except FileNotFoundError:
        res = {"started": False}
    except Exception as exc:  # noqa: BLE001
        res = {"started": False, "_exc": str(exc)}

    cleanup_ok = bool(res.get("cleanup_ok", True))
    try:
        _try_rmtree(sandbox)
    except Exception:  # noqa: BLE001
        cleanup_ok = False

    # verdict first (needed to decide whether artifacts are trustworthy)
    verdict, runtime_error = _classify(executor, res.get("started"), res.get("timed_out"),
                                       res.get("exit_code"), cleanup_ok,
                                       phase_error=bool(res.get("_phase_error")))
    summary = _parse_pytest_summary(res.get("stdout_text", "")) if verdict in (PASS, FAIL) else {}

    # Artifacts: only collected for subprocess executor (process mode has a
    # real host artifact dir). Container executor uses ephemeral tmpfs
    # (artifacts always [] per frozen M4-C v1 contract).
    artifacts = []
    if verdict in (PASS, FAIL) and executor == "process":
        try:
            artifacts = _collect_artifacts(artifact_dir)
        except Exception:  # noqa: BLE001
            cleanup_ok = False
    # remove the per-run artifact dir (empty or process artifacts already extracted)
    try:
        _try_rmtree(artifact_dir)
    except Exception:  # noqa: BLE001
        cleanup_ok = False
    # re-classify: if artifact collection failed, verdict becomes ERROR
    if not cleanup_ok and verdict in (PASS, FAIL):
        verdict, runtime_error = ERROR, INTERNAL

    summary = _parse_pytest_summary(res.get("stdout_text", "")) if verdict in (PASS, FAIL) else {}
    out = {
        "schema_version": SCHEMA_VERSION,
        "profiles_version": profiles_version,
        "runner_key": runner_key,
        "verdict": verdict,
        "exit_code": res.get("exit_code"),
        "duration_ms": int(res.get("duration_ms", 0)),
        "timed_out": bool(res.get("timed_out")),
        "summary": summary,
        "stdout_digest": res.get("stdout_digest") or hashlib.sha256(b"").hexdigest(),
        "stderr_digest": res.get("stderr_digest") or hashlib.sha256(b"").hexdigest(),
        "stdout_tail": (res.get("stdout_text") or "")[-per_stream:],
        "stderr_tail": (res.get("stderr_text") or "")[-per_stream:],
        "executor": "subprocess" if executor == "process" else "container",
        "isolation": "process" if executor == "process" else "container",
        "network_policy": np_,
        "resource_limits": {
            "timeout_ms": requested_timeout, "max_output_bytes": total_output_budget,
            "memory": trusted["memory"], "cpus": trusted["cpus"], "pids_limit": trusted["pids_limit"],
        },
        "truncated": bool(res.get("truncated")),
        "artifacts": artifacts,
        "_runtime_error": runtime_error,
        "_executed": bool(res.get("started") or res.get("_execution_attempted")),
    }
    return out


def _timeout_output(profiles_version, runner_key, executor, trusted, requested_timeout, total_output_budget, network_policy):
    z = hashlib.sha256(b"").hexdigest()
    return {
        "schema_version": SCHEMA_VERSION, "profiles_version": profiles_version, "runner_key": runner_key,
        "verdict": TIMEOUT, "exit_code": None, "duration_ms": 0, "timed_out": True, "summary": {},
        "stdout_digest": z, "stderr_digest": z, "stdout_tail": "", "stderr_tail": "",
        "executor": "subprocess" if executor == "process" else "container",
        "isolation": "process" if executor == "process" else "container", "network_policy": network_policy,
        "resource_limits": {"timeout_ms": requested_timeout, "max_output_bytes": total_output_budget,
                            "memory": trusted["memory"], "cpus": trusted["cpus"], "pids_limit": trusted["pids_limit"]},
        "truncated": False, "artifacts": [], "_runtime_error": TIMEOUT_SUB, "_executed": False,
    }
