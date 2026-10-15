# 真实模型评测登记（rc.35.5 收口波，2026-10-15）

授权通道：生产同源 `callLlmReviewer` / 直连候选提示词（同 sanitize/validate 契约），
模型=deepseek-chat（部署级既有授权配置，凭据只在容器进程 env，未读取/未转录）。
样本=六组缺陷/正确配对（manifest sha256 钉死，干净检出校验门通过后运行）。

## 运行记录（全量保留，append-only；未挑选）

| run | 模式 | 调用完成 | 目标命中 | 干净误报 |
|---|---|---|---|---|
| 20261015T0451-real-deepseek-chat-llm-w31-v1 | real（生产基线提示词） | **12/12** | **6/6** | **10/6**（CASE-01:4、02:1、03:2、04:2、05:1? 见 result.json per_case） |
| 20261015T0452-candidate-deepseek-chat-bench-sev-cal-v2 | candidate（severity-calibration-v2） | **12/12** | **6/6** | **3/6**（01:0、02:1、03:0、04:1、05:1、06:0） |

事故登记：首次 real 运行（20261015T0440 前后）12 调用全部完成但结果落盘失败
（docker cp 目录 root 属主 EACCES）——调用记录未持久化即丢失，已修复属主后重跑；
该失败本身未产生可保留工件，如实登记于此（不补造数据）。

## 逐项复核（自动分类 + 人工核对）

### 缺陷变体（六组）

| case | 期望严重度（provenance） | 基线定级 | 候选定级 | 复核结论 |
|---|---|---|---|---|
| CASE-01 循环边界 | P2（registered_by_analogy） | P1@q1:3 | P1@q1:3 | 命中✓；两者均高于期望（偏严方向），分类 true_defect 正确 |
| CASE-02 空输入崩溃 | P2（trial_qualitative——trial 曾评 P3 属偏差） | P1@q2:2 | P1@q2:2 | 命中✓；候选/基线均 P1，较期望偏严但**消除了 trial 的 P3 偏差** |
| CASE-03 除零 | P2（trial_qualitative） | P2@q3:2 ✓ | P1@q3:2 | 命中✓；基线符合期望；候选偏严 |
| CASE-04 条件边界 | P1（trial_measured） | P1@q4:2 ✓ | P1@q4:2 ✓ | 命中✓ 定级精确一致 |
| CASE-05 async 缺 return | P1（trial_measured） | P1@q5:5 ✓ | P1@q5:5 ✓ | 命中✓ 定级精确一致 |
| CASE-06 replace-first | P2（trial_measured） | P2@q6:2 ✓ | P2@q6:2 ✓ | 命中✓ 定级精确一致 |

### 正确变体（六组）

- 基线在全部 6 个正确变体上共报 10 条发现（多数 P3 噪声 + 1 条 P2@CASE-04 误报）
  ——全部自动分类 false_positive/general_suggestion，逐项核对无误分类。
- 候选仅 3 条（全部 P3/general 级），且 CASE-01/03/06 三个正确变体**零发现**。
  逐项核对：3 条均为风格/健壮性建议级，不构成缺陷误报。

## 结论（校准面，不自动采纳）

- 召回无回退：两种提示词均 6/6 命中目标。
- 干净误报：候选 3 vs 基线 10（-70%），且消除了 P2 级误报（基线 CASE-04/fixed 有 P2 误报）。
- 严重度校准：trial 波的 P3 偏差（CASE-02/03）在两提示词下均已消除；
  候选把 CASE-01/02/03 统一抬到 P1（较登记期望 P2 偏严一档——偏严方向的偏差，
  是否采纳由维护者决定；采纳需另走生产提示词变更流程，本波不改生产提示词）。
- 局限：单模型（deepseek-chat）、单次运行、样本量 6——指标不外推为总体质量承诺。
