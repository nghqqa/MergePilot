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
AUDIT_DSN = os.environ.get("AUDIT_DSN", "")                     # postgresql://mergepilot:pw@audit-pg:5432/mergepilot_audit
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


def audit(caller, tool, decision, reason_code="", *, args_hash="", ticket_id=None,
          target_repo="", target_branch="", result_status="", http_status=None,
          git_sha="", run_id="", error=""):
    """写一条不可变审计行。失败只记 stderr,不影响请求路径(fail-open 审计,业务 fail-closed)。"""
    if not AUDIT_DSN:
        return ""
    rid = str(uuid.uuid4())
    try:
        conn = _get_audit_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO mcp_calls
                  (request_id, ts, caller_agent, tool, decision, reason_code,
                   policy_version, policy_hash, ticket_id, args_hash,
                   target_repo, target_branch, result_status, http_status, git_sha, run_id, error)
                VALUES (%s, now(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (rid, caller, tool, decision, reason_code,
                 POLICY_VERSION, POLICY_HASH, ticket_id, args_hash,
                 target_repo, target_branch, result_status, http_status, git_sha, run_id, error),
            )
    except Exception as e:  # 审计自身故障不阻断
        print(f"[gateway] audit FAILED ({decision} {tool}): {e}", file=sys.stderr, flush=True)
    return rid


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

    def deny(reason_code, **extra):
        audit(role, name, "DENY", reason_code,
              args_hash=args_hash, target_repo=repo, target_branch=branch)
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

    # 4. update_pull_request 混合风险工具:按角色字段白名单
    #    fixer 仅 title/body;state→L2(任何角色);coordinator 其他字段→PR_FIELD_NOT_ALLOWED
    if name == "update_pull_request":
        if "state" in args:
            return deny("L2_TICKET_REQUIRED")
        allowed_fields = _PR_IDENTITY | (_FIXER_PR_FIELDS if role == "fixer" else set())
        unexpected = set(args.keys()) - allowed_fields
        if unexpected:
            return deny("PR_FIELD_NOT_ALLOWED", fields=",".join(sorted(unexpected)))
        # 通过 → 落到 ALLOW+forward

    # 5. fixer 写操作:base/fix 前缀/受保护分支/路径 denylist(repo allowlist 已在步骤 2)
    if cfg["write_checks"] and name in _FIX_SET:
        reason = _check_write_args(name, args)
        if reason:
            return deny(reason)

    # 6. L2 动作(merge/delete):B2.1 一律要票据,B4 才校验票据
    if name in _L2_SET and cfg["l2_requires_ticket"]:
        return deny("L2_TICKET_REQUIRED")

    # 7. ALLOW + 转发上游
    audit(role, name, "ALLOW", "B2_POLICY_ALLOW",
          args_hash=args_hash, target_repo=repo, target_branch=branch)
    print(f"[gateway] ALLOW role={role} tool={name} repo={repo} branch={branch} → forward", flush=True)
    try:
        result = await upstream.call_tool(name, args)
        try:
            is_err = getattr(result, "is_error", False)
            audit(role, name, "ALLOW" if not is_err else "ERROR",
                  "UPSTREAM_RESULT", args_hash=args_hash, target_repo=repo,
                  target_branch=branch, result_status="ERROR" if is_err else "OK")
        except Exception:
            pass
        return result
    except Exception as e:
        audit(role, name, "ERROR", "UPSTREAM_FAIL", args_hash=args_hash,
              target_repo=repo, target_branch=branch, error=str(e)[:200])
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
