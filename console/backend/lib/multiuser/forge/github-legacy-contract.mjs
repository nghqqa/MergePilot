// console/backend/lib/multiuser/forge/github-legacy-contract.mjs — GitHub legacy
// webhook payload 入队前契约预检（PR-B）。
//
// 职责：
//  * legacy 7 字段 payload（{event, delivery_id, installation_id, github_repo_id,
//    pr_number, head_sha, action}；无 schema_version——forge/index.mjs 头注明确该链
//    不经过 validateForgeEventV1，故本模块为并列新模块而非其扩展）的入队前校验：
//    坏 payload 在 webhook 入口处 rejected 留痕并 200 明确响应，不再把必败 job
//    挂进队列等消费侧 event_payload_invalid 兜底（#389 教训同款：缺字段可定位，
//    不挂 queued）。
//
// 纪律（与 forge/index.mjs 同款）：零网络、零 env 读取、零秘密——纯函数。
//
// 契约唯一来源（导出映射，禁止在本文件新造规则）：
//  * 消费侧闸=api.mjs executeEventSync（302-306 行，逐字）：
//      const prNumber = Number(job.payload?.pr_number ?? 0);
//      const headSha  = String(job.payload?.head_sha ?? '');
//      const githubRepoId = Number(job.payload?.github_repo_id ?? 0);
//      if (!prNumber || !/^[0-9a-f]{6,40}$/i.test(headSha)) → event_payload_invalid
//  * 构造处=api.mjs /api/mu/github/webhook pull_request 分支（与 enqueueJob 同点，
//    PR-B 后构造对象被上提为局部 payload 并在入队前先过本校验器）。
//  * 强度=逐字对齐消费侧：不加强（不新增 action 白名单、github_repo_id 闸、
//    head_sha 长度收紧至 {6,64} 等消费侧没有的判定——注意 {6,40} 是消费侧事实，
//    不得「对齐 forge v1」改成 {6,64}）；不减弱（消费侧拒绝的必被预检拒绝）。
//    双向等价由 github-legacy-contract.test.mjs 属性测试锁定。
//  * 消费侧读取但【不】据以拒绝的字段（github_repo_id/event/delivery_id/
//    installation_id/action）本校验器一律不闸——它们由 binding/installation 双闸
//    与 dead-letter（event_kind_unsupported）在各自层面负责，不归 payload 契约。

/** 预检闸字段清单（=消费侧 :302-306 实际据以拒绝的字段，配对证明用）。 */
export const GITHUB_LEGACY_PRECHECK_FIELDS = ['pr_number', 'head_sha'];

/**
 * GitHub legacy pull_request payload 契约预检（纯函数）。
 * 输入=api.mjs GitHub webhook 分支构造的 7 字段 payload 对象（字段变换原样：
 * pr_number=Number(body.pull_request.number ?? 0)、
 * head_sha=String(body.pull_request.head?.sha ?? '')，见 api.mjs 构造处）。
 * 返回 { ok:true } | { ok:false, reason:'event_payload_invalid', field }。
 * reason 词表与 forge/index.mjs / 消费侧一致（event_payload_invalid 族）；
 * field 仅供响应与 delivery/审计留痕（Gitee 链同款：rejected:<field>）。
 *
 * 字段判定顺序=消费侧 :305 的 `||` 求值顺序（pr_number 先于 head_sha）：
 * 两字段同坏时 field 报 pr_number。消费侧对字段不可区分（笼统
 * event_payload_invalid），该顺序只决定留痕字段，不改变放行集合。
 */
export function validateGitHubLegacyPullRequestPayload(payload) {
  // 来源：api.mjs:302（消费侧）——Number(job.payload?.pr_number ?? 0)
  const prNumber = Number(payload?.pr_number ?? 0);
  // 来源：api.mjs:305（消费侧）——!prNumber（falsy 即拒：0/NaN/负零/''等；
  //   注意消费侧【不】要求正整数——0.5/-5/'12'→12 均放行，本校验器同样放行）
  if (!prNumber) return { ok: false, reason: 'event_payload_invalid', field: 'pr_number' };
  // 来源：api.mjs:303（消费侧）——String(job.payload?.head_sha ?? '')
  const headSha = String(payload?.head_sha ?? '');
  // 来源：api.mjs:305（消费侧）——!/^[0-9a-f]{6,40}$/i.test(headSha)（逐字含
  //   /i 与 {6,40}：大小写不敏感；≥6 ≤40 位 hex，64 位完整 sha256 反而超界被拒——
  //   GitHub 实发为 40 位 sha1，与消费侧行为完全一致）
  if (!/^[0-9a-f]{6,40}$/i.test(headSha)) {
    return { ok: false, reason: 'event_payload_invalid', field: 'head_sha' };
  }
  return { ok: true };
}
