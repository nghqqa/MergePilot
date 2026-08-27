# Phase 7 · 真实 HiClaw/AgentTeams 链路（本次为 BLOCKED_RUNTIME 存证）

## 裁决
MERGEPILOT_AGENTLOOP_PHASE7_REAL_AGENTTEAMS_BLOCKED_RUNTIME（L3 现值：BLOCKED_CONFIGURATION——e2e 外部 Matrix 资源待重开）

## 首个真实阻塞（2026-08-27 实测·已更新两层）

层1(已修复并实测生效)：snapshot_worker 无密码 LOGIN vs DSN 契约 → cli db_prepare 现按同密码 ALTER ROLE（通用修复，日志见 CREATE ROLE/ALTER ROLE rc=0）。
层2(当前首败,未诊断完)：种子完成后 gh_bootstrap 阶段无类型 INTERNAL_ERROR；首错已保留,未重试。
栈 postgres 仅挂 init.sql，缺 M4-F1 迁移对象；--m4f 启动断言 FATAL「M4-F1 migration/API 未就绪」→ controller 退出 → 会话自动回滚（postgres/gateway healthy）。修复=把 m4f1_state.sql(+hotfix) 接入栈初始化（产品级一行改动，待确认后实施）。三次尝试完整日志在 D:/goai/temp/phase7-start*.txt（仓库外）。模型调用=0，伪造 span=0。

## 分类账
- VERIFIED: 7.1 接线(worker env 透传/cli 父回挂/GenAI 防伪造挂载点/清单装载器)；7.2 离线门禁 121/121(含 M6A 回归)；原 5 个 bind 测试复跑 5/5 通过
- NOT_VERIFIED: 真实四 Agent 链路、Matrix 多跳同 Trace 实测、AgentLoop AI Agent 语义页(未见到语义视图前恒为 NOT_VERIFIED)、LLM/Tool 语义 span(未发生真实调用,零伪造)
- BLOCKED_RUNTIME: 六容器栈未在本会话执行——host 无 docker;运行时位于 WSL 发行版 MergePilot-Test(已停,内含 docker 29.1.3);一键启动套件 46 用例就绪
- BLOCKED_CONFIGURATION/CONNECTIVITY/TRACE/REDACTION/EVALUATION: 未触发

## 可恢复步骤
1. wsl -d MergePilot-Test 启动发行版; 2. 在栈环境注入五变量(endpoint/认证头值不入库);
3. 跑 tests/isolated_live/test_one_click_startup.py 全量; 4. 合成 PR 触发 Manager→Reviewer→Fixer→Verifier;
5. 用 poc_evaluators.load_manifest 对 span-manifest 出双板; 6. 按本目录文件名回填真实数据并重算 SHA256SUMS。

## L3（2026-08-27 追加）
正确载体判定为 --github-e2e（本地 gh-proxy，无需外网）。typed 门禁生效：GITHUB_E2E_PREREQUISITES_INCOMPLETE -> 已复用历史 20 键 provision 至 state；tuwunel 容器已不存在，需按 rewire-real 剧本重开外部资源后重跑。诊断钩子新增：未知异常 redact 后写 owned diagnostics-traceback.txt（本轮凭它捕获 L2 NameError 真身）。