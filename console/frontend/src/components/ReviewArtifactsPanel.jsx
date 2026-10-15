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
  // 应用修复 / 合并 PR（用户授权远端写入——每步 preview→confirm）
  const [applyPreview, setApplyPreview] = useState(null);
  const [applyBusy, setApplyBusy] = useState(false);
  const [applyMsg, setApplyMsg] = useState(null);
  const [mergePreview, setMergePreview] = useState(null);
  const [mergeBusy, setMergeBusy] = useState(false);
  const [mergeMsg, setMergeMsg] = useState(null);
  const [mergeMethod, setMergeMethod] = useState('merge');
  // UX-05（2026-10-15）：result_uncertain 的恢复入口——操作号与「读回对账」按钮必须
  // 同屏可达（此前按钮只在预览 Drawer 内，而 Drawer 在结果不明时已被关闭，指引落空）。
  // 对账只查询/定案（POST /reconcile），绝不重新发起 apply/merge。
  const [applyUncertain, setApplyUncertain] = useState(null);  // {operationId} | {operationId:null}
  const [mergeUncertain, setMergeUncertain] = useState(null);  // 同上
  const [mergeReconBusy, setMergeReconBusy] = useState(false);
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

  // 应用修复 / 合并 PR 按钮置灰原因（与发布同口径：最新 run + 角色能力；后端 RBAC 为最终授权方）
  const roleBlockText = (action) => (identity?.role === 'platform_admin'
    ? '当前角色为平台管理员，未授予该操作权限（平台管理与审查裁定分离）——请用维护者账号操作'
    : identity?.role === 'auditor'
      ? '审计角色仅只读，不能执行远端写操作'
      : `当前角色（${ROLE_LABEL[identity?.role] ?? identity?.role ?? '未知'}）无该操作权限——仅维护者（maintainer）可执行`);
  const applyBlockReason =
    (!isLatestRun && runsList.length) ? '应用修复需要最新审查 run（当前显示为历史运行）——点击「查看最新 run」切换'
    : !effActions.includes('apply_fix') ? roleBlockText('apply_fix')
    : !art?.patch ? '本轮审查未产生建议补丁，无可应用内容'
    : null;
  const mergeBlockReason =
    (!isLatestRun && runsList.length) ? '合并需要最新审查 run（当前显示为历史运行）——点击「查看最新 run」切换'
    : !effActions.includes('merge_pr') ? roleBlockText('merge_pr')
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

  // 拉取指定 run 的产物（run 选择语义：面板绑定用户明确选中的 run——切换时取消旧请求）。
  // opts.soft=true：同 run 的刷新（操作/对账后）——保留应用/合并的操作上下文与结果消息，
  // 否则 confirm 的错误文案与「结果不明」入口会被刷新无条件清掉（UX-05 修复）。
  const loadArtifacts = useCallback(async (rid, signal, opts = {}) => {
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
    if (opts.soft !== true) {
      setPubCtx(null); setPubMsg(null); setPubPreview(null); // 切 run 后旧发布上下文/结果不残留
      setApplyPreview(null); setApplyMsg(null); setMergePreview(null); setMergeMsg(null); // 同理：应用/合并上下文不残留
      setApplyUncertain(null); setMergeUncertain(null); // 切 run 后旧的结果不明上下文一并失效（操作号绑定旧 run）
    }
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

  // ── 应用修复 / 合并 PR（用户授权远端写；每步 preview→confirm）──

  const openApplyPreview = async () => {
    setApplyBusy(true); setApplyMsg(null);
    try {
      const r = await apiFetch(`/api/mu/runs/${runId}/apply-fix/preview`);
      const b = await r.json().catch(() => ({}));
      if (!r.ok) {
        setApplyMsg({ tone: 'warning', text: `预览未完成（HTTP ${r.status}）：${b?.error?.reason ?? ''}` });
      } else {
        setApplyPreview(b);
        if (!b.can_apply) {
          const why = { remote_unreachable: '远端不可达', head_moved: '远端 head 已变化（补丁基于旧版本——PR 更新后重新审查即可）',
            already_applied: '该补丁已成功应用过（重复确认被阻止）' }[b.blocked_reason] ?? b.blocked_reason;
          setApplyMsg({ tone: why === 'already_applied' ? 'success' : 'warning', text: `当前不可应用：${why ?? '条件不满足'}` });
        }
      }
    } catch { setApplyMsg({ tone: 'warning', text: '网络异常，预览未完成' }); }
    setApplyBusy(false);
  };

  const confirmApply = async () => {
    setApplyBusy(true);
    try {
      const r = await apiFetch(`/api/mu/runs/${runId}/apply-fix/confirm`, {
        method: 'POST', headers: { 'content-type': 'application/json', ...csrfHeader() },
        body: JSON.stringify({
          confirm: true,
          expected_remote_head: applyPreview?.confirm_binds?.expected_remote_head
            ?? applyPreview?.target?.remote_head ?? null,
          expected_patch_digest: applyPreview?.confirm_binds?.expected_patch_digest
            ?? applyPreview?.patch?.digest ?? null,
        }),
      });
      const b = await r.json().catch(() => ({}));
      if (r.ok) {
        setApplyMsg({ tone: 'success', text: b.idempotent
          ? `该补丁此前已应用（幂等返回，未重复提交）——提交 ${String(b.commit_sha ?? '').slice(0, 12)}`
          : `已提交到 PR 源分支——新提交 ${String(b.commit_sha ?? '').slice(0, 12)}（patch v${b.patch_version ?? '?'}）。${b.next_step ?? '等待按新 head 的重新审查与 CI 通过后，再预览合并。'}` });
        setApplyUncertain(null); // 本轮已定案——旧的结果不明入口撤下
        setApplyPreview(null);
        loadArtifacts(runId, loadCtlRef.current?.signal, { soft: true }); // 新 head run 入队后刷新产物列表
      } else {
        const er = b?.error?.reason ?? '';
        const why = { stale_head: '远端 head 已变化——补丁基于旧版本，未写入任何内容（PR 更新后重新审查）',
          head_moved_since_preview: '预览后远端又推进了——已拒绝，请刷新预览重试',
          patch_changed: '预览后补丁已更新（修正闭环产出了新版本）——已拒绝静默应用新版本，请重新预览',
          digest_mismatch: '存储补丁内容与登记摘要不一致——已拒绝执行，请重新生成补丁',
          invalid_confirm_fields: '确认缺少必要的预览绑定字段——请从预览发起确认',
          patch_apply_failed: '补丁在当前 head 上真实试应用失败——未写入任何内容（远端零副作用）',
          materialize_failed: '补丁在当前 head 上物化失败——未写入任何内容（远端零副作用）',
          branch_not_found: '远端源分支不存在或不可读',
          push_failed: '远端推送未完成（网络或平台原因）——请稍后重试',
          operation_in_progress: '已有一次相同操作正在进行——请稍后刷新查看结果' }[er];
        setApplyMsg({ tone: 'warning',
          text: er === 'result_uncertain'
            ? (() => {
              const opId = b?.error?.operation_id ?? null;
              setApplyUncertain({ operationId: opId });
              return opId
                ? `推送结果不明：候选提交已登记（操作 ${String(opId).slice(0, 8)}）——请点下方「读回对账」确认远端实际状态，勿直接重试`
                : '推送结果不明：操作已登记但未取回操作号——请刷新页面后从「应用修复与合并」预览的历史操作中对账，勿直接重试';
            })()
            : (why ?? `应用未完成（HTTP ${r.status}）：${er}`) });
        if (!/stale_head|head_moved/.test(er)) setApplyPreview(null);
        if (er === 'result_uncertain') loadArtifacts(runId, loadCtlRef.current?.signal, { soft: true });
      }
    } catch { setApplyMsg({ tone: 'warning', text: '网络异常，应用未完成' }); }
    setApplyBusy(false);
  };

  // 读回对账（apply+merge 通用端点 /api/mu/apply-operations/:id/reconcile）：
  // 只查询远端事实并定案操作记录——绝不重新发起 apply/merge，不产生第二次远端写。
  // definitive（reconciled 或已是确定态）→ 清除「结果不明」入口；仍不确定 → 保留可重试。
  const reconcileOpInto = async (opId, setMsg, setBusy, onDefinite) => {
    setBusy(true);
    try {
      const r = await apiFetch(`/api/mu/apply-operations/${opId}/reconcile`, {
        method: 'POST', headers: { ...csrfHeader() },
      });
      const b = await r.json().catch(() => ({}));
      if (r.ok && r.status !== 204) {
        const definitive = Boolean(b.reconciled)
          || ['committed', 'merged', 'completed', 'failed', 'previewed'].includes(b.status);
        setMsg({ tone: b.reconciled ? (b.status === 'failed' ? 'warning' : 'success') : 'info',
          text: b.note ?? `对账完成：${b.status ?? '未定案'}` });
        if (definitive) onDefinite?.(b);
        loadArtifacts(runId, loadCtlRef.current?.signal, { soft: true }); // 对账后刷新资格与操作记录（软刷新——保留结果消息）
      } else if (r.status === 204 || r.ok) {
        setMsg({ tone: 'info', text: '对账已发起' });
        loadArtifacts(runId, loadCtlRef.current?.signal);
      } else {
        setMsg({ tone: 'warning', text: `对账未完成（HTTP ${r.status}）：${b?.error?.reason ?? ''}——可稍后重试对账` });
      }
    } catch { setMsg({ tone: 'warning', text: '网络异常，对账未完成——可稍后重试' }); }
    setBusy(false);
  };
  const reconcileOp = (opId) => reconcileOpInto(opId, setApplyMsg, setApplyBusy, () => setApplyUncertain(null));
  const reconcileMergeUncertain = () => {
    const opId = mergeUncertain?.operationId;
    if (!opId) return;
    reconcileOpInto(opId, setMergeMsg, setMergeReconBusy, () => setMergeUncertain(null));
  };

  const openMergePreview = async () => {
    setMergeBusy(true); setMergeMsg(null);
    try {
      const r = await apiFetch(`/api/mu/runs/${runId}/merge-pr/preview`);
      const b = await r.json().catch(() => ({}));
      if (!r.ok) setMergeMsg({ tone: 'warning', text: `预览未完成（HTTP ${r.status}）：${b?.error?.reason ?? ''}` });
      else {
        setMergePreview(b);
        if (!b.can_merge) {
          const why = { pr_not_open: 'PR 已关闭', pr_not_mergeable: 'PR 当前不可合并（存在冲突）', head_unknown: '无法读取 PR head' }[b.blocked_reason] ?? b.blocked_reason;
          setMergeMsg({ tone: 'warning', text: `当前不可合并：${why ?? '条件不满足'}` });
        }
      }
    } catch { setMergeMsg({ tone: 'warning', text: '网络异常，预览未完成' }); }
    setMergeBusy(false);
  };

  const confirmMerge = async () => {
    setMergeBusy(true);
    try {
      const r = await apiFetch(`/api/mu/runs/${runId}/merge-pr/confirm`, {
        method: 'POST', headers: { 'content-type': 'application/json', ...csrfHeader() },
        body: JSON.stringify({ confirm: true, expected_head: mergePreview?.confirm_binds?.expected_head
          ?? mergePreview?.target?.head ?? null, merge_method: mergeMethod }),
      });
      const b = await r.json().catch(() => ({}));
      if (r.ok) {
        setMergeMsg({ tone: 'success', text: b.idempotent
          ? `该 PR 在此 head 上已合并过（幂等返回，未重复操作）`
          : `已合并——合并提交 ${String(b.merge_sha ?? '').slice(0, 12)}。请在平台上确认目标分支状态。` });
        setMergeUncertain(null); // 本轮已定案——旧的结果不明入口撤下
        setMergePreview(null);
      } else {
        const er = b?.error?.reason ?? '';
        const why = { pr_not_open: 'PR 已关闭（预览后被他人关闭）', pr_not_mergeable: 'PR 存在冲突，平台拒绝合并',
          head_moved_since_preview: '预览后 PR head 又推进了——已拒绝，请刷新预览重试',
          merge_not_eligible: `合并资格未满足：${(b?.error?.blocked ?? []).map((x) => x.detail ?? x.code).join('；') || '条件不满足'}`,
          invalid_confirm_fields: '确认缺少必要的预览绑定字段——请从预览发起确认',
          merge_failed: '平台合并被拒（保护规则/冲突/权限——详见平台页面）' }[er];
        if (er === 'result_uncertain') {
          const opId = b?.error?.operation_id ?? null;
          setMergeUncertain({ operationId: opId });
          setMergeMsg({ tone: 'warning', text: opId
            ? '合并结果不明：可能已合并也可能未合并——请点下方「读回对账」按 PR 实际状态定案，勿盲目重试'
            : '合并结果不明：操作已登记但未取回操作号——请刷新页面后重新预览查看历史操作中对账，勿盲目重试' });
        } else {
          setMergeMsg({ tone: 'warning', text: why ?? `合并未完成（HTTP ${r.status}）：${er}` });
        }
        if (!/head_moved/.test(er)) setMergePreview(null);
        if (er === 'result_uncertain') loadArtifacts(runId, loadCtlRef.current?.signal, { soft: true });
      }
    } catch { setMergeMsg({ tone: 'warning', text: '网络异常，合并未完成' }); }
    setMergeBusy(false);
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
                {identity.actions?.includes('apply_fix') ? <Tag color="green">可应用修复</Tag> : null}
                {identity.actions?.includes('merge_pr') ? <Tag color="green">可合并 PR</Tag> : null}
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

        {/* ⑥ 应用修复与合并（用户授权远端写——两步独立确认，任何一步都不自动执行） */}
        <div style={{ marginTop: 12, paddingTop: 10, borderTop: '1px dashed var(--c-border)' }}>
          <Typography.Title level={5} style={{ marginTop: 0, marginBottom: 8 }}>应用修复与合并</Typography.Title>
          <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 8 }}>
            两步手动确认流程：① 把建议补丁提交到 PR 源分支（新增一个提交，不合并）→
            平台触发重新审查/CI → ② 重新确认后合并 PR。每一步都先预览、后确认；
            远端已被他人推进时操作会被拒绝，不覆盖任何人的提交。
          </Typography.Paragraph>
          <Descriptions size="small" column={1} bordered style={{ marginBottom: 8 }}>
            <Descriptions.Item label="远端写入身份">
              {applyPreview?.robot_identity || mergePreview?.robot_identity
                ? <>{applyPreview?.robot_identity ?? mergePreview?.robot_identity}（系统机器人，非你的个人账号）</>
                : <Typography.Text type="secondary" style={{ fontSize: 12 }}>预览后显示——系统机器人，不使用个人账号</Typography.Text>}
            </Descriptions.Item>
            <Descriptions.Item label="影响范围">
              ① 在 PR 源分支新增提交（包含补丁全部变更）；② 将 PR 合并进目标分支。
              {publish?.remote_url ? null : null}
              两步均为条件写入；合并后回退需在平台上手动 revert（服务回滚不撤销已发生的远端提交/合并）。
            </Descriptions.Item>
          </Descriptions>
          <Space wrap>
            <Tooltip title={applyBlockReason ?? undefined}>
              <Button disabled={Boolean(applyBlockReason) || applyBusy} loading={applyBusy}
                onClick={openApplyPreview}>
                ① 预览并应用修复
              </Button>
            </Tooltip>
            <Tooltip title={mergeBlockReason ?? undefined}>
              <Button danger disabled={Boolean(mergeBlockReason) || mergeBusy} loading={mergeBusy}
                onClick={openMergePreview}>
                ② 预览并合并 PR
              </Button>
            </Tooltip>
          </Space>
          {applyBlockReason ? (
            <Alert style={{ marginTop: 6 }} type="info" showIcon message={applyBlockReason} />
          ) : mergeBlockReason ? (
            <Alert style={{ marginTop: 6 }} type="info" showIcon message={mergeBlockReason} />
          ) : (
            <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
              应用前自动重核远端 head 并在隔离副本真实试应用补丁；服务端权限与远端状态为最终授权方。
            </div>
          )}
          {applyMsg ? <Alert style={{ marginTop: 6 }} type={applyMsg.tone} showIcon message={applyMsg.text} /> : null}
          {/* UX-05：结果不明的恢复入口与文案同屏（对账按钮只调 reconcile，不重发写入） */}
          {applyUncertain ? (
            <Alert style={{ marginTop: 6 }} type="warning" showIcon
              message={applyUncertain.operationId
                ? `应用结果不明（操作 ${applyUncertain.operationId.slice(0, 8)}）——先对账定案，再决定下一步`
                : '应用结果不明（未取回操作号）——请刷新页面后从预览的历史操作中对账'}
              action={applyUncertain.operationId ? (
                <Button size="small" disabled={applyBusy} loading={applyBusy}
                  onClick={() => reconcileOp(applyUncertain.operationId)}>读回对账</Button>
              ) : undefined} />
          ) : null}
          {mergeMsg ? <Alert style={{ marginTop: 6 }} type={mergeMsg.tone} showIcon message={mergeMsg.text} /> : null}
          {mergeUncertain ? (
            <Alert style={{ marginTop: 6 }} type="warning" showIcon
              message={mergeUncertain.operationId
                ? `合并结果不明（操作 ${mergeUncertain.operationId.slice(0, 8)}）——先对账定案，再决定下一步`
                : '合并结果不明（未取回操作号）——请刷新页面后重新预览查看历史操作'}
              action={mergeUncertain.operationId ? (
                <Button size="small" disabled={mergeReconBusy} loading={mergeReconBusy}
                  onClick={reconcileMergeUncertain}>读回对账</Button>
              ) : undefined} />
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

      {/* 应用修复预览（确认后才会向远端写入） */}
      <Drawer title="应用修复预览（确认后才会写入远端）" width={640} open={Boolean(applyPreview)}
        onClose={() => setApplyPreview(null)}
        footer={
          <Space>
            <Button onClick={() => setApplyPreview(null)}>取消</Button>
            <Button type="primary" danger={!applyPreview?.can_apply} disabled={!applyPreview?.can_apply}
              loading={applyBusy} onClick={confirmApply}>
              确认提交到 PR 源分支
            </Button>
          </Space>
        }>
        {applyPreview ? (
          <>
            <Descriptions size="small" column={1} bordered style={{ marginBottom: 10 }}>
              <Descriptions.Item label="目标">
                {applyPreview.target.provider} · {applyPreview.target.repo} · PR#{applyPreview.target.pr_number}
                （源分支 <code className="mono">{applyPreview.target.source_branch ?? '—'}</code>）
              </Descriptions.Item>
              <Descriptions.Item label="补丁基于 head">
                <code className="mono">{String(applyPreview.target.review_head ?? '').slice(0, 12)}</code>
              </Descriptions.Item>
              <Descriptions.Item label="远端当前 head">
                {applyPreview.target.remote_head_error
                  ? <Typography.Text type="warning">无法读取（{applyPreview.target.remote_head_error}）</Typography.Text>
                  : <>{applyPreview.target.head_match
                    ? <Tag color="green">与补丁 head 一致，可应用</Tag>
                    : <Tag color="red">已变化——确认将被拒绝</Tag>}
                    <code className="mono">{String(applyPreview.target.remote_head ?? '').slice(0, 12)}</code></>}
              </Descriptions.Item>
              <Descriptions.Item label="补丁验证">
                <Space wrap size={4}>
                  <span>可应用性：</span>
                  {applyPreview.patch.applicability_state === 'pass' ? <Tag color="green">git apply --check 通过</Tag>
                    : applyPreview.patch.applicability_state === 'fail' ? <Tag color="red">未通过</Tag>
                    : <Tag>未执行</Tag>}
                  <span>修复测试：</span>
                  {applyPreview.patch.fix_test?.executed
                    ? (applyPreview.patch.fix_test.ok ? <Tag color="green">已执行并通过</Tag> : <Tag color="red">已执行未通过</Tag>)
                    : <Tag>未执行</Tag>}
                </Space>
              </Descriptions.Item>
              <Descriptions.Item label="写入身份">
                {applyPreview.robot_identity}（系统机器人，非个人账号）
              </Descriptions.Item>
              <Descriptions.Item label="动作">
                在源分支新增一个提交（包含补丁全部变更）；不合并 PR。
              </Descriptions.Item>
              {applyPreview.previous_operations?.length ? (
                <Descriptions.Item label="历史操作">
                  {applyPreview.previous_operations.map((o, i) => (
                    <div key={i} style={{ fontSize: 12 }}>
                      <Tag color={o.status === 'committed' ? 'green' : o.status === 'failed' ? 'red'
                        : o.status === 'result_uncertain' ? 'orange' : 'default'}>{o.status}</Tag>
                      {o.commit ? <code className="mono">{String(o.commit).slice(0, 12)}</code> : null}
                      {o.error_code ? <Typography.Text type="secondary">（{o.error_code}）</Typography.Text> : null}
                      {o.stale_lock ? <Tag color="orange">卡锁超 10 分钟</Tag> : null}
                      {o.reconcilable ? (
                        <Button size="small" type="link" style={{ padding: 0, marginLeft: 6 }}
                          loading={applyBusy} onClick={() => reconcileOp(o.operation_id)}>
                          读回对账
                        </Button>
                      ) : null}
                    </div>
                  ))}
                </Descriptions.Item>
              ) : null}
            </Descriptions>
            {applyPreview.patch.fix_test?.executed && !applyPreview.patch.fix_test.ok ? (
              <Alert type="warning" showIcon style={{ marginBottom: 8 }}
                message="修复测试未通过——提交前请重读验证三分说明；是否应用由你判断。" />
            ) : null}
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>{applyPreview.operation_note}</Typography.Text>
            <div style={{ marginTop: 4 }}>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>{applyPreview.rollback_note}</Typography.Text>
            </div>
          </>
        ) : null}
      </Drawer>

      {/* 合并 PR 预览（确认后才会合并） */}
      <Drawer title="合并 PR 预览（确认后才会合并）" width={640} open={Boolean(mergePreview)}
        onClose={() => setMergePreview(null)}
        footer={
          <Space>
            <Button onClick={() => setMergePreview(null)}>取消</Button>
            <Button type="primary" danger disabled={!mergePreview?.can_merge}
              loading={mergeBusy} onClick={confirmMerge}>
              确认合并 PR
            </Button>
          </Space>
        }>
        {mergePreview ? (
          <>
            <Descriptions size="small" column={1} bordered style={{ marginBottom: 10 }}>
              <Descriptions.Item label="目标">
                {mergePreview.target.provider} · {mergePreview.target.repo} · PR#{mergePreview.target.pr_number}
              </Descriptions.Item>
              <Descriptions.Item label="分支方向">
                <code className="mono">{mergePreview.target.source_branch ?? '—'}</code> →
                <code className="mono">{mergePreview.target.target_branch ?? '—'}</code>
              </Descriptions.Item>
              <Descriptions.Item label="当前 head">
                <code className="mono">{String(mergePreview.target.head ?? '').slice(0, 12)}</code>
                {mergePreview.applied_commit ? (
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    （含 MergePilot 应用提交 <code className="mono">{String(mergePreview.applied_commit).slice(0, 12)}</code>）
                  </Typography.Text>
                ) : null}
              </Descriptions.Item>
              <Descriptions.Item label="合并资格（服务端统一门，确认时重核）">
                <Space wrap size={4}>
                  {mergePreview.merge_conditions.pr_open ? <Tag color="green">PR 开放</Tag> : <Tag color="red">PR 已关闭</Tag>}
                  {mergePreview.merge_conditions.mergeable === true ? <Tag color="green">无冲突</Tag>
                    : mergePreview.merge_conditions.mergeable === false ? <Tag color="red">有冲突</Tag>
                    : <Tag color="orange">可合并性未知（不予放行）</Tag>}
                  {(() => {
                    const rv = mergePreview.merge_conditions.review;
                    const rvMap = { pass: ['green', '当前 head 审查完成（无未处理 P0/P1）'],
                      missing: ['red', '当前 head 无审查 run（旧 head 审查不可替代）'],
                      pending: ['orange', '当前 head 审查未完成'],
                      blocked_findings: ['red', '当前 head 有未处理 P0/P1 发现'] };
                    const [c, t] = rvMap[rv] ?? ['orange', `审查证据：${rv ?? '未知'}`];
                    return <Tag color={c}>{t}</Tag>;
                  })()}
                  {(() => {
                    const ci = mergePreview.merge_conditions.ci ?? {};
                    const ciMap = { pass: ['green', '真实 CI 通过'],
                      pending: ['orange', 'CI 运行中'],
                      failed: ['red', 'CI 未通过'],
                      not_configured: ['red', '该 head 无 CI 状态（未配置或未运行）'],
                      unsupported: ['orange', '平台 CI 状态不可读'],
                      unknown: ['orange', 'CI 状态读取失败'] };
                    const [c, t] = ciMap[ci.state] ?? ['orange', `CI：${ci.state ?? '未知'}`];
                    return <Tag color={c}>{t}{ci.reason ? `——${ci.reason}` : ''}</Tag>;
                  })()}
                  <Tag>保护规则：平台照常强制执行，本服务不绕过</Tag>
                </Space>
                {Array.isArray(mergePreview.blocked) && mergePreview.blocked.length ? (
                  <div style={{ marginTop: 6 }}>
                    {mergePreview.blocked.map((x, i) => (
                      <div key={i} style={{ fontSize: 12 }}>
                        <Typography.Text type="warning">· {x.detail ?? x.code}</Typography.Text>
                      </div>
                    ))}
                  </div>
                ) : null}
              </Descriptions.Item>
              <Descriptions.Item label="合并方式">
                <Select size="small" value={mergeMethod} onChange={setMergeMethod} style={{ minWidth: 160 }}
                  options={[{ value: 'merge', label: 'merge（保留完整历史）' },
                    { value: 'squash', label: 'squash（压为一个提交）' },
                    { value: 'rebase', label: 'rebase（线性追加）' }]} />
              </Descriptions.Item>
              <Descriptions.Item label="写入身份">
                {mergePreview.robot_identity}（系统机器人，非个人账号）
              </Descriptions.Item>
            </Descriptions>
            <Alert type="warning" showIcon style={{ marginBottom: 8 }}
              message="合并后代码进入目标分支；本服务无法自动撤销已发生的合并，回退需在平台上手动 revert。" />
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>{mergePreview.operation_note}</Typography.Text>
          </>
        ) : null}
      </Drawer>
    </div>
  );
}
