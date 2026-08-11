# D2B-3A.1 · Docker Socket Proxy 设计冻结（v2，AgentTeams v1.2.2 source-proven）

> 状态：**设计冻结（design frozen v2），未实现**。本文基于 AgentTeams v1.2.2 上游源码（commit `849182af8e017168a5a200a87b1062142caf462d`）的 source-proven 审计，取代 D2B-3A v1 中的 [INFER]/[GAP] 推断。
> 分支：`feat/d2b3-docker-socket-proxy`（基于初赛基线 `preliminary-20260811` → `957d7d5`）
> 冻结日期：2026-08-11
> 上游 tag：`v1.2.2`（lightweight tag = commit `849182a`）
> 范围：仅 D2B-3A.1（上游审计 + 设计修订）。D2B-3B（实现）见末尾，**需独立授权**。

---

## 0. 阅读约束与 v1→v2 变更摘要

- 本文档是**设计契约**，不是已实现能力的说明。被 `tests/hiclab/test_blocked_consistency.py::test_no_closed_chain_claim` 禁止的关键词（`proxy deployed` / `proxy is live` / `creation chain closed` / `real creation chain intercepted`）作用域是 `tools/hiclab/**/*.{py,sh}`——本文在 `docs/`，不触发该扫描，但仍自律不作过度声明。
- **v1→v2 关键变更**（基于 v1.2.2 source-proven 证据）：
  1. 所有 [INFER]/[GAP] 项已转为 **SOURCE_PROVEN**（controller 实际调用的 Docker API 已从 `docker.go` 逐行确认）。
  2. **官方 `agentteams-docker-proxy` 不保护 controller**——已 source-proven（§3）。v1 假设的"controller 流量过官方 proxy"是错误的。
  3. **label 隔离不可行**——controller 创建的 worker/manager 容器**没有任何 label**（`interface.go:166` 注释 + `docker.go:642-649` 无 Labels 字段）。v1 的 ALLOW-LABELED 设计必须改为 **name-prefix 隔离 + proxy 注入 label**。
  4. **socket 重定向的唯一机制** = `AGENTTEAMS_PROXY_SOCKET`（`config.go:292`，默认 `/var/run/docker.sock`，fail-open）。这是部署侧把 controller 指向自研 proxy 的唯一锚点。
  5. 容器名从 `hiclaw-worker-*`（v1.1.2）改为 `agentteams-worker-*`（v1.2.2 硬切割重命名，changelog `#1063`/`#1065`）。

---

## 1. v1.1.2 → v1.2.2 破坏性差异（source-proven）

| 维度 | v1.1.2（当前部署） | v1.2.2（上游审计目标） | 证据 |
|---|---|---|---|
| 容器名前缀 | `hiclaw-worker-*` / `hiclaw-manager` | `agentteams-worker-*` / `agentteams-manager` | `changelog/v1.2.0.md:13,33`（`#1063`/`#1065` 硬切割重命名）；`config.go:271` `AGENTTEAMS_RESOURCE_PREFIX` 默认 `agentteams-` |
| 环境变量命名 | `HICLAW_*` | `AGENTTEAMS_*` | 同上重命名；`config.go:292` `AGENTTEAMS_PROXY_SOCKET` |
| 架构 | embedded controller（PR #616 起） | embedded controller（唯一支持路径） | `install/agentteams-install.sh:1113-1118,1121` "Embedded mode is the only supported architecture since PR #616" |
| docker.sock 选择 | 固定 `/var/run/docker.sock` | 可选（rootless/podman/macOS） | `changelog/v1.2.0.md:61`（`#553`）；`install.sh:1545-1578` `detect_socket` |
| Worker auth token | 共享卷 | 每 worker 独立卷 + 原子轮换 | `changelog/v1.2.1.md:23,66`（`#1120`）；`docker.go:136-155,239-241,287-305` |
| 官方 docker-proxy | legacy manager 路径用 | **仍是 legacy-only**，embedded 路径不启动 | `install.sh:4136-4177`（legacy）、`:4007-4023`（embedded 不启动）；卸载注释 `install.sh:4543-4544` "legacy ≤ v1.0.x" |

**结论**：MergePilot 若升级到 v1.2.2，所有 `hiclaw-*` 容器名和 `HICLAW_*` 环境变量必须改为 `agentteams-*` / `AGENTTEAMS_*`。`managed_containers.py`、`guarded_start.py`、`harden_policy.py` 的 name regex 都要同步。

---

## 2. controller / manager / docker-proxy / docker.sock 数据流（source-proven）

### 2.1 v1.2.2 embedded 模式（唯一支持路径）

