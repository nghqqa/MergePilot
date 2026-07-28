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
import re
import time
import asyncio

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://policy-gw:8083").rstrip("/")
COORDINATOR_TOKEN = os.environ.get("COORDINATOR_TOKEN", "")


class GatewayError(Exception):
    """Gateway 不可达 / 认证失败 / 超时 / 上游 is_error。调用方应归 RETRY(不混为 0 PR)。"""


# ── B4c.1 typed outcome:Controller 按类型决定收敛(终结/退避/降级),不再解析字符串 ──
class GatewayUnavailable(GatewayError):
    """瞬时:网络/超时/5xx/L2_DB_UNAVAILABLE。调用方退避重试,**不终结票据**。"""


class GatewayDenied(GatewayError):
    """票据级**确定性**拒绝(claim 前):CLAIM_MISMATCH/REPO_NOT_ALLOWED/L2_TICKET_REQUIRED/INVALID_ACTION。
    调用方应 l2_reject_approved → approval FAILED / task HOLD。"""
    def __init__(self, reason_code, detail=""):
        self.reason_code = reason_code
        self.detail = detail
        super().__init__(f"denied:{reason_code} {detail}")


class GatewayGlobalDegraded(GatewayError):
    """全局配置故障:BAD_TOKEN/ROLE_PATH_MISMATCH/TOOL_NOT_ALLOWED/L2_REQUIRES_COORDINATOR。
    调用方应进入 degraded(本 tick 不再逐张消费票据)。"""
    def __init__(self, reason_code, detail=""):
        self.reason_code = reason_code
        self.detail = detail
        super().__init__(f"global_degraded:{reason_code} {detail}")


_TICKET_DENY_REASONS = {"CLAIM_MISMATCH", "REPO_NOT_ALLOWED", "L2_TICKET_REQUIRED", "INVALID_ACTION"}
_GLOBAL_DEGRADED_REASONS = {"BAD_TOKEN", "ROLE_PATH_MISMATCH", "TOOL_NOT_ALLOWED", "L2_REQUIRES_COORDINATOR"}
_TRANSIENT_REASONS = {"L2_DB_UNAVAILABLE"}


def _classify_error_text(text):
    """从 Gateway is_error 文本(如 'POLICY_DENIED reason_code=CLAIM_MISMATCH …')解析 reason_code,
    返回 (exc_class, reason_code)。未知/无 reason → (GatewayUnavailable, '')(保守瞬时,不终结票据)。
    B4c.1.1:reason 含数字(如 L2_TICKET_REQUIRED/L2_DB_UNAVAILABLE),正则须 [A-Z0-9_]+(旧 [A-Z_]+ 会截成 'L')。"""
    m = re.search(r"reason_code=([A-Z0-9_]+)", text or "")
    rc = m.group(1) if m else ""
    if rc in _TICKET_DENY_REASONS:
        return GatewayDenied, rc
    if rc in _GLOBAL_DEGRADED_REASONS:
        return GatewayGlobalDegraded, rc
    return GatewayUnavailable, rc   # 含 _TRANSIENT_REASONS(L2_DB_UNAVAILABLE)+ 未知/无 reason



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
    """调一个 Gateway 工具。返回 (text, is_error)。失败抛 B4c.1 typed 异常:
    GatewayDenied(票据级确定性拒绝)/ GatewayUnavailable(瞬时)/ GatewayGlobalDegraded(全局配置故障)。
    (三者皆 GatewayError 子类,旧 `except GatewayError` 调用方仍兼容。discovery 仍一律归 RETRY。)"""
    if not COORDINATOR_TOKEN:
        raise GatewayGlobalDegraded("BAD_TOKEN", "COORDINATOR_TOKEN 未配置")
    try:
        res = asyncio.run(_call_tool(tool, args or {}, timeout))
    except asyncio.TimeoutError:
        raise GatewayUnavailable(f"timeout: tool={tool}")
    except GatewayError:
        raise
    except Exception as e:
        raise GatewayUnavailable(f"{type(e).__name__}: {e}")   # 网络/连接/SSE → 瞬时
    text = _result_text(res)
    is_err = bool(getattr(res, "is_error", False))
    if is_err:
        exc_cls, rc = _classify_error_text(text)
        detail = f"tool={tool} {text[:160]}"
        if exc_cls is GatewayUnavailable:
            raise GatewayUnavailable(detail)
        raise exc_cls(rc, detail)   # GatewayDenied / GatewayGlobalDegraded
    return text, is_err


def gateway_list_prs(owner, repo, run_id, timeout=60, deadline=None):
    """分页 list_pull_requests(state=open) + 本地过滤 head.ref STARTS WITH 'fix/<run_id>-'。
    返回 (status, prs):FOUND(1 条,每条已严格校验 head dict + ref str + number int 非 bool)/
       NOT_FOUND(查询成功且确为 0)/ AMBIGUOUS(>1)/ RETRY(网络/认证/schema 异常/缺关键字段)。
    B4c-1.1 P1-1:schema 异常(non-list / head 非 dict / ref 非 str / number 非 int 或为 bool)
       → RETRY,**绝不误判 NOT_FOUND**(否则会错累计 l2_discovery_attempts 把任务 HOLD)。
    B4c.1.1 #4:deadline(monotonic 绝对)→ 分页共享预算,每页超时 = min(timeout, 剩余),到期返回 RETRY。"""
    prefix = f"fix/{run_id}-"
    matched = []
    page = 1
    while page <= 10:
        if deadline and time.monotonic() > deadline:
            return ("RETRY", [])   # B4c.1.1 #4:整轮预算到期,分页中止
        _to = min(timeout, max(deadline - time.monotonic(), 0.1)) if deadline else timeout   # B4c.1.3:无 5s 下限(≤剩余)
        # B4c.1.2:GatewayUnavailable/Denied/GlobalDegraded **透传**(由 discover 开 breaker / HOLD;不再吞成 RETRY)
        text, _ = gateway_call("list_pull_requests",
            {"owner": owner, "repo": repo, "state": "open", "perPage": 100, "page": page}, _to)
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


def gateway_read_branch(owner, repo, branch, timeout=60, deadline=None):
    """list_branches 分页 + 过滤精确 branch → (status, sha)。第二条权威 SHA 来源(B4c-1.1 P1-4):
    pull_request_read.head.sha 与 branch ref sha 必须一致才写 binding(防 PR-head 缓存导致 SHA 固化错)。
    status:OK(找到,返回 sha)/ NOT_FOUND(branch 不在列表)/ RETRY(网络/解析/branch 存在但 sha 缺失)。
    B4c.1.2 #3:deadline(monotonic)→ 分页共享预算,每页超时 = min(timeout, 剩余),到期 RETRY。"""
    page = 1
    while page <= 10:
        if deadline and time.monotonic() > deadline:
            return ("RETRY", None)   # B4c.1.2 #3:整轮预算到期,分页中止
        _to = min(timeout, max(deadline - time.monotonic(), 0.1)) if deadline else timeout
        try:
            text, _ = gateway_call("list_branches",
                {"owner": owner, "repo": repo, "perPage": 100, "page": page}, _to)
        except (ValueError, TypeError):   # B4c.1.2:schema 异常 → RETRY;Unavailable/Denied/GlobalDegraded 透传
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
    except (ValueError, TypeError):   # B4c.1.2:schema 异常 → RETRY;Unavailable/Denied/GlobalDegraded 透传
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
