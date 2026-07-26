#!/usr/bin/env python3
"""
Policy Gateway — MergePilot M3-B
角色 token 认证的 MCP 前置网关,fronting github-mcp bridge。

威胁模型与定位:
  - Gateway 只在内部 docker 网络暴露给 worker/controller,不对公网。
  - 真实 GitHub PAT 只在 github-mcp 容器(后端私有网络),worker 碰不到。
  - 调用者身份 = Authorization Bearer token(env 配的 token→role 映射),不是 URL 路径。
    路径 /{role}/sse 只声明意图,必须与 token 角色一致,不一致直接 401。
  - fail-closed:token 无效/缺失 → 401;路径与 token 不符 → 401。

B1 范围(本文件):
  - 关闭直连 bridge 的旁路(配合网络隔离:bridge 移到私有 mcp-backend-net)
  - 角色 Bearer token 认证 + path/token 一致性
  - 不可变审计:每次 list/call/auth-fail 都写 audit-pg.mcp_calls
  - 策略 B1_PERMISSIVE:认证通过即放行上游全部工具
    (B2 接 policy.yaml 做 deny-by-default 过滤;B4 加 L2 审批票据校验)

接线:
  mcporter --header Authorization=Bearer <role-token>
    → http://policy-gw:8083/<role>/sse   (GET,SSE)
    → POST /messages/?session_id=...
  gateway 内部 → sse_client(UPSTREAM_URL=github-mcp:8082/sse) → 持 PAT 的 bridge → GitHub
"""
import os
import sys
import json
import asyncio
import hashlib
import re
import uuid
import fnmatch
import contextvars
from contextlib import asynccontextmanager

import yaml  # PyYAML

# ───────────────────────── 配置 ─────────────────────────
ROLE_TOKENS = json.loads(os.environ.get("ROLE_TOKENS", "{}"))   # {"reviewer":"tok","fixer":"tok","verifier":"tok","coordinator":"tok"}
UPSTREAM_URL = os.environ.get("UPSTREAM_URL", "http://github-mcp:8082/sse")
AUDIT_DSN = os.environ.get("AUDIT_DSN", "")                     # postgresql://policy_gateway_audit:pw@audit-pg:5432/mergepilot_audit
L2_DSN = os.environ.get("L2_DSN", "")                           # postgresql://policy_gateway_l2:pw@audit-pg:5432/mergepilot_audit
LISTEN_HOST = os.environ.get("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8083"))

# ───────────────────────── 策略(policy.yaml,deny-by-default)─────────────────────────
POLICY_FILE = os.environ.get("POLICY_FILE", "/app/policy.yaml")
with open(POLICY_FILE, "r", encoding="utf-8") as _pf:
    POLICY_TEXT = _pf.read()
POLICY = yaml.safe_load(POLICY_TEXT)
POLICY_VERSION = str(POLICY.get("version", "unknown"))
POLICY_HASH = hashlib.sha256(POLICY_TEXT.encode("utf-8")).hexdigest()[:16]

_TOOL_CLASSES = POLICY.get("tool_classes", {})      # {class: [tool_names]}
_L2_SET = set(_TOOL_CLASSES.get("l2", []))
_FIX_SET = set(_TOOL_CLASSES.get("fix", []))
# 写工具集(comment/fix/l2/update_pull_request):B3 起审计 fail-closed(INTENT 必须先持久化才调 GitHub)
_WRITE_SET = (set(_TOOL_CLASSES.get("comment", [])) | _FIX_SET | _L2_SET | {"update_pull_request"})
# search_scoped:允许但 query 必须含 repo:<allowlist>(防跨仓库搜索)
_SEARCH_SCOPED = {"search_code", "search_commits", "search_issues", "search_pull_requests"}