```
┌─────────────────────────── host ───────────────────────────┐
│  /var/run/docker.sock  (real dockerd)                       │
│       ▲                                                     │
│       │ unix socket (net.Dial("unix", SocketPath))          │
│       │  docker.go:45; config.go:292 SocketPath default     │
│       │              = /var/run/docker.sock                 │
│  ┌────┴────────────────────────────────┐                    │
│  │ agentteams-controller (embedded)     │                    │
│  │  - install.sh:4015 mounts            │                    │
│  │    CONTAINER_SOCK:/var/run/docker.sock                   │
│  │  - backend.DockerBackend 直接 CRUD    │                    │
│  │    worker/manager 容器               │                    │
│  │  - HTTP API :8090                    │                    │
│  │    /docker/* 路由 = 可选 proxy 路由   │                    │
│  │    (http.go:134-136, 需 bearer token)│                    │
│  └────┬────────────────────────────────┘                    │
│       │ agentteams-net (Docker network)                     │
│  ┌────┴──────────┐  ┌──────────────────┐                    │
│  │ agentteams-    │  │ agentteams-worker-<name>              │
│  │ manager        │  │ (controller 创建;无 docker.sock;无 label)│
│  │ (controller 创建;│  └──────────────────┘                    │
│  │  无 docker.sock)│                                         │
│  └────────────────┘                                         │
└─────────────────────────────────────────────────────────────┘
```

**关键事实**（每条 source-proven）：
- **controller 直接挂 docker.sock**：`install/agentteams-install.sh:4015` `-v "${CONTAINER_SOCK}:/var/run/docker.sock"`。
- **controller 的 Docker 调用不经任何 proxy**：`docker.go:42-53` `NewDockerBackend` 用 `net.Dial("unix", config.SocketPath)` 直连；proxy 包（`internal/proxy/`）只被 `internal/server/http.go:14` import，挂为 `/docker/` 路由（`http.go:134-136`），不在 `backend.DockerBackend` 路径上。
- **worker/manager 容器无 docker.sock**：`docker.go` 全树 grep `docker.sock`/`/var/run/docker` 零命中；`buildCreatePayload`（`docker.go:677-759`）只发 caller 提供的 `Volumes` + auth-token 卷。
- **worker/manager 容器无 label**：`dockerCreatePayload`（`docker.go:642-649`）**无 Labels 字段**；`interface.go:166` 注释明确 "Docker backend does NOT synthesize tenant/role defaults"。
- **官方 proxy（Go 库）保护的是 `/docker/` 路由的 bearer-token 调用方（manager/admin），不是 controller 自身**（§3）。

### 2.2 legacy 模式（v1.0.x，v1.2.2 不默认走）

```
manager 容器 ──(AGENTTEAMS_CONTAINER_API=http://agentteams-docker-proxy:2375)──▶
  agentteams-docker-proxy 容器 (mounts /var/run/docker.sock) ──▶ dockerd
```
- 仅 `AGENTTEAMS_FORCE_LEGACY=1` 或 version < v1.1.0 时进入（`install.sh:1148-1153,1163-1168`）。
- proxy 镜像源码**不在仓库**（provenance gap；仅有 registry pull 引用 `install.sh:4152`）。
- **MergePilot 不走此路径**（embedded 是 v1.2.2 唯一支持路径）。

---

## 3. 实际 Docker API 清单（SOURCE_PROVEN，从 `docker.go` 逐行确认）

所有项已从 `agentteams-controller/internal/backend/docker.go`（commit `849182a`）逐行确认。证据列 = `docker.go:<line>` + Go 函数。

| # | Docker Engine API | 方法 | 分类 | 证据 |
|---|---|---|---|---|
| 1 | `/_ping` | GET | **SOURCE_PROVEN** | `docker.go:77` `Available()` |
| 2 | `/containers/create?name=<name>` | POST | **SOURCE_PROVEN** | `docker.go:396-397` `doCreate` |
| 3 | `/containers/{name}/archive?path=<dir>` | PUT | **SOURCE_PROVEN** | `docker.go:266-267` `writeContainerFile`（auth token 投影，`docker.go:136-155` 条件） |
| 4 | `/containers/{name}/exec` | POST | **SOURCE_PROVEN** | `docker.go:320` `execContainer`（token 轮换路径） |
| 5 | `/exec/{id}/start` | POST | **SOURCE_PROVEN** | `docker.go:345-347`（hijack upgrade） |
| 6 | `/exec/{id}/json` | GET | **SOURCE_PROVEN** | `docker.go:362-363` |
| 7 | `/containers/{name}/start` | POST | **SOURCE_PROVEN** | `docker.go:617` `startContainer` |
| 8 | `/containers/{name}/stop?t=10` | POST | **SOURCE_PROVEN** | `docker.go:490` `Stop` |
| 9 | `/containers/{name}/json` | GET | **SOURCE_PROVEN** | `docker.go:516` `Status`（inspect） |
| 10 | `/containers/{name}?force=true` | DELETE | **SOURCE_PROVEN** | `docker.go:441` `Delete` |
| 11 | `/volumes/{authVolumeName}` | DELETE | **SOURCE_PROVEN** | `docker.go:460` `deleteAuthVolume`（auth 卷清理） |
| 12 | `/images/{image}/json` | GET | **SOURCE_PROVEN** | `docker.go:568` `ensureImage`（inspect） |
| 13 | `/images/create?fromImage={image}` | POST | **SOURCE_PROVEN** | `docker.go:585-586` `ensureImage`（pull，仅当镜像缺失） |
| 14 | `/containers/json`（list） | GET | **NOT_REQUIRED** | `docker.go` 无 List 方法；controller 不主动 list（由 reconciler 遍历 CR） |
| 15 | `/events`（stream） | GET | **UNKNOWN_DENY** | 仓库无证据 controller 监听 events；默认拒 |
| 16 | `/version`、`/info` | GET | **UNKNOWN_DENY** | controller 用 `/_ping` 做 liveness（`docker.go:77`）；`/version`/`/info` 无证据 → 默认拒 |
| 17 | `/networks/create`、`/volumes/create` | POST | **NOT_REQUIRED** | controller 不创建网络/卷（网络 `agentteams-net` 由 install.sh 创建；auth 卷由 `/volumes` 隐式创建于 create 时，非显式 POST `/volumes/create`） |
| 18 | `/build`、`/images/load`、`/images/push`、`/system/prune`、`/swarm/*`、`/services`、`/secrets`、`/configs` | * | **NOT_REQUIRED** | controller 不构建/推送/修剪/swarm |

