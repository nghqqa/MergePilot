# review-quality 评测报告 — 20261015T0451-real-deepseek-chat-llm-w31-v1

- 时间：2026-10-15T04:51:32.399Z
- 模式：real（生产基线入口 callLlmReviewer，llm_assist 路径）；模型：deepseek-chat；提示词版本：llm-w31-v1
- 执行入口：callLlmReviewer(production llm_assist path)
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
| CASE-01 | defect | ok | 2 | #1 true_defect(期望P2/评P1)<br>#2 general_suggestion | 目标已入载荷(blocks 1/lines 26) | 2798 |
| CASE-01 | fixed | ok | 4 | #1 false_positive<br>#2 false_positive<br>#3 general_suggestion<br>#4 general_suggestion | 目标已入载荷(blocks 1/lines 26) | 3658 |
| CASE-02 | defect | ok | 3 | #1 true_defect(期望P2/评P1)<br>#2 true_defect(期望P2/评P3)<br>#3 general_suggestion | 目标已入载荷(blocks 1/lines 23) | 2744 |
| CASE-02 | fixed | ok | 2 | #1 false_positive<br>#2 general_suggestion | 目标已入载荷(blocks 1/lines 24) | 2002 |
| CASE-03 | defect | ok | 2 | #1 true_defect(期望P2/评P2)<br>#2 true_defect(期望P2/评P3) | 目标已入载荷(blocks 1/lines 22) | 2006 |
| CASE-03 | fixed | ok | 3 | #1 false_positive<br>#2 false_positive<br>#3 general_suggestion | 目标已入载荷(blocks 1/lines 23) | 3116 |
| CASE-04 | defect | ok | 1 | #1 true_defect(期望P1/评P1) | 目标已入载荷(blocks 1/lines 23) | 1249 |
| CASE-04 | fixed | ok | 3 | #1 false_positive<br>#2 false_positive<br>#3 general_suggestion | 目标已入载荷(blocks 1/lines 23) | 2659 |
| CASE-05 | defect | ok | 1 | #1 true_defect(期望P1/评P1) | 目标已入载荷(blocks 1/lines 26) | 1343 |
| CASE-05 | fixed | ok | 2 | #1 false_positive<br>#2 general_suggestion | 目标已入载荷(blocks 1/lines 26) | 2149 |
| CASE-06 | defect | ok | 1 | #1 true_defect(期望P2/评P2) | 目标已入载荷(blocks 1/lines 21) | 1334 |
| CASE-06 | fixed | ok | 2 | #1 false_positive<br>#2 false_positive | 目标已入载荷(blocks 1/lines 21) | 3058 |

## 三、发现逐条评价（五类）

**CASE-01 / defect**（real）

| # | 位置 | 严重度 | 摘要 | 分类 | 规则 | 说明 |
|---|---|---|---|---|---|---|
| 1 | src/q1.js:3 | P1 | 倒序循环条件 `i > 0` 会在 i 等于 0 时提前终止，导致索引 0 的首元素永远不会被拼接进结果。例如 reve | true_defect | CLA-TRUE-DEFECT | 目标文件 src/q1.js 上命中登记缺陷标记；严重度期望P2/评P1 |
| 2 | tests/q1.test.js:3 | P2 | 测试文件使用顶层 assert 断言并在末尾 console.log，未使用测试框架（如 node:test / moc | general_suggestion | CLA-SUGGESTION | 建议措辞且无强缺陷断言——加固/改进建议（对齐 trial §六先例） |

**CASE-01 / fixed**（real）

| # | 位置 | 严重度 | 摘要 | 分类 | 规则 | 说明 |
|---|---|---|---|---|---|---|
| 1 | src/q1.js:3 | P3 | 倒序循环 `for (let i = items.length - 1; i >= 0; i--)` 在 items 为 | false_positive | CLA-FALSE-POSITIVE | 目标文件上的缺陷断言但与登记缺陷标记不符 |
| 2 | src/q1.js:4 | P3 | 使用 `out ? sep : ''` 判断是否添加分隔符，依赖 out 的字符串真值。当 items 首元素（倒序后首 | false_positive | CLA-FALSE-POSITIVE | 正确版上断言登记缺陷（缺陷已不存在）——按误报计 |
| 3 | src/q1.js:5 | P3 | `items[i]` 直接参与字符串拼接，若元素为对象、null、undefined 等非字符串类型，会隐式调用 toS | general_suggestion | CLA-SUGGESTION | 建议措辞且无强缺陷断言——加固/改进建议（对齐 trial §六先例） |
| 4 | src/q1.js:1 | P3 | 新增文件权限为 100755（可执行位），但 src/q1.js 是模块文件而非可执行脚本，tests/q1.test. | general_suggestion | CLA-SUGGESTION | 建议措辞且无强缺陷断言——加固/改进建议（对齐 trial §六先例） |

