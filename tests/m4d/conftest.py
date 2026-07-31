"""Shared M4-D fixtures and deterministic in-memory Policy Gateway model."""
from __future__ import annotations

import copy
import hashlib
import os
import sys

import pytest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_THIS_DIR))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from skills.pr_lifecycle import core  # noqa: E402


EXPECTED_PASS = 54
BASE_SHA = "1" * 40
BAD_SHA = "2" * 40
PARENT_SHA = "3" * 40


def _fake_auth():
    return "fixture-" + "a" * 40


def trusted_env(*, role="fixer", action="ensure_fix_pr", risk="L1"):
    env = {
        "MERGEPILOT_PRL_GATEWAY_URL": "http://policy-gw:8083",
        "MERGEPILOT_PRL_ROLE": role,
        "MERGEPILOT_PRL_TOKEN": _fake_auth(),
        "MERGEPILOT_PRL_REPO": "example/project",
        "MERGEPILOT_PRL_BASE_BRANCH": "main",
        "MERGEPILOT_PRL_RUN_ID": "run-123",
        "MERGEPILOT_PRL_RISK_LEVEL": risk,
        "MERGEPILOT_PRL_HMAC_KEY": "fixture-binding-" + "k" * 32,
    }
    if action == "ensure_fix_pr":
        env["MERGEPILOT_PRL_EXPECTED_BASE_SHA"] = BASE_SHA
    if action == "ensure_revert_pr":
        env["MERGEPILOT_PRL_REVERT_BAD_SHA"] = BAD_SHA
        env["MERGEPILOT_PRL_REVERT_PARENT_SHA"] = PARENT_SHA
    return env


def fix_input(**overrides):
    value = {
        "action": "ensure_fix_pr",
        "idempotency_key": "fix-stage-1",
        "changes": [{"path": "src/app.py", "content": "print('fixed')\n"}],
        "commit_message": "fix: app",
        "pr_title": "Fix app",
        "pr_body": "Structured fix.",
    }
    value.update(overrides)
    return value


def revert_input(**overrides):
    value = {
        "action": "ensure_revert_pr",
        "idempotency_key": "revert-stage-1",
        "commit_message": "revert: bad merge",
        "pr_title": "Revert bad merge",
        "pr_body": "Restore the verified parent state.",
    }
    value.update(overrides)
    return value


def merge_input(**overrides):
    value = {
        "action": "merge_pr",
        "idempotency_key": "merge-outbox-1",
        "pull_number": 1,
        "approval_ticket": "tkt-00000000-0000-4000-8000-000000000001",
        "merge_method": "squash",
        "commit_title": "Merge fix",
    }
    value.update(overrides)
    return value


def close_input(**overrides):
    value = {
        "action": "close_pr",
        "idempotency_key": "close-outbox-1",
        "pull_number": 1,
        "approval_ticket": "tkt-00000000-0000-4000-8000-000000000001",
    }
    value.update(overrides)
    return value


