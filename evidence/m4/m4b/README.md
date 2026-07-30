# M4-B · diff-parse + risk-classify — 证据

本轮（M4-B）落地两个核心 Skill：**diff-parse**（真实 unified diff → 结构化变更上下文）
与 **risk-classify**（确定性、建议型、只升不降的 L0/L1/L2 风险聚合器）。两者均为纯读取 /
纯计算能力，复用 M4-A 公共 runtime，不接入真实 Agent、GitHub MCP、Policy Gateway、Nacos
或数据库。生成日期：2026-07-30。**本轮未 commit / 未 tag / 未 push。**

## 目标

在不修改 M3 与 M4-A 公共 runtime 的前提下，交付两个可复用、可测试、可版本化、框架中立的
Skill，并给出可复现证据。

## 文件范围（本轮新增）

- `skills/diff_parse/`：`__init__.py`、`core.py`（框架中立解析器，仅标准库）、`run.py`
  （复用 `skills.common.runtime.*` 的入口）、`SKILL.md`、`schema/{input,output}.schema.json`
- `skills/risk_classify/`：`__init__.py`、`core.py`（规则匹配 + 只升不降聚合，仅标准库 +
  复用 jsonschema）、`run.py`、`SKILL.md`、`schema/{input,output,rules}.schema.json`、
  `rules/risk-rules.v1.json`（版本化规则）
- `tests/m4b/`：`conftest.py`、`test_diff_parse.py`、`test_risk_classify.py`、
  `test_integration.py`、`run_all.sh`、`fixtures/*`（含 `gen_real_diffs.py` 与 manifest）
- `evidence/m4/m4b/`：本 README、`test-output-r1.txt`、`test-output-r2.txt`、`verification.txt`

**窄范围授权修改** `skills/common/runtime/cli.py`（第二轮审计授权；见下“第二轮审计修复 R2-A”）：
新增 `_emit_finalized`，使所有错误发射经 `_finalize`（脱敏 + 1 MiB 限制 + schema 校验）。这是
`skills/common/**` 内**唯一**文件改动（`run_all.sh` 有 cli.py-only 门禁 + 该文件凭据/AI 扫描）；
M4-A 75/75 ×2 回归通过，旧标签 `m4a-runtime-closed` 原位未动。其余未改：`tests/skills/**`、
`evidence/m4/m4a/**`、`THIRD_PARTY.md`、任何 M3 文件。

### 目录命名偏差（Python 导入约束）

为满足“独立入口复用 `skills.common.runtime.cli`（经 `--skills.module.func` 解析）”，两个
Skill 目录使用下划线 `skills/diff_parse/`、`skills/risk_classify/`（合法 Python 包名）；SKILL
名称仍为 `diff-parse` / `risk-classify`（见各自 `SKILL.md` 的 `name:`）。两个目录各自自包含。

## Contract 与版本

- 公共 envelope `contract_version = "1"`（M4-A，未改）。两个 Skill 业务 output 各自走自身的
  Draft 2020-12 schema，其 `schema_version = "1"`。
- 复用：`skills.common.runtime.{cli,envelope,errors,redact}` —— 不复制其代码。`run.py` 直接
  调 `run_request(...)`（fd 隔离、deadline、build_response、脱敏 + 1 MiB 限制 + schema 校验、
  退出码映射），自身只做 stdin 读取 / emit 与业务桥接。
- Skill 专属错误码：`DIFF_PARSE_UNSUPPORTED_FORMAT` / `DIFF_PARSE_INPUT_TOO_LARGE` /
  `DIFF_PARSE_MALFORMED` / `DIFF_PARSE_PARTIAL_CONTEXT`；`RISK_CLASSIFY_RULES_MISSING` /
  `RISK_CLASSIFY_RULESET_INVALID` / `RISK_CLASSIFY_RULESET_VERSION_UNSUPPORTED` /
  `RISK_CLASSIFY_INVALID_CONTEXT`（上下文结构 / 聚合不一致）。通用非法输入（业务 input schema）
  复用公共 `INVALID_INPUT`。
