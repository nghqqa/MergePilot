// ReviewArtifactsPanel — 审查产物面板（2026-10-13 产物交付波）。
// 数据：/api/mu/runs?repo_id → 最新 run → /api/mu/runs/:id/artifacts。
// 区块：结果概览 / 问题详情（含反馈）/ 建议补丁（diff）/ 验证记录 / 下载 / 发布到 PR。
// 状态：loading / no-run / legacy（无产物）/ ready；下载 403=read_code_content 门如实提示。
import React, { useCallback, useEffect, useState } from 'react';
import {
  Alert, Button, Collapse, Descriptions, Drawer, Input, Modal, Popconfirm,
  Select, Space, Table, Tag, Typography,
} from 'antd';

const csrfHeader = () => {
  const row = document.cookie.split(';').map((s) => s.trim()).find((s) => s.startsWith('mp_csrf='));
  return row ? { 'X-CSRF-Token': decodeURIComponent(row.slice('mp_csrf='.length)) } : {};
};
const apiFetch = (path, opts = {}) => fetch(path, { credentials: 'same-origin', ...opts });

const SEV_COLOR = { P0: 'red', P1: 'orange', P2: 'gold', P3: 'default' };
const FEEDBACK_LABEL = { confirm: '确认问题', false_positive: '误报', wont_fix: '暂不处理' };
const VERIF_TONE = { static_rules: 'blue', model_verifier: 'orange', real_tests: 'default' };

