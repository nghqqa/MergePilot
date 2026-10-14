// ReviewArtifactsPanel — 审查产物面板（2026-10-13 产物交付波；2026-10-14 展示收口）。
// 数据：/api/mu/runs?repo_id → 最新 run → /api/mu/runs/:id/artifacts（概览/问题/链输出）；
//       补丁正文经 /api/mu/runs/:id/patch.diff（read_code_content 门，与下载同源同权限）。
// 阅读顺序：发现什么问题 → 为什么 → 建议怎么改 → 验证了什么 → 下载或发布。
// 状态：loading / no-pr / no-run / forbidden / error / ready；切换 PR/run 时取消旧请求。
import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  Alert, Button, Collapse, Descriptions, Drawer, Input, Popconfirm,
  Select, Space, Table, Tag, Tooltip, Typography,
} from 'antd';

const csrfHeader = () => {
  const row = document.cookie.split(';').map((s) => s.trim()).find((s) => s.startsWith('mp_csrf='));
  return row ? { 'X-CSRF-Token': decodeURIComponent(row.slice('mp_csrf='.length)) } : {};
};
const apiFetch = (path, opts = {}) => fetch(path, { credentials: 'same-origin', ...opts });

const NL = String.fromCharCode(10);
const SEV_COLOR = { P0: 'red', P1: 'orange', P2: 'gold', P3: 'default' };
const FEEDBACK_LABEL = { confirm: '确认问题', false_positive: '误报', wont_fix: '暂不处理' };
const ROLE_LABEL = { maintainer: '维护者', platform_admin: '平台管理员', reviewer: '审查者', contributor: '贡献者', auditor: '审计员' };
const LOGIN_LABEL = { 'github-oauth': 'GitHub OAuth', 'gitee-oauth': 'Gitee OAuth' };
const PROVIDER_LABEL = { github: 'GitHub', gitee: 'Gitee' };

/** 用户可见时间：明确标注 UTC（避免被当作北京时间）。 */
const utc = (v) => (v ? `${String(v).slice(0, 19).replace('T', ' ')} UTC` : '—');

/**
 * 补丁验证三分的权威口径（与后端 artifacts.applicabilityVerdict 同一逻辑）：
 *  1) 格式与路径（结构级）——结构通过 ≠ 可应用；
 *  2) 真实可应用性 git apply --check——pass/fail/未执行/环境不可用/证据失效（digest 绑定）五态；
 *  3) 实际修复测试——test_verification 缺失=未执行。
 * 返回 { level, text, structuralText } 或 null（无补丁）。
 */
function patchVerdict(applicability, patchDigest) {
  const a = applicability ?? null;
  if (!a) return null;
  const ISSUE_MAP = {
    path_not_in_change_set: '触及了变更集之外的文件',
    unsafe_path: '包含不安全路径',
    not_unified_diff: '不是统一 diff 格式',
  };
  const msgs = (a.issues ?? []).map((i) => ISSUE_MAP[String(i).split(':')[0]] ?? i);
  const structuralText = a.ok
    ? '格式与路径检查（结构级）：通过（这只是结构检查，不代表可应用）'
    : `格式与路径检查（结构级）：未通过——${msgs.join('；') || '未知原因'}`;
  const ga = a.git_apply ?? null;
  let level = 'not_executed';
  let text = '真实可应用性：未执行 git apply --check（结构检查通过 ≠ 可应用）';
  if (ga && patchDigest && ga.patch_digest && ga.patch_digest !== patchDigest) {
    level = 'stale';
    text = '真实可应用性：检查证据已失效（补丁内容已更新，当前 digest 与检查时不一致）——旧结果不代表当前补丁';
  } else if (!ga) {
    level = 'not_executed';
  } else if (ga.verified === false) {
    level = 'env_unavailable';
    text = `真实可应用性：未验证（环境不可用：${ga.reason ?? '未知原因'}）——不误报通过`;
  } else if (ga.ok) {
    level = 'pass';
    text = `真实可应用性：通过（git apply --check 对准确 head${ga.checked_at ? `，${utc(ga.checked_at)}` : ''}）`;
  } else {
    level = 'fail';
    text = `真实可应用性：失败——补丁不可应用（${ga.output ?? 'git apply --check 未通过'}）`;
  }
  return { level, text, structuralText };
}

/** 历史追溯记录（retro_* 键）——保留原始证据，只读展示不参与当前版本判定。 */
function retroRecords(applicability) {
  const keys = Object.keys(applicability ?? {}).filter((k) => k.startsWith('retro_'));
  return keys.map((k) => {
    const ev = applicability[k] ?? {};
    const ga = ev.git_apply ?? {};
    return { key: k, ok: ga.ok ?? null, checked_at: ga.checked_at ?? null,
      note: ev.attribution ? String(ev.attribution).slice(0, 200) : null };
  });
}