**CASE-02 / defect**（real）

| # | 位置 | 严重度 | 摘要 | 分类 | 规则 | 说明 |
|---|---|---|---|---|---|---|
| 1 | src/q2.js:2 | P1 | initials(name) 直接调用 name.trim()，未对 null/undefined 做防护。测试用例 i | true_defect | CLA-TRUE-DEFECT | 目标文件 src/q2.js 上命中登记缺陷标记；严重度期望P2/评P1 |
| 2 | src/q2.js:2 | P3 | 对空字符串或纯空白输入，split(/\s+/) 会产生 ['']，w[0] 为 undefined，toUpperCa | true_defect | CLA-TRUE-DEFECT | 目标文件 src/q2.js 上命中登记缺陷标记；严重度期望P2/评P3 |
| 3 | src/q2.js:2 | P3 | 使用 w[0] 取首字符，对包含代理对（如 emoji、部分 CJK 扩展字符）的姓名会截断为半个码元，产生乱码。 | general_suggestion | CLA-SUGGESTION | 建议措辞且无强缺陷断言——加固/改进建议（对齐 trial §六先例） |

**CASE-02 / fixed**（real）

| # | 位置 | 严重度 | 摘要 | 分类 | 规则 | 说明 |
|---|---|---|---|---|---|---|
| 1 | src/q2.js:3 | P3 | initials 对非字符串真值（如数字、对象、数组）会调用 .trim() 抛出 TypeError；对仅含空白的字符 | false_positive | CLA-FALSE-POSITIVE | 正确版上断言登记缺陷（缺陷已不存在）——按误报计 |
| 2 | tests/q2.test.js:1 | P3 | 测试文件使用顶层 assert 与 console.log，无测试框架，断言失败会直接抛错但缺少结构化报告；且未覆盖 u | general_suggestion | CLA-SUGGESTION | 建议措辞且无强缺陷断言——加固/改进建议（对齐 trial §六先例） |

**CASE-03 / defect**（real）

| # | 位置 | 严重度 | 摘要 | 分类 | 规则 | 说明 |
|---|---|---|---|---|---|---|
| 1 | src/q3.js:2 | P2 | pct(part, total) 在 total 为 0 时执行 part/0，得到 Infinity 或 NaN（0/ | true_defect | CLA-TRUE-DEFECT | 目标文件 src/q3.js 上命中登记缺陷标记；严重度期望P2/评P2 |
| 2 | src/q3.js:1 | P3 | pct 未对入参做类型或范围校验，非数字入参会静默产生 NaN，调用方难以定位问题。 | true_defect | CLA-TRUE-DEFECT | 目标文件 src/q3.js 上命中登记缺陷标记；严重度期望P2/评P3 |

**CASE-03 / fixed**（real）

| # | 位置 | 严重度 | 摘要 | 分类 | 规则 | 说明 |
|---|---|---|---|---|---|---|
| 1 | src/q3.js:2 | P3 | `if (!total) return 0;` 使用 falsy 判断，会把 total 为 NaN、undefined | false_positive | CLA-FALSE-POSITIVE | 正确版上断言登记缺陷（缺陷已不存在）——按误报计 |
| 2 | src/q3.js:3 | P3 | `Math.round((part / total) * 100)` 未校验 part 类型；part 为字符串时会发生 | false_positive | CLA-FALSE-POSITIVE | 正确版上断言登记缺陷（缺陷已不存在）——按误报计 |
| 3 | tests/q3.test.js:1 | P3 | 测试文件使用顶层 assert 与 console.log，未使用测试框架；一旦某个断言失败，后续断言不会执行，且退出码 | general_suggestion | CLA-SUGGESTION | 建议措辞且无强缺陷断言——加固/改进建议（对齐 trial §六先例） |

**CASE-04 / defect**（real）

| # | 位置 | 严重度 | 摘要 | 分类 | 规则 | 说明 |
|---|---|---|---|---|---|---|
| 1 | src/q4.js:2 | P1 | inRange 使用严格不等号 v > min && v < max，排除了等于边界的情况。测试用例却断言 inRang | true_defect | CLA-TRUE-DEFECT | 目标文件 src/q4.js 上命中登记缺陷标记；严重度期望P1/评P1 |

**CASE-04 / fixed**（real）

