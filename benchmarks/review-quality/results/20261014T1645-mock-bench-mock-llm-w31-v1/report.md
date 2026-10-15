# review-quality 评测报告 — 20261014T1645-mock-bench-mock-llm-w31-v1

- 时间：2026-10-14T16:45:02.437Z
- 模式：mock（脚本化发现，零网络——仅证明流水线，不代表模型能力）；模型：bench-mock；提示词版本：llm-w31-v1
- 执行入口：scripted findings (schema-validated, no network)
- 样本清单版本：f784526ab64b8462；分类规则版本：rq-rules-v2
- **mock 声明：脚本化发现（非模型输出）**

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
| CASE-01 | defect | ok | 2 | #1 true_defect(期望P2/评P2)<br>#2 general_suggestion | 目标已入载荷(blocks 1/lines 26) | 0 |
| CASE-01 | fixed | ok | 0 | — | 目标已入载荷(blocks 1/lines 26) | 0 |
| CASE-02 | defect | ok | 1 | #1 true_defect(期望P2/评P3) | 目标已入载荷(blocks 1/lines 23) | 0 |
| CASE-02 | fixed | ok | 1 | #1 false_positive | 目标已入载荷(blocks 1/lines 24) | 0 |
| CASE-03 | defect | ok | 1 | #1 true_defect(期望P2/评P3) | 目标已入载荷(blocks 1/lines 22) | 0 |
| CASE-03 | fixed | ok | 0 | — | 目标已入载荷(blocks 1/lines 23) | 0 |
| CASE-04 | defect | ok | 2 | #1 true_defect(期望P1/评P1)<br>#2 false_positive | 目标已入载荷(blocks 1/lines 23) | 0 |
| CASE-04 | fixed | ok | 0 | — | 目标已入载荷(blocks 1/lines 23) | 0 |
| CASE-05 | defect | ok | 2 | #1 true_defect(期望P1/评P1)<br>#2 duplicate_report | 目标已入载荷(blocks 1/lines 26) | 0 |
| CASE-05 | fixed | ok | 0 | — | 目标已入载荷(blocks 1/lines 26) | 0 |
| CASE-06 | defect | ok | 1 | #1 true_defect(期望P2/评P2) | 目标已入载荷(blocks 1/lines 21) | 0 |
| CASE-06 | fixed | ok | 0 | — | 目标已入载荷(blocks 1/lines 21) | 0 |

## 三、发现逐条评价（五类）

**CASE-01 / defect**（mock）

| # | 位置 | 严重度 | 摘要 | 分类 | 规则 | 说明 |
|---|---|---|---|---|---|---|
| 1 | src/q1.js:3 | P2 | 倒序循环条件为 i > 0，index 0 的首元素永远不会参与拼接，结果缺少第一个元素 | true_defect | CLA-TRUE-DEFECT | 目标文件 src/q1.js 上命中登记缺陷标记；严重度期望P2/评P2 |
| 2 | src/q1.js:1 | P3 | 建议对 items 参数增加数组类型校验，非数组输入会抛异常 | general_suggestion | CLA-SUGGESTION | 建议措辞且无强缺陷断言——加固/改进建议（对齐 trial §六先例） |

**CASE-02 / defect**（mock）

| # | 位置 | 严重度 | 摘要 | 分类 | 规则 | 说明 |
|---|---|---|---|---|---|---|
| 1 | src/q2.js:2 | P3 | name 为 null 时直接调用 trim 会抛 TypeError，缺少空值防护 | true_defect | CLA-TRUE-DEFECT | 目标文件 src/q2.js 上命中登记缺陷标记；严重度期望P2/评P3 |

**CASE-02 / fixed**（mock）

| # | 位置 | 严重度 | 摘要 | 分类 | 规则 | 说明 |
|---|---|---|---|---|---|---|
| 1 | src/q2.js:2 | P2 | 当 name 为非字符串对象时仍可能抛异常，防护不完整 | false_positive | CLA-FALSE-POSITIVE | 目标文件上的缺陷断言但与登记缺陷标记不符 |

**CASE-03 / defect**（mock）

| # | 位置 | 严重度 | 摘要 | 分类 | 规则 | 说明 |
|---|---|---|---|---|---|---|
| 1 | src/q3.js:2 | P3 | total 为 0 时除法产生 NaN，Math.round(NaN) 仍为 NaN，污染下游统计 | true_defect | CLA-TRUE-DEFECT | 目标文件 src/q3.js 上命中登记缺陷标记；严重度期望P2/评P3 |

**CASE-04 / defect**（mock）

| # | 位置 | 严重度 | 摘要 | 分类 | 规则 | 说明 |
|---|---|---|---|---|---|---|
| 1 | src/q4.js:2 | P1 | inRange 使用严格比较，min 与 max 边界值本身被排除在范围外，边界语义错误 | true_defect | CLA-TRUE-DEFECT | 目标文件 src/q4.js 上命中登记缺陷标记；严重度期望P1/评P1 |
| 2 | tests/q4.test.js:5 | P2 | 测试断言边界值应包含在范围内，与当前实现冲突，测试必然失败 | false_positive | CLA-FALSE-POSITIVE | 缺陷断言指向 diff 外/非目标文件（tests/q4.test.js） |

**CASE-05 / defect**（mock）

| # | 位置 | 严重度 | 摘要 | 分类 | 规则 | 说明 |
|---|---|---|---|---|---|---|
| 1 | src/q5.js:5 | P1 | async 函数 double 缺少 return，await double(21) 恒为 undefined 而非 4 | true_defect | CLA-TRUE-DEFECT | 目标文件 src/q5.js 上命中登记缺陷标记；严重度期望P1/评P1 |
| 2 | src/q5.js:4 | P2 | double 未返回内部 Promise，调用方拿到 undefined | duplicate_report | CLA-DUPLICATE | 与第 1 条正确缺陷同位置(±3)同类别 |

**CASE-06 / defect**（mock）

| # | 位置 | 严重度 | 摘要 | 分类 | 规则 | 说明 |
|---|---|---|---|---|---|---|
| 1 | src/q6.js:2 | P2 | replace 传字符串参数只替换首个空格，多个空格时产出非法 slug | true_defect | CLA-TRUE-DEFECT | 目标文件 src/q6.js 上命中登记缺陷标记；严重度期望P2/评P2 |

## 四、指标（显式分子/分母，mock 与真实分列）

### mock：mock（脚本化发现，零网络——仅证明流水线，不代表模型能力）

| 指标 | 分子/分母 | 明细 |
|---|---|---|
| 调用完成率 | **12/12** | 无失败 |
| 目标缺陷命中率（缺陷版） | **6/6** | 命中: CASE-01, CASE-02, CASE-03, CASE-04, CASE-05, CASE-06；漏报: — |
| 正确版误报数 | **1/6**（误报条数/正确版run数） | CASE-02: 1 |
| 发现准确性（precision） | **6/8** = true_defect/(true_defect+false_positive) | 五类计数: true_defect=6, duplicate_report=1, false_positive=2, ambiguous_spec=0, general_suggestion=1 |
| 严重度一致性 | 精确 4/6；容差±1内 6/6 | 无超容差偏差 |
| 输入覆盖核对 | **12/12**（有coverage记录/ok run） | 目标文件入载荷: 12/12；截断 run: 0 |

## 五、边界与限制

- mock 数据：脚本化模拟发现，非模型输出——指标仅证明流水线正确性，不代表模型审查能力。

---
生成：benchmarks/review-quality（结果全量保留于 results/20261014T1645-mock-bench-mock-llm-w31-v1/；禁止反复运行直到命中）
