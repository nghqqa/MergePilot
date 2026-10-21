# 生产栈手工容器重启策略（2026-10-21 加固）

## 背景

宿主机重启实证（2026-10-21）：以下手工 `docker run` 容器因缺少重启策略，引擎重启后
未能自动恢复，需运维逐个手工 start（agentteams-beta-ctrl、trial-bge）。
本文件为这些容器的**权威重启策略记录**：重建时务必带 `--restart unless-stopped`；
已运行容器用 `docker update --restart unless-stopped <name>` 就地补齐（不停机）。

## 策略清单（2026-10-21 已就地生效）

| 容器 | 策略 | 说明 |
|---|---|---|
| agentteams-beta-ctrl | unless-stopped | 2026-10-21 docker update 补齐（原 no） |
| trial-bge | unless-stopped | 2026-10-21 docker update 补齐（原 no；18700 RAG sidecar） |
| beta-mp-pg | unless-stopped | 已有策略（核实确认，未变更） |
| at-auth-proxy | unless-stopped | 已有策略；启动顺序依赖 ctrl（nginx 启动时解析 upstream，ctrl 未起则 emerg 退出重启循环直至 ctrl 可解析——unless-stopped 重试循环即自愈） |
| beta-mp-console / mp-trial-console | unless-stopped | 已有策略 |

## 回滚

`docker update --restart no agentteams-beta-ctrl trial-bge`（恢复 no 策略；
不影响运行中进程）。

## 网络别名（2026-10-21 事故固化：重建必带）

agentteams-beta-ctrl 在 agentteams-beta_atnet 上必须携带**三个**网络别名
（与 deploy/agentteams-beta/docker-compose.yml 的权威声明一致）：

| 别名 | 消费方 |
|---|---|
| `agentteams-beta-controller` | 容器内三服务统一寻址：AGENTTEAMS_CONTROLLER_URL(:8090)、AGENTTEAMS_FS_ENDPOINT(:9000，内嵌 MinIO)、AGENTTEAMS_MATRIX_URL(:6167) |
| `agentteams-beta-matrix` | AGENTTEAMS_MATRIX_DOMAIN——worker 侧 Matrix 消息寻址域（@…:agentteams-beta-matrix:6167） |
| `aigw-local.agentteams.io` | controller 镜像 generate-worker-config.sh 的固定 AI 网关回退域名——worker 的 LLM provider base_url（:8080 Higress）。**缺它=新投影 worker 的 LLM 全部 Connection error**（2026-10-21 MT_REPLY_TIMEOUT 事故根因三；恢复走逐 worker 热改是临时态，别名补齐后已全部回退上游原生配置） |

**事故实证（2026-10-21）**：今晨主机重启恢复中 ctrl 以手工 `docker run` 重建
（Created=Started 同秒），网络接入与 IP 正确但别名漏带 → 容器内解析
`agentteams-beta-controller` 失败 → projects 接口因 DNS 返回 500（MinIO 本体健康）。
代理链（console→at-auth-proxy→nginx→`agentteams-beta-ctrl:8090` 容器名）不受影响，
仅别名消费方受损。同日第三别名 `aigw-local` 缺漏造成 worker LLM 不可达——
该域名是**上游本地部署契约**（脚本注释：workers on agentteams-net reach Higress），
我们的创建配方从未配置它；修复=补别名，worker 配置回退原样，不需要改镜像。

重建命令必带（IP 按当时实值，当前=192.168.96.6）：

    docker run -d --name agentteams-beta-ctrl --restart unless-stopped \
      --network agentteams-beta_atnet --ip 192.168.96.6 \
      --network-alias agentteams-beta-controller --network-alias agentteams-beta-matrix \
      --network-alias aigw-local.agentteams.io \
      …（其余参数以 inspect 保全件为准，不凭记忆重造）

重启/重建后 60 秒核查（巡检项）：

    docker inspect agentteams-beta-ctrl --format '{{(index .NetworkSettings.Networks "agentteams-beta_atnet").Aliases}}'
    docker exec agentteams-beta-ctrl getent hosts agentteams-beta-controller agentteams-beta-matrix aigw-local.agentteams.io

别名丢失的最小修复（不重启容器、不断其他网络；已连接端点不可重复 network connect，
须 disconnect 后带原 IP+全量别名重连；失败立即不带别名重连还原）：

    docker network disconnect agentteams-beta_atnet agentteams-beta-ctrl
    docker network connect --ip 192.168.96.6 \
      --alias agentteams-beta-controller --alias agentteams-beta-matrix \
      --alias aigw-local.agentteams.io \
      agentteams-beta_atnet agentteams-beta-ctrl

注意：nginx upstream 按容器名解析且启动时缓存——修复保持原 IP 时 proxy 无需 reload；
若 IP 变更须 `docker exec at-auth-proxy nginx -s reload`。

## console 重建（2026-10-21 matrixpw 事故固化：受控秘密源 + 形状检查）

console 的 `MU_AGENTTEAMS_MATRIX_PASSWORD` 等认证 env 一旦缺失，执行器门会如实
拒绝（MT_NOT_CONFIGURED）——**恢复路径是"从受控秘密源的保全件重建"，不是找回密码**。
规范重建入口 = `deploy/agentteams-beta/recreate-console-verified.mjs`（读 inspect
锚点→**变更前形状断言**（必需 env 全非空才放行）→同镜像同参重建→变更后身份验证
（health + Tuwunel 登录探针，只输出状态码不输出值））。锚点与秘密文件只放
`D:/goai/secrets/`（不入版本库）；输出/日志/报告中永远不出现明文凭据。

## Worker CR env 注入（ACL 真源）——**人工步骤，如实限制**

`deploy/agentteams-beta/patch-worker-cr-env.sh`（支持 `--check` 与任意 worker 名）。

- **性质与限制（如实）**：这是**人工配置步骤**，未被任何自动创建路径调用——REST
  `POST /api/v1/workers` 不认 spec.env（上游控制器限制，2026-10-21 实测），且
  6443 k8s API 的 pki 凭据仅 ctrl 容器内可达，console 自动补建路径
  （ensureFourAgents）无法安全调用本工具。**新建 worker 后必须人工补跑一次**；
  已有四 worker 的 CR 自带该 env（2026-10-13 迁移补丁随数据卷持久化），那是
  迁移步骤的产物，**不构成自动创建路径的证据**。
- 验收基线：REST 新建→注入→容器重建/唤醒两轮，授权消息往返零 ACL 手工修改
  （2026-10-21 ITEST3_ACL_OK / ITEST3_REBUILD_OK，一次性测试身份，已清理）。

## 边界

- unless-stopped 不等同于进程级守护：容器内进程崩溃仍由引擎按重启策略拉起，
  但**手工 docker rm/stop 后不会自动重建**（配置漂移需人工介入）。
- 启动顺序：ctrl 先于 at-auth-proxy；beta-mp-pg 先于 beta-mp-console；
  trial-bge 先于 mp-trial-console（RAGTRIAL_EMBED_ENDPOINT 依赖）。
  except ctrl/proxy 对外，其余顺序依赖由重试策略兜底。