- 错误消息绝不回显 diff 不可信内容；两个直接入口的预校验错误路径均经 `_safe_finalize`（脱敏 +
  1 MiB 限制）后再序列化，凭据形状不会进入 stdout。

## diff-parse 支持的 diff 特性

单/多文件（含无 `diff --git` 的 plain 多文件，按 top-level `--- ` 正确切分）、A/M/D/R/C/T、
rename/copy（quoted 路径正确反转义）、多 hunk、binary（`Binary files … differ` 与
`GIT binary patch`，quoted binary 路径正确）、mode/type change、`/dev/null`、
`No newline at end of file`、Git quoted path（空格 + 八进制 unicode + **转义双引号 `\"`**，tokenize 与
`_git_unquote` 协同正确还原如 `foo" bar.bin`）、CRLF、空 diff、
malformed/truncated（fail-closed）、**hunk 计数严格校验**（body 行数多于/少于 `@@` 声明即
`DIFF_PARSE_MALFORMED`，杜绝多计 `+`/`-`）、超限（字节硬上限 ERROR；文件/行数软上限 PARTIAL）、
prompt-injection 文本（视为不可信纯文本，不执行）、secret 形状字符串（不进入 output，仅单向
digest）。输出仅含结构 / 范围 / 统计 / digest，不含源码或 patch 明文。

## risk-classify · rules_version 与规则表

- `rules_version = "1.0.0"`（`skills/risk_classify/rules/risk-rules.v1.json`），由
  `rules.schema.json` 校验；阈值写在规则文件，不散落 Python。
- 16 条声明式规则：L2 = `DEP_MANIFEST`、`WORKFLOW_CI`、`MIGRATION_SCHEMA`、
  `SECURITY_SENSITIVE_PATH`、`BINARY_FILE`、`SOURCE_DELETION`、`LARGE_CHANGE`(≥1000 行)；
  L1 = `PARTIAL_CONTEXT`、`RENAME_OR_COPY`、`MEDIUM_CHANGE`(≥201 行)、`SOURCE_CONFIG_CHANGE`、
  `DELETION`、`MULTI_FILE_CHANGE`(≥20 文件)、`UNCATEGORIZED_CHANGE`(无首要类别的文件，如
  `CODEOWNERS`，保守升至 L1)；L0 = `DOCS_ONLY_SMALL`(纯文档 ≤200 行)、
  `TEST_ONLY_SMALL`(纯测试 ≤300 行)。
- `only_categories` 要求 change_categories **非空**且为允许集子集（空集不再满足任意 only 规则，
  避免有效变更被错误降为 L0）。规则中的 `path_pattern` 在加载时编译，非法正则 →
  `RISK_CLASSIFY_RULESET_INVALID`（fail-closed，不静默“不匹配”）。

### 只升不降证明

`risk_level = max(risk_floor, 命中规则的最高等级)`。规则只能“贡献”等级、永不相减；与 floor 取
max，故结果永不低于 floor。已由测试覆盖：floor=L2 任何输入仍 L2；floor=L1 + 仅 L0 规则仍 L1；
高风险规则后出现低风险规则不降级；规则顺序打乱后等级与逐字节业务 output 完全一致。

## 测试矩阵（固定常量 EXPECTED_PASS = 96）

来源唯一：`tests/m4b/conftest.py::EXPECTED_PASS`。

- **diff-parse 41 项**：4 组真实 git diff fixture + SHA-256 校验、多文件聚合、binary、quoted
  path（含 unicode）、no-newline、mode/type change、prompt-injection 中立、CRLF、空 diff、
  whitespace-only、malformed、truncated、超限（字节/文件/行数）、unsupported format、统计自洽、
  modules_touched（根 `.`）、路径规范化、确定性、input_sha256、类别词汇与映射、secret 不外泄、
  路径穿越文本中立、output schema 校验、pr_number 可选、handle OK/PARTIAL/ERROR/非法输入；
  **审计驱动**：hunk 超额计数拒绝、plain 多文件不合并、quoted rename/copy/binary 反转义、错误消息
  不回显 secret、直接入口对凭据形状请求脱敏、**quoted 路径含转义双引号 `\"` 正确还原**。
