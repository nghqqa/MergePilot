# M4-D · PRLifecycle 设计冻结

> 冻结日期：2026-07-31
>
> 状态：设计已冻结；实现、确定性验证和真实 fixture E2E 已通过，待独立
> 复审与发布授权；尚未提交、打标签或推送
>
> 目标 Skill：`pr-lifecycle`（Python 模块 `skills/pr_lifecycle/`）
> 后续里程碑：CaseRetrieval 单独推进，不并入 M4-D

## 1. 结论

M4-D 只工程化一个高风险写 Skill：**PRLifecycle**。它提供受控的修复 PR
创建、受控回滚 PR 创建，以及带 M3 L2 审批票据的 merge/close。它不是任意
GitHub MCP 代理，不接受工具名、任意参数、PAT、Gateway URL、角色、仓库、
基础分支或 head 分支等调用方可控信任配置。

PRLifecycle 自身唯一允许的写路径：

```text
PRLifecycle
  -> Policy Gateway（固定角色 Bearer token）
  -> github-mcp 私网 sidecar
  -> GitHub
```

禁止直接 GitHub API、直接 github-mcp、`gh`/`mcporter` 子进程、PAT、shell、
本地 git 写和绕过 M3 Policy Gateway / L2 ticket / 不可变审计。

M4-D delivery 只交付可复用 Skill，不在同一提交中迁移现有 SOUL、Controller
或 `skills/gh-mcp/*.sh`。因此发布 M4-D 不能声称“系统中的旧调用入口已经全部
切换”；后续集成必须让 worker 不再持 Gateway token，并以 PRLifecycle
sidecar/受控执行入口替换旧脚本后，才能声称系统级唯一写入口。

## 2. 继承资产与权威边界

M4-D 复用而不重写：

- M4-A 公共 Contract/runtime：Draft 2020-12、envelope、通用错误码、脱敏、
  CLI、1 MiB 输出限制。
- M3-B Policy Gateway：角色 token、path/token 一致性、deny-by-default
  工具矩阵、仓库/分支/路径约束、写前 INTENT 审计 fail-closed。
- M3-B4 L2：approval ticket、canonical payload/args hash、一次 CAS claim、
  GitHub 权威 TOCTOU、complete/fail/UNKNOWN、Outbox 与对账。
- M3-C 回滚语义：坏 merge 与 parent 必须来自 GitHub 权威状态；回滚 PR 合并
  仍走 L2；验证失败不允许盲目二次回滚。
- 已探测的生产 Gateway 上游工具 Schema：
  `create_branch`、`push_files`、`create_pull_request`、`list_branches`、
  `list_pull_requests`、`pull_request_read`、`get_file_contents`、
  `get_commit`、`list_commits`、`merge_pull_request`、
  `update_pull_request`。

保护边界：

- 不修改 `skills/common/**`。
- 不修改 `tools/policy-gateway/**`、`tools/workflow-controller/**`、
  `tools/audit-db/**` 和任何 M3 migration/evidence。
- 不修改 M4-A/B/C 已发布代码、测试、证据或标签。
- 不移动任何旧标签。
- M4-D 只新增 `skills/pr_lifecycle/`、`tests/m4d/`、
  `evidence/m4/m4d/`，发布时再最小更新项目文档和 `THIRD_PARTY.md`。

## 3. 已确认的安全缺口与冻结处理

### 3.1 不暴露低层工具透传

请求不得出现 `tool`、`args`、`command`、`argv`、Gateway URL/token、PAT、
role、repo、base branch、head branch 或任意本地路径。Core 只把冻结的高层
动作映射到固定 Gateway 工具。

### 3.2 `delete_file` 不进入 M4-D v1

现有 Policy Gateway 把 `delete_file` 列入 L2 类，但当前 ticket claim 派生只
覆盖 `merge_pull_request -> merge` 与
`update_pull_request(state) -> close`。直接开放 `delete_file` 会缺少等价的
ticket/canonical-payload/TOCTOU 链。

