# review-quality 评测报告 — 20261015T0452-candidate-deepseek-chat-bench-sev-cal-v2

- 时间：2026-10-15T04:52:10.014Z
- 模式：candidate；模型：deepseek-chat；提示词版本：bench-sev-cal-v2
- 执行入口：candidatePrompt(same sanitize/validate contract)
- 样本清单版本：f784526ab64b8462；分类规则版本：rq-rules-v2

## 一、执行前样本校验

| 样本 | 校验 | 失败类别 | 明细 |
|---|---|---|---|
| CASE-01 | 通过 | — | defect 测试失败 / fixed 测试通过 / 目标文件在 diff 中 / 规格完备 |
| CASE-02 | 通过 | — | defect 测试失败 / fixed 测试通过 / 目标文件在 diff 中 / 规格完备 |
| CASE-03 | 通过 | — | defect 测试失败 / fixed 测试通过 / 目标文件在 diff 中 / 规格完备 |
| CASE-04 | 通过 | — | defect 测试失败 / fixed 测试通过 / 目标文件在 diff 中 / 规格完备 |
| CASE-05 | 通过 | — | defect 测试失败 / fixed 测试通过 / 目标文件在 diff 中 / 规格完备 |
| CASE-06 | 通过 | — | defect 测试失败 / fixed 测试通过 / 目标文件在 diff 中 / 规格完备 |

## 二、逐样本运行明细

| 样本 | 变体 | 调用 | 发现数 | 分类结果 | 输入覆盖 | 耗时ms |
|---|---|---|---|---|---|---|
| CASE-01 | defect | ok | 2 | #1 true_defect(期望P2/评P1)<br>#2 general_suggestion | 目标已入载荷(blocks 1/lines 26) | 3184 |
| CASE-01 | fixed | ok | 0 | — | 目标已入载荷(blocks 1/lines 26) | 964 |
| CASE-02 | defect | ok | 2 | #1 true_defect(期望P2/评P1)<br>#2 true_defect(期望P2/评P3) | 目标已入载荷(blocks 1/lines 23) | 2965 |
| CASE-02 | fixed | ok | 2 | #1 false_positive<br>#2 general_suggestion | 目标已入载荷(blocks 1/lines 24) | 2473 |
| CASE-03 | defect | ok | 1 | #1 true_defect(期望P2/评P1) | 目标已入载荷(blocks 1/lines 22) | 2086 |
| CASE-03 | fixed | ok | 0 | — | 目标已入载荷(blocks 1/lines 23) | 1312 |
| CASE-04 | defect | ok | 1 | #1 true_defect(期望P1/评P1) | 目标已入载荷(blocks 1/lines 23) | 2367 |
| CASE-04 | fixed | ok | 1 | #1 false_positive | 目标已入载荷(blocks 1/lines 23) | 2343 |
| CASE-05 | defect | ok | 1 | #1 true_defect(期望P1/评P1) | 目标已入载荷(blocks 1/lines 26) | 2297 |
| CASE-05 | fixed | ok | 1 | #1 false_positive | 目标已入载荷(blocks 1/lines 26) | 2104 |
| CASE-06 | defect | ok | 1 | #1 true_defect(期望P2/评P2) | 目标已入载荷(blocks 1/lines 21) | 1867 |
| CASE-06 | fixed | ok | 0 | — | 目标已入载荷(blocks 1/lines 21) | 1097 |

## 三、发现逐条评价（五类）

**CASE-01 / defect**（real-candidate）

