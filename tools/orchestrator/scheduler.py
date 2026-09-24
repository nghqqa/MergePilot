"""调度器接口(ARCHITECTURE-V3 §4):派发计划 + 并发上限 + 唯一调度权。

- DispatchPlanner 是 run 派发决策的唯一出处;flag 关闭时不产生任何派发。
- 并发:多 PR 全局 1(两路并发测试通过并授权前不调高);单 PR 审查器并发 2
  (可配 1 = 全串行);一个 worker 同一时间只执行一个任务。
- 全部并发任务共享全局预算(costmeter BudgetGuard 由调用方传入钩子)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

MAX_PR_CONCURRENCY = 1            # 全局多 PR 上限(测试通过+授权前不改)
DEFAULT_REVIEWER_CONCURRENCY = 2  # 单 PR 内独立审查器并发

# 计划步骤类型(与 stages 维度同名)
STEP_REVIEWERS = "reviewers"        # 并行组:风险档决定的审查器集合
STEP_AGGREGATE = "aggregate"
STEP_FINDING_VALIDATION = "finding_validation"
STEP_PUBLISH = "publish"
STEP_GATE = "gate"                  # 需要修复时的人工门
STEP_FIX = "fix"
STEP_PATCH_VALIDATION = "patch_validation"
STEP_DELIVER = "deliver"


@dataclass(frozen=True)
class OrchestratorConfig:
    reviewer_concurrency: int = DEFAULT_REVIEWER_CONCURRENCY
    stage_timeout_s: Dict[str, float] = field(default_factory=dict)   # 每 agent/阶段
    run_hard_deadline_s: float = 1800.0                               # run 硬上限
    max_attempts: Dict[str, int] = field(default_factory=dict)        # 每阶段重试预算
    degradation_policy: str = "degrade"   # delay | degrade | manual(不自动换模型)
    gate_enabled: bool = False            # D-1/D-2 未拍板前不得开启真实门
    critical_reviewers: tuple = ("security",)


@dataclass(frozen=True)
class PlanStep:
    step_type: str
    payload: dict                      # reviewers=[...]/patch_ctx=...
    depends_on: tuple = ()
    timeout_s: Optional[float] = None
    max_attempts: int = 1


class DispatchPlanner:
    """按风险档与配置产出有序计划。纯函数式:不改任何运行状态。"""

    def __init__(self, config: OrchestratorConfig = None):
        self.config = config or OrchestratorConfig()
        if self.config.reviewer_concurrency < 1:
            raise ValueError("reviewer_concurrency 必须 ≥1")
        if not self.config.gate_enabled:
            pass  # 门关闭时 plan 不含 gate/fix/patch 段,发建议即终

    def build_plan(self, risk_grade, needs_fix: Optional[bool] = None) -> List[PlanStep]:
        """needs_fix=None=未知(需人工门后判定);False=低风险自动路径;True=已批修复。"""
        steps: List[PlanStep] = []
        steps.append(PlanStep(
            STEP_REVIEWERS, {"reviewers": list(risk_grade.reviewers),
                             "concurrency": min(self.config.reviewer_concurrency,
                                                max(1, len(risk_grade.reviewers)))},
            timeout_s=self.config.stage_timeout_s.get(STEP_REVIEWERS),
            max_attempts=self.config.max_attempts.get(STEP_REVIEWERS, 1)))
        steps.append(PlanStep(STEP_AGGREGATE, {}))
        steps.append(PlanStep(STEP_FINDING_VALIDATION, {}))
        steps.append(PlanStep(STEP_PUBLISH, {}))
        if needs_fix is True:
            if not self.config.gate_enabled:
                raise ValueError("需要修复但 gate 未启用(D-1/D-2 未拍板);禁止进入 fixer")
            steps.append(PlanStep(STEP_GATE, {"mode": "await_approval"}))
            steps.append(PlanStep(STEP_FIX, {}))
            steps.append(PlanStep(STEP_PATCH_VALIDATION, {}))
        elif needs_fix is None and self.config.gate_enabled:
            steps.append(PlanStep(STEP_GATE, {"mode": "await_approval"}))
        steps.append(PlanStep(STEP_DELIVER, {}))
        return steps


class SerialReviewExecutor:
    """最小执行器:按计划推进维度状态;审查器按并发上限运行(默认 2,可 1=串行)。

    这是接口骨架的最小实现,用于本地并行/超时/降级测试;
    真实 Agent 适配(R1/R2)在授权后接入,替换 runner 钩子。
    调度权唯一:本执行器是 v3 路径唯一允许推进 RunStages 的组件。
    """

    def __init__(self, config: OrchestratorConfig = None,
                 stages=None, runner: Optional[Callable] = None,
                 budget_hook: Optional[Callable] = None):
        self.config = config or OrchestratorConfig()
        self.stages = stages
        self.runner = runner or (lambda step, name: None)   # (step, reviewer_name) -> None | raises
        self.budget_hook = budget_hook                      # () -> None | raises BudgetExceeded

    def _run_one_reviewer(self, step: PlanStep, stages, name: str, now: str):
        dim = "review:%s" % name
        budget = max(1, int(step.max_attempts))
        while True:
            rec = stages.record(dim)
            stages.transition(dim, "RUNNING", at=now)
            try:
                if self.budget_hook:
                    self.budget_hook()
                self.runner(step, name)
                stages.transition(dim, "SUCCEEDED", at=now)
                return
            except TimeoutError as e:
                stages.transition(dim, "TIMEOUT", at=now, error=str(e))
            except Exception as e:
                stages.transition(dim, "FAILED", at=now, error=str(e))
            # 预算内重试(TIMEOUT/FAILED → RUNNING);耗尽即停留(报告态)
            if stages.stages[dim].attempts >= budget:
                return

    def run_reviewers(self, step: PlanStep, stages, now: str) -> dict:
        """按并发上限执行审查器组;结果返回每维度终态(测试断言用)。"""
        import threading
        names = list(step.payload.get("reviewers", []))
        conc = max(1, int(step.payload.get("concurrency", 1)))
        results: Dict[str, str] = {}
        # 一个 worker 一个任务:线程数 ≤ 并发上限,分批推进
        for i in range(0, len(names), conc):
            batch = names[i:i + conc]
            threads = [threading.Thread(target=self._run_one_reviewer,
                                        args=(step, stages, n, now))
                       for n in batch]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        for n in names:
            dim = "review:%s" % n
            results[dim] = stages.stages[dim].status if dim in stages.stages else "PENDING"
        return results