- **risk-classify 45 项**：L0/L1/L2 正向样例、文档删除落 L1（非 L2）、只升不降 4 项负向、规则顺
  序不变、reasons/matched_rules 稳定排序、确定性、rules 缺失/JSON 损坏/缺字段/未知主版本/期望版
  本不符/重复 rule_id 全部 fail-closed、advisory 契约（无 approved/denied/merge/author_trust/
  nacos）、recommended_controls 分级、approval_recommended 仅 L2、output schema 校验、规则文件
  meta-valid、handle 非法上下文 / 拒绝 author_trust / OK；**审计驱动**：空类别文件升 L1（非 L0）、
  非法正则规则拒绝、负数统计拒绝、聚合不一致拒绝、only_categories 非空、直接入口凭据脱敏、
  **`complete_false/empty/has_uncategorized:false` 被 `const:true` 拒绝（fail-closed）**。
- **集成 10 项**：documentation→L0、source→L1、dependency→L2、migration+workflow→L2、partial
  保守升级、security→L2、source deletion→L2、全链路确定性、CLI 子进程 DiffParse→RiskClassify
  端到端（经公共 runtime）、**公共 CLI `--skill` 预校验错误路径对凭据形状 envelope 脱敏**。

满足路线图“SASTScan、RiskClassify、TestRunner 至少各覆盖 10 个样例”中对 risk-classify 的
要求（独立风险样例 >10）。

## 审计修复（独立负向审计发现的 P1/P2，已全部修复并加测试）

- **P1-A 错误路径脱敏**：直接入口预校验失败时原 `run.py` 直接 `serialize` 绕过 `_finalize`。已增
  `_safe_finalize`（脱敏 + 1 MiB 限制），凭据形状请求字段不再进入 stdout（`redactions` 记录路径）。
- **P1-B 空类别降 L0**：`CODEOWNERS` 等无类别文件因空集 `issubset` 命中 L0 规则。已修：`only_categories`
  要求非空；新增 `UNCATEGORIZED_CHANGE`(L1) 兜底。
- **P1-C fail-closed**：Schema 合法但正则非法的规则原被静默判为“不匹配”。已修：加载时编译
  `path_pattern`，非法即 `RULESET_INVALID`。上下文（负数统计 / 聚合不一致 / 未知类别 / 文件形状）
  原返回 OK/L0。已修：`_validate_context` 全量校验，不一致即 `RISK_CLASSIFY_INVALID_CONTEXT`。
- **P1-D DiffParse 畸形输入**：hunk 超额计数原 `complete=true` 多计；plain 多文件原被合并；quoted
  rename/copy 路径未反转义。已重写为单遍线性状态机：严格计数校验、plain 多文件正确切分、rename/copy/
  binary quoted 路径反转义。
- **P2-A 业务 output 生产校验**：原仅在测试中校验单样例。已在两个 `run.py` 的 `handle` 对业务 output
  强制走自身 output schema，不合格 → `INTERNAL_ERROR`。
- **P2-B/C**：补齐上述场景的负向测试；本 README 措辞按修复后事实重写。

### 第二轮审计修复（R2，已全部修复并加测试）

- **R2-A 公共 CLI 错误路径泄漏**（P1-A 的剩余项）：`skills.common.runtime.cli` 的预校验/解析失败
  原直接 `_emit(E.build_response(...))`，绕过 `_finalize`，畸形 envelope 中凭据形状（如
  `contract_version`）会进入 stdout。**经窄范围授权**新增 `_emit_finalized`，所有错误发射
  （`_emit_error`、`_read_and_validate` 2 处、`_resolve_isolated` 1 处）统一经 `_finalize`（脱敏 +
  1 MiB 限制 + schema 校验）。`run_request` 正常路径本就 `_finalize`，不受影响（不重复脱敏、不丢失
  redactions 审计轨迹）。**M4-A 75/75 ×2 回归通过**，旧标签 `m4a-runtime-closed` 原位未动；该改动
  为 `skills/common` 内**唯一**文件改动（`run_all.sh` 有 cli.py-only 窄授权门禁）。
