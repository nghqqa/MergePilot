// patch-test-cmd.mjs — 预批准测试命令解析（实际修复测试第三分的命令来源唯一入口）。
// 纪律：测试命令只信部署级配置（deps.testCmd 测试注入 / env.MU_FIX_TEST_CMDS JSON 映射），
// 绝不信模型输出或 PR 内容——模型无权决定在沙箱里执行什么。

export function resolvePreApprovedTestCmd(env, deps) {
  if (typeof deps?.testCmd === 'string' && deps.testCmd.trim()) return deps.testCmd.trim();
  const raw = env?.MU_FIX_TEST_CMDS;
  if (!raw) return null;
  try {
    const map = JSON.parse(raw);
    const owner = String(deps?.owner ?? '');
    const repo = String(deps?.repoName ?? '');
    if (!owner || !repo) return null;
    const k1 = `${deps?.provider ?? 'gitee'}:${owner}/${repo}`;
    const k2 = `${owner}/${repo}`;
    const v = typeof map[k1] === 'string' ? map[k1] : (typeof map[k2] === 'string' ? map[k2] : null);
    return v && v.trim() ? v.trim() : null;
  } catch { return null; }
}
