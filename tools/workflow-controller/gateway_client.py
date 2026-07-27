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
    返回 (status, prs):FOUND(1 条,每条已严格校验 head dict + ref str + number int 非 bool)/
       NOT_FOUND(查询成功且确为 0)/ AMBIGUOUS(>1)/ RETRY(网络/认证/schema 异常/缺关键字段)。
    B4c-1.1 P1-1:schema 异常(non-list / head 非 dict / ref 非 str / number 非 int 或为 bool)
       → RETRY,**绝不误判 NOT_FOUND**(否则会错累计 l2_discovery_attempts 把任务 HOLD)。"""
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
            return ("RETRY", [])
        if not isinstance(items, list):
            return ("RETRY", [])   # 非 list(单对象/包装均不猜测)→ schema 异常 → RETRY
        if not items:
            break
        for pr in items:
            if not isinstance(pr, dict):
                return ("RETRY", [])
            head = pr.get("head")
            if not isinstance(head, dict):
                return ("RETRY", [])   # head 非 dict → RETRY
            ref = head.get("ref")
            if not isinstance(ref, str):
                return ("RETRY", [])   # ref 非 str(None/非 str)→ RETRY,不靠 fallback
            num = pr.get("number")
            if isinstance(num, bool) or not isinstance(num, int):
                return ("RETRY", [])   # number 必须是 int 且非 bool(True 是 int 子类,排除)
            if ref.startswith(prefix):
                matched.append(pr)
        if len(items) < 100:
            break
        page += 1
    else:
        return ("RETRY", [])   # 第10页仍满 → 扫描不完整 → RETRY
    if len(matched) == 0:
        return ("NOT_FOUND", [])
    if len(matched) > 1:
        return ("AMBIGUOUS", matched)
    return ("FOUND", matched)


def gateway_read_branch(owner, repo, branch, timeout=60):
    """list_branches 分页 + 过滤精确 branch → (status, sha)。第二条权威 SHA 来源(B4c-1.1 P1-4):
    pull_request_read.head.sha 与 branch ref sha 必须一致才写 binding(防 PR-head 缓存导致 SHA 固化错)。
    status:OK(找到,返回 sha)/ NOT_FOUND(branch 不在列表)/ RETRY(网络/解析/branch 存在但 sha 缺失)。"""
    page = 1
    while page <= 10:
        try:
            text, _ = gateway_call("list_branches",
                {"owner": owner, "repo": repo, "perPage": 100, "page": page}, timeout)
        except GatewayError:
            return ("RETRY", None)
        try:
            items = json.loads(text)
        except Exception:
            return ("RETRY", None)
        if isinstance(items, dict) and isinstance(items.get("branches"), list):
            items = items["branches"]
        if not isinstance(items, list):
            return ("RETRY", None)
        for b in items:
            if isinstance(b, dict) and b.get("name") == branch:
                sha = b.get("sha")
                if _is_sha40(sha):
                    return ("OK", sha)
                return ("RETRY", None)   # branch 存在但 sha 非完整 40hex → RETRY
        if len(items) < 100:
            break
        page += 1
    # PR 存在(上游 FOUND)但 branch 未在列表 → 扫描不完整/瞬态 → RETRY(不判 NOT_FOUND)
    return ("RETRY", None)


def _parse_bool(v):
    """严格解析布尔(B4c-1:修 bool("false")→True bug)。
    bool 直接返回;数字 0/1 → False/True;字符串 true/false/1/0(忽略大小写)→ 对应值;
    其他(None/未知串/对象)→ None(调用方决定默认,不臆断 True)。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)) and not isinstance(v, bool) and v in (0, 1):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "1"):
            return True
        if s in ("false", "0", ""):
            return False
    return None


def _is_sha40(s):
    """完整 Git object SHA:^[[0-9a-f]]{40}$。绑定 + 票据固化都要求完整 40hex,不接受短 SHA。"""
    return isinstance(s, str) and len(s) == 40 and all(c in "0123456789abcdef" for c in s)


def gateway_read_pr(owner, repo, pr_num, timeout=60):
    """pull_request_read(method=get) → (status, pr_dict)。status ∈ {'OK','RETRY'}(无 NOT_FOUND)。
    B4c-1.2 P1-2 全字段严格(不补默认值;缺/伪造 → RETRY,绝不假一致):
      head_sha 必须 40hex;PR number 必须来自响应(int 且非 bool,**不 fallback 到请求参数**);
      state/head_ref/head_repo_full_name/base_ref/merged 必须存在且类型正确(merged 不默认 false,缺失→RETRY);
      head_repo 缺失不接受(防 fork PR 绕过)。"""
    try:
        text, _ = gateway_call("pull_request_read",
            {"method": "get", "owner": owner, "repo": repo, "pullNumber": int(pr_num)}, timeout)
    except (GatewayError, ValueError, TypeError):
        return ("RETRY", None)
    try:
        d = json.loads(text)
    except Exception:
        return ("RETRY", None)
    if not isinstance(d, dict):
        return ("RETRY", None)
    head = d.get("head")
    base = d.get("base")
    if not isinstance(head, dict) or not isinstance(base, dict):
        return ("RETRY", None)
    head_sha = head.get("sha")
    if not _is_sha40(head_sha):
        return ("RETRY", None)   # head_sha 必须完整 40hex
    state = d.get("state")
    if not isinstance(state, str) or not state:
        return ("RETRY", None)   # state 必须存在(str)
    base_ref = base.get("ref")
    if not isinstance(base_ref, str) or not base_ref:
        return ("RETRY", None)   # base_ref 必须存在
    head_ref = head.get("ref")
    if not isinstance(head_ref, str) or not head_ref:
        return ("RETRY", None)   # head_ref 必须存在
    head_repo = head.get("repo")
    if not isinstance(head_repo, dict):
        return ("RETRY", None)   # head.repo 必须存在(dict)
    head_repo_full_name = head_repo.get("full_name")
    if not isinstance(head_repo_full_name, str) or not head_repo_full_name:
        return ("RETRY", None)   # head.repo.full_name 必须存在(防 fork PR 绕过)
    merged = _parse_bool(d.get("merged"))
    if merged is None:
        merged = _parse_bool(d.get("isMerged"))
    if merged is None:
        return ("RETRY", None)   # merged 必须明确 bool(不默认 false,B4c-4 对账依赖)
    pr_number = d.get("number")
    if isinstance(pr_number, bool) or not isinstance(pr_number, int):
        return ("RETRY", None)   # number 必须来自响应(int 非 bool),不 fallback 到请求参数
    merge_commit = d.get("merge_commit")
    mcs = d.get("mergeCommitSha")
    if not isinstance(mcs, str) and isinstance(merge_commit, dict):
        mcs = merge_commit.get("sha")
    return ("OK", {
        "head_sha": head_sha,
        "state": state,
        "base": base_ref,
        "merged": merged,
        "merge_commit_sha": mcs if isinstance(mcs, str) else None,
        "pr_number": pr_number,
        "head_ref": head_ref,
        "head_repo_full_name": head_repo_full_name,
    })


def canonical_args_hash(payload):
    """与 gateway.py canonical_args_hash 完全一致的序列化(排除 approval_ticket;64hex)。
    Controller 建票时用此算 args_hash,Gateway 运行时再算一次比对。"""
    import hashlib
    clean = {k: v for k, v in payload.items() if k != "approval_ticket"}
    return hashlib.sha256(
        json.dumps(clean, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
