"""R1/R2/R3 执行计划与 R4/R7 同步计划(声明式,供 dry-run 打印与人工审核)。

每步 = (动作描述, 命令/操作要点, 证据采集点)。计划即文档:批准人审核的就是
这些步骤;执行脚本(steps 的消费者)在获批后按计划逐步执行并采集证据。
"""
from __future__ import annotations

from typing import Dict, List

Plan = List[Dict[str, str]]

R1_PLAN: Plan = [
    {"step": "1 注入测试投递行",
     "op": "服务器 PG INSERT github_deliveries 2-3 行,delivery_id 前缀 'test-',repo=白名单仓库",
     "evidence": "SELECT 导出行 JSON(打标可见)"},
    {"step": "2 起桥(off 模式冒烟)",
     "op": "MERGEPILOT_REVIEW_V3=off 运行桥 --once,确认旧链路行为不变",
     "evidence": "台账终态与 v3 hook 零调用日志"},
    {"step": "3 kill -9 @ 认领后",
     "op": "桥 claim 后立即 kill -9;重启桥,观察 take_over_stale 接管",
     "evidence": "接管 CAS rowcount=1 日志;新 claim_id;resume 分流记录"},
    {"step": "4 kill -9 @ 派发后",
     "op": "kickoff 发出后 kill -9;重启,验证不重发 kickoff(场景2)",
     "evidence": "resume 只续观察/续发布的日志;无第二条 kickoff 消息"},
    {"step": "5 kill -9 @ 发布前",
     "op": "审查终态后、publish 前 kill -9;重启,验证 receipt/对账收敛(场景3)",
     "evidence": "receipt 采纳或 reconcile 采纳;PROCESSED 终态"},
    {"step": "6 清场",
     "op": "DELETE test- 行;导出台账前后对照",
     "evidence": "清场后 SELECT 空结果"},
]

R2_PLAN: Plan = [
    {"step": "1 测试分支 PR",
     "op": "在 mergepilot-test/* 分支准备 PR(用户或获批 agent 操作)",
     "evidence": "PR 号与 head SHA 记录"},
    {"step": "2 v3 shadow 先行",
     "op": "MERGEPILOT_REVIEW_V3=shadow 跑一轮,产出 v3 证据(无 Agent)",
     "evidence": "RunStore 记录 + manifest_hash;风险档位与真实 diff 一致"},
    {"step": "3 真实 Agent 审查",
     "op": "恢复 on 流程(仍不自动批准/合并/推代码),跑完整桥链路",
     "evidence": "check-run POST 201;result.md 引用;run-manifest 全字段"},
    {"step": "4 发布故障恢复",
     "op": "断 reporter 通道制造 PUBLISH_FAILED→恢复→对账收敛",
     "evidence": "ERROR(retryable)→reconcile 采纳→PROCESSED 轨迹"},
    {"step": "5 独立判据归档",
     "op": "逐场景对照 ACCEPTANCE-M1 行,一条一判据,不合并判定",
     "evidence": "M1 验收矩阵更新(带证据指针)"},
]

R3_PLAN: Plan = [
    {"step": "1 回写成功",
     "op": "正常链 POST check-run",
     "evidence": "check_run_id + 台账 PROCESSED(note 含 id)"},
    {"step": "2 重复 webhook",
     "op": "同 delivery 重放",
     "evidence": "already_processed 短路,无第二次审查"},
    {"step": "3 PR 更新",
     "op": "同 PR 新 head 触发 synchronize",
     "evidence": "旧 run superseded+旧 check-run 不代表新 head;两 check 并存"},
    {"step": "4 回写失败恢复",
     "op": "同 R2 步骤 4",
     "evidence": "ERROR(retryable)→恢复轨迹"},
    {"step": "5 旧 run 失效",
     "op": "RunStore 检查 superseded 标记与 CANCELLED 维度",
     "evidence": "控制台 /api/runs 显示 superseded=true"},
]

R4_SYNC_PLAN: Plan = [
    {"step": "1 备份",
     "op": "copy r3work\\scripts\\gh_bridge.py → gh_bridge.py.bak-<date>(连同涉及的 tools 子集清单)",
     "evidence": "备份文件存在 + sha256 记录"},
    {"step": "2 校验",
     "op": "记录 repo 侧与运行副本的 sha256 前后对照",
     "evidence": "对照表"},
    {"step": "3 同步",
     "op": "复制 gh_bridge.py 与 tools/{orchestrator,rag,approval,costmeter,console_v3} 所需文件",
     "evidence": "副本 sha256 == repo sha256"},
    {"step": "4 off 冒烟",
     "op": "MERGEPILOT_REVIEW_V3=off 跑 --once,旧行为不变",
     "evidence": "台账终态正常;v3 零调用"},
    {"step": "5 回退命令存档",
     "op": "打印并记录: copy .bak → 原文件",
     "evidence": "回退命令文本入库"},
]

R7_SYNC_PLAN: Plan = [
    {"step": "1 备份运行语料",
     "op": "copy rag-live-corpus.json → .bak-<date>",
     "evidence": "备份 sha256"},
    {"step": "2 导入(幂等)",
     "op": "corpus_tool.import(repo 语料 → 运行语料);内容相同则零操作",
     "evidence": "import 输出 changed/snapshot_id"},
    {"step": "3 启动 rag-live",
     "op": "node tools/rag/live/rag-live-server.mjs(RAG_LIVE_PORT=4184,env 指向运行语料)",
     "evidence": "/healthz ok=true, chunks=12"},
    {"step": "4 快照核对",
     "op": "桥 manifest rag.snapshot 与 corpus_tool.snapshot 一致",
     "evidence": "两者 sha256 相同记录"},
    {"step": "5 回退",
     "op": "copy .bak;停服务=结束 node 进程",
     "evidence": "回退命令文本入库"},
]


def print_plan(name: str, plan: Plan) -> None:
    print("== %s(%d 步;dry-run,未执行任何动作)==" % (name, len(plan)))
    for s in plan:
        print("[%s] %s\n  操作: %s\n  证据: %s" % (name, s["step"], s["op"], s["evidence"]))


PLANS: Dict[str, Plan] = {"R1": R1_PLAN, "R2": R2_PLAN, "R3": R3_PLAN,
                          "R4": R4_SYNC_PLAN, "R7": R7_SYNC_PLAN}

if __name__ == "__main__":
    import sys
    for key in (sys.argv[1:] or list(PLANS)):
        print_plan(key, PLANS[key])
