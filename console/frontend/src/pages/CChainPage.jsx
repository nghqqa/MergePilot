import { Alert, Button, Space, Table, Tag, Typography } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import React, { useCallback, useEffect, useState } from 'react';
import { api } from '../api.js';
import { cchainMap, cchainOverallMap, toneToColor } from '../status-map.js';

// C 链状态面板（B 轮接线 feat/core-b-parallel）。
// 数据 100% 来自 /api/cchain/status（真实接口层）：缺真实依赖时显示 BLOCKED 与
// 具体阻塞条件——前端不做任何"猜测可用"或 fixture 冒充。
// 状态映射走 status-map.js 独立键空间（CCHAIN/CCHAIN_OVERALL）：人话标签 + 语义色，
// 未知枚举兜底显示原始值并标注"未知状态"。

function stateTag(state) {
  const m = cchainMap(state);
  return <Tag color={toneToColor(m.tone)} title={m.note}>{m.label}</Tag>;
}

function overallTag(overall) {
  const m = cchainOverallMap(overall);
  return <Tag color={toneToColor(m.tone)} title={m.note}>{m.label}</Tag>;
}

export default function CChainPage() {
  const [status, setStatus] = useState(null);
  const [metrics, setMetrics] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [s, m] = await Promise.all([api.cchainStatus(), api.cchainMetrics()]);
      setStatus(s); setMetrics(m);
    } catch (e) {
      setError(e); setStatus(null); setMetrics(null);
    } finally { setLoading(false); }
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 30_000); // 30s 轮询；后端为真实探测（无缓存伪装）
    return () => clearInterval(t);
  }, [refresh]);

  const overallOk = status?.overall === 'READY';

  return (
    <div>
      <Typography.Title level={1} style={{ fontSize: 24, marginBottom: 4 }}>签名验证状态</Typography.Title>
      <Typography.Paragraph type="secondary">
        合并凭证的三道校验：模型缓存校验 / 修复方在线公证 / 修复凭证密钥分发——
        三项均为<b>真实探测</b>：缺依赖即显示"已阻断"与原因，不伪装可用。
      </Typography.Paragraph>

      {error ? (
        <Alert type="error" showIcon
          message="签名验证状态不可用"
          description={`${error.message}${error.reason ? `（${error.reason}）` : ''} — 后端未接通时此页面不显示任何推断数据。`} />
      ) : null}

      {status ? (
        <>
          <div className="panel" style={{ padding: 'var(--sp-4)', marginBottom: 16 }}>
            <Space size="large" wrap>
              <span>总体：{overallTag(status.overall)}</span>
              <span>强制校验：<Tag color={status.enforce?.flag ? 'orange' : 'default'}>
                {status.enforce?.flag ? '开启（自动修复前必须通过就绪校验）' : '关闭'}</Tag></span>
              {status.audit_note?.written === false && status.audit_note?.error ? (
                <span><Tag color="orange">审计未落库</Tag>{String(status.audit_note.error).slice(0, 80)}</span>
              ) : null}
            </Space>
            {!overallOk && (status.blocked_conditions ?? []).length > 0 ? (
              <ul className="compact-list" style={{ marginTop: 12, marginBottom: 0 }}>
                {status.blocked_conditions.map((c) => <li key={c}><code>{c}</code></li>)}
              </ul>
            ) : null}
          </div>

          <Table
            rowKey="component" size="small" pagination={false}
            dataSource={status.components ?? []}
            columns={[
              { title: '组件', dataIndex: 'component', render: (v) => <code>{v}</code> },
              { title: '状态', dataIndex: 'state', render: stateTag },
              { title: '说明 / 阻塞条件', render: (_, r) => r.blocked_condition ?? r.blocked_reason
                  ?? (r.state === 'READY' || r.state === 'ATTESTED' ? '—' : '未知') },
            ]}
          />
        </>
      ) : null}

      {metrics ? (
        <section className="section" style={{ marginTop: 24 }}>
          <div className="section-head"><h3>指标（进程内存计数，重启归零）</h3></div>
          <ul className="compact-list">
            <li>修复凭证校验通过：<code>{metrics.counters?.run_binding_verify_ok ?? 0}</code>；
                拒绝：<code>{metrics.counters?.run_binding_verify_denied ?? 0}</code>；
                密钥轮换：<code>{metrics.counters?.key_rotations ?? 0}</code></li>
            <li>就绪度（1/0，最近一次观测）：
                model_cache=<code>{metrics.gauges?.cchain_model_cache_ready ?? 'null'}</code>、
                provider=<code>{metrics.gauges?.cchain_provider_attested ?? 'null'}</code>、
                run_binding=<code>{metrics.gauges?.cchain_run_binding_ready ?? 'null'}</code>、
                overall=<code>{metrics.gauges?.cchain_overall_ready ?? 'null'}</code></li>
          </ul>
        </section>
      ) : null}

      <div style={{ marginTop: 16 }}>
        <Button icon={<ReloadOutlined />} loading={loading} onClick={refresh}>立即刷新</Button>
      </div>
    </div>
  );
}
