"""Cross-component hardening tests without network or model downloads."""
from __future__ import annotations

import json
import math
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from skills.case_retrieval import core
from skills.case_retrieval.embedding.fastembed_provider import DeterministicFakeProvider
from skills.case_retrieval.embedding.fastembed_provider import FastEmbedProvider
from skills.common.runtime.cli import Deadline
from skills.common.runtime import errors


ENV = {
    "MERGEPILOT_CR_PG_DSN": "redacted",
    "MERGEPILOT_CR_REPO_SCOPE": "repo-alpha",
    "MERGEPILOT_CR_EMBEDDING_VERSION": "1.0.0",
}


def row(case_id, created_at, score=0.5, **overrides):
    value = {
        "id": case_id,
        "task_id": "t",
        "finding_id": "f-%s" % case_id,
        "category": "quality",
        "severity": "low",
        "issue": "issue",
        "fix": "fix",
        "repo_scope": "repo-alpha",
        "source_pr_url": "https://example.test/pull/%s" % case_id,
        "source_commit_sha": None,
        "source_version": "source-v1",
        "embedding_version": "1.0.0",
        "created_at": created_at,
        "score": score,
    }
    value.update(overrides)
    return value


class Adapter:
    def __init__(self, rows, fail_stats=False):
        self.rows = rows
        self.fail_stats = fail_stats
        self.closed = False

    def retrieve(self, **kwargs):
        return {"rows": list(self.rows), "total_found": len(self.rows)}

    def stats(self, *args, **kwargs):
        if self.fail_stats:
            raise core.CaseRetrievalError(core.DB_UNAVAILABLE, "secret")
        return {"knowledge_base_size": len(self.rows), "trusted_available": len(self.rows)}

    def close(self):
        self.closed = True


def run_with(adapter):
    return core.run(
        {"query": "test", "top_k": 20},
        adapter=adapter,
        embedding_provider=DeterministicFakeProvider(),
        trusted_env=ENV,
    )


def test_core_tie_breaker_is_score_desc_date_desc_case_asc():
    rows = [
        row(3, "2026-01-01T00:00:00+00:00", score=0.8),
        row(2, "2026-02-01T00:00:00+00:00", score=0.8),
        row(1, "2026-02-01T00:00:00+00:00", score=0.8),
        row(4, "2026-03-01T00:00:00+00:00", score=0.7),
    ]
    out = run_with(Adapter(rows))
    assert [item["case_id"] for item in out["results"]] == ["1", "2", "3", "4"]


def test_core_rejects_malformed_or_naive_created_at():
    for created_at in ("bad", "2026-01-01T00:00:00"):
        with pytest.raises(core.CaseRetrievalError) as raised:
            run_with(Adapter([row(1, created_at)]))
        assert raised.value.subcode == core.INTERNAL


def test_invalid_citation_is_untrusted_not_verifiable():
    out = run_with(Adapter([row(
        1,
        "2026-01-01T00:00:00Z",
        source_pr_url="https://user:password@example.test/pull/1",
        source_commit_sha="not-a-sha",
    )]))
    result = out["results"][0]
    assert result["untrusted"] is True
    assert result["citation"]["verifiable"] is False
    assert result["citation"]["source_url"] is None
    assert out["degraded"] == [
        {"type": "untrusted", "reason": core.DEGRADED_CITATION}
    ]


def test_valid_commit_citation_is_normalized():
    out = run_with(Adapter([row(
        1,
        "2026-01-01T00:00:00Z",
        source_pr_url=None,
        source_commit_sha="A" * 40,
    )]))
    citation = out["results"][0]["citation"]
    assert citation["source_type"] == "commit"
    assert citation["source_id"] == "a" * 40
    assert citation["verifiable"] is True


def test_stats_failure_cannot_become_zero_success():
    with pytest.raises(core.CaseRetrievalError) as raised:
        run_with(Adapter([row(1, "2026-01-01T00:00:00Z")], fail_stats=True))
    assert raised.value.subcode == core.DB_UNAVAILABLE


def test_injected_adapter_ownership_remains_with_caller():
    adapter = Adapter([row(1, "2026-01-01T00:00:00Z")])
    run_with(adapter)
    assert adapter.closed is False


