# 无关文件核查（PR #233，157 文件逐一归属）

方法: numstat 逐文件 → 目录/内容归属 → 对照 R3..RPD-08 各轮 PROGRESS/STATUS 记录。

结论: **157/157 全部可归属**，零无关文件。
- r3ops/*(14): 第 R3 轮 backfill（wh-evidence 打包/OTel/kickoff 模板等，PROGRESS 第三轮）
- orchestrator/*(11): v3 骨架+PG RunStore（第七~九轮，设计分支接受架构的隔离实现）
- approval/*(9)+tests/approval(9): M2 审批规格实现+门闭环（第十、十二、三十三轮）
- integration_prep/*(9)+tests(6): R1-R3 授权计划/prerun gate/migrate 工具（第七~十轮）
- model_gateway(5)+case_retrieval/deploy(6): 模型切换准备+接线（二十六~三十五轮）
- gh-bridge(3)+tests/gh_bridge(10): CASE1/CASE2 加固+run_context+修复（二十六~三十四轮）
- console_pg+console_v3(7): 只读控制台+gate_display（十轮/三十一轮）
- costmeter(4)+rag(3)+rag_live tests(3)+skills/case_retrieval(1): 成本脚手架/RAG 工具（历史轮）
- docs/productization(22)+evidence(11)+RPD(3): 各轮记录与本轮 RPD/证据
- .gitignore/pyproject.toml(2): 历史轮/本轮收集修复

边界外文件（demo-platform/frontend/生成物/env/私钥/生产配置）: **0**。
