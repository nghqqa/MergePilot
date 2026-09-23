# -*- coding: utf-8 -*-
"""run_context.py — trusted run context (R5 缺口修复:审计流无 run 归属)。

问题:CASE1 复核确认,skill/RAG 审计流(rag-tool-spans.jsonl)的每条记录
只有 tool/args_hash/时间,没有任何可信 run 归属;run_id 只出现在 kickoff
文本和 check-run summary 里(模型可转述、可丢失,不构成编排器事实源)。

本模块给出最小可信透传:全部字段由桥/编排器在派发边界产出,来源只有
   run-manifest(桥 write-once) + 投递行(服务器 PG) + 桥自身计数,
绝不接受模型输出或普通请求参数填充。产出三个 artifact:

  1. run-context.json   MinIO 项目目录 write-once(与 run-manifest 同语义);
  2. bridge.run_context / bridge.run_end  审计流记录(best-effort, advisory;
     审计服务不可达只记日志,不阻断派发——与 rag advisory 门同语义);
  3. attribute_audit_calls()  隔离回放:按 run_context 边界把审计流
     逐条归属到 run(窗口相关法,只在单飞链路下唯一,输出如实标注方法)。

字段(最小集,§R5):
  run_id / attempt_no / repo / pr / head_sha / skill_digest /
  retrieval_mode / manifest_id
"""
from __future__ import annotations

import datetime
import json
from typing import Any, Dict, List, Optional, Tuple

RECORD_TYPE_CONTEXT = "bridge.run_context"
RECORD_TYPE_END = "bridge.run_end"
DATA_MODE = "ORCHESTRATION"   # 非模型产出、非语料检索:编排器事实
CONTEXT_VERSION = 1

REQUIRED_FIELDS = ("run_id", "attempt_no", "repo", "pr", "head_sha",
                   "skill_digest", "retrieval_mode", "manifest_id")


def _now() -> str:
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def build_run_context(manifest: Dict[str, Any], delivery: Dict[str, Any],
                      attempt_no: int, manifest_id: str,
                      now: Optional[str] = None) -> Dict[str, Any]:
    """从编排器自有输入构造 run context。纯函数。

    manifest     桥派发前 write-once 的 run-manifest(已含 code/skills/rag);
    delivery     服务器 PG 投递行(delivery_id/repo/pr_number/observed_head_sha);
    attempt_no   桥自身计数:1 + 回队次数(RQn),>=1;
    manifest_id  run-manifest 的规范 sha256(与 kickoff 引用行同值)。

    任一字段缺失时置 None 并记入 missing[](可追溯≠可复算,不伪造)。
    """
    if int(attempt_no) < 1:
        raise ValueError("attempt_no must be >= 1 (orchestrator-owned count)")
    code = manifest.get("code") or {}
    skills = manifest.get("skills") or {}
    rag = manifest.get("rag") or {}
    digests = skills.get("content_sha256")
    ctx = {
        "context_version": CONTEXT_VERSION,
        "run_id": manifest.get("run_id"),
        "attempt_no": int(attempt_no),
        "repo": code.get("repo") or delivery.get("repo"),
        "pr": code.get("pr") if isinstance(code.get("pr"), int) else delivery.get("pr_number"),
        "head_sha": code.get("head_sha") or delivery.get("observed_head_sha"),
        "skill_digest": dict(digests) if isinstance(digests, dict) else None,
        "retrieval_mode": rag.get("retrieval_mode"),
        "manifest_id": manifest_id,
        "delivery_id": delivery.get("delivery_id"),
        "authored_by": "gh_bridge",   # 唯一合法作者;模型/请求参数不可写此记录
        "created_at": now or _now(),
    }
    # 与 manifest 的 code.pr 对齐(manifest 本身不存 pr;以投递行为准)
    missing = [k for k in REQUIRED_FIELDS if ctx.get(k) is None]
    ctx["missing"] = missing
    return ctx


def validate_run_context(obj: Any) -> Tuple[bool, str]:
    """契约校验:形状 + 作者 + 字段来源标记。读方(回放/审计)用。"""
    if not isinstance(obj, dict):
        return False, "not an object"
    if obj.get("context_version") != CONTEXT_VERSION:
        return False, "context_version mismatch"
    if obj.get("authored_by") != "gh_bridge":
        return False, "authored_by must be gh_bridge (orchestrator-only record)"
    for k in REQUIRED_FIELDS:
        if k not in obj:
            return False, "missing field: %s" % k
    if not isinstance(obj.get("attempt_no"), int) or obj["attempt_no"] < 1:
        return False, "attempt_no must be int >= 1"
    if obj.get("missing") is not None and not isinstance(obj.get("missing"), list):
        return False, "missing must be a list"
    return True, ""