| # | 位置 | 严重度 | 摘要 | 分类 | 规则 | 说明 |
|---|---|---|---|---|---|---|
| 1 | src/q4.js:2 | P2 | PR 标题声称『范围边界排除了等于（不含边界）』，但实现使用 v >= min && v <= max，为闭区间（包含边 | false_positive | CLA-FALSE-POSITIVE | 正确版上断言登记缺陷（缺陷已不存在）——按误报计 |
| 2 | src/q4.js:1 | P3 | inRange 未对 min > max 的非法区间做处理，也未校验参数类型；当 min > max 时函数恒返回 fa | false_positive | CLA-FALSE-POSITIVE | 目标文件上的缺陷断言但与登记缺陷标记不符 |
| 3 | src/q4.js:1 | P3 | 新增源文件与测试文件权限为 100755（可执行位），对纯 JS 模块与测试文件而言通常不必要，可能带来不必要的执行权限 | general_suggestion | CLA-GENERAL | 无缺陷断言、无歧义表达、无建议措辞（兜底） |

**CASE-05 / defect**（real）

| # | 位置 | 严重度 | 摘要 | 分类 | 规则 | 说明 |
|---|---|---|---|---|---|---|
| 1 | src/q5.js:5 | P1 | async 函数 double 内部调用 delayed(x * 2, 10) 但未 return 也未 await，导 | true_defect | CLA-TRUE-DEFECT | 目标文件 src/q5.js 上命中登记缺陷标记；严重度期望P1/评P1 |

**CASE-05 / fixed**（real）

| # | 位置 | 严重度 | 摘要 | 分类 | 规则 | 说明 |
|---|---|---|---|---|---|---|
| 1 | src/q5.js:4 | P3 | PR 标题声称修复“async 函数漏 return（恒 undefined）”，但当前 diff 中 double 已 | false_positive | CLA-FALSE-POSITIVE | 正确版上断言登记缺陷（缺陷已不存在）——按误报计 |
| 2 | tests/q5.test.js:3 | P3 | 测试使用顶层 IIFE 并在 catch 中 process.exit(1)，属于可接受的简单脚本式测试。但缺少对 do | general_suggestion | CLA-SUGGESTION | 建议措辞且无强缺陷断言——加固/改进建议（对齐 trial §六先例） |

**CASE-06 / defect**（real）

| # | 位置 | 严重度 | 摘要 | 分类 | 规则 | 说明 |
|---|---|---|---|---|---|---|
| 1 | src/q6.js:2 | P2 | slugify 使用 String.prototype.replace(' ', '-') 仅替换第一个空格，输入含多个 | true_defect | CLA-TRUE-DEFECT | 目标文件 src/q6.js 上命中登记缺陷标记；严重度期望P2/评P2 |

**CASE-06 / fixed**（real）

| # | 位置 | 严重度 | 摘要 | 分类 | 规则 | 说明 |
|---|---|---|---|---|---|---|
| 1 | src/q6.js:2 | P3 | slugify 仅将空格替换为连字符，未处理连续空格、首尾空格、其他空白字符（制表符/换行）及非字母数字字符，可能产生  | false_positive | CLA-FALSE-POSITIVE | 正确版上断言登记缺陷（缺陷已不存在）——按误报计 |
| 2 | tests/q6.test.js:3 | P3 | 测试文件在模块顶层直接执行断言并 console.log，未使用测试框架，断言失败会抛出未捕获异常；仅覆盖单一正常路径， | false_positive | CLA-FALSE-POSITIVE | 缺陷断言指向 diff 外/非目标文件（tests/q6.test.js） |

## 四、指标（显式分子/分母，mock 与真实分列）

### real：real（生产基线入口 callLlmReviewer，llm_assist 路径）

| 指标 | 分子/分母 | 明细 |
|---|---|---|
| 调用完成率 | **12/12** | 无失败 |
| 目标缺陷命中率（缺陷版） | **6/6** | 命中: CASE-01, CASE-02, CASE-03, CASE-04, CASE-05, CASE-06；漏报: — |
| 正确版误报数 | **10/6**（误报条数/正确版run数） | CASE-01: 2; CASE-02: 1; CASE-03: 2; CASE-04: 2; CASE-05: 1; CASE-06: 2 |
| 发现准确性（precision） | **8/18** = true_defect/(true_defect+false_positive) | 五类计数: true_defect=8, duplicate_report=0, false_positive=10, ambiguous_spec=0, general_suggestion=8 |
| 严重度一致性 | 精确 4/8；容差±1内 8/8 | 无超容差偏差 |
| 输入覆盖核对 | **12/12**（有coverage记录/ok run） | 目标文件入载荷: 12/12；截断 run: 0 |

## 五、边界与限制

- 本入口直连生产审查函数 callLlmReviewer（llm_assist 路径），未经完整栈（webhook/DB）；token 计数生产入口不返回，如实记 null。
- 分母仅含调用成功的 run；调用失败逐条列出（调用完成率），漏报按 covered_but_missed / target_not_covered 分档。

---
生成：benchmarks/review-quality（结果全量保留于 results/20261015T0451-real-deepseek-chat-llm-w31-v1/；禁止反复运行直到命中）
