# RAG-IMAGE-SYNC 只读调查（2026-09-24 复核轮）

## 根因与差异面
- worker 镜像 `agentteams/copaw-worker:223ddc2-agentloop-v5fix` 内
  `/opt/mergepilot/skills/case_retrieval/core.py` = `38f159977a86…`（**无**
  `MERGEPILOT_CR_REPO_SCOPE_FILE` 回退）
- repo `skills/case_retrieval/core.py` = `e754551ba156…`（**有**回退，16979B）
- case_retrieval 目录其余 7 个 .py **零差异** → 最小同步面 = **core.py 单文件**

## 最小修复方案（待批准后执行）
1. 用 repo 版 core.py 重建 copaw-worker 镜像（tag 递增，如 `-v6scope`；本地构建，无需外部 registry）；
2. `agt update worker --name reviewer --image <新tag>`（官方 CLI；CR spec.env 保留——上一轮已验证
   env 随 CR 持久化、随容器重建注入）；
3. worker 重建后复核：env×2、scope file、`validate_env` 同款容器内校验、
   skill_case_retrieval 试探（隔离查询，只读）。
回滚：`agt update worker --image agentteams/copaw-worker:223ddc2-agentloop-v5fix` + 重启（旧 tag 本地仍在）。
影响面：reviewer 单 Worker 重建（分钟级）；leader/Matrix/MinIO/gateway 不动。

## 边界
镜像重建/替换属独立部署授权 → 本任务 **WAITING_HUMAN**；本轮零构建、零替换、零容器文件手改。