**所有 [INFER] 已消除**。剩余 UNKNOWN_DENY（`/events`、`/version`、`/info`）= 无证据 → fail-closed 拒绝。RUNTIME_CAPTURE_REQUIRED = **无**（全部已 source-proven；若 D2B-3B 受控 stub 发现新端点，回炉）。

### 3.1 controller create body 的实际字段（`docker.go:642-759`）

`dockerCreatePayload` struct（`docker.go:642-675`）+ `buildCreatePayload`（`docker.go:677-759`）实际发送：
- `Image`（从 `req.Image` 或 runtime 默认）
- `Env`（sorted `KEY=VALUE`；controller 注入 `AGENTTEAMS_AUTH_TOKEN[_FILE]`、`AGENTTEAMS_CONTROLLER_URL`）
- `WorkingDir`、`ExposedPorts`
- `HostConfig.NetworkMode`（默认 `agentteams-net`，`config.go:523`）
- `HostConfig.ExtraHosts`、`HostConfig.Binds`（来自 `req.Volumes`）、`HostConfig.PortBindings`
- `HostConfig.RestartPolicy`（**仅当 `req.RestartPolicy != ""`**；manager = `unless-stopped`，worker = 空）
- `HostConfig.SecurityOpt`（struct 有字段但**从不赋值**）
- `NetworkingConfig.EndpointsConfig[net].Aliases`
- **无 Labels**（struct 无此字段）

---

## 4. 官方 proxy 是否足够？（调用链证据 → 不够）

### 4.1 官方 proxy 的两个形态（source-proven，命名澄清）

| 形态 | 位置 | 监听 | 保护谁 |
|---|---|---|---|
| **Go in-process proxy 库**（`internal/proxy/proxy.go` 120 行 + `security.go` 187 行） | controller HTTP API 的 `/docker/` 路由（`http.go:134-136`），需 bearer token + `gateway` action | TCP `:8090/docker/*` | manager/admin 等 token 调用方；**不在 controller 自身 Docker 路径上** |
| **`agentteams-docker-proxy` 镜像**（legacy standalone） | install.sh legacy 路径（`install.sh:4151-4175`） | TCP `:2375` | legacy manager（`AGENTTEAMS_CONTAINER_API`）；**embedded 路径不启动**；**镜像源码不在仓库**（provenance gap） |

### 4.2 调用链证据：官方 proxy 不保护 controller

1. controller 的 worker/manager provisioning 走 `backend.DockerBackend`（`app.go:837` 构造，`docker.go:42-53` 实现）。
2. `DockerBackend` 的 transport 用 `net.Dial("unix", config.SocketPath)`（`docker.go:45`），`SocketPath` = `AGENTTEAMS_PROXY_SOCKET`（默认 `/var/run/docker.sock`，`config.go:292`）。
3. **`DockerBackend` 从不调用 `proxy.Handler`**。`proxy` 包仅被 `internal/server/http.go:14` import，仅用于 `/docker/` 路由（`http.go:135`）。
4. 因此 controller 的全部 Docker CRUD（§3 的 1-13 项）**绕过官方 proxy 的全部校验**，直击 `/var/run/docker.sock`。

### 4.3 官方 proxy（Go 库）的校验覆盖（source-proven，`security.go:41-53` struct 字段）

| 维度 | 官方 proxy 校验？ | 证据 | 缺口 |
|---|---|---|---|
| HTTP method/path 白名单 | 部分 | `proxy.go:59,65-85`（GET/HEAD 透传；POST/DELETE 6 regex） | PUT/PATCH 全拒；但 GET/HEAD 全透传 |
| `/containers/create` body 解析 | 是 | `proxy.go:100-104` | 仅 Image + HostConfig 子集 |
| Image allowlist | 弱 | `security.go:124-126,167-187` | **default-allow 所有 local/localhost/higress 镜像**（`ubuntu:latest` 也过） |
| `Privileged` | 是 | `security.go:143-145` | — |
| `CapAdd` | 部分 | `security.go:158-162` | 仅 6 cap（`SYS_ADMIN,SYS_PTRACE,DAC_OVERRIDE,NET_ADMIN,SYS_RAWIO,SYS_MODULE`）；`NET_RAW`/`CHOWN`/`FOWNER` 等过 |
| `Binds`/`Mounts[bind]` | 是 | `security.go:133-140` | `Mounts` 其他类型（volume/tmpfs）不拒 |
| `NetworkMode` | 部分 | `security.go:148-150` | 仅 `host` 拒；`container:<id>` 过 |
| `PidMode` | 部分 | `security.go:153-155` | 仅 `host` 拒 |
| `IpcMode`/`SecurityOpt`/`Sysctls`/`Devices`/`RestartPolicy`/`Tmpfs`/`StorageOpt`/`CapDrop`/`UsernsMode` | **否** | `security.go:41-53` struct 无这些字段 | **`Devices` 不检 → 递归 docker.sock via Devices 不被挡** |
| Labels 注入/strip/校验 | **否** | `security.go:41-44` 无 Labels 字段 | — |
| 递归 docker.sock 防护 | **否** | `Binds` 挡字符串但 `Devices` 不检 | **重大缺口** |
| body size limit | 否 | `proxy.go:91` `io.ReadAll` 无界 | — |
| hijack/streaming | 透传 | `proxy.go:70-75` | — |
| 测试覆盖 | 仅 `security_test.go`（`ValidateContainerCreate`） | — | **`proxy.ServeHTTP` HTTP 层零测试** |