因此：

- M4-D v1 不调用、不列出、不封装 `delete_file`。
- 普通修复只支持文件 create/update，不支持删除。
- 自动 revert 遇到坏提交中的 `added` 文件时，在任何 GitHub 写之前
  fail-closed 为 `PRL_REVERT_DELETE_UNSUPPORTED`。
- 若未来要支持删除，必须先单独加固 Gateway/DB 的 delete ticket 语义和真实
  负向 E2E；不得在 Skill 内自行绕过。

### 3.3 同一进程不持有双角色 token

同一 PRLifecycle 实例只能由 deploy 固定为 `fixer` 或 `coordinator`，只注入
该角色的一个 token：

- `fixer`：仅允许 `ensure_fix_pr`、`ensure_revert_pr`。
- `coordinator`：仅允许 `merge_pr`、`close_pr`。

请求不能选择或切换角色。缺失、未知或动作/角色不匹配均在网络调用前拒绝。

## 4. Deploy-owned 信任配置

以下全部来自进程环境，不是请求字段：

| 变量 | 说明 |
|---|---|
| `MERGEPILOT_PRL_GATEWAY_URL` | Policy Gateway 内网地址；校验 scheme/host，无 userinfo/query/fragment |
| `MERGEPILOT_PRL_ROLE` | `fixer` 或 `coordinator`，实例启动后固定 |
| `MERGEPILOT_PRL_TOKEN` | 该固定角色的 Bearer token；不得输出、日志或落证据 |
| `MERGEPILOT_PRL_REPO` | `owner/repo`，当前任务绑定仓库 |
| `MERGEPILOT_PRL_BASE_BRANCH` | 当前任务允许的 base branch |
| `MERGEPILOT_PRL_RUN_ID` | 当前 Controller run；用于确定性分支和绑定 |
| `MERGEPILOT_PRL_RISK_LEVEL` | `L0/L1/L2`；L2 创建 PR 时强制 draft |
| `MERGEPILOT_PRL_EXPECTED_BASE_SHA` | 普通修复开始时预期 base 40-hex SHA |
| `MERGEPILOT_PRL_HMAC_KEY` | 至少 32 字节的 deploy secret，用于安全幂等绑定 |
| `MERGEPILOT_PRL_REVERT_BAD_SHA` | revert 模式下的坏 merge SHA |
| `MERGEPILOT_PRL_REVERT_PARENT_SHA` | revert 模式下经 Controller/GitHub 权威派生的 parent SHA |

配置按动作条件化校验。任何 secret 形状进入错误路径时，仍必须经 M4-A
`_finalize` 脱敏。不得复制完整 `os.environ`，不得把 token/HMAC key 传给任何
子进程；M4-D 本身不启动子进程。

## 5. 冻结业务输入

公共字段：

```json
{
  "action": "ensure_fix_pr | ensure_revert_pr | merge_pr | close_pr",
  "idempotency_key": "安全字符集、1..128"
}
```

`additionalProperties: false`，条件 Schema 精确限制每个 action 可出现的字段。
`idempotency_key` 只允许 `[A-Za-z0-9._:-]`，不得承载自由文本或凭据。

### 5.1 `ensure_fix_pr`

```json
{
  "action": "ensure_fix_pr",
  "idempotency_key": "run-stage-attempt",
  "changes": [
    {"path": "src/example.py", "content": "..."}
  ],
  "commit_message": "fix: ...",
  "pr_title": "...",
  "pr_body": "..."
}
```

冻结限制：

- 最多 32 个文件。
- 单文件 UTF-8 最大 256 KiB。
- 全部内容合计最大 1 MiB。
- 不允许空 changes、重复路径、NUL、绝对路径、`..`、空段、
  `.git/**` 或反斜杠路径。
