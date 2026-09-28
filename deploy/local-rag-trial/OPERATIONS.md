# LOCAL_RAG_TRIAL 运维手册（独立本地栈）

隔离合同：本项目（compose project=`local-rag-trial`）与 promote*/fxv-stage/
mp-stage/coreb 等任何既有栈**零共享**——独立网络 `local-rag-trial-net`、独立卷
前缀 `localragtrial-*`、独立端口段（console 48440 / pg 15436 / minio 9103，
全部仅绑定 127.0.0.1）。凭据为 `.env` 中的一次性本地值（模板见
`.env.example`；`.env` 已 gitignore，绝不提交、绝不复用真实凭据）。

## 启停

前置：console 镜像 COPY `console/frontend/dist`（dist 不入库，需先本地构建）：
`cd console/frontend && npm ci && npm run build`。

```bash
cd deploy/local-rag-trial
cp .env.example .env                # 首次
docker compose up -d --build        # 构建并起栈（镜像全部本地，pull never）
docker compose ps                   # 三服务 healthy
# 访问 http://127.0.0.1:48440 → 登录 → 「RAG 试验」页
docker compose down                 # 停栈（卷保留，数据不丢）
docker compose down -v              # 销毁（连卷删除，慎用）
```

## 数据面操作（全部走 console API，留审计）

- 灌语料：`POST /api/rag-trial/ingest {"repo","branch","corpus_dir":"/app/rag-corpus"}`
- 查询：`POST /api/rag-trial/query {"q","repo","branch","k"}`（六状态）
- 删除文档：`POST /api/rag-trial/delete {"repo","branch","doc_path"}`
- 索引失效：`POST /api/rag-trial/index/invalidate` → 查询将 `index_stale`，
  需 re-ingest 生成新版本行
- 索引回滚：`POST /api/rag-trial/index/rollback {"to_index_version":1}`
  （只允许回到保留窗口内的版本；窗口=最近 2 个版本）

## 授权门（fail-closed，默认拒绝；PHASE0A 起双层）

会话端 RAG 端点（query/review-aux/eval/ingest/delete/jobs/index 操作）过**组合授权门**：

1. **部署 scope 门**（`RAGTRIAL_ALLOWED_SCOPES`，`repo@branch`）：未配置/为空 →
   一律 403 `scope_not_configured`；越界 → 403 `scope_not_allowed`；
2. **会话 repo 门**（`CONSOLE_REPO_ALLOWLIST`，repo 级；compose 默认
   `nghqqa/mergepilot`，经 `RAGTRIAL_REPO_ALLOWLIST` 可覆盖）：会话授权面不含
   该 repo → 403 `repo_not_in_allowlist`（与 /api/pulls 同语义）。

有效授权 = 两层交集；请求参数/body/job payload 只能被校验、不能扩大交集。
批量 jobs 任一越权 → 整批拒绝零入队；index/invalidate、index/rollback 要求
模型现存文档仓库 ⊆ 会话授权面，否则 403 `index_op_crosses_repo_boundary`。
两路拒绝均写有界脱敏审计（不落查询正文/密钥/allowlist 原值）。
机器端点（machine/query）不经会话层：保持 HMAC 验签 + 部署 scope 原语义。

compose 以 interpolation 默认值放行唯一试验 scope
`nghqqa/mergepilot@feat/local-rag-trial`（与 e2e 常量同源）；在 `.env` 中：

- 覆盖：`RAGTRIAL_ALLOWED_SCOPES=my/repo@main,my/repo@dev`（逗号分隔多 scope）
- 全拒绝：`RAGTRIAL_ALLOWED_SCOPES=`（显式置空，改后 `docker compose up -d` 重建生效）
- 会话授权面：`RAGTRIAL_REPO_ALLOWLIST=my/repo,other/org`（逗号分隔；缺省=试验 repo）

## 故障处置

- **PG 不可用**：查询返回 503 `error/pg_unavailable`，进程内存计数补齐
  error 态指标；`docker compose start pg` 恢复，数据卷完好。
- **console 重启**：会话失效重登即可；索引/审计/查询日志都在 PG，零丢失。
- **镜像回滚**：console 镜像为本分支源码本地构建（`local-rag-trial-console:local`）。
  回滚到基线 = 用基线代码重新 build+up（数据卷向后兼容，ragtrial schema 幂等；
  注意基线镜像无 /api/rag-trial/* 端点，回滚期间 RAG 试验页 404 属预期）。

## e2e（26 项断言，全新卷单跑）

```bash
node deploy/local-rag-trial/scripts/run-e2e.mjs
# 产物：evidence/local-rag-trial/<ts>/（summary/manifest/transcript/…）
# 注意：S8/D1/S14a 锚定 index_version=1→2、A1 期待全新 ingest——须在干净卷上
# 单次运行（重跑脏卷会伪影失败）；隔离验证可用 RAGTRIAL_COMPOSE_PROJECT=名
# 配合 `docker compose -p 名` 起独立 project（默认 project=local-rag-trial）。
```

## 明确不做

- 不连接 promote/生产/共享卷；不对 GitHub 做任何写操作；
- 不宣称生产级 RAG（词法级哈希嵌入，质量边界见报告）；
- 不修改 A 链（/api/rag/org-search）与 C 链（/api/cchain/*）。
