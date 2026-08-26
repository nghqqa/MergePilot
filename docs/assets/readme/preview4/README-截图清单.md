# README 截图清单（preview4 轮）

> 生成时间：2026-08-26 · 截图操作者：ZCode 代理 · 本文件是本轮 README 截图的审核记录。

## 获取方式（如实记录）

- 页面来源：仓库 HEAD 的 `tools/demo_console/live_assets/e2e-status.html` + `live-refresh.js`（当前修复后的页面本体）。
- 伺服方式：本地静态伺服于 `127.0.0.1:8600`，将 `/api/e2e/status` 端点映射为投影 JSON——与正式栈"挂载 status.json 由 API 暴露"的机制一致。**测试栈（WSL）本轮未启动**，如需严格全栈截图可后续复拍。
- 截图工具：Chrome 151 headless（`--headless=new`，`--force-device-scale-factor=1`，无浏览器个人信息）。
- 数据源：`docs/preview/projections/complete.run35.json`（真实 E2E 运行 `b8-e2e-run35`，github_e2e=true）与 `failed.fixture.json`（生产投影函数派生的演示夹具）。

## 截图清单

| 文件 | 尺寸 | 页面状态 | 数据源 | 用途 | 证明的事实 | 不证明的事实 | 允许放入 README |
|---|---|---|---|---|---|---|---|
| `console-overview-preview4.png` | 1600×1000 | complete（首屏） | `complete.run35.json` | 首屏控制台总览 | 17 阶段时间线全绿收敛、五项真实性边界常驻、`direct_routing_verified=false` 可见 | 生产验证、独立物理机验收 | 是（已放入） |
| `console-complete-preview4.png` | 1600×2400 | complete（全页证据） | `complete.run35.json` | 完整证据区 | 16/16 前置、6/6 路由边逐边 VERIFIED、Receipt/Matrix verified、wsl-user-relay 如实标注 | 内核直连路由已验证 | 是（已放入） |
| `console-failed-preview4.png` | 1600×1000 | failed | `failed.fixture.json`（演示夹具） | 失败定位能力 | 第 10 阶段红、首个稳定错误 `E2E_ROUTE_PROBE_FAILED` 置顶、失败路由边、后续阶段灰显 | 真实客户事故或生产故障 | 是（已放入，说明中已标注为演示夹具） |
| `console-mobile-preview4.png` | — | — | — | 移动端展示 | 390px 视口实测布局横向溢出、时间线截断 | — | **否**（已删除；记录为待验证：控制台未适配窄屏） |

## 旧图处置

- 根 README 本轮之前**没有任何截图引用**（仅 badge），无旧图替换。
- `.impeccable/review/` 旧截图（desktop.png 等）保留不动，属历史评审资产；历史材料目录已由 `HISTORICAL-SNAPSHOT.md` 标注。
- 已知残留：`docs/复赛材料/finals-v1/` PPT P12 页 Sources 仍引用 `.impeccable/review/desktop.png`（旧图）。修改需重渲染 PPT/PDF，超出本轮"只改 README 与图片资产"范围，**列为后续待办**。

## 验证记录（2026-08-26）

- 图片引用存在性：3/3 OK；本地链接（快速入口、badge 目标）全部存在；正式 Release URL 返回 200。
- 旧口径扫描（README）：`preview.3` / `379744d` / `2246` = 0。
- 禁语扫描：仅两处**否定性披露**（"不等于 production ready"、"不使用'可观测侧车'或'MCP sidecar'"），无肯定性声称；`EXTERNAL_ACCEPTED` / beautify 署名 = 0。
- 秘密/绝对机器路径扫描：0。
- `git diff --check`：clean（仅行尾符警告，非阻断）。
- 敏感信息目检（视觉模型复核）：三张截图均无用户名、主机名、IP、token。

## 待人工确认

- 截图在 GitHub 暗色/亮色主题下的观感（页面为深色背景，暗色主题下对比度需目检一次）。
- 若评审要求"全栈真实伺服"截图，可在测试栈启动后按本清单同参数复拍。
