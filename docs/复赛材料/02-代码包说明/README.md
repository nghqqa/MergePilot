# MergePilot v0.1.0-preview.3 代码包说明

Release：<https://github.com/nghqqa/MergePilot/releases/tag/v0.1.0-preview.3>

## 支持范围

- Windows 11 + WSL2本地Preview。
- 默认发行版名`MergePilot-Test`，可通过受控参数指定其他已注册发行版。
- Windows发布边仅监听`127.0.0.1:8600`和`127.0.0.1:8090`。
- 这是Pre-release；同机验收已通过，独立物理机验收未完成。

## Release资产

| 资产 | 用途 |
|---|---|
| `mergepilot-v0.1.0-preview.3-package.zip` | bootstrapper、文档、配置模板与样例投影 |
| `images-oci.tar` | 9个离线镜像，847.4MB |
| `checksums.sha256` | 交付文件SHA-256 |
| `manifest.json` | commit、镜像pin、端口和真实性边界 |

下载后先校验`checksums.sha256`和`manifest.json`。checksum或镜像集合不匹配时，Install在`docker load`前fail-closed。

## 生命周期入口

```powershell
powershell -ExecutionPolicy Bypass -File .\bootstrapper.ps1 -Action Check
powershell -ExecutionPolicy Bypass -File .\bootstrapper.ps1 -Action Install -ImageTar .\images-oci.tar
powershell -ExecutionPolicy Bypass -File .\bootstrapper.ps1 -Action Doctor
powershell -ExecutionPolicy Bypass -File .\bootstrapper.ps1 -Action Start
powershell -ExecutionPolicy Bypass -File .\bootstrapper.ps1 -Action Status
powershell -ExecutionPolicy Bypass -File .\bootstrapper.ps1 -Action Stop
powershell -ExecutionPolicy Bypass -File .\bootstrapper.ps1 -Action Cleanup
```

实际执行以Release包内README为准。控制台入口：<http://127.0.0.1:8600/e2e-status.html>。

## 安全与回退

- bootstrapper不读取、输出或打包秘密正文。
- Worker不持GitHub PAT；PAT仅进入GitHub MCP隔离服务。
- Start在首个mutation前持久化资源owner，失败后逆序清理。
- Stop保留镜像与历史证据；Cleanup仅删除工具自有镜像和安装状态。
- keepalive与Windows loopback forwarder使用PID、进程名、发行版和token共同证明owner。

## 已知限制

- `SAME_MACHINE_ACCEPTED`，但`EXTERNAL_BLOCKED`：尚无独立Windows 11物理机验收。
- 传输配置为`wsl-user-relay`，`direct_routing_verified=false`。
- 安装包较大，9镜像tar为847.4MB。
- 冻结代码门禁为2246 passed / 20 skipped；skipped项目必须按测试报告语义保留，不写成全量执行。
- Release不标Latest，也不代表production ready。

## 真实性边界

```text
application_integration_verified=false
database_verified=false
production_verified=false
revision_producer_contract=NOT_VERIFIED
audit_producer_contract=NOT_VERIFIED
direct_routing_verified=false
transport_profile=wsl-user-relay
```