### 4.4 结论

**官方 proxy 不足以保护 controller**，原因有二：
1. **架构上不在路径上**（§4.2）——即便其校验完美，也保护不到 controller 的 provisioning 流量。
2. **校验覆盖窄**（§4.3）——即便强行接到 controller 路径上，`Devices`/`IpcMode`/`SecurityOpt`/`Sysctls`/`RestartPolicy`/labels/递归 socket/size 都不挡。

---

## 5. 推荐代理部署拓扑（唯一推荐方案 C）

### 5.1 三方案比较

| 方案 | 描述 | 安全性 | 维护 | HiClaw 兼容 | 评价 |
|---|---|---|---|---|---|
| A. 直接用官方 agentteams-docker-proxy | 把官方镜像放到 controller 前 | **不可行** | 低 | 低 | 官方 proxy 是 TCP :2375（legacy）或 `/docker/` 路由（Go 库）；controller 的 `net.Dial("unix", ...)`（`docker.go:45`）**只认 unix socket，不认 TCP**。无法接到 controller 路径。且校验窄（§4.3）。**拒绝**。 |
| B. 加固/包装官方 proxy | fork `internal/proxy/` 加字段 + 暴露为 unix socket | 中 | 中 | 中 | 仍需解决"Go 库不在 controller 路径"问题（要暴露为 unix socket）；且 fork 上游代码长期维护成本高。**次选**。 |
| **C. MergePilot 自研 controller→dockerd 窄代理**（推荐） | 自研 unix socket 反向代理，`AGENTTEAMS_PROXY_SOCKET` 指向它 | **高** | 中 | **高** | 直接复用 v1.2.2 唯一的 socket 重定向锚点（`config.go:292`）；策略层复用 `harden_policy.py`；与 `tools/hiclab/` 栈一致。**首选**。 |

### 5.2 推荐拓扑（方案 C）

```
┌─────────────────────────── host ───────────────────────────┐
│  /var/run/docker.sock  (real dockerd)                       │
│       ▲                                                     │
│       │ unix socket (proxy 转发，deny-by-default)           │
│  ┌────┴───────────────────────────┐                         │
│  │ mergepilot-socket-proxy         │                         │
│  │  owns /run/mp/docker.sock 0600  │                         │
│  │  policy = harden_policy.py +    │                         │
│  │           deny rules (§6)       │                         │
│  └────▲───────────────────────────┘                         │
│       │ AGENTTEAMS_PROXY_SOCKET=/run/mp/docker.sock         │
│  ┌────┴────────────────────────────────┐                    │
│  │ agentteams-controller (embedded)     │                    │
│  │  install.sh:4015 改挂:               │                    │
│  │   -v /run/mp/docker.sock:/var/run/docker.sock            │
│  │  (而不是 CONTAINER_SOCK)             │                    │
│  │  config.go:292 SocketPath =          │                    │
│  │   env AGENTTEAMS_PROXY_SOCKET        │                    │
│  └────┬────────────────────────────────┘                    │
│       │ agentteams-net                                     │
│  ┌────┴──────────┐  ┌──────────────────┐                    │
│  │ agentteams-    │  │ agentteams-worker-*                   │
│  │ manager        │  │ (proxy 创建后注入 label)              │
│  └────────────────┘  └──────────────────┘                    │
└─────────────────────────────────────────────────────────────┘
```

**部署侧关键改动**（D2B-3B）：
1. install.sh（或 MergePilot 的 deploy 脚本）启动 `mergepilot-socket-proxy`，它以 root 拥有 `/run/mp/docker.sock`（0600）+ 可读 `/var/run/docker.sock`。
2. controller 容器的 docker.sock 挂载点从 `${CONTAINER_SOCK}` 改为 `/run/mp/docker.sock`（覆盖 `install.sh:4015` 的默认）。
3. controller env 设 `AGENTTEAMS_PROXY_SOCKET=/var/run/docker.sock`（容器内路径，即挂载点）——利用 `config.go:292` 的唯一锚点。
4. proxy 健康时 deploy 写 `/etc/hiclab/proxy-deployed` marker（`guarded_start.py:37` 契约），放行 controller/manager。

---

## 6. 安全契约（v2，适配 v1.2.2 + label 注入）

### 6.1 请求分类（deny-by-default）