def test_core_closes_adapter_it_creates(monkeypatch):
    from skills.case_retrieval.adapters import pg_vector

    owned = Adapter([row(1, "2026-01-01T00:00:00Z")])
    monkeypatch.setattr(pg_vector, "PgVectorAdapter", lambda config: owned)
    core.run(
        {"query": "x"},
        embedding_provider=DeterministicFakeProvider(),
        trusted_env=ENV,
    )
    assert owned.closed is True


def test_core_closes_owned_adapter_after_failure(monkeypatch):
    from skills.case_retrieval.adapters import pg_vector

    owned = Adapter([row(1, "2026-01-01T00:00:00Z")], fail_stats=True)
    monkeypatch.setattr(pg_vector, "PgVectorAdapter", lambda config: owned)
    with pytest.raises(core.CaseRetrievalError):
        core.run(
            {"query": "x"},
            embedding_provider=DeterministicFakeProvider(),
            trusted_env=ENV,
        )
    assert owned.closed is True


def test_cooperative_deadline_maps_slow_provider_to_timeout_after_call():
    class SlowProvider:
        def embed(self, text, deadline=None):
            time.sleep(0.02)
            return [0.0] * 384

    with pytest.raises(errors.SkillTimeout) as raised:
        core.run(
            {"query": "x"},
            adapter=Adapter([]),
            embedding_provider=SlowProvider(),
            trusted_env=ENV,
            deadline=Deadline(1),
        )
    assert "deadline exceeded" in str(raised.value)


def _run_tree_stub(monkeypatch, tmp_path, mode):
    """Run _stub_tree_worker in the given mode; return (provider, subcode|None,
    worker_pid, grandchild_pid). Verifies BOTH the worker and its grandchild are
    gone -- not merely provider._proc.poll()."""
    from skills.case_retrieval.embedding import fastembed_provider as fp

    monkeypatch.setattr(
        fp,
        "_WORKER_PATH",
        str(Path(__file__).parent / "fixtures" / "_stub_tree_worker.py"),
    )
    marker = tmp_path / ("tree_%s.txt" % mode)
    provider = fp.FastEmbedProvider("stub-model", "1.0.0")
    started = time.monotonic()
    subcode = None
    try:
        provider.embed("%s|%s" % (mode, marker), deadline=Deadline(2000))
    except core.CaseRetrievalError as exc:
        subcode = exc.subcode
    elapsed = time.monotonic() - started
    assert elapsed < 6, "cleanup took too long: %.1fs" % elapsed
    # the worker always writes the marker before sleeping/exiting
    worker_pid, grandchild_pid = (int(x) for x in marker.read_text().split())
    return provider, subcode, worker_pid, grandchild_pid


def test_fastembed_timeout_reaps_whole_tree_including_grandchild(monkeypatch, tmp_path):
    from skills.case_retrieval.embedding import fastembed_provider as fp

    _provider, subcode, worker_pid, grandchild_pid = _run_tree_stub(
        monkeypatch, tmp_path, "sleep"
    )
    assert subcode == core.TIMEOUT_SUB
    # worker still alive at snapshot time -> snapshot + ordered terminate reaps both
    assert not fp._pid_alive(worker_pid), "worker pid %d survived" % worker_pid
    assert not fp._pid_alive(grandchild_pid), "grandchild pid %d survived" % grandchild_pid


def test_fastembed_worker_error_reaps_reparented_grandchild(monkeypatch, tmp_path):
    from skills.case_retrieval.embedding import fastembed_provider as fp

    _provider, subcode, worker_pid, grandchild_pid = _run_tree_stub(
        monkeypatch, tmp_path, "error"
    )
    # worker emits error JSON and exits 0 -> grandchild is reparented; the Job
    # Object must still reap it. Stable subcode, no leak.
    assert subcode == core.MODEL_UNAVAILABLE
    assert not fp._pid_alive(worker_pid), "worker pid %d survived" % worker_pid
    assert not fp._pid_alive(grandchild_pid), "grandchild pid %d survived" % grandchild_pid


