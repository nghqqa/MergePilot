---
name: sast-scan
description: 对代码做真实静态分析(密钥泄漏 / SQL 注入 / 危险调用 / 依赖漏洞),输出结构化 findings。审查代码时调用。
---

# SASTScan · 真实静态分析

对代码做**真实静态分析**(确定性工具,非 LLM 推理):正则密钥检测 + AST 注入检测 + 危险调用 + 依赖漏洞。

## 何时调用
**每次审查代码,第一步先跑这个 skill**,拿到工具实测的 findings,再结合你的判断补其它维度。

## 如何调用
1. 把要审查的代码写到临时文件(Python 代码写 `/tmp/review_target.py`;依赖写 `/tmp/requirements.txt`;可放同一目录如 `/tmp/review/`)
2. 执行:`python3 skills/sast-scan/scan.py /tmp/review/`(对目录跑,或对单个文件跑)
3. 读取 stdout 的 JSON:`{"findings":[...], "count":N}`

## 输出字段(每条 finding)
- `category`:security / quality
- `severity`:critical / high / medium / low
- `risk_level`:L0(低) / L1(中) / L2(高)
- `file`、`line`、`description`、`suggestion`

## 要求(关键)
- **工具实测的 findings 作为审查结论的基础**——工具报了就算数,别凭"感觉没问题"覆盖掉。
- 你可以补充工具没覆盖的维度(规范、测试覆盖、业务逻辑),但**安全类(密钥/注入/危险调用/依赖)以工具结果为准**。
- 把工具的 description/suggestion 整理进你给 coordinator 的 findings 里,标注"由 sast-scan 实测"。