- commit message 单行，最大 200 字符。
- PR title 最大 200 字符；body 最大 16 KiB。
- 输入 body/title 不得包含内部 marker 前缀
  `MergePilot-PRL-Marker:`。
- 无删除、rename、binary、symlink 或 submodule 语义。
- `draft`、reviewers、maintainer permission、repo/base/head/risk 均不可由请求控制。

### 5.2 `ensure_revert_pr`

```json
{
  "action": "ensure_revert_pr",
  "idempotency_key": "rollback-attempt",
  "commit_message": "revert: ...",
  "pr_title": "...",
  "pr_body": "..."
}
```

请求不携带坏 SHA、parent SHA、changed files 或恢复内容。Skill 必须：

1. 从 deploy context 取得 bad/parent SHA。
2. 读 base branch，确认 tip 精确等于 bad SHA。
3. `get_commit(bad)` 取得 changed files。
4. `list_commits(bad, perPage=2)` 交叉确认第二项为 parent SHA。
5. 只接受 `modified` 与 `removed`：
   - `modified`：取 parent 内容并覆盖恢复。
   - `removed`：取 parent 内容并重新创建。
6. `added`、renamed/copied、binary、目录、缺内容或 Schema 异常均在建分支前
   fail-closed。
7. 恢复内容仍受 32 文件 / 256 KiB 单文件 / 1 MiB 总量限制。
8. 写后逐文件回读，必须与 parent 内容一致后才能创建 draft revert PR。

revert PR 始终 draft；其后合并必须由独立 coordinator 实例执行
`merge_pr`，并由 M3 L2 ticket 固定 `merge_method=merge`。

### 5.3 `merge_pr`

```json
{
  "action": "merge_pr",
  "idempotency_key": "controller-outbox-id",
  "pull_number": 123,
  "approval_ticket": "tkt-uuid",
  "merge_method": "merge | squash | rebase",
  "commit_title": "...",
  "commit_message": "..."
}
```

- 仅 coordinator 实例。
- `approval_ticket` 必须符合 `tkt-<uuid>`，但绝不回显。
- 透传参数必须与 ticket canonical payload hash 完全一致；最终权威校验仍由
  Gateway 的 claim + TOCTOU 执行。
- 写超时/连接中断后不得盲目重试；返回 effect unknown，让 M3 UNKNOWN 对账。
- PR 已明确 merged 时只返回 `ALREADY_MERGED`，不得再次发 merge。

### 5.4 `close_pr`

```json
{
  "action": "close_pr",
  "idempotency_key": "controller-outbox-id",
  "pull_number": 123,
  "approval_ticket": "tkt-uuid"
}
```

- 仅 coordinator 实例。
- 固定映射为 `update_pull_request(state="closed")`。
- 必须走 close ticket claim/TOCTOU。
- 已 closed 且未 merged 时返回 `ALREADY_CLOSED`；merged PR 不伪装为本次 close。

## 6. 幂等与冲突模型

### 6.1 确定性分支

分支由 deploy context + HMAC 派生，调用方不能提供：

```text
fix/<run_id>-<HMAC_SHA256(hmac_key, idempotency_key)[:12]>
```

不把原始 key、代码内容 digest 或 secret-derived digest 暴露到分支名。

### 6.2 安全 payload 绑定

内部计算：

```text
id_ref  = HMAC(key, "id:" + idempotency_key)
binding = HMAC(key, canonical action payload including file contents)
```

PR body 自动加入可被生产 `github-mcp pull_request_read` 稳定回读的纯文本
HMAC marker：

```text
MergePilot-PRL-Marker: v1 id=<id_ref16> bind=<binding64>
```

调用方 body 不允许自带该 marker。HMAC key 不输出；marker 不包含原始
idempotency key、代码内容或裸 SHA-256 secret digest。不得使用 HTML 注释
承载 marker，因为生产 github-mcp 的 PR 读取结果会移除 HTML 注释。

