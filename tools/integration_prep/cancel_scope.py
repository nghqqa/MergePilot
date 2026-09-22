"""run 范围取消设计(执行保护复核,2026-09-22):纯逻辑,容器操作在获批执行时进行。

调用产生方盘点(谁会产生模型/外部调用):
  leader    编排轮次(模型)——可再委托(重试/新任务) ⇒ 取消时**先停**
  reviewer  审查轮次+MCP 调用(rag_retrieve/PG 查询/skill_*)(模型+本地 IO)
  fixer/verifier  门关闭时空闲;若激活也会调用模型 ⇒ 取消时同样停止
  bridge    无模型调用(编排/台账)
  element-web / proxy / ctrl-gateway  无 agent 模型调用

关键事实(复核结论):
- 停止 reviewer ≠ 停止整个 run:leader 可能重新委托;⇒ 取消顺序 = 先 leader 后 reviewer/fixer/verifier;
- 桥观察超时/进程退出 ≠ 累计消费硬上限:上游在途请求仍可能计费;
- 唯一即时切断 = 停止产生调用的容器;恢复 = 重启容器(已演练可逆)。
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional

AGENT_CONTAINERS = ("elemiso-worker-leader", "elemiso-worker-reviewer",
                    "elemiso-worker-fixer", "elemiso-worker-verifier")
CANCEL_ORDER = ("elemiso-worker-leader",      # 先停再委托方
                "elemiso-worker-reviewer",
                "elemiso-worker-fixer",
                "elemiso-worker-verifier")


def cancel_plan(reason: str, exclusivity_confirmed: bool) -> Dict[str, object]:
    """生成有序取消计划。exclusivity_confirmed=已核对栈内无其他活动项目
    (停止 agent 容器影响整个栈的任务面,独占是前置条件)。"""
    if not reason or not reason.strip():
        raise ValueError("取消必须记录原因")
    steps: List[Dict[str, str]] = [
        {"order": "1", "action": "stop_bridge",
         "detail": "结束桥进程(停止认领/派发;台账已认领行按 M1 恢复语义接管)",
         "cmd": "结束运行中的 gh_bridge 进程"},
        {"order": "2", "action": "stop_agents",
         "detail": "按序停止 agent 容器:%s(先 leader 防再委托)" % ", ".join(CANCEL_ORDER),
         "cmd": " ".join("docker stop %s;" % c for c in CANCEL_ORDER)},
        {"order": "3", "action": "verify_quiet",
         "detail": "核对无新增模型流量(网关访问日志尾部)且无新委托消息(Matrix 团队房)",
         "cmd": "tail 网关日志;检查团队房新消息"},
        {"order": "4", "action": "record_caveat",
         "detail": "记录:上游在途请求可能仍计费;这不构成累计消费硬上限",
         "cmd": ""},
        {"order": "5", "action": "restore_note",
         "detail": "恢复=docker start 四容器(已演练可逆);台账恢复走 M1 resume 语义",
         "cmd": " ".join("docker start %s;" % c for c in CANCEL_ORDER)},
    ]
    return {"reason": reason, "exclusivity_confirmed": exclusivity_confirmed,
            "steps": steps}


def validate_preconditions(exclusivity_confirmed: bool,
                           active_projects_probe: Callable[[], List[str]],
                           ) -> Dict[str, object]:
    """前置校验:独占确认 + 栈内无其他活动项目(探针注入,便于测试)。"""
    active = active_projects_probe() or []
    return {"ok": exclusivity_confirmed and not active,
            "active_projects": active,
            "note": ("独占成立且无其他活动项目" if exclusivity_confirmed and not active
                     else "存在其他活动项目: %s——取消影响整个栈,需先确认" % ", ".join(active)
                     if active else "独占未确认——不可执行容器停止")}


def verify_cancelled(stop_probe: Callable[[str], bool],
                     quiet_probe: Callable[[], bool]) -> Dict[str, object]:
    """取消后核验:四容器均已停止且网关流量静默(探针注入)。"""
    stopped = {c: bool(stop_probe(c)) for c in CANCEL_ORDER}
    quiet = bool(quiet_probe())
    return {"containers_stopped": stopped, "gateway_quiet": quiet,
            "ok": all(stopped.values()) and quiet}
