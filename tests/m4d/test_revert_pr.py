"""Fail-closed revert derivation and restoration tests."""
from __future__ import annotations

import pytest

from skills.pr_lifecycle import core

from .conftest import BAD_SHA, PARENT_SHA, FakeAdapter, revert_input, trusted_env


def _revert_fixture(*, status="modified", binary=False):
    adapter = FakeAdapter(base_sha=BAD_SHA)
    adapter.branch_history["main"] = [BAD_SHA]
    adapter.commits[BAD_SHA] = {
        "sha": BAD_SHA,
        "files": [{"path": "src/app.py", "status": status, "binary": binary}],
    }
    adapter.commit_sequences[BAD_SHA] = [BAD_SHA, PARENT_SHA]
    adapter.sha_files[PARENT_SHA] = {"src/app.py": "verified parent\n"}
    return adapter


def test_25_revert_modified_file_creates_draft_pr():
    adapter = _revert_fixture(status="modified")
    out = core.run(
        revert_input(), adapter=adapter,
        trusted_env=trusted_env(action="ensure_revert_pr"),
    )
    assert out["outcome"] == "CREATED"
    assert out["action"] == "ensure_revert_pr"
    assert out["draft"] is True
    assert adapter.branch_files[out["head_branch"]]["src/app.py"] == "verified parent\n"


def test_26_revert_removed_file_recreates_parent_content():
    adapter = _revert_fixture(status="removed")
    out = core.run(
        revert_input(), adapter=adapter,
        trusted_env=trusted_env(action="ensure_revert_pr"),
    )
    assert out["outcome"] == "CREATED"
    assert out["changed_paths"] == ["src/app.py"]


def test_27_revert_added_file_is_rejected_before_any_write():
    adapter = _revert_fixture(status="added")
    with pytest.raises(core.PRLifecycleError) as exc:
        core.run(revert_input(), adapter=adapter,
                 trusted_env=trusted_env(action="ensure_revert_pr"))
    assert exc.value.subcode == core.REVERT_DELETE_UNSUPPORTED
    assert not any(x in adapter.calls for x in ("create_branch", "push_files",
                                                 "create_pull_request"))


def test_28_revert_rename_is_rejected_fail_closed():
    adapter = _revert_fixture(status="renamed")
    with pytest.raises(core.PRLifecycleError) as exc:
        core.run(revert_input(), adapter=adapter,
                 trusted_env=trusted_env(action="ensure_revert_pr"))
    assert exc.value.subcode == core.REVERT_STATE_MISMATCH
    assert "create_branch" not in adapter.calls


def test_29_revert_binary_file_is_rejected_fail_closed():
    adapter = _revert_fixture(status="modified", binary=True)
    with pytest.raises(core.PRLifecycleError) as exc:
        core.run(revert_input(), adapter=adapter,
                 trusted_env=trusted_env(action="ensure_revert_pr"))
    assert exc.value.subcode == core.REVERT_STATE_MISMATCH
    assert "push_files" not in adapter.calls


def test_30_revert_requires_base_tip_to_equal_bad_merge():
    adapter = _revert_fixture()
    adapter.branches["main"] = "f" * 40
    with pytest.raises(core.PRLifecycleError) as exc:
        core.run(revert_input(), adapter=adapter,
                 trusted_env=trusted_env(action="ensure_revert_pr"))
    assert exc.value.subcode == core.REVERT_STATE_MISMATCH


def test_31_revert_requires_authoritative_parent_sha():
    adapter = _revert_fixture()
    adapter.commit_sequences[BAD_SHA] = [BAD_SHA, "4" * 40]
    with pytest.raises(core.PRLifecycleError) as exc:
        core.run(revert_input(), adapter=adapter,
                 trusted_env=trusted_env(action="ensure_revert_pr"))
    assert exc.value.subcode == core.REVERT_STATE_MISMATCH


def test_32_revert_missing_parent_content_is_not_guessed():
    adapter = _revert_fixture()
    adapter.sha_files.clear()
    with pytest.raises(core.PRLifecycleError) as exc:
        core.run(revert_input(), adapter=adapter,
                 trusted_env=trusted_env(action="ensure_revert_pr"))
    assert exc.value.subcode == core.REVERT_STATE_MISMATCH
