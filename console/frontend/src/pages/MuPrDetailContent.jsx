// MuPrDetailContent — PR 详情内容块（master-detail 右栏 + 独立路由页共用）。
// 数据：/api/mu/prs/:prId（编号双寻址）+ PipelinePanel（/api/mu/runs）。
// 状态：loading / not_found(404 或无权限) / error / ready（含空审查记录紧凑空态）。
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, Button, Popconfirm, Space, Table, Tag, Typography } from 'antd';
import { PipelinePanel } from './MultiUserPage.jsx';
import { ticketMap, SEVERITY, toneToColor, unknownEntry } from '../status-map.js';

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

  const decideApproval = async (approvalId, action) => {
    setActionMsg(null);
    const csrf = (document.cookie.match(/(?:^|; )mp_csrf=([^;]*)/) ?? [])[1] ?? '';
    const r = await fetch(`/api/mu/approvals/${approvalId}/${action}`, { method: 'POST',
      credentials: 'same-origin', headers: { 'content-type': 'application/json', 'X-CSRF-Token': csrf },
      body: JSON.stringify({}) });
    const body = await r.json().catch(() => null);
    setActionMsg({ ok: r.status === 200, label: action === 'approve' ? '批准受控修复' : '拒绝修复',
      status: r.status, reason: body?.error?.reason ?? null });
    if (r.status === 200) { await load(); await loadApprovals(); onChanged?.(); }
  };

  const runAction = async (label, path, payload) => {
    setActionMsg(null);
    const csrf = (document.cookie.match(/(?:^|; )mp_csrf=([^;]*)/) ?? [])[1] ?? '';
    const r = await fetch(path, { method: 'POST', credentials: 'same-origin',
      headers: { 'content-type': 'application/json', 'X-CSRF-Token': csrf },
      body: JSON.stringify(payload ?? {}) });
    const body = await r.json().catch(() => null);
    setActionMsg({ ok: r.status === 200, label, status: r.status,
      reason: body?.error?.reason ?? null });
    if (r.status === 200) { await load(); onChanged?.(); }
  };

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
                      <Button size="small" type="primary">批准受控修复</Button>
                    </Popconfirm>
                    <Popconfirm title="拒绝修复（Fixer 永不启动，run 进入 BLOCKED）"
                      onConfirm={() => decideApproval(t.approval_id, 'reject')}>
                      <Button size="small" danger>拒绝修复</Button>
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
        <Button size="small" onClick={() => runAction('触发只读审查', `/api/mu/prs/${pr.pr_id}/review`)}>触发只读审查</Button>
        <Button size="small" onClick={() => runAction('审批通过', `/api/mu/prs/${pr.pr_id}/decision`, { action: 'approve' })}>审批通过</Button>
        <Button size="small" onClick={() => runAction('驳回', `/api/mu/prs/${pr.pr_id}/decision`, { action: 'reject' })}>驳回</Button>
        <Button size="small" onClick={() => runAction('发起受控修复', `/api/mu/prs/${pr.pr_id}/repair`)}>发起受控修复</Button>
      </Space>
      {actionMsg ? (
        <Alert className="mu-detail-actionmsg" type={actionMsg.ok ? 'success' : 'warning'} showIcon
          message={`${actionMsg.label} → HTTP ${actionMsg.status}${actionMsg.reason ? `（${actionMsg.reason}）` : ''}`} />
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
