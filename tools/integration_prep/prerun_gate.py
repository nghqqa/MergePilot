"""真实案例运行前检查门(prerun gate):全部本地/只读,可注入探针以便测试。

用途:首个受控真实案例执行前跑一遍,全绿才允许起桥。检查项对应
执行确认单(2026-09-22)的运行前清单 A 节。任何一项 FAIL => 不启动。
"""
from __future__ import annotations

import hashlib
import os
from typing import Callable, Dict, List, Optional, Tuple


def _sha(p: str) -> str:
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def check_result(name: str, ok: bool, detail: str) -> Dict[str, str]:
    return {"check": name, "ok": "PASS" if ok else "FAIL", "detail": detail}


def run_gate(*,
             repo_head: str,
             git_head: str,
             git_clean: bool,
             repo_bridge: str,
             run_bridge: str,
             corpus_snapshot_bridge: Optional[str],
             corpus_snapshot_expected: str,
             v3_mode: str,
             expected_mode: str = "off",
             bridge_running: Optional[bool] = None,
             ledger_running_rows: int = 0,
             containers_present: Dict[str, bool] = None,
             rag_live_required: bool = False,
             rag_health_ok: Optional[bool] = None,
             budget_state: str = "MISSING_ACK",   # SET | MISSING_ACK
             delivery_head_processed: Optional[bool] = None,
             delivery_head: str = "",
             ) -> List[Dict[str, str]]:
    """执行全部检查并返回报告。探针值全部由调用方采集(本函数无 IO)。"""
    out: List[Dict[str, str]] = []
    out.append(check_result(
        "git_pinned", git_head == repo_head and git_clean,
        "HEAD=%s clean=%s(期望 %s)" % (git_head[:12], git_clean, repo_head[:12])))
    try:
        same = _sha(repo_bridge) == _sha(run_bridge)
        out.append(check_result("run_copy_synced", same,
                                "repo==run sha %s" % _sha(run_bridge)[:12]))
    except OSError as e:
        out.append(check_result("run_copy_synced", False, str(e)[:120]))
    if corpus_snapshot_bridge is not None:
        out.append(check_result(
            "corpus_snapshot", corpus_snapshot_bridge == corpus_snapshot_expected,
            "snapshot %s" % corpus_snapshot_bridge[:12]))
    else:
        out.append(check_result("corpus_snapshot", False, "snapshot 不可得(桥读不到语料)"))
    out.append(check_result(
        "v3_mode", v3_mode == expected_mode,
        "mode=%s(期望 %s);shadow 对照需单独阶段并改期望" % (v3_mode, expected_mode)))
    out.append(check_result(
        "single_orchestrator", bridge_running is False and ledger_running_rows == 0,
        "bridge_running=%s ledger_RUNNING=%d(无第二执行者争抢)" % (
            bridge_running, ledger_running_rows)))
    missing = [k for k, v in (containers_present or {}).items() if not v]
    out.append(check_result(
        "stack_containers", not missing,
        "全部在位" if not missing else "缺失: %s" % ", ".join(missing)))
    if rag_live_required:
        out.append(check_result(
            "rag_live", rag_health_ok is True,
            "需要 RAG 的案例: health ok=%s" % rag_health_ok))
    else:
        out.append(check_result("rag_live", True, "本轮案例不要求 RAG(未启动)"))
    if budget_state == "SET":
        out.append(check_result("budget", True, "预算边界已配置(含 provider 侧硬上限)"))
    else:
        out.append(check_result(
            "budget", False,
            "预算未配置且未获显式确认(MISSING_ACK)——真实调用前必须落实硬边界"))
    if delivery_head_processed is None:
        out.append(check_result(
            "delivery_prereq", False, "未核对: head=%s" % delivery_head[:12]))
    elif delivery_head_processed:
        out.append(check_result(
            "delivery_prereq", False,
            "head 已被处理过(already_processed 会跳过)——需新 head 触发"))
    else:
        out.append(check_result(
            "delivery_prereq", True,
            "head %s 从未处理,满足触发条件" % delivery_head[:12]))
    return out


def gate_passed(report: List[Dict[str, str]]) -> bool:
    return all(r["ok"] == "PASS" for r in report)


def format_report(report: List[Dict[str, str]]) -> str:
    lines = ["== PRERUN GATE =="]
    for r in report:
        lines.append("[%s] %s — %s" % (r["ok"], r["check"], r["detail"]))
    lines.append("GATE: %s" % ("PASS — 允许启动" if gate_passed(report)
                               else "FAIL — 存在未满足项,不启动"))
    return "\n".join(lines)


def default_budget_state(environ: Optional[Dict[str, str]] = None,
                         ack_env: str = "MERGEPILOT_BUDGET_MISSING_ACK") -> str:
    """预算状态判定:配置了 MERGEPILOT_RUN_BUDGET_TOKENS => SET;
    未配置但显式设置 MISSING_ACK=1(用户已知悉无进程内硬预算) => MISSING_ACK 仍 FAIL
    但报告注明"已知悉";两者都无 => MISSING_ACK。
    诚实语义:进程内 worker 预算不存在(R5 缺口),硬上限必须来自 provider 侧配额。"""
    env = os.environ if environ is None else environ
    return "SET" if env.get("MERGEPILOT_RUN_BUDGET_TOKENS") else "MISSING_ACK"
