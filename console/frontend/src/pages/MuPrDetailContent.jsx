// MuPrDetailContent — PR 详情内容块（master-detail 右栏 + 独立路由页共用）。
// 数据：/api/mu/prs/:prId（编号双寻址）+ PipelinePanel（/api/mu/runs）。
// 状态：loading / not_found(404 或无权限) / error / ready（含空审查记录紧凑空态）。
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, Button, Space, Tag, Typography } from 'antd';
import { PipelinePanel } from './MultiUserPage.jsx';

/**
 * 详情内容块。props:
 *  - prRef: { repoId, owner, name, prNumber }——由调用方解析（URL/列表行）
 *  - onChanged: 操作成功后的回调（父级刷新列表）
 */
export function MuPrDetailContent({ prRef, onChanged }) {
  const [detail, setDetail] = useState(null);
  const [state, setState] = useState('loading');
  const [actionMsg, setActionMsg] = useState(null);

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
          {stageTag('验证结论', lr.verification_verdict)}
          {stageTag('测试证据', lr.tests_status)}
          {stageTag('合并资格', lr.merge_eligibility)}
          {lr.review_mode ? <span className="mu-detail-meta">模式：<Tag>{lr.review_mode === 'external_api' ? '外部 API' : lr.review_mode === 'local' ? '本地接口' : '仅证据'}</Tag></span> : null}
          {lr.architecture_version === 'v2' && Number(lr.code_egress ?? 0) > 0 ? (
            <span className="mu-detail-meta">出站：<Tag color="blue">{Number(lr.code_egress)} 次调用（审计在案）</Tag></span>
          ) : null}
          <Typography.Paragraph type="secondary" style={{ fontSize: 12, margin: 0 }}>
            四项独立判定互不冒充：protection 不明时合并资格恒为"未知"（fail-closed）；测试证据与模型判断分列。
          </Typography.Paragraph>
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
