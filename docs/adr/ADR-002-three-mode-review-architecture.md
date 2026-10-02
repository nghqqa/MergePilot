# ADR-002：三档可配置多 Agent PR 审查架构

- 日期：2026-10-02
- 状态：**架构定义完成，待实施**（本文只定义，不写业务代码）
- 前置：ADR-001-r2（Reviewer 保留）+ GAP-ANALYSIS（EVIDENCE_TRIAGE_ONLY 能力实证）
- 基线：main=`b01a91de84d9`（未变）
- **最终裁决：THREE_MODE_ARCHITECTURE_DEFINED**

## 0. 产品定位（既定）

MergePilot 是**自主多 Agent PR 审查与修复验证平台**。默认保护代码边界（零出站），允许组织管理员**显式选择**外部 LLM API 进行完整代码审查。人工代码审核**不是正常流程的必需步骤**——三档中最高两档（external-api/local）由 Agent 链自主完成审查，人只处理冲突升级与最终合并决策。

## 1. 三档模式

| | evidence-only（默认） | external-api | local |
|---|---|---|---|
| 代码出站 | **零** | **有**（受控 diff + 上下文，manifest 审计） | 零（私有 endpoint） |
| 审查链 | precheck → evidence triage → 终态 | precheck → **Reviewer → Leader → Fixer → Verifier** | 同 external-api（共享协议） |
| 谁能开 | 默认即此 | 组织管理员显式 opt-in + consent | 组织管理员配置私有 endpoint |
| verdict | EVIDENCE_TRIAGE_COMPLETE（无 full-code verdict） | FULL_REVIEW_COMPLETE | FULL_REVIEW_COMPLETE |
| UI 标识 | "未执行代码级 AI 审查"（明示） | 常驻徽标：Provider+模型+"代码已发送至外部模型" | 常驻徽标：私有模型+endpoint 域 |
| 能力边界 | 发现=precheck 规则集；证据复核不新增代码发现 | **Reviewer 必须能发现 precheck 未命中的问题**（读真实受控 diff） | 同左（模型能力取决于私有部署） |
| 硬件 | 无 | 无（API 调用） | 需自有推理服务（**本 ADR 只定义接口，不建设 GPU/模型部署，不阻塞 external-api 路线**） |

**evidence-only 禁止事项**：不进入完整 Reviewer/Fixer/Verifier 代码链；不生成 full-code review verdict；不得因证据复核"全确认"而暗示代码无问题。

## 2. 统一控制面：租户级 review policy

```
mu.review_policy（单行/租户，CAS+policy_version 乐观并发——复用 agent_policy 模式）：
  mode                ENUM('evidence_only','external_api','local')  DEFAULT 'evidence_only'
  provider_id         TEXT NULL     -- external 模式必填
  model_id            TEXT NULL
  provider_policy_status ENUM('verified','custom_acknowledged','blocked')
  code_egress_allowed BOOLEAN       -- mode=external_api 时必须 true（一致性由服务端校验）
  consent_version     TEXT NULL     -- external opt-in 时必填；consent 文本随版本演进
  file_allowlist      TEXT[] NULL   -- 默认空=变更文件全集；denylist 优先
  file_denylist       TEXT[] NOT NULL DEFAULT '{...凭据/密钥路径族}'
  context_budget      JSONB         -- {max_files, max_lines_per_file, max_total_bytes, max_tokens}
  retention_ack       BOOLEAN       -- custom Provider 必须确认已知悉数据条款不完整
  enabled_at/by       TIMESTAMPTZ/UUID
  policy_version      INT
```

**policy snapshot 冻结**：run 创建时把当时 policy 全字段快照进 run 行（新增列或 JSONB），运行中策略变更不影响进行中的 run；审计引用 snapshot 的 policy_version + consent_version。

禁止字段（同 agent_policy 纪律）：任何凭据形状（api_key/token/secret）不入本表——Provider 凭据仍在部署 env（deployment 级），policy 只引用 provider_id。

## 3. Provider 状态机

```
verified          MergePilot 项目已核验该 Provider 政策（零留存+不训练+DPA+地域/删除机制）
custom_acknowledged 组织管理员显式接受未知/不完整数据条款（retention_ack=true + consent）
blocked           禁止调用（默认未知 Provider 一律 blocked，fail-closed）
```