marker 只用于幂等绑定，不是授权凭据。即使旧入口或外部用户改写 PR body，
PRLifecycle 也只会检测到 binding mismatch 并 fail-closed，不会因 marker
存在而放行写操作。

### 6.3 `ensure_*` 恢复状态机

1. **无分支**：读并验证 base SHA -> create branch -> 回读确认 branch SHA
   仍等于预期 base -> `push_files` 一次原子提交 -> 回读全部内容 -> create PR。
2. **分支存在且等于 base**：视为 create 后、push 前中断，可继续原子 push。
3. **分支已有提交、无 PR**：严格验证 head commit 文件集合及逐文件内容与本次
   payload 完全一致；一致才补建 PR，否则 `PRL_IDEMPOTENCY_CONFLICT`。
4. **已有唯一 PR**：验证 repo/base/head、marker binding 与期望完全一致；
   一致返回 `EXISTING`，否则冲突。
5. **多个匹配 PR、分页不完整、远端 Schema 不完整**：fail-closed，不猜测。

不得 force-push、不得覆盖未知 branch 状态、不得把“409/422”一律当作成功。

### 6.4 L2 effect unknown

merge/close 在上游写可能已发生但响应丢失时：

- `status=ERROR`
- subcode `PRL_EFFECT_UNKNOWN`
- `retryable=false`
- `effect_state=UNKNOWN`
- 声明已实际尝试的 side effects

merge 的成功响应若包含由 Policy Gateway/GitHub 返回的完整 40-hex
`sha`，该 SHA 是本次 merge 的权威结果，直接进入 `L2_CONFIRMED`；若没有
权威 SHA，则只进行受 deadline 约束的只读 PR 状态轮询，轮询仍无法确认才
返回上述 UNKNOWN。后续由 M3 Gateway ticket `UNKNOWN` 对账；Skill 不自行
重复消费 ticket。

## 7. 输出契约

业务 output `schema_version="1"`，核心字段：

```text
action
outcome: CREATED | EXISTING | MERGED | CLOSED |
         ALREADY_MERGED | ALREADY_CLOSED
effect_state: NOT_ATTEMPTED | ATTEMPTED | CONFIRMED | UNKNOWN
repository
base_branch
head_branch?
pull_number?
pull_url?
head_sha?
result_sha?
draft?
changed_paths[]       # 只返回路径，不返回内容或内容 digest
phases[]              # 固定枚举，不含上游原文
```

不输出：

- token、HMAC key、approval ticket、原始 idempotency key。
- file content、PR body、commit message 或上游错误正文。
- 基于秘密/代码内容的裸 digest。
- 任意未经过 allowlist 的 Gateway reason/error 文本。

生产 output 必须由独立 `output.schema.json` 校验；校验失败为
`INTERNAL_ERROR`，不得带着不合格结构返回成功。

## 8. 状态、错误与退出码

只复用 M4-A 通用错误码；细分 subcode 放入受控 message：

| subcode | 通用错误码 | retryable |
|---|---|---:|
| `PRL_INVALID_INPUT` | `INVALID_INPUT` | false |
| `PRL_LIMIT_EXCEEDED` | `INVALID_INPUT` | false |
| `PRL_TRUSTED_CONFIG_MISSING` | `DENIED` | false |
| `PRL_ROLE_ACTION_DENIED` | `DENIED` | false |
| `PRL_POLICY_DENIED` | `DENIED` | false |
| `PRL_IDEMPOTENCY_CONFLICT` | `DENIED` | false |
| `PRL_REVERT_DELETE_UNSUPPORTED` | `DENIED` | false |
| `PRL_REVERT_STATE_MISMATCH` | `DENIED` | false |
| `PRL_GATEWAY_UNAVAILABLE` | `DEPENDENCY_UNAVAILABLE` | true（仅确定未写时） |
| `PRL_EFFECT_UNKNOWN` | `DEPENDENCY_UNAVAILABLE` | false |
| `PRL_DEADLINE_EXCEEDED` | `TIMEOUT` | false |
| `PRL_INTERNAL` | `INTERNAL_ERROR` | false |
| `PRL_OUTPUT_SCHEMA_INVALID` | `INTERNAL_ERROR` | false |

