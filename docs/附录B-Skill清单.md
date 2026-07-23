# 附录 B · Skill 清单(MergePilot)

> Skill 是本赛题**必选项**。MergePilot 选择**自研沉淀可复用 Skill**(而非仅使用阿里云官方用云 Skills),
> 因为场景是代码/PR 域,自研 Skill 更贴合、可开源复用;阿里云用云 Skills 作为可选扩展(如触发云上 CI/部署)。
> 每个 Skill 都是**任务能力抽象层**(可被多 Agent / 多场景调用),而非一次性 Agent 行为。
>
> 字段:名称 / 用途 / 调用条件 / 入口 / 参数 / 返回 / 依赖工具 / 失败处理 / 安全边界 / 复用价值 / 版本。

---

## 0. Skill 总览表

| Skill | 用途 | 调用方 Agent | 依赖工具 | 风险关联 |
|---|---|---|---|---|
| DiffParse | 解析 PR diff,产出变更清单与影响面 | Triage | Git/GitHub MCP | — |
| RiskClassify | 变更风险分级 L0/L1/L2 | Triage, Coordinator | 规则库 + Nacos | 驱动自治策略 |
| SASTScan | 静态安全扫描 | Reviewer, Verifier | Semgrep | 安全 |
| SecretScan | 密钥/凭证泄漏检测 | Reviewer, Verifier | Gitleaks | L2 强制 |
| DepVulnCheck | 依赖漏洞检查 | Reviewer, Verifier | OSV/Trivy | L2 强制 |
| CoverageImpact | 测试覆盖影响分析 | Reviewer | 覆盖率报告 + CI MCP | 质量 |
| TestRunner | 执行测试套件 | Fixer, Verifier | CI MCP / 容器 | 验证 |
| PRCreate | 创建 fix PR/commit/评论(幂等) | Fixer | GitHub/GitLab MCP | L2 仅 draft |
| CaseRetrieval | 历史相似 PR/修复案例 RAG | Fix Planner, Reviewer | PolarDB-PG + pgvector | 知识 |
| RunbookRag | 规范/Runbook 检索 | Reviewer, Fix Planner | PolarDB-PG + pgvector | 规范约束 |
| Postmortem | 复盘报告 + 经验沉淀 | Verifier | LLM + 知识库 | 自学习闭环 |

复用关系示例:`TestRunner` 同时服务 Fixer 自测与 Verifier 验证;`SASTScan` 同时服务 Reviewer 发现与 Verifier 重扫——每个 Skill 都是跨 Agent 的能力抽象。

---

## 1. DiffParse

- **用途**:解析 PR diff,产出结构化变更清单与影响面,是所有审查的输入起点。
- **调用条件**:Triage 收到 PR 事件时;PR sync 后重新调用。
- **入口**:`diffparse.run(pr_ref, base, head)`
- **参数**:`{ pr_ref: string, base: sha, head: sha, repo: string }`
- **返回**:`{ files: [{path, change_type: A|M/D, additions, deletions}], modules_touched: [string], change_categories: [dep|secret|deletion|logic|doc], stats }`
- **依赖工具**:Git MCP(git diff)、GitHub/GitLab MCP(取 PR 元数据)
- **失败处理**:diff 拉取失败重试 3 次 → 降级用 GitHub patch API → 仍失败上报 Coordinator 标记 PR 不可分析
- **安全边界**:只读;源码明文不跨 session 缓存;大 PR(>5000 行)分块处理并告警
- **复用价值**:Triage / Reviewer / Fix Planner 共享其输出;可被任何"代码变更分析"场景复用(不限 PR)
- **版本**:v1.0;schema 走 semver,大版本变更需下游 Agent 适配

## 2. RiskClassify

- **用途**:对变更按风险分级 L0/L1/L2,驱动后续自治策略(自动修 / 需 review / 需审批)。
- **调用条件**:Triage 拿到 DiffParse 输出后;Coordinator 最终裁定前复核。
- **入口**:`riskclassify.classify(change_context)`
- **参数**:`{ change_categories, files, modules_touched, author_trust_level }`
- **返回**:`{ risk_level: L0|L1|L2, reasons: [string], gated_actions: [auto_fix|need_review|need_approval] }`
- **依赖工具**:内置规则库 + Nacos(风险策略动态下发,支持按仓库/团队定制)
- **失败处理**:规则缺失默认 L1(保守);策略拉取失败用本地缓存
- **安全边界**:分级必须可解释(reasons 非空);L2 判定需双人规则命中或显式关键字(依赖文件/密钥文件/删除)
- **复用价值**:Coordinator / Fixer / Verifier 共享同一风险语义
- **版本**:v1.0;风险策略版本独立管理,支持灰度