**DeepSeek 当前标定：`custom_acknowledged`（附注 restricted_experiment）**——依据 GAP-ANALYSIS 实证（零留存不可核验/无 DPA/条款 "as long as necessary"）。产品与文档**不得**对 DeepSeek 宣称零留存或不用于训练。verified 名单初始为空；入选标准与核验记录另立 `docs/provider-verification/`。

## 4. Context Builder（full-code 模式统一输入契约）

```
输入绑定（强一致四元组）：repo_id + provider_pr_number + head_sha + diff_digest
选择规则：
  默认只读变更文件（changed files）；allowlist 收窄 / denylist 排除（优先级最高）
  关联上下文：按 finding 命中符号的 import/定义处追加（按符号与引用，不按目录漫游）
排除（硬编码 + denylist 双保险）：二进制、生成文件（dist/build/vendor/node_modules）、
  lockfile、凭据路径（.env/*key*/secret*/*credential*）
脱敏：双通道 secret redaction（正则族 + 熵检测）——发送前应用
上限：{文件数, 每文件行数, 总字节, 总 token}（context_budget，超限截断并记 manifest）
fail-closed：redaction 管道抛错/超时 → 本次审查 attempt FAILED（不降级为"跳过脱敏"）
不可信边界：PR 内容（diff/路径名/commit message）一律视为 untrusted——不得包含指令语义；
  prompt 中文件内容置于明确分隔的数据区，指令区只含固定协议文本
无扩大权：模型输出不得触发追加文件读取（无工具调用读文件——上下文一次性构建）
```

**审计只保存 manifest + digest**：`{files[], lines, bytes, tokens, redactions_applied, diff_digest}`——不保存任何额外原始代码副本（DB/日志/重试队列均只此一份元数据；代码本体仅存在于发送时的瞬时请求）。

## 5. 工作流语义

```
共同入口：webhook → snapshot → policy resolver（冻结 snapshot）→ context builder → precheck
                                                                    │
                              ┌─────────────────────────────────────┴──────────────┐
                              │ evidence_only                                     │ external_api / local
                              ▼                                                   ▼
                     evidence triage（AT Reviewer，脱敏证据）              Reviewer（读受控 diff——必须能发现 precheck 未命中问题）
                              ▼                                                   ▼
                    EVIDENCE_TRIAGE_COMPLETE                          Leader（消费 findings + 代码证据引用）
                                                                                    │ fix_required
                                                                                    ▼
                                                                              Fixer（sandbox 内生成 patch artifact）
                                                                                    ▼
                                                                              Verifier（区分模型判断与真实测试）
                                                                                    ▼
                                                                             FULL_REVIEW_COMPLETE
```

**角色约束**：
- precheck：`stage='precheck'`，不冒充 Reviewer（不写 agent_role='reviewer'），产物=结构化 rule evidence
- Reviewer：findings 必须落 canonical `mu.agent_finding`（source 列区分）——禁止只存 output_digest 的成功路径（死端防回归）；full-code 模式下其新发现必须引用代码证据（path+span，可回溯到 context manifest）
- Leader：必须消费 findings **及其对应代码证据引用**（不是只看结论）；冲突规则沿用 ADR-001-r2 §5（P0/P1 reject=双记录+人工升级）
- Fixer：只能在 sandbox 中生成 patch（artifact 存储引用，不应用）；DRY_RUN 语义不变
- Verifier：输出必须区分 `model_judgment`（模型对建议的一致性意见）与 `test_evidence`（真实测试执行结果）——两字段独立，不得互写
- **任何 Agent 均无 GitHub merge 权限**（结构性禁止，维持现状）

## 6. 状态模型（独立保存，禁止合并语义）

run 行新增独立列（或等价 JSONB，实施时定）：

```
run_status            生命周期（既有状态域 + EVIDENCE_TRIAGE_COMPLETE / FULL_REVIEW_COMPLETE）
review_scope          'evidence_only' | 'full_code'（run 冻结，来自 policy snapshot）
execution_mode        'evidence_only' | 'external_api' | 'local'
review_verdict        Reviewer 链结论（仅 full_code 模式可非空）
verification_verdict  Verifier 结论（model_judgment 与 test_evidence 分列）
tests_status          真实测试执行状态（与 verification_verdict 分离）
merge_eligibility     合并资格判定（唯一可表达"能否合并"的字段）
provider_policy_status（快照）
code_egress           本 run 是否发生代码出站 + 出站次数
consent_version      （快照）
```