# 展开 role → 允许工具集合(classes + extra_tools)+ 约束标志
ROLES_CFG = {}
for _role, _cfg in POLICY.get("roles", {}).items():
    _allowed = set()
    for _cls in _cfg.get("classes", []):
        _allowed.update(_TOOL_CLASSES.get(_cls, []))
    _allowed.update(_cfg.get("extra_tools", []))   # update_pull_request 等混合风险工具
    ROLES_CFG[_role] = {
        "allowed": _allowed,
        "write_checks": bool(_cfg.get("write_checks", False)),
        "l2_requires_ticket": bool(_cfg.get("l2_requires_ticket", False)),
    }

_GLOBAL_REPOS = set(POLICY.get("repos", {}).get("allowlist", []))
_BASE_ALLOW = set(POLICY.get("branches", {}).get("base_allowlist", []))
_FIX_PREFIX = POLICY.get("branches", {}).get("fix_prefix", "fix/")
_PROTECTED = set(POLICY.get("branches", {}).get("protected", []))
_PATH_DENY = POLICY.get("file_paths", {}).get("denylist", [])

TOKEN_TO_ROLE = {tok: role for role, tok in ROLE_TOKENS.items()}

# PR 更新字段白名单(update_pull_request 混合风险工具,按角色分级)
_PR_IDENTITY = {"owner", "repo", "pullNumber"}
_FIXER_PR_FIELDS = {"title", "body"}   # fixer 只能改 title/body


def _check_search_query(query: str):
    """search 工具的 query 安全校验。返回 None=通过,或 reason_code。
    不信任调用者提供的 scope:GitHub 支持 OR/NOT/括号 + repo:/org:/user: 限定符,
    合法 repo: 的存在不能保证整表达式受约束(repo:allowed OR password 会逃逸)。
    所以只允许纯术语:禁止任何限定符(含冒号)、括号、布尔算子;scope 由 gateway 注入。"""
    ql = (query or "").strip()
    if not ql:
        return None  # 空 query 合法,gateway 会注入 repo scope
    if ":" in ql or "(" in ql or ")" in ql:
        return "SEARCH_QUALIFIER_FORBIDDEN"   # 含限定符(任何 word:)或括号
    if re.search(r"\b(OR|NOT|AND)\b", ql, re.IGNORECASE):
        return "SEARCH_OPERATOR_NOT_ALLOWED"
    return None


def _inject_search_scope(query: str) -> str:
    """gateway 自己追加可信 repo scope(单仓库 allowlist)。
    多仓库场景需显式策略,当前单仓库直接注入。"""
    ql = (query or "").strip()
    if len(_GLOBAL_REPOS) == 1:
        return f"{ql} repo:{next(iter(_GLOBAL_REPOS))}".strip()
    # 多仓库:不注入(调用方需显式策略),返回原 query —— 此分支当前不应触发
    return ql


def _path_denied(path: str) -> bool:
    """denylist 匹配:支持 glob:**/x(basename + 子串)和普通 fnmatch。"""
    if not path:
        return False
    name = path.rsplit("/", 1)[-1]
    for raw in _PATH_DENY:
        pat = raw[len("glob:"):].strip() if raw.startswith("glob:") else raw
        if pat.startswith("**/"):
            tail = pat[3:]
            if fnmatch.fnmatch(name, tail) or tail.rstrip("*") in path:
                return True
        else:
            if fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(name, pat):
                return True
    return False


def _check_write_args(name: str, args: dict) -> str:
    """fixer 写操作的 arg 校验。返回 None=通过,否则返回 reason_code。
    注意:repo allowlist 和 update_pull_request 字段白名单已在 call_tool 全局/特殊处理,
    此处只做分支/路径约束。"""
    branch = args.get("branch") or args.get("head")
    base = args.get("base") or args.get("from_branch")   # B2.1:真实参数是 from_branch(此前误用 from)
    if name == "create_branch":
        if branch and not branch.startswith(_FIX_PREFIX):
            return "BRANCH_NOT_FIX_PREFIX"
        if base and base not in _BASE_ALLOW:
            return "BASE_NOT_ALLOWED"
    elif name in ("create_or_update_file", "push_files", "update_pull_request_branch", "delete_file"):
        if branch:
            if branch in _PROTECTED:
                return "BRANCH_PROTECTED"
            if not branch.startswith(_FIX_PREFIX):
                return "BRANCH_NOT_FIX_PREFIX"
        paths = []
        if args.get("path"):
            paths.append(args.get("path"))
        for f in (args.get("files") or []):
            if isinstance(f, dict) and f.get("path"):
                paths.append(f.get("path"))
        for p in paths:
            if _path_denied(p):
                return "PATH_DENIED"
    elif name == "create_pull_request":
        if base and base not in _BASE_ALLOW:
            return "BASE_NOT_ALLOWED"
        if branch and not branch.startswith(_FIX_PREFIX):
            return "HEAD_NOT_FIX_BRANCH"
    return None


