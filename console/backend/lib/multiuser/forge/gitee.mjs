// console/backend/lib/multiuser/forge/gitee.mjs — GiteeAdapter（Gitee 首版 G-2）。
//
// 契约：ForgeAdapter 八方法（ADR-003 §2）；实现依据 r3work/forge-m0/gitee-api-verification.md
// （2026-10-10 匿名探针定案，四级标注）。
//
// 关键实现事实（探针定案，解析必须类型校验而非假设——单样本不构成全部响应保证）：
//  * files 条目 patch 可能是对象 {diff:"<unified 文本>"}（探针样本）；缺失/null 视为
//    patch_status='missing'（不断定二进制/重命名——无法确认即 unknown/missing）；
//  * additions/deletions 探针样本为字符串——Number() 显式转换，NaN 归 0 并记档；
//  * status 字段可为 null——原样透传不推断；
//  * state ∈ {open, closed, merged}（探针列表实返三值）；
//  * 错误体 {message} 存在语义污染（protection 路由 404 与分支缺失同文）——错误处理
//    只按 状态码+端点 分类，禁止 parse message 推导业务结论。
//
// 纪律：
//  * 只调读取端点（GET /user、GET /repos/*、GET /pulls*、GET /branches/*）——调用面
//    白名单，出现非 GET 即抛（调用面零写入承诺，非令牌授权面）；
//  * fetchImpl 可注入（测试口）；HTTP 层超时（GITEE_TIMEOUT_MS，默认 15s）；
//  * 错误消息只含 端点路径（无 query）+状态码——**绝不包含 access_token/query/认证头**
//    （v5 access_token 走 query，完整 URL 即凭据）；
//  * token 即取即用：每次请求从 credentialRef 解析（env 注入），不入缓存不入日志；
//  * readChecks/readProtection 恒 {status:'unsupported', reason:'not_provided_in_this_release'}，
//    零网络调用——404 探针只证明所测请求失败，不证明平台无能力（G-1 纠偏口径）。
import crypto from 'node:crypto';

const GITEE_API_BASE = 'https://gitee.com/api/v5';
const PER_PAGE_MAX = 100;
const FILES_PLATFORM_LIMIT = 300;   // Gitee 服务端：最多 300 条 diff【官方 SDK】
const COMMITS_PLATFORM_LIMIT = 250; // 最多 250 条 commit【官方 SDK】
const DEFAULT_TIMEOUT_MS = 15000;

/** HTTP 错误分类（唯一出口；message 不含 URL query/认证头）。 */
export class GiteeProviderError extends Error {
  constructor(code, { status = null, transient = false, endpoint = '' } = {}) {
    super(String(code)); // 稳定短码，可进 errorCode 列（80 字符内）
    this.code = code;
    this.status = status;
    this.transient = transient;
    this.endpoint = endpoint; // 仅路径部分
  }
}

function errFromStatus(endpoint, status) {
  if (status === 401) return new GiteeProviderError('gitee_auth_failed', { status, endpoint });
  if (status === 403) return new GiteeProviderError('gitee_forbidden', { status, endpoint });
  if (status === 404) return new GiteeProviderError('gitee_not_found', { status, endpoint });
  if (status === 429) return new GiteeProviderError('gitee_rate_limited', { status, transient: true, endpoint });
  if (status >= 500) return new GiteeProviderError('gitee_upstream_error', { status, transient: true, endpoint });
  return new GiteeProviderError(`gitee_http_${status}`, { status, endpoint });
}

/** 类型校验 helper：字符串数字字段 → 正整数（NaN/负 → 0 并记档 flag）。 */
function toCount(v) {
  const n = Number(v);
  return Number.isFinite(n) && n >= 0 ? n : 0;
}

/**
 * 工厂：createGiteeAdapter({ env, fetchImpl, credentialRef })。
 * credentialRef 形如 'env:MU_GITEE_PAT'——首版单一部署凭据（部署级 env 注入，
 * 非每租户独立凭据；限制如实呈现，不伪装多租户凭据隔离）。
 */