**禁止矩阵**：

| 禁止 | 实现手段 |
|---|---|
| evidence-only 产生 full-code verdict | review_verdict 列写入前校验 scope |
| LLM verdict 冒充测试结果 | tests_status 只能由真实测试执行器写 |
| tests passed 冒充 merge eligible | merge_eligibility 独立判定（protection+tests+review 三输入） |
| protection unknown 产生 eligible | merge_eligibility 计算硬编码保护检查 |
| 新 head 复用旧 verdict | run 按 head_sha 幂等创建（既有机制），verdict 绑定 run |

## 7. 出站审计（每次外部调用）

```
mu.code_egress_event：
  tenant_id, repo_id, run_id, attempt_id
  provider_id, model_id, provider_policy_status
  head_sha, diff_digest
  input_digest          -- 重试必须复用同一 digest（幂等键 = run+attempt+input_digest）
  files[]               -- 路径清单（仅路径）
  bytes_sent, tokens_sent
  redactions_applied
  policy_version, consent_version（run 快照引用）
  response_digest
  timeout, retry_count
禁止记录：secret、代码正文、完整 prompt/response（digest 即可追溯）
```

## 8. 前端要求

**设置页（组织管理员）**：
- 三档 Segmented control；evidence_only 默认选中
- 切到 external_api 时：显示出站说明（发什么/到哪/记什么）+ Provider 政策状态徽标 + consent 确认（记 consent_version）；**custom Provider 一律显示 "custom_acknowledged——数据条款未经核验"，不得显示为 verified**
- provider_policy_status=blocked 的 Provider 不可选

**PR 详情页**：
- 显示本 run 冻结的模式（scope+mode，来自快照——设置变更不追溯）
- evidence-only：明示"本次运行未进行代码级 AI 审查"
- external/local：显示 Provider、模型、head、上下文范围（files 数/bytes/redactions，来自 egress manifest）
- review / verification / tests / merge-eligibility **四栏分立**——不使用含义模糊的单一 PASS

## 9. 实施拆分

| PR | 范围 | 关键边界 |
|---|---|---|
| **A** | 模式枚举、review_policy 表、Provider 状态机、consent、policy snapshot、run 新状态字段 | **不发送真实代码；不改变 beta.5 现有行为**（默认档=现状语义） |
| **B** | Context Builder + 出站 manifest + egress_event | mock Provider 验证零越界（哨兵 secret/代码行断言） |
| **C** | external-api Reviewer 读真实受控 diff；findings 落 agent_finding+证据引用；Leader 消费 | DeepSeek=custom_acknowledged 路径 |
| **D** | Fixer sandbox + patch artifact | patch 不出 sandbox |
| **E** | Verifier 工具执行 + test_evidence | model_judgment/test 分列 |
| **F** | 前端：设置三档、常驻模式标识、PR 四栏分立 | custom≠verified 显示纪律 |
| **G** | 历史兼容、安全评测（prompt injection/out-of-scope）、故障恢复、跨租户 | 含历史双 reviewer run 回放 |

依赖：A→B→C→(D,E 可并行)→F→G。

## 10. 版本边界

- **beta.5 保持现状**：不追加任何架构功能（当前 RC 即终态候选）
- **beta.6 目标 = external-api 完整审查**（PR A-F 全量）
- **local 只保留接口**（provider_id/endpoint 配置面），GPU/模型部署不在 beta.6 范围，**不作为 beta.6 阻断项**
- **外部邀请前置门**（四项全过才放）：租户 opt-in 实测、出站审计实测、跨租户隔离实测、prompt injection 防护实测

## 11. 决策记录

- 三档架构取代"单一模式"讨论：ADR-001-r2 的 input_mode=A 成为**默认档**而非唯一档；GAP-ANALYSIS 的 EVIDENCE_TRIAGE_ONLY 结论成为 evidence-only 档的能力边界声明
- DeepSeek 标定 custom_acknowledged/restricted_experiment——不宣称零留存/不训练（GAP-ANALYSIS 实证依据）
- B 方案（原 ADR-001-r2）复活为 external-api 档，但前提从"项目核验 Provider"放宽为"组织管理员显式接受"（verified 与 custom_acknowledged 二分）
- r1"删除 Reviewer"维持撤回；Reviewer 在 external/local 档获得真实代码上下文（经 Context Builder）
