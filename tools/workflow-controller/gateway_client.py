#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gateway_client.py — Controller → Policy Gateway MCP client(B4c)。

经 coordinator Bearer token 调 Gateway /coordinator/sse 的 github-mcp 工具。
- 身份 = Authorization Bearer(env COORDINATOR_TOKEN,非 PAT)。
- token/url 从 env 读,Gateway 侧 path/token 一致性校验仍生效(gateway.py handle_sse)。
- asyncio.run 桥到 controller 的 sync 主循环(每次调用新 event loop;L2 调用低频,可接受)。

错误分类(复审 #4,Gateway/网络错误 ≠ "0 PR"):
  网络/认证/超时/schema 解析失败 → GatewayError → 调用方归 RETRY(不累加 0-PR 计数)。
  查询成功但 0 条 → NOT_FOUND(累加,达阈值 HOLD)。
  >1 条 → AMBIGUOUS(立即 HOLD)。
  恰好 1 条 → FOUND。

绑定来源 head_sha 一律 pull_request_read 读回(GitHub 权威),不信任 LLM/调用方自报。
"""
import os
import json
import asyncio

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://policy-gw:8083").rstrip("/")
COORDINATOR_TOKEN = os.environ.get("COORDINATOR_TOKEN", "")


class GatewayError(Exception):
    """Gateway 不可达 / 认证失败 / 超时 / 上游 is_error。调用方应归 RETRY(不混为 0 PR)。"""


async def _lifecycle(tool, args):
    """完整 MCP 生命周期:SSE 连接 → ClientSession → initialize → call_tool。
    mcp SDK 懒导入(controller L2 关闭时不强依赖)。"""
    from mcp.client.sse import sse_client
    from mcp import ClientSession
    async with sse_client(f"{GATEWAY_URL}/coordinator/sse",
                          headers={"Authorization": f"Bearer {COORDINATOR_TOKEN}"}) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            return await s.call_tool(tool, args or {})


async def _call_tool(tool, args, timeout):
    """总超时包住整个生命周期(B4c-0.1 加固):SSE 连接 + initialize + call_tool。
    超时 → asyncio.TimeoutError → async with 退出清理 → GatewayError。"""
    return await asyncio.wait_for(_lifecycle(tool, args), timeout=timeout)


def _result_text(res):
    try:
        return "\n".join(c.text for c in (res.content or []) if hasattr(c, "text"))
    except Exception:
        return ""


def gateway_call(tool, args, timeout=60):
    """调一个 Gateway 工具。返回 (text, is_error)。失败抛 GatewayError。"""
    if not COORDINATOR_TOKEN:
        raise GatewayError("COORDINATOR_TOKEN 未配置")
    try:
        res = asyncio.run(_call_tool(tool, args or {}, timeout))
    except Exception as e:
        raise GatewayError(f"{type(e).__name__}: {e}")
    text = _result_text(res)
    is_err = bool(getattr(res, "is_error", False))
    if is_err:
        raise GatewayError(f"tool={tool} 返回 is_error: {text[:200]}")
    return text, is_err


def gateway_list_prs(owner, repo, run_id, timeout=60):
    """分页 list_pull_requests(state=open) + 本地过滤 head.ref STARTS WITH 'fix/<run_id>-'。
    返回 (status, prs):FOUND(1 条)/ NOT_FOUND(0)/ AMBIGUOUS(>1)/ RETRY(网络/认证/解析失败)。
    复审 #1:list_pull_requests 不支持 head 通配 → 分页 + 本地过滤。
    B4c-0.1 加固:malformed item 或第 10 页仍满(扫描不完整)→ RETRY,不误判 NOT_FOUND。"""
    prefix = f"fix/{run_id}-"
    matched = []
    page = 1
    while page <= 10:
        try:
            text, _ = gateway_call("list_pull_requests",
                {"owner": owner, "repo": repo, "state": "open", "perPage": 100, "page": page}, timeout)
        except GatewayError:
            return ("RETRY", [])
        try:
            items = json.loads(text)
        except Exception:
            return ("RETRY", [])  # schema 解析失败 → 重试
        if isinstance(items, dict):
            items = items.get("items") or ([items] if items.get("number") else [])
        if not isinstance(items, list) or not items:
            break
        for pr in items:
            if not isinstance(pr, dict):
                return ("RETRY", [])  # malformed item → 响应形态异常,重试(不误判 0 PR)
            head = pr.get("head") or {}
            ref = head.get("ref") or pr.get("headRefName") or ""
            if ref.startswith(prefix):
                matched.append(pr)
        if len(items) < 100:
            break
        page += 1
    else:
        # while 正常退出(page>10)且最后一页仍满 → 扫描不完整 → RETRY
        return ("RETRY", [])
    if len(matched) == 0:
        return ("NOT_FOUND", [])
    if len(matched) > 1:
        return ("AMBIGUOUS", matched)
    return ("FOUND", matched)


def gateway_read_pr(owner, repo, pr_num, timeout=60):
    """pull_request_read(method=get) → 权威 PR 态。返回 dict 或 None(失败)。
    head_sha 来自 GitHub,用于 TOCTOU 比对 + 绑定记录。merged/state/base 用于对账。"""
    try:
        text, _ = gateway_call("pull_request_read",
            {"method": "get", "owner": owner, "repo": repo, "pullNumber": int(pr_num)}, timeout)
    except (GatewayError, ValueError, TypeError):
        return None
    try:
        d = json.loads(text)
    except Exception:
        return None
    head = d.get("head") or {}
    base = d.get("base") or {}
    return {
        "head_sha": head.get("sha") or d.get("headSha"),
        "state": d.get("state"),
        "base": base.get("ref") or d.get("baseRef"),
        "merged": bool(d.get("merged") or d.get("isMerged")),
        "merge_commit_sha": d.get("mergeCommitSha") or (d.get("merge_commit") or {}).get("sha"),
        "pr_number": d.get("number") or d.get("pullNumber") or int(pr_num),
    }


def canonical_args_hash(payload):
    """与 gateway.py canonical_args_hash 完全一致的序列化(排除 approval_ticket;64hex)。
    Controller 建票时用此算 args_hash,Gateway 运行时再算一次比对。"""
    import hashlib
    clean = {k: v for k, v in payload.items() if k != "approval_ticket"}
    return hashlib.sha256(
        json.dumps(clean, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
