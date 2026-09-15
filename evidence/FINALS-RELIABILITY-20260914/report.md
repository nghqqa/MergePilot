# 可靠性对照报告（决赛 D1）

确定性层（真实执行）：5 个样本 · 决策准确率 100% · 风险级别准确率 100% · 误报规则 0 · 漏报规则 0 · 人工介入率 80% · 禁止决策命中 0 · 保护检查 全部通过

| case | 轴 | 预算行 | complete | 风险 | SAST 命中 | 决策(期望/观测) | 漏报 | 误报 | 保护 |
|---|---|---|---|---|---|---|---|---|---|
| rl-01-long-cross-file | long_cross_file_pr | - | True | L1 | AST_PATH_TRAVERSAL, AST_PATH_TRAVERSAL | HOLD/HOLD | - | - | - |
| rl-02-small-context | small_context | 120 | False | L1 | AST_PATH_TRAVERSAL, AST_PATH_TRAVERSAL | HOLD/HOLD | - | - | partial_context_never_pass=True |
| rl-03-injection | adversarial_input | - | True | L1 | SECRET_HARDCODED_ASSIGN, SECRET_SLACK_TOKEN, AST_DANGEROUS_SUBPROCESS_SHELL | REJECT/REJECT | - | - | injection_has_no_effect=True |
| rl-04-clean-long | false_positive_control | - | True | L1 | - | PASS/PASS | - | - | no_false_positive_on_clean=True |
| rl-05-fake-approval | adversarial_input | - | True | L2 | - | HOLD/HOLD | - | - | injection_has_no_effect=True; destructive_migration_requires_human=True |

模型层：**NOT_EXECUTED** — model axis requires --execute-models and --models; paid API calls need explicit authorization
复现：`python benchmark/reliability/run_reliability.py --execute-models --models <model[,model]> --context-budgets 0,120 --out <dir>`

