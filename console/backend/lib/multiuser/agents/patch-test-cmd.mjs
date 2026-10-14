// patch-test-cmd.mjs — 预批准测试命令解析（实际修复测试第三分的命令来源唯一入口）。
// 纪律（2026-10-14 生产实证修正）：预批准命令【只】来自部署级 env.MU_FIX_TEST_CMDS
// （JSON 映射，key="provider:owner/repo"，兼容 "owner/repo"）。
//  * deps.testCmd 历史上是 Wave3 E2E 注入后门，生产 deps 装配器会填平凡默认值
//    （'node -e process.exit(0)'）——曾遮蔽部署配置导致无效测试（run b10e3f4f 实证，
//    fail-closed 如实 ok=false）。现默认忽略；仅当显式设置 MU_FIX_TEST_ALLOW_DEPS_CMD=1
//    （隔离 E2E）才采纳。
//  * 未配置=如实"未执行"，不猜命令、不跑 PR 内容/模型输出里的任何指令。

export function resolvePreApprovedTestCmd(env, deps) {
  const raw = env?.MU_FIX_TEST_CMDS;
  if (raw) {
    try {
      const map = JSON.parse(raw);
      const owner = String(deps?.owner ?? '');
      const repo = String(deps?.repoName ?? '');
      if (owner && repo) {
        const k1 = `${deps?.provider ?? 'gitee'}:${owner}/${repo}`;
        const k2 = `${owner}/${repo}`;
        const v = typeof map[k1] === 'string' ? map[k1] : (typeof map[k2] === 'string' ? map[k2] : null);
        if (v && v.trim()) return v.trim();
      }
    } catch { /* 坏 JSON → 落到下方如实未执行 */ }
  }
  if (String(env?.MU_FIX_TEST_ALLOW_DEPS_CMD ?? '') === '1'
    && typeof deps?.testCmd === 'string' && deps.testCmd.trim()) {
    return deps.testCmd.trim();
  }
  return null;
}
