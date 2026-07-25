# Task Plan: iso5-pr6-fix

**Task ID**: iso5-pr6-fix
**Assigned to**: fixer
**Started**: 2026-07-25T12:20:00Z
**Findings source**: shared/tasks/iso5-pr6-review/findings.md
**Repo**: nghqqa/mergepilot-test
**Base branch**: feature/m1-e2e
**File**: user_service.py

## Risk Categorization

| Finding | Risk | Action |
|---------|------|--------|
| F-1: 硬编码生产密钥 (line 3) | L2 | ✅ 只出方案 |
| F-2: 硬编码凭证 API_KEY (line 3) | L2 | ✅ 只出方案 |
| F-3: SQL 注入 (line 7) | L2 | ✅ 只出方案 |
| F-4: 连接未关闭 (line 6-7) | L1 | 🔧 修复+PR(需人工审) |
| F-5: 无错误处理 (line 5-7) | L0 | 🔧 修复+PR |
| F-6: 数据库路径硬编码 (line 6) | L0 | 🔧 修复+PR |
| F-7: 不可测试 (line 5-7) | L1 | 🔧 修复+PR(需人工审) |

## Plan

### Phase 1: L0+L1 修复(合入一个 PR, branch: fix/iso5-l0l1)

修改 `user_service.py`,针对 F-4/F-5/F-6/F-7,生成完整的修复后文件:

1. **F-6 (L0)** — db_path 改为环境变量可配(默认`db.sqlite`)
2. **F-5 (L0)** — 包裹 try/except sqlite3.Error,返回错误信息
3. **F-4 (L1)** — 使用 `with sqlite3.connect(...) as conn:` 上下文管理器
4. **F-7 (L1)** — `db_path` 参数化,允许 test 注入 mock 连接
5. 调用 gh-mcp-fix.sh 提交 PR

注意:不触及 F-1/F-2(密钥)和 F-3(SQL 注入)——L2 需人工审批。

### Phase 2: L2 方案输出(不出代码)

输出 F-1/F-2/F-3 的修复方案,标记 needs-approval,交协调者走审批门。

## Steps

- [x] Step 1: 读取 findings
- [x] Step 2: 获取 source `user_service.py` @ feature/m1-e2e
- [ ] Step 3: 编写 L0+L1 修复文件 → `/tmp/fix/user_service.py`
- [ ] Step 4: 编写 PR body → `/tmp/fix/pr-body-l0l1.md`
- [ ] Step 5: 执行 gh-mcp-fix.sh 提交 PR
- [ ] Step 6: 编写 L2 方案 → `/root/hiclaw-fs/shared/tasks/iso5-pr6-fix/l2-plans.md`
- [ ] Step 7: 推送成果到 MinIO
- [ ] Step 8: @manager 报告完成
