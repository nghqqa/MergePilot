# QwenPaw 2.2.1 / AgentTeams v1.2.4 部署真源（候选栈 MANIFEST）

- 日期：2026-10-11（v1；QwenPaw 迁移评估波 B4 交付）
- 性质：QwenPaw 候选栈的**可重复部署唯一真源**（与 223ddc2 旧栈 MANIFEST.md 并列，互不覆盖）。
  凭据只以环境变量引用出现，版本库零密钥。
- 执行入口：`apply-qwenpaw-v124.sh`（首次/重装）+ `verify-qwenpaw-v124.sh`（验收）。
- 评估依据：`docs/forge/QWENPAW-MIGRATION-ASSESSMENT.md` + 隔离评估报告
  （r3work/qwenpaw-eval-20261010/REPORT.md，GO with gates）。

## 1. 组件与镜像（digest 锁定——完整值，禁止 latest/摘要前缀）

| 组件 | 镜像（higress-registry.cn-hangzhou.cr.aliyuncs.com/agentteams/） | digest |
|---|---|---|
| controller（embedded：ctrl+Tuwunel+MinIO+Higress+Element） | `agentteams-embedded:v1.2.4` | `sha256:d96eb4dd4cef0ba8f02cfea4bb114d00f248cd8ecac0b2ce909150bd3e56a289` |
| manager（qwenpaw runtime，v1.2.4 默认） | `agentteams-manager-qwenpaw:v1.2.4` | `sha256:965517c923fe09b4e7238b294bc6511f2344acf6b04ca79a6b17059995555805` |
| worker（qwenpaw 2.2.1 runtime） | `agentteams-qwenpaw-worker:v1.2.4` | `sha256:4f91b1026fe116f87c639f19e92f7e1f26f59fb408a90425691c32f700c3a5f5` |
| worker（copaw runtime，迁移源/对照） | `agentteams-copaw-worker:v1.2.4` | `sha256:25e10515a61ed7c877c1993e515a7b27064bbc87c7eab9248ae7a7c1198a147a` |
| worker（copaw 旧线，回滚锚） | `agentteams-manager:v1.2.4`（openclaw manager 对照）`sha256:41ce349d23a37125ad87ac259509f7e385c033463702dd28a47b0b7ae3e580ca` | — |

上游源码锚：AgentTeams `v1.2.4 = 8765d6973b80dda5a6064d2dee6eac3c026c8aae`；
QwenPaw `v2.2.1 = cae5773707b26ab2fd00903f84b712387894b256`（官方配对证据：
v1.2.4 源码 `manager/Dockerfile.qwenpaw` 默认 `QWENPAW_PIP_SPEC=qwenpaw==2.2.1`）。

## 2. 与旧栈（223ddc2）的配置体系差异——持久性结论

| 层 | 旧栈（223ddc2，copaw） | v1.2.4 栈（QwenPaw） |
|---|---|---|
| worker 模型配置 | ctrl 模板→MinIO `openclaw.json`→mirror+60s re-bridge；**无 durable 路径**（ctrl 周期性模板重推覆盖，recover-workers.sh 收敛而非状态） | **runtime.yaml desired state**：controller 每次投影 worker 期望（model/runtime/identity/SOUL/AGENTS/MCP 策略）到 worker 存储，worker 启动与 reconcile 均以 controller 为唯一真源——模板重推即期望态本身，不存在"覆盖用户 patch"问题（用户 patch 的正位=Higress provider 层，见下） |
| LLM 上游 | deepseek-direct 绕过（:8080 网关断链，返回控制台 HTML） | **Higress provider+AI route 持久化于 ctrl 数据卷**：`/v1/chat/completions` 经 envoy :8080 真实路由上游（apply 脚本步骤 5 播种；一次配置持久生效） |
| MCP/审批策略 | openclaw.json 手工层 | 内置插件策略先持久化再 desired-state reload（v1.2.1+ 修复） |

**铁律（v1.2.4 版）**：模型/上游变更改 Higress provider（console API 或 apply 脚本），**不改 worker 本地/MinIO JSON**——worker 侧 JSON 由 desired state 管理，手工修改无位置。

## 3. 环境契约（apply 脚本注入；值全部来自部署时 env，零密钥入库）

| env | 说明 |
|---|---|
| `QP_LLM_BASE_URL` / `QP_LLM_API_KEY` / `QP_LLM_MODEL` | Higress provider 上游（真源同旧栈 deepseek-direct：`https://api.deepseek.com` + `deepseek-chat`） |
| `QP_ADMIN_PASSWORD` / `QP_REGISTRATION_TOKEN` | 隔离栈管理凭据（全新生成，不复用生产值） |
| `QP_MINIO_USER` / `QP_MINIO_PASSWORD` | 隔离 MinIO（apply 自动生成并只在首轮写入数据卷） |
| `QP_NETWORK` / `QP_VOLUME` / `QP_PORT_CTRL` / `QP_PORT_MATRIX` / `QP_PORT_MINIO` / `QP_PORT_MGR_CONSOLE` | 隔离资源命名与端口（默认 `qwp2-*` 前缀避免与既有 qwp 评估栈/生产冲突；manager console 默认 18889——生产旧栈占 18888） |
| 镜像 digest | 上表锁定值硬编码于脚本（不使用变量缺省 latest） |

