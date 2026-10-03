# RAG 模型安装（自托管 · ModelScope 官方通道）

自托管部署的可选增量功能：从 **ModelScope 官方源**安装 bge-m3 嵌入模型，把 RAG 试用检索从
local-hash 确定性基线（256 维，R@5≈0.62）升级到真实语义检索（1024 维，准入实测 R@5=0.9615）。

## 信任模型（为什么这样做是安全的）

- **工件来源**：下载 URL 只来自仓库内版本化 manifest（`deploy/rag-model-install/bge-m3.modelscope.manifest.json`），
  域名白名单（`modelscope.cn`）外拒绝、跨域重定向拒绝；console 不提供任意 URL 下载。
- **完整性**：manifest 的逐文件 sha256 来自 ModelScope 官方文件 API 的 `Sha256` 字段
  （钉死在 revision `e44369c5623cc…`）；2026-10-03 波内已做官方通道下载字节级三方一致核验
  （官方 API = 下载实测 = manifest）。安装时逐文件复算，**哈希不匹配拒绝激活**（fail-closed）。
- **许可证**：bge-m3 为 MIT（官方 README）。
- **回退**：local-hash 永为安全基线；激活后可随时一键回退（安装保留，可重激活）。

## 使用（控制台）

「知识库」页 → 「RAG 模型安装」面板：

1. 点 **安装（官方下载）**（约 2.1 GiB；中断后再点=断点续传；进度条实时）；
2. 校验自动执行；全文件 sha256 通过 → **就绪**；
3. （部署侧需先起 sidecar，见下）点 **激活 bge-m3**——三重门探测（/health → /manifest 字节级
   pin → /embed 维度冒烟）任一不过即拒并给出错误码；
4. 回退：点 **回退到 local-hash**（即时生效）。

面板同页披露：来源/revision/许可证/文件清单与逐文件期望 sha256/最近错误码。全程审计
（只落元数据）。写操作仅 platform_admin。

## 部署侧 sidecar（激活前置）

console 不 spawn 容器；sidecar 为 compose 固定服务（模型卷挂载到安装目录）：

```yaml
  rag-m3-sidecar:
    build: ./deploy/rag-prod/bge-sidecar        # 仓库自带（python:3.11-slim + numpy/tokenizers）
    volumes:
      - ${RAG_MODEL_ROOT:-./rag-models}:/app/model:ro
    environment:
      BGE_MODEL_DIR: /app/model
      BGE_MANIFEST: /app/model/bge-m3/manifest.json   # 激活时由 console 按清单生成
      BGE_MAX_LEN: "128"
    # 不对外暴露端口——console 经服务网络访问
  beta-mp-console:                                # 你的 console 服务
    environment:
      RAG_MODEL_ROOT: /rag-models                 # 与上面同卷
      RAGTRIAL_EMBED_ENDPOINT: http://rag-m3-sidecar:8080/embed
```

sidecar 启动即按 manifest 逐文件 sha256 fail-closed 校验；任何缺失/漂移拒绝服务。
未配置 `RAGTRIAL_EMBED_ENDPOINT` 时激活如实返回 409（不猜测）。

## 状态机与故障排查

`UNINSTALLED → DOWNLOADING → VERIFYING → READY → ACTIVE`；
失败态：`DOWNLOAD_FAILED`（重试点安装=续传）/ `HASH_MISMATCH`（重下；**不可激活**）/
`INSUFFICIENT_DISK`（预留 1 GiB 余量后重试）/ `SIDECAR_START_FAILED`（查 sidecar 健康与
manifest 一致性）/ `ACTIVATION_FAILED`。取消保留已下载部分供续传。

## API（供脚本化运维；写操作需 manage_instance+CSRF）

- `GET  /api/mu/rag-model/install?model_key=bge-m3` 状态；`GET …/manifest` 清单
- `POST /api/mu/rag-model/install`（安装/续传，202 异步启动）；`…/install/cancel`
- `POST /api/mu/rag-model/install/verify`（重校验）；`…/install/log`（审计摘要）
- `POST /api/mu/rag-model/activate` / `…/rollback`

## 版本演进

实现工件变化时**不要改本目录 manifest 的 sha256**——在控制台发布递增版本（旧版本不可变）；
manifest 变更本身=发版（可审查可回滚）。

## 与 C 波的关系

本波只做安装与激活控制面；**Skill/RAG 调用留痕是 C 波**——本波不伪造任何调用记录。
