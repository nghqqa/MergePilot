# MergePilot v0.1 Preview — 快速开始（Windows + WSL2）

> 版本：v0.1.0-preview.2 · git SHA 以 manifest.json 的 git_commit 为准 · 只读运维控制台 + 真实 GitHub E2E 证据
>
> **真实性声明**：本 Preview 的传输档案为 `transport_profile=wsl-user-relay`，
> `direct_routing_verified=false`（跨桥边经由用户态 TCP 中继，非直连路由）；
> 五项真实性边界**全部未验证**：
> `application_integration_verified=false` · `database_verified=false` ·
> `production_verified=false` · `revision_producer_contract=NOT_VERIFIED` ·
> `audit_producer_contract=NOT_VERIFIED`。
> 控制台是发布管道，不构成应用集成，也不等同于生产验证。

## 前置条件

- Windows 10 2004+ / Windows 11，WSL2 已启用
- WSL 发行版内可用的 Docker daemon（本仓在 `MergePilot-Test` 发行版验证）
- Python 3.10+（宿主机，运行 MergePilot CLI）
- 磁盘 ≥ 8 GB（镜像与证据）；本机 loopback 端口 8600 / 8090 空闲

## 一条命令检查环境

```powershell
powershell -ExecutionPolicy Bypass -File release\preview\bootstrapper.ps1 -Action Check
```

输出 Windows/WSL2/Docker/端口/磁盘五项检查，任何一项 FAIL 即停止。

## 安装与启动

```powershell
# 方式 A：从预构建 OCI tar 导入镜像（推荐，离线可装）
powershell -ExecutionPolicy Bypass -File release\preview\bootstrapper.ps1 -Action Install -ImageTar dist\preview-v0.1.0\images-oci.tar

# 方式 B：从源码构建（需要网络拉取 python:3.12-slim 与 pgvector 基础镜像）
powershell -ExecutionPolicy Bypass -File release\preview\bootstrapper.ps1 -Action Install -BuildFromSource

# 启动（run-id 必须是播种用例：run-showcase-a / b / c）
powershell -ExecutionPolicy Bypass -File release\preview\bootstrapper.ps1 -Action Start
```

打开控制台：**http://127.0.0.1:8600/e2e-status.html**（仅 loopback，外部不可达）。

## 日常操作

```powershell
.\bootstrapper.ps1 -Action Status    # 栈分类：absent / partial / healthy
.\bootstrapper.ps1 -Action Doctor    # 只读环境与栈体检
.\bootstrapper.ps1 -Action Stop      # 移除会话容器/网络/秘密（保留镜像与证据）
.\bootstrapper.ps1 -Action Cleanup   # stop + 删除本地镜像 + 删除安装清单
```

底层等价命令（bootstrapper 即它们的包装）：

```
python tools/cli/mergepilot.py install | doctor | status | start --run-id X | stop | cleanup
```

## WSL 休眠说明

WSL2 发行版在无前台进程时自然休眠，staging 容器随之暂停（持久保留）。
恢复：唤醒发行版后容器自动续跑，或重新执行 `-Action Start`。
bootstrapper 的 `Start` 会以**持续前台 `wsl.exe` 进程**保活会话窗口，
`Stop` 后自动结束保活（无短周期退出型任务、无计划任务残留）。

## 回退

安装失败自动回退（CLI start 失败即回滚本会话全部资源）；版本升级回退见
[ROLLBACK.md](ROLLBACK.md)（保留上一版本镜像 digest 与安装清单）。

## 下一步

- [DEMO-SCRIPT.md](DEMO-SCRIPT.md) — 5–8 分钟演示脚本
- [ARCHITECTURE-SECURITY.md](ARCHITECTURE-SECURITY.md) — 架构与安全边界
- [PROJECTIONS.md](PROJECTIONS.md) — complete / failed / stale 三种真实投影
- [SCREENSHOTS.md](SCREENSHOTS.md) — 控制台截图索引
