import { Alert, Button, Card, Checkbox, Descriptions, Input, InputNumber, Select, Space, Table, Tag, Typography } from 'antd';
import { ReloadOutlined, SearchOutlined, ThunderboltOutlined } from '@ant-design/icons';
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { api } from '../api.js';
import { ragMap, toneToColor } from '../status-map.js';

// RAG 本地试验页（LOCAL_RAG_TRIAL）。数据 100% 来自 /api/rag-trial/*：
// 六状态如实显示（hit/empty/model_missing/index_stale/provider_unavailable/
// error + backend_not_wired）；每条命中必须携带引用，否则该行后端已丢弃——
// 本页不渲染任何无引用内容，也不把 RAG 结果表述为证据（reference_only 边界不变）。
// 状态映射走 status-map.js 独立键空间（RAG）：人话标签 + 语义色，未知枚举兜底。
// 灌入：目标摘要 + 明确确认 + 进行中禁用重复提交；失败保留输入并给出可执行建议，
// 技术路径与 HTTP 细节收进"技术详情"，不展示堆栈/密钥。

// 常见失败的人话映射（reason 为后端稳定机器码；未知时回落到 message 截断）
function ragHumanError(e) {
  const reason = String(e?.reason ?? '');
  if (e?.status === 403 || reason.includes('scope')) return '当前账户没有这个仓库的 RAG 权限（scope 门默认拒绝）。请联系管理员把仓库加入授权范围。';
  if (e?.status === 401) return '登录状态已失效——请重新登录后再试。';
  if (e?.status === 503) return '检索服务暂不可用（依赖的数据库或模型未就绪）——稍后重试。';
  if (e?.status === 400) return '请求参数不完整——请确认已选择仓库与分支后重试。';
  if (e?.code === 'NETWORK') return '无法连接控制台后端——请确认服务正在运行。';
  return `操作未完成：${String(e?.message ?? '未知错误').slice(0, 120)}`;
}

function ragRetryHint(e) {
  if (e?.status === 403 || String(e?.reason ?? '').includes('scope')) {
    return '可以做的：换一个已授权的仓库；或联系管理员把该仓库加入 RAGTRIAL_ALLOWED_SCOPES。';
  }
  if (e?.status === 503) return '可以做的：稍后重试；若持续失败请联系管理员检查服务状态。';
  return '可以做的：确认仓库与分支填写正确、该仓库已灌入过语料；重试一次；仍失败时附上下方技术详情联系管理员。';
}

// 技术详情内容：只含 HTTP 状态与响应/错误摘要（有界），不含堆栈与任何密钥材料
function techOf(e) {
  return JSON.stringify({ status: e?.status ?? null, reason: e?.reason ?? null, message: String(e?.message ?? '').slice(0, 300) }, null, 2);
}
function techOfBody(body) {
  return JSON.stringify(body, null, 2).slice(0, 2000);
}

function stateTag(state) {
  const m = ragMap(state);
  return <Tag color={toneToColor(m.tone)} title={m.note}>{m.label}</Tag>;
}

