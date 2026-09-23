# RPD-24H 最终报告（2026-09-23，轮次收口）

## 最终状态判定：MANUAL-REQUIRED
存在需用户授权/人工决策事项（CASE2 门决策、D-1/D-2、正式 controller 注入、真实 CASE2），
已安全停止；证据、commit 与复现命令已保存。必要隔离验收全部 DONE，但"真实审批/CASE2"
不可由 AI 完成，故不判 PROMOTE-READY。

## 交付物（PR #233：feat/rpd-24h-delivery → main @ 84ad9af）
| RPD | 任务 | 状态 | 关键证据 |
|---|---|---|---|
| RPD-01 | 启动门禁+基线 | DONE | startup-report.md（BASE_HEAD=b79cd39，工作树干净） |
| RPD-02 | pgvector 证据复核 | DONE | 新一次性实例重放 **11/11** 后销毁 |
| RPD-03 | case_retrieval 接线契约 | DONE | validate_env `--preflight` 正/负实测（0/5）；路径深度 bug 修复 |
| RPD-04 | TicketStore 人工门闭环 | DONE | gate_ticket_smoke **13/13** + 28 单测 |
| RPD-05 | 后端回归 | DONE | 本地 **282/0**；PG 门控 25/26（race=基线既有，TEST-DEBT 保持） |
| RPD-06 | 前端契约对齐 | DONE | gate_display 八态纯映射 + 审批响应字段（5/5 测试） |
| RPD-07 | PR 交付 | DONE | PR #233（branch push + 唯一 PR，均显式核验） |
| RPD-08 | 最终报告 | DONE | 本文件 |

## GitHub 写入声明
仅两项：push feature branch `feat/rpd-24h-delivery` + 创建 PR #233。
无 main push、无 merge、无 check-run、无 issue/其他对象。
（首次 push 403 权限拒绝=明确失败，改用 gh 凭据助手后成功，远端核验一致。）

## 边界遵守声明
零真实审查；零付费模型调用；零空提交推送（业务仓库）；零共享 case-pg/生产 MinIO/
AgentTeams 配置修改；零 fixer/verifier 唤醒；零 embedding 模型下载（D-7 未变）；
TicketStore 未接真实部署；approve/reject 仍为本地隔离接口。

## 复现命令
```bash
# 回归（本地）
python -X utf8 -m pytest tests/approval/ tests/gh_bridge/ tests/model_gateway/ tests/skills/ -q -p no:cacheprovider
# PG 门控（隔离实例）
MERGEPILOT_PG_CONTRACT=1 python -X utf8 -m pytest tests/approval/test_store_pg.py tests/approval/test_migration_runner.py -q -p no:cacheprovider
# TicketStore smoke
python -X utf8 tools/approval/gate_ticket_smoke.py
# pgvector 全查询 smoke（需一次性 pgvector 实例；本轮重放 11/11）
# 配置校验
python -X utf8 tools/case_retrieval/deploy/validate_env.py --mode repo
```

## 回滚命令
```bash
# PR 层：关闭 PR #233 + 删除远端分支即完全回滚（main 未动）
gh pr close 233; git push origin --delete feat/rpd-24h-delivery
# 本地：回到基线
git checkout feat/backend-pg-storage && git reset --hard b79cd39
# 运行副本回滚点：r3work/rollback-20260923-222605/（CASE2 实测版三文件）
```

## 人工决策点（WAITING_HUMAN）
1. CASE2 人工门：HIGH finding 批准或拒绝
2. D-1/D-2：真实审批动作子集与审批人身份
3. 正式 controller/Worker 环境注入（共享变更）
4. 真实 CASE2 执行（新 head 空提交+预算门+单 check-run，见 CASE2-CANDIDATES §7）
5. gh CLI 凭据 vs git 凭据管理器权限不一致（本轮以 gh 通道完成授权内推送；是否统一凭据由用户决定）

## 资源清理
- 一次性 pgvector 容器已销毁（两轮）；口令文件已删除
- 本地 Docker 栈（elemiso/mp-*）保持原状（未新启长期资源）