- **R2-B quoted-path 转义双引号**：`_tokenize_paths` 原逐 `"` 翻转引号状态，遇到 git 转义的 `\"`
  会错误断词（如 `foo\" bar.bin` 被解析成长串）。改为反斜杠取下一字符字面值、不翻转引号，与
  `_git_unquote` 协同正确还原 `foo" bar.bin`（binary/文本/rename 均覆盖）。
- **R2-C 布尔谓词 `false` 静默失效**：`empty`/`complete_false`/`has_uncategorized` 原为 `boolean`，
  `false` 是 Schema 合法却永不命中的“哑规则”，可能让 L2 规则静默失效、整体降为 L0。
  `rules.schema.json` 现将其限制为 `{"type":"boolean","const":true}`，`false` 即
  `RISK_CLASSIFY_RULESET_INVALID`（fail-closed）。`binary` 保留完整布尔语义（`false` 有意义且已实现）。

## 两轮稳定运行

`tests/m4b/run_all.sh` 连续跑 pytest 两轮，分别落 `test-output-r1.txt` / `test-output-r2.txt`。
两轮均 `passed=96 failed=0 rc=0`。

## M4-A 回归

以仓库外 venv（`D:\goai\m4a-venv`，`jsonschema==4.25.1` / `pytest==8.4.2`）+
`PYTHONDONTWRITEBYTECODE=1` + `-p no:cacheprovider` 独立重跑 `tests/skills` 两轮，**75/75 ×2，0
failed**；**未运行** `tests/skills/run_all.sh`，`evidence/m4/m4a/**` 未被触碰或覆盖。

## 真实 diff fixture 来源与 SHA-256

`tests/m4b/fixtures/gen_real_diffs.py` 在临时目录建一次性 git 仓，做真实编辑后捕获
`git diff` / `git diff --cached` 的原始输出，落为静态 fixture。`fixtures-manifest.json` 记录
每个 fixture 的 SHA-256（测试断言磁盘 SHA-256 与之一致）。真实 fixture：

- `real-modified.diff`  `ddbf51d3bcc473d98b8128c04f083c453ecc75b77065e74fa846fde113a55bdd`
- `real-new-file.diff`  `243c3930ba765be2fd8f1025bcf43adb2f15afbec38f37e0552a381c7e4ecc79`
- `real-deleted.diff`   `69226a509a2f800743cc76fa549f3573bd75e4692f88926c1d847016a8f4550c`
- `real-rename.diff`    `6003b630beadef627224dd7268d04a95c76b98af03a53ecaf1a7212d44a6a0b8`

手写 fixture（确定性静态文件，覆盖边界）：`multi-file`、`binary`、`quoted-path`、`no-newline`、
`mode-change`、`prompt-injection`、`malformed`。不依赖网络或外部仓库状态。

## 权限与副作用边界

- 两个 Skill 均为纯读取 / 纯计算：无网络、无 GitHub/Gateway/DB 写、无 Nacos、无 LLM、不读取
  `policy.yaml`、不读取 diff path 指向的本地文件、不 shell 解释 diff 内容。`side_effects` 为
  空（`run.py` 不设置 side_effects；临时 git 仓由 `gen_real_diffs.py` 在系统临时目录创建，属
  测试夹具行为，不计入生产 Skill 的 side_effects）。
- diff-parse 不在 output 中复制完整源码或 patch；secret 形状字符串不进入 stdout / 证据 / 未
  脱敏 stderr（仅单向 SHA-256）。
- risk-classify `advisory_only` 恒为 `true`，永不输出 approved/denied/merge 等授权结论；
  Policy Gateway 始终是最终授权权威；不使用作者 / 团队 / 信任等级降低风险（输入 schema
  `additionalProperties:false` 拒绝 author_trust 字段）。

## 静态 / 清洁 / 扫描门禁（均通过）