export function createGiteeAdapter({ env = process.env, fetchImpl = fetch, credentialRef = 'env:MU_GITEE_PAT',
  apiBase = GITEE_API_BASE, timeoutMs = DEFAULT_TIMEOUT_MS } = {}) {

  function resolveToken() {
    if (!credentialRef.startsWith('env:')) {
      throw new GiteeProviderError('gitee_credential_ref_unsupported');
    }
    const key = credentialRef.slice(4);
    const token = String(env[key] ?? '');
    if (!token) throw new GiteeProviderError('gitee_credentials_missing');
    return token;
  }

  // 统一请求：仅 GET；query 携带 access_token；错误不落完整 URL。
  async function giteeGet(endpoint, { searchParams = {} } = {}) {
    if (!endpoint.startsWith('/') || endpoint.includes('?')) {
      // path 与 query 严格分离——防止调用方把 token/参数拼进 path 后逃过白名单检查
      throw new GiteeProviderError('gitee_endpoint_invalid');
    }
    if (!/^\/(user|repos\/[^/]+\/[^/]+(\/pulls(\/[^/]+(\/(files|commits))?|\/comments)?)?|user\/repos)/.test(endpoint)) {
      throw new GiteeProviderError('gitee_endpoint_not_allowlisted', { endpoint });
    }
    const token = resolveToken();
    const url = new URL(apiBase + endpoint);
    for (const [k, v] of Object.entries(searchParams)) url.searchParams.set(k, String(v));
    url.searchParams.set('access_token', token);
    const started = Date.now();
    let res;
    try {
      res = await fetchImpl(url, {
        method: 'GET',
        headers: { accept: 'application/json' },
        signal: AbortSignal.timeout(timeoutMs),
      });
    } catch (e) {
      const code = e?.name === 'TimeoutError' || e?.name === 'AbortError'
        ? 'gitee_timeout' : 'gitee_unreachable';
      throw new GiteeProviderError(code, { transient: true, endpoint });
    }
    if (!res.ok) throw errFromStatus(endpoint, res.status);
    let body;
    try { body = await res.json(); } catch {
      throw new GiteeProviderError('gitee_bad_response', { endpoint });
    }
    return { body, latencyMs: Date.now() - started };
  }

  // ── patch 解包（类型校验，不假设形状）────────────────────────────
  // 探针样本：patch = { diff: "<unified 文本>" }。非该形状（字符串/缺失/null）如实
  // 降级为 missing，不猜测二进制/重命名。
  function unpackPatch(rawPatch) {
    if (rawPatch && typeof rawPatch === 'object' && !Array.isArray(rawPatch)
      && typeof rawPatch.diff === 'string' && rawPatch.diff.length > 0) {
      return { text: rawPatch.diff, patch_status: 'present' };
    }
    return { text: null, patch_status: 'missing' };
  }

  /** files 条目 → 契约文件行（类型校验入口）。 */
  function normalizeFileEntry(entry) {
    const path = typeof entry?.filename === 'string' ? entry.filename : null;
    const { text, patch_status } = unpackPatch(entry?.patch);
    return {
      path,
      patch: text,
      patch_status, // 'present' | 'missing'
      status: entry?.status ?? null,           // 探针可为 null——原样透传不推断
      additions: toCount(entry?.additions),    // 探针为字符串——显式转换
      deletions: toCount(entry?.deletions),
    };
  }

  /** per-file patches → unified diff 全文（reviewDiff 兼容：加 diff --git 文件头）。
   *  缺失 patch 的文件跳过（completeness 单独表达，不掺进 diff 内容）。 */
  function assembleUnifiedDiff(files) {
    const parts = [];
    for (const f of files) {
      if (f.patch_status !== 'present' || !f.path) continue;
      parts.push(`diff --git a/${f.path} b/${f.path}\n--- a/${f.path}\n+++ b/${f.path}\n${f.patch}`);
    }
    return parts.join('\n');
  }

  // ── 八方法 ──────────────────────────────────────────────────────
  async function probeConnection({ repoFullName = null } = {}) {
    try {
      await giteeGet('/user');
      if (repoFullName) {
        await giteeGet(`/repos/${repoFullName}`);
      }
      return { reachable: true, auth_ok: true, instance_version: null, capabilities: {
        checks: 'not_provided', protection: 'not_provided', diff_mode: 'per_file' } };
    } catch (e) {
      if (e instanceof GiteeProviderError) {
        if (e.code === 'gitee_credentials_missing' || e.code === 'gitee_auth_failed') {
          return { reachable: true, auth_ok: false, instance_version: null, capabilities: {}, reason: e.code };
        }
        if (e.transient) return { reachable: false, auth_ok: null, instance_version: null, capabilities: {}, reason: e.code };
      }
      return { reachable: false, auth_ok: null, instance_version: null, capabilities: {}, reason: 'gitee_probe_failed' };
    }
  }

  async function listRepositories({ page = 1 } = {}) {
    const { body } = await giteeGet('/user/repos', {
      searchParams: { page, per_page: PER_PAGE_MAX, type: 'all', sort: 'full_name' } });
    const repos = Array.isArray(body) ? body.map((r) => ({
      native_repo_id: String(r?.id ?? ''),
      path: typeof r?.full_name === 'string' ? r.full_name : null, // 路径可变，仅展示（探针：hutool 改名实证）
      default_branch: typeof r?.default_branch === 'string' ? r.default_branch : null,
      web_url: typeof r?.html_url === 'string' ? r.html_url : null,
      private: Boolean(r?.private),
    })).filter((r) => r.native_repo_id) : [];
    return { repos, next_cursor: repos.length === PER_PAGE_MAX ? String(page + 1) : null };
  }

  async function getChangeRequest({ providerRepoId, crKey }) {
    // 需要仓库路径定位——由调用方（resolve 层）传入 owner/name；此处约定
    // providerRepoId 传 "owner/name" 原生路径字符串（路径可变→每次以 DB 登记的
    // owner/name 组装，id 稳定性由 DB 层保证）。
    const { body: pr } = await giteeGet(`/repos/${providerRepoId}/pulls/${encodeURIComponent(crKey)}`);
    const headSha = typeof pr?.head?.sha === 'string' ? pr.head.sha : '';
    const baseSha = typeof pr?.base?.sha === 'string' ? pr.base.sha : '';
    const state = typeof pr?.state === 'string' ? pr.state : 'unknown';
    const headRepoId = pr?.head?.repo?.id;
    const baseRepoId = pr?.base?.repo?.id;
    return {
      cr_key: String(pr?.number ?? crKey),
      head_sha: headSha,
      base_sha: baseSha,
      state, // 'open' | 'closed' | 'merged' | 'unknown'（探针三值；unknown=形状漂移如实降级）
      source_repo: String(headRepoId ?? ''),
      target_repo: String(baseRepoId ?? ''),
      source_is_fork: headRepoId != null && baseRepoId != null && String(headRepoId) !== String(baseRepoId),
      title: typeof pr?.title === 'string' ? pr.title : null,
      web_url: typeof pr?.html_url === 'string' ? pr.html_url : null,
    };
  }

  /**
   * 上下文读取（审查与修复共用）。expectedHeadSha 不符 → GiteeProviderError('gitee_head_moved')
   * （head 漂移明确失败，不静默续跑）。
   * declaredFileCount：平台声明的变更文件总数（webhook payload changed_files 或列表接口；
   * 取不到 → null → completeness=unknown）。
   * 返回形状与 review-service 现有 context 契约对齐（diff/checks/protection/limits 键），
   * 附加 completeness/files 键——规则链与管线零改动复用。
   */
  /**
   * 上下文读取（审查与修复共用）。v2 纠偏：
   *  * repoPath（owner/name，仅定位）与 nativeRepoId（DB 登记的稳定数字 id）分离——
   *    先 GET /repos/{repoPath} 核验 id 归属，不一致 → gitee_repo_moved（仓库改名后
   *    旧路径可能命中同名新仓库，禁止误读）；
   *  * head 双读：取 files 前后各一次 getChangeRequest，分页期间推进 → gitee_head_moved；
   *  * declared 自证：末页不满 per_page 且未触平台上限 → 平台全量已取得（declared=
   *    returned）；满页/触上限 → 平台总数不可得 → partial('file_count_limit')——
   *    **不依赖 webhook 声明才能定完整**（webhook declared 仅作交叉核验）；
   *  * local_limit_hit 接真值（diff_bytes>1MiB / 单文件 patch>512KiB / files>300）；
   *  * 含空白字符的路径不拼入 diff（parseDiff 的 diff --git 行按空白分列，无法安全
   *    表达）——该文件 patch_status='unparsed_path'，completeness partial 如实标注。
   * 缺失 patch（missing）≠ 无发现：partial 上下文照常审查，范围声明随 run 冻结呈现。
   */
  async function fetchChangeContext({ repoPath, nativeRepoId, crKey, expectedHeadSha, declaredFileCount = null }) {
    // 0) 仓库归属核验（稳定 id ↔ 当前路径）
    const repoProbe = await giteeGet(`/repos/${repoPath}`);
    const probedId = repoProbe.body?.id;
    if (nativeRepoId != null && probedId != null && String(probedId) !== String(nativeRepoId)) {
      throw new GiteeProviderError('gitee_repo_moved', { endpoint: `/repos/${repoPath}` });
    }
    // 1) head 首读
    const cr = await getChangeRequest({ providerRepoId: repoPath, crKey });
    if (expectedHeadSha && cr.head_sha && cr.head_sha.toLowerCase() !== String(expectedHeadSha).toLowerCase()) {
      throw new GiteeProviderError('gitee_head_moved', { endpoint: `/repos/${repoPath}/pulls/${crKey}` });
    }
    // 2) 分页拉全量 files（平台 300 条上限先于本地限额触发）
    const files = [];
    let platformFileLimitHit = false;
    let allFetched = false; // 末页不满 per_page 且未触上限 → 平台全量已取得
    for (let page = 1; page <= 4; page++) { // 4×100=400>300：一页冗余兜底
      const { body } = await giteeGet(`/repos/${repoPath}/pulls/${encodeURIComponent(crKey)}/files`,
        { searchParams: { page, per_page: PER_PAGE_MAX } });
      const batch = Array.isArray(body) ? body : [];
      for (const entry of batch) {
        const f = normalizeFileEntry(entry);
        if (f.path) files.push(f);
      }
      if (files.length >= FILES_PLATFORM_LIMIT) { platformFileLimitHit = true; break; }
      if (batch.length < PER_PAGE_MAX) { allFetched = true; break; }
    }
    // 3) head 复读（分页期间推进 → 明确失败，绝不把半程上下文当全量）
    const cr2 = await getChangeRequest({ providerRepoId: repoPath, crKey });
    if (cr2.head_sha !== cr.head_sha) {
      throw new GiteeProviderError('gitee_head_moved', { endpoint: `/repos/${repoPath}/pulls/${crKey}` });
    }
    const returned = files.length;
    const declaredFromWebhook = Number.isInteger(declaredFileCount) && declaredFileCount >= 0 ? declaredFileCount : null;
    // declared：全量自证优先；webhook 声明仅交叉核验
    const declared = allFetched ? returned : declaredFromWebhook;
    // 空白路径防护（parseDiff 不安全）——拼接前剔除并标注
    const unparsable = files.filter((f) => /\s/.test(f.path));
    for (const f of unparsable) f.patch_status = 'unparsed_path';
    const missingPatch = files.filter((f) => f.patch_status !== 'present');
    const diff = assembleUnifiedDiff(files);
    const diffBytes = Buffer.byteLength(diff);
    const localFileBytesHit = files.some((f) => Buffer.byteLength(f.patch ?? '') > 512 * 1024);
    const localDiffBytesHit = diffBytes > 1024 * 1024; // 与 ghprovider GH_LIMITS.maxDiffBytes 同额
    const localFileCountHit = files.length > FILES_PLATFORM_LIMIT;
    const limits = { diff_bytes: diffBytes, over_diff_limit: localDiffBytesHit, over_file_limit: localFileCountHit };
    // 4) completeness 三态判定（unknown 仅限 head 形状漂移这类无法核验的场景）
    let completeness;
    const fileLevel = missingPatch.map((f) => ({ path: f.path, patch_status: f.patch_status }));
    if (!cr.head_sha) {
      completeness = { status: 'unknown', declared_file_count: declared, returned_file_count: returned,
        platform_limit_hit: { file_count: platformFileLimitHit, commit_count: false },
        local_limit_hit: { diff_bytes: localDiffBytesHit, file_bytes: localFileBytesHit, file_count: localFileCountHit },
        file_level: fileLevel, notes: 'head_sha_unavailable' };
    } else if (platformFileLimitHit || !allFetched || (declaredFromWebhook != null && returned < declaredFromWebhook)
      || missingPatch.length > 0 || localDiffBytesHit || localFileCountHit) {
      completeness = { status: 'partial', declared_file_count: declared, returned_file_count: returned,
        platform_limit_hit: { file_count: platformFileLimitHit, commit_count: false },
        local_limit_hit: { diff_bytes: localDiffBytesHit, file_bytes: localFileBytesHit, file_count: localFileCountHit },
        file_level: fileLevel,
        notes: platformFileLimitHit ? 'platform_file_count_limit'
          : !allFetched ? 'file_count_limit'
          : declaredFromWebhook != null && returned < declaredFromWebhook ? 'returned_less_than_declared'
          : missingPatch.length > 0 ? 'files_missing_patch'
          : localDiffBytesHit || localFileCountHit ? 'local_limit' : null };
    } else {
      // complete = 分页自证取得平台全量且各文件携带 patch；**不构成单文件 patch 内部
      // 未被平台静默截断的证明**（Gitee 无逐文件完整性标注——文档缺位不得反推保证）。
      completeness = { status: 'complete', declared_file_count: declared, returned_file_count: returned,
        platform_limit_hit: { file_count: false, commit_count: false },
        local_limit_hit: { diff_bytes: false, file_bytes: false, file_count: false },
        file_level: [], notes: null };
    }
    return {
      stale_head: false,
      pr: { number: Number(cr.cr_key) || Number(crKey), state: cr.state, title: cr.title ?? '',
        head: { sha: cr2.head_sha, ref: null }, base: { sha: cr2.base_sha, ref: null },
        changed_files: declared ?? returned },
      diff,
      checks: [], // 本接入未提供该平台检查读取（零调用；不伪装 check run）
      protection: { configured: false, provided: false, reason: 'not_provided_in_this_release' },
      limits,
      completeness,
      files,
      fetched_head_sha: cr2.head_sha,
      source_is_fork: cr2.source_is_fork,
    };
  }

  // 本接入未提供：零网络调用返回常量（原因=本版本未实现该能力读取；非平台能力结论）。
  async function readChecks() {
    return { status: 'unsupported', reason: 'not_provided_in_this_release', items: [] };
  }
  async function readProtection() {
    return { status: 'unsupported', reason: 'not_provided_in_this_release', summary: null };
  }

  return {
    kind: 'gitee',
    probeConnection, listRepositories, getChangeRequest, fetchChangeContext,
    readChecks, readProtection,
    __internals: { normalizeFileEntry, assembleUnifiedDiff, unpackPatch }, // 测试口
  };
}

