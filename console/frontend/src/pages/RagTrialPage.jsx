import { Alert, Button, Card, Descriptions, Input, InputNumber, Space, Table, Tag, Typography } from 'antd';
import { ReloadOutlined, SearchOutlined, ThunderboltOutlined } from '@ant-design/icons';
import React, { useCallback, useEffect, useState } from 'react';
import { api } from '../api.js';

// RAG 本地试验页（LOCAL_RAG_TRIAL）。数据 100% 来自 /api/rag-trial/*：
// 六状态如实显示（hit/empty/model_missing/index_stale/provider_unavailable/
// error + backend_not_wired）；每条命中必须携带引用，否则该行后端已丢弃——
// 本页不渲染任何无引用内容，也不把 RAG 结果表述为证据。

const STATE_COLOR = {
  hit: 'green', empty: 'default',
  model_missing: 'orange', index_stale: 'orange',
  provider_unavailable: 'red', error: 'red',
  backend_not_wired: 'default',
};

function stateTag(state) {
  return <Tag color={STATE_COLOR[state] ?? 'red'}>{state ?? 'UNKNOWN'}</Tag>;
}

export default function RagTrialPage() {
  const [status, setStatus] = useState(null);
  const [metrics, setMetrics] = useState(null);
  const [error, setError] = useState(null);
  const [q, setQ] = useState('');
  const [repo, setRepo] = useState('nghqqa/mergepilot');
  const [branch, setBranch] = useState('feat/local-rag-trial');
  const [k, setK] = useState(5);
  const [result, setResult] = useState(null);
  const [querying, setQuerying] = useState(false);
  const [ingesting, setIngesting] = useState(false);
  const [ingestMsg, setIngestMsg] = useState(null);

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
    try { setResult(await api.ragTrialQuery({ q, repo, branch, k })); }
    catch (e) { setResult({ service_state: 'error', error_kind: e.reason ?? 'http', error: e.message }); }
    finally { setQuerying(false); refresh(); }
  };

  const runIngest = async () => {
    setIngesting(true); setIngestMsg(null);
    try { setIngestMsg(await api.ragTrialIngest({ repo, branch, corpus_dir: '/app/rag-corpus' })); }
    catch (e) { setIngestMsg({ error: String(e.message) }); }
    finally { setIngesting(false); refresh(); }
  };

  const state = result?.service_state;

  return (
    <div>
      <Typography.Title level={1} style={{ fontSize: 24, marginBottom: 4 }}>RAG 本地试验</Typography.Title>
      <Typography.Paragraph type="secondary">
        LOCAL_RAG_TRIAL：独立 pgvector 索引 + MinIO 原文归档。检索结果仅作<b>带引用的参考</b>，
        不构成 finding/ticket/gate 输入；无引用命中在后端即被丢弃，本页不显示无来源内容。
      </Typography.Paragraph>

      <div className="panel" style={{ padding: 'var(--sp-4)', marginBottom: 16 }}>
        <Typography.Title level={3} style={{ fontSize: 16 }}>A 链 org-search × ragtrial（集成联调）</Typography.Title>
        <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
          /api/rag/org-search 内部接线（MERGEPILOT_RAG_TRIAL_A_CHAIN=ragtrial）——结果一律 <b>reference_only</b> 辅助引用，
          不构成 finding/gate/ticket/fixer 输入；Verifier 只接受独立测试证据。
        </Typography.Paragraph>
        <Space.Compact style={{ width: '100%', maxWidth: 560 }}>
          <Input placeholder="组织知识检索词（如：回滚 锚点）" value={orgQ}
            onChange={(e) => setOrgQ(e.target.value)} onPressEnter={runOrgSearch} />
          <Button type="primary" loading={orgLoading} disabled={!orgQ} onClick={runOrgSearch}>org-search</Button>
        </Space.Compact>
        {orgRes ? (
          <div style={{ marginTop: 12 }}>
            <Space size="small" wrap>
              <Tag>{orgRes.source ?? '-'}</Tag>
              <Tag color={orgRes.service_state === 'ok' || orgRes.service_state === 'hit' ? 'green' : 'orange'}>{orgRes.service_state}</Tag>
              {orgRes.model ? <Tag color="blue">{orgRes.model.model_id}@iv{orgRes.model.index_version}</Tag> : null}
            </Space>
            {orgRes.note ? <p className="section-note" style={{ marginTop: 8 }}>{orgRes.note}</p> : null}
            {(orgRes.results ?? []).length ? (
              <ul className="compact-list" style={{ marginTop: 8 }}>
                {orgRes.results.map((r, i) => (
                  <li key={i}>
                    <Tag color="purple">reference_only</Tag>
                    <code>{r.citation.doc_path}</code> · L{r.citation.line_start}-{r.citation.line_end} · score {r.score?.toFixed(3)}
                    <div style={{ fontSize: 12, color: 'var(--text-secondary)' }}>{String(r.snippet ?? '').slice(0, 120)}…</div>
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
          message="ragtrial API 不可用"
          description={`${error.message} — 后端未接线或 PG 不可达时如实显示错误，不显示任何推断数据。`} />
      ) : null}

      <Card title="检索" size="small" style={{ marginBottom: 16 }}>
        <Space wrap style={{ marginBottom: 12 }}>
          <Input style={{ width: 360 }} placeholder="查询（中/英）" value={q}
            onChange={(e) => setQ(e.target.value)} onPressEnter={runQuery} />
          <Input style={{ width: 220 }} addonBefore="repo" value={repo} onChange={(e) => setRepo(e.target.value)} />
          <Input style={{ width: 220 }} addonBefore="branch" value={branch} onChange={(e) => setBranch(e.target.value)} />
          <InputNumber min={1} max={20} value={k} onChange={(v) => setK(v || 5)} addonAfter="k" />
          <Button type="primary" icon={<SearchOutlined />} loading={querying} disabled={!q} onClick={runQuery}>查询</Button>
          <Button icon={<ThunderboltOutlined />} loading={ingesting} onClick={runIngest}>灌入语料（/app/rag-corpus）</Button>
        </Space>
        {result ? (
          <div>
            <Space size="large" wrap style={{ marginBottom: 8 }}>
              <span>状态：{stateTag(state)}</span>
              {result.latency_ms != null ? <span>耗时 {result.latency_ms} ms</span> : null}
              {result.model_id ? <span>模型 <code>{result.model_id}</code></span> : null}
              {result.index_version != null ? <span>index_version {result.index_version}</span> : null}
              {result.score_floor != null ? <span>score≥{result.score_floor}</span> : null}
              {result.drifted_rows ? <Tag color="orange">drifted_rows={result.drifted_rows}</Tag> : null}
              {result.dropped_uncited ? <Tag color="orange">dropped_uncited={result.dropped_uncited}</Tag> : null}
              {result.note ? <Typography.Text type="secondary">{result.note}</Typography.Text> : null}
              {result.error ? <Typography.Text type="danger">{result.error_kind}：{String(result.error).slice(0, 120)}</Typography.Text> : null}
            </Space>
            {(result.results ?? []).length ? (
              <Table rowKey={(r) => r.citation.chunk_sha256 + r.citation.chunk_index} size="small"
                pagination={false}
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
            ) : (
              <Typography.Text type="secondary">（无结果——状态如实展示，未伪装）</Typography.Text>
            )}
          </div>
        ) : null}
        {ingestMsg ? (
          <pre style={{ marginTop: 12, background: 'var(--bg-inset, #fafafa)', padding: 8, fontSize: 12, maxHeight: 200, overflow: 'auto' }}>
            {JSON.stringify(ingestMsg, null, 2)}
          </pre>
        ) : null}
      </Card>

      {status && status.service_state === 'backend_not_wired' ? (
        <Alert type="warning" showIcon message="BACKEND_NOT_WIRED" description={status.note} />
      ) : status && status.service_state !== undefined && status.service_state !== 'ready' ? (
        <Alert type="warning" showIcon message={status.service_state}
          description={status.note ?? status.error ?? 'ragtrial 显式失败——不伪装'} />
      ) : null}

      {status?.models ? (
        <Card title="索引状态" size="small" style={{ marginBottom: 16 }}>
          <Descriptions size="small" column={3}>
            <Descriptions.Item label="pgvector">
              {status.pgvector ? `${status.pgvector.extname} ${status.pgvector.extversion}` : '未安装'}
            </Descriptions.Item>
            <Descriptions.Item label="文档">{status.documents?.active} active / {status.documents?.deleted} deleted</Descriptions.Item>
            <Descriptions.Item label="chunks">{status.chunks?.total}（{status.chunks?.versions} 个 index 版本）</Descriptions.Item>
          </Descriptions>
          <Table size="small" pagination={false} rowKey="model_id" dataSource={status.models}
            columns={[
              { title: 'model_id', dataIndex: 'model_id' },
              { title: 'digest', render: (_, r) => <code>{r.model_digest.slice(0, 16)}…</code> },
              { title: 'provider', dataIndex: 'provider_kind' },
              { title: 'index_version', dataIndex: 'index_version' },
              { title: 'active', dataIndex: 'active', render: (v) => <Tag color={v ? 'green' : 'default'}>{String(v)}</Tag> },
            ]} />
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
            <Table size="small" pagination={false} style={{ marginTop: 8 }} rowKey="eval_id"
              dataSource={metrics.eval_runs}
              columns={[
                { title: 'eval', dataIndex: 'eval_id' },
                { title: 'qa_set', dataIndex: 'qa_set' },
                { title: 'k', dataIndex: 'k', width: 60 },
                { title: 'Recall@K', dataIndex: 'recall_at_k', render: (v) => <Tag color="blue">{v}</Tag> },
                { title: 'hit/total', render: (_, r) => `${r.hit_at_k}/${r.total}` },
              ]} />
          ) : null}
          <Typography.Paragraph type="secondary" style={{ marginTop: 8, marginBottom: 0, fontSize: 12 }}>
            error 态（PG 不可达）无法写 PG 日志，由进程内存计数补齐：{JSON.stringify(metrics.in_memory_error_states ?? {})}
          </Typography.Paragraph>
        </Card>
      ) : null}
    </div>
  );
}