依赖可导入；py_compile（全部 M4-B `.py`）；全部 schema Draft 2020-12 meta-valid；bundled
ruleset 符合 `rules.schema.json`；`git diff --check`（tracked）+ 每个新源文件
`--no-index --check`（`.diff` 数据文件排除：空白上下文行与 Makefile 制表符属 diff 数据，非代
码风格）；源文件无尾随空白；`tests/skills/scan_delivery.py` 对 delivery 0 命中（凭据 + AI 标
识）；无 `__pycache__`/`.pytest_cache`/`.pyc` 残留；`verification.txt` 自扫描 0 命中；M3 文件
零 diff；新增文本文件 LF。

## 已知限制与偏差

- diff-parse 的超时为协作式 Deadline（M4-A 公共 runtime 提供）；硬资源上限由
  `max_diff_bytes` / `max_files` / `max_total_lines` 保守约束（默认 2 MiB / 1000 / 200000）。
- 风险阈值（行数 / 文件数）保守：纯文档/测试变更超过阈值仍会按规模升级（如 ≥201 行文档→L1，
  ≥1000 行→L2），符合“只升不降 + 保守”；阈值集中于规则文件可调。
- 首要类别（source/test/doc/dep/workflow/migration/config）互斥，`security_sensitive`/
  `binary`/`deletion` 为叠加层；少量无扩展或非常规扩展文件可能无首要类别（不会触发 source
  规则，保守由 floor 兜底）。
- 本阶段 diff-parse 只接收调用方提供的 unified diff 文本，不从 GitHub 拉取；Agent 在环、
  Benchmark、GitHub/MCP 接入均不在本轮范围（分别留给 M6 / M5 / 后续）。
- **公共 CLI 预校验脱敏**：第二轮审计授权修复 `skills/common/runtime/cli.py`（`_emit_finalized`，
  所有错误发射经 `_finalize`），公共 CLI `--skill` 与两个直接入口现在对畸形请求 envelope 中的凭据
  形状均脱敏；M4-A 75/75 ×2 回归通过，`m4a-runtime-closed` 标签原位未动，`skills/common` 仅此一文件
  改动（`run_all.sh` 有 cli.py-only 窄授权门禁 + 该文件凭据/AI 扫描）。
- 未为 Skill 增加框架专用 metadata / adapter（按要求）。

## 未声明的事项

- **未**声称 risk-classify 是最终授权权威（它仅建议；Policy Gateway 才是）。
- **未**接入真实 Agent；**未**完成 M5 Benchmark；**未**完成 GitHub/MCP 集成；**未**验证未实
  际执行的场景。

## 提交状态

本轮**未 commit / 未 tag / 未 push**，未改写历史，未移动任何旧标签（30 个历史标签原位）。
工作树改动 = 4 个新顶层目录（`evidence/m4/m4b/`、`skills/diff_parse/`、`skills/risk_classify/`、
`tests/m4b/`）+ 4 个已跟踪文件修改（`skills/common/runtime/cli.py`、3 个 docs）。

### 建议发布顺序（复审已确认：标签须指向 delivery，使其快照包含 common 修复）

```
199b78d (origin/main)
  └─ H  common-runtime 加固      仅 skills/common/runtime/cli.py
       └─ D  M4-B delivery       skills/diff_parse/ skills/risk_classify/ tests/m4b/ evidence/m4/m4b/
            ↑ tag m4b-diff-risk-closed (annotated) → D
            └─ G  docs commit    docs/附录B-Skill清单.md docs/项目状态.md docs/复赛路线图.md
```

- 先提交 H（common 加固），再提交 D（delivery）于 H 之上，**标签打在 D**——这样 checkout 标签即包含
  common 修复，公共 CLI 测试不会因缺补丁而失败（若标签打在 delivery 之前会丢失 common 修复）。
- docs commit（G）位于 delivery 之上；`THIRD_PARTY.md` 不改（无新依赖）。
- 旧 `m4a-runtime-closed` 标签保持原位；如需单独记录公共层补丁可另建新标签，不得移动旧标签。
- 全部非 force push main 与新标签。