class FakeAdapter:
    def __init__(self, *, base_sha=BASE_SHA):
        self.branches = {"main": base_sha}
        self.branch_history = {"main": [base_sha]}
        self.branch_files = {"main": {}}
        self.commits = {base_sha: {"sha": base_sha, "files": []}}
        self.commit_sequences = {}
        self.sha_files = {}
        self.prs = []
        self.calls = []
        self.failures = {}
        self.next_pr = 1
        self._counter = 10

    def fail(self, method, kind, reason="", *, forwarded=False):
        self.failures[method] = core.GatewayFailure(
            kind, reason, forwarded=forwarded
        )

    def _before(self, method):
        self.calls.append(method)
        failure = self.failures.get(method)
        if failure is not None:
            raise failure

    def _new_sha(self, label):
        self._counter += 1
        return hashlib.sha1(("%s-%d" % (label, self._counter)).encode()).hexdigest()

    def list_branches(self, *, page, per_page, timeout_ms):
        self._before("list_branches")
        items = [{"name": name, "sha": sha} for name, sha in sorted(self.branches.items())]
        start = (page - 1) * per_page
        return items[start:start + per_page]

    def list_pull_requests(self, *, state, page, per_page, timeout_ms):
        self._before("list_pull_requests")
        items = [
            copy.deepcopy(pr) for pr in self.prs
            if state == "all" or pr["state"] == state
        ]
        start = (page - 1) * per_page
        return items[start:start + per_page]

    def read_pull_request(self, pull_number, *, timeout_ms):
        self._before("read_pull_request")
        for pr in self.prs:
            if pr["number"] == pull_number:
                return copy.deepcopy(pr)
        raise core.GatewayFailure("SCHEMA", "UPSTREAM_SCHEMA_INVALID")

    def list_pull_request_files(self, pull_number, *, page, per_page, timeout_ms):
        self._before("list_pull_request_files")
        pr = self.read_pull_request(pull_number, timeout_ms=timeout_ms)
        files = self.commits[pr["head_sha"]]["files"]
        items = [{"path": item["path"]} for item in files]
        start = (page - 1) * per_page
        return items[start:start + per_page]

    def get_file(self, path, *, ref=None, sha=None, timeout_ms):
        self._before("get_file")
        if sha is not None:
            content = self.sha_files.get(sha, {}).get(path)
        elif ref and ref.startswith("refs/heads/"):
            content = self.branch_files.get(ref[len("refs/heads/"):], {}).get(path)
        elif ref and ref.startswith("refs/pull/"):
            number = int(ref.split("/")[2])
            pr = next(item for item in self.prs if item["number"] == number)
            content = self.branch_files.get(pr["head_ref"], {}).get(path)
        else:
            content = None
        if content is None:
            return {"status": "MISSING", "content": None, "sha": None}
        return {
            "status": "OK",
            "content": content,
            "sha": hashlib.sha1(content.encode()).hexdigest(),
        }

    def get_commit(self, sha, *, timeout_ms):
        self._before("get_commit")
        if sha not in self.commits:
            raise core.GatewayFailure("SCHEMA", "UPSTREAM_SCHEMA_INVALID")
        return copy.deepcopy(self.commits[sha])

    def list_commits(self, ref, *, per_page, timeout_ms):
        self._before("list_commits")
        if ref in self.branch_history:
            values = self.branch_history[ref]
        else:
            values = self.commit_sequences.get(ref, [ref])
        return [{"sha": value} for value in values[:per_page]]

    def create_branch(self, branch, from_branch, *, timeout_ms):
        self._before("create_branch")
        if branch in self.branches:
            raise core.GatewayFailure("DENIED", "UPSTREAM_REJECTED")
        sha = self.branches[from_branch]
        self.branches[branch] = sha
        self.branch_history[branch] = [sha]
        self.branch_files[branch] = copy.deepcopy(self.branch_files.get(from_branch, {}))
        return {}

    def push_files(self, branch, files, message, *, timeout_ms):
        self._before("push_files")
        parent = self.branches[branch]
        new_sha = self._new_sha(branch)
        data = copy.deepcopy(self.branch_files.get(branch, {}))
        for item in files:
            data[item["path"]] = item["content"]
        self.branch_files[branch] = data
        self.branches[branch] = new_sha
        self.branch_history[branch] = [new_sha, parent]
        self.commits[new_sha] = {
            "sha": new_sha,
            "files": [{"path": item["path"], "status": "modified"} for item in files],
        }
        return {}

    def create_pull_request(self, head, base, title, body, draft, *, timeout_ms):
        self._before("create_pull_request")
        number = self.next_pr
        self.next_pr += 1
        self.prs.append({
            "number": number,
            "state": "open",
            "head_ref": head,
            "head_sha": self.branches[head],
            "head_repo_full_name": "example/project",
            "base_ref": base,
            "title": title,
            "body": body,
            "merged": False,
            "draft": bool(draft),
            "merge_commit_sha": None,
            "url": "https://github.test/example/project/pull/%d" % number,
        })
        return {}

    def merge_pull_request(self, pull_number, ticket, merge_method, commit_title,
                           commit_message, *, timeout_ms):
        self._before("merge_pull_request")
        pr = next(item for item in self.prs if item["number"] == pull_number)
        pr["merged"] = True
        pr["state"] = "closed"
        pr["merge_commit_sha"] = self._new_sha("merge")
        return {"sha": pr["merge_commit_sha"]}

    def close_pull_request(self, pull_number, ticket, *, timeout_ms):
        self._before("close_pull_request")
        pr = next(item for item in self.prs if item["number"] == pull_number)
        pr["state"] = "closed"
        return {}

    def seed_pr(self, *, state="open", merged=False, draft=False, base="main",
                head="fix/existing", title="Existing PR", body="body", head_sha=None):
        head_sha = head_sha or self._new_sha("head")
        self.branches[head] = head_sha
        self.branch_history[head] = [head_sha, self.branches[base]]
        self.branch_files.setdefault(head, {})
        self.commits.setdefault(head_sha, {"sha": head_sha, "files": []})
        number = self.next_pr
        self.next_pr += 1
        pr = {
            "number": number,
            "state": state,
            "head_ref": head,
            "head_sha": head_sha,
            "head_repo_full_name": "example/project",
            "base_ref": base,
            "title": title,
            "body": body,
            "merged": merged,
            "draft": draft,
            "merge_commit_sha": self._new_sha("merge") if merged else None,
            "url": "https://github.test/example/project/pull/%d" % number,
        }
        self.prs.append(pr)
        return pr


@pytest.fixture
def adapter():
    return FakeAdapter()