## 3. SASTScan

- **用途**:静态安全扫描,识别注入/XSS/反序列化等代码缺陷。
- **调用条件**:Reviewer 安全维度;Verifier 修复后重扫。
- **入口**:`sastscan.scan(target, ruleset)`
- **参数**:`{ target: diff|path, ruleset: semgrep_default|custom, languages: [] }`
- **返回**:`{ findings: [{id, rule_id, severity, file, line, message, cwe}], summary }`
- **依赖工具**:Semgrep(MCP/CLI);规则集社区或自研
- **失败处理**:引擎崩溃隔离重试 → 跳过该语言并标 partial;超时按文件分片
- **安全边界**:仅扫 PR 变更相关文件(增量扫描)减少误报与越界;规则白名单可控
- **复用价值**:Reviewer 发现阶段 + Verifier 验证阶段双用;可独立用于 commit 扫描
- **版本**:v1.0;规则集版本与引擎版本分离

## 4. SecretScan

- **用途**:检测密钥/凭证泄漏(API key、token、私钥)。
- **调用条件**:Reviewer 安全维度;DiffParse 检出疑似密钥文件时优先。
- **入口**:`secretscan.scan(diff)`
- **参数**:`{ diff, detectors: [aws, gcp, github_pat, generic], allowlist }`
- **返回**:`{ leaks: [{file, line, rule_id, redacted_preview, severity}] }`
- **依赖工具**:Gitleaks / Trivy secret;可接 MCP
- **失败处理**:误报通过 allowlist 收敛;引擎失败降级到正则规则集
- **安全边界**:输出永远 redacted,不回传完整密钥;命中即强制 L2 + 阻断合并
- **复用价值**:审查与验证双用;可独立做 pre-commit hook
- **版本**:v1.0

## 5. DepVulnCheck

- **用途**:检查依赖(锁文件/manifest 变更)引入的已知漏洞。
- **调用条件**:DiffParse 检出依赖文件变更时;Reviewer 安全维度。
- **入口**:`depvulncheck.check(manifest_changes, ecosystems)`
- **参数**:`{ files: [package.json, go.mod, pom.xml], ecosystems: [npm, go, maven] }`
- **返回**:`{ vulns: [{pkg, installed, fixed, id: CVE|OSV, severity, is_direct}], upgrade_path }`
- **依赖工具**:OSV.dev / Trivy;可封装为 MCP
- **失败处理**:漏洞库不可达用本地缓存(带 TTL);无法判定 fixed 版本标 needs_human
- **安全边界**:直接依赖高危 → 强制 L2;传递依赖按策略
- **复用价值**:Reviewer + Verifier 双用;可驱动 Dependabot 式批量升级
- **版本**:v1.0

## 6. CoverageImpact

- **用途**:分析变更的测试覆盖影响,识别"改了代码但无测试覆盖"的高风险区。
- **调用条件**:Reviewer 测试影响维度。
- **入口**:`coverageimpact.analyze(diff, coverage_report)`
- **参数**:`{ changed_lines, coverage: {file: {line: hit}} }`
- **返回**:`{ uncovered_changes: [{file, line, risk}], coverage_delta }`
- **依赖工具**:覆盖率报告(istanbul/jacoco/go cover)+ CI MCP 取报告
- **失败处理**:无覆盖率报告标 unknown,降级为"变更复杂度"启发式
- **安全边界**:只读分析;不执行任何代码
- **复用价值**:Reviewer 用;可驱动 Fix Planner 决定是否补测试
- **版本**:v1.0

## 7. TestRunner

- **用途**:执行测试套件,收集通过/失败结果。
- **调用条件**:Fixer 修复后自测;Verifier 验证阶段。
- **入口**:`testrunner.run(repo, sha, command, filter?)`
- **参数**:`{ repo, sha, command: "npm test", filter?: "受影响测试", timeout }`
- **返回**:`{ passed, failed, skipped, failures: [{test, error}], duration, artifacts_url }`
- **依赖工具**:CI MCP(GitHub Actions/GitLab CI)或本地容器执行
- **失败处理**:CI 不可达降级本地容器;超时按 suite 拆分;flaky 检测(连续失败才报)
- **安全边界**:隔离容器/CI 临时环境执行,不触碰生产;产物按 PR 隔离
- **复用价值**:Fixer + Verifier 双用;可被任何"代码变更验证"复用
- **版本**:v1.0

## 8. PRCreate