/** 统一 diff → 逐文件结构（供逐文件渲染；解析失败返回 null——调用方如实展示）。 */
function splitDiffFiles(diffText) {
  const text = String(diffText ?? '');
  if (!text.trim()) return null;
  const files = [];
  let cur = null;
  let sawHunk = false;
  for (const line of text.split('\n')) {
    if (line.startsWith('--- ')) {
      cur = { oldPath: line.slice(4).trim(), additions: 0, deletions: 0, lines: [] };
      continue;
    }
    if (line.startsWith('+++ ')) {
      if (!cur) return null;
      cur.path = line.slice(4).trim().replace(/^b\//, '');
      if (cur.oldPath.startsWith('a/')) cur.oldPath = cur.oldPath.slice(2);
      files.push(cur);
      continue;
    }
    if (line.startsWith('@@')) { sawHunk = true; if (cur) cur.lines.push(line); continue; }
    if (cur && files.length) {
      cur.lines.push(line);
      if (line.startsWith('+')) cur.additions += 1;
      else if (line.startsWith('-')) cur.deletions += 1;
    }
  }
  if (!files.length || !sawHunk) return null;
  return files;
}

/** 单文件 diff 块（行级增删着色；横向滚动在自身区域）。 */
function DiffFile({ file }) {
  return (
    <div className="mu-diff-file">
      <div className="mu-diff-file-header">
        <span className="path">{file.path}</span>
        <span>
          <Tag color="green" style={{ marginInlineEnd: 4 }}>+{file.additions}</Tag>
          <Tag color="red" style={{ marginInlineEnd: 0 }}>-{file.deletions}</Tag>
        </span>
      </div>
      <pre className="mu-diff-pre">
        {file.lines.map((line, i) => {
          const cls = line.startsWith('+') ? 'mu-diff-line-add'
            : line.startsWith('-') ? 'mu-diff-line-del' : null;
          return cls
            ? <span key={i} className={cls}>{line + NL}</span>
            : <span key={i}>{line + NL}</span>;
        })}
      </pre>
    </div>
  );
}

export function ReviewArtifactsPanel({ repoId, prNumber, myActions = [] }) {
  const [state, setState] = useState('loading'); // loading|no-pr|no-run|forbidden|error|ready
  const [art, setArt] = useState(null);
  const [runId, setRunId] = useState(null);
  const [runMeta, setRunMeta] = useState(null); // {owner, repo_name, provider}
  // rc.34 权限与发布上下文可见化：会话身份 / 该 PR 全部 runs / 用户选中 run / 预览远端上下文
  const [identity, setIdentity] = useState(null); // {login, provider, tenant, role, actions}
  const [runsList, setRunsList] = useState([]);   // 该 PR 全部 run（新→旧）
  const [selectedRunId, setSelectedRunId] = useState(null);
  const [pubCtx, setPubCtx] = useState(null);     // {remote_head, head_match, robot_identity, remote_head_error}
  // 补丁正文加载态：none（无补丁）/ loading / loaded / forbidden / failed / corrupt
  const [patchState, setPatchState] = useState('idle');
  const [patchText, setPatchText] = useState(null);
  const [fbNote, setFbNote] = useState({});   // 每 finding 独立说明
  const [fbBusy, setFbBusy] = useState(null); // `${findingId}:${action}`
  const [fbMsg, setFbMsg] = useState({});     // {findingId: {tone,text}}
  const [copied, setCopied] = useState(null); // 复制反馈：文件 key
  const [pubPreview, setPubPreview] = useState(null);
  const [pubBusy, setPubBusy] = useState(false);
  const [pubMsg, setPubMsg] = useState(null);
  const [dlMsg, setDlMsg] = useState(null);
  const loadCtlRef = useRef(null);
  const patchCtlRef = useRef(null);

  const canFeedback = myActions.includes('request_review') || myActions.includes('decide_review');
  const isLatestRun = Boolean(runsList.length && selectedRunId
    && selectedRunId === runsList[0].run_id);
  // 发布按钮置灰原因（用户可理解文案；后端 RBAC 仍为最终授权方）
  const effActions = identity?.actions ?? myActions;
  const publishButtonBlockReason =
    (!isLatestRun && runsList.length) ? '发布需要最新审查 run（当前显示为历史运行，仅查看）——点击「查看最新 run」切换'
    : !effActions.includes('decide_review')
      ? (identity?.role === 'platform_admin'
        ? '当前角色为平台管理员，未授予 PR 评论发布权限（平台管理与审查裁定分离）——请用维护者账号发布'
        : identity?.role === 'auditor'
          ? '审计角色仅只读，不能发布 PR 评论'
          : `当前角色（${ROLE_LABEL[identity?.role] ?? identity?.role ?? '未知'}）无发布权限——仅维护者（maintainer）可发布`)
      : null;

  const loadPatch = useCallback(async (rid, signal) => {
    setPatchState('loading');
    try {
      const r = await apiFetch(`/api/mu/runs/${rid}/patch.diff`, { signal: signal ?? patchCtlRef.current?.signal });
      if (signal?.aborted) return;
      if (r.status === 403) { setPatchState('forbidden'); return; }
      if (r.status === 404) { setPatchState('none'); return; }
      if (!r.ok) { setPatchState('failed'); return; }
      const text = await r.text();
      if (signal?.aborted) return;
      if (!text.trim()) { setPatchState('corrupt'); return; }
      setPatchText(text);
      setPatchState('loaded');
    } catch (e) {
      if (e?.name !== 'AbortError' && !signal?.aborted) setPatchState('failed');
    }
  }, []);

  // 拉取指定 run 的产物（run 选择语义：面板绑定用户明确选中的 run——切换时取消旧请求）
  const loadArtifacts = useCallback(async (rid, signal) => {
    if (!rid) return;
    const ar = await apiFetch(`/api/mu/runs/${rid}/artifacts`, { signal });
    if (signal?.aborted) return;
    if (ar.status === 403) { setState('forbidden'); return; }
    if (!ar.ok) { setState('error'); return; }
    const ad = await ar.json().catch(() => null);
    if (signal?.aborted || !ad?.run) return;
    setRunId(rid);
    setRunMeta({ owner: ad.run.owner, repo_name: ad.run.repo_name, provider: ad.run.provider,
      head_sha: ad.run.head_sha, created_at: ad.run.created_at });
    setArt(ad);
    setPubCtx(null); setPubMsg(null); setPubPreview(null); // 切 run 后旧发布上下文/结果不残留
    setState('ready');
    if (ad.patch?.patch_id) loadPatch(rid, signal);
  }, [loadPatch]);

  const load = useCallback(async () => {
    if (!repoId || !prNumber) { setState('no-pr'); return; }
    if (loadCtlRef.current) loadCtlRef.current.abort();
    const ctl = new AbortController();
    loadCtlRef.current = ctl;
    setState('loading');
    try {
      // 会话身份与权限（rc.34）：/api/mu/session 只读展示字段（无 token/内部键）
      apiFetch('/api/mu/session', { signal: ctl.signal })
        .then((r) => (r.ok ? r.json() : null))
        .then((s) => { if (!ctl.signal.aborted && s?.user) {
          setIdentity({ login: s.user.login, provider: s.provider ?? null,
            tenant: s.tenant?.display_name ?? s.tenant?.slug ?? null,
            role: s.role ?? null, actions: s.actions ?? [] });
        } }).catch(() => {});
      const lr = await apiFetch(`/api/mu/runs?repo_id=${encodeURIComponent(repoId)}`, { signal: ctl.signal });
      if (ctl.signal.aborted) return;
      if (!lr.ok) { setState('error'); return; }
      const ld = await lr.json().catch(() => null);
      if (ctl.signal.aborted) return;
      const mine = (ld?.runs ?? []).filter((r) => Number(r.provider_pr_number) === Number(prNumber))
        .sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
      setRunsList(mine);
      const latest = mine[0];
      if (!latest) { setState('no-run'); return; }
      setSelectedRunId(latest.run_id);
      await loadArtifacts(latest.run_id, ctl.signal);
    } catch (e) {
      if (e?.name !== 'AbortError' && !ctl.signal.aborted) setState('error');
    }
  }, [repoId, prNumber, loadArtifacts]);

  useEffect(() => { load(); return () => { loadCtlRef.current?.abort(); patchCtlRef.current?.abort(); }; }, [load]);

  // 有补丁但正文尚未加载（历史挂载/重载后）→ 补加载
  useEffect(() => {
    if (state === 'ready' && art?.patch?.patch_id && patchState === 'idle') {
      loadPatch(runId);
    }
  }, [state, art, runId, patchState, loadPatch]);

  const retryPatch = () => { setPatchState('idle'); loadPatch(runId); };

  const sendFeedback = async (findingId, action) => {
    const key = `${findingId}:${action}`;
    setFbBusy(key);
    try {
      const r = await apiFetch(`/api/mu/findings/${findingId}/feedback`, {
        method: 'POST', headers: { 'content-type': 'application/json', ...csrfHeader() },
        body: JSON.stringify({ action, note: fbNote[findingId] ?? '' }),
      });
      const b = await r.json().catch(() => ({}));
      if (r.status === 403) setFbMsg((m) => ({ ...m, [findingId]: { tone: 'warning', text: '当前角色无权提交反馈（需 reviewer 及以上）' } }));
      else if (!r.ok) setFbMsg((m) => ({ ...m, [findingId]: { tone: 'warning', text: `反馈未完成（HTTP ${r.status}）` } }));
      else {
        setFbMsg((m) => ({ ...m, [findingId]: { tone: 'success', text: `已记录：${FEEDBACK_LABEL[action]}（原始审查结论保留）` } }));
        setFbNote((n) => ({ ...n, [findingId]: '' }));
      }
    } catch {
      setFbMsg((m) => ({ ...m, [findingId]: { tone: 'warning', text: '网络异常，反馈未提交' } }));
    }
    setFbBusy(null);
  };

  const openPreview = async () => {
    setPubBusy(true);
    try {
      const r = await apiFetch(`/api/mu/runs/${runId}/publish`);
      const b = await r.json().catch(() => ({}));
      if (!r.ok) setPubMsg({ tone: 'warning', text: `预览未完成（HTTP ${r.status}）：${b?.error?.reason ?? ''}` });
      else {
        setPubPreview(b);
        // rc.34：远端上下文可见化（远端当前 head / 一致性 / 机器人写入身份）
        setPubCtx({ remote_head: b.remote_head ?? null, head_match: b.head_match ?? null,
          robot_identity: b.robot_identity ?? null, remote_head_error: b.remote_head_error ?? null });
      }
    } catch { setPubMsg({ tone: 'warning', text: '网络异常，预览未完成' }); }
    setPubBusy(false);
  };

  const confirmPublish = async () => {
    setPubBusy(true);
    try {
      const r = await apiFetch(`/api/mu/runs/${runId}/publish`, {
        method: 'POST', headers: { 'content-type': 'application/json', ...csrfHeader() },
        body: JSON.stringify({ confirm: true }),
      });
      const b = await r.json().catch(() => ({}));
      if (r.ok) {
        setPubMsg({ tone: 'success', url: b.remote_url,
          text: `已${b.updated ? '原位更新' : '发布'}到原 PR${b.remote_url ? '——点击右侧查看' : ''}（评论 ${String(b.remote_comment_id ?? '').slice(0, 12)}）` });
        setPubPreview(null);
      } else {
        const stale = b?.error?.stage === 'stale_head';
        const detail = String(b?.error?.detail?.reason ?? b?.error?.reason ?? '');
        setPubMsg({ tone: 'warning', text: stale
          ? 'head 已变化，拒绝发布：该结果基于旧版本——远端代码已推进，陈旧结论不再发出（PR 更新后重新审查即可获得新结果）'
          : /403|token|permission/i.test(detail) && /ghp_|github/i.test(detail)
            ? `目标平台远端写权限不足——检查 GitHub App 安装授权（${detail.slice(0, 60)}）`
            : `发布未完成（HTTP ${r.status}）：${b?.error?.reason ?? ''}${b?.error?.status === 'unconfirmed' ? '（结果不明，请先查验远端再重试）' : ''}` });
        if (!stale) setPubPreview(null);
      }
    } catch { setPubMsg({ tone: 'warning', text: '网络异常，发布未完成' }); }
    setPubBusy(false);
  };

  const download = async (kind, filename) => {
    setDlMsg(null);
    const r = await apiFetch(`/api/mu/runs/${runId}/${kind}`);
    if (r.status === 403) { setDlMsg({ tone: 'warning', text: '下载被拒：当前角色无代码内容读取权限' }); return; }
    if (r.status === 404 && kind === 'patch.diff') { setDlMsg({ tone: 'warning', text: '本轮无结构化补丁可下载' }); return; }
    if (!r.ok) { setDlMsg({ tone: 'warning', text: `下载失败（HTTP ${r.status}）` }); return; }
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = filename; a.click();
    URL.revokeObjectURL(url);
    setDlMsg({ tone: 'success', text: `已下载 ${filename}` });
  };

  const copyText = async (key, text) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(key);
      setTimeout(() => setCopied((c) => (c === key ? null : c)), 2000);
    } catch { setCopied(`${key}:fail`); setTimeout(() => setCopied(null), 2000); }
  };

  // ── 渲染 ──
  if (state === 'loading') return <Alert type="info" showIcon message="审查产物：加载中…" />;
  if (state === 'no-pr' || state === 'no-run') {
    return <Alert type="info" showIcon message="审查产物：该 PR 尚无审查 run"
      description="发起审查后，问题清单、建议补丁、验证记录与下载会展示在这里。" />;
  }
  if (state === 'forbidden') return <Alert type="warning" showIcon message="审查产物：当前角色无权查看" />;
  if (state === 'error') return <Alert type="warning" showIcon message="审查产物：加载失败（网络或服务异常）"
    action={<Button size="small" onClick={load}>重试</Button>} />;

  const findings = art?.findings ?? [];
  const outputs = art?.outputs ?? {};
  const patch = art?.patch ?? null;
  const publish = art?.publish ?? null;
  const parseState = art?.parse_state ?? {};
  const run = art?.run ?? {};
  const provider = runMeta?.provider ?? run?.provider ?? null;
  const owner = runMeta?.owner ?? run?.owner ?? null;
  const repoName = runMeta?.repo_name ?? run?.repo_name ?? null;
  const headSha = run.head_sha ?? '';
  const p0 = findings.filter((f) => f.severity === 'P0').length;
  const p1 = findings.filter((f) => f.severity === 'P1').length;
  // LLM 二阶段 run 只有 findings（无四角色 review_output）——有 finding 不算历史缺失
  const outputsMissing = !outputs.reviewer && !outputs.leader && !outputs.fixer && !outputs.verifier
    && !(art?.findings ?? []).length;
  const sourceUrl = (f) => {
    if (!provider || !owner || !repoName) return null;
    const base = provider === 'gitee' ? 'https://gitee.com' : provider === 'github' ? 'https://github.com' : null;
    if (!base) return null;
    return `${base}/${owner}/${repoName}/blob/${headSha}/${f.path}${f.line_start ? '#L' + f.line_start : ''}`;
  };
  const patchFiles = patch?.files ?? [];
  const verResult = [
    { kind: 'static_rules', label: '静态规则检查（首轮规则引擎）', executed: true,
      result: findings.length ? `发现 ${findings.length} 项（P0×${p0}，P1×${p1}）` : '无发现' },
    { kind: 'model_chain', label: '模型评估（AgentTeams 四角色链）',
      executed: Object.keys(outputs).length > 0,
      result: outputs.verifier?.verdict ? `verifier 结论：${outputs.verifier.verdict}` : (Object.keys(outputs).length ? '已执行' : '无输出'),
      note: '模型判断，非实际测试' },
    { kind: 'real_tests', label: '实际修复测试（隔离环境+预批准命令）',
      executed: Boolean(patch?.test_verification?.verified),
      result: patch?.test_verification?.verified
        ? (patch.test_verification.ok ? `通过（${patch.test_verification.test_cmd}）` : '未通过')
        : '未执行',
      note: patch?.test_verification?.verified
        ? (patch.test_verification.ok ? '修复前失败→修复后通过' : '修复后未通过或修复前即通过——如实展示')
        : '模型评估/可应用性通过 ≠ 行为正确——未执行如实标注' },
  ];
  const verdict = patchVerdict(patch?.applicability, patch?.patch_digest ?? null);
  const retro = retroRecords(patch?.applicability);
  const diffFiles = patchState === 'loaded' ? splitDiffFiles(patchText) : null;

  const findingDetail = (f) => {
    const srcLink = sourceUrl(f);
    // 口径：静态规则发现——"原因"=规则说明（title），命中行归"依据"；
    // 模型发现——"原因"=模型实际输出（summary）。绝不拿代码片段冒充原因。
    const isStatic = String(f.source ?? '') === 'precheck';
    const sm = String(f.summary_masked ?? '');
    // 历史占位文案兼容（rc.35.4）：占位（'LLM 建议详见 remediation…'）=summary_masked
    // 丢失了真实原因——优先回退 title（已保存的真实模型问题描述）；无 title 显示
    // "未提供问题原因"。不把修复建议冒充原因、不编造历史分析。
    const HISTORICAL_PLACEHOLDER = 'LLM 建议详见 remediation';
    const cause = isStatic
      ? (f.title ? `静态规则命中：${f.title}` : '（未提供）')
      : (sm.includes(HISTORICAL_PLACEHOLDER)
        ? (String(f.title ?? '').trim() || '未提供问题原因')
        : (sm || String(f.title ?? '') || '（未提供）'));
    const evidenceLine = isStatic && f.summary_masked ? `命中行：${f.summary_masked}` : null;
    return (
      <div className="mu-finding-detail">
        <dl>
          <dt>位置</dt>
          <dd><code className="mono">{f.path}{f.line_start ? ':' + f.line_start : ''}{f.line_end && f.line_end !== f.line_start ? `-${f.line_end}` : ''}</code></dd>
          <dt>原因</dt>
          <dd>{cause}</dd>
          <dt>依据</dt>
          <dd>{evidenceLine ? <div style={{ marginBottom: 2 }}><code className="mono" style={{ fontSize: 11 }}>{evidenceLine}</code></div> : null}
            {f.evidence_ref ? <code className="mono" style={{ fontSize: 11, wordBreak: 'break-all' }}>{String(f.evidence_ref).slice(0, 80)}</code> : (evidenceLine ? null : '未提供')}</dd>
          <dt>修复建议</dt>
          <dd>{f.remediation ?? '未提供'}</dd>
          <dt>源码（被审查 head）</dt>
          <dd>{srcLink
            ? <a href={srcLink} target="_blank" rel="noreferrer">{f.path}:{f.line_start ?? 1} @ {String(headSha).slice(0, 10)}</a>
            : '（当前接入平台未提供源码链接）'}</dd>
        </dl>
        {canFeedback ? (
          <Space wrap style={{ marginTop: 4 }}>
            {['confirm', 'false_positive', 'wont_fix'].map((a) => (
              <Popconfirm key={a} title={`${FEEDBACK_LABEL[a]}？`} description="原始审查结论将保留存档"
                onConfirm={() => sendFeedback(f.finding_id, a)}>
                <Button size="small" loading={fbBusy === `${f.finding_id}:${a}`}>{FEEDBACK_LABEL[a]}</Button>
              </Popconfirm>
            ))}
            <Input size="small" style={{ width: 220, maxWidth: '100%' }} placeholder="可选说明（≤500 字）"
              value={fbNote[f.finding_id] ?? ''}
              onChange={(e) => setFbNote((n) => ({ ...n, [f.finding_id]: e.target.value }))} />
          </Space>
        ) : (
          <div className="muted" style={{ fontSize: 12, marginTop: 6 }}>反馈需 reviewer 及以上角色。</div>
        )}
        {fbMsg[f.finding_id] ? (
          <Alert size="small" style={{ marginTop: 6 }} type={fbMsg[f.finding_id].tone} showIcon message={fbMsg[f.finding_id].text} />
        ) : null}
      </div>
    );
  };

  const publishStatusNode = publish?.status === 'published'
    ? <Tag color="green">已发布（{publish.provider} PR#{publish.provider_pr_number}）</Tag>
    : publish?.status === 'unconfirmed'
      ? <Tag color="orange">结果待确认</Tag>
      : publish?.status === 'failed'
        ? <Tag color="red">上次发布失败</Tag>
        : <Tag>未发布</Tag>;

  return (
    <div className="mu-artifacts-panel" style={{ margin: '10px 0' }}>
      <Typography.Title level={5} style={{ marginBottom: 8 }}>审查产物与交付</Typography.Title>

      {/* ① 结果概览（默认可见） */}
      <Descriptions size="small" column={{ xs: 1, sm: 2 }} bordered style={{ marginBottom: 10 }}>
        <Descriptions.Item label="发现">{`${findings.length} 项（P0×${p0}，P1×${p1}）`}</Descriptions.Item>
        <Descriptions.Item label="建议补丁">{patch
          ? <Tag color="blue">已有（触及 {(patchFiles.length || (patch.files ?? []).length)} 文件，尚未应用）</Tag>
          : <Tag>无（仅文字建议或历史 run）</Tag>}</Descriptions.Item>
        <Descriptions.Item label="实际修复测试">{patch?.test_verification?.verified
          ? (patch.test_verification.ok
            ? <Tag color="green">通过（修复前败→修复后过）</Tag>
            : <Tag color="red">未通过</Tag>)
          : <Tag>未执行（如实登记）</Tag>}</Descriptions.Item>
        <Descriptions.Item label="PR 发布">{publishStatusNode}</Descriptions.Item>
        <Descriptions.Item label="被审查 head"><code className="mono">{String(headSha).slice(0, 12)}</code></Descriptions.Item>
        <Descriptions.Item label="审查轮次">{`首轮规则引擎${Object.keys(outputs).length ? ' + AgentTeams 模型评估' : '（仅首轮）'}`}</Descriptions.Item>
      </Descriptions>
      {parseState.parse_failures?.length ? (
        <Alert type="warning" showIcon style={{ marginBottom: 8 }}
          message={`部分角色输出解析失败：${parseState.parse_failures.join('、')}——原始内容已保留，不影响其余产物`} />
      ) : null}
      {outputsMissing ? (
        <Alert type="info" showIcon style={{ marginBottom: 8 }}
          message="历史产物缺失：该 run 早于产物持久化上线——如实展示既有摘要，不伪造补丁或正文" />
      ) : null}

      <Collapse ghost defaultActiveKey={['findings']}
        items={[
          { key: 'findings', label: `发现的问题（${findings.length}）——原因、依据与处置`, children: (
            findings.length ? (
              <Table size="small" pagination={findings.length > 8 ? { pageSize: 8 } : false}
                rowKey="finding_id" dataSource={findings}
                scroll={{ x: 'max-content' }}
                columns={[
                  { title: '问题', render: (_, f) => (
                    <div style={{ minWidth: 0 }}>
                      <div style={{ wordBreak: 'break-word' }}>{String(f.title ?? f.summary_masked ?? '（无标题）')}</div>
                      <div style={{ marginTop: 2 }}>
                        <Tag color={SEV_COLOR[f.severity] ?? 'default'}>{f.severity}</Tag>
                        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                          {f.path}{f.line_start ? ':' + f.line_start : ''} · 规则 {f.rule_id}
                        </Typography.Text>
                      </div>
                    </div>
                  ) },
                ]} expandable={{
                  defaultExpandAllRows: findings.length <= 5,
                  expandedRowRender: findingDetail,
                }} />
            ) : (
              <Typography.Text type="secondary">
                自动审查未发现问题——这是真实无发现，与"解析失败"或"产物缺失"是不同状态。
              </Typography.Text>
            )
          ) },
          { key: 'patch', label: patch ? '建议怎么改（建议补丁，尚未应用）' : '建议怎么改（本轮无补丁）', children: (
            patch ? (
              <>
                {verdict ? (
                  <div style={{ marginBottom: 8 }}>
                    <Typography.Paragraph style={{ fontSize: 13, marginBottom: 4 }}>
                      <Tag color={verdict.level === 'pass' ? 'green' : verdict.level === 'fail' ? 'red'
                        : verdict.level === 'env_unavailable' ? 'orange' : 'default'}>
                        {verdict.level === 'pass' ? '可应用性：通过' : verdict.level === 'fail' ? '可应用性：失败'
                          : verdict.level === 'env_unavailable' ? '可应用性：未验证' : verdict.level === 'stale' ? '可应用性：证据已失效' : '可应用性：未执行'}
                      </Tag>
                      {verdict.level === 'fail' ? <b>{verdict.text}</b> : verdict.text}
                    </Typography.Paragraph>
                    <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 4 }}>
                      {verdict.structuralText}
                    </Typography.Paragraph>
                    {patch?.patch_digest ? (
                      <Typography.Paragraph type="secondary" style={{ fontSize: 12, margin: 0 }}>
                        补丁 digest：<code className="mono">{String(patch.patch_digest).slice(0, 16)}</code>
                        {patch?.patch_version > 1 ? ` · 版本 v${patch.patch_version}（${patch?.correction_kind === 'deterministic' ? '确定性格式修正' : patch?.correction_kind === 'regenerated' ? '模型再生成' : '修正'}产物，原始失败版本已保留）` : ''}
                        {' '}——页面/复制/下载/检查使用同一版本；补丁更新后旧检查证据自动失效
                      </Typography.Paragraph>
                    ) : null}
                    {retro.length ? (
                      <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 0 }}>
                        历史追溯记录（保留原始证据，不参与当前版本判定）：
                        {retro.map((r) => ` ${r.key} → ${r.ok === true ? '可应用' : r.ok === false ? '不可应用' : '未验证'}${r.checked_at ? ` @ ${utc(r.checked_at)}` : ''}`).join('；')}
                      </Typography.Paragraph>
                    ) : null}
                  </div>
                ) : null}
                {patchState === 'loading' ? (
                  <Alert type="info" showIcon message="补丁正文加载中…" />
                ) : patchState === 'forbidden' ? (
                  <Alert type="warning" showIcon message="当前角色无权查看补丁正文（需代码内容读取权限）" />
                ) : patchState === 'failed' ? (
                  <Alert type="warning" showIcon message="补丁正文加载失败"
                    action={<Button size="small" onClick={retryPatch}>重试</Button>} />
                ) : patchState === 'corrupt' ? (
                  <Alert type="warning" showIcon message="补丁正文异常（内容为空或损坏）——已保留原始记录，不展示失真内容" />
                ) : patchState === 'loaded' ? (
                  diffFiles
                    ? <>
                      {diffFiles.map((f) => <DiffFile key={f.path} file={f} />)}
                      <Space wrap style={{ marginTop: 4 }}>
                        <Button size="small" onClick={() => copyText('diff', patchText)}>
                          {copied === 'diff' ? '已复制' : copied === 'diff:fail' ? '复制失败' : '复制完整 diff'}
                        </Button>
                        <Button size="small" onClick={() => download('patch.diff', `mergepilot-suggest-${String(runId).slice(0, 8)}.patch`)}>下载 .patch</Button>
                      </Space>
                      {copied === 'diff:fail' ? (
                        <Typography.Text type="warning" style={{ fontSize: 12, marginLeft: 8 }}>复制失败（浏览器未授权剪贴板）——可用下载代替</Typography.Text>
                      ) : null}
                    </>
                    : <Alert type="warning" showIcon message="补丁正文格式异常（无法按文件解析）——原始内容已保留，可尝试下载查看" />
                ) : null}
                <Typography.Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 6 }}>
                  对应 head：{String(patch.head_sha ?? headSha).slice(0, 12)} · 页面展示与下载的 .patch 为同一份内容。
                </Typography.Text>
              </>
            ) : (
              <Typography.Text type="secondary">
                本轮未产出结构化补丁——fixer 仅给出文字建议，或该 run 早于补丁持久化上线。不展示伪造补丁。
              </Typography.Text>
            )
          ) },
          { key: 'verify', label: '验证了什么（含未验证项）', children: (
            <Table size="small" rowKey="kind" pagination={false}
              dataSource={verResult} columns={[
                { title: '方式', dataIndex: 'label', render: (l, r) => (
                  <Space size={4}>{l}{!r.executed ? <Tag>未执行</Tag> : null}</Space>) },
                { title: '结果', dataIndex: 'result' },
                { title: '说明', dataIndex: 'note', render: (n) => n ?? '—' },
              ]} />
          ) },
        ]} />

      {/* ⑤ 统一操作区：身份与权限 → 审查对象 → 机器人身份 → 影响范围 → 按钮 → 结果 */}
      <div className="mu-art-actions" style={{ marginTop: 10, paddingTop: 10, borderTop: '1px solid var(--c-border)' }}>
        <Typography.Title level={5} style={{ marginTop: 0, marginBottom: 8 }}>下载与发布</Typography.Title>
        <Space wrap style={{ marginBottom: 8 }}>
          <Button size="small" onClick={() => download('report.md', `mergepilot-report-${String(runId).slice(0, 8)}.md`)}>下载 Markdown 报告</Button>
          <Button size="small" onClick={() => download('report.json', `mergepilot-report-${String(runId).slice(0, 8)}.json`)}>下载 JSON</Button>
          <Button size="small" disabled={!patch} onClick={() => download('patch.diff', `mergepilot-suggest-${String(runId).slice(0, 8)}.patch`)}>下载 .patch</Button>
        </Space>
        {dlMsg ? <Alert style={{ marginBottom: 8 }} type={dlMsg.tone} showIcon message={dlMsg.text} /> : null}

        {/* ① 当前身份与权限（页面 RBAC 展示；不展示 token/secret/内部权限键） */}
        <Descriptions size="small" column={1} bordered style={{ marginBottom: 8 }}
          title={<span style={{ fontSize: 13 }}>当前身份与权限</span>}>
          <Descriptions.Item label="登录身份">
            {identity
              ? <>{identity.login} · {LOGIN_LABEL[identity.provider] ?? '已登录'}{identity.tenant ? ` · 租户 ${identity.tenant}` : ''}</>
              : '读取中…'}
          </Descriptions.Item>
          <Descriptions.Item label="当前角色">
            {identity
              ? <Tag color={identity.role === 'maintainer' ? 'green' : identity.role === 'platform_admin' ? 'purple' : 'default'}>
                  {ROLE_LABEL[identity.role] ?? identity.role ?? '—'}
                </Tag>
              : '—'}
          </Descriptions.Item>
          <Descriptions.Item label="能力">
            {identity ? (
              <Space wrap size={4}>
                <Tag>{identity.actions?.includes('read_pull_request') ? '可查看审查产物' : '仅限基础查看'}</Tag>
                {identity.actions?.includes('decide_review')
                  ? <Tag color="green">可审批 / 可发布</Tag>
                  : <Tag>不可审批 / 不可发布</Tag>}
                {identity.actions?.includes('manage_membership') ? <Tag>可管理成员</Tag> : null}
              </Space>
            ) : '—'}
          </Descriptions.Item>
        </Descriptions>

        {/* ② run 选择（面板绑定用户明确选中的 run——历史 run 仅查看，不静默替换） */}
        {runsList.length > 1 ? (
          <div style={{ marginBottom: 8 }}>
            <Space wrap>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>产物面板默认显示该 PR 的最新运行结果；可切换查看：</Typography.Text>
              <Select size="small" style={{ minWidth: 220 }} value={selectedRunId}
                onChange={(v) => { setSelectedRunId(v); loadArtifacts(v, loadCtlRef.current?.signal); }}
                options={runsList.map((r, i) => ({ value: r.run_id,
                  label: `${i === 0 ? '（最新）' : ''}${String(r.head_sha ?? '').slice(0, 10)} · ${String(r.created_at ?? '').slice(0, 16).replace('T', ' ')} UTC` }))} />
              {!isLatestRun ? (
                <Button size="small" type="link" onClick={() => { setSelectedRunId(runsList[0].run_id); loadArtifacts(runsList[0].run_id, loadCtlRef.current?.signal); }}>
                  查看最新 run
                </Button>
              ) : null}
            </Space>
            {!isLatestRun ? (
              <div style={{ marginTop: 4 }}>
                <Tag color="orange">历史运行，仅查看</Tag>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>发布需要最新审查 run。</Typography.Text>
              </div>
            ) : null}
          </div>
        ) : null}

        {/* ②b 审查对象与目标 PR（目标平台由 run 所属仓库决定，与登录渠道无关） */}
        <Descriptions size="small" column={1} bordered style={{ marginBottom: 8 }}>
          <Descriptions.Item label="审查对象">
            {runMeta ? <>{PROVIDER_LABEL[runMeta.provider] ?? runMeta.provider} · {runMeta.owner}/{runMeta.repo_name} · PR#{prNumber}</> : '—'}
          </Descriptions.Item>
          <Descriptions.Item label="本次发布 run">
            <code className="mono">{String(runId ?? '').slice(0, 8)}</code>
            {runMeta?.head_sha ? <> · 审查 head <code className="mono" title={String(runMeta.head_sha)}>{String(runMeta.head_sha).slice(0, 12)}</code></> : null}
          </Descriptions.Item>
          <Descriptions.Item label="远端当前 head">
            {pubCtx
              ? (pubCtx.remote_head_error
                ? <Typography.Text type="warning">无法读取（{pubCtx.remote_head_error}）——点「预览并发布」重试</Typography.Text>
                : <> {pubCtx.head_match
                    ? <Tag color="green">与审查 head 一致，可发布</Tag>
                    : <Tag color="red">已变化——发布将被拒绝</Tag>}
                  <code className="mono" title={String(pubCtx.remote_head ?? '')}>{String(pubCtx.remote_head ?? '').slice(0, 12)}</code>
                </>)
              : <Typography.Text type="secondary" style={{ fontSize: 12 }}>点击「预览并发布」后自动核对</Typography.Text>}
          </Descriptions.Item>
          {/* ③ 远端写入身份（系统机器人凭证；与登录账号无关） */}
          <Descriptions.Item label="评论写入身份">
            {pubCtx?.robot_identity
              ? <>{pubCtx.robot_identity}（系统机器人凭证，非你的个人账号）</>
              : <Typography.Text type="secondary" style={{ fontSize: 12 }}>预览后显示——系统机器人，不使用个人账号</Typography.Text>}
          </Descriptions.Item>
          {/* ④ 影响范围 */}
          <Descriptions.Item label="影响范围">
            仅发布一条审查评论到原 PR，不修改任何代码；同一 run 重复发布为原位更新。
            {publish?.remote_url ? <> 评论直链：<a href={publish.remote_url} target="_blank" rel="noreferrer">查看已发布评论</a>。</> : null}
          </Descriptions.Item>
        </Descriptions>

        {/* ⑤ 按钮 + 状态（置灰必有用户可理解的原因） */}
        <div style={{ marginBottom: 6 }}>
          <Space wrap>
            <Tooltip title={publishButtonBlockReason ?? undefined}>
              <Button type="primary" disabled={Boolean(publishButtonBlockReason) || pubBusy}
                loading={pubBusy} onClick={openPreview}>
                预览并发布到 PR
              </Button>
            </Tooltip>
            {publishStatusNode}
          </Space>
          {publishButtonBlockReason ? (
            <Alert style={{ marginTop: 6 }} type="info" showIcon message={publishButtonBlockReason} />
          ) : (
            <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
              发布前会自动核对 head 是否变化；服务端权限与远端状态为最终授权方。
            </div>
          )}
          {pubMsg ? (
            <Alert style={{ marginTop: 6 }} type={pubMsg.tone} showIcon message={pubMsg.text}
              action={pubMsg.url ? <Button size="small" href={pubMsg.url} target="_blank" rel="noreferrer">查看评论</Button> : undefined} />
          ) : null}
        </div>
      </div>

      <Drawer title="发布预览（确认后才会写出）" width={720} open={Boolean(pubPreview)}
        onClose={() => setPubPreview(null)}
        footer={
          <Space>
            <Button onClick={() => setPubPreview(null)}>取消</Button>
            <Button type="primary" loading={pubBusy} onClick={confirmPublish}>
              {pubPreview?.action === 'update' ? '更新已有评论' : '确认发布到原 PR'}
            </Button>
          </Space>
        }>
        {pubPreview ? (
          <>
            <Descriptions size="small" column={1} bordered style={{ marginBottom: 10 }}>
              <Descriptions.Item label="目标">{pubPreview.target.provider} · {pubPreview.target.repo} · PR#{pubPreview.target.pr_number}</Descriptions.Item>
              <Descriptions.Item label="被审查 head"><code className="mono">{String(pubPreview.target.head).slice(0, 12)}</code></Descriptions.Item>
              <Descriptions.Item label="远端当前 head">{pubCtx?.remote_head_error
                ? <Typography.Text type="warning">无法读取（{pubCtx.remote_head_error}）</Typography.Text>
                : <>{pubCtx?.head_match ? <Tag color="green">与审查 head 一致</Tag> : <Tag color="red">已变化——确认将被拒绝</Tag>}<code className="mono">{String(pubCtx?.remote_head ?? '').slice(0, 12)}</code></>}</Descriptions.Item>
              <Descriptions.Item label="写入身份">{pubCtx?.robot_identity ?? '—'}（系统机器人，非个人账号）</Descriptions.Item>
              <Descriptions.Item label="动作">{pubPreview.action === 'update' ? '更新已有 MergePilot 评论（不覆盖人工评论）' : '创建新评论'}</Descriptions.Item>
            </Descriptions>
            <pre className="mu-diff-pre" style={{ whiteSpace: 'pre-wrap' }}>{pubPreview.comment}</pre>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              仅生成建议，未修改代码；默认不附原始源码/完整补丁/敏感原值。
            </Typography.Text>
          </>
        ) : null}
      </Drawer>
    </div>
  );
}
