# L2 修复方案 — 需人工审批

## F-1 (L2) · 硬编码生产密钥

| Field | Value |
|-------|-------|
| **file** | `user_service.py` line 3 |
| **severity** | critical |
| **action** | needs-approval |
| **idempotency_key** | fix-iso5-f1 |

### 根因

`sast-scan` 实测命中:行 3 在源码中明文包含 OpenAI 风格生产 API key
`sk-live-1234567890abcdef`，该密钥暴露在版本控制中。

### 风险

- 密钥已被 git 历史追溯，任何有仓库访问权限者可获取
- 若该密钥在生产环境有效，可能导致未经授权的 API 调用和费用消耗

### 修复步骤

1. **立即吊销**该密钥(联系平台管理员或通过控制台撤销 `sk-live-*`)
2. 删除源码中的字面量赋值
3. 改用环境变量读取:
   ```python
   API_KEY = os.environ["API_KEY"]
   ```
4. 在生产部署中通过 CI/CD secrets / 密钥管理服务注入 `API_KEY`
5. 可选:添加启动时检查,若环境变量缺失则抛出明确错误

### 预期 diff

```diff
- API_KEY = "sk-live-1234567890abcdef"
+ import os
+ API_KEY = os.environ["API_KEY"]
```

### 审批人建议

Security team + 基础设施负责人审批,确认密钥已吊销后再合并。

---

## F-2 (L2) · 硬编码凭证 API_KEY

| Field | Value |
|-------|-------|
| **file** | `user_service.py` line 3 |
| **severity** | critical |
| **action** | needs-approval |
| **idempotency_key** | fix-iso5-f2 |

### 根因

与 F-1 同一行、同一问题。`API_KEY` 的赋值使用字符串字面量,任何获得源码副本者均可提取凭证。

### 修复步骤

与 F-1 相同:
1. 删除硬编码行
2. 添加 `import os`(如尚未导入)
3. 改为 `API_KEY = os.environ["API_KEY"]`
4. 确保 CI/CD 环境变量配置正确

### 审批人建议

同 F-1。

---

## F-3 (L2) · SQL 注入

| Field | Value |
|-------|-------|
| **file** | `user_service.py` line 7 |
| **severity** | critical |
| **action** | needs-approval |
| **idempotency_key** | fix-iso5-f3 |

### 根因

行 7 使用字符串拼接构造 SQL:
```python
conn.execute("SELECT * FROM users WHERE name='" + name + "'")
```
当 `name` 包含单引号(如 `' OR 1=1 --`)时,可操纵查询逻辑,绕过认证或泄露全部数据。`sast-scan` 实测确认。

### 修复步骤

将拼接改为参数化查询(使用 `?` 占位符):
```python
conn.execute("SELECT * FROM users WHERE name = ?", (name,))
```

### 预期 diff

```diff
- return conn.execute("SELECT * FROM users WHERE name='" + name + "'").fetchall()
+ return conn.execute("SELECT * FROM users WHERE name = ?", (name,)).fetchall()
```

### 风险说明

- 需与 F-4(L1 上下文管理器)和 F-5(L0 错误处理)的改动协调,确保最终代码一致
- 如 F-4/F-5/L0L1 PR 先合并,后续此改动的冲突范围极小(仅该行)
- 强烈建议 L2 审批通过后,在已合入 L0L1 修复的 `feature/m1-e2e` 上另开分支提交

### 审批人建议

需 security review 确认改动覆盖所有可能的注入面。建议同步检查仓库中其他 SQL 操作模式。