# ───────────────────────── 每连接角色上下文 ─────────────────────────
# connect_sse → server.run 是同一个 task,handler 在该 task 内执行,contextvar 可见。
current_role: contextvars.ContextVar[str] = contextvars.ContextVar("current_role", default="")

# ───────────────────────── 审计(不可变)─────────────────────────
import psycopg2  # noqa: E402

# 注意:连接缓存变量名绝不能与下面的函数名相同(否则 def 会把变量重绑成函数对象,
# 函数内 `if var is None` 永远 False,永不建连,audit 静默失败)。早期版本踩过此坑。
_audit_db = None


def _get_audit_conn():
    global _audit_db
    if not AUDIT_DSN:
        return None
    if _audit_db is None or _audit_db.closed:
        _audit_db = psycopg2.connect(AUDIT_DSN, connect_timeout=3)
        _audit_db.autocommit = True
    return _audit_db


def audit_event(corr_id, phase, caller, tool, decision, reason_code="",
                *, args_hash="", ticket_id=None, execution_id=None, target_repo="", target_branch="",
                result_status="", http_status=None, git_sha="", run_id="", error=""):
    """追加一条不可变审计行。返回 True=已持久化,False=未持久化。
    B3:INTENT→RESULT/ERROR 共享 correlation_id;B4b:带 ticket_id+execution_id。"""
    if not AUDIT_DSN:
        return False
    rid = str(uuid.uuid4())
    try:
        conn = _get_audit_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO mcp_calls
                  (request_id, correlation_id, phase, ts, caller_agent, tool, decision, reason_code,
                   policy_version, policy_hash, ticket_id, execution_id, args_hash,
                   target_repo, target_branch, result_status, http_status, git_sha, run_id, error)
                VALUES (%s, %s, %s, now(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (rid, corr_id, phase, caller, tool, decision, reason_code,
                 POLICY_VERSION, POLICY_HASH, ticket_id, execution_id, args_hash,
                 target_repo, target_branch, result_status, http_status, git_sha, run_id, error),
            )
        return True
    except Exception as e:
        print(f"[gateway] audit FAILED [{phase} {decision} {tool}]: {e}", file=sys.stderr, flush=True)
        return False


# ───────────────────────── L2 票据连接(B4b)─────────────────────────
_l2_db = None


def _get_l2_conn():
    global _l2_db
    if not L2_DSN:
        return None
    if _l2_db is None or _l2_db.closed:
        _l2_db = psycopg2.connect(L2_DSN, connect_timeout=3)
        _l2_db.autocommit = True
    return _l2_db


def l2_claim_ticket(ticket_id, action, repo, pr_number, args_hash):
    """调用 SECURITY DEFINER l2_claim_ticket(一次 CAS 全校验)。
    返回 (status, claim_dict)。
    status: 'CLAIMED'(成功)/ 'MISMATCH'(0 行,票不匹配)/ 'DB_ERROR'(连接/查询失败)。
    B4b P1#2:区分 DB 不可用 vs 票不匹配(不混为 CLAIM_MISMATCH)。"""
    try:
        conn = _get_l2_conn()
        if not conn:
            return ("DB_ERROR", None)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT execution_id, canonical_payload::text, expected_head_sha, target_branch "
                "FROM l2_claim_ticket(%s, %s, %s, %s, %s)",
                (ticket_id, action, repo, pr_number, args_hash))
            row = cur.fetchone()
            if row and row[0]:
                return ("CLAIMED", {"execution_id": str(row[0]),
                                    "canonical_payload": json.loads(row[1]),
                                    "expected_head_sha": row[2],
                                    "target_branch": row[3]})
        return ("MISMATCH", None)
    except Exception as e:
        print(f"[gateway] l2_claim_ticket FAILED: {e}", file=sys.stderr, flush=True)
        return ("DB_ERROR", None)


