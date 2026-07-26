#!/usr/bin/env python3
"""
Policy Gateway — MergePilot M3-B1
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
  - 审计能力已接入:每次 list/call/auth-fail 都尝试写 audit-pg.mcp_calls
    (注:当前审计为 fail-open —— 审计写入自身故障时不阻断业务调用。
     写操作和 L2 动作的 fail-closed-on-audit-failure 留待 B3/B4。)
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
import uuid
import contextvars
from contextlib import asynccontextmanager

# ───────────────────────── 配置 ─────────────────────────
ROLE_TOKENS = json.loads(os.environ.get("ROLE_TOKENS", "{}"))   # {"reviewer":"tok","fixer":"tok","verifier":"tok","coordinator":"tok"}
UPSTREAM_URL = os.environ.get("UPSTREAM_URL", "http://github-mcp:8082/sse")
AUDIT_DSN = os.environ.get("AUDIT_DSN", "")                     # postgresql://mergepilot:pw@audit-pg:5432/mergepilot_audit
LISTEN_HOST = os.environ.get("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8083"))
POLICY_VERSION = os.environ.get("POLICY_VERSION", "b1-permissive")
# B1 policy_hash = 角色集合 + 版本的 hash(B2 改为 policy.yaml 内容 hash)
POLICY_HASH = hashlib.sha256(
    json.dumps({"roles": sorted(ROLE_TOKENS.keys()), "ver": POLICY_VERSION}).encode()
).hexdigest()[:16]

TOKEN_TO_ROLE = {tok: role for role, tok in ROLE_TOKENS.items()}

# ───────────────────────── 每连接角色上下文 ─────────────────────────
# connect_sse → server.run 是同一个 task,handler 在该 task 内执行,contextvar 可见。
current_role: contextvars.ContextVar[str] = contextvars.ContextVar("current_role", default="")

# ───────────────────────── 审计 ─────────────────────────
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
    """写一条审计行。fail-open:审计自身故障只记 stderr,不阻断业务(B3/B4 改 fail-closed)。"""
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
    except Exception as e:  # 审计自身故障不阻断(fail-open)
        print(f"[gateway] audit FAILED ({decision} {tool}): {e}", file=sys.stderr, flush=True)
    return rid


# ───────────────────────── MCP server(proxy 语义)─────────────────────────
from mcp.server import Server  # noqa: E402
from mcp.server.sse import SseServerTransport  # noqa: E402
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402
from mcp import ClientSession  # noqa: E402
from mcp.client.sse import sse_client  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.routing import Route, Mount  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse, Response  # noqa: E402
import uvicorn  # noqa: E402

server = Server("policy-gateway")
upstream: ClientSession | None = None
upstream_tools: list = []  # 缓存上游 Tool 列表


@server.list_tools()
async def list_tools():
    role = current_role.get()
    audit(role, "(list_tools)", "ALLOW", "B1_PERMISSIVE_LIST")
    print(f"[gateway] list_tools role={role} → {len(upstream_tools)} tools (B1 permissive)", flush=True)
    # B1:返回上游全量。B2:按 policy.yaml[role] 过滤,deny-by-default。
    return upstream_tools


@server.call_tool()
async def call_tool(name: str, arguments: dict | None):
    role = current_role.get()
    args = arguments or {}
    repo = ""
    if args.get("owner") and args.get("repo"):
        repo = f"{args.get('owner')}/{args.get('repo')}"
    branch = str(args.get("branch") or args.get("head") or args.get("base") or "")
    args_hash = hashlib.sha256(
        json.dumps(args, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]
    # B1:认证即放行。B2:此处先 policy 决策,DENY 返回结构化错误。
    audit(role, name, "ALLOW", "B1_PERMISSIVE_CALL",
          args_hash=args_hash, target_repo=repo, target_branch=branch)
    print(f"[gateway] call_tool role={role} tool={name} repo={repo} branch={branch} → forward (B1)", flush=True)
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