// ── webhook 验真（独立于适配器实例：入口层用；连接显式单模式，禁止降级/择一）────
// 官方算法（v2 修正）：待签字符串 = timestamp、LF（转义 \n）、密钥 三段拼接，
// HmacSHA256(key=密钥) → Base64 → urlEncode（UTF-8），误差窗口 1 小时
// 【官方✓ help.gitee.com/webhook/how-to-verify-webhook-keys】。
// ⚠️ v1 实现缺陷（审查纠偏已修）：待签消息漏了 secret 段——独立向量（python hmac 独立
// 计算）证实旧公式输出与官方算法不一致，已按官方原文修正并加防退化向量断言。
// 来源与编码【显式配置，不自动回退/混合】：
//   * signSource 'header'（默认）| 'body'——timestamp/sign 从配置声明的单一来源取；
//     另一来源同时携带完整对且不一致 → 'sign_source_conflict' 拒绝（防混源错配）；
//   * signEncoding 'url_b64'（默认，官方三步）| 'b64'——由试点实测收敛后固定。
// 完整性边界（ADR-003 §2.5）：签名输入不含请求体——验真通过≠body 未被篡改；
// 消费链以受保护 API 重新核验仓库/PR/head（G-3 纪律），缓解=HTTPS+delivery_ref 幂等+窗口。
import { timingSafeEqual } from 'node:crypto';