def test_fastembed_parent_early_exit_reaps_reparented_grandchild(monkeypatch, tmp_path):
    from skills.case_retrieval.embedding import fastembed_provider as fp

    _provider, subcode, worker_pid, grandchild_pid = _run_tree_stub(
        monkeypatch, tmp_path, "exit"
    )
    # worker emits ok JSON and exits 0 immediately -> success, but the grandchild
    # is reparented and must still be reaped by the Job Object.
    assert subcode is None
    assert not fp._pid_alive(worker_pid), "worker pid %d survived" % worker_pid
    assert not fp._pid_alive(grandchild_pid), "grandchild pid %d survived" % grandchild_pid


def test_fastembed_bounded_stdout_overrun_terminates_tree(monkeypatch):
    from skills.case_retrieval.embedding import fastembed_provider as fp

    monkeypatch.setattr(
        fp,
        "_WORKER_PATH",
        str(Path(__file__).parent / "fixtures" / "_stub_modes_worker.py"),
    )
    provider = fp.FastEmbedProvider("stub-model", "1.0.0")
    started = time.monotonic()
    with pytest.raises(core.CaseRetrievalError) as raised:
        provider.embed("stdout_overrun", deadline=Deadline(10000))
    elapsed = time.monotonic() - started
    assert raised.value.subcode == core.MODEL_UNAVAILABLE
    assert elapsed < 6  # no pipe-full deadlock
    assert provider._proc is not None and provider._proc.poll() is not None


def test_fastembed_stderr_flood_to_devnull_does_not_deadlock(monkeypatch):
    from skills.case_retrieval.embedding import fastembed_provider as fp

    monkeypatch.setattr(
        fp,
        "_WORKER_PATH",
        str(Path(__file__).parent / "fixtures" / "_stub_modes_worker.py"),
    )
    provider = fp.FastEmbedProvider("stub-model", "1.0.0")
    vec = provider.embed("stderr_flood", deadline=Deadline(10000))
    # stderr is DEVNULL -> discarded; the valid ok response still parses.
    assert isinstance(vec, list) and len(vec) == 384
    assert provider._proc is not None and provider._proc.poll() is not None


def test_fastembed_nonzero_exit_with_valid_json_is_model_unavailable(monkeypatch):
    from skills.case_retrieval.embedding import fastembed_provider as fp

    monkeypatch.setattr(
        fp,
        "_WORKER_PATH",
        str(Path(__file__).parent / "fixtures" / "_stub_modes_worker.py"),
    )
    provider = fp.FastEmbedProvider("stub-model", "1.0.0")
    with pytest.raises(core.CaseRetrievalError) as raised:
        provider.embed("badexit", deadline=Deadline(10000))
    assert raised.value.subcode == core.MODEL_UNAVAILABLE
    assert provider._proc is not None and provider._proc.poll() is not None


def test_nonfinite_score_is_rejected():
    with pytest.raises(core.CaseRetrievalError) as raised:
        run_with(Adapter([row(1, "2026-01-01T00:00:00Z", score=float("nan"))]))
    assert raised.value.subcode == core.INTERNAL


# Decoy credentials used only in-process; never written to evidence. Values are
# non-matching fakes so they do not trip the delivery credential scanner.
_DECOYS = {
    "MERGEPILOT_CR_PG_DSN": "decoy_dsn_value",
    "MERGEPILOT_CR_REPO_SCOPE": "decoy_scope_value",
    "DECOY_API_TOKEN": "decoy_token_value",
    "DECOY_DB_PASSWORD": "decoy_password_value",
    "DECOY_USER_PASSWD": "decoy_passwd_value",
    "DECOY_CLIENT_SECRET": "decoy_secret_value",
    "DECOY_SIGNING_KEY": "decoy_key_value",
    "DECOY_AUTH_BLOB": "decoy_auth_value",
    "DECOY_SESSION_COOKIE": "decoy_cookie_value",
    "DECOY_AWS_CREDENTIAL": "decoy_credential_value",
}


def test_core_tie_break_string_order_puts_10_before_2():
    rows = [
        row(2, "2026-01-01T00:00:00Z", score=0.5),
        row(10, "2026-01-01T00:00:00Z", score=0.5),
    ]
    out = run_with(Adapter(rows))
    assert [item["case_id"] for item in out["results"]] == ["10", "2"]


