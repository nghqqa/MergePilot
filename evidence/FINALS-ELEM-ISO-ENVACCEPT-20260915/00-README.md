# FINALS-ELEM-ISO-ENVACCEPT-20260915 · 阶段一：隔离栈环境验收（执行记录）

- 授权依据：docs/决赛优化/07 v4.5.5（仅阶段一），用户 2026-09-15 批准；key 路径由用户指定为工作树根 `deepseek.key`
- 执行时间：2026-09-15 21:04–22:10（本地，UTC+8）；全程未发 kickoff、零 GitHub 写入、未 push/未建 PR
- 结论：**步骤 1–5、7、8 完成；步骤 6（llm-preflight）未通过——已执行一次，HTTP 404（0 次上游请求），根因已修复并经零上游探测验证；未重跑（待重新授权）**
- 附带发现（最重要）：路由数据面生效后，**栈内组件自主产生了 3 次上游请求**（控制器 onboarding 探测 1 次提前断开 + manager 欢迎流式调用 2 次，网关记录合计 ~52,932 token），见 `preflight-and-usage-incident.txt`

## 资源与拓扑（与 v4.5.3 声明清单对照）

| 声明 | 实际 | 状态 |
|---|---|---|
| `elemiso-ctrl`（embedded） | 是，label `mp.task=elem-iso-20260915`，卷 `elemiso-ctrl-data`，网 `elemiso-net`，alias `elemiso-controller`/`elemiso-matrix` | ✅ Exited(0) 保留 |
| `elemiso-manager` | 控制器 bootstrap 自建（CR `default`），镜像/网络正确 | ✅ Exited(0) 保留 |
| `elemiso-worker-{reviewer,fixer,verifier}` | `agt create worker --runtime copaw --image ...copaw-worker:223ddc2-build1 --model deepseek-chat` | ✅ Exited(0) 保留 |
| 派生子资源 | 3 × `elemiso-worker-*-auth` 卷（凭据投影，控制器机制） | 已记录，保留 |
| 镜像 ID | `ae47995d209f` / `cafca0c1dc16` / `cdc8f8a4ab8d`（sha256 前缀）与 v4.5.3 声明一致 | `images-verified.txt` |
| 既有 11 容器/历史卷/消息 | 全程 Exited，零接触 | `inventory-final.txt` |

限制（如实）：控制器创建的 manager/worker 容器不携带 `mp.task` label（仅网/卷/ctrl 有）；manager CR 无 DesiredState=sleep 命令（`agt update manager` 无此选项），本次以显式 `docker stop` 结束；下次启动 ctrl 时 reconciler 会按 CR 拉起 manager（空闲不调用模型，PHASE13.1 实证）。

## 八步执行结果

1. **建栈/健康/清单对照**：✅（含一次计划外发现：控制器启动即 bootstrap `default` Manager CR → `elemiso-manager` 自动出现；名称在声明清单内、零继承，按中止条件逐字对照不构成中止，已作为偏差记录）。健康：6167/8001/8080=200，8090(API 根 404 正常)/9000(MinIO 根 403 正常)。
2. **控制台/零继承**：✅ 登录 201；路由表初始为空；消费者仅 manager；MinIO 仅骨架+本会话 manager 自建对象；teams/workers 空；无历史任务/凭据。
3. **模型出口配置**：✅ `setup-higress.sh`（docker cp 入本栈 ctrl 后 bash 执行；首次 `sh` 解释器失败无任何写入）创建域名/服务源/provider/AI 路由全部 201；GitHub MCP 因无 token 跳过。用户 key 经 `docker exec -e` 注入，仅存在于网关 provider 配置（卷内）与 provider tokens 字段。
4. **worker 注册**：✅（含一次授权内修复：env-file 缺 `AGENTTEAMS_CONTROLLER_URL/FS_ENDPOINT/FS_ACCESS_KEY/FS_SECRET_KEY` → ctrl 原参重建（同卷保状态）+ 删除自有 CR/容器重建；此后 5 声明容器 Running、各 worker 独立房间）。
5. **限制回读**：✅ 值一致（manager 128000 / worker openclaw 8000；copaw 运行时 10/600/3）——**但 copaw 运行时配置无 max_tokens 字段，worker 输出上限运行时生效未证实**（`config-readback.txt`）。
6. **preflight**：❌ 一次执行 → 404（站内路由未命中，0 上游）；根因=envoy 对无点单标签 Host 的 ingress 校验丢弃该路由规则；修复=路由 domains 置空（等价旧栈 ai-gw.sh 的不限主机行为），无凭据探测验证三 Host 均 401。**未重跑（用户授权"仅一次"）**。
7. **导出/停止演练**：✅ admin 登录导出 5 房间（Manager 房、3 Worker 房、Admin Room）样例，无 token；`agt worker sleep`×3（phase=Sleeping）→ stop manager → stop ctrl → stop workers，T0=14:00:49Z 全 Exited(0)；T0+120s+ 无复活，全机 0 运行容器，旧栈零接触。
8. **证据**：本目录，SHA256SUMS 锁定；密钥扫描通过（见下）。

