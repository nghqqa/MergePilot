"""Framework-neutral PRLifecycle orchestration.

The core accepts only high-level lifecycle actions and an injected normalized
Gateway adapter. It never accepts an arbitrary tool name/argument set and never
accesses GitHub, a PAT, a shell, or local git directly.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
from dataclasses import dataclass
from urllib.parse import urlsplit


INVALID_INPUT = "PRL_INVALID_INPUT"
LIMIT_EXCEEDED = "PRL_LIMIT_EXCEEDED"
TRUSTED_CONFIG_MISSING = "PRL_TRUSTED_CONFIG_MISSING"
ROLE_ACTION_DENIED = "PRL_ROLE_ACTION_DENIED"
POLICY_DENIED = "PRL_POLICY_DENIED"
IDEMPOTENCY_CONFLICT = "PRL_IDEMPOTENCY_CONFLICT"
REVERT_DELETE_UNSUPPORTED = "PRL_REVERT_DELETE_UNSUPPORTED"
REVERT_STATE_MISMATCH = "PRL_REVERT_STATE_MISMATCH"
GATEWAY_UNAVAILABLE = "PRL_GATEWAY_UNAVAILABLE"
EFFECT_UNKNOWN = "PRL_EFFECT_UNKNOWN"
DEADLINE_EXCEEDED = "PRL_DEADLINE_EXCEEDED"
INTERNAL = "PRL_INTERNAL"
OUTPUT_SCHEMA_INVALID = "PRL_OUTPUT_SCHEMA_INVALID"

FIX_ACTIONS = {"ensure_fix_pr", "ensure_revert_pr"}
L2_ACTIONS = {"merge_pr", "close_pr"}
ALL_ACTIONS = FIX_ACTIONS | L2_ACTIONS

MAX_FILES = 32
MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 1024 * 1024
MAX_PAGES = 10
PAGE_SIZE = 100
SETTLE_ATTEMPTS = 15
SETTLE_SLEEP_SECONDS = 0.75
MARKER_PREFIX = "MergePilot-PRL-Marker:"
_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_RUN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")
_SAFE_REASON_RE = re.compile(r"^[A-Z0-9_]{1,80}$")


class GatewayFailure(Exception):
    """Normalized adapter failure.

    ``kind`` is one of DENIED / UNAVAILABLE / UNKNOWN / SCHEMA.
    ``forwarded`` means an upstream GitHub write might have been attempted.
    """

    def __init__(self, kind, reason_code="", *, forwarded=False):
        super().__init__(kind)
        self.kind = kind
        self.reason_code = reason_code if _SAFE_REASON_RE.match(reason_code or "") else ""
        self.forwarded = bool(forwarded)


class PRLifecycleError(Exception):
    def __init__(self, subcode, detail="", *, retryable=False, effects=None, output=None):
        super().__init__(subcode)
        self.subcode = subcode
        self.detail = detail
        self.retryable = bool(retryable)
        self.effects = list(effects or [])
        self.output = dict(output or {})


@dataclass(frozen=True)
class TrustedConfig:
    gateway_url: str
    role: str
    auth_bearer: str
    repo: str
    owner: str
    repo_name: str
    base_branch: str
    run_id: str
    risk_level: str
    expected_base_sha: str | None
    hmac_key: bytes
    revert_bad_sha: str | None
    revert_parent_sha: str | None


class EffectTracker:
    def __init__(self, repo):
        self.repo = repo
        self._types = []

    def add(self, effect_type):
        if effect_type not in self._types:
            self._types.append(effect_type)

    def items(self):
        out = []
        for effect_type in self._types:
            out.append({
                "type": effect_type,
                "target": self.repo,
                "via": "policy-gateway",
                "declared": True,
            })
        return out


def _required_env(env, name):
    value = env.get(name)
    if not isinstance(value, str) or not value:
        raise PRLifecycleError(TRUSTED_CONFIG_MISSING)
    return value


def _valid_gateway_url(value):
    try:
        parsed = urlsplit(value)
    except Exception:
        return False
    return (
        parsed.scheme in ("http", "https")
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and parsed.path in ("", "/")
    )


def load_trusted_config(action, env=None):
    env = os.environ if env is None else env
    gateway_url = _required_env(env, "MERGEPILOT_PRL_GATEWAY_URL").rstrip("/")
    role = _required_env(env, "MERGEPILOT_PRL_ROLE")
    auth_bearer = _required_env(env, "MERGEPILOT_PRL_TOKEN")
    repo = _required_env(env, "MERGEPILOT_PRL_REPO")
    base_branch = _required_env(env, "MERGEPILOT_PRL_BASE_BRANCH")
    run_id = _required_env(env, "MERGEPILOT_PRL_RUN_ID")
    risk_level = _required_env(env, "MERGEPILOT_PRL_RISK_LEVEL")
    hmac_value = _required_env(env, "MERGEPILOT_PRL_HMAC_KEY")

    if not _valid_gateway_url(gateway_url):
        raise PRLifecycleError(TRUSTED_CONFIG_MISSING)
    if role not in ("fixer", "coordinator"):
        raise PRLifecycleError(TRUSTED_CONFIG_MISSING)
    if action in FIX_ACTIONS and role != "fixer":
        raise PRLifecycleError(ROLE_ACTION_DENIED)
    if action in L2_ACTIONS and role != "coordinator":
        raise PRLifecycleError(ROLE_ACTION_DENIED)
    if not _REPO_RE.match(repo):
        raise PRLifecycleError(TRUSTED_CONFIG_MISSING)
    if not _BRANCH_RE.match(base_branch) or base_branch.startswith("fix/"):
        raise PRLifecycleError(TRUSTED_CONFIG_MISSING)
    if not _RUN_RE.match(run_id):
        raise PRLifecycleError(TRUSTED_CONFIG_MISSING)
    if risk_level not in ("L0", "L1", "L2"):
        raise PRLifecycleError(TRUSTED_CONFIG_MISSING)
    if len(hmac_value.encode("utf-8")) < 32:
        raise PRLifecycleError(TRUSTED_CONFIG_MISSING)

    expected_base_sha = env.get("MERGEPILOT_PRL_EXPECTED_BASE_SHA") or None
    if action == "ensure_fix_pr" and not _is_sha40(expected_base_sha):
        raise PRLifecycleError(TRUSTED_CONFIG_MISSING)
    if expected_base_sha is not None and not _is_sha40(expected_base_sha):
        raise PRLifecycleError(TRUSTED_CONFIG_MISSING)

    bad_sha = env.get("MERGEPILOT_PRL_REVERT_BAD_SHA") or None
    parent_sha = env.get("MERGEPILOT_PRL_REVERT_PARENT_SHA") or None
    if action == "ensure_revert_pr":
        if not _is_sha40(bad_sha) or not _is_sha40(parent_sha) or bad_sha == parent_sha:
            raise PRLifecycleError(TRUSTED_CONFIG_MISSING)
    elif bad_sha is not None or parent_sha is not None:
        if bad_sha is not None and not _is_sha40(bad_sha):
            raise PRLifecycleError(TRUSTED_CONFIG_MISSING)
        if parent_sha is not None and not _is_sha40(parent_sha):
            raise PRLifecycleError(TRUSTED_CONFIG_MISSING)

    owner, repo_name = repo.split("/", 1)
    return TrustedConfig(
        gateway_url=gateway_url,
        role=role,
        auth_bearer=auth_bearer,
        repo=repo,
        owner=owner,
        repo_name=repo_name,
        base_branch=base_branch,
        run_id=run_id,
        risk_level=risk_level,
        expected_base_sha=expected_base_sha,
        hmac_key=hmac_value.encode("utf-8"),
        revert_bad_sha=bad_sha,
        revert_parent_sha=parent_sha,
    )


def _is_sha40(value):
    return isinstance(value, str) and bool(_SHA40_RE.match(value))


def _safe_path(path):
    if not isinstance(path, str) or not path or len(path) > 1024:
        raise PRLifecycleError(INVALID_INPUT)
    if "\x00" in path or "\\" in path or path.startswith(("/", "~")):
        raise PRLifecycleError(INVALID_INPUT)
    if re.match(r"^[A-Za-z]:", path):
        raise PRLifecycleError(INVALID_INPUT)
    parts = path.split("/")
    if any(not part or part in (".", "..") for part in parts):
        raise PRLifecycleError(INVALID_INPUT)
    if any(part.lower() == ".git" for part in parts):
        raise PRLifecycleError(INVALID_INPUT)
    return "/".join(parts)


def _validate_text_fields(inp):
    for field in ("commit_message", "pr_title"):
        value = inp.get(field)
        if not isinstance(value, str) or not value or "\r" in value or "\n" in value:
            raise PRLifecycleError(INVALID_INPUT)
        if len(value) > 200:
            raise PRLifecycleError(LIMIT_EXCEEDED)
    body = inp.get("pr_body", "")
    if not isinstance(body, str):
        raise PRLifecycleError(INVALID_INPUT)
    if len(body.encode("utf-8")) > 16 * 1024:
        raise PRLifecycleError(LIMIT_EXCEEDED)
    for field in ("pr_title", "pr_body"):
        value = inp.get(field, "")
        if MARKER_PREFIX in value:
            raise PRLifecycleError(INVALID_INPUT)


def _validate_l2_fields(inp):
    pull_number = inp.get("pull_number")
    if isinstance(pull_number, bool) or not isinstance(pull_number, int) or pull_number < 1:
        raise PRLifecycleError(INVALID_INPUT)
    ticket = inp.get("approval_ticket")
    if (
        not isinstance(ticket, str)
        or not re.fullmatch(
            r"tkt-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            ticket,
        )
    ):
        raise PRLifecycleError(INVALID_INPUT)
    if inp["action"] == "merge_pr":
        if inp.get("merge_method") not in ("merge", "squash", "rebase"):
            raise PRLifecycleError(INVALID_INPUT)
        title = inp.get("commit_title")
        if not isinstance(title, str) or not title or "\r" in title or "\n" in title:
            raise PRLifecycleError(INVALID_INPUT)
        if len(title) > 200:
            raise PRLifecycleError(LIMIT_EXCEEDED)
        message = inp.get("commit_message", "")
        if not isinstance(message, str) or "\r" in message or "\n" in message:
            raise PRLifecycleError(INVALID_INPUT)
        if len(message) > 4096:
            raise PRLifecycleError(LIMIT_EXCEEDED)


def _validate_changes(changes):
    if not isinstance(changes, list) or not changes or len(changes) > MAX_FILES:
        raise PRLifecycleError(LIMIT_EXCEEDED)
    seen = set()
    total = 0
    out = []
    for item in changes:
        if not isinstance(item, dict):
            raise PRLifecycleError(INVALID_INPUT)
        path = _safe_path(item.get("path"))
        content = item.get("content")
        if not isinstance(content, str) or "\x00" in content:
            raise PRLifecycleError(INVALID_INPUT)
        size = len(content.encode("utf-8"))
        if size > MAX_FILE_BYTES:
            raise PRLifecycleError(LIMIT_EXCEEDED)
        total += size
        if total > MAX_TOTAL_BYTES:
            raise PRLifecycleError(LIMIT_EXCEEDED)
        if path in seen:
            raise PRLifecycleError(INVALID_INPUT)
        seen.add(path)
        out.append({"path": path, "content": content})
    out.sort(key=lambda item: item["path"])
    return out


def _canonical_bytes(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _hmac_hex(config, purpose, payload):
    data = purpose.encode("utf-8") + b"\x00" + _canonical_bytes(payload)
    return hmac.new(config.hmac_key, data, hashlib.sha256).hexdigest()


def _binding(config, inp, extra=None):
    payload = {
        "action": inp["action"],
        "idempotency_key": inp["idempotency_key"],
        "commit_message": inp.get("commit_message", ""),
        "pr_title": inp.get("pr_title", ""),
        "pr_body": inp.get("pr_body", ""),
        "changes": inp.get("changes", []),
        "extra": extra or {},
    }
    id_ref = _hmac_hex(config, "id", inp["idempotency_key"])
    bind = _hmac_hex(config, "binding", payload)
    marker = "%s v1 id=%s bind=%s" % (MARKER_PREFIX, id_ref[:16], bind)
    branch = "fix/%s-%s" % (config.run_id, id_ref[:12])
    if len(branch) > 255 or not _BRANCH_RE.match(branch):
        raise PRLifecycleError(TRUSTED_CONFIG_MISSING)
    return branch, marker


def _remaining_ms(deadline):
    if deadline is None:
        return 60000
    try:
        if deadline.expired():
            return 0
        return max(0, int(deadline.remaining_ms()))
    except Exception:
        return 0


def _error_output(config, action, phases, effect_state, policy_reason=""):
    output = {
        "schema_version": "1",
        "action": action,
        "outcome": "ERROR",
        "effect_state": effect_state,
        "repository": config.repo,
        "base_branch": config.base_branch,
        "phases": list(phases),
        "changed_paths": [],
    }
    if policy_reason:
        output["policy_reason"] = policy_reason
    return output


def _raise_error(subcode, config, action, tracker, phases, *, retryable=False,
                 effect_state="NOT_ATTEMPTED", policy_reason=""):
    raise PRLifecycleError(
        subcode,
        retryable=retryable,
        effects=tracker.items(),
        output=_error_output(config, action, phases, effect_state, policy_reason),
    )


def _timeout_or_value(deadline, config, action, tracker, phases):
    value = _remaining_ms(deadline)
    if value <= 0:
        _raise_error(
            DEADLINE_EXCEEDED, config, action, tracker, phases,
            effect_state="ATTEMPTED" if tracker.items() else "NOT_ATTEMPTED",
        )
    return value


def _settle_pause(deadline, config, action, tracker, phases):
    remaining = _remaining_ms(deadline)
    if remaining <= 0:
        _raise_error(
            DEADLINE_EXCEEDED, config, action, tracker, phases,
            effect_state="ATTEMPTED" if tracker.items() else "NOT_ATTEMPTED",
        )
    time.sleep(min(SETTLE_SLEEP_SECONDS, remaining / 1000.0))


def _translate_gateway_failure(exc, config, action, tracker, phases):
    if exc.forwarded:
        tracker.add("github_write")
    reason = exc.reason_code if _SAFE_REASON_RE.match(exc.reason_code or "") else ""
    if exc.kind == "DENIED":
        _raise_error(
            POLICY_DENIED, config, action, tracker, phases,
            effect_state="ATTEMPTED", policy_reason=reason or "POLICY_DENIED",
        )
    if exc.kind == "UNKNOWN" or exc.forwarded:
        _raise_error(
            EFFECT_UNKNOWN, config, action, tracker, phases,
            retryable=False, effect_state="UNKNOWN", policy_reason=reason,
        )
    if exc.kind in ("UNAVAILABLE", "SCHEMA"):
        _raise_error(
            GATEWAY_UNAVAILABLE, config, action, tracker, phases,
            retryable=True, effect_state="ATTEMPTED", policy_reason=reason,
        )
    _raise_error(INTERNAL, config, action, tracker, phases, effect_state="ATTEMPTED")


def _read(adapter, method, config, action, tracker, phases, deadline, *args, **kwargs):
    tracker.add("network_read")
    timeout_ms = _timeout_or_value(deadline, config, action, tracker, phases)
    try:
        return getattr(adapter, method)(*args, timeout_ms=timeout_ms, **kwargs)
    except GatewayFailure as exc:
        _translate_gateway_failure(exc, config, action, tracker, phases)
    except PRLifecycleError:
        raise
    except Exception:
        _raise_error(
            GATEWAY_UNAVAILABLE, config, action, tracker, phases,
            retryable=True, effect_state="ATTEMPTED",
        )


def _write(adapter, method, config, action, tracker, phases, deadline, *args, **kwargs):
    tracker.add("network_write")
    timeout_ms = _timeout_or_value(deadline, config, action, tracker, phases)
    try:
        result = getattr(adapter, method)(*args, timeout_ms=timeout_ms, **kwargs)
    except GatewayFailure as exc:
        _translate_gateway_failure(exc, config, action, tracker, phases)
    except PRLifecycleError:
        raise
    except Exception:
        _raise_error(
            EFFECT_UNKNOWN, config, action, tracker, phases,
            retryable=False, effect_state="UNKNOWN",
        )
    tracker.add("github_write")
    return result


def _load_branches(adapter, config, action, tracker, phases, deadline):
    branches = {}
    for page in range(1, MAX_PAGES + 1):
        items = _read(
            adapter, "list_branches", config, action, tracker, phases, deadline,
            page=page, per_page=PAGE_SIZE,
        )
        if not isinstance(items, list):
            _raise_error(GATEWAY_UNAVAILABLE, config, action, tracker, phases, retryable=True,
                         effect_state="ATTEMPTED")
        for item in items:
            if not isinstance(item, dict):
                _raise_error(GATEWAY_UNAVAILABLE, config, action, tracker, phases, retryable=True,
                             effect_state="ATTEMPTED")
            name, sha = item.get("name"), item.get("sha")
            if not isinstance(name, str) or not _is_sha40(sha):
                _raise_error(GATEWAY_UNAVAILABLE, config, action, tracker, phases, retryable=True,
                             effect_state="ATTEMPTED")
            if name in branches and branches[name] != sha:
                _raise_error(IDEMPOTENCY_CONFLICT, config, action, tracker, phases,
                             effect_state="ATTEMPTED")
            branches[name] = sha
        if len(items) < PAGE_SIZE:
            return branches
    _raise_error(GATEWAY_UNAVAILABLE, config, action, tracker, phases, retryable=True,
                 effect_state="ATTEMPTED")


def _normalize_pr(pr, *, require_details=True):
    if not isinstance(pr, dict):
        return None
    required_strings = [
        "state",
        "head_ref",
        "head_sha",
        "head_repo_full_name",
        "base_ref",
    ]
    if require_details:
        required_strings.extend(("body", "title"))
    if any(not isinstance(pr.get(key), str) for key in required_strings):
        return None
    if not _is_sha40(pr.get("head_sha")):
        return None
    if require_details and not pr.get("title"):
        return None
    number = pr.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        return None
    merged = pr.get("merged")
    draft = pr.get("draft")
    if not isinstance(merged, bool) or not isinstance(draft, bool):
        return None
    merge_sha = pr.get("merge_commit_sha")
    if merge_sha is not None and not _is_sha40(merge_sha):
        return None
    return dict(pr)


def _load_prs(adapter, config, action, tracker, phases, deadline, head_branch=None):
    matched = []
    for page in range(1, MAX_PAGES + 1):
        items = _read(
            adapter, "list_pull_requests", config, action, tracker, phases, deadline,
            state="all", page=page, per_page=PAGE_SIZE,
        )
        if not isinstance(items, list):
            _raise_error(GATEWAY_UNAVAILABLE, config, action, tracker, phases, retryable=True,
                         effect_state="ATTEMPTED")
        for raw in items:
            pr = _normalize_pr(raw, require_details=False)
            if pr is None:
                _raise_error(GATEWAY_UNAVAILABLE, config, action, tracker, phases, retryable=True,
                             effect_state="ATTEMPTED")
            if head_branch is None or (
                pr["head_ref"] == head_branch
                and pr["base_ref"] == config.base_branch
                and pr["head_repo_full_name"].lower() == config.repo.lower()
            ):
                matched.append(pr)
        if len(items) < PAGE_SIZE:
            return matched
    _raise_error(GATEWAY_UNAVAILABLE, config, action, tracker, phases, retryable=True,
                 effect_state="ATTEMPTED")


def _read_pr(adapter, pull_number, config, action, tracker, phases, deadline):
    raw = _read(
        adapter, "read_pull_request", config, action, tracker, phases, deadline,
        pull_number,
    )
    pr = _normalize_pr(raw, require_details=True)
    if pr is None or pr["number"] != pull_number:
        _raise_error(GATEWAY_UNAVAILABLE, config, action, tracker, phases, retryable=True,
                     effect_state="ATTEMPTED")
    return pr


def _load_pr_files(adapter, pull_number, config, action, tracker, phases, deadline):
    paths = []
    for page in range(1, MAX_PAGES + 1):
        items = _read(
            adapter, "list_pull_request_files", config, action, tracker, phases, deadline,
            pull_number, page=page, per_page=PAGE_SIZE,
        )
        if not isinstance(items, list):
            _raise_error(GATEWAY_UNAVAILABLE, config, action, tracker, phases, retryable=True,
                         effect_state="ATTEMPTED")
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                _raise_error(GATEWAY_UNAVAILABLE, config, action, tracker, phases, retryable=True,
                             effect_state="ATTEMPTED")
            paths.append(_safe_path(item["path"]))
        if len(items) < PAGE_SIZE:
            return paths
    _raise_error(GATEWAY_UNAVAILABLE, config, action, tracker, phases, retryable=True,
                 effect_state="ATTEMPTED")


def _verify_contents(adapter, changes, *, ref=None, sha=None, config, action,
                     tracker, phases, deadline):
    for item in changes:
        result = _read(
            adapter, "get_file", config, action, tracker, phases, deadline,
            item["path"], ref=ref, sha=sha,
        )
        if not isinstance(result, dict) or result.get("status") != "OK":
            _raise_error(IDEMPOTENCY_CONFLICT, config, action, tracker, phases,
                         effect_state="ATTEMPTED")
        if result.get("content") != item["content"]:
            _raise_error(IDEMPOTENCY_CONFLICT, config, action, tracker, phases,
                         effect_state="ATTEMPTED")


def _verify_branch_commit(adapter, head_branch, head_sha, base_sha, changes, config,
                          action, tracker, phases, deadline):
    commits = _read(
        adapter, "list_commits", config, action, tracker, phases, deadline,
        head_branch, per_page=2,
    )
    if (
        not isinstance(commits, list)
        or len(commits) < 2
        or not isinstance(commits[0], dict)
        or not isinstance(commits[1], dict)
        or commits[0].get("sha") != head_sha
        or commits[1].get("sha") != base_sha
    ):
        _raise_error(IDEMPOTENCY_CONFLICT, config, action, tracker, phases,
                     effect_state="ATTEMPTED")
    commit = _read(
        adapter, "get_commit", config, action, tracker, phases, deadline, head_sha,
    )
    if not isinstance(commit, dict) or commit.get("sha") != head_sha:
        _raise_error(GATEWAY_UNAVAILABLE, config, action, tracker, phases, retryable=True,
                     effect_state="ATTEMPTED")
    files = commit.get("files")
    if not isinstance(files, list):
        _raise_error(GATEWAY_UNAVAILABLE, config, action, tracker, phases, retryable=True,
                     effect_state="ATTEMPTED")
    remote_paths = []
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            _raise_error(GATEWAY_UNAVAILABLE, config, action, tracker, phases, retryable=True,
                         effect_state="ATTEMPTED")
        remote_paths.append(_safe_path(item["path"]))
    if sorted(remote_paths) != sorted(item["path"] for item in changes):
        _raise_error(IDEMPOTENCY_CONFLICT, config, action, tracker, phases,
                     effect_state="ATTEMPTED")
    _verify_contents(
        adapter, changes, ref="refs/heads/" + head_branch,
        config=config, action=action, tracker=tracker, phases=phases, deadline=deadline,
    )


def _verify_existing_pr(adapter, pr, marker, changes, config, action, tracker,
                        phases, deadline, *, expected_title=None, expected_draft=None):
    if marker not in pr["body"]:
        _raise_error(IDEMPOTENCY_CONFLICT, config, action, tracker, phases,
                     effect_state="ATTEMPTED")
    if expected_title is not None and pr.get("title") != expected_title:
        _raise_error(IDEMPOTENCY_CONFLICT, config, action, tracker, phases,
                     effect_state="ATTEMPTED")
    if expected_draft is not None and pr.get("draft") is not expected_draft:
        _raise_error(IDEMPOTENCY_CONFLICT, config, action, tracker, phases,
                     effect_state="ATTEMPTED")
    remote_paths = _load_pr_files(
        adapter, pr["number"], config, action, tracker, phases, deadline,
    )
    if sorted(remote_paths) != sorted(item["path"] for item in changes):
        _raise_error(IDEMPOTENCY_CONFLICT, config, action, tracker, phases,
                     effect_state="ATTEMPTED")
    _verify_contents(
        adapter, changes, ref="refs/pull/%d/head" % pr["number"],
        config=config, action=action, tracker=tracker, phases=phases, deadline=deadline,
    )


def _success_pr_output(config, action, outcome, phases, changes, branch, pr):
    return {
        "schema_version": "1",
        "action": action,
        "outcome": outcome,
        "effect_state": "CONFIRMED",
        "repository": config.repo,
        "base_branch": config.base_branch,
        "head_branch": branch,
        "pull_number": pr["number"],
        "pull_url": pr.get("url") or "https://github.com/%s/pull/%d" % (config.repo, pr["number"]),
        "head_sha": pr["head_sha"],
        "draft": pr["draft"],
        "changed_paths": [item["path"] for item in changes],
        "phases": list(phases),
    }


def _ensure_pr(adapter, inp, changes, config, tracker, phases, deadline, *,
               expected_base_sha, force_draft, binding_extra=None):
    action = inp["action"]
    branch, marker = _binding(config, {**inp, "changes": changes}, binding_extra)
    body = marker + ("\n\n" + inp["pr_body"] if inp.get("pr_body") else "")
    phases.append("REMOTE_READ")
    branches = _load_branches(adapter, config, action, tracker, phases, deadline)
    base_sha = branches.get(config.base_branch)
    if base_sha != expected_base_sha:
        _raise_error(
            REVERT_STATE_MISMATCH if action == "ensure_revert_pr" else IDEMPOTENCY_CONFLICT,
            config, action, tracker, phases, effect_state="ATTEMPTED",
        )
    phases.append("BASE_VERIFIED")

    prs = _load_prs(adapter, config, action, tracker, phases, deadline, branch)
    if len(prs) > 1:
        _raise_error(IDEMPOTENCY_CONFLICT, config, action, tracker, phases,
                     effect_state="ATTEMPTED")
    if len(prs) == 1:
        pr = _read_pr(
            adapter, prs[0]["number"], config, action, tracker, phases, deadline,
        )
        if (
            pr["head_ref"] != branch
            or pr["base_ref"] != config.base_branch
            or pr["head_repo_full_name"].lower() != config.repo.lower()
        ):
            _raise_error(IDEMPOTENCY_CONFLICT, config, action, tracker, phases,
                         effect_state="ATTEMPTED")
        _verify_existing_pr(
            adapter, pr, marker, changes, config, action, tracker, phases, deadline,
            expected_title=inp["pr_title"], expected_draft=force_draft or config.risk_level == "L2",
        )
        phases.extend(["CONTENT_VERIFIED", "PR_RECONCILED"])
        return _success_pr_output(config, action, "EXISTING", phases, changes, branch, pr)

    head_sha = branches.get(branch)
    if head_sha is None:
        _write(
            adapter, "create_branch", config, action, tracker, phases, deadline,
            branch, config.base_branch,
        )
        phases.append("BRANCH_CREATED")
        for attempt in range(SETTLE_ATTEMPTS):
            branches = _load_branches(adapter, config, action, tracker, phases, deadline)
            if branches.get(config.base_branch) != expected_base_sha:
                _raise_error(IDEMPOTENCY_CONFLICT, config, action, tracker, phases,
                             effect_state="ATTEMPTED")
            visible_sha = branches.get(branch)
            if visible_sha == expected_base_sha:
                break
            if visible_sha is not None or attempt == SETTLE_ATTEMPTS - 1:
                _raise_error(IDEMPOTENCY_CONFLICT, config, action, tracker, phases,
                             effect_state="ATTEMPTED")
            _settle_pause(deadline, config, action, tracker, phases)
        head_sha = expected_base_sha

    if head_sha == expected_base_sha:
        _write(
            adapter, "push_files", config, action, tracker, phases, deadline,
            branch, changes, inp["commit_message"],
        )
        phases.append("CONTENT_WRITTEN")
        for attempt in range(SETTLE_ATTEMPTS):
            branches = _load_branches(adapter, config, action, tracker, phases, deadline)
            if branches.get(config.base_branch) != expected_base_sha:
                _raise_error(IDEMPOTENCY_CONFLICT, config, action, tracker, phases,
                             effect_state="ATTEMPTED")
            head_sha = branches.get(branch)
            if _is_sha40(head_sha) and head_sha != expected_base_sha:
                break
            if head_sha not in (None, expected_base_sha):
                _raise_error(IDEMPOTENCY_CONFLICT, config, action, tracker, phases,
                             effect_state="ATTEMPTED")
            if attempt == SETTLE_ATTEMPTS - 1:
                _raise_error(EFFECT_UNKNOWN, config, action, tracker, phases,
                             effect_state="UNKNOWN")
            _settle_pause(deadline, config, action, tracker, phases)

    _verify_branch_commit(
        adapter, branch, head_sha, expected_base_sha, changes,
        config, action, tracker, phases, deadline,
    )
    phases.append("CONTENT_VERIFIED")
    draft = True if force_draft else config.risk_level == "L2"
    _write(
        adapter, "create_pull_request", config, action, tracker, phases, deadline,
        branch, config.base_branch, inp["pr_title"], body, draft,
    )
    phases.append("PR_CREATED")
    for attempt in range(SETTLE_ATTEMPTS):
        prs = _load_prs(adapter, config, action, tracker, phases, deadline, branch)
        if len(prs) == 1:
            break
        if len(prs) > 1:
            _raise_error(IDEMPOTENCY_CONFLICT, config, action, tracker, phases,
                         effect_state="ATTEMPTED")
        if attempt == SETTLE_ATTEMPTS - 1:
            _raise_error(EFFECT_UNKNOWN, config, action, tracker, phases,
                         effect_state="UNKNOWN")
        _settle_pause(deadline, config, action, tracker, phases)
    pr = _read_pr(
        adapter, prs[0]["number"], config, action, tracker, phases, deadline,
    )
    if (
        pr["head_ref"] != branch
        or pr["base_ref"] != config.base_branch
        or pr["head_repo_full_name"].lower() != config.repo.lower()
    ):
        _raise_error(IDEMPOTENCY_CONFLICT, config, action, tracker, phases,
                     effect_state="ATTEMPTED")
    _verify_existing_pr(
        adapter, pr, marker, changes, config, action, tracker, phases, deadline,
        expected_title=inp["pr_title"], expected_draft=draft,
    )
    if pr["draft"] != draft:
        _raise_error(IDEMPOTENCY_CONFLICT, config, action, tracker, phases,
                     effect_state="ATTEMPTED")
    phases.append("PR_RECONCILED")
    return _success_pr_output(config, action, "CREATED", phases, changes, branch, pr)


def _run_fix(adapter, inp, config, tracker, phases, deadline):
    _validate_text_fields(inp)
    changes = _validate_changes(inp.get("changes"))
    return _ensure_pr(
        adapter, inp, changes, config, tracker, phases, deadline,
        expected_base_sha=config.expected_base_sha,
        force_draft=False,
    )


def _run_revert(adapter, inp, config, tracker, phases, deadline):
    _validate_text_fields(inp)
    action = inp["action"]
    bad_sha = config.revert_bad_sha
    parent_sha = config.revert_parent_sha
    phases.append("REMOTE_READ")
    branches = _load_branches(adapter, config, action, tracker, phases, deadline)
    if branches.get(config.base_branch) != bad_sha:
        _raise_error(REVERT_STATE_MISMATCH, config, action, tracker, phases,
                     effect_state="ATTEMPTED")
    commits = _read(
        adapter, "list_commits", config, action, tracker, phases, deadline,
        bad_sha, per_page=2,
    )
    if (
        not isinstance(commits, list)
        or len(commits) < 2
        or not isinstance(commits[0], dict)
        or not isinstance(commits[1], dict)
        or commits[0].get("sha") != bad_sha
        or commits[1].get("sha") != parent_sha
    ):
        _raise_error(REVERT_STATE_MISMATCH, config, action, tracker, phases,
                     effect_state="ATTEMPTED")
    commit = _read(
        adapter, "get_commit", config, action, tracker, phases, deadline, bad_sha,
    )
    if not isinstance(commit, dict) or commit.get("sha") != bad_sha or not isinstance(commit.get("files"), list):
        _raise_error(GATEWAY_UNAVAILABLE, config, action, tracker, phases,
                     retryable=True, effect_state="ATTEMPTED")
    if not commit["files"] or len(commit["files"]) > MAX_FILES:
        _raise_error(LIMIT_EXCEEDED, config, action, tracker, phases,
                     effect_state="ATTEMPTED")
    changes = []
    for item in commit["files"]:
        if not isinstance(item, dict):
            _raise_error(GATEWAY_UNAVAILABLE, config, action, tracker, phases,
                         retryable=True, effect_state="ATTEMPTED")
        path = _safe_path(item.get("path"))
        status = item.get("status")
        if status == "added":
            _raise_error(REVERT_DELETE_UNSUPPORTED, config, action, tracker, phases,
                         effect_state="ATTEMPTED")
        if item.get("binary") is True:
            _raise_error(REVERT_STATE_MISMATCH, config, action, tracker, phases,
                         effect_state="ATTEMPTED")
        if status not in ("modified", "removed"):
            _raise_error(REVERT_STATE_MISMATCH, config, action, tracker, phases,
                         effect_state="ATTEMPTED")
        parent = _read(
            adapter, "get_file", config, action, tracker, phases, deadline,
            path, sha=parent_sha, ref=None,
        )
        if not isinstance(parent, dict) or parent.get("status") != "OK" or not isinstance(parent.get("content"), str):
            _raise_error(REVERT_STATE_MISMATCH, config, action, tracker, phases,
                         effect_state="ATTEMPTED")
        changes.append({"path": path, "content": parent["content"]})
    changes = _validate_changes(changes)
    phases.append("REVERT_VERIFIED")
    return _ensure_pr(
        adapter, inp, changes, config, tracker, phases, deadline,
        expected_base_sha=bad_sha,
        force_draft=True,
        binding_extra={"bad_sha": bad_sha, "parent_sha": parent_sha},
    )


def _terminal_output(config, action, outcome, pull_number, phases, *, result_sha=None):
    output = {
        "schema_version": "1",
        "action": action,
        "outcome": outcome,
        "effect_state": "CONFIRMED",
        "repository": config.repo,
        "base_branch": config.base_branch,
        "pull_number": pull_number,
        "changed_paths": [],
        "phases": list(phases),
    }
    if result_sha is not None:
        output["result_sha"] = result_sha
    return output


def _validate_pr_binding(pr, config, action, tracker, phases):
    if pr["base_ref"] != config.base_branch or pr["head_repo_full_name"].lower() != config.repo.lower():
        _raise_error(IDEMPOTENCY_CONFLICT, config, action, tracker, phases,
                     effect_state="ATTEMPTED")


def _run_merge(adapter, inp, config, tracker, phases, deadline):
    action = inp["action"]
    _validate_l2_fields(inp)
    phases.append("REMOTE_READ")
    pr = _read_pr(adapter, inp["pull_number"], config, action, tracker, phases, deadline)
    _validate_pr_binding(pr, config, action, tracker, phases)
    if pr["merged"]:
        phases.append("PR_RECONCILED")
        return _terminal_output(
            config, action, "ALREADY_MERGED", pr["number"], phases,
            result_sha=pr.get("merge_commit_sha"),
        )
    if pr["state"] != "open":
        _raise_error(IDEMPOTENCY_CONFLICT, config, action, tracker, phases,
                     effect_state="ATTEMPTED")
    merge_result = _write(
        adapter, "merge_pull_request", config, action, tracker, phases, deadline,
        pr["number"], inp["approval_ticket"], inp["merge_method"],
        inp["commit_title"], inp.get("commit_message", ""),
    )
    phases.append("L2_REQUESTED")
    result_sha = merge_result.get("sha") if isinstance(merge_result, dict) else None
    if _is_sha40(result_sha):
        phases.append("L2_CONFIRMED")
        return _terminal_output(
            config, action, "MERGED", pr["number"], phases,
            result_sha=result_sha,
        )
    for attempt in range(SETTLE_ATTEMPTS):
        after = _read_pr(adapter, pr["number"], config, action, tracker, phases, deadline)
        if after["merged"] and _is_sha40(after.get("merge_commit_sha")):
            break
        if attempt == SETTLE_ATTEMPTS - 1:
            _raise_error(EFFECT_UNKNOWN, config, action, tracker, phases,
                         effect_state="UNKNOWN")
        _settle_pause(deadline, config, action, tracker, phases)
    phases.append("L2_CONFIRMED")
    return _terminal_output(
        config, action, "MERGED", pr["number"], phases,
        result_sha=after["merge_commit_sha"],
    )


def _run_close(adapter, inp, config, tracker, phases, deadline):
    action = inp["action"]
    _validate_l2_fields(inp)
    phases.append("REMOTE_READ")
    pr = _read_pr(adapter, inp["pull_number"], config, action, tracker, phases, deadline)
    _validate_pr_binding(pr, config, action, tracker, phases)
    if pr["merged"]:
        phases.append("PR_RECONCILED")
        return _terminal_output(config, action, "ALREADY_MERGED", pr["number"], phases)
    if pr["state"] == "closed":
        phases.append("PR_RECONCILED")
        return _terminal_output(config, action, "ALREADY_CLOSED", pr["number"], phases)
    if pr["state"] != "open":
        _raise_error(IDEMPOTENCY_CONFLICT, config, action, tracker, phases,
                     effect_state="ATTEMPTED")
    _write(
        adapter, "close_pull_request", config, action, tracker, phases, deadline,
        pr["number"], inp["approval_ticket"],
    )
    phases.append("L2_REQUESTED")
    for attempt in range(SETTLE_ATTEMPTS):
        after = _read_pr(adapter, pr["number"], config, action, tracker, phases, deadline)
        if not after["merged"] and after["state"] == "closed":
            break
        if attempt == SETTLE_ATTEMPTS - 1:
            _raise_error(EFFECT_UNKNOWN, config, action, tracker, phases,
                         effect_state="UNKNOWN")
        _settle_pause(deadline, config, action, tracker, phases)
    phases.append("L2_CONFIRMED")
    return _terminal_output(config, action, "CLOSED", pr["number"], phases)


def run(inp, *, adapter=None, trusted_env=None, deadline=None):
    if not isinstance(inp, dict):
        raise PRLifecycleError(INVALID_INPUT)
    action = inp.get("action")
    if action not in ALL_ACTIONS:
        raise PRLifecycleError(INVALID_INPUT)
    config = load_trusted_config(action, trusted_env)
    tracker = EffectTracker(config.repo)
    phases = ["CONFIG_VALIDATED"]
    if adapter is None:
        from .adapters.policy_gateway import PolicyGatewayAdapter
        adapter = PolicyGatewayAdapter(config)
    try:
        if action == "ensure_fix_pr":
            output = _run_fix(adapter, inp, config, tracker, phases, deadline)
        elif action == "ensure_revert_pr":
            output = _run_revert(adapter, inp, config, tracker, phases, deadline)
        elif action == "merge_pr":
            output = _run_merge(adapter, inp, config, tracker, phases, deadline)
        else:
            output = _run_close(adapter, inp, config, tracker, phases, deadline)
    except PRLifecycleError:
        raise
    except Exception:
        _raise_error(INTERNAL, config, action, tracker, phases,
                     effect_state="ATTEMPTED" if tracker.items() else "NOT_ATTEMPTED")
    output["_side_effects"] = tracker.items()
    return output
