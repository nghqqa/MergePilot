# MergePilot v0.1 Preview 包说明

版本 `v0.1.0-preview.1` · git `5e10cca` · 平台 Windows + WSL2 · **loopback-only**

## 包内物

| 路径 | 说明 |
|---|---|
| `bootstrapper.ps1` | Windows 入口：`Check / Install / Start / Status / Doctor / Stop / Cleanup` |
| `images-oci.tar` | 8 个栈镜像的 `docker save` OCI tar（离线 `docker load` 导入） |
| `docs/` | 快速开始 / 架构与安全边界 / 演示脚本 / 三投影说明 / 截图索引 / 回退合同 |
| `manifest.json` | 版本 manifest：git SHA、镜像 digest、文件清单、绑定与真实性边界声明 |
| `checksums.sha256` | 全部交付文件（含 tar）的 SHA-256 |

源码仓即安装目标：bootstrapper 假定本仓检出存在，包内不重复携带源码。
镜像也可不经 tar 由 `bootstrapper.ps1 -Action Install -BuildFromSource` 从源码重建。

## 安装（管理员 PowerShell 不需要；普通用户即可）

```powershell
.\bootstrapper.ps1 -Action Check
.\bootstrapper.ps1 -Action Install -ImageTar images-oci.tar
.\bootstrapper.ps1 -Action Start            # 默认 run-showcase-a
# 控制台: http://127.0.0.1:8600/e2e-status.html
```

校验完整性：

```powershell
Get-Content checksums.sha256
certutil -hashfile images-oci.tar SHA256     # 与 manifest.json 对照
```

## 三句硬标注（写入 manifest，任何演示都必须带上）

1. `transport_profile = wsl-user-relay`
2. `direct_routing_verified = false`（页面显示 `false（经中继）`）
3. 五项真实性边界全部 `false / NOT_VERIFIED`：
   application_integration_verified / database_verified / production_verified /
   revision_producer_contract / audit_producer_contract

部署、演示或安装本包**不会**翻转任何边界；本包是 staging/demo 级 Preview，
**不是生产验证**。

## 回退

见 `docs/ROLLBACK.md`（安装失败自动回退；版本升级保留旧 digest 与 manifest；
stop/cleanup 后会话资源零残留）。
