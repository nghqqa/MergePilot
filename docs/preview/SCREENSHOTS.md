# 控制台截图索引

版本 v0.1.0-preview.1 · 全部为真实浏览器捕获（IAB fullPage），
PNG 内嵌 `Description` tEXt 出处块（视口 / 投影 / 捕获方式 / 时间戳）。
源文件位于仓库 `.impeccable/review/`。

| 文件 | 视口 | 投影状态 | 用途 |
|---|---|---|---|
| `.impeccable/review/desktop.png` | 1280×800 | complete（run35 真实投影） | 主证据：应用栏/状态带/17 阶段时间线/诊断栏/六边路由表 |
| `.impeccable/review/wide.png` | 1440×900 | complete | 宽屏布局（双栏 + 332px 诊断轨） |
| `.impeccable/review/mobile.png` | 390×780 | complete | 移动端：应用栏换行收拢、路由表→可展开边卡片、无横向溢出 |
| `.impeccable/review/mobile-tall.png` | 412×915 | complete | 高屏移动端全页 |
| `.impeccable/review/desktop-failed.png` | 1280×800 | failed（生产代码派生演示投影） | 失败态：Failed 裁决、第 10 行错误框、FAIL 路由行、置顶稳定错误横幅 |

注意：fullPage 截图由视口条带拼接，条带接缝处的"重复应用栏"是**捕获工艺**，
不是页面缺陷——独立 finish reviewer 已以非拼接捕获复测布局并裁定 APPROVED。

## 每张截图都应能看到的三件事

1. `直连路由：false（经中继）` —— `transport_profile=wsl-user-relay` 的如实标注；
2. 五项真实性边界全部 `NOT_VERIFIED`；
3. 页脚 `只读视图 Read-only —— 本控制台不提供任何写操作`。

live（未知态/未提供）与 stale（陈旧）为行为态，无静态截图；
现场演示步骤见 [DEMO-SCRIPT.md](DEMO-SCRIPT.md) 第 2、5 段。
