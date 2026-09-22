// console/backend/lib/runs.mjs — normalize locked evidence packs into run records.
//
// Every field is extracted from a concrete file in the pack; when the file or
// the pattern is absent the field is null and the UI must render it as
// 未记录/未接入 — never guessed. Extraction precedence per field is documented
// in docs/productization/console/API-CONTRACT.md.

import fs from 'node:fs';
import path from 'node:path';
import {
  readJsonSafe,
  readTextSafe,
  existsFile,
  parseTs,
  isoOrNull,
  fmtDurationMs,
  SHA40_RE,
} from './util.mjs';

const VERDICT_RE = /\*\*(FINDING_CONFIRMED|NOT_CONFIRMED)[^*]*\*\*/;
const SEVERITY_RE = /\/\s*(CRITICAL|HIGH|MEDIUM|LOW)\b/;
const CWE_RE = /(CWE-\d+)/;

function parseResultMd(packDir) {
  const text = readTextSafe(path.join(packDir, 'project/result.md'));
  if (!text) return null;
  const out = { raw: 'project/result.md' };
  const runId = text.match(/-\s*\*\*Run ID\*\*:\s*(\S+)/);
  if (runId) out.run_id = runId[1];
  const repo = text.match(/-\s*\*\*Repo\*\*:\s*https:\/\/github\.com\/([\w.-]+\/[\w.-]+)/);
  if (repo) out.repo = repo[1];
  const pr = text.match(/-\s*\*\*PR\*\*:\s*#(\d+)/);
  if (pr) out.pr_number = Number(pr[1]);
  const head = text.match(/head SHA `?([0-9a-f]{40})/);
  if (head) out.head_sha = head[1];
  const base = text.match(/base `?([0-9a-f]{40})/);
  if (base) out.base_sha = base[1];
  const status = text.match(/-\s*\*\*Status\*\*:\s*(.+)$/m);
  if (status) out.status_line = status[1].trim();
  const gate = text.match(/Human security gate\*\*:\s*(\w+)/);
  if (gate) out.gate = gate[1]; // APPROVED | REJECTED
  if (/no human gate/i.test(text)) out.gate = out.gate ?? 'NOT_REQUIRED';
  // Review node line: "- [x] `...-review-1` — title (role) — **FINDING_CONFIRMED / HIGH / ...**"
  const dag = [];
  const dagRe = /^[-*] \[([ x!\-])\] `([\w.-]+)` — (.+)$/gm;
  let m;
  while ((m = dagRe.exec(text)) !== null) {
    dag.push({ mark: m[1], task_id: m[2], rest: m[3] });
  }
  out.dag = dag;
  const reviewNode = dag.find((n) => /-review-\d+$/.test(n.task_id));
  if (reviewNode) {
    const v = reviewNode.rest.match(VERDICT_RE);
    if (v) {
      out.verdict = v[1]; // FINDING_CONFIRMED | NOT_CONFIRMED
      const sev = v[0].match(SEVERITY_RE);
      if (sev) out.severity = sev[1];
      const cwe = v[0].match(CWE_RE);
      if (cwe) out.cwe = cwe[1];
    }
    out.review_task_id = reviewNode.task_id;
  }
  return out;
}

function parsePrMetadataMd(packDir) {
  const text = readTextSafe(path.join(packDir, 'PR-METADATA.md'));
  if (!text) return null;
  const out = { raw: 'PR-METADATA.md' };
  const runId = text.match(/（(run-[A-Za-z0-9-]+)）|run_id[:：]\s*(run-[A-Za-z0-9-]+)/);
  if (runId) out.run_id = runId[1] || runId[2];
  const repo = text.match(/仓库：https:\/\/github\.com\/([\w.-]+\/[\w.-]+)/);
  if (repo) out.repo = repo[1];
  const pr = text.match(/PR：#(\d+)/);
  if (pr) out.pr_number = Number(pr[1]);
  const head = text.match(/head SHA：([0-9a-f]{40})/);
  if (head) out.head_sha = head[1];
  const base = text.match(/base SHA：([0-9a-f]{40})/);
  if (base) out.base_sha = base[1];
  const title = text.match(/^-\s*标题：(.+)$/m);
  if (title) out.title = title[1].trim();
  return out;
}

function parseCheckRun(packDir) {
  const cr = readJsonSafe(path.join(packDir, 'check-run.json'));
  if (!cr) return null;
  const out = {
    check_run_id: cr.id ?? null,
    conclusion: cr.conclusion ?? null,
    url: cr.html_url ?? null,
    started_at: cr.started_at ?? null,
    completed_at: cr.completed_at ?? null,
    app: cr.app ?? null,
    raw: 'check-run.json',
  };
  const summary = cr.output?.summary ?? '';
  const runId = summary.match(/run_id: (\S+)/);
  if (runId) out.run_id = runId[1];
  const verdict = summary.match(/verdict: (\S+)/);
  if (verdict) out.verdict = verdict[1];
  return out;
}

function parseKickoffAsSent(packDir) {
  const text = readTextSafe(path.join(packDir, 'kickoff-as-sent.txt'));
  if (!text) return null;
  const out = {};
  const head = text.match(/checkout head SHA ([0-9a-f]{40})/);
  if (head) out.head_sha = head[1];
  const base = text.match(/git diff ([0-9a-f]{7,40})\.\./);
  if (base) out.base_sha_short = base[1];
  return Object.keys(out).length ? out : null;
}

// 拒绝案例包（如 PR3-WH）没有 project/result.md — 审查结论在 reviewer 任务的
// result.md（"SUMMARY: STATUS: FINDING_CONFIRMED; SEVERITY: HIGH; ..."）。
// LIVE 轮的等价文件在包根 reviewer-result.md。
function parseReviewTaskResult(packDir) {
  const candidates = [];
  const dir = path.join(packDir, 'tasks');
  try {
    const entries = fs.readdirSync(dir, { withFileTypes: true });
    const reviewDir = entries.find((e) => e.isDirectory() && /-review-\d+$/.test(e.name));
    if (reviewDir) candidates.push({ rel: `tasks/${reviewDir.name}/result.md`, abs: path.join(dir, reviewDir.name, 'result.md') });
  } catch { /* no tasks dir */ }
  candidates.push({ rel: 'reviewer-result.md', abs: path.join(packDir, 'reviewer-result.md') });
  for (const c of candidates) {
    const text = readTextSafe(c.abs);
    if (!text) continue;
    const out = { raw: c.rel };
    const st = text.match(/STATUS:\s*(FINDING_CONFIRMED|NOT_CONFIRMED)/);
    if (st) out.verdict = st[1];
    const sev = text.match(/SEVERITY:\s*(CRITICAL|HIGH|MEDIUM|LOW)/);
    if (sev) out.severity = sev[1];
    const cwe = text.match(/(CWE-\d+)/);
    if (cwe) out.cwe = cwe[1];
    if (out.verdict) return out;
  }
  return null;
}

// 人工门决策文件（project/human-gate-approval.md / human-gate-rejection.md，
// 兼容包根位置的旧布局）。部分包（如 SK5 拒绝轮）的门文件是唯一带
// repo/PR 身份的记录，一并提取。
function parseGateMd(packDir) {
  const tryFiles = [
    ['project/human-gate-approval.md', 'APPROVE'],
    ['human-gate-approval.md', 'APPROVE'],
    ['project/human-gate-rejection.md', 'REJECT'],
    ['human-gate-rejection.md', 'REJECT'],
  ];
  for (const [rel, kind] of tryFiles) {
    const text = readTextSafe(path.join(packDir, rel));
    if (!text) continue;
    const out = { raw: rel };
    if (kind === 'REJECT' || /HUMAN_SECURITY_REJECTED|Gate decision: \*\*REJECTED/i.test(text)) {
      out.decision = 'REJECTED';
    } else if (/Gate decision: \*\*APPROVED|APPROVED — remediation authorized/i.test(text)) {
      out.decision = 'APPROVED';
    } else {
      out.decision = 'RECORDED';
    }
    const head = text.match(/Head SHA under review: ([0-9a-f]{40})/);
    if (head) out.head_sha = head[1];
    const pr = text.match(/Head SHA under review:[^\n]*?PR #(\d+)/);
    if (pr) out.pr_number = Number(pr[1]);
    const repo = text.match(/Head SHA under review:[^\n]*?PR #\d+ ([A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+)/);
    if (repo) out.repo = repo[1];
    return out;
  }
  return null;
}

function parseTasks(packDir) {
  const dir = path.join(packDir, 'tasks');
  let entries;
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch {
    return [];
  }
  const tasks = [];
  for (const e of entries) {
    if (!e.isDirectory()) continue;
    const meta = readJsonSafe(path.join(dir, e.name, 'meta.json'));
    if (!meta) continue;
    tasks.push({
      task_id: meta.task_id ?? e.name,
      project_id: meta.project_id ?? null,
      title: meta.task_title ?? null,
      role: meta.assigned_to ?? null,
      status: meta.status ?? null,
      depends_on: meta.depends_on ?? [],
      assigned_at: meta.assigned_at ?? null,
      acknowledged_at: meta.acknowledged_at ?? null,
      submitted_at: meta.submitted_at ?? null,
      result_path: existsFile(path.join(dir, e.name, 'result.md')) ? `tasks/${e.name}/result.md` : null,
      spec_path: existsFile(path.join(dir, e.name, 'spec.md')) ? `tasks/${e.name}/spec.md` : null,
      findings_path: existsFile(path.join(dir, e.name, 'findings.md'))
        ? `tasks/${e.name}/findings.md`
        : existsFile(path.join(dir, e.name, 'workspace/findings.md'))
          ? `tasks/${e.name}/workspace/findings.md`
          : null,
    });
  }
  tasks.sort((a, b) => (a.assigned_at ?? '').localeCompare(b.assigned_at ?? ''));
  return tasks;
}

function parseSpanSummary(packDir) {
  return (
    readJsonSafe(path.join(packDir, 'span-summary.json')) ??
    readJsonSafe(path.join(packDir, 'agentloop/span-summary.json'))
  );
}

function parseSkillAudit(packDir) {
  return readJsonSafe(path.join(packDir, 'skill-audit.json'));
}

function parseRagSpans(packDir) {
  const file = path.join(packDir, 'rag/rag-tool-spans.jsonl');
  if (!existsFile(file)) return null;
  const lines = readTextSafe(file)?.split(/\r?\n/).filter(Boolean) ?? [];
  const calls = [];
  for (const line of lines) {
    try {
      calls.push(JSON.parse(line));
    } catch { /* malformed line → skip, counted below as unparsed */ }
  }
  return { raw: 'rag/rag-tool-spans.jsonl', total_lines: lines.length, calls };
}

function parseUsage(packDir) {
  const u = readJsonSafe(path.join(packDir, 'usage-summary.json'));
  if (!u) return null;
  return { raw: 'usage-summary.json', windows: u.windows ?? null, note: u.note ?? null };
}

function usageWindowForPack(packId, usage) {
  if (!usage?.windows) return null;
  const norm = (s) => s.toLowerCase().replace(/[^a-z0-9]/g, '');
  const pack = norm(packId);
  const hit = Object.keys(usage.windows).find((k) => pack.includes(norm(k)));
  return hit ? { key: hit, ...usage.windows[hit] } : null;
}

// ---- run record assembly ----------------------------------------------------

export function buildRunRecord(packId, packDir) {
  const ledgerArr = readJsonSafe(path.join(packDir, 'delivery-ledger.json'));
  const ledger = Array.isArray(ledgerArr) ? ledgerArr[0] ?? null : ledgerArr;
  const kickoff = readJsonSafe(path.join(packDir, 'kickoff.json'));
  const projectMeta = readJsonSafe(path.join(packDir, 'project/meta.json'));
  const resultMd = parseResultMd(packDir);
  const prMeta = parsePrMetadataMd(packDir);
  const checkRun = parseCheckRun(packDir);
  const kickoffSent = parseKickoffAsSent(packDir);
  const gateSent = readJsonSafe(path.join(packDir, 'gate-approval-sent.json'));
  const reviewTask = parseReviewTaskResult(packDir);
  const gateMd = parseGateMd(packDir);

  const runId =
    kickoff?.run ?? checkRun?.run_id ?? resultMd?.run_id ?? prMeta?.run_id ?? null;
  const repoFromCheckUrl = checkRun?.url?.match(/github\.com\/([\w.-]+\/[\w.-]+)\/runs\//)?.[1] ?? null;
  const repo =
    ledger?.repo ?? resultMd?.repo ?? prMeta?.repo ?? gateMd?.repo ?? repoFromCheckUrl ?? null;
  const prNumber =
    ledger?.pr_number ?? resultMd?.pr_number ?? prMeta?.pr_number ?? gateMd?.pr_number ?? null;
  const headSha =
    ledger?.observed_head_sha ?? resultMd?.head_sha ?? prMeta?.head_sha ?? kickoffSent?.head_sha ?? gateMd?.head_sha ?? null;
  if (headSha && !SHA40_RE.test(headSha)) {
    // full 40-hex only; anything shorter is displayed raw but flagged
    console.warn(`[runs] ${packId}: head sha not 40-hex: ${headSha}`);
  }
  const baseSha =
    ledger?.observed_base_sha ?? resultMd?.base_sha ?? prMeta?.base_sha ?? null;

  const trigger = ledger ? 'webhook' : kickoff ? 'matrix' : 'unknown';

  // 执行状态 — 来源显式标注，webhook 轮来自投递台账，matrix 轮来自项目 meta
  const execution = ledger
    ? {
        source: 'delivery_ledger',
        status: ledger.status ?? null,
        received_at: ledger.received_at ?? null,
        claimed_at: ledger.claimed_at ?? null,
        processed_at: ledger.processed_at ?? null,
        note: ledger.error ?? null,
        delivery_id: ledger.delivery_id ?? null,
        event: ledger.event_name && ledger.action ? `${ledger.event_name}.${ledger.action}` : null,
      }
    : {
        source: 'project_meta',
        status: projectMeta?.status ?? null,
        received_at: kickoff?.KICKOFF_TS_UTC ?? null,
        claimed_at: null,
        processed_at: null,
        note: null,
        delivery_id: null,
        event: null,
      };

  // 审查结论 — 与执行状态、发布状态分离
  const gateDecision = gateSent?.mode
    ? gateSent.mode === 'approve'
      ? 'APPROVED'
      : gateSent.mode === 'reject'
        ? 'REJECTED'
        : gateSent.mode
    : resultMd?.gate ?? gateMd?.decision ?? null;
  const gateSource = gateSent?.mode
    ? 'gate-approval-sent.json'
    : resultMd?.gate
      ? 'project/result.md'
      : gateMd?.raw ?? null;
  const review = {
    source: resultMd ? 'project/result.md' : reviewTask ? reviewTask.raw : checkRun ? 'check-run.json (summary)' : null,
    project_status: projectMeta?.status ?? null,
    verdict: resultMd?.verdict ?? reviewTask?.verdict ?? checkRun?.verdict ?? null,
    severity: resultMd?.severity ?? reviewTask?.severity ?? null,
    cwe: resultMd?.cwe ?? reviewTask?.cwe ?? null,
    human_gate: gateDecision,
    human_gate_source: gateSource,
    status_line: resultMd?.status_line ?? null,
  };

  // 发布状态 — GitHub check-run 事实，独立于审查结论
  const publish = checkRun
    ? {
        status: 'published',
        check_run_id: checkRun.check_run_id,
        conclusion: checkRun.conclusion,
        url: checkRun.url,
        started_at: checkRun.started_at,
        completed_at: checkRun.completed_at,
        source: 'check-run.json',
      }
    : {
        status: ledger?.status === 'PROCESSED' ? 'processed_no_checkrun_record' : 'not_recorded',
        check_run_id: null,
        conclusion: null,
        url: null,
        started_at: null,
        completed_at: null,
        source: null,
      };

  const createdAt =
    parseTs(execution.received_at) ?? parseTs(kickoff?.KICKOFF_TS_UTC) ?? parseTs(gateSent?.ts);
  let durationMs = null;
  if (execution.processed_at && execution.received_at) {
    const a = parseTs(execution.processed_at);
    const b = parseTs(execution.received_at);
    if (a && b) durationMs = a - b;
  }
  if (durationMs == null && kickoff?.KICKOFF_TS_UTC) {
    const tasks = parseTasks(packDir);
    const lastSubmitted = tasks
      .map((t) => parseTs(t.submitted_at))
      .filter((d) => d)
      .sort((a, b) => b - a)[0];
    const k = parseTs(kickoff.KICKOFF_TS_UTC);
    if (lastSubmitted && k) durationMs = lastSubmitted - k;
  }

  const prUrl = repo && prNumber ? `https://github.com/${repo}/pull/${prNumber}` : null;

  return {
    pack_id: packId,
    run_id: runId,
    repo,
    pr_number: prNumber,
    pr_title: prMeta?.title ?? null,
    pr_url: prUrl,
    head_sha: headSha,
    base_sha: baseSha,
    trigger,
    execution,
    review,
    publish,
    created_at: isoOrNull(createdAt),
    duration_ms: durationMs,
    duration_human: fmtDurationMs(durationMs),
    has_sums: existsFile(path.join(packDir, 'SHA256SUMS')),
  };
}

export function buildRunDetail(packId, packDir) {
  const record = buildRunRecord(packId, packDir);
  const tasks = parseTasks(packDir);
  const spanSummary = parseSpanSummary(packDir);
  const skillAudit = parseSkillAudit(packDir);
  const rag = parseRagSpans(packDir);
  const usage = parseUsage(packDir);
  const kickoff = readJsonSafe(path.join(packDir, 'kickoff.json'));
  const gateSent = readJsonSafe(path.join(packDir, 'gate-approval-sent.json'));

  // ---- timeline（各来源时间点合并，ts 缺失的排最后并标注来源） ----
  const events = [];
  const push = (ts, label, source, detail = null) =>
    events.push({ ts: isoOrNull(parseTs(ts)), ts_raw: ts ?? null, label, source, detail });
  if (record.execution.source === 'delivery_ledger') {
    push(record.execution.received_at, 'webhook 投递接收', 'delivery-ledger.json');
    push(record.execution.claimed_at, '桥认领', 'delivery-ledger.json');
    push(record.execution.processed_at, '投递处理完成', 'delivery-ledger.json', record.execution.note);
  }
  if (kickoff?.KICKOFF_TS_UTC) push(kickoff.KICKOFF_TS_UTC, 'kickoff 派发（Matrix）', 'kickoff.json');
  for (const t of tasks) {
    push(t.assigned_at, `${t.task_id} 委派（${t.role ?? '?'}）`, `tasks/${t.task_id}/meta.json`);
    push(t.acknowledged_at, `${t.task_id} 确认`, `tasks/${t.task_id}/meta.json`);
    push(t.submitted_at, `${t.task_id} 提交`, `tasks/${t.task_id}/meta.json`);
  }
  if (gateSent?.ts) push(gateSent.ts, `人工门决策：${gateSent.mode}`, 'gate-approval-sent.json');
  if (record.publish.status === 'published') {
    push(record.publish.started_at, 'check-run 发布', 'check-run.json', `id=${record.publish.check_run_id}`);
    push(record.publish.completed_at, 'check-run 完成', 'check-run.json', record.publish.conclusion);
  }
  events.sort((a, b) => {
    if (a.ts && b.ts) return a.ts.localeCompare(b.ts);
    if (a.ts) return -1;
    if (b.ts) return 1;
    return a.label.localeCompare(b.label);
  });

  // ---- 版本信息（只报包内实际存在的；缺失即 null） ----
  const resultMd = parseResultMd(packDir);
  const readmeText = readTextSafe(path.join(packDir, 'README.md')) ?? '';
  const imageMatch = readmeText.match(/(?:image|镜像)[^\n]*?([0-9a-f]{12,64})(?![\w.-])/i);
  const usageNote = usage?.note ?? '';
  const modelMatch = usageNote.match(/requested model (\S+)/);
  const skillByTool = new Map();
  if (skillAudit?.invocations) {
    for (const inv of skillAudit.invocations) {
      const cur = skillByTool.get(inv.tool) ?? { tool: inv.tool, count: 0, data_modes: new Set(), source_refs: new Set() };
      cur.count += 1;
      if (inv.data_mode) cur.data_modes.add(inv.data_mode);
      for (const r of inv.source_refs ?? []) cur.source_refs.add(r);
      skillByTool.set(inv.tool, cur);
    }
  }
  const versions = {
    run_manifest: existsFile(path.join(packDir, 'run-manifest.json')) ? 'run-manifest.json' : null,
    model: modelMatch ? modelMatch[1] : null,
    model_basis: modelMatch ? 'usage-summary.json note（会话级，非 per-run 记录）' : null,
    image: imageMatch ? imageMatch[1] : null,
    image_basis: imageMatch ? 'README.md 文本提及' : null,
    skills: [...skillByTool.values()].map((s) => ({
      tool: s.tool,
      count: s.count,
      data_modes: [...s.data_modes],
      source_refs: [...s.source_refs],
    })),
    span_summary: spanSummary,
    note: '历史证据包早于 run-manifest 机制（桥自 81e0045 起在派发前写入 MinIO 清单）；本页字段均取自包内记录，缺失即未记录。',
  };

  // ---- RAG 状态判定（六态区分） ----
  let ragState;
  if (rag && rag.calls.length > 0) {
    ragState = { state: 'called', ...rag };
  } else if (rag) {
    ragState = { state: 'no_calls', ...rag };
  } else if (spanSummary) {
    const total = Object.values(spanSummary).reduce((acc, v) => acc + (v.rag ?? 0), 0);
    ragState = {
      state: total > 0 ? 'counted_only' : 'not_called',
      basis: 'span-summary.json 各角色 rag 计数',
      total,
    };
  } else {
    ragState = { state: 'insufficient_data' };
  }

  const window = usageWindowForPack(packId, usage);

  return {
    ...record,
    project: {
      project_id: kickoff?.project ?? readJsonSafe(path.join(packDir, 'project/meta.json'))?.project_id ?? null,
      title: readJsonSafe(path.join(packDir, 'project/meta.json'))?.title ?? null,
    },
    dag: resultMd?.dag ?? [],
    tasks,
    timeline: events,
    versions,
    skill_audit: skillAudit,
    rag: ragState,
    usage: usage ? { ...usage, matched_window: window } : null,
  };
}
