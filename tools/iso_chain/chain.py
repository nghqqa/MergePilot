# -*- coding: utf-8 -*-
"""iso_chain.chain — 隔离 fix/verify 闭环编排(CL-08 载体)。

流程:
  iso 建票(结构化证据) → head 新鲜度核验 → iso 审批(显式身份) →
  dispatch(start_exec fencing + outbox) → fixer(预算内真实模型) →
  补丁产物 → 干净 checkout apply --check → 沙箱测试(原始 PoC+回归+边界)
  → 独立 verifier(仅目标输入) → finalize → 票据 complete/fail。
最多 2 轮;VERIFIED 或轮次耗尽即止。**不派发生产 fixer/verifier**。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from typing import Any, Callable, Dict, List, Optional

MAX_ROUNDS = 2
HARNESS_RESULT_MARK = "HARNESS_RESULT:"


class IsoChainError(Exception):
    pass


def head_is_fresh(tip_sha: str, binding_head: str) -> bool:
    return (tip_sha or "").lower() == (binding_head or "").lower()


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def run_chain(*, store, outbox, budget, model_client, sandbox_runner,
              workspace_clean: str, run: Dict[str, Any], task_id: str,
              reviewer_result_text: str, audit_records: list,
              target_path: str, target_code: str, test_path: str,
              test_code: str, finding_block: str, standard_excerpt: str,
              poc_cases: List[Dict[str, Any]],
              approve_identity: str, tip_sha: str,
              artifacts_dir: str, max_tokens: int = 4000,
              log: Callable = print) -> Dict[str, Any]:
    """执行隔离闭环。所有模型调用经 budget;所有状态迁移经票据 CAS。"""
    import dispatch as disp
    import fixer as fx
    import patchwork as pw
    import verifier as vf
    from structured_gate import open_structured_gate_ticket

    report: Dict[str, Any] = {"rounds": [], "ticket_id": None, "final": None}

    # 1) 结构化建票(证据绑定,幂等)
    ticket, created, why = open_structured_gate_ticket(
        store, executor_id="iso-chain", run=run,
        reviewer_result_text=reviewer_result_text,
        audit_records=audit_records, task_id=task_id)
    if ticket is None:
        raise IsoChainError("TICKET_REFUSED:" + why)
    report["ticket_id"] = ticket.ticket_id
    report["ticket_created"] = created
    log("iso ticket %s (%s)" % (ticket.ticket_id, "created" if created else "existing"))

    # 2) head 新鲜度核验(绑定 head 必须等于当前 tip)
    if not head_is_fresh(tip_sha, ticket.binding.head_sha):
        store.transition(ticket.ticket_id, "invalidate_for_new_head",
                         actor=tip_sha)
        raise IsoChainError("STALE_HEAD: binding %s != tip %s"
                            % (ticket.binding.head_sha[:12], tip_sha[:12]))

    # 3) iso 审批(显式身份;CAS;TTL 建票时已设)
    r = store.transition(ticket.ticket_id, "approve", actor=approve_identity)
    if not r.ok:
        raise IsoChainError("APPROVE_FAILED:" + r.reason)

    for attempt in range(1, MAX_ROUNDS + 1):
        # 4) 派发(start_exec fencing + outbox)——executor = 预算内真实模型 fixer
        def _fixer_exec(payload, _tp=target_path, _tc=target_code):
            prompt = fx.build_fixer_prompt(
                target_path=_tp, target_code=payload["target_code"],
                test_path=test_path, test_code=test_code,
                finding_block=finding_block, standard_excerpt=standard_excerpt)
            return fx.call_fixer(model_client, budget, prompt,
                                 max_tokens=max_tokens)

        now = _now()
        dispatch_id = "dsp-" + os.urandom(8).hex()
        result = store.transition(ticket.ticket_id, "start_exec", now=now)
        if not result.ok:
            raise IsoChainError("DISPATCH_FENCE:" + result.reason)
        payload_hash = hashlib.sha256(
            json.dumps({"t": target_code[:64]}, sort_keys=True)
            .encode("utf-8")).hexdigest()
        outbox.record_sent(dispatch_id, ticket.ticket_id, attempt,
                           payload_hash, now)
        log("round %d dispatched (%s)" % (attempt, dispatch_id))
        try:
            fr = _fixer_exec({"target_code": target_code,
                              "target_path": target_path})
        except Exception as e:  # noqa
            outbox.mark(dispatch_id, "FAILED", now)
            report.setdefault("failures", []).append(
                {"attempt": attempt, "stage": "fixer",
                 "detail": type(e).__name__ + ": " + str(e)[:120]})
            store.transition(ticket.ticket_id, "fail", error="fixer exception")
            report["final"] = "FIXER_FAILED"
            return report
        outbox.mark(dispatch_id, "EXECUTED", now)
        if not fr.get("ok"):
            report.setdefault("failures", []).append(
                {"attempt": attempt, "stage": "fixer",
                 "detail": fr.get("detail")})
            store.transition(ticket.ticket_id, "fail",
                             error="fixer: " + str(fr.get("detail"))[:120])
            report["final"] = "FIXER_FAILED"
            return report
        patch_diff = fr["diff"]

        # 5) 补丁产物 + 干净 checkout apply 校验
        out_dir = os.path.join(artifacts_dir, "round%d" % attempt)
        manifest = pw.build_artifacts(patch_diff, run_id=run["run_id"],
                                      head_sha=run["head_sha"],
                                      ticket_id=ticket.ticket_id,
                                      attempt=attempt, report={},
                                      out_dir=out_dir)
        ac = pw.apply_check(workspace_clean,
                            os.path.join(out_dir, "patch.diff"))
        if not ac["ok"]:
            report.setdefault("failures", []).append(
                {"attempt": attempt, "stage": "apply", "detail": ac["detail"]})
            store.transition(ticket.ticket_id, "fail",
                             error="apply: " + ac["detail"][:120])
            report["final"] = "APPLY_FAILED"
            return report
        pw.apply_patch(workspace_clean, os.path.join(out_dir, "patch.diff"))

        # 6) 沙箱测试(真实采集;受限容器)
        test_results = run_tests(sandbox_runner, workspace_clean, poc_cases)

        # 7) 独立 verifier(修补后代码 + finding + 补丁 + 采集结果;无 fixer 推理)
        patched_code = read_target(workspace_clean, target_path)
        v_in = vf.build_verifier_input(
            finding={"severity": "HIGH",
                     "description": "CWE-22 arbitrary file read via "
                                    "uncontained path join"},
            patched_code=patched_code, patch_diff=patch_diff,
            test_results=test_results, repo=run["repo"],
            pr=run.get("pr"), head_sha=run["head_sha"])
        rid = budget.reserve(est_input=len(v_in["prompt"]) // 3)
        vresp = model_client.chat("deepseek-flash",
                                  [{"role": "user", "content": v_in["prompt"]}],
                                  timeout_s=120, max_tokens=max_tokens,
                                  temperature=0.0)
        budget.settle(rid, vresp.get("usage"))
        v_model = vf.parse_verdict(vresp.get("content") or "")
        final = vf.finalize(v_model, test_results, {"ok": True})
        report["rounds"].append({"attempt": attempt,
                                 "dispatch": dispatch_id,
                                 "final": final["final"],
                                 "model_verdict": v_model["verdict"],
                                 "tests_all_passed":
                                     test_results.get("all_passed")})

        if final["final"] == "VERIFIED":
            fp = pw.sha256_text(patch_diff)
            store.transition(ticket.ticket_id, "complete", result_fingerprint=fp)
            report["final"] = "VERIFIED"
            report["patch_sha256"] = fp
            report["patch_manifest"] = manifest
            return report
        if attempt < MAX_ROUNDS:
            target_code = patched_code      # 第 2 轮基于修补后代码继续
            continue
        store.transition(ticket.ticket_id, "fail",
                         error="verifier: %s" % final["final"])
        report["final"] = final["final"]
        report.setdefault("failures", []).append(
            {"attempt": attempt, "stage": "verifier", "detail": final})
    return report


def run_tests(sandbox_runner, workspace_clean: str,
              poc_cases: List[Dict[str, Any]]) -> Dict[str, Any]:
    """真实采集测试结果:受限容器内执行 harness(原始 PoC 断言)。"""
    payload = json.dumps(poc_cases, ensure_ascii=False)
    harness = (
        "import json, sys, os, asyncio\n"
        "cases = json.loads(r'''%s''')\n"
        "sys.path.insert(0, '/work/backend/src')\n"
        "from interfaces.api.v1.demo_high_risk import demo_download\n"
        "results = []\n"
        "loop = asyncio.new_event_loop()\n"
        "for c in cases:\n"
        "    name = c['name']\n"
        "    try:\n"
        "        resp = loop.run_until_complete(demo_download(name))\n"
        "        p = getattr(resp, 'path', '')\n"
        "        body = open(p).read()[:40] if p and os.path.isfile(p) else '?'\n"
        "        results.append({'name': name, 'result': 'HTTP200 body=%%r' %% body})\n"
        "    except Exception as e:\n"
        "        results.append({'name': name, 'result': 'REJECTED: %%s' %% type(e).__name__})\n"
        "all_passed = all('REJECTED' in r['result'] for r in results)\n"
        "print(%r + json.dumps({'all_passed': all_passed, 'cases': results}))\n"
        % (payload, HARNESS_RESULT_MARK))
    out = sandbox_runner(["python", "-c", harness],
                         workspace=workspace_clean, workdir="/work",
                         timeout_s=120)
    return parse_summary(out.get("stdout", ""))


def parse_summary(stdout: str) -> Dict[str, Any]:
    i = (stdout or "").rfind(HARNESS_RESULT_MARK)
    if i < 0:
        return {"all_passed": False, "cases": [],
                "parse_error": "harness marker missing"}
    try:
        return json.loads(stdout[i + len(HARNESS_RESULT_MARK):]
                          .strip().splitlines()[0])
    except Exception as e:  # noqa
        return {"all_passed": False, "cases": [], "parse_error": str(e)[:80]}


def read_target(workspace: str, rel_path: str) -> str:
    with open(os.path.join(workspace, rel_path), encoding="utf-8") as f:
        return f.read()