每个进入 proxy 的请求落入：**DENY** / **ALLOW-READONLY** / **ALLOW-NAMEPREFIX** / **TRANSFORM**。未匹配任何 allow → DENY。

**v1→v2 关键变更**：v1 的 ALLOW-LABELED（基于 container 已有 label）**不可行**，因为 controller 创建的容器无 label（§2.1）。改为 **ALLOW-NAMEPREFIX**：proxy 用容器名前缀（`agentteams-worker-*` / `agentteams-manager`）做范围判定，**并在 TRANSFORM 时注入 label**（之后才能用 label 做后续操作的校验）。

### 6.2 DENY 规则（fail-closed）

#### D1. 危险容器字段（针对 `POST /containers/create` 与 `POST /containers/{id}/update`）

| 规则 | 拒绝条件 | 理由 |
|---|---|---|
| D1.1 | `HostConfig.Privileged == true` | 特权 = 宿主 root |
| D1.2-D1.6 | `PidMode`/`IpcMode`/`NetworkMode`/`UsernsMode`/`CgroupnsMode` == `host` 或 `container:*` | 命名空间逃逸（v1.2.2 官方 proxy 仅挡 `host`，本设计挡 `container:*`——更严） |
| D1.7 | `HostConfig.Binds` 或 `Mounts`（任何 Type）含 `docker.sock` / `/var/run/docker.sock` / `/run/docker.sock` / `/run/mp/docker.sock` | **递归 socket 根除**（官方 proxy 仅挡 Binds 字符串 + Mounts[bind]，`Devices` 不挡——本设计补全 `Devices` 与所有 Mounts Type） |
| D1.8 | `HostConfig.Devices` 非空 | 设备直通（官方 proxy 不检——补全） |
| D1.9 | `HostConfig.Binds`/`Mounts` 含任意宿主绝对路径且不在 bind-allowlist | 禁任意 bind |
| D1.10 | `HostConfig.CapAdd` 含高危 cap（全列表，含官方 proxy 遗漏的 `NET_RAW`/`CHOWN`/`FOWNER`/`SETFCAP`/`MKNOD`/`SYS_NICE`/`DAC_READ_SEARCH`/`SETUID`/`SETGID`/`KILL`） | 最小权限（官方 proxy 仅 6 cap，本设计更全） |
| D1.11 | `HostConfig.SecurityOpt` 含 `apparmor=unconfined`/`seccomp=unconfined`/`label=` | MAC/seccomp 绕过（官方 proxy 不检） |
| D1.12 | `HostConfig.Sysctls` 修改 `net.*`/`kernel.*` | 内核参数（官方 proxy 不检） |
| D1.13 | `HostConfig.RestartPolicy` ≠ `{Name:"no"}`（worker）或不在 manager allowlist | **proxy 强制覆盖为 no**（§6.4 TRANSFORM）；任何其他值 → DENY 或被覆盖 |

#### D2. 资源范围越界

| 规则 | 拒绝条件 |
|---|---|
| D2.1 | `POST /containers/create` 的 `name` 不匹配 `^agentteams-worker-[a-z0-9-]+$` 且不匹配 `^agentteams-manager(-[a-z0-9-]+)?$` | controller 只许创建 worker/manager（v1.2.2 名前缀） |
| D2.2 | 任何针对 `/containers/{name}` 的写/删操作（start/stop/rm/exec/logs/json）的目标 name 不在 `agentteams-worker-*`/`agentteams-manager` 范围 | **name-prefix 隔离**（替代 v1 的 label 隔离） |
| D2.3-D2.8 | networks create/delete、volumes create、build、images load/push、prune、swarm/services/secrets/configs | controller 不需要（§3 NOT_REQUIRED） |

#### D3. 镜像与 name

| 规则 | 拒绝条件 |
|---|---|
| D3.1 | `Image` 不在 image-allowlist（**按 digest**，不接受 tag） | 比 v1.2.2 官方 proxy 的"default-allow local"更严 |
| D3.2 | `name` 含 `/`、`..`、非 ASCII | 防注入（官方 proxy 已有部分，本设计全化） |

#### D4. 协议层

| 规则 | 拒绝条件 |
|---|---|
| D4.1 | HTTP `Upgrade: tcp`（hijack）目标非 `/containers/{name}/exec` 或 `/exec/{id}/start` | controller 的 exec 是 SOURCE_PROVEN（`docker.go:320,345`）；其他 hijack 拒 |
| D4.2 | 请求体 > 1 MiB | 防 body 炸弹（官方 proxy 无界——补全） |
| D4.3 | `GET /containers/json` 无 `?filters=...` 含 `name=agentteams-` 前缀 filter | controller 不主动 list（§3 NOT_REQUIRED）；若放开则必须 filter |

### 6.3 ALLOW-READONLY / ALLOW-NAMEPREFIX

**ALLOW-READONLY**（无条件透传）：
- `GET /_ping`（`docker.go:77` SOURCE_PROVEN）
- （`/version`、`/info` = UNKNOWN_DENY，默认拒——controller 不用）

