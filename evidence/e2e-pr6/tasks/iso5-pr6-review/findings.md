# Review Findings: nghqqa/mergepilot-test PR#6 — user_service.py

**Branch**: feature/m1-e2e
**Reviewer**: reviewer
**Method**: gh-mcp-read → sast-scan 实测 + 人工补充

---

## F-1 (Security · Critical · L2)

| Field | Value |
|-------|-------|
| **file** | `user_service.py` |
| **line** | 3 |
| **category** | security |
| **severity** | critical |
| **risk_level** | L2 |
| **description** | OpenAI 风格生产密钥明文硬编码。由 sast-scan 实测命中。 |
| **suggestion** | 改用环境变量或密钥管理服务读取凭证。立即吊销已泄漏密钥 `sk-liv***cdef`。 |

## F-2 (Security · Critical · L2)

| Field | Value |
|-------|-------|
| **file** | `user_service.py` |
| **line** | 3 |
| **category** | security |
| **severity** | critical |
| **risk_level** | L2 |
| **description** | 硬编码凭证 `API_KEY` 赋值,密钥字面量在源码中明文暴露。由 sast-scan 实测命中。 |
| **suggestion** | 删除该行,改用环境变量 `os.environ["API_KEY"]` 读取。 |

## F-3 (Security · Critical · L2)

| Field | Value |
|-------|-------|
| **file** | `user_service.py` |
| **line** | 7 |
| **category** | security |
| **severity** | critical |
| **risk_level** | L2 |
| **description** | SQL 注入: `execute()` 使用字符串拼接(`'...WHERE name=\'' + name + '\''`),攻击者可构造恶意 name 操纵查询。由 sast-scan 实测命中。 |
| **suggestion** | 改用参数化查询: `execute("SELECT * FROM users WHERE name = ?", (name,))`。 |

## F-4 (Quality · Medium · L1)

| Field | Value |
|-------|-------|
| **file** | `user_service.py` |
| **line** | 6-7 |
| **category** | quality |
| **severity** | medium |
| **risk_level** | L1 |
| **description** | 数据库连接未使用上下文管理器,`conn` 未关闭,可能耗尽连接池。 |
| **suggestion** | 使用 `with sqlite3.connect(...) as conn:` 确保自动关闭。 |

## F-5 (Quality · Medium · L0)

| Field | Value |
|-------|-------|
| **file** | `user_service.py` |
| **line** | 5-7 |
| **category** | quality |
| **severity** | medium |
| **risk_level** | L0 |
| **description** | 无错误处理:数据库文件不存在或查询异常时函数直接抛异常,调用方无法区分错误类型。 |
| **suggestion** | 增加 try/except 并返回有意义的错误信息或自定义异常。 |

## F-6 (Convention · Low · L0)

| Field | Value |
|-------|-------|
| **file** | `user_service.py` |
| **line** | 6 |
| **category** | convention |
| **severity** | low |
| **risk_level** | L0 |
| **description** | 数据库路径 `db.sqlite` 硬编码,无法在不同环境间切换。 |
| **suggestion** | 通过配置或环境变量传入数据库路径。 |

## F-7 (Test-impact · Medium · L1)

| Field | Value |
|-------|-------|
| **file** | `user_service.py` |
| **line** | 5-7 |
| **category** | test-impact |
| **severity** | medium |
| **risk_level** | L1 |
| **description** | `get_user` 函数直接依赖真实 SQLite 文件,缺乏依赖注入或 mock 钩子,无法在无真实数据库时进行单元测试。 |
| **suggestion** | 将数据库连接以参数方式传入,或采用 Repository 模式解耦。 |

---

## Summary

| Severity | Count |
|----------|-------|
| Critical | 3 |
| Medium | 3 |
| Low | 1 |
| **Total** | **7** |

**安全维度**:3 个 critical,均为 sast-scan 实测,必须优先修复(密钥吊销 + SQL 参数化)。
**质量维度**:连接泄漏 + 无错误处理,需重构。
**规范维度**:路径硬编码,建议配置化。
**测试影响**:函数不可测,需解耦。