Windows 宿主注意：worker console 端口由 controller 随机分配 127.0.0.1 高位端口，
可能落入 WinNAT 保留段（2026-10-11 实证一次，worker 卡 Created，删除 worker 资源重建即换端口）。
`netsh interface ipv4 show excludedportrange protocol=tcp` 预检；生产切换规划见 §6。

## 4. 部署步骤（apply-qwenpaw-v124.sh 的契约顺序）

1. 预检：docker 可达、端口空闲、env 就绪（缺即失败，不猜默认值）。
2. `docker network/volume create`（幂等）。
3. 启动 controller（embedded digest 锁定；env 契约含 `AGENTTEAMS_COPAW_WORKER_IMAGE`/
   `AGENTTEAMS_QWENPAW_WORKER_IMAGE` 锁定 digest——缺省 latest 会拉取失败，2026-10-10 实证）。
4. 等 controller 健康 + cli-token 落盘（fail 超时即失败）。
5. **Higress provider + AI route 播种**（console API，basic auth=admin:QP_ADMIN_PASSWORD）：
   service-sources(dns, 上游域名) → ai/providers(openai-compat, tokens=QP_LLM_API_KEY) →
   ai/routes(PRE /v1/chat/completions → provider)。幂等重跑（已存在即成功）。
6. 网关真实探测：POST :8080/v1/chat/completions 须 200 且返回补全（fail-closed——
   2026-10-10 实测：漏此步则 manager welcome LLM 探测 404 循环、team 引导停滞）。
7. （生产形态）`ensureFourAgents` 具名 worker + team 建立——走 Console 执行器链
   （MergePilot executor，兼容 v1.2.4 扁平 body 的 PR 版本）。

## 5. 验收（verify-qwenpaw-v124.sh）

- 控制器/网关/Matrix/MinIO 四服务健康；
- `GET /api/mu/...` 无关——此处为 AgentTeams 面：workers 列表四具名且 runtime=期望值
  （读回验证拒绝漂移，与 executor QC 同语义）；
- 网关真实 LLM 往返 200；
- （可选 `--roundtrip`）向指定 worker 房间发 mp_task 信封，收集 marker+JSON 回复
  （真实模型调用业务验收——不以容器 Up/心跳代替）。

## 6. 切换顺序与回滚（详见评估报告 §5-6；此处为操作契约）

- **组合矩阵（2026-10-11 验证状态）**：v1.2.4 ctrl+copaw worker ✅ / v1.2.4 ctrl+qwenpaw worker ✅ /
  223ddc2 ctrl+qwenpaw worker **未验证且不预设可行**（旧 ctrl 无扁平契约、qwenpaw worker
  依赖 v1.2.4 runtime.yaml 投影）——**切换顺序：controller 先行，worker 随后**，禁止旧 ctrl 带新 worker。
- **Controller 切换**：新 embedded 容器 + **原数据卷挂载**（2026-10-11 实证：空数据卷起旧
  223ddc2 ctrl 时 cli-token 引导不落盘——回滚到旧 ctrl 必须用原卷，不可空卷重建）。
- **Worker 切换**：官方热应用 `PUT /api/v1/workers/{name} {"runtime":"qwenpaw",…}`（executor
  已适配双代契约）；迁移在 qwenpaw worker 启动时自动执行（restore→migrate→marker，幂等）。
  **禁止用"删除 worker 重建"作为迁移工具**——DELETE 会连带清空该 worker 的 MinIO 存储前缀
  （2026-10-10 实证：reviewer 旧 .copaw 全灭）。
- **回滚**：worker 级 `PUT runtime=copaw` 热回退（.copaw 旧态从远端恢复，业务往返已验证）；
  QwenPaw 期间新产生的 `.qwenpaw` 数据保留远端（CoPaw 不可见但不丢失）。Console 镜像与
  worker 栈独立回滚（同旧栈 MANIFEST §5 口径）。

## 7. 未过门项（诚实清单）

1. **≥24h 隔离 soak**：未完成——保持未过门，不以短时检查替代；
2. 真实委派任务（TeamHarness/leader 驱动）上的 cancel：委派链未业务验收，cancel 404 语义已按
   "未委派=幂等完成"适配（QC-7）；
3. Manager（openclaw→qwenpaw）状态迁移：manager 容器在 v1.2.4 全新初始化已验证；存量
   openclaw manager 工作区迁移未验证（生产 manager 已停用，风险有限但列入门槛）；
4. Tuwunel 版本差异（生产 223ddc2 内嵌 vs v1.2.4 内嵌）未逐版本比对（协议面实测兼容）；
5. 生产 Console↔v1.2.4 ctrl 的执行器全链（mp_task 真实审查轮）在隔离 Console 的复现验证。