| # | 位置 | 严重度 | 摘要 | 分类 | 规则 | 说明 |
|---|---|---|---|---|---|---|
| 1 | src/q1.js:3 | P1 | 倒序循环条件 `i > 0` 使索引 0 的元素（首元素）永远不会被访问，因此 reverseJoin(['a','b' | true_defect | CLA-TRUE-DEFECT | 目标文件 src/q1.js 上命中登记缺陷标记；严重度期望P2/评P1 |
| 2 | tests/q1.test.js:1 | P3 | 测试文件使用顶层 assert 与 console.log，未使用测试框架；断言失败时仅抛异常，缺少用例名称与聚合报告， | general_suggestion | CLA-SUGGESTION | 建议措辞且无强缺陷断言——加固/改进建议（对齐 trial §六先例） |

**CASE-02 / defect**（real-candidate）

| # | 位置 | 严重度 | 摘要 | 分类 | 规则 | 说明 |
|---|---|---|---|---|---|---|
| 1 | src/q2.js:2 | P1 | initials(name) 直接调用 name.trim()，未对 null/undefined 做防护。测试用例 i | true_defect | CLA-TRUE-DEFECT | 目标文件 src/q2.js 上命中登记缺陷标记；严重度期望P2/评P1 |
| 2 | src/q2.js:2 | P3 | split(/\s+/) 对含前导/尾随空白的输入（如 ' John Doe '）会产生空字符串元素，w[0] 为 un | true_defect | CLA-TRUE-DEFECT | 目标文件 src/q2.js 上命中登记缺陷标记；严重度期望P2/评P3 |

**CASE-02 / fixed**（real-candidate）

| # | 位置 | 严重度 | 摘要 | 分类 | 规则 | 说明 |
|---|---|---|---|---|---|---|
| 1 | src/q2.js:3 | P3 | initials 对空白字符串（如 '   '）会先通过 !name 检查，trim() 后得到空串，split(/\s | false_positive | CLA-FALSE-POSITIVE | 正确版上断言登记缺陷（缺陷已不存在）——按误报计 |
| 2 | tests/q2.test.js:1 | P3 | 测试文件使用顶层 assert 与 console.log，未使用测试框架；一旦断言失败会直接抛错终止，无法报告后续用例 | general_suggestion | CLA-SUGGESTION | 建议措辞且无强缺陷断言——加固/改进建议（对齐 trial §六先例） |

**CASE-03 / defect**（real-candidate）

| # | 位置 | 严重度 | 摘要 | 分类 | 规则 | 说明 |
|---|---|---|---|---|---|---|
| 1 | src/q3.js:2 | P1 | pct(part, total) 在 total 为 0 时执行 part/0，得到 Infinity 或 NaN（0/ | true_defect | CLA-TRUE-DEFECT | 目标文件 src/q3.js 上命中登记缺陷标记；严重度期望P2/评P1 |

**CASE-04 / defect**（real-candidate）

| # | 位置 | 严重度 | 摘要 | 分类 | 规则 | 说明 |
|---|---|---|---|---|---|---|
| 1 | src/q4.js:2 | P1 | inRange 使用严格不等号 v > min && v < max，排除了等于边界的情况，即区间为开区间 (min,  | true_defect | CLA-TRUE-DEFECT | 目标文件 src/q4.js 上命中登记缺陷标记；严重度期望P1/评P1 |

**CASE-04 / fixed**（real-candidate）

| # | 位置 | 严重度 | 摘要 | 分类 | 规则 | 说明 |
|---|---|---|---|---|---|---|
| 1 | src/q4.js:2 | P2 | PR 标题声称『范围边界排除了等于（不含边界）』，但实现使用 v >= min && v <= max，为闭区间（包含边 | false_positive | CLA-FALSE-POSITIVE | 正确版上断言登记缺陷（缺陷已不存在）——按误报计 |

**CASE-05 / defect**（real-candidate）

| # | 位置 | 严重度 | 摘要 | 分类 | 规则 | 说明 |
|---|---|---|---|---|---|---|
| 1 | src/q5.js:5 | P1 | async 函数 double 内部调用 delayed(x * 2, 10) 但未 return 也未 await，函 | true_defect | CLA-TRUE-DEFECT | 目标文件 src/q5.js 上命中登记缺陷标记；严重度期望P1/评P1 |

**CASE-05 / fixed**（real-candidate）

| # | 位置 | 严重度 | 摘要 | 分类 | 规则 | 说明 |
|---|---|---|---|---|---|---|
| 1 | src/q5.js:5 | P3 | 标题声称修复了“async 函数漏 return（恒 undefined）”，但当前代码 `return delayed | false_positive | CLA-FALSE-POSITIVE | 正确版上断言登记缺陷（缺陷已不存在）——按误报计 |

**CASE-06 / defect**（real-candidate）

| # | 位置 | 严重度 | 摘要 | 分类 | 规则 | 说明 |
|---|---|---|---|---|---|---|
| 1 | src/q6.js:2 | P2 | String.prototype.replace 传入字符串参数时只替换第一个匹配项，因此 slugify('a b c | true_defect | CLA-TRUE-DEFECT | 目标文件 src/q6.js 上命中登记缺陷标记；严重度期望P2/评P2 |

## 四、指标（显式分子/分母，mock 与真实分列）

### real-candidate：real-candidate（候选提示词校准对照，同一 sanitize/schema 契约）

| 指标 | 分子/分母 | 明细 |
|---|---|---|
| 调用完成率 | **12/12** | 无失败 |
| 目标缺陷命中率（缺陷版） | **6/6** | 命中: CASE-01, CASE-02, CASE-03, CASE-04, CASE-05, CASE-06；漏报: — |
| 正确版误报数 | **3/6**（误报条数/正确版run数） | CASE-02: 1; CASE-04: 1; CASE-05: 1 |
| 发现准确性（precision） | **7/10** = true_defect/(true_defect+false_positive) | 五类计数: true_defect=7, duplicate_report=0, false_positive=3, ambiguous_spec=0, general_suggestion=2 |
| 严重度一致性 | 精确 3/7；容差±1内 7/7 | 无超容差偏差 |
| 输入覆盖核对 | **12/12**（有coverage记录/ok run） | 目标文件入载荷: 12/12；截断 run: 0 |

## 五、边界与限制

- 本入口直连生产审查函数 callLlmReviewer（llm_assist 路径），未经完整栈（webhook/DB）；token 计数生产入口不返回，如实记 null。
- 分母仅含调用成功的 run；调用失败逐条列出（调用完成率），漏报按 covered_but_missed / target_not_covered 分档。

---
生成：benchmarks/review-quality（结果全量保留于 results/20261015T0452-candidate-deepseek-chat-bench-sev-cal-v2/；禁止反复运行直到命中）