def context_record(ctx: Dict[str, Any], now: Optional[str] = None) -> Dict[str, Any]:
    """审计流 run_context 记录(advisory 通道用;含全部上下文字段)。"""
    ok, why = validate_run_context(ctx)
    if not ok:
        raise ValueError("invalid run context: %s" % why)
    return {"record_type": RECORD_TYPE_CONTEXT, "tool": RECORD_TYPE_CONTEXT,
            "ts": now or _now(), "data_mode": DATA_MODE, "run_context": ctx}


def end_record(ctx: Dict[str, Any], terminal_status: str,
               now: Optional[str] = None) -> Dict[str, Any]:
    """审计流 run_end 记录(窗口右边界)。terminal_status 来自项目权威状态。"""
    ok, why = validate_run_context(ctx)
    if not ok:
        raise ValueError("invalid run context: %s" % why)
    return {"record_type": RECORD_TYPE_END, "tool": RECORD_TYPE_END,
            "ts": now or _now(), "data_mode": DATA_MODE,
            "run_id": ctx["run_id"], "attempt_no": ctx["attempt_no"],
            "terminal_status": terminal_status}


# ── 隔离回放:审计流 → run 归属 ─────────────────────────────────────────────
def attribute_audit_calls(audit_records: List[Dict[str, Any]],
                          contexts: List[Dict[str, Any]],
                          strict_windows: bool = False) -> Dict[str, Any]:
    """把审计流逐条归属到 run。纯函数,供隔离回放/离线审计。

    contexts      已校验的 run context 列表(含 created_at 排序由调用方保证
                  或本函数按 created_at 稳定排序);
    audit_records 解析后的审计行(须含 ts/tool;bridge.* 记录是边界,不归属自身)。

    归属方法 = 窗口相关法:run_i 的窗口 = [ctx_i.created_at, 下一 context
    的 created_at 或流末尾)。单飞链路(桥串行处理+定向认领)下窗口内记录
    唯一归属;strict_windows=True 时要求流里存在显式 run_context/run_end
    边界记录,否则返回 unattributed_all(用于多飞场景拒绝猜测)。

    输出如实标注 method 与局限,不把窗口相关说成硬绑定。"""
    ctxs = sorted((dict(c) for c in contexts), key=lambda c: str(c.get("created_at") or ""))
    for c in ctxs:
        ok, why = validate_run_context(c)
        if not ok:
            raise ValueError("invalid run context in input: %s" % why)
    boundaries = [r for r in audit_records
                  if isinstance(r, dict) and str(r.get("record_type", "")).startswith("bridge.")]
    method = "bridge-boundary" if boundaries else "window-correlation"
    if strict_windows and not boundaries:
        return {"method": method, "strict": True, "runs": [],
                "unattributed": len(audit_records),
                "note": "no bridge boundary records; refusing to guess (strict)"}
    spans = []
    for i, c in enumerate(ctxs):
        start = _ts_key(c.get("created_at"))
        end = _ts_key(ctxs[i + 1].get("created_at")) if i + 1 < len(ctxs) else None
        spans.append((start, end, c))
    runs, unattributed = [], 0
    for r in audit_records:
        if not isinstance(r, dict) or not r.get("ts") or not r.get("tool"):
            unattributed += 1
            continue
        if str(r.get("record_type", "")).startswith("bridge."):
            continue   # 边界记录本身不归属
        ts = _ts_key(r["ts"])
        hit = None
        for start, end, c in spans:
            if ts >= start and (end is None or ts < end):
                hit = c
                break
        if hit is None or ts == float("-inf"):
            unattributed += 1
            continue
        runs.append({"ts": r["ts"], "tool": r["tool"],
                     "run_id": hit["run_id"], "attempt_no": hit["attempt_no"],
                     "manifest_id": hit["manifest_id"]})
    return {"method": method,
            "note": ("window correlation; unique only under single-flight dispatch"
                     if method == "window-correlation" else
                     "bracketed by bridge.run_context/run_end records"),
            "runs": runs, "unattributed": unattributed}


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _ts_key(ts: Any) -> float:
    """ISO-8601/Z 时间戳 → 可比较 epoch 秒(毫秒精度差异不能靠字典序)。"""
    s = str(ts or "").strip()
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return float("-inf")   # 不可解析的时间戳永远落不进任何窗口