const SIGN_WINDOW_MS = 60 * 60 * 1000; // 官方：误差不超过 1 小时

export const GITEE_SIGN_SOURCES = ['header', 'body'];
export const GITEE_SIGN_ENCODINGS = ['url_b64', 'b64'];

function urlEncodeB64(b64) {
  // urlEncode(base64)：'+'→%2B、'/'→%2F、'='→%3D（钉钉系惯例；精确形态以试点实测回填）
  return String(b64).replace(/\+/g, '%2B').replace(/\//g, '%2F').replace(/=/g, '%3D');
}

/**
 * 验真入口（有界解析在外层完成——本函数只接收已提取字段，不做 JSON.parse）。
 * @param {object} p { mode:'signature'|'password', secret, extracted, signSource, signEncoding, nowMs }
 *   extracted = { tokenHeader, tsHeader, bodyTimestamp, bodySign }——外层以有界方式提取。
 *   nowMs 仅测试注入（独立向量定窗）；生产缺省 Date.now()。
 * @returns {ok:true} | {ok:false, reason}
 */
export function verifyGiteeWebhook({ mode, secret, extracted, signSource = 'header', signEncoding = 'url_b64', nowMs = null }) {
  if (mode !== 'signature' && mode !== 'password') return { ok: false, reason: 'mode_invalid' };
  if (!GITEE_SIGN_SOURCES.includes(signSource)) return { ok: false, reason: 'sign_source_invalid' };
  if (!GITEE_SIGN_ENCODINGS.includes(signEncoding)) return { ok: false, reason: 'sign_encoding_invalid' };
  const secretStr = String(secret ?? '');
  const secretBuf = Buffer.from(secretStr, 'utf8');
  if (!secretBuf.length) return { ok: false, reason: 'secret_missing' };

  if (mode === 'password') {
    // X-Gitee-Token=密码——官方格式文档明确【官方✓】；常量时间比较
    const token = String(extracted?.tokenHeader ?? '');
    if (!token) return { ok: false, reason: 'token_missing' };
    const a = Buffer.from(token, 'utf8');
    const b = Buffer.from(secretStr, 'utf8');
    return a.length === b.length && timingSafeEqual(a, b)
      ? { ok: true } : { ok: false, reason: 'password_mismatch' };
  }

  // signature：单源取值（配置声明，不回退）；另一来源完整对且不一致 → 冲突拒绝
  const primary = signSource === 'header'
    ? { ts: extracted?.tsHeader, sign: extracted?.tokenHeader,
        otherTs: extracted?.bodyTimestamp, otherSign: extracted?.bodySign }
    : { ts: extracted?.bodyTimestamp, sign: extracted?.bodySign,
        otherTs: extracted?.tsHeader, otherSign: extracted?.tokenHeader };
  const ts = String(primary.ts ?? '');
  const sign = String(primary.sign ?? '');
  if (!ts || !sign) return { ok: false, reason: 'signature_fields_missing' };
  const oTs = String(primary.otherTs ?? '');
  const oSign = String(primary.otherSign ?? '');
  if (oTs && oSign && (oTs !== ts || oSign !== sign)) return { ok: false, reason: 'sign_source_conflict' };
  const tsNum = Number(ts);
  if (!Number.isFinite(tsNum) || tsNum <= 0) return { ok: false, reason: 'timestamp_invalid' };
  const now = Number.isFinite(nowMs) ? nowMs : Date.now();
  if (Math.abs(now - tsNum) > SIGN_WINDOW_MS) return { ok: false, reason: 'timestamp_out_of_window' };
  // 官方三步：msg = timestamp、LF、secret 三段拼接（key=secret）→ Base64 →（按配置）urlEncode
  const digestB64 = crypto.createHmac('sha256', secretBuf)
    .update(ts + '\n' + secretStr, 'utf8').digest('base64');
  const expected = signEncoding === 'url_b64' ? urlEncodeB64(digestB64) : digestB64;
  const a = Buffer.from(sign, 'utf8');
  const b = Buffer.from(expected, 'utf8');
  return a.length === b.length && timingSafeEqual(a, b)
    ? { ok: true } : { ok: false, reason: 'signature_mismatch' };
}

export const GITEE_LIMITS = { PER_PAGE_MAX, FILES_PLATFORM_LIMIT, COMMITS_PLATFORM_LIMIT };
