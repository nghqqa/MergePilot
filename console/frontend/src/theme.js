// theme.js — MergePilot 审查工作台 antd 主题 token（唯一主题来源）。
// 映射既有视觉世界：冷灰阶工作区 / 墨色侧栏 / 深青唯一强调 / 四状态色仅表状态。
// 正文 14px、页面标题 24px；技术字段（SHA/run_id/时间）用 --font-mono（由 CSS 变量承载，
// antd token 不覆盖等宽场景，组件内用 .mono/.sha 类）。
import { theme } from 'antd';
import { SEVERITY, TICKET, toneToColor } from './status-map.js';

export const MP = {
  // 中性阶
  bg: '#f5f6f8',
  panel: '#ffffff',
  inset: '#f9fafb',
  border: '#e5e8ee',
  borderStrong: '#cfd5df',
  text: '#131a26',
  text2: '#4a5468',
  text3: '#5e6a84',
  // 墨色侧栏
  nav: '#0b0f19',
  navHover: '#141a29',
  navActive: '#1a2236',
  navText: '#d4dae6',
  navDim: '#7d879e',
  // 深青强调
  accent: '#0e6b62',
  accentStrong: '#0a524b',
  accentSoft: '#e4f1ef',
  // 状态色（仅状态）
  ok: '#067647', warn: '#b54708', bad: '#b42318', info: '#175cd3',
};

export const mpTheme = {
  algorithm: theme.defaultAlgorithm,
  token: {
    colorPrimary: MP.accent,
    colorInfo: MP.info,
    colorSuccess: MP.ok,
    colorWarning: MP.warn,
    colorError: MP.bad,
    colorBgLayout: MP.bg,
    colorBgContainer: MP.panel,
    colorBgElevated: MP.panel,
    colorBorder: MP.border,
    colorBorderSecondary: MP.border,
    colorText: MP.text,
    colorTextSecondary: MP.text2,
    colorTextTertiary: MP.text3,
    borderRadius: 6,
    borderRadiusLG: 10,
    fontSize: 14,
    fontSizeSM: 13,
    fontSizeLG: 16,
    fontSizeHeading1: 24,
    fontSizeHeading2: 20,
    fontSizeHeading3: 16,
    fontFamily: "system-ui, -apple-system, 'Segoe UI', 'PingFang SC', 'HarmonyOS Sans SC', 'Noto Sans CJK SC', 'Source Han Sans SC', 'Microsoft YaHei', sans-serif",
    controlHeight: 32,
    wireframe: false,
  },
  components: {
    Layout: {
      siderBg: MP.nav,
      headerBg: MP.panel,
      headerHeight: 52,
      bodyBg: MP.bg,
    },
    Menu: {
      darkItemBg: MP.nav,
      darkItemSelectedBg: MP.navActive,
      darkItemHoverBg: MP.navHover,
      darkItemColor: MP.navText,
      darkGroupColor: MP.navDim,
      itemHeight: 38,
      itemMarginInline: 8,
      itemBorderRadius: 6,
    },
    Table: {
      headerBg: MP.inset,
      headerColor: MP.text2,
      rowHoverBg: '#f3f6fa',
      cellPaddingBlock: 9,
      cellPaddingInline: 12,
      fontSize: 13,
    },
    Tag: { borderRadiusSM: 4 },
    Button: { controlHeight: 32, fontWeight: 500 },
  },
};

// 状态语义映射：人话标签为主显示，机器值入详情（视觉目标 5）。
// PR-2 单源收敛：票据域与严重度域的键不再自持文案——直接从 status-map.js 词表
// 派生（label/tone 同源同口径），消除双源分歧（HIGH:'高风险' vs '高'、
// PENDING:'等待处理' vs '待审批'）。status-map 是唯一权威词表。
const fromVocab = (e) => ({ label: e.label, tone: toneToColor(e.tone) });
export const STATUS_META = {
  POSTGRESQL_LIVE: { label: '实时数据', tone: 'success' },
  // Wave 3.7 起 MU 部署五个 Core API 的数据源契约值（server.mjs muMeta）——同样代表
  // 健康 PG 接线；缺这条映射时 CorePage 会把健康态渲染成红色"请检查 PG 连接"。
  MU_CANONICAL_LIVE: { label: 'MU 实时数据', tone: 'success' },
  BACKEND_NOT_WIRED: { label: '未接线', tone: 'warning' },
  BACKEND_ERROR: { label: '后端错误', tone: 'error' },
  AUTH_REQUIRED: { label: '需要登录', tone: 'warning' },
  OK: { label: '正常', tone: 'success' },
  PRODUCE: { label: '通过（技能层）', tone: 'success' },
  REFUSE: { label: '拒绝', tone: 'error' },
  // 票据域（PENDING/APPROVED/REJECTED/USED/EXPIRED/STALE/CONSUMED）与严重度域
  // （CRITICAL-LOW、P0-P3）全部同源派生自 status-map.js——新增枚举自动跟随。
  ...Object.fromEntries(Object.entries(TICKET).map(([k, v]) => [k, fromVocab(v)])),
  ...Object.fromEntries(Object.entries(SEVERITY).map(([k, v]) => [k, fromVocab(v)])),
};
