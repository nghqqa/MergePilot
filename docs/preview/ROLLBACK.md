# MergePilot v0.1 Preview — 回退合同

版本 v0.1.0-preview.1 · 本文档是包的正式回退承诺；所有路径相对仓库根。

> **硬标注**：`transport_profile=wsl-user-relay`，
> `direct_routing_verified=false`（经中继）；五项真实性边界
> （应用集成/数据库/生产/revision 契约/audit 契约）全部 false / NOT_VERIFIED——
> 回退或重装不会翻转任何边界，本包不构成生产验证。

## 1. 安装失败回退（自动，无需人工）

- **镜像导入失败**（`docker load` rc≠0）：bootstrapper 立即抛错终止；
  未导入完的镜像层由 Docker 自身的 layer store 原子性保证——
  不会出现"半个镜像"被 start 使用。重试 `-Action Install` 即可。
- **start 失败**：CLI 语义为**失败即回滚本会话全部资源**
  （容器/网络/秘密，`failed_rolled_back`），不留部分栈；这一点由
  `run_e2e_start`/`_rollback_all` 的生产路径保证并有测试覆盖
  （`tests/gh_app/test_e2e_lifecycle_r2.py::TestRollbackMatrix`）。
- **bootstrapper 侧兜底**：start 抛错时同时终止 keepalive 进程并删除
  PID 文件（见 `bootstrapper.ps1` 的 `catch` 分支），无悬挂保活。

## 2. 版本升级回退（保留旧 digest 与 manifest）

升级流程：新版本 `-Action Install` → 若失败，按第 1 节自动回退；
若成功但需退回旧版本：

1. `.\bootstrapper.ps1 -Action Stop`（会话资源清零，见第 3 节）；
2. 旧版本镜像 digest 已保留在：
   - `release\preview\manifests\install.current.json`（每次成功 Install 后快照）
   - `dist\preview-v0.1.0\manifest.json`（打包时的完整 digest 清单）
   - 历史 digest 记录：`D:\goai\temp\m8gh4-run27\staging_pre_images.txt`
     （v0.1 升级前的 8 镜像旧 digest）
3. 回退方式（二选一）：
   - **源码回退**（推荐，始终可用）：`git checkout <旧版本提交>` →
     `.\bootstrapper.ps1 -Action Install -BuildFromSource` → `-Action Start`；
   - **镜像回退**：若旧 digest 仍在本地 daemon（`docker images --digests`
     或按 sha256 前缀查询），`docker tag <旧digest> <镜像名:local>` 后
     直接 `-Action Start`。注意：`-Action Cleanup` 会删除本地镜像，
     执行 Cleanup 前请确认已留档 digest 或旧包 tar。

## 3. stop / cleanup 残留为零

- `stop`：移除**本会话**容器、网络、秘密（relay 清理按 journal 归属字段
  幂等逆序执行：探测容器 → 中继容器 → systemd 单元 → iptables 输入规则 →
  网络；归属不明的资源保留并报告，绝不前缀猜测删除）；
- `cleanup`：stop + 删除本地镜像 + 删除安装清单；**journal、receipt、
  run35 历史证据目录永不触碰**；
- 验证残留为零（v0.1 发布轮实测记录）：
  - `docker ps -a | grep -c mergepilot` → 0；`docker network ls | grep -c mergepilot` → 0
  - relay/临时容器/网络 grep → 0
  - `iptables-save` 无任何 `mergepilot|relay` 手工规则
    （指纹差异仅为 Docker 自有桥规则）
- keepalive 卫生：保活是**单个持续前台 `wsl.exe` sleep 进程**，
  PID 落盘 `.mergepilot\preview-keepalive.pid`，`Stop`/`Cleanup` 时终止；
  无计划任务、无短周期退出型任务。

## 4. 数据与证据（回退中保留的对象）

| 对象 | 保留策略 |
|---|---|
| `.mergepilot/github-e2e.json`（run35 journal） | 永久，stop/cleanup 不触碰 |
| `D:\goai\temp\m8gh4-run27\evidence\`（run27–35 输出） | 永久（操作机路径） |
| `.mergepilot/public/status.json`（live 投影） | 随下一次 start 由单写者重建 |
| `release\preview\manifests\install.current.json` | 每次成功 Install 覆盖前**先归档**为 `install.previous.json` |
| 镜像 digest 清单 | 见第 2 节三处 |

## 5. 安全验证边界（本合同不做的事）

- 不执行破坏性卸载演练（删除用户数据/发行版）；
- 不读取或输出任何秘密正文（PAT/PEM/Matrix token/MinIO credential）；
- staging 回退演练以 stop/dry-run 为限，不破坏已验证的 HiClaw 运行态。
