#!/usr/bin/env python3
"""receiver.py — GitHub webhook → INSERT-only github_deliveries(M8-GH-1)。

冻结合同(设计 2026-08-18 收口):
  * HMAC-SHA256 对**原始字节**常时比较(X-Hub-Signature-256);
    验签失败 401,零数据库写入。
  * body > 2 MiB → 413;严格 JSON(拒绝重复键/NaN/Infinity),允许 GitHub
    未知字段 —— 只提取最小 allowlisted envelope;canonical_payload 仅保存
    最小信封 + body_sha256,不保存原始 body。
  * 仅 pull_request × opened|synchronize|reopened 进入 PENDING;ping 与其余
    一切事件 → IGNORED(零工作流写入)。
  * 重复交付判定: 纯 INSERT + PostgreSQL 唯一违反(pgcode 23505)→
    200 duplicate。**不使用 ON CONFLICT (delivery_id) DO NOTHING** —— 带
    conflict_target 的 ON CONFLICT 要求仲裁列的 SELECT 权限,INSERT-only
    角色在真实 PostgreSQL 上会被确定性拒绝(隔离 staging 实证;
    mock 单测无法覆盖该服务器端语义)。
  * HTTP: 新 PENDING=202;重复/IGNORED=200;格式/字段违规=400;验签=401;
    超限=413;DB 故障=503(未提交即失败,GitHub 重试安全)。
  * 本模块不导入 controller/process_event/submit_task,不写任何治理表;
    stage_events 命名空间('gh:<delivery_id>')由 Controller drain 侧使用。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from typing import Any, Callable, Optional

MAX_BODY_BYTES = 2 * 1024 * 1024

_EVENT_NAME_RE = re.compile(r"^[a-z_]{1,64}$")
_DELIVERY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{7,63}$")
_ACTION_RE = re.compile(r"^[a-z_]{1,64}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,255}$")

MAPPED_ACTIONS = frozenset({"opened", "synchronize", "reopened"})

HTTP_OK = 200
HTTP_ACCEPTED = 202
HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401
HTTP_TOO_LARGE = 413
HTTP_UNAVAILABLE = 503


class ReceiverError(Exception):
    """永久接收错误(HTTP 4xx)。"""

    def __init__(self, status: int, code: str):
        self.status = status
        self.code = code
        super().__init__(code)


# ── 严格 JSON(重复键 / 非有限数拒绝;未知字段允许) ────────────────────────

def _strict_loads(raw: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ReceiverError(HTTP_BAD_REQUEST,
                                    "duplicate JSON key: %s" % key)
            result[key] = value
        return result

    def reject_constant(token):
        raise ReceiverError(HTTP_BAD_REQUEST,
                            "non-finite JSON number: %s" % token)

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                           parse_constant=reject_constant)
    except ReceiverError:
        raise
    except (UnicodeDecodeError, ValueError) as exc:
        raise ReceiverError(HTTP_BAD_REQUEST, "invalid JSON") from exc
    if not isinstance(value, dict):
        raise ReceiverError(HTTP_BAD_REQUEST, "payload is not an object")
    return value


def _need_int(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReceiverError(HTTP_BAD_REQUEST, "expected integer field")
    return value


def _need_str(value, regex, code) -> str:
    if not isinstance(value, str) or not regex.fullmatch(value):
        raise ReceiverError(HTTP_BAD_REQUEST, code)
    return value


# ── 最小 envelope 提取 ──────────────────────────────────────────────────────

def extract_envelope(event_name: str, payload: dict) -> dict:
    """提取并严格校验最小 allowlisted envelope(未知字段忽略)。"""
    envelope = {
        "schema_version": "1",
        "event_name": _need_str(event_name, _EVENT_NAME_RE,
                                "invalid event name"),
    }
    action = payload.get("action")
    if action is not None:
        envelope["action"] = _need_str(action, _ACTION_RE, "invalid action")
    installation = payload.get("installation") or {}
    installation_id = installation.get("id") if isinstance(installation,
                                                           dict) else None
    if installation_id is not None:
        installation_id = _need_int(installation_id)
        if installation_id <= 0:
            raise ReceiverError(HTTP_BAD_REQUEST, "invalid installation id")
        envelope["installation_id"] = installation_id
    repository = payload.get("repository") or {}
    repo = repository.get("full_name") if isinstance(repository, dict) else None
    if repo is not None:
        envelope["repo"] = _need_str(repo, _REPO_RE, "invalid repo")
    pull = payload.get("pull_request")
    if isinstance(pull, dict):
        envelope["pr_number"] = _need_int(pull.get("number"))
        if envelope["pr_number"] < 1:
            raise ReceiverError(HTTP_BAD_REQUEST, "invalid pr number")
        head = pull.get("head") or {}
        base = pull.get("base") or {}
        envelope["observed_head_sha"] = _need_str(
            head.get("sha") if isinstance(head, dict) else None,
            _SHA40_RE, "invalid head sha")
        envelope["observed_base_sha"] = _need_str(
            base.get("sha") if isinstance(base, dict) else None,
            _SHA40_RE, "invalid base sha")
        branch = head.get("ref") if isinstance(head, dict) else None
        if branch is not None:
            envelope["branch"] = _need_str(branch, _BRANCH_RE,
                                           "invalid branch")
    return envelope


def classify(envelope: dict, allowlist: Optional[frozenset]) -> str:
    """'PENDING' | 'IGNORED'(仅映射动作进入工作流)。"""
    if envelope.get("event_name") != "pull_request":
        return "IGNORED"
    if envelope.get("action") not in MAPPED_ACTIONS:
        return "IGNORED"
    repo = envelope.get("repo")
    if not repo:
        raise ReceiverError(HTTP_BAD_REQUEST, "pull_request missing repo")
    for field in ("installation_id", "pr_number", "observed_head_sha",
                  "observed_base_sha"):
        if envelope.get(field) is None:
            raise ReceiverError(HTTP_BAD_REQUEST,
                                "pull_request missing %s" % field)
    if allowlist is not None and repo not in allowlist:
        return "IGNORED"
    return "PENDING"


def canonicalize(envelope: dict, body_sha256: str) -> str:
    envelope = dict(envelope)
    envelope["body_sha256"] = body_sha256
    return json.dumps(envelope, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


# ── HMAC ────────────────────────────────────────────────────────────────────

def verify_signature(raw: bytes, header: Optional[str],
                     secret: str) -> None:
    if not header or not header.startswith("sha256="):
        raise ReceiverError(HTTP_UNAUTHORIZED, "signature header missing")
    expected = hmac.new(secret.encode("utf-8"), raw,
                        hashlib.sha256).hexdigest()
    provided = header[len("sha256="):].strip().lower()
    if not hmac.compare_digest(expected, provided):
        raise ReceiverError(HTTP_UNAUTHORIZED, "signature mismatch")


# ── 处理入口(HTTP 层与测试共用) ───────────────────────────────────────────

_INSERT_SQL = """
INSERT INTO public.github_deliveries
       (delivery_id, event_name, action, installation_id, repo, pr_number,
        observed_head_sha, observed_base_sha, body_sha256,
        canonical_payload, status)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def handle_webhook(*, raw: bytes, event_header: Optional[str],
                   delivery_header: Optional[str], signature_header:
                   Optional[str], secret: str,
                   connect: Callable[[], Any],
                   allowlist: Optional[frozenset] = None) -> tuple:
    """完整处理一次 webhook。返回 (http_status, outcome, detail)。

    connect() 必须返回一个 psycopg2 风格连接(持有 github_event_ingress
    INSERT-only 角色);本函数对该连接只执行 _INSERT_SQL 一条语句。
    """
    if len(raw) > MAX_BODY_BYTES:
        return (HTTP_TOO_LARGE, "rejected", "body exceeds 2 MiB")
    try:
        verify_signature(raw, signature_header, secret)   # 401 → 零 DB 写
        event_name = _need_str(event_header or "", _EVENT_NAME_RE,
                               "invalid event header")
        delivery_id = _need_str(delivery_header or "", _DELIVERY_ID_RE,
                                "invalid delivery header")
        payload = _strict_loads(raw)                       # 400 → 零 DB 写
        envelope = extract_envelope(event_name, payload)   # 400 → 零 DB 写
        status_value = classify(envelope, allowlist)       # 400/IGNORED
    except ReceiverError as exc:
        return (exc.status, "rejected", exc.code)

    body_sha256 = hashlib.sha256(raw).hexdigest()
    canonical = canonicalize(envelope, body_sha256)

    conn = None
    try:
        conn = connect()
        with conn.cursor() as cur:
            cur.execute(_INSERT_SQL, (
                delivery_id, envelope["event_name"],
                envelope.get("action") or "unspecified",
                envelope.get("installation_id"), envelope.get("repo"),
                envelope.get("pr_number"),
                envelope.get("observed_head_sha"),
                envelope.get("observed_base_sha"), body_sha256, canonical,
                status_value))
        conn.commit()
    except Exception as exc:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        if getattr(exc, "pgcode", None) == "23505":
            # 主键冲突 = delivery GUID 重放(INSERT-only 判重路径)。
            return (HTTP_OK, "duplicate", "delivery id replay")
        return (HTTP_UNAVAILABLE, "error", "database unavailable")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    if status_value == "IGNORED":
        return (HTTP_OK, "ignored", "event not mapped")
    return (HTTP_ACCEPTED, "accepted", "delivery queued")


def healthz(connect: Callable[[], Any]) -> bool:
    """SELECT 1 连接活性探测(无需任何表权限)。"""
    conn = None
    try:
        conn = connect()
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return True
    except Exception:
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def connect_from_env() -> Any:
    """INSERT-only DSN(env GITHUB_INGRESS_DSN);psycopg2 懒加载。"""
    import psycopg2
    return psycopg2.connect(os.environ["GITHUB_INGRESS_DSN"])


__all__ = [
    "HTTP_ACCEPTED", "HTTP_BAD_REQUEST", "HTTP_OK", "HTTP_TOO_LARGE",
    "HTTP_UNAUTHORIZED", "HTTP_UNAVAILABLE", "MAX_BODY_BYTES",
    "MAPPED_ACTIONS", "ReceiverError", "canonicalize", "classify",
    "connect_from_env", "extract_envelope", "handle_webhook", "healthz",
    "verify_signature",
]
