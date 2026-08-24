---
name: MergePilot E2E Operations Console
description: 安静、密集、可扫描的只读工程控制台——30 秒内判断一次 E2E 运行的可信度、失败阶段、网络路径与残留。
colors:
  workspace-ground: "#f4f4f1"
  panel-white: "#ffffff"
  inset-gray: "#eceded"
  nav-ink: "#17191e"
  nav-line: "#2c2f36"
  nav-text: "#e9eae6"
  nav-dim: "#9aa0a8"
  accent-teal: "#0e6b62"
  accent-teal-press: "#0a524b"
  status-green: "#17693c"
  status-green-bg: "#e4f0e6"
  status-amber: "#8a5a00"
  status-amber-bg: "#f6eedb"
  status-red: "#b3261e"
  status-red-bg: "#f9e6e4"
  status-blue: "#1d5da4"
  status-blue-bg: "#e3ecf6"
  text-primary: "#1f2328"
  text-secondary: "#575e68"
  text-tertiary: "#656c76"
  border-hairline: "#d9dad4"
  border-strong: "#b9bab2"
typography:
  body:
    fontFamily: "system-ui, 'Segoe UI', 'Microsoft YaHei', sans-serif"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.45
  label:
    fontFamily: "system-ui, 'Segoe UI', 'Microsoft YaHei', sans-serif"
    fontSize: "11.5px"
    fontWeight: 650
    lineHeight: 1.3
  data-mono:
    fontFamily: "ui-monospace, 'Cascadia Mono', Consolas, Menlo, monospace"
    fontSize: "11.5px"
    fontWeight: 400
    lineHeight: 1.4
rounded:
  sm: "3px"
  md: "5px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "14px"
  lg: "18px"
components:
  button-refresh:
    backgroundColor: "{colors.nav-line}"
    textColor: "{colors.nav-text}"
    rounded: "{rounded.md}"
    height: "32px"
    padding: "0 12px"
  button-refresh-hover:
    backgroundColor: "#383c44"
  verdict-chip-ok:
    backgroundColor: "{colors.status-green-bg}"
    textColor: "{colors.status-green}"
    rounded: "{rounded.md}"
    padding: "5px 12px"
  verdict-chip-err:
    backgroundColor: "{colors.status-red-bg}"
    textColor: "{colors.status-red}"
    rounded: "{rounded.md}"
    padding: "5px 12px"
  stage-status-chip:
    rounded: "{rounded.sm}"
    padding: "1px 8px"
  panel-card:
    backgroundColor: "{colors.panel-white}"
    rounded: "{rounded.md}"
    padding: "12px 14px"
---

# Design System: MergePilot E2E Operations Console

## Overview

**Creative North Star: "The Shift-Handover Board"（交接班看板）**

这是一块挂在维护者工位上的交接班看板：数据自己说话，界面不插话。深色导航结构像车间的钢梁骨架，浅暖中性工作区像摊开的值班日志；绿、琥珀、红只用于生命状态，从不用于装饰。整个系统服务一个动作——30 秒内判断"这次运行可不可信、卡在哪、网络走的哪条路、有没有残留"。

密度是刻意的：10.5–14px 的字号阶梯、1px 分隔线代替留白堆叠、右栏诊断轨与左侧时间线在 960px 以上并列。安静来自克制：无渐变、无玻璃、无阴影堆叠、无入场动画，唯一的强调色（深青 #0e6b62）只出现在焦点环和数据来源标记上。诚实优先于安抚——缺失字段写"未提供"，未验证边界写 NOT_VERIFIED，冻结的日志投影会把年龄如实走秒。

**Key Characteristics:**
- 浅暖中性工作区（#f4f4f1）+ 深色导航条（#17191e）的双层结构
- 状态三色（绿/琥珀/红）永远是"图标 + 文字"，绝不只靠颜色
- 等宽字体只用于代码、标识符与测量值，从不作为"科技感"戏服
- 150–200ms 缓动过渡，`prefers-reduced-motion` 下全部归零
- 所有交互只读；页脚明示"本控制台不提供任何写操作"

## Colors

角色分工明确的克制调色板：中性做 95% 的界面，三个状态色做剩下的 5%，一个强调色做焦点。