成功/幂等已有结果为 `status=OK`、exit 0。写 Skill 不使用 `PARTIAL`：
多阶段中断必须返回 ERROR + phase/effect state，避免 exit 0 被下游误当完整成功。

Gateway reason code 只允许固定映射：

- 权限/票据/TOCTOU 确定性拒绝 -> `DENIED`。
- Gateway/DB/上游明确未写的不可用 -> `DEPENDENCY_UNAVAILABLE`、可重试。
- 写后未知 -> `PRL_EFFECT_UNKNOWN`、不可盲重试。
- 未知 reason 或非预期 Schema -> fail-closed，不回显原文。

## 9. Deadline、网络与 side effects

- 整个 Skill 共用 M4-A monotonic deadline。
- 每次 MCP 生命周期 timeout 为 `min(configured, remaining)`。
- 写操作不做自动重试；读恢复可有限重读，但必须共享 deadline 和固定上限。
- 不启动 shell/子进程，不访问本地 git/workspace，不写证据目录。
- Gateway URL/token 仅 deploy-owned；请求无法造成 SSRF。

`side_effects` 精确声明：

- 权威读已发出：`network_read`，target 为仓库，via 为 `policy-gateway`。
- 写请求发到 Gateway：`network_write`。
- Gateway 已转发、确认或 outcome unknown：`github_write`。
- Gateway 在策略层拒绝、明确未转发：不虚报 `github_write`。
- 预校验拒绝：空数组。

## 10. 实现结构

```text
skills/pr_lifecycle/
  __init__.py
  core.py
  run.py
  adapters/
    __init__.py
    policy_gateway.py
  schema/
    input.schema.json
    output.schema.json
  requirements.txt
  SKILL.md

tests/m4d/
  conftest.py
  test_contract.py
  test_fix_pr.py
  test_revert_pr.py
  test_l2.py
  test_adapter.py
  test_integration.py
  fixtures/
  run_all.sh

evidence/m4/m4d/
  README.md
  test-output-r1.txt
  test-output-r2.txt
  gateway-e2e.json
  verification.txt
```

`core.py` 仅依赖协议化/注入式 adapter，单元测试使用内存 fake，不访问网络。
`policy_gateway.py` 懒导入 MCP SDK，返回 typed result，不让 core 解析自由文本。
`run.py` 复用 M4-A `run_request` / `_safe_finalize`，生产 output 再做 Skill Schema
校验。

新增运行时依赖必须精确 pin：

```text
mcp==1.28.1
httpx==0.28.1
anyio==4.14.2
```

上述 MCP SDK 要求生产 adapter 使用 Python 3.10+。M4-A/B/C 回归和
framework-neutral core 继续在既有 Python 3.9.25 验证环境运行；真实 Gateway
adapter/E2E 使用独立 Python 3.10+ 环境，不把 M4-A 已发布基线原地升级。

发布前写入 `THIRD_PARTY.md`；不得用 `>=`。

## 11. 测试与发布门禁

### 11.1 确定性测试

至少覆盖：

- 四 action 的 Draft 2020-12 Schema 正/负向。
- 请求注入 role/repo/base/head/token/url/tool/args/command/argv 被拒。
- trusted config 缺失、非法 URL、双角色、动作/角色不匹配，且零网络调用。
- 32 文件、256 KiB、1 MiB、标题/body/路径精确边界。
- marker 注入、重复路径、NUL、反斜杠、绝对路径、`..`、`.git` 拒绝。
- 分支不存在、仅建分支、已 push 未建 PR、已有唯一 PR、多个 PR、Schema 异常。
- 同 idempotency + 同 payload 只产生一个 branch/commit/PR。
- 同 idempotency + 不同 payload fail-closed，零额外写。
- base 在 create 前/后移动、branch SHA 不符、未知 branch 内容拒绝。
- L2 缺票/伪造/篡改/过期/USED/TOCTOU mismatch 映射。
- Gateway 写前不可用与写后 effect unknown 分离。
- revert modified/removed 成功；added/rename/binary/超限在写前拒绝。
- raw token/HMAC/ticket/file content/upstream error 不进入 stdout、message、evidence。
- side_effects 在预拒绝、策略拒绝、确认写、unknown 四种状态下准确。
- output schema 对 action/outcome/字段条件做机器化约束。

