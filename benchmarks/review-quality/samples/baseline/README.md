# bench-samples 固定干净基线

本目录是 benchmarks/review-quality 六组配对样本（CASE-01..06）的公共基底。
只含本 README 与 package.json 两个文件——样本的缺陷版/正确版分支都在此基础上
**新增** src/ 与 tests/ 文件，因此 diff 天然只含目标文件。

设计动机：trial 波（rc26-release）Q1-Q3 的 LLM 证据因「分支继承内容与目标缺陷混杂」
而不可归因（见 r3work/rc26-release/trial/QUALITY-REPORT.md §三）。本基线从源头
消除该问题：基线内容钉 sha256 进 manifest.json，任何漂移在校验阶段即失败。
