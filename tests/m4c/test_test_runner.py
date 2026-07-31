"""M4-C TestRunner tests (deterministic, fixed count).

Live subprocess tests use a fixture profile whose executable is the interpreter
running pytest. Exit-code classification, trusted-config validation, env
sanitisation, image-digest validation, cleanup, artifact isolation and path
safety are unit-tested without a Docker daemon.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

import jsonschema
import pytest

from skills.common.runtime import errors
from skills.test_runner import core
from skills.test_runner.executors import _common
from skills.test_runner import run as tr

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _write_ws(ws):
    os.makedirs(os.path.join(ws, "tests"))
    with open(os.path.join(ws, "tests", "test_ok.py"), "w", encoding="utf-8") as fh:
        fh.write("def test_ok():\n    assert 1 + 1 == 2\n")


def _venv_profile(path):
    doc = {"profiles_version": "1.0.0", "profiles": [{
        "runner_key": "pytest", "executable": sys.executable, "module": "pytest",
        "fixed_args": ["-q", "-p", "no:cacheprovider"], "image_repository": "mergepilot/test-runner-py",
        "tool_version": "pytest==8.4.2", "network_required": False}]}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)


def _trusted_env(ws, artifact_root, **over):
    base = {"MERGEPILOT_TR_WORKSPACE": ws, "MERGEPILOT_TR_EXECUTOR": "process",
            "MERGEPILOT_TR_TRUSTED_DEV": "true", "MERGEPILOT_TR_NETWORK_POLICY": "allowed",
            "MERGEPILOT_TR_MAX_TIMEOUT_MS": "60000", "MERGEPILOT_TR_ARTIFACT_ROOT": artifact_root}
    base.update(over)
    return base


def _output_validator():
    with open(os.path.join(_REPO_ROOT, "skills", "test_runner", "schema", "output.schema.json"), encoding="utf-8") as fh:
        return jsonschema.Draft202012Validator(json.load(fh))


# --------------------------------------------------------------------------- #
# live subprocess (trusted-dev)
# --------------------------------------------------------------------------- #
def test_01_pass(tmp_path, monkeypatch):
    ws = str(tmp_path / "ws"); os.makedirs(ws); _write_ws(ws)
    prof = str(tmp_path / "p.json"); _venv_profile(prof)
    os.makedirs(str(tmp_path / "art"), exist_ok=True)
    for k, v in _trusted_env(ws, str(tmp_path / "art")).items():
        monkeypatch.setenv(k, v)
    out = core.run({"runner_key": "pytest", "test_paths": ["tests/test_ok.py"], "timeout_ms": 30000}, profiles_path=prof)
    out.pop("_runtime_error", None); out.pop("_executed", None)
    assert out["verdict"] == "PASS" and out["exit_code"] == 0
    assert out["summary"]["passed"] == 1
    assert out["network_policy"] == "allowed" and out["executor"] == "subprocess"
    _output_validator().validate(out)


def test_02_fail(tmp_path, monkeypatch):
    ws = str(tmp_path / "ws"); os.makedirs(ws)
    with open(os.path.join(ws, "t.py"), "w", encoding="utf-8") as fh:
        fh.write("def test_bad():\n    assert 1 == 2\n")
    prof = str(tmp_path / "p.json"); _venv_profile(prof)
    os.makedirs(str(tmp_path / "art"), exist_ok=True)
    for k, v in _trusted_env(ws, str(tmp_path / "art")).items():
        monkeypatch.setenv(k, v)
    out = core.run({"runner_key": "pytest", "test_paths": ["t.py"], "timeout_ms": 30000}, profiles_path=prof)
    assert out["verdict"] == "FAIL" and out["exit_code"] == 1
    assert out["summary"]["failed"] == 1


def test_03_timeout(tmp_path, monkeypatch):
    ws = str(tmp_path / "ws"); os.makedirs(os.path.join(ws, "tests"))
    with open(os.path.join(ws, "tests", "x.py"), "w", encoding="utf-8") as fh:
        fh.write("import time\ndef test_slow():\n    time.sleep(5)\n")
    prof = str(tmp_path / "p.json"); _venv_profile(prof)
    os.makedirs(str(tmp_path / "art"), exist_ok=True)
    for k, v in _trusted_env(ws, str(tmp_path / "art")).items():
        monkeypatch.setenv(k, v)
    out = core.run({"runner_key": "pytest", "test_paths": ["tests/x.py"], "timeout_ms": 1500}, profiles_path=prof)
    assert out["verdict"] == "TIMEOUT" and out["timed_out"] is True
    assert out["_runtime_error"] == core.TIMEOUT_SUB


# --------------------------------------------------------------------------- #
# error / boundary
# --------------------------------------------------------------------------- #
def test_04_unknown_runner(tmp_path, monkeypatch):
    ws = str(tmp_path / "ws"); os.makedirs(ws)
    prof = str(tmp_path / "p.json"); _venv_profile(prof)
    for k, v in _trusted_env(ws, str(tmp_path / "art")).items():
        monkeypatch.setenv(k, v)
    with pytest.raises(core.TestRunnerError) as ei:
        core.run({"runner_key": "npm", "test_paths": ["x"]}, profiles_path=prof)
    assert ei.value.subcode == core.INVALID_COMMAND


def test_05_path_escape(tmp_path, monkeypatch):
    ws = str(tmp_path / "ws"); os.makedirs(ws)
    prof = str(tmp_path / "p.json"); _venv_profile(prof)
    for k, v in _trusted_env(ws, str(tmp_path / "art")).items():
        monkeypatch.setenv(k, v)
    with pytest.raises(core.TestRunnerError) as ei:
        core.run({"runner_key": "pytest", "test_paths": ["../secret"]}, profiles_path=prof)
    assert ei.value.subcode == core.PATH_ESCAPE


def test_06_missing_artifact_root(tmp_path, monkeypatch):
    ws = str(tmp_path / "ws"); os.makedirs(ws); _write_ws(ws)
    prof = str(tmp_path / "p.json"); _venv_profile(prof)
    for k, v in _trusted_env(ws, "").items():
        if k == "MERGEPILOT_TR_ARTIFACT_ROOT":
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    with pytest.raises(core.TestRunnerError) as ei:
        core.run({"runner_key": "pytest", "test_paths": ["tests/test_ok.py"]}, profiles_path=prof)
    assert ei.value.subcode == core.TRUSTED_CONFIG_MISSING


def test_07_process_without_trusted_dev(tmp_path, monkeypatch):
    ws = str(tmp_path / "ws"); os.makedirs(ws); _write_ws(ws)
    prof = str(tmp_path / "p.json"); _venv_profile(prof)
    for k, v in _trusted_env(ws, str(tmp_path / "art"), **{"MERGEPILOT_TR_TRUSTED_DEV": "false"}).items():
        monkeypatch.setenv(k, v)
    with pytest.raises(core.TestRunnerError) as ei:
        core.run({"runner_key": "pytest", "test_paths": ["tests/test_ok.py"]}, profiles_path=prof)
    assert ei.value.subcode == core.NO_TRUSTED_EXECUTOR


# --------------------------------------------------------------------------- #
# T1 env allowlist fail-closed
# --------------------------------------------------------------------------- #
def test_08_empty_allowlist_allows_zero():
    eff = core._effective_env({"FOO": "1", "BAR": "2"}, allowlist=set())
    assert eff == {}


def test_09_allowlist_case_insensitive_minus_sensitive():
    eff = core._effective_env({"foo": "1", "API_TOKEN": "x", "PG_PASS": "x", "MERGEPILOT_TR_X": "x"},
                              allowlist={"FOO", "API_TOKEN", "PG_PASS", "MERGEPILOT_TR_X"})
    assert eff == {"foo": "1"}


# --------------------------------------------------------------------------- #
# T2 trusted-config validation (fail-closed -> DENIED/CONFIG, not FAIL)
# --------------------------------------------------------------------------- #
def test_10_container_requires_denied_network(tmp_path, monkeypatch):
    ws = str(tmp_path / "ws"); os.makedirs(ws); _write_ws(ws)
    prof = str(tmp_path / "p.json"); _venv_profile(prof)
    for k, v in _trusted_env(ws, str(tmp_path / "art"),
                             **{"MERGEPILOT_TR_EXECUTOR": "container", "MERGEPILOT_TR_NETWORK_POLICY": "allowed",
                                "MERGEPILOT_TR_IMAGE": "mergepilot/test-runner-py@sha256:" + "a" * 64}).items():
        monkeypatch.setenv(k, v)
    with pytest.raises(core.TestRunnerError) as ei:
        core.run({"runner_key": "pytest", "test_paths": ["tests/test_ok.py"]}, profiles_path=prof)
    assert ei.value.subcode in (core.NETWORK_DENIED, core.TRUSTED_CONFIG_MISSING)


# --------------------------------------------------------------------------- #
# T3 image must be deploy digest; tag-only rejected
# --------------------------------------------------------------------------- #
def test_11_tag_only_image_rejected():
    with pytest.raises(core.TestRunnerError) as ei:
        core._validate_image("mergepilot/test-runner-py:1.0.0", "mergepilot/test-runner-py")
    assert ei.value.subcode == core.TRUSTED_CONFIG_MISSING


def test_12_digest_image_accepted_repo_mismatch_rejected():
    core._validate_image("mergepilot/test-runner-py@sha256:" + "a" * 64, "mergepilot/test-runner-py")
    with pytest.raises(core.TestRunnerError):
        core._validate_image("other/repo@sha256:" + "a" * 64, "mergepilot/test-runner-py")


# --------------------------------------------------------------------------- #
# T4 exit classification
# --------------------------------------------------------------------------- #
def test_13_pytest_exit_classification():
    assert core._classify("subprocess", True, False, 0, True)[0] == "PASS"
    assert core._classify("subprocess", True, False, 1, True)[0] == "FAIL"
    assert core._classify("subprocess", True, False, 5, True)[0] == "FAIL"  # no tests collected
    for c in (2, 3, 4):
        v, re_ = core._classify("subprocess", True, False, c, True)
        assert v == "ERROR" and re_ == core.INTERNAL


def test_14_docker_exit_classification():
    assert core._classify("container", True, False, 0, True)[0] == "PASS"
    assert core._classify("container", True, False, 125, True) == ("ERROR", core.CONTAINER_UNAVAILABLE)
    assert core._classify("container", True, False, 126, True) == ("ERROR", core.EXEC_UNAVAILABLE)
    assert core._classify("container", True, False, 127, True) == ("ERROR", core.EXEC_UNAVAILABLE)
    assert core._classify("container", False, False, None, True) == ("ERROR", core.CONTAINER_UNAVAILABLE)


# --------------------------------------------------------------------------- #
# T5 cleanup fail-closed (mock residue)
# --------------------------------------------------------------------------- #
def test_15_cleanup_failure_propagates(monkeypatch):
    import skills.test_runner.executors.container_executor as ce
    plan = {"transport": "native", "wsl_distro": "Ubuntu-22.04", "cwd": "/tmp", "artifact_dir": "/tmp",
            "run_id": "r1", "image": "x@sha256:" + "a" * 64, "argv": ["python"], "env": {},
            "timeout_ms": 5000, "max_output_bytes": 4096, "memory": "512m", "cpus": "1.0",
            "pids_limit": 64, "host_uid": 1000, "host_gid": 1000}

    def fake_run_captured(*a, **k):
        # simulate cleanup hook raising (residue) -> cleanup_ok False
        hook = k.get("cleanup_hook")
        cleanup_ok = True
        if hook:
            try:
                hook()
            except Exception:
                cleanup_ok = False
        return {"started": True, "exit_code": 0, "timed_out": False, "duration_ms": 1,
                "stdout_text": "", "stderr_text": "", "stdout_digest": "d" * 64, "stderr_digest": "d" * 64,
                "truncated": False, "cleanup_ok": cleanup_ok, "executor": "container", "isolation": "container",
                "artifacts": []}
    monkeypatch.setattr("skills.test_runner.executors._common.run_captured", fake_run_captured)
    # make _cleanup_container raise (mocked directly, not via subprocess.run)
    monkeypatch.setattr(ce, "_cleanup_container", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("cleanup fail")))
    res = ce.run(plan)
    assert res["cleanup_ok"] is False


def _raise():
    raise RuntimeError("docker rm failed")


# --------------------------------------------------------------------------- #
# T6 per-run artifact dir + isolation
# --------------------------------------------------------------------------- #
def test_16_artifact_per_run_dir_isolation_and_no_empty(tmp_path, monkeypatch):
    # A no-artifact run (process mode) must NOT leave an empty run dir behind;
    # and two consecutive same-path runs get distinct run ids and both succeed.
    ws = str(tmp_path / "ws"); os.makedirs(ws); _write_ws(ws)
    art = str(tmp_path / "art"); os.makedirs(art)
    prof = str(tmp_path / "p.json"); _venv_profile(prof)
    for k, v in _trusted_env(ws, art).items():
        monkeypatch.setenv(k, v)
    o1 = core.run({"runner_key": "pytest", "test_paths": ["tests/test_ok.py"], "timeout_ms": 30000}, profiles_path=prof)
    o2 = core.run({"runner_key": "pytest", "test_paths": ["tests/test_ok.py"], "timeout_ms": 30000}, profiles_path=prof)
    assert o1["verdict"] == "PASS" and o2["verdict"] == "PASS"
    # no artifacts produced -> no empty run dirs left in the controlled root
    subdirs = [d for d in os.listdir(art) if os.path.isdir(os.path.join(art, d))]
    assert subdirs == []


def test_16b_artifact_collection_caps_and_unsafe_types(tmp_path):
    # _collect_artifacts: recursive, MAX caps enforced, symlink/socket/device fail-closed
    ad = tmp_path / "art"; (ad / "sub").mkdir(parents=True)
    (ad / "a.txt").write_text("hello", encoding="utf-8")
    (ad / "sub" / "b.txt").write_text("world", encoding="utf-8")
    arts = core._collect_artifacts(str(ad))
    assert sorted(a["rel_path"] for a in arts) == ["a.txt", "sub/b.txt"]
    assert all(a["digest"] for a in arts)
    # too many files -> fail-closed
    ad2 = tmp_path / "art2"; ad2.mkdir()
    for i in range(core.MAX_ARTIFACT_FILES + 1):
        (ad2 / ("f%d" % i)).write_text("x", encoding="utf-8")
    with pytest.raises(core.TestRunnerError) as ei:
        core._collect_artifacts(str(ad2))
    assert ei.value.subcode == core.INTERNAL


def test_16c_run_id_unique_per_call():
    ids = {core._new_run_id() for _ in range(200)}
    assert len(ids) == 200  # all unique (concurrency + retries safe)


# --------------------------------------------------------------------------- #
# T7 workspace symlink overall reject
# --------------------------------------------------------------------------- #
def test_17_workspace_symlink_rejected(tmp_path):
    ws = tmp_path / "ws"; ws.mkdir(); (ws / "real.py").write_text("x=1\n", encoding="utf-8")
    try:
        os.symlink(str(ws / "real.py"), str(ws / "link.py"))
    except (OSError, NotImplementedError):
        pytest.skip("symlink not supported")
    dst = tmp_path / "sandbox"
    with pytest.raises(core.TestRunnerError) as ei:
        core._copy_workspace(str(ws), str(dst), 64 * 1024 * 1024)
    assert ei.value.subcode in (core.PATH_ESCAPE, core.INTERNAL)


# --------------------------------------------------------------------------- #
# T8/T9/T10/T11: argv, wsl path, docker argv, schema, network, cli timeout
# --------------------------------------------------------------------------- #
def test_18_wsl_path_translation():
    assert _common.to_wsl_path(r"D:\goai\merge pilot") == "/mnt/d/goai/merge pilot"
    assert _common.to_wsl_path(r"C:\Users\fé") == "/mnt/c/Users/fé"
    assert _common.to_wsl_path(r"d:\X") == "/mnt/d/X"  # drive case-insensitive


def test_19_docker_argv_hardening():
    argv = _common.build_docker_run_argv(
        "wsl", "Ubuntu-22.04", "repo@sha256:" + "a" * 64, "/mnt/d/work",
        ["python", "-m", "pytest", "tests/"], "rid", "mp-tr-rid",
        "512m", "1.0", 64, 1000, 1000, ["FOO=bar"])
    assert argv[:3] == ["wsl.exe", "-d", "Ubuntu-22.04"]
    for tok in ["--network=none", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                "--read-only", "--pids-limit=64", "--memory=512m", "--user=1000:1000",
                "--label=mp-run=rid", "-e", "FOO=bar", "repo@sha256:" + "a" * 64]:
        assert tok in argv
    s = " ".join(argv)
    assert "/work:ro" in s
    # /artifacts uses tmpfs (NOT a volume or host bind mount)
    assert "--tmpfs" in argv
    tmpfs_args = [argv[i+1] for i in range(len(argv)) if argv[i] == "--tmpfs"]
    artifacts_tmpfs = [t for t in tmpfs_args if "/artifacts" in t]
    assert len(artifacts_tmpfs) == 1
    assert "size=8388608" in artifacts_tmpfs[0]  # 8 MiB execution-time limit
    # no -v mount for /artifacts (not a host bind, not a volume)
    v_args = [argv[i+1] for i in range(len(argv)) if argv[i] == "-v"]
    assert not any("/artifacts" in v for v in v_args)


def test_20_input_schema_rejects_forbidden_fields():
    with open(os.path.join(_REPO_ROOT, "skills", "test_runner", "schema", "input.schema.json"), encoding="utf-8") as fh:
        v = jsonschema.Draft202012Validator(json.load(fh))
    assert list(v.iter_errors({"runner_key": "pytest", "test_paths": ["t"], "command": "rm -rf /"}))
    assert list(v.iter_errors({"runner_key": "pytest", "test_paths": ["t"], "workspace_root": "/h"}))
    assert list(v.iter_errors({"runner_key": "pytest", "test_paths": ["t"], "artifact_globs": ["x"]}))


def test_21_env_values_must_be_strings():
    with open(os.path.join(_REPO_ROOT, "skills", "test_runner", "schema", "input.schema.json"), encoding="utf-8") as fh:
        v = jsonschema.Draft202012Validator(json.load(fh))
    assert list(v.iter_errors({"runner_key": "pytest", "test_paths": ["t"], "env_values": {"X": 123}}))


def test_22_cli_structured_timeout(tmp_path, monkeypatch):
    # exec_timeout < MIN_EXEC_MS when deadline is nearly exhausted -> structured TIMEOUT
    ws = str(tmp_path / "ws"); os.makedirs(ws); _write_ws(ws)
    art = str(tmp_path / "art"); os.makedirs(art)
    prof = str(tmp_path / "p.json"); _venv_profile(prof)
    for k, v in _trusted_env(ws, art).items():
        monkeypatch.setenv(k, v)

    class NearDeadline:
        def remaining_ms(self): return 50  # < MIN_EXEC_MS + budget
        def check(self): pass
    out = core.run({"runner_key": "pytest", "test_paths": ["tests/test_ok.py"], "timeout_ms": 30000},
                   profiles_path=prof, deadline=NearDeadline())
    assert out["verdict"] == "TIMEOUT" and out["_runtime_error"] == core.TIMEOUT_SUB
    out.pop("_runtime_error", None); out.pop("_executed", None)
    _output_validator().validate(out)


def test_23_bundled_profile_cli_plumbing(tmp_path):
    # REAL bundled profile (executable=python) via CLI end-to-end. Asserts the
    # bundled profile is loaded and produces a structured BUSINESS verdict with
    # the correct contract/profile/executor/isolation/network fields and a
    # schema-valid output (not a runner ERROR, not "any envelope"). The bundled
    # profile's python is the container interpreter; on the host subprocess it may
    # PASS or FAIL depending on interpreter availability, but must be a business
    # verdict -- never a runner misclassification leaking as ERROR.
    ws = tmp_path / "ws"; ws.mkdir(); (ws / "tests").mkdir()
    (ws / "tests" / "test_ok.py").write_text("def test_ok():\n    assert 2 == 2\n", encoding="utf-8")
    venv_scripts = os.path.dirname(sys.executable)
    env = dict(os.environ)
    env["PATH"] = venv_scripts + os.pathsep + env.get("PATH", "")
    os.makedirs(str(tmp_path / "art"), exist_ok=True)
    env.update({"MERGEPILOT_TR_WORKSPACE": str(ws), "MERGEPILOT_TR_EXECUTOR": "process",
                "MERGEPILOT_TR_TRUSTED_DEV": "true", "MERGEPILOT_TR_NETWORK_POLICY": "allowed",
                "MERGEPILOT_TR_ARTIFACT_ROOT": str(tmp_path / "art"),
                "PYTHONPATH": _REPO_ROOT, "PYTHONDONTWRITEBYTECODE": "1"})
    req = {"contract_version": "1", "request_id": "r", "trace_id": "t",
           "input": {"runner_key": "pytest", "test_paths": ["tests/test_ok.py"], "timeout_ms": 30000}}
    proc = subprocess.run([sys.executable, "-m", "skills.test_runner.run"], input=json.dumps(req),
                          capture_output=True, text=True, cwd=_REPO_ROOT, env=env)
    env_out = json.loads(proc.stdout)
    o = env_out["output"]
    assert env_out["contract_version"] == "1" and env_out["name"] == "test-runner"
    assert env_out["status"] == "OK"  # business verdict, not a runner ERROR
    assert o["profiles_version"] == "1.0.0" and o["runner_key"] == "pytest"
    assert o["executor"] == "subprocess" and o["isolation"] == "process" and o["network_policy"] == "allowed"
    assert o["verdict"] in ("PASS", "FAIL")
    _output_validator().validate(o)


# --------------------------------------------------------------------------- #
# Round-2 hardening: resource bounds, bounded output, reparse chain, cleanup
# paths, deadline-during-copy
# --------------------------------------------------------------------------- #
def _good_tc(ws, art):
    return {"workspace": ws, "executor": "process", "trusted_dev": True,
            "network_policy": "allowed", "env_allowlist": set(),
            "image": None, "transport": "native", "wsl_distro": "Ubuntu-22.04",
            "artifact_root": art, "cpus": "1.0", "memory": "512m", "uid": 1000, "gid": 1000,
            "pids_limit": 64, "max_timeout_ms": 60000, "max_output_bytes": 262144}


def test_24_resource_zero_and_unbounded_rejected(tmp_path):
    ws = tmp_path / "ws"; ws.mkdir(); art = tmp_path / "art"; art.mkdir()
    prof = {"runner_key": "pytest", "executable": "python", "module": "pytest",
            "fixed_args": [], "image_repository": "mergepilot/test-runner-py",
            "tool_version": "pytest==8.4.2", "network_required": False}
    for bad in ({"cpus": "0"}, {"cpus": "999"}, {"memory": "0"}, {"memory": "999g"},
                {"uid": 0}, {"uid": -1}, {"gid": 0}, {"pids_limit": 0}):
        tc = _good_tc(str(ws), str(art)); tc.update(bad)
        with pytest.raises(core.TestRunnerError) as ei:
            core._validate_trusted(tc, prof, "process")
        assert ei.value.subcode == core.TRUSTED_CONFIG_MISSING


def test_25_docker_argv_non_root_and_no_user_zero():
    argv = _common.build_docker_run_argv(
        "native", "Ubuntu-22.04", "repo@sha256:" + "a" * 64, "/w",
        ["python"], "rid", "mp-tr-rid", "512m", "1.0", 64, 1000, 1000, [])
    assert "--user=1000:1000" in argv and "--user=0" not in argv
    assert "--rm" not in argv  # no --rm; cleanup is explicit
    # /artifacts uses tmpfs, not host bind or volume
    assert "--tmpfs" in argv
    tmpfs_args = [argv[i+1] for i in range(len(argv)) if argv[i] == "--tmpfs"]
    assert any("/artifacts" in t and "size=8388608" in t for t in tmpfs_args)
    v_args = [argv[i+1] for i in range(len(argv)) if argv[i] == "-v"]
    assert not any("/artifacts" in v for v in v_args)


def test_26_output_budget_lowered_and_huge_stdout_bounded(tmp_path, monkeypatch):
    ws = str(tmp_path / "ws"); os.makedirs(ws)
    # a test that prints a lot
    with open(os.path.join(ws, "t.py"), "w", encoding="utf-8") as fh:
        fh.write("print('A' * 5000)\n")
    prof = str(tmp_path / "p.json"); _venv_profile(prof)
    os.makedirs(str(tmp_path / "art"), exist_ok=True)
    for k, v in _trusted_env(ws, str(tmp_path / "art")).items():
        monkeypatch.setenv(k, v)
    out = core.run({"runner_key": "pytest", "test_paths": ["t.py"], "timeout_ms": 30000,
                    "max_output_bytes": 4}, profiles_path=prof)
    per = out["resource_limits"]["max_output_bytes"]
    assert per <= 4  # lowered value respected
    assert len(out["stdout_tail"]) <= max(1, per // 2)  # tail bounded by per-stream
    assert out["truncated"] is True


def test_27_assert_no_reparse_chain_rejects_symlink(tmp_path):
    real = tmp_path / "real"; real.mkdir()
    link = tmp_path / "link"
    try:
        os.symlink(str(real), str(link))
    except (OSError, NotImplementedError):
        pytest.skip("symlink not supported")
    with pytest.raises(core.TestRunnerError) as ei:
        core._assert_no_reparse_chain(str(link))
    assert ei.value.subcode == core.PATH_ESCAPE


def test_28_cleanup_paths_fail_closed(monkeypatch):
    import skills.test_runner.executors.container_executor as ce

    class _R:
        def __init__(self, rc, out=b"", err=b""):
            self.returncode = rc; self.stdout = out; self.stderr = err

    # _cleanup_container now makes 4 docker calls: rm, ps, volume rm, volume ls
    # case 1: all succeed (rm "no such" ok, ps empty, vol rm "no such" ok, vol ls empty)
    c1 = iter([
        _R(1, b"", b"Error: No such container"),  # rm
        _R(0, b"", b""),                           # ps (no residue)
        _R(1, b"", b"Error: No such volume"),      # volume rm
        _R(0, b"", b""),                           # volume ls (no residue)
    ])
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: next(c1))
    ce._cleanup_container("native", "Ubuntu-22.04", "r1", "mp-tr-r1")  # no raise

    # case 2: rm other error -> raise
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _R(1, b"", b"permission denied"))
    with pytest.raises(RuntimeError):
        ce._cleanup_container("native", "Ubuntu-22.04", "r2", "mp-tr-r2")

    # case 3: container residue after rm -> raise
    calls = iter([_R(0, b"", b""), _R(0, b"deadbeef\n", b"")])
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: next(calls))
    with pytest.raises(RuntimeError):
        ce._cleanup_container("native", "Ubuntu-22.04", "r3", "mp-tr-r3")


def test_29_deadline_consumed_during_copy_is_timeout(tmp_path, monkeypatch):
    from skills.common.runtime import errors as _e
    ws = str(tmp_path / "ws"); os.makedirs(ws); _write_ws(ws)
    art = str(tmp_path / "art"); os.makedirs(art)
    prof = str(tmp_path / "p.json"); _venv_profile(prof)
    for k, v in _trusted_env(ws, art).items():
        monkeypatch.setenv(k, v)

    class ExpiredDeadline:
        def remaining_ms(self): return 999999
        def check(self): raise _e.SkillTimeout("copy deadline")
    out = core.run({"runner_key": "pytest", "test_paths": ["tests/test_ok.py"], "timeout_ms": 30000},
                   profiles_path=prof, deadline=ExpiredDeadline())
    assert out["verdict"] == "TIMEOUT" and out["_runtime_error"] == core.TIMEOUT_SUB
    # no empty artifact dir left
    assert [d for d in os.listdir(art) if os.path.isdir(os.path.join(art, d))] == []


# --------------------------------------------------------------------------- #
# Round-3 hardening: _Drain edge cases, run_id concurrency, artifact boundary,
# error-no-artifact, side_effects, POSIX/UNC path
# --------------------------------------------------------------------------- #
import io as _io
import hashlib as _hashlib


def test_30_drain_cap_zero_drains_fully():
    data = b"hello world"
    d = _common._Drain(_io.BytesIO(data), cap=0)
    d.run()
    assert d.total == len(data)
    assert d.tail == ""                       # cap=0 -> empty tail
    assert d.truncated is True                # total(11) > cap(0)
    assert d.digest == _hashlib.sha256(data).hexdigest()  # digest covers full stream


def test_31_drain_utf8_and_budget_enforced():
    data = ("é" * 5000).encode("utf-8")  # 2 bytes/char = 10000 bytes
    d = _common._Drain(_io.BytesIO(data), cap=100)
    d.run()
    assert d.total == len(data)
    assert len(d.tail.encode("utf-8", "replace")) <= 100  # re-encoded <= cap
    assert d.digest == _hashlib.sha256(data).hexdigest()
    assert d.truncated is True


def test_32_drain_invalid_utf8_bounded():
    d = _common._Drain(_io.BytesIO(b"\xff\xfe\x00" * 100), cap=10)
    d.run()
    assert len(d.tail.encode("utf-8", "replace")) <= 10


def test_33_run_id_real_concurrency():
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=16) as pool:
        ids = list(pool.map(lambda _: core._new_run_id(), range(200)))
    assert len(set(ids)) == 200  # no collisions under real parallelism


def test_34_artifact_boundary_exact_8mib_ok(tmp_path):
    ad = tmp_path / "art"; ad.mkdir()
    # exactly MAX_ARTIFACT_BYTES should NOT raise
    remaining = core.MAX_ARTIFACT_BYTES
    f = ad / "big.bin"
    with open(f, "wb") as fh:
        fh.write(b"\0" * remaining)
    arts = core._collect_artifacts(str(ad))
    assert len(arts) == 1


def test_35_artifact_boundary_over_8mib_error(tmp_path):
    ad = tmp_path / "art"; ad.mkdir()
    with open(ad / "over.bin", "wb") as fh:
        fh.write(b"\0" * (core.MAX_ARTIFACT_BYTES + 1))
    with pytest.raises(core.TestRunnerError) as ei:
        core._collect_artifacts(str(ad))
    assert ei.value.subcode == core.INTERNAL


def test_36_error_verdict_no_artifacts(tmp_path, monkeypatch):
    # container executor mock: started=False -> verdict ERROR -> no artifacts extracted
    ws = str(tmp_path / "ws"); os.makedirs(ws); _write_ws(ws)
    art = str(tmp_path / "art"); os.makedirs(art)
    prof = str(tmp_path / "p.json"); _venv_profile(prof)
    for k, v in _trusted_env(ws, art, **{"MERGEPILOT_TR_EXECUTOR": "container",
            "MERGEPILOT_TR_IMAGE": "mergepilot/test-runner-py@sha256:" + "a"*64,
            "MERGEPILOT_TR_NETWORK_POLICY": "denied"}).items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr("skills.test_runner.executors._common.run_captured",
        lambda *a, **k: {"started": False, "exit_code": None, "timed_out": False,
                         "duration_ms": 0, "stdout_text": "", "stderr_text": "",
                         "stdout_digest": "d"*64, "stderr_digest": "d"*64,
                         "truncated": False, "cleanup_ok": True,
                         "executor": "container", "isolation": "container", "artifacts": []})
    out = core.run({"runner_key": "pytest", "test_paths": ["tests/test_ok.py"]}, profiles_path=prof)
    assert out["verdict"] == "ERROR"
    assert out["artifacts"] == []  # ERROR -> no untrusted artifact extraction
    # artifact dir cleaned up
    assert [d for d in os.listdir(art) if os.path.isdir(os.path.join(art, d))] == []


def test_37_cli_side_effects_declared(tmp_path):
    ws = tmp_path / "ws"; ws.mkdir(); (ws / "tests").mkdir()
    (ws / "tests" / "test_ok.py").write_text("def test_ok():\n    assert 2 == 2\n", encoding="utf-8")
    venv_scripts = os.path.dirname(sys.executable)
    env = dict(os.environ)
    env["PATH"] = venv_scripts + os.pathsep + env.get("PATH", "")
    os.makedirs(str(tmp_path / "art"), exist_ok=True)
    env.update({"MERGEPILOT_TR_WORKSPACE": str(ws), "MERGEPILOT_TR_EXECUTOR": "process",
                "MERGEPILOT_TR_TRUSTED_DEV": "true", "MERGEPILOT_TR_NETWORK_POLICY": "allowed",
                "MERGEPILOT_TR_ARTIFACT_ROOT": str(tmp_path / "art"),
                "PYTHONPATH": _REPO_ROOT, "PYTHONDONTWRITEBYTECODE": "1"})
    req = {"contract_version": "1", "request_id": "r", "trace_id": "t",
           "input": {"runner_key": "pytest", "test_paths": ["tests/test_ok.py"], "timeout_ms": 30000}}
    proc = subprocess.run([sys.executable, "-m", "skills.test_runner.run"], input=json.dumps(req),
                          capture_output=True, text=True, cwd=_REPO_ROOT, env=env)
    env_out = json.loads(proc.stdout)
    assert env_out["status"] in ("OK", "ERROR")
    se_types = {s["type"] for s in env_out.get("side_effects", [])}
    assert "fs_tmp" in se_types
    if env_out["status"] == "OK":
        assert "process_exec" in se_types


def test_38_posix_and_unc_path_rejected():
    # absolute POSIX, UNC, and backslash paths must be rejected as test_paths
    BS = chr(92)
    for bad in ["/etc/passwd", BS*2+"server"+BS+"share"+BS+"file", "C:"+BS+"Users"+BS+"x"]:
        with pytest.raises(core.TestRunnerError) as ei:
            core._safe_rel(bad)
        assert ei.value.subcode in (core.PATH_ESCAPE, core.INPUT_INVALID)


# --------------------------------------------------------------------------- #
# Round-4: budget=1 dual-stream, POSIX ancestor chain correctness
# --------------------------------------------------------------------------- #
def test_39_output_budget_1_byte_both_streams_empty(tmp_path, monkeypatch):
    ws = str(tmp_path / "ws"); os.makedirs(ws)
    with open(os.path.join(ws, "t.py"), "w", encoding="utf-8") as fh:
        fh.write("import sys\nsys.stdout.write('A'*100)\nsys.stderr.write('B'*100)\n")
    prof = str(tmp_path / "p.json"); _venv_profile(prof)
    os.makedirs(str(tmp_path / "art"), exist_ok=True)
    for k, v in _trusted_env(ws, str(tmp_path / "art")).items():
        monkeypatch.setenv(k, v)
    out = core.run({"runner_key": "pytest", "test_paths": ["t.py"], "timeout_ms": 30000,
                    "max_output_bytes": 1}, profiles_path=prof)
    # per_stream = 1 // 2 = 0 -> both tails empty, total <= 1
    assert len(out["stdout_tail"]) == 0
    assert len(out["stderr_tail"]) == 0
    assert out["truncated"] is True


def test_40_assert_no_reparse_chain_correct_abspath(tmp_path):
    # _assert_no_reparse_chain must check the symlink itself (not lose it via
    # manual string building). Uses os.path.dirname iteration -> correct on all OS.
    real = tmp_path / "real"; real.mkdir()
    link = tmp_path / "link"
    try:
        import os as _os; _os.symlink(str(real), str(link))
    except (OSError, NotImplementedError):
        pytest.skip("symlink not supported")
    with pytest.raises(core.TestRunnerError) as ei:
        core._assert_no_reparse_chain(str(link))
    assert ei.value.subcode == core.PATH_ESCAPE
    # normal dir passes
    core._assert_no_reparse_chain(str(real))


# --------------------------------------------------------------------------- #
# Round-5: capture-layer cleanup_ok=False must NOT be recovered;
# container cleanup always runs regardless of capture state
# --------------------------------------------------------------------------- #
def test_41_capture_failure_cleanup_runs(monkeypatch):
    """capture_ok=False → container cleanup IS called;
    final cleanup_ok=False (never recovers)."""
    import skills.test_runner.executors.container_executor as ce
    cleanup_called = []

    def fake_run_captured(*a, **k):
        return {"started": True, "exit_code": 0, "timed_out": False, "duration_ms": 1,
                "stdout_text": "1 passed", "stderr_text": "", "stdout_digest": "d" * 64,
                "stderr_digest": "d" * 64, "truncated": False, "cleanup_ok": False,
                "executor": "container", "isolation": "container", "artifacts": []}

    def fake_cleanup(*a, **k): cleanup_called.append(True)

    monkeypatch.setattr("skills.test_runner.executors._common.run_captured", fake_run_captured)
    monkeypatch.setattr(ce, "_cleanup_container", fake_cleanup)
    plan = {"transport": "native", "wsl_distro": "Ubuntu-22.04", "cwd": "/tmp", "artifact_dir": "/tmp",
            "run_id": "r1", "image": "x@sha256:" + "a" * 64, "argv": ["python"], "env": {},
            "timeout_ms": 5000, "max_output_bytes": 4096, "memory": "512m", "cpus": "1.0",
            "pids_limit": 64, "host_uid": 1000, "host_gid": 1000}
    res = ce.run(plan)
    assert len(cleanup_called) == 1     # container cleanup DID run
    assert res["cleanup_ok"] is False   # never recovered



def test_43_all_succeed_cleanup_ok_true(monkeypatch):
    """All phases succeed → cleanup_ok=True (no regression)."""
    import skills.test_runner.executors.container_executor as ce

    def fake_run_captured(*a, **k):
        return {"started": True, "exit_code": 0, "timed_out": False, "duration_ms": 1,
                "stdout_text": "", "stderr_text": "", "stdout_digest": "d" * 64,
                "stderr_digest": "d" * 64, "truncated": False, "cleanup_ok": True,
                "executor": "container", "isolation": "container", "artifacts": []}

    monkeypatch.setattr("skills.test_runner.executors._common.run_captured", fake_run_captured)
    monkeypatch.setattr(ce, "_cleanup_container", lambda *a, **k: None)
    plan = {"transport": "native", "wsl_distro": "Ubuntu-22.04", "cwd": "/tmp", "artifact_dir": "/tmp",
            "run_id": "r3", "image": "x@sha256:" + "a" * 64, "argv": ["python"], "env": {},
            "timeout_ms": 5000, "max_output_bytes": 4096, "memory": "512m", "cpus": "1.0",
            "pids_limit": 64, "host_uid": 1000, "host_gid": 1000}
    res = ce.run(plan)
    assert res["cleanup_ok"] is True


def test_44_capture_ok_false_persists_after_cleanup(monkeypatch):
    """Even if cleanup succeeds, capture_ok=False means final cleanup_ok=False."""
    import skills.test_runner.executors.container_executor as ce

    def fake_run_captured(*a, **k):
        return {"started": True, "exit_code": 0, "timed_out": False, "duration_ms": 1,
                "stdout_text": "", "stderr_text": "", "stdout_digest": "d" * 64,
                "stderr_digest": "d" * 64, "truncated": False, "cleanup_ok": False,
                "executor": "container", "isolation": "container", "artifacts": []}

    monkeypatch.setattr("skills.test_runner.executors._common.run_captured", fake_run_captured)
    monkeypatch.setattr(ce, "_cleanup_container", lambda *a, **k: None)
    plan = {"transport": "native", "wsl_distro": "Ubuntu-22.04", "cwd": "/tmp", "artifact_dir": "/tmp",
            "run_id": "r4", "image": "x@sha256:" + "a" * 64, "argv": ["python"], "env": {},
            "timeout_ms": 5000, "max_output_bytes": 4096, "memory": "512m", "cpus": "1.0",
            "pids_limit": 64, "host_uid": 1000, "host_gid": 1000}
    res = ce.run(plan)
    assert res["cleanup_ok"] is False  # capture_ok=False → never recovers


# --------------------------------------------------------------------------- #
# Round-6: Phase 1 exception still cleans up container; missing cleanup_ok
# defaults False
# --------------------------------------------------------------------------- #
def test_45_phase1_exception_still_cleans_up(monkeypatch):
    """run_captured raises RuntimeError after container starts -> container
    cleanup MUST still execute; final cleanup_ok=False."""
    import skills.test_runner.executors.container_executor as ce
    cleanup_calls = []

    def boom(*a, **k):
        raise RuntimeError("capture layer exploded")

    def track_cleanup(*a, **k):
        cleanup_calls.append(True)

    monkeypatch.setattr("skills.test_runner.executors._common.run_captured", boom)
    monkeypatch.setattr(ce, "_cleanup_container", track_cleanup)
    plan = {"transport": "native", "wsl_distro": "Ubuntu-22.04", "cwd": "/tmp",
            "artifact_dir": "/tmp", "run_id": "r5",
            "image": "x@sha256:" + "a" * 64, "argv": ["python"], "env": {},
            "timeout_ms": 5000, "max_output_bytes": 4096,
            "memory": "512m", "cpus": "1.0", "pids_limit": 64, "host_uid": 1000, "host_gid": 1000}
    res = ce.run(plan)
    assert len(cleanup_calls) == 1           # cleanup ran despite exception
    assert res["cleanup_ok"] is False        # exception -> fail-closed


def test_46_missing_cleanup_ok_defaults_false(monkeypatch):
    """Result dict without cleanup_ok key -> defaults False (strict)."""
    import skills.test_runner.executors.container_executor as ce

    def fake_run_captured(*a, **k):
        # intentionally omit cleanup_ok from result
        return {"started": True, "exit_code": 0, "timed_out": False, "duration_ms": 1,
                "stdout_text": "", "stderr_text": "", "stdout_digest": "d" * 64,
                "stderr_digest": "d" * 64, "truncated": False,
                "executor": "container", "isolation": "container", "artifacts": []}

    monkeypatch.setattr("skills.test_runner.executors._common.run_captured", fake_run_captured)
    monkeypatch.setattr(ce, "_cleanup_container", lambda *a, **k: None)
    plan = {"transport": "native", "wsl_distro": "Ubuntu-22.04", "cwd": "/tmp",
            "artifact_dir": "/tmp", "run_id": "r6",
            "image": "x@sha256:" + "a" * 64, "argv": ["python"], "env": {},
            "timeout_ms": 5000, "max_output_bytes": 4096,
            "memory": "512m", "cpus": "1.0", "pids_limit": 64, "host_uid": 1000, "host_gid": 1000}
    res = ce.run(plan)
    assert res["cleanup_ok"] is False       # strict default



# --------------------------------------------------------------------------- #
# Round-7: Phase 1 post-start exception -> INTERNAL_ERROR (not DEP_UNAVAILABLE);
# process_exec declared; cleanup runs; no raw exception in envelope
# --------------------------------------------------------------------------- #
def test_48_phase1_post_start_exception_via_core(monkeypatch, tmp_path):
    """Simulate: Popen succeeds, then a thread RuntimeError occurs inside
    run_captured (which propagates as an uncaught exception). The container
    executor's outer try/finally catches it. Core must classify as
    INTERNAL_ERROR (not CONTAINER_UNAVAILABLE), process_exec must be declared,
    and the raw exception message must NOT appear in the envelope."""
    ws = str(tmp_path / "ws"); os.makedirs(ws); _write_ws(ws)
    art = str(tmp_path / "art"); os.makedirs(art)
    prof = str(tmp_path / "p.json"); _venv_profile(prof)
    for k, v in _trusted_env(ws, art, **{"MERGEPILOT_TR_EXECUTOR": "container",
            "MERGEPILOT_TR_IMAGE": "mergepilot/test-runner-py@sha256:" + "a" * 64,
            "MERGEPILOT_TR_NETWORK_POLICY": "denied"}).items():
        monkeypatch.setenv(k, v)

    def boom_after_start(*a, **k):
        # Simulate: Popen started, then an unexpected RuntimeError
        raise RuntimeError("thread exploded after Popen")

    cleanup_calls = []
    import skills.test_runner.executors.container_executor as ce
    monkeypatch.setattr("skills.test_runner.executors._common.run_captured", boom_after_start)
    monkeypatch.setattr(ce, "_cleanup_container", lambda *a, **k: cleanup_calls.append(1))

    out = core.run({"runner_key": "pytest", "test_paths": ["tests/test_ok.py"]}, profiles_path=prof)
    # Must be INTERNAL_ERROR, NOT CONTAINER_UNAVAILABLE
    assert out["verdict"] == "ERROR"
    assert out["_runtime_error"] == core.INTERNAL  # not CONTAINER_UNAVAILABLE
    assert out["_executed"] is True  # process_exec should be declared
    # cleanup ran
    assert len(cleanup_calls) == 1


def test_49_output_schema_enforces_executor_artifact_contract():
    """Container artifacts must be empty; trusted-dev subprocess may return one."""
    output = core._timeout_output(
        "1.0.0", "pytest", "container",
        {"memory": "512m", "cpus": "1.0", "pids_limit": 64},
        1000, 4096, "denied",
    )
    output.pop("_runtime_error", None)
    output.pop("_executed", None)
    validator = _output_validator()
    validator.validate(output)

    output["artifacts"] = [{
        "name": "result.txt",
        "rel_path": "result.txt",
        "size": 5,
        "digest": "a" * 64,
    }]
    errors = list(validator.iter_errors(output))
    assert len(errors) == 1
    assert list(errors[0].absolute_path) == ["artifacts"]

    output["executor"] = "subprocess"
    output["isolation"] = "process"
    validator.validate(output)