def l2_call_func(fn, args):
    """调用一个无返回值的 l2_* 函数(complete/fail/mark_unknown)。返回 True=成功。"""
    conn = _get_l2_conn()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {fn}", args)
            return cur.fetchone()[0]
    except Exception as e:
        print(f"[gateway] l2_{fn} FAILED: {e}", file=sys.stderr, flush=True)
        return False


def canonical_args_hash(args):
    """与 Controller 完全一致的 canonical hash(Python sort_keys+紧凑,64hex)。
    **排除 approval_ticket**(它是 gateway 验证参数,不属于上游载荷)。"""
    clean = {k: v for k, v in args.items() if k != "approval_ticket"}
    return hashlib.sha256(
        json.dumps(clean, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def l2_exec(sql, args):
    """执行一个 l2_* 函数(sql 形如 'SELECT l2_complete_ticket(%s,%s::uuid,%s)')。返回首列。"""
    conn = _get_l2_conn()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            row = cur.fetchone()
            return row[0] if row else False
    except Exception as e:
        print(f"[gateway] l2_exec FAILED [{sql[:50]}]: {e}", file=sys.stderr, flush=True)
        return False


def _derive_l2_action(name, args):
    """从工具名 + 参数推导 L2 action。merge_pull_request→merge;update_pull_request(state)→close。"""
    if name == "merge_pull_request":
        return "merge"
    if name == "update_pull_request" and "state" in args:
        return "close"
    return None


def _extract_text(result):
    """从 CallToolResult 提取文本内容(拼接所有 text content)。"""
    try:
        parts = [c.text for c in (result.content or []) if hasattr(c, "text")]
        return "\n".join(parts)
    except Exception:
        return ""


def _extract_sha(result):
    """从结果文本提取 merge commit SHA(如有)。"""
    m = re.search(r'"sha"\s*:\s*"([0-9a-f]{7,40})"', _extract_text(result))
    return m.group(1) if m else ""


async def _read_pr_upstream(owner, repo_name, pr_num):
    """查 GitHub PR 实际态(head_sha/state/base),用于 L2 TOCTOU 校验。
    经上游 pull_request_read(method=get)—— GitHub 权威态,不信任调用方。"""
    try:
        res = await upstream.call_tool(
            "pull_request_read",
            {"method": "get", "owner": owner, "repo": repo_name, "pullNumber": int(pr_num)})
        d = json.loads(_extract_text(res))
        head = d.get("head") or {}
        base = d.get("base") or {}
        return {
            "head_sha": head.get("sha") or d.get("headSha"),
            "state": d.get("state"),
            "base": base.get("ref") or d.get("baseRef"),
        }
    except Exception as e:
        print(f"[gateway] TOCTOU read_pr failed: {e}", file=sys.stderr, flush=True)
        return None


# 旧名兼容(handle_sse 的 _deny 还在用)
def audit(caller, tool, decision, reason_code="", **kw):
    return audit_event(str(uuid.uuid4()), "INTENT", caller, tool, decision, reason_code, **kw)


# ───────────────────────── MCP server(proxy 语义)─────────────────────────
from mcp.server import Server  # noqa: E402
from mcp.server.sse import SseServerTransport  # noqa: E402
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402
from mcp import ClientSession  # noqa: E402
from mcp.client.sse import sse_client  # noqa: E402
from mcp.types import CallToolResult, TextContent  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.routing import Route, Mount  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse, Response  # noqa: E402
import uvicorn  # noqa: E402

server = Server("policy-gateway")
upstream: ClientSession | None = None
upstream_tools: list = []  # 缓存上游 Tool 列表


def _deny_result(reason_code: str, **extra) -> CallToolResult:
    """工具级拒绝:返回结构化 MCP 错误(is_error=True),不是 HTTP 403。"""
    msg = f"POLICY_DENIED reason_code={reason_code}"
    for k, v in extra.items():
        msg += f" {k}={v}"
    return CallToolResult(content=[TextContent(type="text", text=msg)], is_error=True)


@server.list_tools()
async def list_tools():
    """deny-by-default:只返回当前角色 allow 集合内的工具;其余上游工具不可见。"""
    role = current_role.get()
    cfg = ROLES_CFG.get(role)
    allowed = cfg["allowed"] if cfg else set()
    filtered = [t for t in upstream_tools if t.name in allowed]
    audit(role, "(list_tools)", "ALLOW", "B2_FILTERED_LIST")
    print(f"[gateway] list_tools role={role} → {len(filtered)}/{len(upstream_tools)} tools "
          f"(policy={POLICY_VERSION})", flush=True)
    return filtered


@server.call_tool()
async def call_tool(name: str, arguments: dict | None):
    role = current_role.get()
    cfg = ROLES_CFG.get(role)
    args = arguments or {}
    owner = args.get("owner")
    repo = f"{owner}/{args.get('repo')}" if owner and args.get("repo") else ""
    branch = str(args.get("branch") or args.get("head") or args.get("base") or args.get("from_branch") or "")
    args_hash = hashlib.sha256(
        json.dumps(args, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]

    corr_id = str(uuid.uuid4())  # B3:一次调用的 INTENT/RESULT/ERROR 共享同一 id
    audit_kw = dict(args_hash=args_hash, target_repo=repo, target_branch=branch)

    def deny(reason_code, **extra):
        # 策略 DENY:尽力记 INTENT(fail-open,审计挂也不放行被拒调用)
        audit_event(corr_id, "INTENT", role, name, "DENY", reason_code, **audit_kw)
        print(f"[gateway] DENY role={role} tool={name} → {reason_code}", flush=True)
        return _deny_result(reason_code, tool=name, **extra)

    # 1. 工具是否在角色 allow 集合内(deny-by-default;disabled 类的工具对任何角色都不可见)
    if not cfg or name not in cfg["allowed"]:
        return deny("TOOL_NOT_ALLOWED")

    # 2. 全局 repo allowlist(所有角色,所有带 owner+repo 的工具,含读)
    if owner and args.get("repo") and repo not in _GLOBAL_REPOS:
        return deny("REPO_NOT_ALLOWED", repo=repo)

    # 3. search_scoped:不信任调用者 scope —— 拒限定符/布尔算子,gateway 自己注入 repo:<allowlist>
    if name in _SEARCH_SCOPED:
        sreason = _check_search_query(args.get("query", ""))
        if sreason:
            return deny(sreason)
        args = dict(args)  # 不修改原 args;注入可信 scope 后转发
        args["query"] = _inject_search_scope(args.get("query", ""))
        print(f"[gateway] search {name} scope-injected: {args['query'][:80]}", flush=True)

    # 4. B4b:L2 动作(merge/close)—— 仅 coordinator + 必须有 approval_ticket
    #    merge_pull_request→merge;update_pull_request(state)→close。走 claim→TOCTOU→上游→complete/fail/mark_unknown。
    l2_action = _derive_l2_action(name, args)
    if l2_action:
        if role != "coordinator":
            return deny("L2_REQUIRES_COORDINATOR")          # B4b step 1:仅 coordinator
        if "approval_ticket" not in args:
            return deny("L2_TICKET_REQUIRED")               # 无票 → 拒(旧占位语义保留)
        ticket_id = args.get("approval_ticket", "")
        ahash = canonical_args_hash(args)
        pr_num = args.get("pullNumber")
        # 4a. claim(一次 CAS;区分 DB_ERROR vs MISMATCH vs CLAIMED)
        cstat, claim = l2_claim_ticket(ticket_id, l2_action, repo, pr_num, ahash)
        if cstat == "DB_ERROR":
            audit_event(corr_id, "INTENT", role, name, "DENY", "L2_DB_UNAVAILABLE",
                        args_hash=ahash, target_repo=repo, ticket_id=ticket_id)
            print(f"[gateway] DENY L2 tool={name} → L2_DB_UNAVAILABLE", flush=True)
            return _deny_result("L2_DB_UNAVAILABLE", tool=name)
        if cstat != "CLAIMED" or claim is None:
            audit_event(corr_id, "INTENT", role, name, "DENY", "CLAIM_MISMATCH",
                        args_hash=ahash, target_repo=repo, ticket_id=ticket_id)
            print(f"[gateway] DENY L2 tool={name} ticket={ticket_id[:16]} → CLAIM_MISMATCH", flush=True)
            return _deny_result("CLAIM_MISMATCH", tool=name)
        eid = claim["execution_id"]
        payload = claim["canonical_payload"]
        # 4b. INTENT 审计 fail-closed
        if not audit_event(corr_id, "INTENT", role, name, "ALLOW", "L2_CLAIMED",
                           args_hash=ahash, target_repo=repo, target_branch=claim["target_branch"],
                           ticket_id=ticket_id, execution_id=eid):
            l2_exec("SELECT l2_fail_ticket(%s,%s::uuid,%s)",
                    (ticket_id, eid, "audit INTENT unavailable"))
            audit_event(corr_id, "ERROR", role, name, "DENY", "AUDIT_UNAVAILABLE",
                        args_hash=ahash, ticket_id=ticket_id, execution_id=eid,
                        error="INTENT audit failed; refused to call GitHub")
            return _deny_result("AUDIT_UNAVAILABLE", tool=name)
        print(f"[gateway] L2 CLAIMED tool={name} ticket={ticket_id[:16]} eid={eid[:8]}", flush=True)
        # 4c. TOCTOU(60s 超时;hang → mark_unknown 不留 EXECUTING)
        try:
            pr_actual = await asyncio.wait_for(
                _read_pr_upstream(args.get("owner"), args.get("repo"), pr_num), timeout=60)
        except (asyncio.TimeoutError, Exception) as e:
            l2_exec("SELECT l2_mark_unknown(%s,%s::uuid,%s)",
                    (ticket_id, eid, f"TOCTOU read timeout/error: {str(e)[:80]}"))
            audit_event(corr_id, "ERROR", role, name, "ERROR", "L2_MARKED_UNKNOWN",
                        args_hash=ahash, ticket_id=ticket_id, execution_id=eid,
                        error=f"TOCTOU: {str(e)[:120]}")
            print(f"[gateway] L2 UNKNOWN tool={name} → TOCTOU timeout/error", flush=True)
            return _deny_result("L2_TIMEOUT", tool=name)
        toctou_ok = (pr_actual
                     and pr_actual.get("head_sha") == claim["expected_head_sha"]
                     and pr_actual.get("state") == "open"
                     and pr_actual.get("base") == claim["target_branch"])
        if not toctou_ok:
            l2_exec("SELECT l2_fail_ticket(%s,%s::uuid,%s)",
                    (ticket_id, eid, f"TOCTOU: {pr_actual}"))
            audit_event(corr_id, "ERROR", role, name, "DENY", "TOCTOU_MISMATCH",
                        args_hash=ahash, ticket_id=ticket_id, execution_id=eid,
                        target_repo=repo, target_branch=claim["target_branch"], error=f"actual={pr_actual}")
            return _deny_result("TOCTOU_MISMATCH", tool=name)
        # 4d. 从 canonical_payload 构造上游 args
        upstream_args = {k: v for k, v in payload.items() if k != "approval_ticket"}
        # 4e. 调上游(60s 超时;timeout/exception → mark_unknown;明确拒绝 → fail;成功 → complete)
        try:
            result = await asyncio.wait_for(
                upstream.call_tool(name, upstream_args), timeout=60)
            is_err = getattr(result, "is_error", False)
            if is_err:
                err_txt = _extract_text(result)[:200]
                l2_exec("SELECT l2_fail_ticket(%s,%s::uuid,%s)",
                        (ticket_id, eid, f"upstream reject: {err_txt}"))
                audit_event(corr_id, "RESULT", role, name, "ERROR", "L2_UPSTREAM_REJECT",
                            args_hash=ahash, ticket_id=ticket_id, execution_id=eid,
                            target_repo=repo, result_status="ERROR", error=err_txt)
            else:
                sha = _extract_sha(result)
                # B4b P1#3:检查 complete 的 CAS 返回值;失败 → mark_unknown(防"GitHub 已 merge 但票 EXECUTING")
                ok = l2_exec("SELECT l2_complete_ticket(%s,%s::uuid,%s)",
                             (ticket_id, eid, sha or ""))
                if not ok:
                    l2_exec("SELECT l2_mark_unknown(%s,%s::uuid,%s)",
                            (ticket_id, eid, "l2_complete_ticket CAS failed"))
                    audit_event(corr_id, "ERROR", role, name, "ERROR", "L2_COMPLETE_FAILED",
                                args_hash=ahash, ticket_id=ticket_id, execution_id=eid,
                                target_repo=repo, git_sha=sha or "",
                                error="complete_ticket CAS failed; marked UNKNOWN for reconcile")
                else:
                    audit_event(corr_id, "RESULT", role, name, "ALLOW", "L2_COMPLETE",
                                args_hash=ahash, ticket_id=ticket_id, execution_id=eid,
                                target_repo=repo, git_sha=sha or "", result_status="OK")
            return result
        except (asyncio.TimeoutError, Exception) as e:
            # 网络超时/中断(请求可能已发,结果未知)→ mark_unknown,绝不自动重试
            l2_exec("SELECT l2_mark_unknown(%s,%s::uuid,%s)",
                    (ticket_id, eid, f"network: {str(e)[:120]}"))
            audit_event(corr_id, "ERROR", role, name, "ERROR", "L2_MARKED_UNKNOWN",
                        args_hash=ahash, ticket_id=ticket_id, execution_id=eid,
                        target_repo=repo, error=str(e)[:200])
            raise

    # 5. update_pull_request 非状态字段(fixer title/body 等):字段白名单
    if name == "update_pull_request":
        allowed_fields = _PR_IDENTITY | (_FIXER_PR_FIELDS if role == "fixer" else set())
        unexpected = set(args.keys()) - allowed_fields
        if unexpected:
            return deny("PR_FIELD_NOT_ALLOWED", fields=",".join(sorted(unexpected)))

    # 6. fixer 写操作:base/fix 前缀/受保护分支/路径 denylist
    if cfg["write_checks"] and name in _FIX_SET:
        reason = _check_write_args(name, args)
        if reason:
            return deny(reason)

    # 7. ALLOW —— B3:写工具 fail-closed(INTENT 必须先持久化才调 GitHub),读工具 fail-open
    is_write = name in _WRITE_SET
    if is_write:
        intent_ok = audit_event(corr_id, "INTENT", role, name, "ALLOW", "POLICY_ALLOW", **audit_kw)
        if not intent_ok:
            # fail-closed:绝不在 INTENT 未持久化时调 GitHub。尽力补一条 ERROR(可能也挂)。
            audit_event(corr_id, "ERROR", role, name, "DENY", "AUDIT_UNAVAILABLE",
                        **audit_kw, error="INTENT audit write failed; refused to call GitHub")
            print(f"[gateway] DENY role={role} tool={name} → AUDIT_UNAVAILABLE (no GitHub call)", flush=True)
            return _deny_result("AUDIT_UNAVAILABLE", tool=name)
        print(f"[gateway] ALLOW role={role} tool={name} repo={repo} branch={branch} "
              f"→ forward (intent logged corr={corr_id[:8]})", flush=True)
    else:
        # 读/search/list 等:fail-open 记 INTENT(失败也放行)
        audit_event(corr_id, "INTENT", role, name, "ALLOW", "READ_ALLOW", **audit_kw)
    # 调 GitHub
    try:
        result = await upstream.call_tool(name, args)
        try:
            is_err = getattr(result, "is_error", False)
            audit_event(corr_id, "RESULT", role, name,
                        "ALLOW" if not is_err else "ERROR", "UPSTREAM_RESULT",
                        **audit_kw, result_status="ERROR" if is_err else "OK")
        except Exception:
            pass
        return result
    except Exception as e:
        audit_event(corr_id, "ERROR", role, name, "ERROR", "UPSTREAM_FAIL",
                    **audit_kw, error=str(e)[:200])
        raise


# ───────────────────────── SSE + 认证 ─────────────────────────
# DNS rebinding 防护默认会按 Host 白名单拦内部主机名(policy-gw:8083)。
# Gateway 仅内部 docker 网络暴露 + 自带 Bearer token 认证,关闭此防护可接受。
sse = SseServerTransport(
    "/messages/",
    security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def _deny(scope, role_label, reason, status=401):
    audit(role_label, "(auth)", "DENY", reason,
          http_status=status, error=f"path={scope.get('path')}")
    return JSONResponse({"error": "POLICY_DENIED", "reason_code": reason}, status_code=status)


async def handle_sse(request: Request):
    scope = request.scope
    path_role = scope.get("path_params", {}).get("role", "")
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    role = TOKEN_TO_ROLE.get(token, "")
    if not role:
        return _deny(scope, f"path={path_role}", "BAD_TOKEN", 401)
    if role != path_role:  # 路径声明角色必须与 token 角色一致
        return _deny(scope, role, "ROLE_PATH_MISMATCH", 401)
    print(f"[gateway] SSE connect role={role} path=/{path_role}/sse → ACCEPT", flush=True)
    tok_ctx = current_role.set(role)
    try:
        async with sse.connect_sse(scope, request.receive, request._send) as (r, w):
            await server.run(r, w, server.create_initialization_options())
    finally:
        current_role.reset(tok_ctx)
    return Response()


async def messages_app(scope, receive, send):
    # POST /messages/?session_id=... — 由 SseServerTransport 路由到对应 session 的 stream
    await sse.handle_post_message(scope, receive, send)


@asynccontextmanager
async def lifespan(app):
    """启动时建一个共享上游 client session,缓存 tool 列表;关闭时清理。"""
    global upstream, upstream_tools
    # B3:search scope 注入当前只支持单仓库 allowlist;多仓库会静默放行未限定 query,启动即拒绝。
    if len(_GLOBAL_REPOS) != 1:
        raise RuntimeError(
            f"search scope requires exactly one allowlisted repo, got {len(_GLOBAL_REPOS)}; "
            "multi-repo needs structured scope composition (not implemented)"
        )
    print(f"[gateway] policy_version={POLICY_VERSION} policy_hash={POLICY_HASH}", flush=True)
    print(f"[gateway] roles={sorted(ROLE_TOKENS.keys())} upstream={UPSTREAM_URL}", flush=True)
    # 带重试的上游连接(bridge 可能晚于 gateway 起来)
    for attempt in range(1, 31):
        try:
            async with sse_client(UPSTREAM_URL) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    upstream = session
                    upstream_tools = (await session.list_tools()).tools
                    names = [t.name for t in upstream_tools]
                    print(f"[gateway] upstream ready: {len(upstream_tools)} tools "
                          f"(sample: {names[:5]})", flush=True)
                    yield  # 服务运行期
                    return
        except Exception as e:
            print(f"[gateway] upstream connect attempt {attempt}/30 failed: {e}", file=sys.stderr, flush=True)
            await asyncio.sleep(2)
    raise RuntimeError("upstream unreachable after 30 attempts")


app = Starlette(
    routes=[
        Route("/{role}/sse", handle_sse),
        Mount("/messages/", app=messages_app),
    ],
    lifespan=lifespan,
)


if __name__ == "__main__":
    print(f"[gateway] starting on {LISTEN_HOST}:{LISTEN_PORT}  AUDIT={'on' if AUDIT_DSN else 'off'}", flush=True)
    uvicorn.run(app, host=LISTEN_HOST, port=LISTEN_PORT, log_level="info")
