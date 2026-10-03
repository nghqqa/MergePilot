// MuPrDetailContent — PR 详情内容块（master-detail 右栏 + 独立路由页共用）。
// 数据：/api/mu/prs/:prId（编号双寻址）+ PipelinePanel（/api/mu/runs）。
// 状态：loading / not_found(404 或无权限) / error / ready（含空审查记录紧凑空态）。
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, Button, Popconfirm, Space, Table, Tag, Typography } from 'antd';
import { PipelinePanel } from './MultiUserPage.jsx';
import { ticketMap, SEVERITY, toneToColor, unknownEntry } from '../status-map.js';

// ── 动作安全化（PR-5）──
// 超时上限：批准受控修复（approve）端点同步内联 fixVerifyRound（clone+test，可达分钟级）
// 给 5 分钟上限；其余快动作（job 入队/决策留痕）30 秒。超时/网络失败均提示可重试。
const ACTION_TIMEOUT_LONG_MS = 5 * 60 * 1000;
const ACTION_TIMEOUT_MS = 30 * 1000;

// 错误分形：网络失败/超时→可重试；401→登录过期；403→角色无权；409/其他→后端 note/原文如实展示
function actionFailure(status, body) {
  if (status === 401) return { tone: 'warning', text: '登录已过期，请刷新页面' };
  if (status === 403) return { tone: 'warning', text: '当前角色无权执行该动作' };
  const detail = body?.note ?? body?.error?.detail ?? body?.error?.reason ?? null;
  return { tone: 'warning',
    text: detail ? `操作未完成（HTTP ${status}）：${detail}` : `操作未完成（HTTP ${status}）` };
}

/**
 * 详情内容块。props:
 *  - prRef: { repoId, owner, name, prNumber }——由调用方解析（URL/列表行）
 *  - onChanged: 操作成功后的回调（父级刷新列表）
 */
