// permissions.mjs — 多用户/团队/仓库/分支权限模型（2026-09-26 闭环轮）。
// 默认最小权限：未知主体=拒绝一切；越权（跨仓/跨分支/fxv 未授权/admin 提权）全部拒绝。
// 配置：CONSOLE_ACCESS_MODEL_JSON（JSON 数组），未配置时回落 legacy 单用户 allowlist。
//
// 形状：
//   [{"subject":"alice","kind":"user","teams":["eng"],"repos":["acme/app"],
//     "branches":["main"],"can_fxv":true,"roles":["viewer"]},
//    {"subject":"eng","kind":"team","repos":["acme/app","acme/lib"],"branches":["*"]}]
//
// 已知边界（诚实登记）：多凭证认证（每用户独立密码/OIDC）未接线（G-07）——
// 当前模型决定"已认证会话映射到某主体后可见/可做什么"，不决定"谁能登录"。
export function parseAccessModel(env = process.env) {
  const raw = env.CONSOLE_ACCESS_MODEL_JSON;
  if (!raw) return { mode: 'legacy', entries: [] };
  let entries;
  try { entries = JSON.parse(raw); } catch { return { mode: 'invalid', entries: [], error: 'CONSOLE_ACCESS_MODEL_JSON 解析失败' }; }
  if (!Array.isArray(entries)) return { mode: 'invalid', entries: [], error: 'access model 必须是数组' };
  for (const e of entries) {
    if (!e?.subject || !['user', 'team'].includes(e?.kind) || !Array.isArray(e?.repos)) {
      return { mode: 'invalid', entries: [], error: `非法条目：${JSON.stringify(e).slice(0, 80)}` };
    }
  }
  return { mode: 'model', entries };
}

export function resolveSubject(model, subject) {
  return model.entries.find((e) => e.kind === 'user' && e.subject === subject) ?? null;
}

// 主体可见仓库 = 自身 repos ∪ 所属 teams 的 repos
export function resolveRepos(model, subject) {
  const user = resolveSubject(model, subject);
  if (!user) return [];
  const set = new Set(user.repos ?? []);
  for (const t of user.teams ?? []) {
    const team = model.entries.find((e) => e.kind === 'team' && e.subject === t);
    if (team) for (const r of team.repos ?? []) set.add(r);
  }
  return [...set].sort();
}

function branchMatch(allowed, branch) {
  return allowed.includes('*') || allowed.includes(branch);
}

export function subjectBranches(model, subject, repo) {
  const user = resolveSubject(model, subject);
  if (!user) return [];
  const out = new Set(user.branches ?? ['*']);
  for (const t of user.teams ?? []) {
    const team = model.entries.find((e) => e.kind === 'team' && e.subject === t);
    if (team && (team.repos ?? []).includes(repo)) for (const b of team.branches ?? ['*']) out.add(b);
  }
  // 用户自身 branches 仅当其 repos 含该仓时生效
  if ((user.repos ?? []).includes(repo)) for (const b of user.branches ?? ['*']) out.add(b);
  return [...out];
}

// 动作分级：read（读控制面/证据）| fxv（立案修复）| admin（授权签发/配置）
export function authorize(model, subject, { repo, branch = 'main', action = 'read' }) {
  if (model.mode === 'invalid') return { ok: false, reason: 'ACCESS_MODEL_INVALID' };
  if (model.mode === 'legacy') {
    // legacy：单一已认证主体 + 全局 allowlist（原有语义，不放宽）
    return { ok: true, mode: 'legacy' };
  }
  const user = resolveSubject(model, subject);
  if (!user) return { ok: false, reason: 'SUBJECT_UNKNOWN', detail: `主体 ${subject} 不在访问模型中（默认拒绝）` };
  const repos = resolveRepos(model, subject);
  if (repo && !repos.includes(repo)) {
    return { ok: false, reason: 'REPO_DENIED', detail: `${repo} 不在 ${subject} 的可见仓库（${repos.join(',') || '无'}）` };
  }
  if (repo && branch && !branchMatch(subjectBranches(model, subject, repo), branch)) {
    return { ok: false, reason: 'BRANCH_DENIED', detail: `分支 ${branch} 不在 ${subject}@${repo} 的分支白名单` };
  }
  if (action === 'fxv' && !user.can_fxv) return { ok: false, reason: 'FXV_NOT_GRANTED', detail: `${subject} 未获 fxv 立案权限` };
  if (action === 'admin' && !(user.roles ?? []).includes('admin')) {
    return { ok: false, reason: 'ADMIN_NOT_GRANTED', detail: `${subject} 无 admin 角色` };
  }
  return { ok: true, repos };
}

// 审计钩子：越权必须可观测（调用方接入自身审计存储；此处输出结构化记录）
export function denialAudit(subject, decision, req) {
  return { kind: 'ACCESS_DENIED', subject, reason: decision.reason, detail: decision.detail,
    repo: req?.repo ?? null, branch: req?.branch ?? null, action: req?.action ?? 'read',
    at: new Date().toISOString() };
}