### Primary
- **深青（Deep Teal）** (#0e6b62)：唯一强调色。只出现在键盘焦点环与"单写者"来源标记上。稀缺即意义。

### Secondary
- **生命绿（Quiet Green）** (#17693c / 底 #e4f0e6)：通过、verified、Complete。
- **值班琥珀（Duty Amber）** (#8a5a00 / 底 #f6eedb)：未知态、陈旧、待执行以外的前瞻警示。
- **故障红（Signal Red）** (#b3261e / 底 #f9e6e4)：失败、未通过、FAIL、稳定错误码。
- **进行蓝（Work Blue）** (#1d5da4 / 底 #e3ecf6)：running 专用。蓝只是状态之一，不是主题色。

### Neutral
- **暖灰地（Warm Paper）** (#f4f4f1)：页面地面。
- **面板白** (#ffffff)：时间线、表格、诊断轨的容器。
- **内嵌灰** (#eceded)：表头、阶段序号列、待执行芯片。
- **墨黑导航** (#17191e / 线 #2c2f36)：唯一深色区域，锚定页面结构。
- **文字三级** (#1f2328 / #575e68 / #656c76)：正文/次要/注释。最弱级在最差底色上 ≥4.5:1。

### Named Rules
**The 5% Status Rule.** 三个状态色合计不超过任何视口面积的 5%。它们出现即意味着状态判断，绝不用于分组、强调或装饰。
**The Never-Color-Only Rule.** 每一个状态表达都是"图标 + 文字 + 颜色"三元组；色盲视图下信息零损失。

## Typography

**Display Font:** 无（工程控制台没有 display 层级；最大字号是 14px 的裁决芯片）
**Body Font:** system-ui（13px 基准）, 'Segoe UI', 'Microsoft YaHei', sans-serif
**Label/Mono Font:** 'Cascadia Mono', 'SF Mono', Consolas, monospace

**Character:** 系统字体栈 + 中文系统字体，零字距（letter-spacing: 0），字号阶梯刻意紧密（10.5–14px），层级靠字重（400/650）与颜色而不是字号跳跃——这是密集可扫描的看板，不是杂志。

### Hierarchy
- **裁决（Verdict）** (650, 14px)：状态带里的 Complete/Failed/陈旧 芯片。
- **节标题（Section）** (650, 12px)：时间线/路由表的 h2，底部 1px 分隔线，右侧悬挂来源注释。
- **正文（Body）** (400, 13px)：状态带键值、诊断行、通知。
- **注释（Meta）** (400, 11px)：evidence 数字、表头、"未提供"。
- **数据（Data）** (mono 400, 11.5px)：错误码、run id、边名、时间戳、布尔值。

### Named Rules
**The Tight-Ladder Rule.** 字号阶梯保持 10.5–14px 的紧密跨度（检测器实测 1.3:1）。放宽阶梯之前先问：这是要给人读的看板，还是要给人欣赏的海报。

## Layout

- 应用栏（46px，sticky，深色）→ 状态带（flex-wrap 键值行）→ 主区（max-width 1180px 居中）。
- 960px 以上：`minmax(0,1fr) + 332px` 双栏——左时间线/路由，右诊断轨；960px 以下单栏堆叠。
- 720px 以下：路由表格切换为可展开的 `<details>` 边卡片；应用栏允许换行并收起绝对时间戳。
- 间距节奏：控件内 4–8px，行间 6px，节间 18px。标题上方空间大于下方。

## Elevation & Depth

**无阴影系统。** 深度由三层表达：地面（#f4f4f1）→ 面板（#ffffff + 1px 边框）→ 内嵌（#eceded）。焦点是唯一的"浮起"：双层焦点环（2px 地面色 + 4px 深青）。

## Shapes

半径上限 8px，实际只用了 3px（芯片内圆角）与 5px（面板、按钮）。一切形语言来自 1px 发丝线（#d9dad4）：分隔、包裹、表格线。

## Components

### Buttons
- **Shape:** 5px 圆角，32px 固定高
- **Primary（唯一按钮=手动刷新）:** 导航线底 #2c2f36 + 浅文字，内联 SVG 图标 + "刷新"
- **Hover / Focus:** 底色加深至 #383c44；键盘焦点为深青双层环；请求进行中 disabled + 55% 不透明度
- **无其他按钮**：控制台只读，没有 apply/delete/rollback

### Chips
- **裁决芯片:** 14px/650 字重 + 图标，四态各配色（绿/红/蓝/琥珀）+ 无数据灰
- **阶段状态芯片:** 11px，通过绿底/失败红底加粗/进行蓝底加粗/待执行内嵌灰/未知琥珀底
- **VERIFIED / FAIL 单元格:** 等宽字体 + 颜色，表格与移动卡片同构

### Cards / Containers
- **Corner Style:** 5px
- **Background:** #ffffff + 1px #d9dad4 边框
- **Shadow Strategy:** 无（见 Elevation）
- **Internal Padding:** 12px 14px；时间线用分隔线代替卡片堆叠，杜绝卡片套卡片

### Inputs / Fields
- 无输入控件。只读控制台没有任何字段。

### Navigation
- 单层深色应用栏：品牌 / 面名 / run 芯片（等宽、26vw 截断）/ 更新时间 / 刷新按钮 / 自动刷新状态。移动端换行两行、隐藏绝对时间戳只留相对年龄。

### Stage Timeline（签名组件）
17 行三列网格（34px 序号列带内嵌灰底 + 1fr 名称与 evidence + auto 状态芯片）。失败行下挂整行错误框：等宽错误码 + 中文解释，`overflow-wrap: anywhere`。序号携带真实信息（阶段 N），不是装饰性编号。

## Do's and Don'ts

### Do:
- **Do** 缺失字段如实写"未提供"，未验证边界如实写 NOT_VERIFIED——诚实是这个系统唯一的修辞。
- **Do** 状态表达保持"图标 + 文字 + 颜色"三元组。
- **Do** 时间相关 UI 用投影年龄（updated_utc 距今），冻结的数据必须显示为逐渐变旧。
- **Do** 保持 150–200ms 过渡并在 `prefers-reduced-motion` 下禁用。
- **Do** 新增数据视图时先问投影里有没有这个字段；没有就等单写者加，不要在客户端合成。

### Don't:
- **Don't** 使用渐变、玻璃效果、装饰光球、hero 区或营销文案。
- **Don't** 把页面做成深蓝单色主题；蓝只是 running 状态色。
- **Don't** 添加任何写操作按钮（apply/delete/rewire/rollback）。
- **Don't** 用颜色作为唯一状态载体，或用状态色做装饰。
- **Don't** 入场动画、逐项 stagger、滚动视差。