测试必须连续两轮等于 `EXPECTED_PASS`，0 failed，rc 0。本轮
`EXPECTED_PASS=54`，两轮均为 54/54。

### 11.2 回归与边界

- M4-A 75/75 ×2。
- M4-B 96/96 ×2。
- M4-C 87/87 ×2。
- M4-D 54/54 ×2；真实 fixture E2E 11/11，open PR/测试分支/DB/runner
  residue 均为 0。
- M3、Policy Gateway、Controller、DB migrations、旧 Skill 与旧 evidence 零 diff。
- `py_compile`、全部 Schema meta-valid、`bash -n`、`git diff --check`、
  新文件 `--no-index --check`、无尾随空白、全 LF、无 cache/pyc。
- delivery、docs、verification 自扫描：凭据和 AI 标识 0 命中。

### 11.3 真实 GitHub E2E（发布阻断，不得 SKIP）

只允许 fixture 仓库，双重保护：

- E2E policy allowlist 不含生产 `nghqqa/MergePilot`。
- runner 默认拒绝生产仓库；任何生产 E2E 需独立显式人工门，本里程碑不启用。

结构化 `gateway-e2e.json` 当前证明 11 条真实生产链场景：

1. `ensure_fix_pr` 真实生产链创建 1 branch + 1 atomic commit + 1 draft/ready PR。
2. 同请求重放只返回 EXISTING，commit/PR 数不增加。
3. 相同 idempotency、不同 payload 被拒且无额外写。
4. forbidden path、错误 role、非 allowlist repo 均被 Gateway 拒，GitHub 零副作用。
5. ticket denial 在 Gateway 侧拒绝且未触发 merge。
6. 合法 APPROVED ticket 只 merge 一次，审计恰好
   `L2_CLAIMED=1` + `L2_COMPLETE=1`。
7. 合法 APPROVED close ticket 只 close 一次，重放返回 `ALREADY_CLOSED`。
8. `ensure_revert_pr` 对 modified 文件真实恢复并创建 draft PR。
9. added-file revert 在建分支前拒绝。
10. E2E 后 fixture 0 open PR、无测试分支残留；审计记录完整。
11. 每个 envelope 的 credential scan 为 0，生产链入口为
    `python -m skills.pr_lifecycle.run`。

不得把人工 `gh`/`docker` 写结果伪装为 Skill E2E；入口必须是：

```text
python -m skills.pr_lifecycle.run
  -> core
  -> policy_gateway adapter
  -> Policy Gateway
  -> github-mcp
  -> GitHub fixture
```

## 12. 发布顺序

设计与实现阶段均不自行发布。独立复审和真实 E2E 全过后：

```text
D  M4-D delivery
   skills/pr_lifecycle/
   tests/m4d/
   evidence/m4/m4d/
   docs/M4-D-PRLifecycle设计冻结.md

tag m4d-pr-lifecycle-closed (annotated) -> D

G  docs commit
   docs/复赛路线图.md
   docs/项目状态.md
   docs/附录B-Skill清单.md
   THIRD_PARTY.md

non-force push main + new tag
```

旧标签保持原位。若实现意外需要改 `skills/common`、Policy Gateway、Controller
或 DB，必须停止并重新做窄范围设计/授权，不得混入 M4-D delivery。