export function MuPrDetailContent({ prRef, onChanged }) {
  const [detail, setDetail] = useState(null);
  const [state, setState] = useState('loading');
  const [actionMsg, setActionMsg] = useState(null);
  const [fixApprovals, setFixApprovals] = useState([]);

  const prNumber = prRef?.prNumber ?? null;
  const repoId = prRef?.repoId ?? null;
  // 竞态守卫（终审修正）：以 repoId|prNumber 为代际键 + mountedRef——
  // 闭包快照比对是恒等缺陷（prRef 是 useCallback 捕获的同渲染快照，永不触发）。
  // 本模式：任何 success/403/404/500/网络异常落 state 前先比对【最新】键；
  // 切换选择/换仓库/组件卸载均使旧响应失效。
  const latestKeyRef = useRef(null);
  const mountedRef = useRef(true);
  useEffect(() => {
    latestKeyRef.current = `${repoId}|${prNumber}`;
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  });
  const isStale = (myKey) => !mountedRef.current || latestKeyRef.current !== myKey;

  const load = useCallback(async () => {
    if (!prNumber || !repoId) { setState('not_found'); return; }
    const myKey = `${repoId}|${prNumber}`;
    setState('loading');
    try {
      const res = await fetch(`/api/mu/prs/${prNumber}?repo_id=${repoId}`, { credentials: 'same-origin' });
      if (isStale(myKey)) return; // 已切走/卸载——丢弃（含 403/404/500 一切分支）
      if (res.status === 404 || res.status === 403) { setState('not_found'); return; }
      if (!res.ok) { setState('error'); return; }
      const d = await res.json().catch(() => null);
      if (isStale(myKey)) return;
      if (d?.pull_request) { setDetail(d); setState('ready'); }
      else setState('not_found');
    } catch { if (!isStale(myKey)) setState('error'); }
  }, [prNumber, repoId]);

  useEffect(() => { load(); }, [load]);

  // v16 高危修复审批门：P0/P1 审批票（有票或 run WAITING 时展示面板）
  const loadApprovals = useCallback(async () => {
    if (!detail?.pull_request?.pr_id) return;
    try {
      const res = await fetch(`/api/mu/prs/${detail.pull_request.pr_id}/fix-approvals`,
        { credentials: 'same-origin' });
      if (!res.ok) { setFixApprovals([]); return; }
      const body = await res.json().catch(() => null);
      setFixApprovals(body?.fix_approvals ?? []);
    } catch { setFixApprovals([]); }
  }, [detail?.pull_request?.pr_id]);
  useEffect(() => { if (state === 'ready') loadApprovals(); }, [state, loadApprovals]);

  // ── 动作安全化（PR-5）：统一 POST 封装 ──
  // busyRef 同步锁：双击只放行一次 POST（state 更新前第二次点击也进不来）；
  // busyAction 驱动按钮 loading 文案 + disabled；成功后按既有逻辑重载详情/审批票；
  // 失败分形见 actionFailure；finally 恢复可点。
  const [busyAction, setBusyAction] = useState(null);
  const busyRef = useRef(false);
  const lastActionRef = useRef(null); // 网络失败/超时时供「重试」复用

  const postAction = useCallback(async (spec) => {
    const { key, label, path, payload, longRunning, loadingText, successText } = spec;
    if (busyRef.current) return; // 防双击重复 POST
    busyRef.current = true;
    setBusyAction(key);
    setActionMsg(null);
    lastActionRef.current = spec;
    const csrf = (document.cookie.match(/(?:^|; )mp_csrf=([^;]*)/) ?? [])[1] ?? '';
    try {
      const r = await fetch(path, { method: 'POST', credentials: 'same-origin',
        headers: { 'content-type': 'application/json', 'X-CSRF-Token': csrf },
        body: JSON.stringify(payload ?? {}),
        signal: AbortSignal.timeout(longRunning ? ACTION_TIMEOUT_LONG_MS : ACTION_TIMEOUT_MS) });
      const body = await r.json().catch(() => null);
      if (r.status === 200) {
        // 不谎称已完成：长操作成功只说明服务端已受理/已记录——结果以管线面板/记录为准
        setActionMsg({ ok: true, tone: 'success', label, note: body?.note ?? null,
          text: successText ?? '已执行成功' });
        await load();
        await loadApprovals();
        onChanged?.();
      } else {
        setActionMsg({ ok: false, label, ...actionFailure(r.status, body) });
      }
    } catch (e) {
      const timedOut = e && (e.name === 'TimeoutError' || e.name === 'AbortError');
      setActionMsg({ ok: false, label, tone: 'error', retry: true,
        text: timedOut
          ? `请求超时（${longRunning ? '5 分钟' : '30 秒'}上限）——操作可能已在服务端受理，请稍后在下方管线面板确认结果后重试`
          : '网络失败——请重试' });
    } finally {
      busyRef.current = false;
      setBusyAction(null);
    }
  }, [load, loadApprovals, onChanged]);

  const decideApproval = (approvalId, action) => {
    const approve = action === 'approve';
    return postAction({
      key: `fix-${action}-${approvalId}`, label: approve ? '批准受控修复' : '拒绝修复',
      path: `/api/mu/approvals/${approvalId}/${action}`, payload: {},
      longRunning: approve,
      loadingText: approve ? '受控修复预演进行中（可能需要数分钟）…' : '提交中…',
      successText: approve
        ? '已记录批准，受控修复预演已受理——实际进展以下方「审查管线」面板为准（DRY_RUN：不写 GitHub、不自动合并）'
        : '已记录拒绝——Fixer 不启动，run 保持 BLOCKED',
    });
  };

  const runAction = (label, path, payload, opts = {}) => postAction({ label, path, payload, ...opts });

  if (state === 'loading') {
    return <div className="mu-detail-state" role="status">正在读取 PR #{prNumber} 详情…</div>;
  }
  if (state === 'not_found') {
    return (
      <Alert type="warning" showIcon message="PR 记录不可得"
        description={`PR #${prNumber ?? '？'} 无记录或当前角色无权限（404/403 如实）——不回退其他数据源。`} />
    );
  }
  if (state === 'error') {
    return (
      <Alert type="error" showIcon message="读取失败"
        action={<Button size="small" onClick={load}>重试</Button>}
        description="网络或服务暂时不可用——可重试；持续失败请联系管理员。" />
    );
  }

  const pr = detail.pull_request;
  // PR-5 权限可见性：读后端 my_permissions.actions（authz 矩阵投影——request_review=reviewer+，
  // decide_review/request_repair=maintainer）。无权限禁用+title 说明所需角色（不隐藏，布局稳定）；
  // 后端仍为最终授权方，前端禁用只防误点。
  const myActions = detail.my_permissions?.actions ?? [];
  const canDecide = myActions.includes('decide_review');
  const ACTION_DEFS = [
    { key: 'review', label: '触发只读审查', need: 'request_review', needRole: 'reviewer 及以上角色',
      loadingText: '审查发起中…',
      desc: '触发一次只读审查：仅发起审查任务，不写 GitHub、不影响分支保护',
      run: () => runAction('触发只读审查', `/api/mu/prs/${pr.pr_id}/review`, null,
        { key: 'review', successText: '已发起只读审查（任务已排队）——进展以下方「审查管线」面板为准' }) },
    { key: 'approve', label: '审批通过', need: 'decide_review', needRole: 'maintainer 角色',
      loadingText: '提交审批中…',
      desc: '记录 approve 人工审查决策：仅结论留痕，不合并、不写 GitHub',
      run: () => runAction('审批通过', `/api/mu/prs/${pr.pr_id}/decision`, { action: 'approve' },
        { key: 'approve', successText: '已记录审批通过（见下方审查记录）——本动作不含任何合并执行' }) },
    { key: 'reject', label: '驳回', need: 'decide_review', needRole: 'maintainer 角色',
      loadingText: '提交驳回中…',
      desc: '记录 reject 人工审查决策：仅结论留痕，不合并、不写 GitHub',
      run: () => runAction('驳回', `/api/mu/prs/${pr.pr_id}/decision`, { action: 'reject' },
        { key: 'reject', successText: '已记录驳回（见下方审查记录）' }) },
    { key: 'repair', label: '发起受控修复', need: 'request_repair', needRole: 'maintainer 角色（且仓库需 active 绑定）',
      loadingText: '修复授权发起中…',
      desc: '发起 DRY_RUN 受控修复授权：不写 GitHub、不自动合并；执行前服务端再次复查',
      run: () => runAction('发起受控修复', `/api/mu/prs/${pr.pr_id}/repair`, null,
        { key: 'repair', successText: '已发起受控修复授权（DRY_RUN）——进展以下方「审查管线」面板为准' }) },
  ];
  // ADR-002 分立结果列：四域互不冒充（legacy run 零 v2 列→按"未运行"呈现，不猜值）
  const STAGE_COPY = {
    not_run: '未运行', no_blocking_findings: '无阻断发现', changes_requested: '要求修改',
    inconclusive: '不确定', passed: '通过', failed: '未通过', unavailable: '不可用',
    unknown: '未知（protection 不明）', eligible: '具备资格', ineligible: '不具备资格',
  };
  const STAGE_TONE = {
    no_blocking_findings: 'green', changes_requested: 'red', inconclusive: 'orange',
    passed: 'green', failed: 'red', unavailable: 'orange', eligible: 'green',
    ineligible: 'red', unknown: 'orange', not_run: 'default',
  };
  const lr = detail.latest_run;
  const stageTag = (label, v) => (
    <span className="mu-detail-meta">{'\u00A0'}{label}：
      <Tag color={STAGE_TONE[v] ?? 'default'}>{STAGE_COPY[v] ?? v ?? '未运行'}</Tag>
    </span>
  );
  return (
    <div className="mu-detail-body">
      <div className="mu-detail-head">
        <span className="mu-detail-title mono">
          {prRef.owner}/{prRef.name} <strong>#{pr.provider_pr_number}</strong>
        </span>
        <Space size="large" wrap>
          <span className="mu-detail-meta">head：<code className="mono">{String(pr.head_sha ?? '').slice(0, 12)}</code></span>
          <span className="mu-detail-meta">protection：
            <Tag color={pr.branch_protection_status === 'known_clean' ? 'green' : 'orange'}>{pr.branch_protection_status}</Tag>
          </span>
          {pr.title ? <span className="mu-detail-meta muted">{String(pr.title).slice(0, 60)}</span> : null}
        </Space>
      </div>

      {lr ? (
        <div className="mu-stage-verdicts" style={{ margin: '8px 0', lineHeight: 2 }}>
          {stageTag('审查结论', lr.review_verdict)}
          {stageTag('验证结论（模型域）', lr.verification_verdict)}
          {stageTag('测试证据（工具域）', lr.tests_status)}
          {stageTag('合并资格', lr.merge_eligibility)}
          {lr.model_judgment ? (
            <span className="mu-detail-meta">模型判定（原始）：
              <Tag color={lr.model_judgment.verdict === 'PASS' ? 'green' : lr.model_judgment.verdict === 'FAIL' ? 'red' : 'orange'}>
                {lr.model_judgment.verdict}
              </Tag>
              <span className="muted" style={{ fontSize: 12 }}>（输入={lr.model_judgment.input}，仅审计留痕）</span>
            </span>
          ) : null}
          {lr.test_evidence ? (
            <span className="mu-detail-meta" style={{ fontSize: 12 }}>工具证据：<code className="mono">{lr.test_evidence}</code></span>
          ) : null}
          {lr.review_mode ? <span className="mu-detail-meta">模式：<Tag>{lr.review_mode === 'external_api' ? '外部 API' : lr.review_mode === 'local' ? '本地接口' : '仅证据'}</Tag></span> : null}
          {lr.architecture_version === 'v2' && Number(lr.code_egress ?? 0) > 0 ? (
            <span className="mu-detail-meta">出站：<Tag color="blue">{Number(lr.code_egress)} 次调用（审计在案）</Tag></span>
          ) : null}
          <Typography.Paragraph type="secondary" style={{ fontSize: 12, margin: 0 }}>
            模型未读取完整 patch；Verifier 的模型结论可能为 inconclusive，工具验证结果独立有效。
            四项独立判定互不冒充：protection 不明时合并资格恒为"未知"（fail-closed）。
          </Typography.Paragraph>
        </div>
      ) : null}

      {(fixApprovals.length > 0 || lr?.status === 'WAITING_FOR_HUMAN_APPROVAL') ? (
        <div className="mu-fix-approvals" style={{ margin: '10px 0' }}>
          <Typography.Title level={5}>高危修复审批（P0/P1 逐条）</Typography.Title>
          <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
            批准仅允许生成 DRY_RUN 修复建议（不写入 GitHub、不自动合并、branch protection 保持有效）；
            修复建议仍需人工复核后才可能被应用；合并资格与分支保护状态另行独立判定，本面板不作合并结论。
          </Typography.Paragraph>
          <div className="table-scroll">
            <Table size="small" rowKey="approval_id" pagination={false}
            dataSource={fixApprovals}
            locale={{ emptyText: '审批票加载中/暂不可得——刷新重试' }}
            columns={[
              { title: '级别', dataIndex: 'severity', width: 70,
                render: (v) => {
                  const m = SEVERITY[String(v ?? '').toUpperCase()] ?? unknownEntry(v);
                  return <Tag color={toneToColor(m.tone)} title={m.note}>{m.label}</Tag>;
                } },
              { title: '发现', ellipsis: true,
                render: (_, t) => (
                  <span className="mono" style={{ fontSize: 12 }}>
                    {t.rule_id} · {t.path}{t.line_start ? `:${t.line_start}` : ''}
                    {t.summary_masked ? <span className="muted">（{t.summary_masked}）</span> : null}
                  </span>
                ) },
              { title: 'head', width: 110,
                render: (_, t) => <code className="mono">{String(t.head_sha ?? '').slice(0, 10)}</code> },
              { title: '状态', dataIndex: 'status', width: 110,
                render: (v) => {
                  // PR-2：票据状态全部走 status-map TICKET 词表（含 STALE/CONSUMED）；
                  // 未知枚举 fail-closed（unknownEntry 保留原始值），不吞键。
                  const m = ticketMap(v);
                  return (
                    <Tag style={{ whiteSpace: 'normal', height: 'auto' }}
                      color={toneToColor(m.tone)} title={m.note}>{m.label}</Tag>
                  );
                } },
              { title: '决定人/时间', width: 150,
                render: (_, t) => t.decided_by
                  ? (
                    <div style={{ fontSize: 12, lineHeight: '18px' }}>
                      <div style={{ wordBreak: 'break-all' }}>{t.decided_by}</div>
                      <div className="muted">{String(t.decided_at ?? '').slice(0, 16).replace('T', ' ')}</div>
                    </div>)
                  : <span style={{ fontSize: 12 }}>过期 {String(t.expires_at ?? '').slice(0, 16).replace('T', ' ')}</span> },
              { title: '操作', width: 190,
                render: (_, t) => t.status === 'PENDING' ? (
                  <Space size="small">
                    <Popconfirm title="批准受控修复（仅 DRY_RUN 建议——不写 GitHub、不自动合并）"
                      onConfirm={() => decideApproval(t.approval_id, 'approve')}>
                      <Button size="small" type="primary"
                        title="批准后仅生成 DRY_RUN 修复建议；服务端将同步预演（可能数分钟）——需 maintainer 角色"
                        loading={busyAction === `fix-approve-${t.approval_id}`}
                        disabled={!canDecide || busyAction !== null}>批准受控修复</Button>
                    </Popconfirm>
                    <Popconfirm title="拒绝修复（Fixer 永不启动，run 进入 BLOCKED）"
                      onConfirm={() => decideApproval(t.approval_id, 'reject')}>
                      <Button size="small" danger
                        title="拒绝该票：Fixer 不启动，run 进入 BLOCKED——需 maintainer 角色"
                        loading={busyAction === `fix-reject-${t.approval_id}`}
                        disabled={!canDecide || busyAction !== null}>拒绝修复</Button>
                    </Popconfirm>
                  </Space>
                ) : '—' },
            ]} />
          </div>
          {lr?.status === 'WAITING_FOR_HUMAN_APPROVAL' ? (
            <Alert type="warning" showIcon style={{ marginTop: 8 }}
              message="Fixer 被阻塞：等待高危修复人工审批"
              description="全部 P0/P1 票批准后才进入 DRY_RUN 修复；拒绝/过期/STALE 将阻断并保持 branch protection 有效。" />
          ) : null}
        </div>
      ) : null}

      <Space wrap className="mu-detail-actions">
        {ACTION_DEFS.map((a) => {
          const allowed = myActions.includes(a.need);
          const running = busyAction === a.key;
          const title = allowed ? a.desc : `${a.desc}——当前角色无权执行，需要${a.needRole}`;
          const btn = (
            <Button key={a.key} size="small" title={title}
              loading={running}
              disabled={!allowed || busyAction !== null}
              onClick={a.run}>
              {running ? a.loadingText : a.label}
            </Button>
          );
          // 浏览器对 disabled 按钮不出 hover title——无权限时外包一层带 title 的 span 保持可解释
          return allowed ? btn : <span key={a.key} title={title}>{btn}</span>;
        })}
      </Space>
      {actionMsg ? (
        <Alert className="mu-detail-actionmsg" type={actionMsg.tone ?? (actionMsg.ok ? 'success' : 'warning')} showIcon
          message={`${actionMsg.label}：${actionMsg.text}`}
          description={actionMsg.note ?? undefined}
          action={actionMsg.retry && lastActionRef.current ? (
            <Button size="small" onClick={() => postAction(lastActionRef.current)}>重试</Button>
          ) : undefined} />
      ) : null}

      {(detail.review_records ?? []).length > 0 ? (
        <div className="mu-detail-records">
          <Typography.Title level={5}>审查记录（人工操作）</Typography.Title>
          <ul className="mu-record-list">
            {(detail.review_records ?? []).map((rec) => (
              <li key={rec.review_id}>
                <code>{rec.kind}</code> · {rec.decision} · {rec.actor_login ?? '—'}
                · <code className="mono">{String(rec.head_sha ?? '').slice(0, 10)}</code> · {String(rec.created_at ?? '').slice(0, 19).replace('T', ' ')}
              </li>
            ))}
          </ul>
        </div>
      ) : (
        <p className="mu-empty-hint">审查记录（人工操作）暂无——上方"审批/审查"按钮的操作会记录在这里。</p>
      )}

      <PipelinePanel prNumber={Number(pr.provider_pr_number)} repoId={prRef.repoId} />
    </div>
  );
}
