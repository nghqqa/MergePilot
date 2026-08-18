# MergePilot CLI(最小本地操作入口,开发预览)

`mergepilot` 是 M8 隔离栈(one-click isolated stack)的本地操作 CLI:把
`tools/demo_console/one_click_startup.py` 的版本化计划生成器接到真实执行器上,
覆盖构建、体检、启动、状态、停止与清理六个命令。

**定位与边界**:它只是隔离栈的本地操作入口——不是 GitHub App,不是生产验证,
不是 SaaS,不改变任何真实性边界(`database_verified=false`、
`application_integration_verified=false`、`production_verified=false`、
`revision_producer_contract=NOT_VERIFIED`、`audit_producer_contract=NOT_VERIFIED`
全部保持原值)。

## 平台限制(明确声明,不外推)

- 仅支持 **Windows 10/11 + WSL2 发行版 `MergePilot-Test`** 上的隔离开发预览。
- 所有 Docker 命令经 `wsl.exe -u root -d MergePilot-Test -- docker` 以 argv 数组
  执行(禁止 shell 执行)。
- **不声称支持** Linux、macOS、原生 Windows Docker(npipe)、远程/TCP/SSH daemon
  或任何生产环境。
- `MergePilot-Test` 必须已处于 Running;CLI 绝不隐式启动缺失/Stopped 的发行版
  (doctor 给出明确失败项 `DOCTOR_DISTRO_STOPPED`)。
- 镜像合同:digest 钉死的 pgvector + 5 个本地构建镜像(`pull=never`,无标签回退)。
- `EPHEMERAL_PG_VERIFY` 是测试链路的授权门,不属于 CLI 用户合同。

## 安装(源码 checkout)

```bash
cd <mergepilot-checkout>
pip install -e .
mergepilot doctor        # 先体检
```

运行时仅依赖 Python 标准库;`--project-dir` 显式指定 checkout(默认当前目录)。

## 六命令合同

| 命令 | 语义 | 幂等合同 |
|---|---|---|
| `install` | 构建 5 个本地镜像,inspect 后把真实 image ID 记入 install manifest;支持 `--dry-run` | 重复执行=重建(层缓存),内容一致时 image ID 不变 |
| `doctor` | 纯只读检查:Python/项目布局/planner 计划链/WSL 发行版/daemon endpoint/`DOCKER_HOST`/daemon 指纹/基础镜像/本地镜像/端口/栈状态;逐项稳定 code | 天然只读;全过 0,否则 3 |
| `start --run-id <id>` | run_id 严格匹配 `^[A-Za-z0-9_-]+$`(如 `run-showcase-a`);每会话现生成三个秘密 env 文件;按 planner 顺序启动、等健康、实测 postgres bridge IP、准备数据库、断言 preflight exit 0 且末行 `PREFLIGHT_OK`;可选 `--m4f`、`--dry-run` | 已健康且 run_id 相同 → 0(幂等空转);run_id 不同或 partial → 4,不自动修复 |
| `status` | 只读输出 absent/partial/healthy:6 容器、2 网络、preflight 终态与 `127.0.0.1:8600/api/live/status`;支持 `--json` | 只读;absent/healthy → 0,partial → 3 |
| `stop` | 逆序删除 session 容器与网络、删除并验证秘密文件消失、删除 session manifest;**保留** install manifest 与镜像;支持 `--dry-run` | 资源已不存在=幂等成功;命令失败不得冒充 absent |
| `cleanup` | 默认 dry-run;仅 `--apply` 执行 stop + 删除 5 个**已核验 image ID** 的本地镜像 + install manifest | 残留验证失败返回 9 |

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功,或明确的幂等空转 |
| 2 | CLI 参数错误 |
| 3 | 环境/配置/doctor 前置检查失败,零副作用 |
| 4 | 既有资源或状态冲突(不猜测所有权、不自动修复) |
| 5 | 执行失败但回滚已验证成功 |
| 9 | rollback 或残留验证失败(需人工介入) |

`--json` 输出结构稳定,包含 `command` / `status` / `code`(及 doctor 的
`checks`、其他命令的 `resources`),不含任何秘密。

## 状态文件与秘密

状态位于 `<project>/.mergepilot/`(已加入 `.gitignore`):

- `install.json` — 仅 schema 版本、项目根目录、镜像 tag → 真实 image ID。
- `session.json` — run_id、创建阶段(stage)、容器/网络真实 ID、secret 文件名。
  **它同时就是写前 journal**:第一次 Docker 写操作前创建,每成功创建一个
  网络或容器立即 inspect 并原子记录真实 ID。
- `secrets/{postgres.env, controller.env, demo_console.env}` — 每会话现生成
  (`secrets.token_urlsafe`),经 planner 的 0600 env-file 传输;**密码/DSN 永不
  出现在 argv、日志、manifest 或 JSON 输出中**(收集侧统一 redaction)。

两个 manifest 均以临时文件 + `os.replace()` 原子更新,且永不保存密码、DSN、
token 或 env 文件内容。

## 回滚与所有权模型

- `start` 中途失败:仅按 journal **逆序**回滚本次创建的资源(容器按 ID、网络按
  ID),随后删除秘密文件与 journal;主失败与回滚失败同时报告,互不吞没。
  回滚干净 → 5;回滚/残留验证失败 → 9。
- `stop`/`cleanup` 可按固定名称发现资源,但删除前必须核对 manifest 中的真实 ID:
  **名称存在而 ID 不一致 → fail-closed(4),不得删除**。
- session 缺失但发现同名资源 → 所有权冲突(4),不猜测所有权。
- 一次失败后残留(9)时,`cleanup --apply` 是唯一的人工核验后清场入口。

## cleanup 边界

`cleanup --apply` 只删除:session 记录的容器/网络(逐项 ID 核验)、三个秘密
env 文件、5 个与 install manifest ID 核验一致的本地镜像、两个 manifest 文件。
栈本身 one-shot(compose `restart: "no"`、无持久卷、`PGDATA=/tmp/pgdata`),
**没有可备份的持久数据**;镜像删除后由 `install` 从源码重建。CLI 不触碰
`hiclab-*`(AgentTeams)、`.gstack`、`evidence/`、`verification/` 或任何
非精确名资源。

## 测试

`tests/isolated_live/test_mergepilot_cli.py` 直接驱动 production 入口
(`tools.cli.mergepilot:main`),全部 mock WSL/Docker/HTTP——不接触真实服务。
运行:

```bash
python -m pytest -q tests/isolated_live/test_mergepilot_cli.py --import-mode=importlib
```
