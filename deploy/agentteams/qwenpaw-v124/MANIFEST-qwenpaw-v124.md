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

## 6b. 资源边界（2026-10-11 Docker 整机崩溃事故教训——硬性上限）

- **单机 embedded 测试栈并发上限 = 1**。一套 embedded（Higress JVM×5+envoy+Tuwunel+MinIO+ctrl）+manager+workers 已是数 GB 常驻；本次 qwp+qwp2+oldp 三套并存叠加生产/AT 栈，Docker Desktop 于 12:40 整机退出（backend 0x40010004 终止，紧随代理注册表变更；无 OOM 实锤但负载背景成立），生产容器全部随引擎下线。
- apply/verify 脚本内建前提：同一时间至多一套候选栈（QP_CTRL_NAME 唯一）；跑第二套前必须 `docker stop` 并确认上一套已释放。
- 崩溃恢复后检查单：①生产容器自动回归（restart 策略）②我的栈 `--restart no` 应保持 down（无头 manager 若自行 Up 需手动 stop）③生产 T+1h 式观察（console/healthz、PG 可查、ctrl healthy、执行器端口在位）。

## 7. 未过门项（诚实清单）

1. **≥24h 隔离 soak**：未完成——保持未过门，不以短时检查替代；
2. 真实委派任务（TeamHarness/leader 驱动）上的 cancel：委派链未业务验收，cancel 404 语义已按
   "未委派=幂等完成"适配（QC-7）；
3. Manager（openclaw→qwenpaw）状态迁移：manager 容器在 v1.2.4 全新初始化已验证；存量
   openclaw manager 工作区迁移未验证（生产 manager 已停用，风险有限但列入门槛）；
4. Tuwunel 版本差异（生产 223ddc2 内嵌 vs v1.2.4 内嵌）未逐版本比对（协议面实测兼容）；
5. 生产 Console↔v1.2.4 ctrl 的执行器全链（mp_task 真实审查轮）在隔离 Console 的复现验证。

---

## 3. CAS 原子修复候选（2026-10-11 深夜波，PR #414 本轮交付）

### 3.1 补丁与版本归属

| 文件 | 内容 | sha256 | 状态 |
|---|---|---|---|
| `patches/teamharness-cas-authoritative.patch` | server.py 最终 CAS 实现（对 pristine 8765d697 的 4 处锚定编辑） | `88f32a6340bcbd0bf94a93e57c676c8f23a01a8740b00347e74afe1621192b5c` | **现行** |
| `patches/controller-canceltask-cas.patch` | Go CancelTask 条件写（PutObjectIfMatch+围栏重验+3 次冲突 409） | `b07619f0f30c3372b00bc3ce28b92f3bd3044844e5e59f09c7607f01aff688c1` | **现行** |
| `patches/teamharness-cancel-submit-race.patch` | 旧 pull-fresh 缓解（48 行，d62bca6 入库版） | — | 历史保留（已被最终 CAS 取代） |

**旧版归属声明**：d62bca6 的 pull-fresh 补丁只是收窄窗口的缓解，曾以 runtime 注入（docker cp）部署并实证"拒收文案触发"；本轮核实发现其 CAS 函数（_cas_write_task 等）在 runtime 中**从未被调用（死代码）**——全部状态写入仍走非原子 _write_task。最终 CAS 补丁以 `_write_task` 为唯一漏斗实施条件写，取代 pull-fresh。

### 3.2 最终 CAS 实现要点（teamharness-cas-authoritative.patch）

1. **`_write_task` 全链条件写**：所有任务状态写入者（delegate/ack/submit/cancel/progress/attention/projectflow accept）共用 `_cas_write_task`——权威读（boto3 直读 MinIO，带 ETag）→ 转换表对**权威状态**重验 → `If-Match` PUT（新建 `If-None-Match`）；PreconditionFailed 重读重试（5 次），其余存储失败/重试耗尽/权威拒绝一律 ValueError，**无任何无条件回退**。
2. **权威票面围栏**：cancel_task 对权威 submission_id 校验（防 leader 本地陈旧副本绕过票面）；同态重写身份匹配（竞争 submit 按 submission_id 拒、异因 cancel 按 reason/replacement 拒）。
3. **history 合并**：CAS 重试时并集远端 history（seq 归一），不丢并发审计痕迹。
4. **filesync push 守卫**：`.../tasks/<id>/meta.json` 的 push 被拒（CAS 专用通道）；目录 push 自动排除 meta.json——mirror/残留推送无法覆盖权威。
5. **清理复活守卫**：权威缺失时，仅"无决策性本地状态"（无 submission/cancelled_at/result_digest/单条以内 history）允许新建；否则拒绝（清理/替换后旧提交不能重建状态）。
6. **键位推导**：与 `_filesync` 同源（TEAMHARNESS_RUNTIME_CONFIG storage.sharedPrefix），并剥离 AGENTTEAMS_STORAGE_PREFIX 的 mc alias/bucket 头——双环境断言（宿主=shared/...，容器=teams/mp/shared/...）。

### 3.3 依赖钉版（构建资料，勿运行时 pip）

