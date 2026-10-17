// chartTicks.js — 概览图表整数刻度纯函数（无 React/无 IO，可直接被 node 测试导入）。
// 背景（2026-10-16）：离散计数（PR/run 个数）小样本下 G2 v5 默认出现 0.2/0.4
// 小数刻度；tickInterval 不被线性刻度采纳（实测）——用自定义 tickMethod 生成整数刻度。
// 策略：小域步长 1；大域按 count 均分上取整；恒含 0；恒为整数、单调递增。

export function intTickMethod(min, max, count = 4) {
  const span = Math.max(1, Math.ceil(max - min));
  const step = span <= (count || 4) ? 1 : Math.ceil(span / (count || 4));
  const ticks = [];
  for (let v = Math.max(0, Math.floor(min)); v <= max + 1e-6; v += step) ticks.push(v);
  return ticks.length ? ticks : [min, max];
}