export default function RagTrialPage() {
  const [status, setStatus] = useState(null);
  const [metrics, setMetrics] = useState(null);
  const [error, setError] = useState(null);
  const [q, setQ] = useState('');
  const [repo, setRepo] = useState('');
  const [branch, setBranch] = useState('');
  const [k, setK] = useState(5);
  const [modelId, setModelId] = useState('');
  const [result, setResult] = useState(null);
  const [querying, setQuerying] = useState(false);
  const [ingesting, setIngesting] = useState(false);
  const [ingestConfirm, setIngestConfirm] = useState(false);
  const [ingestMsg, setIngestMsg] = useState(null);

  // 当前租户的已绑定仓库（优先选择器；MU 未启用/未登录时降级为自由输入，静默降级不报错）
  const [mu, setMu] = useState({ state: 'loading', tenant: null, repos: [] });
  useEffect(() => {
    let dead = false;
    (async () => {
      try {
        const s = await fetch('/api/mu/session');
        if (!s.ok) { if (!dead) setMu({ state: 'na', tenant: null, repos: [] }); return; }
        const sb = await s.json().catch(() => null);
        const r = await fetch('/api/mu/repositories');
        const rb = r.ok ? await r.json().catch(() => null) : null;
        if (dead) return;
        setMu({
          state: rb ? 'ok' : 'na',
          tenant: sb?.tenant ?? null,
          repos: (rb?.repositories ?? []).filter((x) => x.binding_id && x.installation_state === 'active'),
        });
      } catch {
        if (!dead) setMu({ state: 'na', tenant: null, repos: [] });
      }
    })();
    return () => { dead = true; };
  }, []);

  const refresh = useCallback(async () => {
    try {
      const [s, m] = await Promise.all([api.ragTrialStatus(), api.ragTrialMetrics()]);
      setStatus(s); setMetrics(m); setError(null);
    } catch (e) {
      setError(e); setStatus(null); setMetrics(null);
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const [orgQ, setOrgQ] = useState('');
  const [orgRes, setOrgRes] = useState(null);
  const [orgLoading, setOrgLoading] = useState(false);
  const runOrgSearch = async () => {
    setOrgLoading(true); setOrgRes(null);
    try { setOrgRes(await api.ragOrgSearch(orgQ)); }
    catch (e) { setOrgRes({ service_state: 'error', error: e.message }); }
    finally { setOrgLoading(false); }
  };

  const runQuery = async () => {
    setQuerying(true); setResult(null);
    try { setResult(await api.ragTrialQuery({ q, repo, branch, k, model_id: modelId || undefined })); }
    catch (e) { setResult({ service_state: 'error', error_kind: e.reason ?? 'http', error: e, human: ragHumanError(e), hint: ragRetryHint(e), tech: techOf(e) }); }
    finally { setQuerying(false); refresh(); }
  };

  const runIngest = async () => {
    setIngesting(true); setIngestMsg(null);
    try {
      const r = await api.ragTrialIngest({ repo, branch, corpus_dir: '/app/rag-corpus' });
      setIngestMsg({ ok: true, human: '灌入完成——语料已写入索引，可以发起查询验证。', tech: techOfBody(r) });
    } catch (e) {
      // 失败保留用户输入（repo/branch/model 均为受控 state，不清空）；重新要求确认
      setIngestConfirm(false);
      setIngestMsg({ ok: false, human: ragHumanError(e), hint: ragRetryHint(e), tech: techOf(e) });
    } finally { setIngesting(false); refresh(); }
  };

  const state = result?.service_state;
  const ingestReady = repo.trim() !== '' && branch.trim() !== '' && ingestConfirm && !ingesting;

  const repoOptions = useMemo(
    () => mu.repos.map((r) => ({ value: `${r.owner}/${r.name}`, branch: r.default_branch ?? '', label: `${r.owner}/${r.name}` })),
    [mu.repos]);
  const useRepoPicker = mu.state === 'ok' && repoOptions.length > 0;
  const onRepoPicked = (v) => {
    setRepo(v);
    const hit = repoOptions.find((o) => o.value === v);
    if (hit?.branch) setBranch(hit.branch);
  };
  // 选择器可用时自动选中首个绑定仓库（刷新后状态可从后端恢复）
  useEffect(() => {
    if (useRepoPicker && !repo) {
      const first = repoOptions[0];
      if (first) { setRepo(first.value); if (first.branch) setBranch(first.branch); }
    }
  }, [useRepoPicker, repo, repoOptions]);

  return (
    <div>
      <Typography.Title level={1} style={{ fontSize: 24, marginBottom: 4 }}>RAG 本地试验</Typography.Title>
      <Typography.Paragraph type="secondary">
        LOCAL_RAG_TRIAL：独立 pgvector 索引 + MinIO 原文归档。检索结果仅作<b>带引用的参考</b>，
        不构成 finding/ticket/gate/VERIFIED 输入；无引用命中在后端即被丢弃，本页不显示无来源内容。
      </Typography.Paragraph>

      <div className="panel" style={{ padding: 'var(--sp-4)', marginBottom: 16 }}>
        <Typography.Title level={3} style={{ fontSize: 16 }}>A 链 org-search × ragtrial（集成联调）</Typography.Title>
        <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
          组织知识检索的内部接线——结果一律 <b>reference_only</b> 辅助引用，
          不构成 finding/gate/ticket/VERIFIED/fixer 输入；Verifier 只接受独立测试证据。
        </Typography.Paragraph>
        <div className="rag-controls">
          <div className="rag-field rag-field-wide">
            <Input placeholder="组织知识检索词（如：回滚 锚点）" value={orgQ} aria-label="组织知识检索词"
              onChange={(e) => setOrgQ(e.target.value)} onPressEnter={runOrgSearch} />
          </div>
          <div className="rag-field-auto">
            <Button type="primary" loading={orgLoading} disabled={!orgQ} onClick={runOrgSearch}>检索组织知识</Button>
          </div>
        </div>
        {orgRes ? (
          <div style={{ marginTop: 12 }}>
            <Space size="small" wrap>
              <Tag>{orgRes.source ?? '-'}</Tag>
              <Tag color={orgRes.service_state === 'hit' ? 'green' : 'orange'}>{orgRes.service_state}</Tag>
              {orgRes.model ? <Tag color="blue">{orgRes.model.model_id}@iv{orgRes.model.index_version}</Tag> : null}
            </Space>
            {orgRes.note ? <p className="section-note" style={{ marginTop: 8 }}>{orgRes.note}</p> : null}
            {(orgRes.results ?? []).length ? (
              <ul className="compact-list" style={{ marginTop: 8 }}>
                {orgRes.results.map((r, i) => (
                  <li key={i}>
                    <Tag color="purple">reference_only</Tag>
                    <code>{r.citation.doc_path}</code> · L{r.citation.line_start}-{r.citation.line_end} · score {r.score?.toFixed(3)}
                    <div className="muted" style={{ fontSize: 12 }}>{String(r.snippet ?? '').slice(0, 120)}…</div>
                  </li>
                ))}
              </ul>
            ) : null}
          </div>
        ) : null}
      </div>

      <Alert type="info" showIcon style={{ marginBottom: 16 }}
        message="安全边界：RAG 结果 = reference only"
        description="Review 可读取引用作辅助证据；Fixer 不得只依据 RAG 文本改码；Verifier 只接受独立 harness/test 证据。" />

      {error ? (
        <Alert type="error" showIcon style={{ marginBottom: 16 }}
          message="RAG 试验服务暂不可用"
          description="后端未接线或数据库不可达时如实显示错误，不显示任何推断数据。可稍后重试，或联系管理员检查服务配置。" />
      ) : null}

      <Card title="检索" size="small" style={{ marginBottom: 16 }}>
        <div className="rag-controls">
          <div className="rag-field rag-field-wide">
            <span className="f-label">查询内容</span>
            <Input placeholder="查询（中/英）" value={q} aria-label="查询内容"
              onChange={(e) => setQ(e.target.value)} onPressEnter={runQuery} />
          </div>
          {useRepoPicker ? (
            <div className="rag-field">
              <span className="f-label">仓库（当前组织已绑定）</span>
              <Select style={{ width: '100%', minWidth: 0 }} value={repo || undefined} placeholder="选择已绑定仓库"
                aria-label="选择已绑定仓库" options={repoOptions} onChange={onRepoPicked} showSearch
                optionFilterProp="label" />
            </div>
          ) : (
            <div className="rag-field">
              <span className="f-label">仓库（owner/name）</span>
              <Input value={repo} placeholder="如 nghqqa/mergepilot" aria-label="仓库（owner/name）"
                onChange={(e) => setRepo(e.target.value)} />
            </div>
          )}
          <div className="rag-field">
            <span className="f-label">分支</span>
            <Input value={branch} placeholder="分支名" aria-label="分支"
              onChange={(e) => setBranch(e.target.value)} />
          </div>
          <div className="rag-field rag-field-auto">
            <span className="f-label">返回条数</span>
            <InputNumber min={1} max={20} value={k} onChange={(v) => setK(v || 5)} aria-label="返回条数 k" />
          </div>
          <div className="rag-field">
            <span className="f-label">模型（留空用默认）</span>
            <Input value={modelId} placeholder="默认 local-hash-v1" aria-label="模型 ID"
              onChange={(e) => setModelId(e.target.value)} />
          </div>
          <div className="rag-field-auto">
            <Button type="primary" icon={<SearchOutlined />} loading={querying}
              disabled={!q || !repo || !branch} onClick={runQuery}>查询</Button>
          </div>
        </div>
        {mu.state === 'ok' ? (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            当前组织：{mu.tenant?.slug ?? '未知'}
            {useRepoPicker ? ` · 已绑定 ${mu.repos.length} 个仓库（可从下拉选择）` : ' · 尚无已绑定仓库，可自由输入仓库与分支'}
          </Typography.Text>
        ) : mu.state === 'na' ? (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            未获取到组织会话——自由输入模式；后端仍按 scope 门校验本仓库权限。
          </Typography.Text>
        ) : null}

        {result ? (
          <div style={{ marginTop: 12 }}>
            <Space size="large" wrap style={{ marginBottom: 8 }}>
              <span>状态：{stateTag(state)}</span>
              {result.latency_ms != null ? <span>耗时 {result.latency_ms} ms</span> : null}
              {result.model_id ? <span>模型 <code>{result.model_id}</code></span> : null}
              {result.index_version != null ? <span>index_version {result.index_version}</span> : null}
              {result.score_floor != null ? <span>score≥{result.score_floor}</span> : null}
              {result.drifted_rows ? <Tag color="orange">drifted_rows={result.drifted_rows}</Tag> : null}
              {result.dropped_uncited ? <Tag color="orange">dropped_uncited={result.dropped_uncited}</Tag> : null}
              {result.note ? <Typography.Text type="secondary">{result.note}</Typography.Text> : null}
            </Space>
            {result.human ? (
              <>
                <Alert type="error" showIcon message={result.human} description={result.hint} />
                <details className="tech-details" style={{ marginTop: 8 }}>
                  <summary>技术详情（HTTP 状态与原因）</summary>
                  <pre className="evidence-pre">{result.tech}</pre>
                </details>
              </>
            ) : null}
            {(result.results ?? []).length ? (
              <div className="table-scroll" style={{ marginTop: 8 }}>
                <Table rowKey={(r) => r.citation.chunk_sha256 + r.citation.chunk_index} size="small"
                  pagination={false} scroll={{ x: true }}
                  dataSource={result.results}
                  columns={[
                    { title: 'score', dataIndex: 'score', width: 90, render: (v) => v?.toFixed(3) },
                    {
                      title: '引用（repo/branch · 文件 · 行）',
                      render: (_, r) => (
                        <Typography.Text code>
                          {r.citation.repo}/{r.citation.branch} · {r.citation.doc_path} · L{r.citation.line_start}-{r.citation.line_end}
                        </Typography.Text>
                      ),
                    },
                    {
                      title: '摘录', dataIndex: 'snippet', ellipsis: true,
                      render: (t) => <Typography.Text style={{ whiteSpace: 'pre-wrap' }}>{t}</Typography.Text>,
                    },
                    {
                      title: '绑定', width: 220,
                      render: (_, r) => (
                        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                          doc {r.citation.doc_sha256.slice(0, 8)}… chunk {r.citation.chunk_sha256.slice(0, 8)}…<br />
                          digest {r.citation.model_digest.slice(0, 8)}… iv{r.citation.index_version}
                        </Typography.Text>
                      ),
                    },
                  ]} />
              </div>
            ) : result.human ? null : (
              <Typography.Text type="secondary">（无结果——状态如实展示，未伪装）</Typography.Text>
            )}
          </div>
        ) : null}

        <div className="rag-ingest" style={{ marginTop: 16, borderTop: '1px solid var(--c-border)', paddingTop: 12 }}>
          <Typography.Title level={4} style={{ fontSize: 14, marginBottom: 6 }}>灌入语料</Typography.Title>
          <Typography.Paragraph type="secondary" style={{ marginBottom: 8 }}>
            目标：仓库 <b>{repo.trim() || '（未填）'}</b> · 分支 <b>{branch.trim() || '（未填）'}</b> · 模型 <b>{modelId.trim() || '默认 local-hash-v1'}</b>
            。语料来源：服务器上的默认语料目录（路径见技术详情）。
          </Typography.Paragraph>
          <div className="rag-controls" style={{ marginBottom: 0 }}>
            <div className="rag-field-auto">
              <Checkbox checked={ingestConfirm} disabled={ingesting || !repo.trim() || !branch.trim()}
                onChange={(e) => setIngestConfirm(e.target.checked)}>
                我确认以上灌入目标（写入索引，不影响审查结论）
              </Checkbox>
            </div>
            <div className="rag-field-auto">
              <Button type="primary" icon={<ThunderboltOutlined />} loading={ingesting}
                disabled={!ingestReady} onClick={runIngest}>灌入语料</Button>
            </div>
          </div>
          <details className="tech-details" style={{ marginTop: 8 }}>
            <summary>技术详情（灌入参数）</summary>
            <pre className="evidence-pre">{`corpus_dir: /app/rag-corpus（服务器容器内语料目录）\nendpoint:  POST /api/rag-trial/ingest（后端按 scope 门校验仓库权限）`}</pre>
          </details>
          {ingesting ? (
            <Alert style={{ marginTop: 8 }} type="info" showIcon
              message="正在灌入语料——完成后自动刷新索引状态。" />
          ) : null}
          {ingestMsg ? (
            <div style={{ marginTop: 8 }}>
              <Alert type={ingestMsg.ok ? 'success' : 'error'} showIcon
                message={ingestMsg.ok ? ingestMsg.human : `灌入未完成。${ingestMsg.human}`}
                description={ingestMsg.ok ? undefined : ingestMsg.hint} />
              <details className="tech-details" style={{ marginTop: 8 }}>
                <summary>技术详情（{ingestMsg.ok ? '灌入结果' : 'HTTP 状态与原因'}）</summary>
                <pre className="evidence-pre">{ingestMsg.tech}</pre>
              </details>
            </div>
          ) : null}
        </div>
      </Card>

      {status && status.service_state === 'backend_not_wired' ? (
        <Alert type="warning" showIcon message={ragMap('backend_not_wired').label} description={status.note} />
      ) : status && status.service_state !== undefined && status.service_state !== 'ready' ? (
        <Alert type="warning" showIcon message={ragMap(status.service_state).label}
          description={status.note ?? status.error ?? 'ragtrial 显式失败——不伪装'} />
      ) : null}

      {status?.models ? (
        <Card title="索引状态" size="small" style={{ marginBottom: 16 }}>
          <Descriptions size="small" column={{ xs: 1, sm: 2, md: 3 }}>
            <Descriptions.Item label="pgvector">
              {status.pgvector ? `${status.pgvector.extname} ${status.pgvector.extversion}` : '未安装'}
            </Descriptions.Item>
            <Descriptions.Item label="文档">{status.documents?.active} active / {status.documents?.deleted} deleted</Descriptions.Item>
            <Descriptions.Item label="chunks">{status.chunks?.total}（{status.chunks?.versions} 个 index 版本）</Descriptions.Item>
          </Descriptions>
          <div className="table-scroll">
            <Table size="small" pagination={false} rowKey="model_id" dataSource={status.models} scroll={{ x: true }}
              columns={[
                { title: 'model_id', dataIndex: 'model_id' },
                { title: 'digest', render: (_, r) => <code>{r.model_digest.slice(0, 16)}…</code> },
                { title: 'provider', dataIndex: 'provider_kind' },
                { title: 'index_version', dataIndex: 'index_version' },
                { title: 'active', dataIndex: 'active', render: (v) => <Tag color={v ? 'green' : 'default'}>{String(v)}</Tag> },
              ]} />
          </div>
        </Card>
      ) : null}

      {status?.production_readiness ? (
        <Card title="生产就绪组件（BLOCKED 即如实 BLOCKED）" size="small" style={{ marginBottom: 16 }}>
          <Space size="large" wrap>
            <span>语义 provider：<Tag color={status.production_readiness.semantic_provider?.state === 'ATTESTED' ? 'green' : 'orange'}>
              {status.production_readiness.semantic_provider?.state ?? 'UNKNOWN'}</Tag>
              {status.production_readiness.semantic_provider?.blocked_condition ? <Typography.Text type="secondary" style={{ fontSize: 12 }}>{status.production_readiness.semantic_provider.blocked_condition}</Typography.Text> : null}
            </span>
            <span>RUN_BINDING_AUTH：<Tag color={status.production_readiness.run_binding_auth?.state === 'READY' ? 'green' : 'orange'}>
              {status.production_readiness.run_binding_auth?.state ?? 'UNKNOWN'}</Tag></span>
            <span>持久队列：<Tag color="blue">{JSON.stringify(status.production_readiness.persistent_queue?.by_state ?? {})}</Tag></span>
          </Space>
        </Card>
      ) : null}

      {metrics ? (
        <Card title="指标（PG 真实推导）" size="small" extra={<Button size="small" icon={<ReloadOutlined />} onClick={refresh}>刷新</Button>}>
          <Space size="large" wrap>
            {Object.entries(metrics.queries_by_state ?? {}).map(([s, n]) => (
              <span key={s}>{stateTag(s)} × {n}</span>
            ))}
            <span>延迟 P50 {metrics.latency_ms?.p50} ms / P95 {metrics.latency_ms?.p95} ms</span>
            <span>命中 {metrics.citation?.hit_rows} 行 / 引用丢弃 {metrics.citation?.dropped_uncited_rows} 行</span>
          </Space>
          {metrics.eval_runs?.length ? (
            <div className="table-scroll">
              <Table size="small" pagination={false} style={{ marginTop: 8 }} rowKey="eval_id" scroll={{ x: true }}
                dataSource={metrics.eval_runs}
                columns={[
                  { title: 'eval', dataIndex: 'eval_id' },
                  { title: 'qa_set', dataIndex: 'qa_set' },
                  { title: 'k', dataIndex: 'k', width: 60 },
                  { title: 'Recall@K', dataIndex: 'recall_at_k', render: (v) => <Tag color="blue">{v}</Tag> },
                  { title: 'hit/total', render: (_, r) => `${r.hit_at_k}/${r.total}` },
                ]} />
            </div>
          ) : null}
          <Typography.Paragraph type="secondary" style={{ marginTop: 8, marginBottom: 0, fontSize: 12 }}>
            error 态（PG 不可达）无法写 PG 日志，由进程内存计数补齐：{JSON.stringify(metrics.in_memory_error_states ?? {})}
          </Typography.Paragraph>
        </Card>
      ) : null}
    </div>
  );
}
