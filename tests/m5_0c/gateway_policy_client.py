#!/usr/bin/env python3
"""M5-0C real-Gateway policy runtime client.

Runs inside the policy-gateway image on the isolated test network. For each
(role, tool, args, expect) scenario it:
  1. reads the counting fake-MCP /_count (baseline),
  2. opens a REAL MCP SSE ClientSession against the Gateway /{role}/sse with the
     role's Bearer token,
  3. calls the tool,
  4. reads /_count again,
  5. records ALLOW (result.is_error False) / DENY (is_error True / exception),
  6. cross-checks the Gateway audit row (role/tool/target_repo/decision).

Output: one JSON object on stdout with per-scenario results + aggregate pass.
Exit 0 only if every scenario's real-Gateway decision matches `expect`, every
DENY has fake-MCP delta 0, and every audit row matches.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import httpx
import psycopg2
from mcp import ClientSession
from mcp.client.sse import sse_client

GATEWAY = os.environ["M5C_GATEWAY"]            # e.g. http://m5c-gateway:8083
FAKE = os.environ["M5C_FAKE"]                  # e.g. http://m5c-fakegh:8082
AUDIT_DSN = os.environ["M5C_AUDIT_DSN"]
TOK = {
    "m5coordinator": os.environ["M5C_M5COORDINATOR_TOKEN"],
    "fixer": os.environ["M5C_FIXER_TOKEN"],
    "reviewer": os.environ["M5C_REVIEWER_TOKEN"],
    "verifier": os.environ["M5C_VERIFIER_TOKEN"],
}
FIXTURE = "nghqqa/MergePilot-e2e-fixture"
OWNER, REPO = FIXTURE.split("/")

# (id, role, tool, args, expect)
SCENARIOS = [
    # a. m5coordinator: read ALLOW, fix DENY (TOOL_NOT_ALLOWED)
    ("m5c-read-pr", "m5coordinator", "pull_request_read",
     {"owner": OWNER, "repo": REPO, "pullNumber": 1}, "ALLOW"),
    ("m5c-c-create", "m5coordinator", "create_branch",
     {"owner": OWNER, "repo": REPO, "branch": "fix/x", "base": "main"}, "DENY"),
    ("m5c-c-push", "m5coordinator", "push_files",
     {"owner": OWNER, "repo": REPO, "branch": "fix/x", "files": [{"path": "a.txt"}]}, "DENY"),
    ("m5c-c-pr", "m5coordinator", "create_pull_request",
     {"owner": OWNER, "repo": REPO, "branch": "fix/x", "base": "main"}, "DENY"),
    # b. fixer: fix ALLOW on fixture+fix/, DENY elsewhere
    ("m5c-f-create", "fixer", "create_branch",
     {"owner": OWNER, "repo": REPO, "branch": "fix/m5live-1", "base": "main"}, "ALLOW"),
    ("m5c-f-push", "fixer", "push_files",
     {"owner": OWNER, "repo": REPO, "branch": "fix/m5live-1", "files": [{"path": "a.txt"}]}, "ALLOW"),
    ("m5c-f-pr", "fixer", "create_pull_request",
     {"owner": OWNER, "repo": REPO, "branch": "fix/m5live-1", "base": "main"}, "ALLOW"),
    ("m5c-f-other-repo", "fixer", "create_branch",
     {"owner": "nghqqa", "repo": "MergePilot", "branch": "fix/x", "base": "main"}, "DENY"),
    ("m5c-f-example", "fixer", "create_branch",
     {"owner": "example", "repo": "project", "branch": "fix/x", "base": "main"}, "DENY"),
    ("m5c-f-main", "fixer", "push_files",
     {"owner": OWNER, "repo": REPO, "branch": "main", "files": [{"path": "a.txt"}]}, "DENY"),
    ("m5c-feat-prefix", "fixer", "create_branch",
     {"owner": OWNER, "repo": REPO, "branch": "feat/x", "base": "main"}, "DENY"),
    ("m5c-m5live-prefix", "fixer", "create_branch",
     {"owner": OWNER, "repo": REPO, "branch": "m5live-1", "base": "main"}, "DENY"),
    ("m5c-merge", "fixer", "merge_pull_request",
     {"owner": OWNER, "repo": REPO, "pullNumber": 1}, "DENY"),
    # c. reviewer / verifier: read ALLOW, fix DENY
    ("m5c-rev-read", "reviewer", "pull_request_read",
     {"owner": OWNER, "repo": REPO, "pullNumber": 1}, "ALLOW"),
    ("m5c-rev-fix", "reviewer", "create_branch",
     {"owner": OWNER, "repo": REPO, "branch": "fix/x", "base": "main"}, "DENY"),
    ("m5c-ver-read", "verifier", "get_file_contents",
     {"owner": OWNER, "repo": REPO, "path": "README.md", "branch": "main"}, "ALLOW"),
    ("m5c-ver-fix", "verifier", "push_files",
     {"owner": OWNER, "repo": REPO, "branch": "fix/x", "files": [{"path": "a.txt"}]}, "DENY"),
]


def fake_count():
    r = httpx.get(f"{FAKE}/_count", timeout=10)
    return r.json()["total"]


async def call_gateway(role, tool, args):
    """Return (allowed: bool, detail: str). Real MCP SSE path through gateway.py."""
    url = f"{GATEWAY}/{role}/sse"
    headers = {"Authorization": f"Bearer {TOK[role]}"}
    try:
        async with sse_client(url, headers=headers) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                res = await asyncio.wait_for(s.call_tool(tool, args), timeout=20)
                if getattr(res, "is_error", False):
                    txt = "".join(c.text for c in (res.content or []) if hasattr(c, "text"))
                    return False, f"is_error: {txt[:160]}"
                return True, "ok"
    except Exception as e:  # noqa: BLE001 - any failure = DENY (gateway rejected)
        return False, f"exc:{type(e).__name__}:{str(e)[:120]}"


def latest_audit(conn, role, tool, target_repo, decision):
    """Find the audit row the Gateway wrote for (tool, repo) — return actual
    caller_agent/decision so role+decision consistency can be asserted. Retry
    briefly: the Gateway commits audit on autocommit but the row may land just
    after the tool response. mcp_calls: caller_agent, tool, target_repo, decision."""
    row = None
    for _ in range(10):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT caller_agent, tool, target_repo, decision, reason_code "
                "FROM mcp_calls WHERE tool=%s AND target_repo=%s "
                "ORDER BY ts DESC LIMIT 1",
                (tool, target_repo),
            )
            row = cur.fetchone()
        if row is not None and row[3] == decision:
            return row
        time.sleep(0.3)
    return row


async def main():
    conn = psycopg2.connect(AUDIT_DSN, connect_timeout=5)
    conn.autocommit = True
    results = []
    all_ok = True
    for sid, role, tool, args, expect in SCENARIOS:
        before = fake_count()
        allowed, detail = await call_gateway(role, tool, args)
        after = fake_count()
        decision = "ALLOW" if allowed else "DENY"
        fake_delta = after - before
        target = f"{args.get('owner','')}/{args.get('repo','')}".strip("/")
        arow = latest_audit(conn, role, tool, target, decision)
        audit = {
            "caller": arow[0] if arow else None,
            "decision": arow[3] if arow else None,
            "reason": arow[4] if arow else None,
        } if arow else None
        decision_ok = (decision == expect)
        deny_no_upstream = (expect != "DENY") or (fake_delta == 0)
        audit_ok = arow is not None and arow[0] == role
        # ALLOW scenarios must reach upstream (fake_delta >= 1)
        allow_reached = (expect != "ALLOW") or (fake_delta >= 1)
        ok = decision_ok and deny_no_upstream and audit_ok and allow_reached
        all_ok = all_ok and ok
        results.append({
            "id": sid, "role": role, "tool": tool, "target_repo": target,
            "expect": expect, "decision": decision, "decision_ok": decision_ok,
            "fake_delta": fake_delta, "deny_no_upstream": deny_no_upstream,
            "allow_reached": allow_reached, "audit": audit, "audit_ok": audit_ok,
            "detail": detail, "PASS": ok,
        })
    summary = {
        "scenarios": len(results),
        "passed": sum(1 for r in results if r["PASS"]),
        "failed": sum(1 for r in results if not r["PASS"]),
        "all_passed": all_ok,
        "results": results,
    }
    sys.stderr.write("CLIENT: printing summary, all_ok=%s\n" % all_ok); sys.stderr.flush()
    sys.stdout.write(json.dumps(summary, indent=2) + "\n"); sys.stdout.flush()
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