**ALLOW-NAMEPREFIX**（转发前校验目标 name 前缀；对应 §3 SOURCE_PROVEN 项）：
- `GET /containers/{name}/json`（`docker.go:516`）—— proxy 先向 dockerd 发只读 inspect，校验返回的 `Name` 在 `agentteams-*` 范围
- `POST /containers/{name}/start`（`docker.go:617`）
- `POST /containers/{name}/stop?t=10`（`docker.go:490`）
- `DELETE /containers/{name}?force=true`（`docker.go:441`）
- `POST /containers/{name}/exec` + `/exec/{id}/start` + `GET /exec/{id}/json`（`docker.go:320,345,362`）—— exec 目标必须 name-prefix 匹配
- `PUT /containers/{name}/archive`（`docker.go:266`）—— auth token 投影路径
- `DELETE /volumes/{authVolumeName}`（`docker.go:460`）—— 校验 volume 名为 `{agentteams-worker-name}-auth`
- `GET /images/{image}/json` + `POST /images/create?fromImage={image}`（`docker.go:568,585`）—— image 必须在 allowlist（digest）

**name-prefix 校验实现**：proxy 以自身身份向 dockerd 发 `GET /containers/{name}/json`（read-only，不计入 controller 配额），取 `Name`，匹配 `^agentteams-(worker|manager)`；不匹配 → DENY。proxy 不把 inspect 结果返回给 controller（仅返回 allow/deny 决定），避免信息泄露。

### 6.4 TRANSFORM（`POST /containers/create` + name 匹配 worker/manager）

1. **先跑 deny 检查**（D1/D3 全部）——任何命中即 403，不进入 transform。
2. **调用 `harden_policy.apply_hardening`**（现有代码，扩展）注入：
   - `HostConfig.Tmpfs`、`HostConfig.StorageOpt`（probe-proven）
   - `HostConfig.RestartPolicy = {Name:"no"}`（强制覆盖）
   - `Labels`：`com.mergepilot.{scope, run_id, agent, hardened=1}` —— **proxy 注入，client 同名 label 被 strip**（§7 B5 修复）
3. transform 后再跑一次 D1/D3 兜底（defense in depth）。
4. 转发改写后的 body。

### 6.5 Label 契约（v2，**labels mandatory** — D2B-3B1.2 闭合）

> **D2B-3B1.2 重要变更**：labels 缺失**不是兼容模式**。所有经 proxy 创建的新容器都带权威 labels；旧版无 label 容器必须清理或重新创建，**不能自动信任**。`_inspect_authoritative` 对 4 项 label 做精确等值校验，缺一即 DENY。

- **scope**：精确等于 `config.scope`（deploy 配置值）。不再用通用 allowlist 兜底。
- **run_id**：精确等于 `config.run_id`；由 proxy 在 TRANSFORM 时注入，**client 不得自带**（§7 B5 strip-then-inject）。
- **agent**：精确等于 `derive_agent_strict(name)`（单一派生函数，transform + inspect 共用，禁止两套规则漂移）；允许集合 = `{reviewer, fixer, verifier, manager}`；未知 name → None → DENY。
- **hardened**：精确等于 `"1"`。
- **exec-create 也必须经过 authoritative inspect**（D2B-3B1.2）：inspect 失败 → 403，exec-create 请求**不转发**给 upstream（FakeUpstreamDaemon 只收到 inspect）。
- **统一 inspect 覆盖**：`POST /containers/{name}/start`、`stop?t=10`、`GET /json`、`PUT /archive`、`DELETE ?force=true`、`POST /exec` 全部走 `_inspect_authoritative`（Name + 4 labels 精确匹配）。
- **旧版无 label 容器**：不自动信任；必须经 proxy 重新创建（获得权威 labels）或手动清理。

### 6.6 Image allowlist / Bind allowlist / Network allowlist

- **Image**：按 digest 固定（非 tag）。deploy 配置提供；proxy 启动时加载并断言非空（空 → fail-closed 拒启动）。
- **Bind allowlist**：默认空（worker/manager 不需宿主 bind；controller 自身 `/data` 卷由 deploy 步骤处理，非 proxy 流量）。
- **Network allowlist**：`agentteams-net`（或测试栈网络）；`NetworkMode` 必须是 named network，不得 `host`/`bridge`/`none`/`container:*`。

### 6.7 Proxy 自身约束 + marker 生命周期 + fail-closed 兜底

（与 v1 §3.7-3.8 一致，此处不重复。关键点：proxy 启动自检（image/scope allowlist 非空、能连上游）→ 否则 exit 非 0 不写 marker；marker 携带 pid，`guarded_start.validate_marker` 校验 pid 存活；所有决策写 INSERT-only 审计；上游不可达 → DENY 不降级；超时 → 504 不重放。）

---

## 7. B5 label 伪造修复（设计闭合）

### 7.1 问题

v1 的 `harden_policy.apply_hardening`（`harden_policy.py:122-128`）对 Labels 是**合并**（`existing_tmpfs.update(...)` 模式）——client 可自带 `com.mergepilot.run_id="victim-run"` 绕过范围隔离。

### 7.2 修复方式

proxy 在 TRANSFORM 时，对安全标签做 **strip-then-inject**（不是 merge）：