/** 统一 diff 逐文件渲染（大 diff 截断 + 展开）。 */
function DiffView({ diffText }) {
  const [expanded, setExpanded] = useState(false);
  const MAX = 4000;
  const text = String(diffText ?? '');
  if (!text) return <Typography.Text type="secondary">（无 diff 内容）</Typography.Text>;
  const files = [];
  let cur = null;
  for (const line of text.split('\n')) {
    if (line.startsWith('--- ')) cur = { header: line, lines: [] };
    else if (line.startsWith('+++ ')) { if (cur) { cur.header += ' ' + line.slice(4); files.push(cur); cur = { header: '', lines: [] }; if (files.length) files[files.length - 1].lines2 = cur.lines; } continue; }
    if (files.length) files[files.length - 1].lines.push(line);
  }
  const shown = expanded ? text : text.slice(0, MAX);
  return (
    <div className="mu-art-diff">
      {files.length ? (
        <Space wrap size={4} style={{ marginBottom: 6 }}>
          {files.map((f, i) => (
            <Tag key={i} color="blue">{String(f.header || '').replace(/^--- a\//, '').replace(/\+\+\+ b\//, '').slice(0, 60)}</Tag>
          ))}
        </Space>
      ) : null}
      <pre className="mu-art-diff-pre" style={{
        maxHeight: expanded ? 'none' : 320,
      }}>{shown}{!expanded && text.length > MAX ? '\n…（截断）' : ''}</pre>
      {text.length > MAX ? (
        <Button size="small" onClick={() => setExpanded(!expanded)}>{expanded ? '收起' : '展开完整 diff'}</Button>
      ) : null}
      <Button size="small" style={{ marginLeft: 6 }}
        onClick={() => navigator.clipboard?.writeText(text).catch(() => {})}>复制</Button>
    </div>
  );
}

export function ReviewArtifactsPanel({ repoId, prNumber, myActions = [] }) {
  const [state, setState] = useState('loading');
  const [art, setArt] = useState(null);
  const [runId, setRunId] = useState(null);
  const [fbBusy, setFbBusy] = useState(null);
  const [fbMsg, setFbMsg] = useState(null);
  const [pubPreview, setPubPreview] = useState(null); // {action, target, comment, marker, existing}
  const [pubBusy, setPubBusy] = useState(false);
  const [pubMsg, setPubMsg] = useState(null);
  const [note, setNote] = useState('');

  const canPublish = myActions.includes('decide_review');
  const canFeedback = myActions.includes('request_review') || myActions.includes('decide_review');
  const canDownload = true; // 服务端按 read_code_content 最终授权——403 如实提示

  const load = useCallback(async () => {
    if (!repoId || !prNumber) { setState('no-pr'); return; }
    setState('loading');
    try {
      const lr = await apiFetch(`/api/mu/runs?repo_id=${encodeURIComponent(repoId)}`);
      if (!lr.ok) { setState('error'); return; }
      const ld = await lr.json().catch(() => null);
      const mine = (ld?.runs ?? []).filter((r) => Number(r.provider_pr_number) === Number(prNumber));
      const latest = mine.sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)))[0];
      if (!latest) { setState('no-run'); return; }
      setRunId(latest.run_id);
      const ar = await apiFetch(`/api/mu/runs/${latest.run_id}/artifacts`);
      if (!ar.ok) { setState(ar.status === 403 ? 'forbidden' : 'error'); return; }
      const ad = await ar.json().catch(() => null);
      if (!ad) { setState('error'); return; }
      setArt(ad);
      setState('ready');
    } catch { setState('error'); }
  }, [repoId, prNumber]);

  useEffect(() => { load(); }, [load]);

  const sendFeedback = async (findingId, action) => {
    setFbBusy(findingId + action);
    try {
      const r = await apiFetch(`/api/mu/findings/${findingId}/feedback`, {
        method: 'POST', headers: { 'content-type': 'application/json', ...csrfHeader() },
        body: JSON.stringify({ action, note }),
      });
      const b = await r.json().catch(() => ({}));
      if (r.status === 403) setFbMsg({ tone: 'warning', text: '当前角色无权提交反馈（需 reviewer+）' });
      else if (!r.ok) setFbMsg({ tone: 'warning', text: `反馈未完成（HTTP ${r.status}）：${b?.error?.reason ?? ''}` });
      else {
        setFbMsg({ tone: 'success', text: `已记录：${FEEDBACK_LABEL[action]}（原始审查结论保留）` });
        setNote('');
        await load();
      }
    } catch { setFbMsg({ tone: 'warning', text: '网络异常，反馈未提交' }); }
    setFbBusy(null);
  };

  const openPreview = async () => {
    setPubBusy(true);
    try {
      const r = await apiFetch(`/api/mu/runs/${runId}/publish`);
      const b = await r.json().catch(() => ({}));
      if (!r.ok) setPubMsg({ tone: 'warning', text: `预览未完成（HTTP ${r.status}）：${b?.error?.reason ?? ''}` });
      else setPubPreview(b);
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
        await load();
      } else {
        const stale = b?.error?.stage === 'stale_head';
        setPubMsg({ tone: 'warning', text: stale
          ? 'head 已变化：该结果基于旧版本，已停止发布——请重新审查后再发布'
          : `发布未完成（HTTP ${r.status}）：${b?.error?.reason ?? ''}${b?.error?.status === 'unconfirmed' ? '（结果不明，请先查验远端再重试）' : ''}` });
        if (!stale) setPubPreview(null);
      }
    } catch { setPubMsg({ tone: 'warning', text: '网络异常，发布未完成' }); }
    setPubBusy(false);
  };

  const download = async (kind, filename) => {
    const r = await apiFetch(`/api/mu/runs/${runId}/${kind}`);
    if (r.status === 403) { setPubMsg({ tone: 'warning', text: '下载被拒：需要 read_code_content 权限（代码内容出站门）' }); return; }
    if (r.status === 404 && kind === 'patch.diff') { setPubMsg({ tone: 'warning', text: '本轮无结构化补丁可下载' }); return; }
    if (!r.ok) { setPubMsg({ tone: 'warning', text: `下载失败（HTTP ${r.status}）` }); return; }
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = filename; a.click();
    URL.revokeObjectURL(url);
  };

  if (state === 'loading') return <Alert type="info" showIcon message="审查产物：加载中…" />;
  if (state === 'no-pr' || state === 'no-run') {
    return <Alert type="info" showIcon message="审查产物：该 PR 尚无审查 run" description="发起审查后，产物（问题清单/建议补丁/验证记录/下载）会展示在这里。" />;
  }
  if (state === 'forbidden') return <Alert type="warning" showIcon message="审查产物：当前角色无权查看" />;
  if (state === 'error') return <Alert type="warning" showIcon message="审查产物：加载失败（网络或服务异常）" />;

  const findings = art?.findings ?? [];
  const outputs = art?.outputs ?? {};
  const patch = art?.patch ?? null;
  const publish = art?.publish ?? null;
  const parseState = art?.parse_state ?? {};
  const verification = art?.verification ?? [];
  const outputsMissing = !outputs.reviewer && !outputs.leader && !outputs.fixer && !outputs.verifier;

  return (
    <div className="mu-artifacts-panel" style={{ margin: '10px 0' }}>
      <Typography.Title level={5}>审查产物与交付</Typography.Title>
      {parseState.parse_failures?.length ? (
        <Alert type="warning" showIcon style={{ marginBottom: 6 }}
          message={`部分角色输出解析失败：${parseState.parse_failures.join(', ')}——原始内容已保留，不影响其余产物`} />
      ) : null}
      {outputsMissing ? (
        <Alert type="info" showIcon style={{ marginBottom: 6 }}
          message="历史产物缺失：该 run 早于产物持久化上线——如实展示既有摘要，不伪造补丁/正文" />
      ) : null}

      <Collapse ghost
        items={[
          { key: 'overview', label: '① 结果概览', children: (
            <Descriptions size="small" column={2} bordered>
              <Descriptions.Item label="审查引擎">{art?.run?.llm_mode ?? '—'}</Descriptions.Item>
              <Descriptions.Item label="run 状态">{art?.run?.status}</Descriptions.Item>
              <Descriptions.Item label="发现数">{findings.length}（P0={findings.filter((f) => f.severity === 'P0').length} / P1={findings.filter((f) => f.severity === 'P1').length}）</Descriptions.Item>
              <Descriptions.Item label="建议补丁">{patch ? `已有（触及 ${(patch.files ?? []).length} 文件）` : '无（仅文字建议或历史 run）'}</Descriptions.Item>
              <Descriptions.Item label="验证范围">{(art?.verification ?? []).map((v) => (
                <Tag key={v.kind} color={v.executed ? (VERIF_TONE[v.kind] ?? 'default') : 'default'}>
                  {v.label}{v.executed ? '' : '（未执行）'}
                </Tag>))}</Descriptions.Item>
              <Descriptions.Item label="发布状态">{publish
                ? <Tag color={publish.status === 'published' ? 'green' : publish.status === 'unconfirmed' ? 'orange' : 'red'}>{publish.status}</Tag>
                : <Tag>未发布</Tag>}</Descriptions.Item>
              <Descriptions.Item label="head"><code className="mono">{String(art?.run?.head_sha ?? '').slice(0, 12)}</code></Descriptions.Item>
              <Descriptions.Item label="口径">DRY_RUN 建议链——流程完成 ≠ 代码已修复；补丁"尚未应用"</Descriptions.Item>
            </Descriptions>
          ) },
          { key: 'findings', label: `② 问题详情（${findings.length}）`, children: (
            <>
              {findings.length ? (
                <Table size="small" pagination={{ pageSize: 10 }} rowKey="finding_id"
                  dataSource={findings} columns={[
                    { title: '级别', dataIndex: 'severity', width: 60,
                      render: (s) => <Tag color={SEV_COLOR[s] ?? 'default'}>{s}</Tag> },
                    { title: '规则', dataIndex: 'rule_id', width: 110 },
                    { title: '位置', width: 200, render: (_, f) => <code className="mono">{f.path}{f.line_start ? ':' + f.line_start : ''}</code> },
                    { title: '摘要', render: (_, f) => String(f.summary_masked ?? f.title ?? '').slice(0, 140) },
                    { title: '依据', render: (_, f) => f.evidence_ref ? <code className="mono" style={{ fontSize: 11 }}>{String(f.evidence_ref).slice(0, 40)}</code> : '—' },
                  ]} expandable={{
                    expandedRowRender: (f) => (
                      <div style={{ padding: '4px 8px' }}>
                        <div style={{ fontSize: 13 }}>
                          建议处置：{f.remediation ?? '（未提供）'}
                        </div>
                        {canFeedback ? (
                          <Space wrap style={{ marginTop: 8 }}>
                            {['confirm', 'false_positive', 'wont_fix'].map((a) => (
                              <Popconfirm key={a} title={`${FEEDBACK_LABEL[a]}？`} description="原始审查结论将保留存档"
                                onConfirm={() => sendFeedback(f.finding_id, a)}>
                                <Button size="small" loading={fbBusy === f.finding_id + a}>{FEEDBACK_LABEL[a]}</Button>
                              </Popconfirm>
                            ))}
                            <Input size="small" style={{ width: 220 }} placeholder="可选说明（≤500 字）"
                              value={note} onChange={(e) => setNote(e.target.value)} />
                          </Space>
                        ) : (
                          <div className="muted" style={{ fontSize: 12, marginTop: 6 }}>反馈需 reviewer 及以上角色。</div>
                        )}
                      </div>
                    ),
                  }} />
              ) : (
                <Typography.Text type="secondary">自动审查未发现问题——这与"解析失败/产物缺失"是不同状态（本轮真实无发现）。</Typography.Text>
              )}
              {fbMsg ? <Alert size="small" style={{ marginTop: 6 }} type={fbMsg.tone} showIcon message={fbMsg.text} /> : null}
            </>
          ) },
          { key: 'patch', label: '③ 建议补丁', children: (
            patch ? (
              <>
                <Alert type="warning" showIcon style={{ marginBottom: 6 }}
                  message="以下是建议补丁，尚未应用" description={`对应 head：${String(patch.head_sha).slice(0, 12)} · 可应用性：${JSON.stringify(patch.applicability ?? {})}（结构级检查，未执行真实 git apply）`} />
                <DiffView diffText={null} />
                <Button size="small" style={{ margin: '6px 0' }}
                  onClick={() => download('patch.diff', `mergepilot-suggest-${String(runId).slice(0, 8)}.patch`)}
                  disabled={!canDownload}>下载 .patch</Button>
                <Alert type="info" showIcon style={{ marginTop: 6 }}
                  message="补丁正文服务端保存：/api/mu/runs/:id/patch.diff（同源下载）" />
              </>
            ) : (
              <Typography.Text type="secondary">
                本轮无结构化补丁——真实原因：fixer 未输出 patch 字段（仅文字建议），或该 run 早于补丁持久化上线。不会展示伪造的补丁。
              </Typography.Text>
            )
          ) },
          { key: 'verify', label: '④ 验证记录', children: (
            <Table size="small" rowKey="kind" pagination={false}
              dataSource={verification} columns={[
                { title: '方式', dataIndex: 'label' },
                { title: '状态', dataIndex: 'executed', width: 110,
                  render: (ex) => ex ? <Tag color="green">已执行</Tag> : <Tag>未执行</Tag> },
                { title: '说明', dataIndex: 'note', render: (n) => n ?? '—' },
              ]} />
          ) },
          { key: 'download', label: '⑤ 下载', children: (
            <>
              <Space wrap>
                <Button size="small" onClick={() => download('report.md', `mergepilot-report-${String(runId).slice(0, 8)}.md`)}>Markdown 报告</Button>
                <Button size="small" onClick={() => download('report.json', `mergepilot-report-${String(runId).slice(0, 8)}.json`)}>结构化 JSON</Button>
                <Button size="small" disabled={!patch} onClick={() => download('patch.diff', `mergepilot-suggest-${String(runId).slice(0, 8)}.patch`)}>补丁 .patch{patch ? '' : '（无）'}</Button>
              </Space>
              <div className="muted" style={{ fontSize: 12, marginTop: 6 }}>
                下载经服务端权限校验（read_code_content）——无公开直链；403 时提示角色要求。
              </div>
              {pubMsg ? <Alert style={{ marginTop: 6 }} type={pubMsg.tone} showIcon message={pubMsg.text} /> : null}
            </>
          ) },
        ]} />

      {/* ── 发布到原 PR（独立用户授权动作；服务端 head 复核+幂等） ── */}
      <div style={{ marginTop: 10 }}>
        <Space wrap>
          <Button type="primary" danger={!canPublish} disabled={!canPublish || pubBusy}
            loading={pubBusy} onClick={openPreview}>
            发布审查结果到原 PR
          </Button>
          {publish?.status === 'published' ? (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              已发布（{publish.provider} PR#{publish.provider_pr_number}，{publish.updated_at}）——再次发布将原位更新同一条评论（Gitee 页面显示的是首次创建时间）。
            </Typography.Text>
          ) : null}
        </Space>
        {pubMsg ? (
          <Alert style={{ marginTop: 6 }} type={pubMsg.tone} showIcon
            message={pubMsg.text}
            action={pubMsg.url ? <Button size="small" href={pubMsg.url} target="_blank" rel="noreferrer">查看评论</Button> : undefined} />
        ) : null}
        {!canPublish ? (
          <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
            发布需要 decide_review 角色（对外写入与裁定同级）；后端为最终授权方。
          </div>
        ) : null}
      </div>

      <Drawer title="发布预览（确认后才会写出到原 PR）" width={720} open={Boolean(pubPreview)}
        onClose={() => setPubPreview(null)}
        footer={
          <Space>
            <Button onClick={() => setPubPreview(null)}>取消</Button>
            <Button type="primary" danger loading={pubBusy} onClick={confirmPublish}>
              {pubPreview?.action === 'update' ? '更新已有评论' : '确认发布到原 PR'}
            </Button>
          </Space>
        }>
        {pubPreview ? (
          <>
            <Descriptions size="small" column={1} bordered style={{ marginBottom: 10 }}>
              <Descriptions.Item label="目标">{pubPreview.target.provider} · {pubPreview.target.repo} · PR#{pubPreview.target.pr_number}</Descriptions.Item>
              <Descriptions.Item label="被审查 head"><code className="mono">{String(pubPreview.target.head).slice(0, 12)}</code></Descriptions.Item>
              <Descriptions.Item label="动作">{pubPreview.action === 'update' ? '更新已有 MergePilot 评论（不覆盖人工评论）' : '创建新评论'}</Descriptions.Item>
            </Descriptions>
            {pubPreview.existing ? null : null}
            <pre className="mu-art-diff-pre" style={{ whiteSpace: 'pre-wrap' }}>
              {pubPreview.comment}
            </pre>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              仅生成建议，未修改代码；默认不附原始源码/完整补丁/敏感原值。
            </Typography.Text>
          </>
        ) : null}
      </Drawer>
    </div>
  );
}