- **用途**:创建 fix commit/PR 或 review 评论,幂等。
- **调用条件**:Fixer 生成修复后;Coordinator 决定 comment-only 时。
- **入口**:`prcreate.create({type, ...})`
- **参数**:`{ repo, base_branch, head_branch, patches: [{file, content}], title, body, idempotency_key }`
- **返回**:`{ pr_url, commit_sha, status: created|skipped(existing) }`
- **依赖工具**:GitHub/GitLab MCP;HiClaw consumer-token 鉴权(不持 PAT)
- **失败处理**:冲突不强推、回报需人工;幂等键防重复;限流退避
- **安全边界**:永不 force-push;L2 审批通过前只创建 draft PR;操作写入审计日志
- **复用价值**:Fixer 用;任何"自动产出代码变更"的 Agent 都可复用
- **版本**:v1.0

## 9. CaseRetrieval(RAG)

- **用途**:检索历史相似 PR/修复案例,为根因定位与方案提供先验。
- **调用条件**:Fix Planner 规划修复时;Reviewer 判断是否已知模式。
- **入口**:`caseretrieval.search(query, top_k)`
- **参数**:`{ query: "变更语义+finding", top_k, filters: [repo, severity] }`
- **返回**:`{ cases: [{pr_url, similarity, summary, applied_fix, outcome}] }`
- **依赖工具**:PolarDB-PG + pgvector;embedding 模型;知识库 = 历史已合并 PR + 修复案例
- **失败处理**:向量库不可达降级关键词检索;低相似度(<阈值)不返回避免误导
- **安全边界**:只检索已脱敏/已合并案例;不跨租户泄露私有仓库明文
- **复用价值**:Fix Planner + Reviewer 双用;知识库随每次成功合并自增长
- **版本**:v1.0;索引 schema 版本独立

## 10. RunbookRag(RAG)

- **用途**:检索团队规范/修复 Runbook/安全基线,约束修复方案符合规范。
- **调用条件**:Reviewer 规范审查;Fix Planner 生成方案前。
- **入口**:`runbookrag.search(query, top_k)`
- **参数**:`{ query, top_k, scope: [security_policy, coding_standard, runbook] }`
- **返回**:`{ docs: [{title, section, content, source_url, version}] }`
- **依赖工具**:PolarDB-PG + pgvector;文档源 = 团队 wiki / SECURITY.md / CONTRIBUTING
- **失败处理**:文档源不可达用缓存;返回必带 source 供溯源
- **安全边界**:引用必带出处;过时文档(版本过期)降权并标注
- **复用价值**:Reviewer + Fix Planner 双用;Runbook 更新即知识更新
- **版本**:v1.0

## 11. Postmortem

- **用途**:生成复盘报告,沉淀经验入知识库。
- **调用条件**:Verifier 完成验证后(无论通过/失败/回滚)。
- **入口**:`postmortem.generate(incident_trace, outcome)`
- **参数**:`{ trace_id, findings, fixes, verification, outcome: merged|rolled_back|rejected }`
- **返回**:`{ report_md, lessons: [], reusable_case_candidate: bool }`
- **依赖工具**:LLM(经 Higress 网关);写入 CaseRetrieval 知识库
- **失败处理**:LLM 失败用模板生成骨架;写库失败入队列重试
- **安全边界**:报告不含密钥明文(脱敏);敏感信息按仓库策略过滤
- **复用价值**:Verifier 用;沉淀结果反哺 CaseRetrieval,形成自学习闭环
- **版本**:v1.0

---

## 12. Skill 工程体系(版本 / 发布 / 回滚 / 质量评估)

评分表要求 Skill 体系说明版本、发布、回滚、质量评估。MergePilot 的 Skill 工程体系:

- **版本**:每个 Skill 独立 semver;参数/返回 schema 单独版本化;策略与规则集(如 RiskClassify 风险策略、SASTScan 规则)独立版本,支持灰度下发(经 Nacos)。
- **发布**:Skill 以包形式注册到 Skill Registry(Nacos 托管 AgentSpec/Skill 元数据 + RBAC);发布走"测试仓验证 → 灰度仓库 → 全量"三段式。
- **回滚**:Skill 版本可一键回退(Registry 保留历史版本);单次调用幂等(`idempotency_key`),回滚不产生副作用残留。
- **质量评估**:每个 Skill 有 Golden/Badcase 数据集;基于可观测 Trace(经 AgentLoop)统计调用成功率、耗时、误报率,作为 Skill 迭代依据;LLM 类 Skill(如 Postmortem)用 LLM-as-Judge 评估输出质量。
- **Skill Schema**:每个 Skill 自带 `{name, version, input_schema, output_schema, auth_scope, timeout, idempotent}` 描述,供 Agent 编排层动态发现与调用——这也是后续平滑迁移 MCP 的基础(工具调用链不变,只换协议适配层)。