```python
SECURE_LABEL_KEYS = {
    "com.mergepilot.scope",
    "com.mergepilot.run_id",
    "com.mergepilot.agent",
    "com.mergepilot.hardened",
}

def apply_hardening(body, kind, hardening_config):
    out = copy.deepcopy(body) if isinstance(body, dict) else {}
    labels = dict(out.get("Labels") or {})
    # STRIP any client-supplied secure label (case-insensitive, see §7.3)
    keys_to_remove = [
        k for k in labels
        if _canonical_label_key(k) in SECURE_LABEL_KEYS_CANONICAL
    ]
    for k in keys_to_remove:
        del labels[k]
    # INJECT authoritative values
    labels["com.mergepilot.scope"] = hardening_config.get("scope", "prod")
    labels["com.mergepilot.run_id"] = hardening_config.get("run_id", "")
    labels["com.mergepilot.agent"] = agent
    labels["com.mergepilot.hardened"] = "1"
    out["Labels"] = labels
    ...
```

### 7.3 绕过测试矩阵（D2B-3B 必须覆盖）

| # | 攻击 | 期望 |
|---|---|---|
| B5.1 | client 发 `Labels={"com.mergepilot.run_id":"victim"}` | strip 后 proxy 注入正确 run_id；转发 body 的 `Labels["com.mergepilot.run_id"]` == proxy 值 |
| B5.2 | 重复 key：JSON `{"com.mergepilot.run_id":"a","com.mergepilot.run_id":"b"}` | Python `json.loads` 取最后值；strip 仍移除；注入权威值（Python dict 不允许真重复，但需测解析器行为） |
| B5.3 | 大小写：`COM.MERGEPILOT.RUN_ID`、`com.MergePilot.Run_ID`、`com.mergepilot.RUN_ID` | canonical 比较（casefold）→ strip；不被绕过 |
| B5.4 | Unicode 同形/正规化：`𝐜ⓞⓜ.ⓜⓔⓡⓖⓔⓟⓘⓛⓞⓣ.run_id`（全角）、`com.mergepilo\u0074.run_id`（转义） | Docker label key 是字节串；proxy 按字节比较 + Unicode NFKC 正规化后 canonical 比较 → strip |
| B5.5 | 空值：`Labels={"com.mergepilot.run_id":null}`、`{"com.mergepilot.run_id":""}` | strip（key 存在即移除，不论值） |
| B5.6 | JSON 类型绕过：`Labels=[["com.mergepilot.run_id","x"]]`（数组而非对象） | body schema 校验 → DENY（D4.2/body 解析失败） |
| B5.7 | 嵌套：`Labels={"com":{"mergepilot":{"run_id":"x"}}}` | Docker Labels 是 flat map；嵌套 → schema DENY |
| B5.8 | 前缀混淆：`com.mergepilot.run_id.evil`、`com.mergepilot.run_id\x00evil`（null byte） | 精确 key 匹配；前缀/null 不命中 → 该 key 保留（非安全 key）但 run_id 仍注入；null byte → DENY（name/label 含 null） |

**canonical 比较函数**（D2B-3B 实现）：
```python
import unicodedata
def _canonical_label_key(k):
    return unicodedata.normalize("NFKC", k).casefold()
SECURE_LABEL_KEYS_CANONICAL = {_canonical_label_key(k) for k in SECURE_LABEL_KEYS}
```

---

## 8. 适配 v1.2.2 的命名/契约修订

MergePilot 升级到 v1.2.2 时（D2B-3B 或独立升级任务），以下必须同步：

| 维度 | 当前（v1.1.2 / hiclaw） | v1.2.2（agentteams） | 影响文件 |
|---|---|---|---|
| 容器名 regex | `^hiclaw-worker-[a-z0-9-]+$`（`harden_policy.py:35`） | `^agentteams-worker-[a-z0-9-]+$` | `harden_policy.py`、`worker_argv.py`、`managed_containers.py`、proxy deny D2.1/D2.2 |
| manager 名 | `hiclaw-manager`（`harden_policy.py:36`） | `agentteams-manager` | 同上 |
| 环境变量 | （MergePilot 侧无 HICLAW_* 依赖） | `AGENTTEAMS_PROXY_SOCKET=/run/mp/docker.sock`（proxy 重定向锚点，`config.go:292`） | deploy 脚本 + proxy 启动 |
| agt CLI | （v1.1.2 用 hiclab CLI） | `agt`（`agentteams-controller/cmd/agt/main.go`） | `create_hardened_worker.sh:6` `docker exec agentteams-controller hiclab create worker` → `agt create worker` |
| CRD | （v1.1.2 旧） | `agentteams.io/v1beta1`（`api/v1beta1/types.go`）；`Worker.spec.skills`（`types.go:187`，`[]string` built-in skills）+ `remoteSkills`（`:188`） | 若 MergePilot 用 CRD 创建 worker（当前是经 controller REST/Docker，非直接 CRD），则 spec 契约要对齐 |
| Worker.spec.skills | （无） | built-in skills 列表（`types.go:187`）；M5-0C 的 `m5c-controller` 经 controller 调 Skill DAG，不直接用此字段 | 设计上：proxy 不解析 skills（它只看 Docker API）；skills 由 controller 内部消费 |

**proxy 本轮的设计不变**：proxy 只看 Docker Engine API（method/path/body），不解析 v1beta1 CRD 或 Worker.spec.skills——那些是 controller 内部逻辑。proxy 的 name regex 与 env 锚点要适配 v1.2.2。

