// fxv/github-gate.mjs — GitHub 写入双闸门：
//   1) 配置闸：FXV_GITHUB_WRITE 必须显式 'authorized'（默认 disabled）
//   2) 授权闸：fxv.grants 一次性消费（具名操作员+repo 范围+TTL，SKIP LOCKED 防双消费）
// 任何真实写操作（commit/push）必须同时通过两道闸；否则 GITHUB_WRITE_UNAUTHORIZED。
export class GitHubWriteUnauthorized extends Error {
  constructor(reason) { super(`GITHUB_WRITE_UNAUTHORIZED: ${reason}`); this.name = 'GitHubWriteUnauthorized'; this.code = 'GITHUB_WRITE_UNAUTHORIZED'; }
}

// 管线在 AWAITING_GITHUB_GRANT 处调用：有有效授权→原子消费并返回；无→null（继续等待）
export async function consumePipelineGrant(cfg, store, repo) {
  if (cfg.githubWrite !== 'authorized') return null;
  return store.consumeGrant(repo);
}

// 交给 commitPush handler 的运行时闸（不信任调用方自证）
export function makeWriteGate(cfg, store) {
  return {
    async assert(repo) {
      if (cfg.githubWrite !== 'authorized') {
        throw new GitHubWriteUnauthorized('FXV_GITHUB_WRITE != authorized');
      }
      if (!cfg.repoAllowlist.includes(repo)) {
        throw new GitHubWriteUnauthorized(`repo ${repo} not in FXV allowlist`);
      }
      return true;
    },
  };
}
