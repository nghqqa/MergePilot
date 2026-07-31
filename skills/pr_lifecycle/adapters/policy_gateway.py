"""MCP client adapter for the MergePilot Policy Gateway.

The adapter exposes only normalized methods used by PRLifecycle core. MCP SDK
imports are lazy so unit tests can inject an in-memory adapter without the
optional production dependencies installed.
"""
from __future__ import annotations

import asyncio
import base64
import json
import re

from ..core import GatewayFailure


_REASON_RE = re.compile(r"reason_code=([A-Z0-9_]+)")
_SHA_RE = re.compile(r"\b[0-9a-f]{40}\b")


def _isolated_httpx_client(headers=None, timeout=None, auth=None):
    """Create the MCP transport client without ambient proxy inheritance.

    The Gateway URL and role credential are deploy-owned. Inheriting
    HTTP(S)_PROXY/ALL_PROXY or following redirects could route that credential
    outside the fixed Policy Gateway trust boundary.
    """
    import httpx

    return httpx.AsyncClient(
        headers=headers,
        timeout=timeout,
        auth=auth,
        follow_redirects=False,
        trust_env=False,
    )


class PolicyGatewayAdapter:
    def __init__(self, config):
        self.config = config

    async def _async_call(self, tool, args, state):
        try:
            from mcp.client.sse import sse_client
            from mcp import ClientSession
        except Exception as exc:
            raise RuntimeError("mcp dependency unavailable") from exc
        url = "%s/%s/sse" % (self.config.gateway_url, self.config.role)
        async with sse_client(
            url,
            headers={"Authorization": "Bearer " + self.config.auth_bearer},
            httpx_client_factory=_isolated_httpx_client,
        ) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                state["call_started"] = True
                result = await session.call_tool(tool, args)
                state["call_completed"] = True
                state["result"] = result
                return result

    def _call_result(self, tool, args, timeout_ms, *, write):
        state = {"call_started": False, "call_completed": False, "result": None}
        try:
            return asyncio.run(
                asyncio.wait_for(
                    self._async_call(tool, args, state),
                    timeout=max(0.001, int(timeout_ms) / 1000.0),
                )
            )
        except Exception:
            if state["call_completed"] and state["result"] is not None:
                return state["result"]
            if write and state["call_started"]:
                raise GatewayFailure("UNKNOWN", "UPSTREAM_OUTCOME_UNKNOWN", forwarded=True)
            raise GatewayFailure("UNAVAILABLE", "GATEWAY_UNAVAILABLE", forwarded=False)

    @staticmethod
    def _content_parts(result):
        summaries = []
        resources = []
        for item in getattr(result, "content", None) or []:
            text = getattr(item, "text", None)
            if isinstance(text, str):
                summaries.append(text)
                continue
            resource = getattr(item, "resource", None)
            if resource is None:
                continue
            text = getattr(resource, "text", None)
            if isinstance(text, str):
                resources.append(text)
                continue
            blob = getattr(resource, "blob", None)
            if isinstance(blob, str):
                try:
                    resources.append(base64.b64decode(blob).decode("utf-8", "replace"))
                except Exception:
                    raise GatewayFailure("SCHEMA", "UPSTREAM_SCHEMA_INVALID")
        return summaries, resources

    @classmethod
    def _all_text(cls, result):
        summaries, resources = cls._content_parts(result)
        return "\n".join(summaries + resources)

    @classmethod
    def _json_payload(cls, result):
        summaries, resources = cls._content_parts(result)
        candidates = resources + summaries + ["\n".join(summaries + resources)]
        for text in candidates:
            try:
                return json.loads(text)
            except Exception:
                continue
        raise GatewayFailure("SCHEMA", "UPSTREAM_SCHEMA_INVALID")

    @classmethod
    def _raise_result_error(cls, result, *, write):
        text = cls._all_text(result)
        match = _REASON_RE.search(text)
        if match:
            raise GatewayFailure("DENIED", match.group(1), forwarded=False)
        if "Validation Failed" in text and "Field:head Code:invalid" in text:
            raise GatewayFailure("UNAVAILABLE", "UPSTREAM_HEAD_NOT_VISIBLE", forwarded=False)
        if write:
            raise GatewayFailure("DENIED", "UPSTREAM_REJECTED", forwarded=False)
        raise GatewayFailure("UNAVAILABLE", "UPSTREAM_REJECTED", forwarded=False)

    def _invoke(self, tool, args, timeout_ms, *, write=False):
        result = self._call_result(tool, args, timeout_ms, write=write)
        text = self._all_text(result).lstrip().lower()
        textual_error = text.startswith(("error", "failed", "missing required parameter"))
        if bool(getattr(result, "is_error", False)) or textual_error:
            self._raise_result_error(result, write=write)
        return result

    @staticmethod
    def _list_payload(payload, keys=()):
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in keys:
                value = payload.get(key)
                if isinstance(value, list):
                    return value
        raise GatewayFailure("SCHEMA", "UPSTREAM_SCHEMA_INVALID")

    def _repo_args(self):
        return {"owner": self.config.owner, "repo": self.config.repo_name}

    def list_branches(self, *, page, per_page, timeout_ms):
        args = {**self._repo_args(), "page": page, "perPage": per_page}
        payload = self._json_payload(self._invoke("list_branches", args, timeout_ms))
        items = self._list_payload(payload, ("branches", "data"))
        out = []
        for item in items:
            if not isinstance(item, dict):
                raise GatewayFailure("SCHEMA", "UPSTREAM_SCHEMA_INVALID")
            sha = item.get("sha")
            if not isinstance(sha, str) and isinstance(item.get("commit"), dict):
                sha = item["commit"].get("sha")
            out.append({"name": item.get("name"), "sha": sha})
        return out

    @staticmethod
    def _bool(value):
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("true", "false"):
            return value.lower() == "true"
        return None

    @classmethod
    def _normalize_pr(cls, item):
        if not isinstance(item, dict):
            raise GatewayFailure("SCHEMA", "UPSTREAM_SCHEMA_INVALID")
        head = item.get("head")
        base = item.get("base")
        if not isinstance(head, dict) or not isinstance(base, dict):
            raise GatewayFailure("SCHEMA", "UPSTREAM_SCHEMA_INVALID")
        head_repo = head.get("repo")
        if not isinstance(head_repo, dict):
            raise GatewayFailure("SCHEMA", "UPSTREAM_SCHEMA_INVALID")
        merged = cls._bool(item.get("merged"))
        if merged is None:
            merged = cls._bool(item.get("isMerged"))
        draft = cls._bool(item.get("draft"))
        if draft is None:
            draft = cls._bool(item.get("isDraft"))
        merge_sha = item.get("merge_commit_sha") or item.get("mergeCommitSha")
        if not isinstance(merge_sha, str) and isinstance(item.get("merge_commit"), dict):
            merge_sha = item["merge_commit"].get("sha")
        return {
            "number": item.get("number"),
            "state": item.get("state"),
            "head_ref": head.get("ref"),
            "head_sha": head.get("sha"),
            "head_repo_full_name": head_repo.get("full_name"),
            "base_ref": base.get("ref"),
            "title": item.get("title") if isinstance(item.get("title"), str) else "",
            "body": item.get("body") if isinstance(item.get("body"), str) else "",
            "merged": merged,
            "draft": draft,
            "merge_commit_sha": merge_sha if isinstance(merge_sha, str) else None,
            "url": item.get("html_url") or item.get("url") or "",
        }

    def list_pull_requests(self, *, state, page, per_page, timeout_ms):
        args = {
            **self._repo_args(), "state": state, "page": page, "perPage": per_page
        }
        payload = self._json_payload(self._invoke("list_pull_requests", args, timeout_ms))
        items = self._list_payload(payload, ("pull_requests", "data"))
        return [self._normalize_pr(item) for item in items]

    def read_pull_request(self, pull_number, *, timeout_ms):
        args = {
            **self._repo_args(), "method": "get", "pullNumber": int(pull_number)
        }
        payload = self._json_payload(self._invoke("pull_request_read", args, timeout_ms))
        return self._normalize_pr(payload)

    def list_pull_request_files(self, pull_number, *, page, per_page, timeout_ms):
        args = {
            **self._repo_args(),
            "method": "get_files",
            "pullNumber": int(pull_number),
            "page": page,
            "perPage": per_page,
        }
        payload = self._json_payload(self._invoke("pull_request_read", args, timeout_ms))
        items = self._list_payload(payload, ("files", "data"))
        out = []
        for item in items:
            if not isinstance(item, dict):
                raise GatewayFailure("SCHEMA", "UPSTREAM_SCHEMA_INVALID")
            out.append({"path": item.get("filename") or item.get("path")})
        return out

    def get_file(self, path, *, ref=None, sha=None, timeout_ms):
        args = {**self._repo_args(), "path": path}
        if sha:
            args["sha"] = sha
        elif ref:
            args["ref"] = ref
        result = self._call_result("get_file_contents", args, timeout_ms, write=False)
        if bool(getattr(result, "is_error", False)):
            text = self._all_text(result)
            if "404" in text or "Not Found" in text or "not found" in text.lower():
                return {"status": "MISSING", "content": None, "sha": None}
            self._raise_result_error(result, write=False)
        summaries, resources = self._content_parts(result)
        if not resources:
            raise GatewayFailure("SCHEMA", "UPSTREAM_SCHEMA_INVALID")
        blob_sha = None
        for text in summaries:
            match = re.search(r"SHA:\s*([0-9a-f]{40})", text)
            if match:
                blob_sha = match.group(1)
                break
        return {"status": "OK", "content": resources[0], "sha": blob_sha}

    def get_commit(self, sha, *, timeout_ms):
        args = {**self._repo_args(), "sha": sha, "detail": "stats"}
        payload = self._json_payload(self._invoke("get_commit", args, timeout_ms))
        if not isinstance(payload, dict):
            raise GatewayFailure("SCHEMA", "UPSTREAM_SCHEMA_INVALID")
        files = payload.get("files")
        if not isinstance(files, list):
            raise GatewayFailure("SCHEMA", "UPSTREAM_SCHEMA_INVALID")
        normalized = []
        for item in files:
            if not isinstance(item, dict):
                raise GatewayFailure("SCHEMA", "UPSTREAM_SCHEMA_INVALID")
            normalized.append({
                "path": item.get("filename") or item.get("path"),
                "status": item.get("status"),
            })
        return {"sha": payload.get("sha"), "files": normalized}

    def list_commits(self, ref, *, per_page, timeout_ms):
        args = {**self._repo_args(), "sha": ref, "page": 1, "perPage": per_page}
        payload = self._json_payload(self._invoke("list_commits", args, timeout_ms))
        items = self._list_payload(payload, ("commits", "data"))
        out = []
        for item in items:
            if not isinstance(item, dict):
                raise GatewayFailure("SCHEMA", "UPSTREAM_SCHEMA_INVALID")
            out.append({"sha": item.get("sha")})
        return out

    def create_branch(self, branch, from_branch, *, timeout_ms):
        args = {
            **self._repo_args(), "branch": branch, "from_branch": from_branch
        }
        self._invoke("create_branch", args, timeout_ms, write=True)
        return {}

    def push_files(self, branch, files, message, *, timeout_ms):
        args = {
            **self._repo_args(), "branch": branch, "files": files, "message": message
        }
        self._invoke("push_files", args, timeout_ms, write=True)
        return {}

    def create_pull_request(self, head, base, title, body, draft, *, timeout_ms):
        args = {
            **self._repo_args(),
            "head": head,
            "base": base,
            "title": title,
            "body": body,
            "draft": bool(draft),
        }
        self._invoke("create_pull_request", args, timeout_ms, write=True)
        return {}

    def merge_pull_request(self, pull_number, ticket, merge_method, commit_title,
                           commit_message, *, timeout_ms):
        args = {
            **self._repo_args(),
            "pullNumber": int(pull_number),
            "approval_ticket": ticket,
            "merge_method": merge_method,
            "commit_title": commit_title,
        }
        if commit_message:
            args["commit_message"] = commit_message
        payload = self._json_payload(
            self._invoke("merge_pull_request", args, timeout_ms, write=True)
        )
        sha = payload.get("sha") if isinstance(payload, dict) else None
        if not isinstance(sha, str) or _SHA_RE.fullmatch(sha) is None:
            raise GatewayFailure("SCHEMA", "UPSTREAM_SCHEMA_INVALID")
        return {"sha": sha}

    def close_pull_request(self, pull_number, ticket, *, timeout_ms):
        args = {
            **self._repo_args(),
            "pullNumber": int(pull_number),
            "approval_ticket": ticket,
            "state": "closed",
        }
        self._invoke("update_pull_request", args, timeout_ms, write=True)
        return {}