---

## 9. 主要绕过风险（v2，更新）

| # | 风险 | 设计对策 | 残余 |
|---|---|---|---|
| B1 | controller 不经 proxy，直连 `/var/run/docker.sock` | deploy 必须**只挂** proxy socket 到 controller 容器；controller 容器 spec 不挂原 socket（deploy 层断言） | D2B-3B 验证 controller 容器内 `/var/run/docker.sock` == proxy socket |
| B4 | 中转容器带 socket bind | D1.7 对**所有** create 强制（含 `Devices` + 所有 `Mounts` Type，补官方 proxy 缺口） | — |
| B5 | label 伪造 | §7 strip-then-inject + canonical 比较 + 绕过测试矩阵 | — |
| B6 | proxy RCE | 代码量最小化 + fuzz + 渗透测试 | D2B-3B |
| B7 | marker/pid 脱节 | marker 携带 pid，`validate_marker` 校验存活 | D2B-3B 同步扩展 |
| B8 | API 版本号协商 | `v1.x` 通配（`proxy.go:18-23` 官方 regex 已有 `(/v[\d.]+)?`）；版本号不参与 allow 决定 | — |
| B11 | **`/containers/{name}/archive` 投影任意文件**（`docker.go:266` SOURCE_PROVEN，controller 用于 auth token 投影） | ALLOW-NAMEPREFIX：目标 name 必须匹配 + 写入路径限定为 auth token 目录 | D2B-3B 验证 archive 路径范围 |
| B12 | **`/exec/{id}/start` hijack 后在 worker 内访问 socket** | worker 无 socket（controller 不挂，§2.1）；exec 目标 name-prefix 校验 | — |
| B13 | **`/images/create` pull 恶意镜像**（`docker.go:585`） | image allowlist（digest）；pull 请求的 `fromImage` 必须在 allowlist | — |

---

## 10. 诚实边界（v2）

- 本设计**未实现、未部署、未在真实 v1.2.2 HiClaw 上验证**。
- §3 的 SOURCE_PROVEN 项基于 v1.2.2 commit `849182a` 静态源码；若 v1.2.3+ 引入新端点，需重新审计。
- §4.3 的官方 proxy 校验缺口基于 `security.go:41-53` struct 字段（commit `849182a`）；上游若扩展 struct，需重审。
- §7 的 B5 修复是**设计**，未实现；D2B-3B 必须实现 + 测试。
- **legacy `agentteams-docker-proxy` 镜像源码不在仓库**（§2.2）——本文不对其下任何结论。
- 在 D2B-3B 部署并通过验收前，`hiclaw_live=false`、D2B-3 `BLOCKED_UPSTREAM`（option b）**不变**。

---

## 11. D2B-3B 精确实现范围（v2）

1. proxy daemon 实现（Python `socket` + `http.client`，监听 `/run/mp/docker.sock`，转发到 `/var/run/docker.sock`）。
2. `harden_policy.py` 扩展：
   - 新增 `deny_request()` + `process_request` 返回 `('deny', reason)`。
   - `apply_hardening` 改为 **strip-then-inject** label（§7）+ canonical 比较。
   - name regex 从 `hiclaw-*` 改为 `agentteams-*`（适配 v1.2.2）。
3. `guarded_start.validate_marker` 扩展 pid 校验 + 同步 `test_guarded_start.py`。
4. 受控 stub：`FakeUpstreamDaemon` + `ControllerStubClient`（模拟 §3 的 13 个 SOURCE_PROVEN 端点）+ `ProxyHarness`。
5. 验收矩阵 85+ 用例全 PASS（含 §7.3 的 B5.1-B5.8）。
6. deploy 步骤：启动 proxy + 写 marker + **改 controller 挂载点为 proxy socket** + 设 `AGENTTEAMS_PROXY_SOCKET`。
7. **仅在上述全过后**：更新 `UPSTREAM_BLOCKED.md` 状态 + 评估 `hiclaw_live`。
8. （可选）v1.1.2→v1.2.2 升级（容器名/env/CLI 重命名）作为独立任务，不阻塞 D2B-3B。

---

## 12. READY_FOR_D2B3_IMPLEMENTATION_V2 判定

**READY_FOR_D2B3_IMPLEMENTATION_V2**（条件就绪，需独立授权）。理由：
- ✅ v1.2.2 上游架构 source-proven（§1-§3，每条带 file:line）
- ✅ controller 的 docker.sock 路径确定（§2.1：`install.sh:4015` 直接挂载 + `docker.go:45` unix dial）
- ✅ 官方 proxy 保护边界确定（§4.2-4.4：**不保护 controller**，调用链证据）
- ✅ B5 label 伪造设计闭合（§7 strip-then-inject + canonical + 8 类绕过测试）
- ✅ 所有 [INFER] 已消除（§3 全部 SOURCE_PROVEN 或 UNKNOWN_DENY）
- ✅ 唯一推荐方案（§5：方案 C 自研窄代理，利用 `AGENTTEAMS_PROXY_SOCKET` 锚点）

未达成（诚实）：D2B-3 未实现/未部署/未在真实 v1.2.2 上验证；`hiclaw_live=false` 不变。