- `boto3==1.35.99` + `botocore==1.35.99`（`patch-qwenpaw-dockerfile.py` 锚定注入 Dockerfile）。
- **1.35.0 弃用实证**：其 S3 服务模型 PutObjectRequest **无 IfMatch/IfNoneMatch**（whl 内 service-2.json 解包核实），传参会 ParamValidationError。
- 1.35.99 真机验证（embedded MinIO）：正确 ETag 写入成功 / 错误 ETag PreconditionFailed / If-None-Match 防并发创建 / 探针清理。
- 凭据=worker 既有 per-worker 限定 MinIO 用户（controller 预置策略，仅自身前缀+shared/+teams/mp/），**未注入 root/admin**。

### 3.4 构建与部署（可重复）

- 构建：`build-candidate.sh`（锁定源 8765d697 + 两补丁 + 插件 zip 打包 + Dockerfile 依赖注入；任何 apply/构建失败即停；完成后源码树恢复 clean）。
- 打包：`build-qwenpaw-plugin.py`（上游 ruby 脚本的 Python 等价复刻；asset 目录名=插件名）。
- 部署：`deploy-candidate-qwp2.sh`（清诊断期 MinIO 陈旧插件副本 → 记录回滚锚 → embedded 重建 ctrl（env 同源+worker 镜像指向新 tag）→ 删四 worker + PUT 触发 reconcile → 读回验证）。

### 3.5 候选镜像（2026-10-11 冻结=22:45 CST；运行实体为准）

| 组件 | 运行实体 image id（qwp2 实际运行+全部验收证据对应） | tag 现值 |
|---|---|---|
| worker | `sha256:1719208fd17ce535fc2d222bfc66c9ab72aa90d21e3008c4f564341903703a24` | 同 tag（一致） |
| embedded（ctrl 容器来源） | `sha256:fc18b533ff474698966513dc257e4180f450f33d38bf22df24810a8fe15a3b76` | tag 现指 `sha256:5f3037b35b2c508e65c5f117f0b793618fe0ae459067446e503632e17d7e9ecc` |

**tag 重构建等价性证明**：标签 `1.2.4-cas` 在冻结后曾因重复构建移动（Go 补丁未变）；
运行容器内 `agentteams-controller` 二进制 sha256=`e241c54c9c46…92634` 与 tag 重建内
**逐字节一致**（同源同补丁的功能等价重构建，未经运行时验证）。候选判定以运行实体
fc18b533 为准；恢复场景从运行镜像 docker commit 或按本脚本重构建均可（二进制等价）。

读回验证（部署后实测）：四 worker builtin+workspace server.py 均含 `_cas_write_task`；boto3/botocore=1.35.99（PutObject IfMatch=True）；陈旧插件副本已清（启动 restore 不覆盖镜像 builtin）；Matrix admin token 刷新=200。

### 3.6 验收证据（qwp2 栈，2026-10-11）

- **七项跨进程回归 15/15 PASS**（`r3work/qwenpaw-eval-20261010/cas-regression-qwp2.py`；证据 `evidence/race-fix/cas-regression/`）：in_progress 取消后迟到提交拒收且 cancelled 不翻转；submit 先到+票面取消契约通过；陈旧 ack 不复活；两提交竞争同一 ETag 仅一胜（submission_id 冲突拒）；同因幂等/异因 conflict/票面缺失+错票双拒；清理复活拒+push 守卫；正常提交+新轮次成功；**Controller HTTP 取消（MergePilot 生产形状：create+replan team_id=mp）assigned 态竞态 409 围栏+200 取消双形态 PASS**。
- **四角色真实往返 4/4**（重启后复测再 4/4；`hotswitch-4role.sh`）。
- **MergePilot 影响 3/3**（`cas-mp-impact-qwp2.py`；证据 `evidence/race-fix/mp-impact/`）：executor `cancelExternalRound` 真实代码路径经 ctrl CAS 端点取消→权威 cancelled；迟到提交拒收；迟到 Matrix 回复投递后 b7 console DB 快照零变化（不推进终态 attempt/不改 run 终态/不产建议与验证产物）。
- **Matrix 401 定案**：非"重启失效"——四角色脚本读的 `qwp2-mx-token.txt` 在敏感文件清理中被删，脚本以空 token 发送=M_MISSING_TOKEN。worker/console token 全程有效；刷新助手 `qwp2-mx-refresh.sh`（ADMIN_PASSWORD→login→whoami 验证）纳入部署工具；重启（token=ctrl 注入 env， Same identity）与重建（reconcile 重注入）后身份自动恢复均实证。
- Go 侧单测：`internal/server`+`internal/oss` 全绿（含三个新竞态测试：并发 submit 围栏保持、并发非终态写收敛 cancelled+history 保留、并发终态胜出 409 拒覆盖）。

### 3.7 生产与边界（不变口径）

- 生产=rc.25（beta-mp-console），本轮全程未动；DRY_RUN 仍为建议链；不合并、不切生产。
- 生产迁移门保持 NO-GO→本轮证据补齐后由维护者依 3.6 判定；有界观察（2–4h）自候选冻结（2026-10-11 22:45 CST 部署完成）起算。
