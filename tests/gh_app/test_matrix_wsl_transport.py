# -*- coding: utf-8 -*-
"""7.3I2-C transport tests: WSL vantage, stdin-only auth, fail-closed."""
import json, sys, types
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "cli"))
import matrix_wsl_transport as mt  # noqa: E402

CFG = {"matrix_homeserver": "http://172.25.0.2:6167"}
NETLOC = "172.25.0.2:6167"


def test_default_windows_path_returns_none_when_host_reachable():
    assert mt.ensure_matrix_transport(CFG, host_probe_fn=lambda: None) is None


def test_fallback_returns_wsl_transport_and_reuses_singleton():
    calls = []
    def failing_probe():
        calls.append(1); raise OSError("refused")
    class GoodExec:
        def __call__(self, argv, input_bytes=None, timeout=10):
            class CP: returncode = 0; stdout = '{"status":200,"body":"{}"}'; stderr = b''
            return CP()
    t1 = mt.ensure_matrix_transport(CFG, host_probe_fn=failing_probe,
                                    smoke_exec_fn=GoodExec())
    t2 = mt.ensure_matrix_transport(CFG, host_probe_fn=failing_probe,
                                    smoke_exec_fn=GoodExec())
    assert t1 is not None and t1 is t2            # 同一 transport/vantage 复用


def test_allowlist_rejects_before_spawn():
    try:
        mt.wsl_matrix_request("GET", "http://evil.example:1234/_matrix/client/versions",
                              allow_netloc=NETLOC, exec_fn=lambda a, **k: (_ for _ in ()).throw(AssertionError("must not spawn")))
    except mt.MatrixTransportUnavailable as e:
        assert "TARGET_REJECTED" in str(e)
    else:
        pytest.fail("disallowed target must be rejected")


def test_token_travels_stdin_only(monkeypatch):
    captured = {}
    class CP: returncode = 0; stdout = '{"status":200,"body":"{}"}'; stderr = ''
    def fake_run(argv, **kw):
        captured["argv"] = argv; captured["stdin"] = kw.get("input", b"")
        return CP()
    monkeypatch.setattr(mt.subprocess, "run", fake_run)
    mt.wsl_matrix_request("GET", "http://172.25.0.2:6167/_matrix/client/v3/joined_rooms",
                          headers={"Authorization": "Bearer SECRET-TOKEN"},
                          allow_netloc=NETLOC, exec_fn=None, timeout=5) if False else None
    # exec_fn 注入路径：
    sent = {}
    def runner_exec(argv, input_bytes=None, timeout=10):
        sent["argv"] = argv; sent["stdin"] = input_bytes
        class CP2: returncode = 0; stdout = '{"status":200,"body":"{}"}'; stderr = b''
        return CP2()
    st, body = mt.wsl_matrix_request(
        "GET", "http://172.25.0.2:6167/_matrix/client/v3/joined_rooms",
        headers={"Authorization": "Bearer SECRET-TOKEN"},
        allow_netloc=NETLOC, exec_fn=runner_exec)
    assert st == 200
    argv_text = " ".join(sent["argv"])
    assert "SECRET-TOKEN" not in argv_text          # token 不进 argv
    assert b"SECRET-TOKEN" in sent["stdin"]         # token 仅经 stdin


def test_timeout_fail_closed():
    def slow_exec(argv, input_bytes=None, timeout=10):
        raise mt.subprocess.TimeoutExpired(cmd="wsl", timeout=timeout)
    try:
        mt.wsl_matrix_request("GET", "http://172.25.0.2:6167/_matrix/client/versions",
                              allow_netloc=NETLOC, exec_fn=slow_exec, timeout=1)
    except mt.MatrixTransportUnavailable as e:
        assert "RUNNER" in str(e)
    else:
        pytest.fail("timeout must fail closed")


def test_initial_and_provider_share_vantage():
    import matrix_wsl_transport as again
    assert mt.ensure_matrix_transport(CFG, host_probe_fn=lambda: (_ for _ in ()).throw(OSError())) \
        is mt.ensure_matrix_transport(CFG, host_probe_fn=lambda: (_ for _ in ()).throw(OSError()))
