# benchmarks/review-quality — 审查质量固定评测流水线（B 线）

对 MergePilot 模型审查（llm_assist 路径）做**可重复**的质量评测：固定配对样本 →
执行前校验 → 生产审查入口 → 发现五类分类 → 显式分子/分母指标 → 证据报告。
先例定性来自 trial 波两轮手工评估（r3work/rc26-release/trial/QUALITY-REPORT.md），
本目录把该定性固化为流水线，**不新造缺陷、不虚报结果**。

## 目录结构

```
benchmarks/review-quality/
├── manifest.json            # 样本清单：全部文件钉 sha256（基线不漂移，漂移即拒跑）
├── samples/
│   ├── baseline/            # 固定干净基线（样本分支在其上新增文件，diff 只含目标文件）
│   └── CASE-01..06/         # 六组配对样本（↔ trial Q1..Q6，内容逐字复用）
│       ├── case.json        # 规格：预期行为 + 评价规则（缺陷标记/期望严重度+来源）
│       └── files/           # src/qN.defect.js / src/qN.fixed.js / tests/qN.test.js
├── rules/classification.json # 五类分类规则（可评审、可版本化，当前 rq-rules-v2）
├── prompts/
│   ├── baseline.json        # 生产 SYSTEM_PROMPT 登记基线（llm-w31-v1，逐字）
│   └── candidates/          # 候选提示词（校准面——是否采纳由主代理决定）
├── fixtures/mock-profile-trial.json # mock 剧本（镜像 trial 定性，显式标 mock）
├── lib/                     # validate-samples / run-review / classify / metrics / report / results-store
├── bin/run-bench.mjs        # 一键入口
├── test/                    # 单测+E2E（44 例，全 mock/注入 fetch，零真实网络）
└── results/                 # 运行留痕（全量保留，含失败；禁止反复运行直到命中）
    ├── <run_id>/            # result.json / validation.json / metrics.json / report.md
    ├── baseline-trial-rc26-20261014/  # trial 波合格证据登记复用（来源与限制见其 README）
    └── index.jsonl          # 运行索引（append-only）
```

## 六组样本（CASE ↔ trial Q）

| case | 缺陷 | 期望严重度（provenance） |
|---|---|---|
| CASE-01 | 倒序循环漏首元素 | P2（registered_by_analogy——trial 无合格定级证据） |
| CASE-02 | null 输入 TypeError | P2（trial_qualitative——trial 测得被评 P3，属偏差先例） |
| CASE-03 | 除零得 NaN | P2（trial_qualitative，同上；trial 空commit坑已由固定基线消除） |
| CASE-04 | 范围边界不含 | P1（trial_measured——run 8883edfe） |
| CASE-05 | async 漏 return | P1（trial_measured——run f56e249f） |
| CASE-06 | replace 只换首个 | P2（trial_measured——run 1c7edcf2） |

每组样本：缺陷版与正确版都从同一固定干净基线分叉（真实 diff，且 diff 只含目标文件）；
独立测试证明缺陷版失败、正确版通过（校验门强制，无判别力即拒绝评测）。

## 如何跑

```bash
# mock（零网络自证，结果显式标 mock——只证明流水线，不代表模型能力）
node benchmarks/review-quality/bin/run-bench.mjs --mode mock

# real（生产基线入口 callLlmReviewer——与 trial 波同一 llm_assist 审查函数）
MU_LLM_PROVIDER=openai_compatible \
MU_LLM_BASE_URL=<授权模型 OpenAI 兼容端点> \
MU_LLM_API_KEY=<凭据> \
MU_LLM_MODEL=<授权模型，trial 波口径 deepseek-chat> \
node benchmarks/review-quality/bin/run-bench.mjs --mode real

# 候选提示词对照（校准面：同输入、同出站裁剪/输出 schema，仅 system 指令不同；
# 对比结果输出给主代理评估，不得为触发审批抬严重度，不触碰生产 SYSTEM_PROMPT）
node benchmarks/review-quality/bin/run-bench.mjs --mode candidate --prompt severity-calibration-v2

# 单测（全 mock，零网络）
node --test benchmarks/review-quality/test/*.test.mjs
```

可选参数：`--samples CASE-01,CASE-04`（默认全部六组）、`--results-dir`、`--work-dir`。

**真实运行前置条件**：能访问授权模型的 OpenAI 兼容端点 + 凭据（env 四件套见上）。
未配置时执行器以退出码 4 拒绝并打印前置条件——绝不伪造结果。
完整栈路径（Gitee mergepilot-test webhook→run→DB 读回，trial 波机制）留作待办，
先例脚本见 r3work/rc26-release/trial/build-samples.py。

## 指标口径（全部显式分子/分母，mock 与真实分列）

| 指标 | 分子 / 分母 | 备注 |
|---|---|---|
| 调用完成率 | 调用 ok 的 run / 全部 run | 失败逐条列 code |
| 目标缺陷命中率 | 有 ≥1 true_defect 的缺陷版 run / 调用成功且可评定的缺陷版 run | 漏报分档：`covered_but_missed`（覆盖充分但漏报）≠ `target_not_covered`（目标代码未进模型载荷——输入问题）；调用失败不进分母但显式列出 |
| 正确版误报数 | fixed 变体上 false_positive 条数 / 调用成功的 fixed run 数 | |
| 发现准确性 | true_defect / (true_defect + false_positive) | duplicate/ambiguous/general 三类单列计数，不混入分母 |
| 严重度一致性 | 精确匹配数与 ±容差内匹配数（默认 ±1）/ 有期望严重度的 true_defect 数 | 超容差偏差逐条列出 |
| 输入覆盖核对 | 有 coverage 记录的 ok run / ok run | 另记截断 run 数、目标文件入载荷 run 数 |

失败类别显式枚举（互不掩盖）：`manifest_drift / spec_incomplete / no_diff /
target_not_in_diff / defect_test_not_failing / fixed_test_not_passing / build_error /
call_failed / target_not_covered / covered_but_missed`。

## 如何加样本

1. `samples/CASE-NN/` 建 `case.json`（含 expected_behaviors + evaluation_rules：
   target_path、defect_marker_patterns、expected_severity 及其 provenance）与
   `files/{src/qN.defect.js, src/qN.fixed.js, tests/qN.test.js}`（内容可另起，测试必须
   缺陷版失败/正确版通过）；
2. 把全部新文件 sha256 补进 `manifest.json`（含 baseline_files 若基线扩展——基线
   扩展须同步所有既有样本重校验）；
3. `node bin/run-bench.mjs --mode mock --samples CASE-NN` 走一遍校验门。

## 纪律

- 结果全量保留（含失败与校验门失败），**禁止反复运行直到命中**；目录只增不覆盖。
- mock 与真实调用在结果 schema（`llm_mode`）与所有指标中分列；mock 结果绝不冒充模型能力。
- token 计数：生产入口不返回，结果如实记 null（与 `mu.agent_attempt.token_count` 同口径）。
- 分类规则与提示词变更一律走版本号（`schema_version`/`prompt_version`），历史结果不回填。
- 本目录不修改业务状态机/生产提示词/authz/schema；真实评测只用授权模型与授权测试仓库。