## 事故与偏差登记（全部如实）

| # | 事件 | 处置 |
|---|---|---|
| I1 | **用户 DeepSeek key 一次泄露进会话记录**：provider 回读时 `rawConfigs.apiTokens` 镜像了 tokens 字段，jq 脱敏未覆盖 | 后续全部读取/证据双字段脱敏；**建议用户在阶段二前轮换该 key**（泄露范围=本机会话记录，未入 Git/证据/外部） |
| I2 | **计划外上游用量 3 请求/~52.9k token**（控制器 onboarding 重试链最后一次探测 DC + manager 欢迎 2 次流式），时间 13:56:20–13:56:28Z | 非操作员发起、未在"唯一 preflight"授权内；如实登记 `preflight-and-usage-incident.txt`；费用估算 <¥0.2，权威数字以用户 DeepSeek 控制台为准；阶段二预算须计入 onboarding 调用与路由 `proxyNextUpstream attempts=3` 乘数 |
| I3 | env-file 缺 4 个注入变量 → ctrl 重建（授权内自有栈修复） | `env-var-names.txt`；重建后零继承复核、路由随卷存活 |
| I4 | manager CR 由控制器 bootstrap 自动出现（v4.5.3"零 agent 容器被拉起"预期不成立） | 名称在声明清单内、零继承、健康；按中止条件字面不构成中止；已记录 |
| I5 | setup-higress.sh 不在 embedded 镜像内 | 从源码树 docker cp 入本栈 ctrl 执行（首次 sh 解释器失败无写入） |
| I6 | 会话初基线 `volume ls` 用 head 截断导致卷计数口径错误 | 按创建时间复核：本会话窗口仅新建 4 卷（1 声明 + 3 auth），无删除残留 |

## 密钥与凭据边界执行情况

- 用户 key：env-file 不含；仅 `docker exec -e` 注入 setup-higress.sh；容器 env 不含；证据仅以 `<redacted>` 出现（I1 一次会话记录泄露除外，已登记）。
- 全部新生成凭据（admin/minio/manager 密码、registration token、4 个网关消费者 key）：仅存于 env-file（`D:\mp-finals-tmp\elemiso-secrets-20260915\ctrl.env`，工作树外）与栈内；证据/日志一律 `<redacted>`。
- 证据目录经 `sk-`/长十六进制/token 模式扫描后锁定 SHA256SUMS。

## 保留状态（阶段一结束，v4.5.4）

5 容器全部 Exited(0) 保留；`elemiso-ctrl-data` 保留（Matrix 域/房间、MinIO、kine CR、网关配置含 provider key）；3 个 auth 卷保留；`deepseek.key` 原样保留于用户指定路径。**删除任何保留资源为独立待确认操作。** E2 关闭、Scenario B 未启用。

## 证据等级

- 本目录全部内容：**REAL_EXECUTED（AgentTeams 隔离栈环境验收，本机现场执行）**；其中 provider 端模型行为（欢迎消息）为真实执行记录，**不冒充真实协作/返工验证**（阶段二范围）。
- 步骤 6 状态：**BLOCKED（待重新授权一次 preflight）**；阶段一整体不据此宣称"模型链路验收通过"——但 I2 的 2 次 200 流式调用客观证明网关→api.deepseek.com 端到端可达（如实记录，不据此扩大宣称）。
