# CASE-MANIFEST 模板 — 每案例唯一需要编写的文件

> 角色行为见 `ROLE-{LEADER,REVIEWER,FIXER,VERIFIER}.md`（v1.0 冻结，跨案例零改动）。
> 新案例 = 复制本模板填空 + Leader kickoff；不编写、不修改任何角色契约。

```markdown
# CASE-MANIFEST · <run_id>

## 标识
- run_id: run-<slug>-<yyyymmdd>-<seq>
- project_id: <elemiso-xxx>          # Controller/CoPaw 项目 id
- repo: <owner/repo>（公开仓，只读）
- pr: #<n>（状态：OPEN，零写入）
- head 分支 / head SHA: <branch> / <sha40>     # 所有角色必须 checkout 并校验
- base SHA: <sha40>
- 变更规模: <n> files +<a>/-<d>

## 审查焦点（给 Reviewer 的指向，不含预期结论）
- 目标文件/函数: <path::func>
- 运行的复现/测试命令: <exact command>

## 验收行为（给 Fixer 的目标 / Verifier 的探针依据）
1. <攻击面：注入/穿越序列被拒绝（4xx），无副作用>
2. <合法路径：仍返回 200 与正确内容>
3. <错误输入：明确错误响应（如 404）>

## 任务与轮次
- tasks: <prefix>-review-1 → <prefix>-fix-1 → <prefix>-verify-1
- fix 最大重派轮数: 2
- 门预期: 批准 / 拒绝 / 由操作员现场决策（默认：现场决策）

## 环境参数
- python: /opt/venv/standard/bin/python
- 各角色工作区目录名: ~/prN-{work,fix-work*,verify-work}
```

## 填空示例（真实用例对照）
| 字段 | run-elem-fastapi-pr2-20260916-01 | run-elem-fastapi-pr3-20260916-01 |
|---|---|---|
| head SHA | 1dedf5e1992c… | ad267a6e5120… |
| 目标 | demo_high_risk.py::demo_download（路径穿越） | demo_cmd_exec.py::demo_ping（命令注入） |
| 门结果 | 批准 → fix+verify → VERIFIED PASS | 拒绝 → rejected/locked → blocked |

角色契约对两轮运行逐字相同（v1.0 回溯固化自两轮的公共协议）。
