# QwenPaw 生产执行器切换——维护窗口执行方案（可审阅/可直接执行版）

- 基准：2026-10-12 C 线演练全链实证（四角色 4/4、委派链、回滚 B、机制 K1-K5）
- 判定前提：候选具备生产切换条件（GO-for-maintenance-window）；**本方案不等于已切换**，
  执行以窗口批准为唯一开关
- 口径：DRY_RUN 恒为四角色建议链；Console 不动（rc.25）；PAT 不轮换；旧票不改

## 0. 生产/候选对照（2026-10-12 实时读回）

| 组件 | 生产现状（实时） | 候选锚（完整，勿用短哈希/移动标签） |
|---|---|---|
| Console | `ghcr.io/nghqqa/mergepilot-console:v0.2.0-beta.6-rc.25`（env MERGEPILOT_VERSION=0.2.0-beta.6-rc.25） | 不动 |
| Controller | `agentteams/agentteams-embedded:223ddc2`（容器=agentteams-beta-ctrl） | embedded 运行实体 image ID `sha256:fc18b533ff474698966513dc257e4180f450f33d38bf22df24810a8fe15a3b76`；本地 tag 重建 `sha256:5f3037b35b2c508e65c5f117f0b793618fe0ae459067446e503632e17d7e9ecc`（controller 二进制同哈希 `e241c54c…`，等价性已证） |
| Worker ×4 | `ghcr.io/nghqqa/copaw-worker:223ddc2-v5fix-get-soul-shim` | `agentteams-qwenpaw-worker@sha256:1719208fd17ce535fc2d222bfc66c9ab72aa90d21e3008c4f564341903703a24` |
| Manager | `agentteams/manager:223ddc2` | `higress-registry.cn-hangzhou.cr.aliyuncs.com/agentteams/agentteams-manager-qwenpaw@sha256:965517c923fe09b4e7238b294bc6511f2344acf6b04ca79a6b17059995555805` |
| LLM 上游 | 直连 deepseek（生产 env key=已撤销旧值） | 网关路由 aigw-local + **现役 key（sk-8434…，key2）播种** |

资源名（生产）：容器 `agentteams-beta-ctrl` / `agentteams-beta-worker-mergepilot-{leader,reviewer,fixer,verifier}` /
`agentteams-beta-manager` / `at-auth-proxy`；卷 `agentteams-beta-data`；状态 bind `/agentteams-beta-data`
（kine DB+PKI+admin-token）；网络 `agentteams-beta_atnet`；数据目录 `/data`（同卷）。

## 1. 窗口前置（T-1 天～T-0）

1. 活动任务收敛：核对无 in_progress 委派（MinIO `shared/tasks/*/meta.json` 非终态清零；
   MergePilot 侧无 RUNNING run——以实时查询为准，不接受缓存清单）。
2. 备份三件套（ migrate-agentteams.sh --mode production --steps backup 自动完成并校验）：
   - MergePilot DB：`docker exec beta-mp-pg pg_dump -U postgres -d mu -Fc > mu-<ts>.dump`
     （恢复对账基准=此前 40/92/12 计数法，恢复后逐表比对）
   - 数据卷：`docker run --rm -v agentteams-beta-data:/data:ro -v <备份盘>:/backup alpine tar cf /backup/beta-data-<ts>.tar -C /data .`
   - Controller 状态目录：`docker cp agentteams-beta-ctrl:/agentteams-beta-data <备份盘>/beta-ctrl-statedir-<ts>/`
     （kine DB+PKI+admin-token；kine 用 SQLite backup API 收敛+integrity_check——工具 backup 步骤已实现）
3. 凭据预检（工具自动，失败即停）：
   - Matrix：ctrl env 口令 whoami 预检；无效→matrix-sync（users reset-password，保留账号/房间）
   - Higress：secret 口令 vs env 口令双向试探→**谁有效用谁**收敛另一侧（changePassword 官方 API 优先）
   - LLM：**现役 key（sk-8434…）显式注入 LLM_KEY**；工具先真实调用验证，401 即硬失败
     （生产 env 里的 sk-980da 为已撤销旧值，禁止重播种）
4. 隔离复核：RESOURCE_PREFIX/DOCKER_NETWORK/FS_ENDPOINT/CONTROLLER_URL/MatrixURL 全部指向
   生产值（对照本文件资源名表）；演练残留容器清零。

## 2. 切换顺序与检查点（migrate-agentteams.sh --mode production，断点续作）