def test_core_top_k_matches_db_window_when_tied_beyond_fetch_k():
    top_k = 5
    fetch_k = min(top_k * 3, 60)
    ids = list(range(1, fetch_k + 6))  # more rows than fetch_k, all fully tied
    rows = [row(i, "2026-01-01T00:00:00Z", score=0.5) for i in ids]
    # The DB returns its LIMIT window pre-sorted by the frozen key. With every
    # row tied on score and date, that order is case_id string order.
    window = sorted(rows, key=lambda value: str(value["id"]))[:fetch_k]
    out = core.run(
        {"query": "test", "top_k": top_k},
        adapter=Adapter(window),
        embedding_provider=DeterministicFakeProvider(),
        trusted_env=ENV,
    )
    expected = [str(i) for i in sorted(ids, key=str)[:top_k]]
    assert [item["case_id"] for item in out["results"]] == expected


def test_fallback_trusted_available_counts_verifiable_citations():
    class NoStatsAdapter:
        def __init__(self, rows):
            self.rows = rows
            self.closed = False

        def retrieve(self, **kwargs):
            return {"rows": list(self.rows), "total_found": len(self.rows)}

        def close(self):
            self.closed = True

    rows = [
        row(1, "2026-01-01T00:00:00Z"),
        row(2, "2026-01-01T00:00:00Z", source_pr_url=None, source_commit_sha="a" * 40),
        row(3, "2026-01-01T00:00:00Z", source_pr_url=None, source_commit_sha=None),
    ]
    out = core.run(
        {"query": "test", "top_k": 20},
        adapter=NoStatsAdapter(rows),
        embedding_provider=DeterministicFakeProvider(),
        trusted_env=ENV,
    )
    # untrusted is always True now; the fallback must count citation.verifiable.
    assert out["stats"]["trusted_available"] == 2


def test_minimal_env_excludes_all_decoy_credentials(monkeypatch):
    from skills.case_retrieval.embedding import fastembed_provider as fp

    for key, value in _DECOYS.items():
        monkeypatch.setenv(key, value)
    env = fp._minimal_env()
    for key in _DECOYS:
        assert key not in env
    for key in env:
        assert not fp._is_sensitive_name(key)
    # A child launched with this environment cannot see any decoy.
    probe = subprocess.run(
        [sys.executable, "-c", "import json,os; print(json.dumps(sorted(os.environ)))"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert probe.returncode == 0
    child_keys = set(json.loads(probe.stdout))
    for key in _DECOYS:
        assert key not in child_keys


def test_minimal_env_is_race_free_under_concurrency(monkeypatch):
    from skills.case_retrieval.embedding import fastembed_provider as fp

    for key, value in _DECOYS.items():
        monkeypatch.setenv(key, value)
    results = [None] * 12

    def worker(index):
        results[index] = fp._minimal_env()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(len(results))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    first = results[0]
    assert first is not None
    for env in results:
        assert env == first
        for key in _DECOYS:
            assert key not in env


def test_fastembed_subprocess_protocol_success(monkeypatch):
    from skills.case_retrieval.embedding import fastembed_provider as fp

    monkeypatch.setattr(
        fp, "_WORKER_PATH", str(Path(__file__).parent / "fixtures" / "_stub_ok_worker.py")
    )
    provider = fp.FastEmbedProvider("stub-model", "1.0.0")
    vector = provider.embed("hello world", deadline=Deadline(10000))
    assert isinstance(vector, list) and len(vector) == 384
    assert all(isinstance(v, float) and math.isfinite(v) for v in vector)
    assert provider._proc is not None and provider._proc.poll() is not None


def test_fastembed_worker_error_maps_to_stable_subcode_without_leak(monkeypatch):
    from skills.case_retrieval.embedding import fastembed_provider as fp

    monkeypatch.setattr(
        fp, "_WORKER_PATH", str(Path(__file__).parent / "fixtures" / "_stub_error_worker.py")
    )
    provider = fp.FastEmbedProvider("stub-model", "1.0.0")
    with pytest.raises(core.CaseRetrievalError) as raised:
        provider.embed("hello", deadline=Deadline(10000))
    assert raised.value.subcode == core.MODEL_UNAVAILABLE
    leaked = "/internal/worker/blob/value"
    assert leaked not in str(raised.value)
    assert leaked not in str(raised.value.detail or "")
