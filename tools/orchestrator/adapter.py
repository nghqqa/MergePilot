"""v3 adapter:M3.5 本地接线(shadow / fixture),桥的真实派发边界调用。

三态(MERGEPILOT_REVIEW_V3,默认 off):
- off:    本模块不被桥触发任何工作(零开销,旧链路逐字节不变);
- shadow: 读真实 PR 元数据与 diff(匿名只读 GET)→ 风险分级 + DispatchPlan
          → 维度状态持久化(run store)→ shadow 证据。**不调用真实 Agent、
          不产生 GitHub 写入、不改变旧链路结论**;
- on:     真实 Agent 接入需 R1/R2 授权。授权前桥侧 on 一律按 shadow 处理
          并显式记录;本地 fixture 管道(run_v3_fixture)仅供测试/演示,
          产出的记录 mode=fixture,永不冒充真实运行。

状态机唯一:所有维度转移经 stages.RunStages;RunStore 只是持久化哑层。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
import types
import urllib.request
from typing import Any, Callable, Dict, List, Optional

try:  # 包内导入(测试/常规包用法)
    from . import aggregate, console_contract, risk, runstore, scheduler, stages
except ImportError:  # 桥侧独立加载:自举兄弟模块到稳定包名
    _DIR = os.path.dirname(os.path.abspath(__file__))
    _pkg = sys.modules.setdefault("mp_orchestrator_pkg", None)
    if _pkg is None:
        _pkg = types.ModuleType("mp_orchestrator_pkg")
        _pkg.__path__ = [_DIR]
        sys.modules["mp_orchestrator_pkg"] = _pkg

    def _sib(name: str):
        if "mp_orchestrator_pkg." + name in sys.modules:
            return sys.modules["mp_orchestrator_pkg." + name]
        spec = importlib.util.spec_from_file_location(
            "mp_orchestrator_pkg." + name, os.path.join(_DIR, name + ".py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules["mp_orchestrator_pkg." + name] = mod
        spec.loader.exec_module(mod)
        return mod

    risk = _sib("risk")
    stages = _sib("stages")
    scheduler = _sib("scheduler")
    aggregate = _sib("aggregate")
    console_contract = _sib("console_contract")
    runstore = _sib("runstore")

MODE_ENV = "MERGEPILOT_REVIEW_V3"
RUNSTORE_ENV = "MERGEPILOT_V3_RUNSTORE"
VALID_MODES = ("off", "shadow", "on")


def review_v3_mode(environ: Optional[Dict[str, str]] = None) -> str:
    env = os.environ if environ is None else environ
    mode = (env.get(MODE_ENV) or "off").strip().lower()
    return mode if mode in VALID_MODES else "off"


def default_run_store_path() -> str:
    return os.environ.get(RUNSTORE_ENV) or os.path.join(
        os.path.expanduser("~"), ".mergepilot", "v3-runs.db")


def open_run_store(path: Optional[str] = None) -> "runstore.RunStore":
    return runstore.RunStore(path or default_run_store_path())


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ── 真实 PR 元数据(只读;匿名 GET 公开仓库,失败返回 None 走降级) ───────────
def fetch_pr_file_changes(repo: str, pr_number: int,
                          timeout_s: float = 10.0) -> Optional[list]:
    """GET /repos/{repo}/pulls/{n}/files —— 只读;任何失败 → None(降级,不抛)。"""
    url = "https://api.github.com/repos/%s/pulls/%d/files?per_page=100" % (
        repo, int(pr_number))
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "mergepilot-v3-shadow",
            "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            items = json.load(resp)
        return [risk.FileChange(f.get("filename", ""),
                                int(f.get("additions", 0)),
                                int(f.get("deletions", 0)))
                for f in items if f.get("filename")]
    except Exception:
        return None


# ── 共用:PR 更新使旧 run 失效(状态转移仍走唯一状态机) ────────────────────
def _supersede_old_runs(store, repo: str, pr_number: int, head_sha: str, now: str):
    for old in store.runs_for_pr(repo, pr_number):
        if old["head_sha"] != head_sha and not old.get("superseded"):
            st = stages.RunStages()
            for dim, rec in (old.get("stages") or {}).items():
                r = st.record(dim)
                r.status = rec.get("status", stages.PENDING)
                r.attempts = rec.get("attempts", 0)
                if r.status in (stages.PENDING, stages.RUNNING):
                    st.transition(dim, stages.CANCELLED, at=now,
                                  error="superseded by new head")
            old["stages"] = st.to_dict()
            old["stages_json"] = json.dumps(st.to_dict(), ensure_ascii=False)
            old["superseded"] = 1
            old["updated_at"] = now
            store.save_run(old)
            store.mark_superseded(old["run_id"], now)


# ── shadow 管道:真实 PR 数据,零 Agent/零外部写 ───────────────────────────
def run_v3_shadow(d: Dict[str, Any], *, run_store,
                  rag_snapshot: Optional[str] = None,
                  diff_fetcher: Callable[[str, int], Optional[list]] = fetch_pr_file_changes,
                  budget_hook: Optional[Callable[[], None]] = None,
                  now: Optional[str] = None) -> Dict[str, Any]:
    now = now or _now()
    repo, pr = d["repo"], int(d["pr_number"])
    head, base = d["observed_head_sha"], d.get("observed_base_sha", "")
    run_id = "shadow-gh-pr%d-%s" % (pr, head[:8])
    st = stages.RunStages()
    downgrade: List[str] = []

    _supersede_old_runs(run_store, repo, pr, head, now)

    # 1) 风险分级(真实 diff;取不到 → 显式 FAILED 降级,不伪造档位)
    st.transition("risk", stages.RUNNING, at=now)
    files = diff_fetcher(repo, pr)
    if files is None:
        st.transition("risk", stages.FAILED, at=now,
                      error="PR files fetch unavailable (read-only)")
        grade = None
        downgrade.append("risk: diff 不可得,档位未知(shadow 不猜测)")
    else:
        grade = risk.grade_risk(files)
        st.transition("risk", stages.SUCCEEDED, at=now)

    # 2) 计划(门未启用;预算钩子失败 → 计划 FAILED)
    plan_steps = []
    if grade is None:
        st.transition("plan", stages.SKIPPED, at=now, error="no risk grade")
        downgrade.append("plan: skipped(无档位)")
    else:
        st.transition("plan", stages.RUNNING, at=now)
        try:
            if budget_hook:
                budget_hook()
            planner = scheduler.DispatchPlanner(scheduler.OrchestratorConfig())
            plan_steps = [s.__dict__ for s in planner.build_plan(grade)]
            st.transition("plan", stages.SUCCEEDED, at=now)
        except Exception as e:
            st.transition("plan", stages.FAILED, at=now, error=str(e))
            downgrade.append("plan: %s" % str(e)[:120])

    # 3) 审查器槽位:shadow 不执行 Agent —— 显式 SKIPPED,绝不伪装已审查
    reviewers = list(grade.reviewers) if grade else []
    for name in reviewers:
        st.transition("review:%s" % name, stages.SKIPPED, at=now,
                      error="shadow: agent not executed")
    downgrade.extend("review:%s skipped(shadow)" % n for n in reviewers)

    # 4) 聚合/验证/发布:shadow 无 findings —— NOT_APPLICABLE / SKIPPED
    st.transition("aggregate", stages.NOT_APPLICABLE, at=now)
    st.transition("finding_validation", stages.NOT_APPLICABLE, at=now)
    st.transition("patch_validation", stages.NOT_APPLICABLE, at=now)
    st.transition("publish", stages.SKIPPED, at=now, error="shadow: no conclusion")

    outcome = stages.derive_outcome(st)
    evidence = {
        "run_id": run_id, "mode": "shadow", "repo": repo, "pr_number": pr,
        "head_sha": head, "base_sha": base,
        "risk": grade.to_dict() if grade else None,
        "plan": plan_steps, "stages": st.to_dict(), "outcome": outcome,
        "rag_snapshot": rag_snapshot,
        "external_writes": "none",
        "github_calls": ["GET pulls/files (read-only)"] if files is not None else [],
        "downgrade_reasons": downgrade,
        "budget": "hook consulted" if budget_hook else "unconfigured",
    }
    record = {
        "run_id": run_id, "repo": repo, "pr_number": pr, "head_sha": head,
        "base_sha": base, "mode": "shadow",
        "risk_tier": grade.level if grade else None,
        "risk_json": evidence["risk"], "plan_json": evidence["plan"],
        "stages_json": st.to_dict(), "outcome_json": outcome,
        "coverage_missing": outcome["coverage_missing"],
        "downgrade_reason": "; ".join(downgrade) or None,
        "finding_validation": "NOT_APPLICABLE", "patch_validation": "NOT_APPLICABLE",
        "rag_snapshot": rag_snapshot,
        "manifest_hash": runstore.evidence_hash(evidence),
        "evidence_path": None, "created_at": now, "updated_at": now,
    }
    run_store.save_run(record)
    evidence["manifest_hash"] = record["manifest_hash"]
    return evidence


# ── fixture 管道:本地测试/演示用假审查器,全链路纵向;mode=fixture ─────────
def run_v3_fixture(d: Dict[str, Any], *, run_store,
                   reviewer_findings: Dict[str, List[Dict[str, Any]]],
                   files: Optional[List] = None,
                   reviewer_fail: Optional[Dict[str, str]] = None,
                   rag_snapshot: Optional[str] = None,
                   budget_hook: Optional[Callable[[], None]] = None,
                   verify_fn: Optional[Callable[[Dict], str]] = None,
                   now: Optional[str] = None) -> Dict[str, Any]:
    """reviewer_fail: {name: "timeout"|"failed"} 注入单审查器故障;
    files: 定档用变更集(省略则从 findings 合成单行文件,通常 TRIVIAL);
    verify_fn(finding_dict) -> CONFIRMED|REFUTED|INCONCLUSIVE。"""
    now = now or _now()
    repo, pr = d["repo"], int(d["pr_number"])
    head = d["observed_head_sha"]
    run_id = "fixture-gh-pr%d-%s" % (pr, head[:8])
    st = stages.RunStages()
    reviewer_fail = reviewer_fail or {}
    downgrade: List[str] = []

    _supersede_old_runs(run_store, repo, pr, head, now)

    if files is None:
        files = [risk.FileChange(f.get("path", "app.py"), 1, 1)
                 for fs in reviewer_findings.values() for f in fs]
    files = [f if isinstance(f, risk.FileChange) else risk.FileChange(*f)
             for f in files]
    st.transition("risk", stages.RUNNING, at=now)
    grade = risk.grade_risk(files) if files else risk.grade_risk(
        [risk.FileChange("app.py", 30, 5)])
    st.transition("risk", stages.SUCCEEDED, at=now)

    st.transition("plan", stages.RUNNING, at=now)
    try:
        if budget_hook:
            budget_hook()
        planner = scheduler.DispatchPlanner(scheduler.OrchestratorConfig(
            reviewer_concurrency=2 if len(grade.reviewers) > 1 else 1))
        plan = planner.build_plan(grade)
        st.transition("plan", stages.SUCCEEDED, at=now)
    except Exception as e:
        st.transition("plan", stages.FAILED, at=now, error=str(e))
        downgrade.append("plan: %s" % str(e)[:120])
        plan = []

    review_step = next((s for s in plan if s.step_type == scheduler.STEP_REVIEWERS), None)
    raw_findings: List[aggregate.Finding] = []
    if review_step:
        def runner(step, name):
            fail = reviewer_fail.get(name)
            if fail == "timeout":
                raise TimeoutError("fixture: reviewer deadline")
            if fail == "failed":
                raise RuntimeError("fixture: model unavailable")
            for f in reviewer_findings.get(name, []):
                raw_findings.append(aggregate.Finding(
                    finding_id="%s-%s" % (name, f.get("finding_id", "f")),
                    source_reviewer=name, category=f.get("category", "generic"),
                    severity=f.get("severity", "MEDIUM"),
                    title=f.get("title", ""), path=f.get("path", "app.py"),
                    line=f.get("line", 1), evidence=f.get("evidence", "")))

        ex = scheduler.SerialReviewExecutor(scheduler.OrchestratorConfig(),
                                            runner=runner, budget_hook=budget_hook)
        ex.run_reviewers(review_step, st, now=now)
        for name in review_step.payload["reviewers"]:
            if st.stages["review:%s" % name].status != stages.SUCCEEDED:
                downgrade.append("review:%s %s" % (
                    name, st.stages["review:%s" % name].status))

    st.transition("aggregate", stages.RUNNING, at=now)
    agg = aggregate.aggregate_findings(raw_findings) if raw_findings else None
    st.transition("aggregate", stages.SUCCEEDED if raw_findings
                  else stages.NOT_APPLICABLE, at=now)

    st.transition("finding_validation", stages.RUNNING, at=now)
    validation = {"status": "NOT_APPLICABLE", "confirmed": 0, "refuted": 0,
                  "inconclusive": 0}
    if agg and agg.findings:
        for f in agg.findings:
            verdict = (verify_fn or _default_verify)(f.to_dict())
            validation[verdict.lower()] = validation.get(verdict.lower(), 0) + 1
        validation["status"] = stages.SUCCEEDED
        st.transition("finding_validation", stages.SUCCEEDED, at=now)
    else:
        st.transition("finding_validation", stages.NOT_APPLICABLE, at=now)

    st.transition("patch_validation", stages.NOT_APPLICABLE, at=now)  # 无门不进 fixer
    st.transition("publish", stages.RUNNING, at=now)
    st.transition("publish", stages.SUCCEEDED, at=now)   # fixture 结论发布(本地)

    outcome = stages.derive_outcome(st)
    evidence = {
        "run_id": run_id, "mode": "fixture", "repo": repo, "pr_number": pr,
        "head_sha": head, "risk": grade.to_dict(), "plan": [s.__dict__ for s in plan],
        "stages": st.to_dict(), "outcome": outcome,
        "aggregates": agg.to_dict() if agg else {"findings": [], "total": 0,
                                                 "dropped_duplicates": 0},
        "finding_validation": validation, "rag_snapshot": rag_snapshot,
        "external_writes": "none", "downgrade_reasons": downgrade,
    }
    record = {
        "run_id": run_id, "repo": repo, "pr_number": pr, "head_sha": head,
        "base_sha": d.get("observed_base_sha", ""), "mode": "fixture",
        "risk_tier": grade.level, "risk_json": evidence["risk"],
        "plan_json": evidence["plan"], "stages_json": st.to_dict(),
        "outcome_json": outcome, "coverage_missing": outcome["coverage_missing"],
        "downgrade_reason": "; ".join(downgrade) or None,
        "aggregates_json": evidence["aggregates"],
        "finding_validation": validation["status"],
        "patch_validation": "NOT_APPLICABLE", "rag_snapshot": rag_snapshot,
        "manifest_hash": runstore.evidence_hash(evidence),
        "evidence_path": None, "created_at": now, "updated_at": now,
    }
    run_store.save_run(record)
    evidence["manifest_hash"] = record["manifest_hash"]
    return evidence


def _default_verify(finding: Dict[str, Any]) -> str:
    return verify_default_verdict(finding.get("severity", "MEDIUM"))


def verify_default_verdict(severity: str) -> str:
    """fixture 默认验证器:HIGH→CONFIRMED,MEDIUM→INCONCLUSIVE,其余 REFUTED。
    仅用于 fixture 演示链路,不用于任何真实结论。"""
    return {"HIGH": "CONFIRMED", "MEDIUM": "INCONCLUSIVE"}.get(severity, "REFUTED")


# ── 控制台读模型(store 记录 → console_contract 载荷) ───────────────────────
def build_read_model(record: Dict[str, Any]) -> Dict[str, Any]:
    return console_contract.build_console_payload(
        run_id=record["run_id"],
        risk=record.get("risk") or {"level": record.get("risk_tier")},
        stages_dict=record.get("stages") or {},
        outcome=record.get("outcome") or {"outcome": "?", "review_complete": False,
                                          "coverage_missing": [],
                                          "critical_failures": [], "note": ""},
        aggregates=record.get("aggregates"),
        finding_validation={"status": record.get("finding_validation")
                            or "NOT_APPLICABLE"},
        patch_validation={"status": record.get("patch_validation")
                          or "NOT_APPLICABLE"},
        fixer=None, github_writeback=None,
        rag={"snapshot_id": record.get("rag_snapshot"), "evidence_refs": []},
        manifest={"manifest_hash": record.get("manifest_hash"),
                  "mode": record.get("mode")})
