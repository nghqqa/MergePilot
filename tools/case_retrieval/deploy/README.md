# case_retrieval 部署接线（本地准备，**尚未部署**）

状态（2026-09-24）：本目录只是接线准备。controller/reviewer 容器**没有**任何
`MERGEPILOT_CR_*` 环境变量（已实测）；本目录的示例与校验脚本未经真实部署验证。
不向正在运行的 r3work 或 controller 注入配置——启用需单独授权（见"缺口"）。

## 需要注入的两个变量

| 变量 | 含义 | 缺失/不一致时的行为（已实现，fail-closed） |
|---|---|---|
| `MERGEPILOT_CR_PG_DSN` | case 库连接串（`postgresql://user:pass@host:5432/db`） | 缺失 → `CASE_RETR_DB_UNAVAILABLE`（明确失败，**不回退全库检索**） |
| `MERGEPILOT_CR_REPO_SCOPE` | 检索 repo 范围（owner/name） | 缺失 → `CASE_RETR_SCOPE_MISSING` |
| `MERGEPILOT_CR_REPO_SCOPE_FILE`（推荐的透传通道） | 指向**桥编写的 run-context.json** 的路径 | 文件缺位/作者不符/形状非法 → `CASE_RETR_SCOPE_MISSING` |

**scope 的唯一可信来源**：gh_bridge 在派发边界 write-once 写出的
`shared/projects/<proj>/run-context.json`（`authored_by: "gh_bridge"`，
`code.repo` 为检索范围）。不接受来自模型输出或请求参数的 scope；
文件作者不是 `gh_bridge` 一律拒绝（skills/case_retrieval/core.py 校验）。

## 网络与挂载要点

- reviewer 容器对 run-context 的可见性来自既有 shared/ 镜像
  （MinIO `teams/elemiso-team/shared/` → 容器 `/root/.copaw-worker/<role>/shared/`，
  周期同步）。若选择"文件透传"，推荐把 `MERGEPILOT_CR_REPO_SCOPE_FILE` 指到
  该镜像路径；挂载只读化（`:ro`）并限制目录权限（仅目标文件可读）。
- case-pg（`elemiso-case-pg`）是**共享数据面**：DSN 只读账号；本目录的示例
  不含真实密码，占位符必须由部署者从本地密钥管理注入，**不进仓库、不进日志**。
- `MERGEPILOT_CR_REPO_SCOPE`（env 直填）与 scope file **同时存在时以 env 为准**；
  二者 repo 不一致属配置错误——`validate_env.py` 会拒绝启动（exit 3）。

## 启动前校验（容器内执行）

```bash
python3 /path/to/validate_env.py --mode container
# exit 0 = 就绪; 2 = DSN 缺失; 3 = scope 缺失/不一致/文件不可信; 4 = 运行上下文校验失败
```

校验器永不打印 DSN/scope 值本身，只打印变量名与判定结果（脱敏）。

## pgvector 隔离验证（2026-09-24，已通过）

一次性隔离实例 = 容器 `mp-pgvector-smoke`（镜像 `pgvector/pgvector:pg16`：
PostgreSQL 16.15 + pgvector **0.8.6**；本轮验证后已销毁，凭据文件已删除）。
`pgvector_smoke.py` **11/11**：有效 repo scope + 只读连接（reader 写入被
ReadOnlySqlTransaction 拒绝）+ pgvector 相似度查询（只命中本 scope 2 行，
stats.total_found/knowledge_base_size=2，citation.source_url/score/case_id 齐）；scope file
由 gh_bridge `run_context.build_run_context` 真实生成（**本轮修复：桥产出 repo 在顶层**，
core/validate_env 读取器已兼容）；失败矩阵（缺 DSN/缺 scope/错误作者/缺文件/scope 不一致/
无 pgvector 库）全部明确失败、零全库回退；错误信息经脱敏（不含口令片段）。
**接线验收状态**：隔离环境链路（env→scope file→只读 DSN→受限查询）全通；
正式 controller/Worker 注入仍未部署。完整查询使用 DeterministicFakeProvider
（本地确定性向量，不下载 embedding 模型，D-7 合规）。

## 缺口（需单独授权，本目录不执行）

1. controller/Worker CR 环境注入（改共享环境）；
2. reviewer/leader 容器重启与只读挂载调整（影响共享运行时）；
3. case-pg 只读账号创建（共享数据面变更）；
4. 案例级联调（依赖 1–3 完成后的一次真实 run 窗口）。
