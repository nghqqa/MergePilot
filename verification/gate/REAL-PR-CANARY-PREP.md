# REAL_PR_FIX_VERIFY_CONTROLLED_CANARY_PREPARATION — 准备报告

基线：b915507（未回退） · 2026-09-26 · Docker 重启后恢复

## 一 · 前置核验（通过）

| 项 | 结果 |
|---|---|
| git | b915507 / 零未提交 |
| staging health | 200 |
| PG | 4\|0\|2（零变化） |
| 两 PR | tizhou#2 PASSED / speaktype#426 PASSED |
| A 链 | rag-live 200 |
| 真实 PR 零 finding | 确认（sast_scan OK×2、pending=0） |

## 二 · 测试 PR 方案

### 测试仓库
`nghqqa/fastapi-boilerplate-demo`（公开、已授权、非主仓）

### 测试分支
`test/cwe22-canary-20260926`（从 main 或默认分支创建）

### Fixture
已知 CWE-22 路径穿越代码（非现有 PR finding，不伪造）：

```python
# test_fixture/vulnerable_endpoint.py
import os
from fastapi import APIRouter

router = APIRouter()

@router.get("/files/{file_path:path}")
async def read_file(file_path: str):
    base = "/app/uploads"
    full = os.path.join(base, file_path)  # CWE-22: no containment
    return open(full).read()
```

### 完整绑定元数据

```json
{
  "repo": "nghqqa/fastapi-boilerplate-demo",
  "branch": "test/cwe22-canary-20260926",
  "base_sha": "<main HEAD at creation>",
  "test_head_sha": "<after fixture commit>",
  "finding_fingerprint": "sha256:<CWE-22 file+lines+rule>",
  "run_id": "run-real-canary-cwe22",
  "ticket_id": "tkt-real-canary-<hash>",
  "rollback_commit": "<base_sha>"
}
```

## 三 · 工作流设计

```
1. 创建测试分支 + commit CWE-22 fixture     [需授权]
2. 创建测试 PR                              [需授权]
3. Review Agent GET /pulls → PR 详情         [只读]
4. skill 调用 → sast_scan → finding + receipt [只读]
5. 票据创建（人工门）→ PENDING               [系统]
6. Fixer 生成 patch（realpath containment）   [隔离→需授权后 push]
7. patch digest + 绑定校验                   [系统]
8. Verifier 独立验证（测试结果来自 harness）   [系统]
9. patch commit + push 到测试分支            [需授权]
10. PR 更新（diff 可见）                     [需授权]
11. 人工审查 → 人工 approve/reject           [仅人工]
12. 人工 merge 或关闭 PR + 删除测试分支       [仅人工]
```

### Console 集成
- `/api/pending` 显示 ACTION_REQUIRED（票据 PENDING）
- `/api/pulls` 显示测试 PR（在 allowlist 中）
- `/api/evidence` 显示 skill receipts
- `/api/audit` 显示 gate 决策
- Console /overview 显示阶段变化

## 四 · 真实 PR 零变化确认

| PR | sast | pending | stage | 变化 |
|---|---|---|---|---|
| tizhou#2 | OK, 0 findings | 0 | PASSED | **零** |
| speaktype#426 | OK, 0 findings | 0 | PASSED | **零** |

## 五 · 逐项人工授权请求

| # | 项目 | 允许？ | 说明 |
|---|---|---|---|
| 1 | 创建测试分支 `test/cwe22-canary-20260926` | □ 是 □ 否 | 在 nghqqa/fastapi-boilerplate-demo |
| 2 | Commit CWE-22 fixture 到测试分支 | □ 是 □ 否 | 仅此一个 commit |
| 3 | Push 测试分支到 GitHub | □ 是 □ 否 | 首次 push |
| 4 | 创建测试 PR（base=main, head=test 分支） | □ 是 □ 否 | 标题含 [CANARY-TEST] |
| 5 | Fixer patch commit 到测试分支 | □ 是 □ 否 | 修复 CWE-22 |
| 6 | Push 修复 commit | □ 是 □ 否 | 第二次 push |
| 7 | 更新测试 PR（评论说明修复） | □ 是 □ 否 | 供审查者参考 |
| 8 | 人工 approve 测试 PR | □ 是 □ 否 | 仅人工操作 |
| 9 | 人工 merge 测试 PR | □ 是 □ 否 | 仅人工操作（禁止自动） |
| 10 | 有效期 | ＿＿＿＿ | 建议 24h |
| 11 | 回滚 | 删除分支 + 关闭 PR | git push origin --delete |

### 明确禁止
- **自动 merge**（设计禁止）
- **自动 approve**（设计禁止）
- 修改现有真实 PR（#426/#2）
- 修改非测试分支

---

**在获得逐项授权前，不执行任何 GitHub 写入。**
**沉默、查看报告或模糊回复均不构成授权。**

## 判定

**REAL_PR_FIX_VERIFY_CANARY_AUTHORIZATION_READY**