| 步 | 命令/动作 | 预期读回 | 失败动作 |
|---|---|---|---|
| 1 | `--steps precheck,backup` | kine integrity ok+快照文件齐 | 停：修复备份后重跑（checkpoint 续作） |
| 2 | `--steps matrix-sync` | env 口令登录 200 | 停：读回失败→现场日志定位 |
| 3 | `--steps higress-converge` | env 口令 console 登录 201 | 停：双侧口令均无效→人工介入（工具拒绝盲写） |
| 4 | **Controller 先行**：`docker rm -f agentteams-beta-ctrl && docker run -d --name agentteams-beta-ctrl --network agentteams-beta_atnet <生产 env 全集，仅替换 AGENTTEAMS_QWENPAW_WORKER_IMAGE/CAS_MANAGER_IMAGE/ RESOURCE_PREFIX 不变> -v agentteams-beta-data:/data -v /agentteams-beta-data:/agentteams-beta-data -v docker.sock … agentteams/agentteams-embedded@sha256:5f3037b3…` | healthz 200；4 worker+manager CR reconcile 无 error；cli-token 存在 | **停**：整栈回滚（§4） |
| 5 | **auth-proxy JWT 重签（K4）**：用 `agentteams-beta-ctrl:/agentteams-beta-data/pki/sa.key` 铸 ES256 SA JWT（模板=现 proxy JWT claims，新 iat/exp/jti）→ 更新 at-auth-proxy nginx conf `set $upstream_token "Bearer <新>"` → `nginx -s reload`（工具 roll-sa-token 逻辑，已实测） | 经 proxy 调 `/api/v1/workers`=200 | 停：恢复旧 JWT 无效则重签重试；仍败→整栈回滚 |
| 6 | **worker 逐个迁移**（间隔观察）：`agt apply worker --name mergepilot-$r --runtime qwenpaw --image <CAS worker 锚>`（经 ctrl cli-token）→ 每 worker 检查点：容器镜像=锚、Matrix logged-in≥1、config.json matrix.enabled=false（channel-fix 同形）、runtime.yaml 投影 generation 前进 | 逐个 PASS | 单 worker 失败→该 worker 热回退：`agt apply worker --name … --runtime copaw --image ghcr.io/nghqqa/copaw-worker:223ddc2-v5fix-get-soul-shim`；连续 2 个失败→停，评估整栈回滚 |
| 7 | **Manager**：`agt apply -f`（runtime=qwenpaw+manager-qwenpaw 锚）→ 容器=965517c9 运行+gateway consumer 恢复无 401 | reconcile 无 error | 停：manager CR 回滚（快照整对象）+整栈回滚评估 |
| 8 | **真实业务验收**：四角色 Matrix 探针（cline-4role 同形，生产 Matrix+生产房间）4/4；
   委派链 delegate→ack→submit 全 ok+cancel+迟到提交拒收（cline-delegate-chain 同形，drill 容器名换生产） | 全 PASS | 任一失败→停+组件回滚 |
| 9 | **T+1h**：错误日志增量/重启计数/网关调用计数/权威任务终态抽查 | 零新增错误 | 异常→按 §4 回滚 |

## 3. 停止条件（任一命中即停，不带病推进）

- 备份完整性/恢复验证失败；kine integrity≠ok
- 凭据三处（Matrix/Higress/LLM）任一无法收敛且无有效候选
- Controller 切换后 CR reconcile 出现不可自愈 error（>10 分钟）
- 连续 2 个 worker 迁移失败；Manager gateway auth 恢复失败
- 真实业务验收任一失败；T+1h 出现任务终态翻转/迟到提交被接纳

## 4. 回滚命令（已实证路径）

- **单 worker 热回退**：`agt apply worker --name mergepilot-$r --runtime copaw --image ghcr.io/nghqqa/copaw-worker:223ddc2-v5fix-get-soul-shim`
  （qwp2 波已证；注意 CR spec.image 残留——必须显式 --image）
- **整栈回滚**（成功后/中途皆同路径，rollback-agentteams.sh 已实证回滚 B）：
  1. 经新 ctrl API 还原完整 CR/secret/CM 快照（工具 rollback 步骤 1-2）
  2. `docker rm -f agentteams-beta-ctrl` → 以**原生产 env 全集**重建
     `agentteams/agentteams-embedded:223ddc2`（同卷 agentteams-beta-data + 同状态 bind
     /agentteams-beta-data——同卷降级已证：旧栈读升级期数据全部可读）
  3. 验证：healthz/0 FATAL/worker 容器（旧 copaw 镜像）/Matrix admin 登录/业务探针真实回复/
     数据对账（MinIO 项目清单+kine integrity）
  4. 新增数据损失边界=切换窗口内产生的任务/CR 变更（窗口内无业务活动时≈0）
- 回滚后 auth-proxy：同 §2-5 重签（223ddc2 重启=旧 JWT 失效；用其数据目录 sa.key 铸新 JWT）

## 5. 禁止事项

删除 worker 资源（用热回退）、空卷回滚、docker cp 注入运行代码、代批审批、修改旧票、
远端补丁写回、以容器 Up/登录日志/API 200 宣布业务成功（必须真实探针）。

## 6. 尚需人工完成（窗口前）

1. 生产 env 的 LLM key 更新为现役值（或窗口内以 LLM_KEY 显式注入工具）——**人工步骤**
2. 维护窗口批准与时间窗选定——**等待批准**（截至本方案无既有切换授权：历史 NO-GO 已由
   本波闭环解除，但生产执行从未被授权；以本方案获批为唯一开关）
3. 窗口内活动任务收敛的最终核对（实时查询）
