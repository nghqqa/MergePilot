---
name: MergePilot Admin Console
description: 运行取证台——现代管理系统语法的只读管理控制台：冷灰阶工作区、墨色侧边栏、深青唯一强调，三态分离永不合并。
colors:
  workspace-ground: "#f5f6f8"
  panel-white: "#ffffff"
  inset-gray: "#f9fafb"
  border-hairline: "#e5e8ee"
  border-strong: "#cfd5df"
  text-primary: "#131a26"
  text-secondary: "#4a5468"
  text-tertiary: "#69748c"
  nav-ink: "#0b0f19"
  nav-hover: "#141a29"
  nav-active: "#1a2236"
  nav-text: "#d4dae6"
  nav-dim: "#7d879e"
  accent-teal: "#0e6b62"
  accent-teal-strong: "#0a524b"
  accent-teal-soft: "#e4f1ef"
  accent-teal-ring: "rgba(14, 107, 98, 0.25)"
  status-green: "#067647"
  status-green-bg: "#ebfdf3"
  status-green-dot: "#17b26a"
  status-amber: "#b54708"
  status-amber-bg: "#fff9eb"
  status-amber-dot: "#f79009"
  status-red: "#b42318"
  status-red-bg: "#fef3f2"
  status-red-dot: "#f04438"
  status-blue: "#175cd3"
  status-blue-bg: "#eff8ff"
  status-blue-dot: "#2e90fa"
  neutral-chip: "#4a5468"
  neutral-chip-bg: "#f2f4f7"
  neutral-dot: "#98a2b3"
typography:
  body:
    fontFamily: "system-ui, 'Segoe UI', 'Microsoft YaHei', sans-serif"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.5
  page-title:
    fontFamily: "system-ui, 'Segoe UI', 'Microsoft YaHei', sans-serif"
    fontSize: "16px"
    fontWeight: 650
    letterSpacing: "-0.01em"
  section-title:
    fontSize: "13px"
    fontWeight: 650
  label:
    fontSize: "11px"
    fontWeight: 600
    color: "{colors.text-tertiary}"
  table-header:
    fontSize: "11px"
    fontWeight: 600
    letterSpacing: "0.05em"
    textTransform: "uppercase"
  data-mono:
    fontFamily: "ui-monospace, 'Cascadia Code', 'JetBrains Mono', Consolas, Menlo, monospace"
    fontSize: "11-12px"
    fontVariantNumeric: "tabular-nums"
rounded:
  sm: "6px"
  md: "8px"
  lg: "10px"
  pill: "999px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "14px"
  lg: "22px"
elevation:
  xs: "0 1px 2px rgba(16,24,40,0.05)"
  sm: "0 1px 3px rgba(16,24,40,0.07), 0 1px 2px rgba(16,24,40,0.04)"
  drawer: "0 24px 56px rgba(11,15,25,0.22), 0 8px 16px rgba(11,15,25,0.08)"
components:
  sidebar:
    width: "236px"
    background: "{colors.nav-ink}"
    activeItem: "{colors.nav-active} 背景 + 600 字重"
    sectionLabel: "10.5px 600 大写 +0.08em nav-dim"
  mode-chip:
    shape: "pill"
    background: "{colors.accent-teal-soft}"
    content: "脉冲点 + SNAPSHOT + 口径短语"
  status-badge:
    shape: "pill"
    anatomy: "色点(6px) + 文字；颜色永不单独承载状态"
    tones: "ok/info/warn/bad/neutral 五档，title 写明语义与来源"
  qf-chip:
    shape: "pill + 内嵌计数徽标"
    active: "{colors.accent-teal} 实底白字"
  data-table:
    header: "sticky、inset 灰、11px 大写"
    row: "44px、悬停 #f7f9fb、全列 nowrap（列表）；长文本表格按 anywhere"
  evidence-drawer:
    width: "min(780px, 92vw)"
    entrance: "220ms cubic-bezier(0.22,1,0.36,1) 滑入 + 180ms 背板渐显"
    header: "inset 灰、路径 mono + 元信息 chips + 主下载钮"
---

# Design System: MergePilot Admin Console（运行取证台）

> 表面范围：`console/frontend`（管理控制台）。仓库根 DESIGN.md 记录的是 E2E 演示控制台（tools/demo_console），
> 不约束本表面。本文档从已建成代码提取（2026-09-22 重构轮，方向契约见 console/frontend/index.html）。

## Overview

**Creative North Star: "运行取证台"**。管理系统的秩序感服务于取证的可信度：执行状态／审查结论／发布状态
是三个独立事实，界面永远分开呈现、各自标注来源；缺失显示"未记录"，全站只读。界面越安静，数据越可信。

**Key Characteristics:**
- 冷灰阶工作区 + 墨色侧边栏（236px，分区大写小标签 + lucide 图标）的双层结构
- 深青 #0e6b62 是唯一强调色：只出现在身份（SNAPSHOT 徽章、品牌标）与交互（active 态、主按钮、焦点环、caret）
- 绿/琥珀/红/蓝四状态色只表达生命状态，一律"色点 + 文字"，绝不装饰（文件类型图标用中性灰，语义靠形状）
- 一切数据（SHA/run_id/时间戳/token 数）等宽字体 + tabular-nums
- 浏览器面全部接管：::selection 深青 18%、2px 焦点环、thin 滚动条、caret 深青
- 140–220ms cubic-bezier(0.22,1,0.36,1)；prefers-reduced-motion 全量降级

## Named Rules

**The Three-Facts Rule.** 执行状态（投递台账/项目 meta）、审查结论（reviewer 结果）、发布状态（check-run）
永不合并且永不共用一枚徽章；每个状态对象携带 `source` 字段并在 UI 标注数据来自哪个文件。
**The Honest-Null Rule.** API 不提供的字段渲染"未记录"（测量值允许"—"仅当语义为不适用），不用演示数据补齐。
**The Read-Only Rule.** 全站无写操作按钮；证据只可查看（转义纯文本）与下载（附件流）。

## Surfaces

- 运行列表：快筛选计数 chips（全部/已回写/未回写）→ 工具栏 → 8 列表（run_id·head SHA 双行标识列）
- 运行详情：面包屑 + mono 标题 + chips 行 + 四态卡（图标瓦片 + 徽章 + mono 来源行）+ 8 标签（图标 + 下划线）
- 证据抽屉：签名交互（220ms 滑入）；头部元信息 chips（归属包/大小/编码/SUMS 状态）
- 未接入页：图标瓦片 + "未接入" 徽章 + 依赖说明 + 回运行列表

## Out of Bounds

渐变、玻璃、装饰性发光、hero 大数卡阵列、装饰图表、彩色左边框、零偏移光晕、
状态色用于非状态元素、unicode 字形充当图标系统。
