# 本地 RAG 指南（语料库 / 案例库 / 嵌入策略 / 备份恢复)

适用路径：**路径 B（源码自建栈 `deploy/local-rag-trial/`）**。预构建发行包
（v0.1.0）不含本地 RAG 端点，请先按 [QUICKSTART · 路径 B](QUICKSTART.md#path-b)
起栈。

## 定位与红线（先读）

本地 RAG 为 PR 审查提供**可引用的参考证据**，仅此而已：

- 检索结果 **只返回 reference**（文档路径 + 行号区间 + 内容 SHA256 + 模型
  digest + 索引版本），**不自动生成 finding/ticket/gate/VERIFIED**；
- 检索结果**不参与风险决策**——PR 阶段推导、门禁判定与 RAG 完全解耦；
- Fixer 的输入会剔除 RAG-only 证据；Verifier 只认 harness/test 结果，不接受
  RAG 引用作为通过依据；
- 查询永远返回六状态之一（`hit` / `empty` / `model_missing` /
  `index_stale` / `provider_unavailable` / `error`），降级**永不**伪装成
  成功；score floor 防低分冒充命中。

## 环境前提

- 路径 B 栈已启动（`docker compose ps` 三服务 healthy，console 在
  `http://127.0.0.1:48440`）；
- RAG scope 门**默认已配置**——compose 内置默认值仅放行试验 scope
  `nghqqa/mergepilot@feat/local-rag-trial`；检索自己的仓库按下节在 `.env` 覆盖。

### scope allowlist（默认已配，可覆盖）

- **默认**：`nghqqa/mergepilot@feat/local-rag-trial`（compose 插值默认值，与
  栈内 e2e 常量同源）——开箱即可查询试验语料；
- **覆盖**：`.env` 中设 `RAGTRIAL_ALLOWED_SCOPES=your-repo@main`（逗号分隔多
  scope，如 `repo-a@main,repo-b@dev`），`docker compose up -d` 重建生效；
- **全拒绝**：显式置空（`RAGTRIAL_ALLOWED_SCOPES=`）→ 所有查询 403
  `scope_not_configured`——门本身**默认拒绝**（未配置/为空即拒，这是有意的
  fail-closed 设计），撤销 env 即时生效（每次请求现读）；
- 越界 scope → 403 `scope_not_allowed` + 有界脱敏审计（仅记 repo@branch 与
  原因，不落查询文本）。

## 语料库操作

所有数据面操作走 console API（会话认证 + 审计落盘）。以下示例中
`<PASSWORD>` 取自你的 `.env` 中 `RAGTRIAL_CONSOLE_PASSWORD`（模板默认用户
`ragtrial-operator`）。

### 0. 登录取会话

```bash
BASE=http://127.0.0.1:48440
curl -s -c /tmp/mp.cookies -H 'content-type: application/json' \
  -d '{"user":"ragtrial-operator","password":"<PASSWORD>"}' \
  $BASE/api/auth/login
```

### 1. 导入语料（ingest → 自动分块 → 嵌入 → pgvector 索引）

方式一：放入语料目录（推荐）。语料目录是宿主机 `deploy/local-rag-trial/corpus/`
（容器内只读挂载为 `/app/rag-corpus`）。要求：**平铺一层**，仅 `.md`/`.txt`：

```bash
cp /path/to/your/security-policy.md deploy/local-rag-trial/corpus/
curl -s -b /tmp/mp.cookies -H 'content-type: application/json' \
  -d '{"repo":"your-repo","branch":"main","corpus_dir":"/app/rag-corpus"}' \
  $BASE/api/rag-trial/ingest
```

方式二：内联文档（适合脚本化）：

```bash
curl -s -b /tmp/mp.cookies -H 'content-type: application/json' \
  -d '{"repo":"your-repo","branch":"main","docs":[{"path":"policy/auth.md","text":"# 鉴权规范\n..."}]}' \
  $BASE/api/rag-trial/ingest
```

行为要点：

- 分块：空行分段 + 相邻段合并 + 超长二次切分；每个 chunk 携带
  `line_start/line_end`、字符偏移、`chunk_sha256`——引用可回溯到行号；
- 幂等：重复 ingest 同内容 → 全部 `unchanged`；文档变更 → 按内容寻址重新归档；
- 原文归档：MinIO 内容寻址（sha256 键）+ 写后读回校验；
- 模型绑定：索引行携带 `model_digest`；嵌入规格变化 → digest 变 → 旧索引
  视为过期（`model_missing`），需重新 ingest；
- 空文档（无可切块内容）→ 0 chunk，不报错。

### 2. 查询（六状态）

```bash
curl -s -b /tmp/mp.cookies -H 'content-type: application/json' \
  -d '{"q":"回滚的第一步是什么","repo":"your-repo","branch":"main","k":5}' \
  $BASE/api/rag-trial/query
```

- `hit`：结果带完整引用链（`citation.doc_path/line_start/line_end/doc_sha256/model_digest/index_version`）；
- `empty`：无足够相似结果（score floor 生效），诚实空，不硬凑；
- `index_stale`：索引已失效（见下），需重新 ingest；
- `model_missing`：当前注册模型与索引 digest 不匹配；
- `provider_unavailable` / `error`：显式失败，带 `error_kind`，不伪装。

repo/branch 越界（不在 scope allowlist）→ 403 + 有界脱敏审计（仅记
repo@branch 与原因，不落查询文本）。

### 3. 删除文档

```bash
curl -s -b /tmp/mp.cookies -H 'content-type: application/json' \
  -d '{"repo":"your-repo","branch":"main","doc_path":"security-policy.md"}' \
  $BASE/api/rag-trial/delete
```

按 `repo@branch@doc_path` 精确删除；MinIO 归档对象保留（内容寻址，供审计
回溯），检索面立即不可见。

### 4. 索引版本管理（失效 / 回滚）

索引按版本推进，保留**最近 2 个版本**供回滚：

```bash
# 主动失效（如语料规范大改后强制重建）
curl -s -b /tmp/mp.cookies -X POST $BASE/api/rag-trial/index/invalidate
# → 此后查询返回 index_stale，直到重新 ingest 生成新版本

# 回滚到保留窗口内的旧版本
curl -s -b /tmp/mp.cookies -H 'content-type: application/json' \
  -d '{"to_index_version":1}' \
  $BASE/api/rag-trial/index/rollback
```

### 5. 状态与指标

```bash
curl -s -b /tmp/mp.cookies $BASE/api/rag-trial/status   # 服务/索引/语义 provider 状态
curl -s -b /tmp/mp.cookies $BASE/api/rag-trial/metrics  # 查询 P50/P95、状态计数
```

## 案例库（历史审查案例检索）——当前未启用

「案例库」指历史 PR 审查案例的语义检索（C 链 `skill_case_retrieval`）。
**当前版本未启用**，属 Enterprise Roadmap，前置缺口：

1. 受控模型缓存通道（operator 离线获取 + SHA256 manifest + 签名审批）；
2. provider 元数据 attestation（`case_provider_metadata` 表 + 在线合同测试）；
3. RUN_BINDING_AUTH 密钥分发（生成/分发/轮换/撤销机制）。

在上述缺口关闭前，任何文档、接口或 UI 都不得声称案例检索可用。当前可用的
检索能力全部在上方「语料库操作」（本地语料 reference-only）。

## 嵌入策略

### 默认：`local-hash-v1`（确定性哈希嵌入）

- 256 维，sha256 token 投影，L2 归一；中文=单字+二元组，拉丁=小写词；
- **零模型下载、零外部依赖、离线可用**——这是默认策略的原因；
- 确定性：同文本永远同向量，适合作为回归基线；
- 能力边界：无语义泛化（同义改写召回有限）。生产质量评估请用仓库内
  QA 集（`corpus/qa-set.json`，20 题 Recall@5）自测，勿外推他人指标。

### 可选：`bge-m3` 语义嵌入（8C16G 档，试用）

`bge-m3`（BAAI，MIT 许可，1024 维，多语 100+）已通过本仓库中文质量门
`zh-gate-v1`（R@5≥0.90、空准确≥0.75、引用=行号回溯=1.0），混合打分参数
固化为 `MODEL_CONFIGS v1`（向量权重 0.7 / IDF 词法 / 地板 0.35）。

接入要求（全部满足才生效，缺一即 fail-closed）：

1. **模型工件自带**：你自行获取 bge-m3 权重（约 2.3GB 磁盘），生成逐文件
   SHA256 manifest；仓库不提供、也不会替你下载任何模型；
2. **embed sidecar 自建**：参考实现见 `deploy/rag-prod/bge-sidecar/`
   （Python，构建期锁定 numpy/scipy/tokenizers 版本，服务 `/embed` 与
   `/health`）；
3. **console 侧配置**（加入 `docker-compose.override.yml`）：

```yaml
services:
  console:
    environment:
      RAGTRIAL_EMBED_ENDPOINT: "http://bge-sidecar:8080/embed"
      RAGTRIAL_EMBED_MODEL_ID: "bge-m3"
      RAGTRIAL_EMBED_DIMS: "1024"
      RAGTRIAL_EMBED_TIMEOUT_MS: "30000"        # CPU 前向慢，放宽超时
      RAGTRIAL_EMBED_EXPECTED_MANIFEST: "<manifest sha256>"  # fail-closed 校验
```

未配置 `RAGTRIAL_EMBED_ENDPOINT` → `status` 如实报告语义链路
`BLOCKED`（不伪装）；manifest 不匹配 → 拒绝启用。切换嵌入策略后旧索引
因 `model_digest` 不匹配需重新 ingest。

### 不支持的用法

- 不做任何自动模型下载（无内置下载通道，这是设计决定）；
- 不接受明文 API key（仅 `RAGTRIAL_EMBED_API_KEY_REF` 引用名）；
- 远程语义 provider 不可达 → `provider_unavailable`，显式失败。

## 备份与恢复

三块数据，分别处理：

### 1. 语料源文件（宿主机）

`deploy/local-rag-trial/corpus/` 是普通目录，纳入你自己的备份方案即可
（git 管理或定期归档）。

### 2. PG（索引 + 审计 + 查询日志）

```bash
# 备份（栈运行中即可）
docker compose exec pg pg_dump -U postgres ragtrial > ragtrial-$(date +%Y%m%d).sql

# 恢复演练（临时 pgvector 容器，不碰运行中的栈；镜像本地已有）
docker run -d --name rag-restore-drill -e POSTGRES_PASSWORD=restore-test pgvector/pgvector:pg16
sleep 5   # 等 initdb 完成（healthcheck 更稳：pg_isready 轮询）
docker exec -i rag-restore-drill psql -U postgres -c "CREATE DATABASE ragtrial"
docker exec -i rag-restore-drill psql -U postgres -d ragtrial < ragtrial-YYYYMMDD.sql
# 抽查行数（表在 ragtrial schema 下）
docker exec rag-restore-drill psql -U postgres -d ragtrial \
  -c "SELECT COUNT(*) FROM ragtrial.documents; SELECT COUNT(*) FROM ragtrial.chunks;"
docker rm -f rag-restore-drill
```

（恢复目标也可以是新栈的 `localragtrial-pgdata` 卷；本仓库已用临时
pgvector 容器完成过 dump→恢复演练。）

### 3. MinIO（原文归档卷）

```bash
# 备份
docker run --rm -v local-rag-trial_localragtrial-miniodata:/data:ro -v "$PWD":/backup alpine \
  tar czf /backup/miniodata-$(date +%Y%m%d).tar.gz -C /data .

# 恢复（栈停止后）
docker compose down
docker run --rm -v local-rag-trial_localragtrial-miniodata:/data -v "$PWD":/backup alpine \
  sh -c "cd /data && tar xzf /backup/miniodata-YYYYMMDD.tar.gz"
docker compose up -d
```

（卷名前缀 `local-rag-trial_` 为 compose project 名 + 卷名，可用
`docker volume ls | grep localragtrial` 确认。）

### 回滚

console 镜像为本分支源码本地构建（`local-rag-trial-console:local`）。回滚
到某个基线 = 用该基线代码重新 `docker compose up -d --build`；数据卷向后
兼容（ragtrial schema 幂等）。注意旧基线镜像可能无 `/api/rag-trial/*`
端点，回滚期间 RAG 页 404 属预期。索引级回滚见上文「索引版本管理」。

## 与其他栈的关系

本栈（compose project `local-rag-trial`）与其他 MergePilot 栈**零共享**：
独立网络、独立卷前缀、独立端口段（全部仅绑 127.0.0.1）。不要把本栈的卷或
网络接到其他栈；也不要在其他栈上调用本文档的 API 路径——端点存在性以
`GET /api/rag-trial/status` 实测为准。
