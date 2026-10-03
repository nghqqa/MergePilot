import React, { useCallback, useEffect, useState } from 'react';
import { Alert, Button, Popconfirm, Progress, Space, Table, Tag, Typography } from 'antd';
import { readCsrfCookie } from '../api-live.js';

// RAG 模型安装面板（RAG-model-install 波 PR4/5）：自托管 ModelScope 官方 bge-m3 安装控制面。
// 合同（任务书四节）：
//  * 展示：当前 provider / local-hash 基线 / bge-m3 状态（十态人话）/ 官方来源 URL /
//    revision / 许可证 / 文件数与总大小 / 每文件期望 sha256 /「哈希不匹配将拒绝激活」声明；
//  * 操作：安装或继续（断点续传）/ 取消 / 重新校验 / 激活（确认）/ 回退（确认）——
//    busy 防重、失败可重试、aria-live 状态播报、确认弹窗明示目标；
//  * 不显示：prompt/文档正文/模型响应/内部容器路径/token/内部工单号/虚假成功态。
//  * 写操作=platform_admin（manage_instance）；只读角色看板面无按钮。
const STATE_COPY = {
  UNINSTALLED: { text: '未安装', tone: 'default' },
  DOWNLOADING: { text: '下载中（官方通道，断点续传）', tone: 'processing' },
  VERIFYING: { text: '校验中', tone: 'processing' },
  READY: { text: '就绪（已安装未激活）', tone: 'green' },
  ACTIVE: { text: '已激活（bge-m3 生效）', tone: 'green' },
  DOWNLOAD_FAILED: { text: '下载失败（可重试续传）', tone: 'red' },
  HASH_MISMATCH: { text: '哈希不匹配（已拒绝激活）', tone: 'red' },
  INSUFFICIENT_DISK: { text: '磁盘空间不足', tone: 'red' },
  SIDECAR_START_FAILED: { text: 'sidecar 启动/探测失败', tone: 'red' },
  ACTIVATION_FAILED: { text: '激活失败', tone: 'red' },
};
const fmtBytes = (n) => n >= 1024 ** 3 ? `${(n / 1024 ** 3).toFixed(2)} GiB`
  : n >= 1024 ** 2 ? `${(n / 1024 ** 2).toFixed(1)} MiB` : `${(n / 1024).toFixed(1)} KiB`;

export default function RagModelInstallPanel({ modelKey = 'bge-m3' }) {
  const [install, setInstall] = useState(null);
  const [manifest, setManifest] = useState(null);
  const [canManage, setCanManage] = useState(false);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(null); // 'install' | 'cancel' | 'verify' | 'activate' | 'rollback'
  const [msg, setMsg] = useState(null);

  const refresh = useCallback(async () => {
    try {
      const [st, mf, sess] = await Promise.all([
        fetch(`/api/mu/rag-model/install?model_key=${encodeURIComponent(modelKey)}`, { credentials: 'same-origin' })
          .then(async (r) => ({ s: r.status, b: await r.json().catch(() => null) })),
        fetch(`/api/mu/rag-model/manifest?model_key=${encodeURIComponent(modelKey)}`, { credentials: 'same-origin' })
          .then(async (r) => ({ s: r.status, b: await r.json().catch(() => null) })),
        fetch('/api/mu/session', { credentials: 'same-origin' }).then((r) => r.json().catch(() => null)),
      ]);
      if (st.s === 200) { setInstall(st.b?.install ?? null); setErr(null); }
      else { setInstall(null); setErr(st.b?.error?.reason ?? `HTTP ${st.s}`); }
      setManifest(mf.s === 200 ? mf.b?.manifest ?? null : null);
      setCanManage((sess?.actions ?? []).includes('manage_instance'));
    } catch {
      setErr('network');
    }
  }, [modelKey]);
  useEffect(() => { refresh(); }, [refresh]);

  // 下载中轮询（2s；页面卸载自然停止）
  useEffect(() => {
    if (install?.state !== 'DOWNLOADING' && !install?.engine_busy) return undefined;
    const t = setInterval(refresh, 2000);
    return () => clearInterval(t);
  }, [install?.state, install?.engine_busy, refresh]);

  const act = async (label, path, op) => {
    setBusy(op); setMsg(null);
    try {
      const r = await fetch(path, { method: 'POST', credentials: 'same-origin',
        headers: { 'content-type': 'application/json', 'x-csrf-token': readCsrfCookie() },
        body: JSON.stringify({ model_key: modelKey }) });
      const b = await r.json().catch(() => null);
      setMsg({ ok: r.status < 300 || r.status === 202, text: `${label} → HTTP ${r.status}${b?.error?.reason ? `（${b.error.reason}${b.error.detail && typeof b.error.detail === 'string' ? '：' + b.error.detail : ''}）` : ''}` });
    } catch {
      setMsg({ ok: false, text: `${label} → 网络异常（可重试）` });
    } finally {
      setBusy(null);
      await refresh();
    }
  };

  const st = install?.state;
  const stateCopy = STATE_COPY[st] ?? { text: st ?? '不可得', tone: 'default' };
  const pct = install && Number(install.total_bytes) > 0
    ? Math.min(100, Math.round(Number(install.downloaded_bytes) / Number(install.total_bytes) * 100)) : 0;
  const isBusyEngine = install?.engine_busy || busy;

  return (
    <div className="panel" style={{ marginTop: 16 }}>
      <Typography.Title level={4} style={{ marginTop: 0 }}>RAG 模型安装（自托管 · ModelScope 官方通道）</Typography.Title>
      <Typography.Paragraph type="secondary" style={{ fontSize: 13 }}>
        从 ModelScope 官方源安装 bge-m3 嵌入模型（约 2.1 GiB，支持断点续传）；
        逐文件 sha256 校验通过才可激活——<strong>哈希不匹配将拒绝激活</strong>；
        local-hash 为始终保留的安全基线，激活后可随时回退。安装与激活全程审计。
      </Typography.Paragraph>

      <div role="status" aria-live="polite">
        {msg ? <Alert style={{ marginBottom: 10 }} type={msg.ok ? 'success' : 'warning'} showIcon message={msg.text} /> : null}
        {err ? <Alert type="warning" showIcon message="安装面板不可用"
          description={`未登录、无成员关系或多用户模式未启用（${err}）——本面板不回退演示数据。`} /> : null}
      </div>

      {install ? (
        <>
          <Space size="large" wrap style={{ marginBottom: 10 }}>
            <span>当前 provider：<Tag color={install.active_provider === modelKey ? 'green' : 'default'}>{install.active_provider}</Tag></span>
            <span>local-hash 基线：<Tag color="green">始终可用</Tag></span>
            <span>{modelKey} 状态：<Tag color={stateCopy.tone}>{stateCopy.text}</Tag></span>
            {install.last_error_code ? <span className="muted" style={{ fontSize: 12 }}>最近错误：{install.last_error_code}</span> : null}
          </Space>

          {st === 'DOWNLOADING' || install.engine_busy ? (
            <div style={{ marginBottom: 10 }}>
              <Progress percent={pct} size="small" status="active"
                format={() => `${fmtBytes(Number(install.downloaded_bytes))} / ${fmtBytes(Number(install.total_bytes))}`} />
            </div>
          ) : null}

          {manifest ? (
            <div className="table-scroll">
              <Table size="small" rowKey="path" pagination={false}
                dataSource={install.expected_files ?? manifest.files ?? []}
                columns={[
                  { title: '文件', dataIndex: 'path', render: (v) => <code style={{ fontSize: 12 }}>{v}</code> },
                  { title: '大小', dataIndex: 'bytes', width: 110,
                    render: (v) => <span style={{ fontSize: 12 }}>{fmtBytes(Number(v))}</span> },
                  { title: '期望 sha256（官方钉死）', dataIndex: 'sha256', ellipsis: true,
                    render: (v) => <code style={{ fontSize: 11 }}>{v}</code> },
                ]} />
              <p className="muted" style={{ fontSize: 12, margin: '6px 0 0' }}>
                来源：{manifest.source?.official_channel} 官方 · revision <code>{manifest.source?.files_revision?.slice(0, 12)}</code> ·
                许可证 {manifest.license} · 共 {manifest.files?.length} 个文件 / {fmtBytes(Number(manifest.total_bytes))}
                —— <code style={{ fontSize: 11 }}>modelscope.cn</code> 域名白名单外拒绝下载。
              </p>
            </div>
          ) : null}

          <Space wrap style={{ marginTop: 12 }}>
            {canManage ? (
              <>
                {['UNINSTALLED', 'DOWNLOAD_FAILED', 'HASH_MISMATCH', 'INSUFFICIENT_DISK'].includes(st) ? (
                  <Button size="small" type="primary" loading={busy === 'install'} disabled={isBusyEngine}
                    onClick={() => act('安装/继续', '/api/mu/rag-model/install', 'install')}>
                    {Number(install.downloaded_bytes) > 0 && st !== 'UNINSTALLED' ? '继续下载（断点续传）' : '安装（官方下载）'}
                  </Button>
                ) : null}
                {st === 'DOWNLOADING' ? (
                  <Popconfirm title={`取消 ${modelKey} 安装？（已下载部分保留，可稍后续传）`}
                    onConfirm={() => act('取消安装', '/api/mu/rag-model/install/cancel', 'cancel')}>
                    <Button size="small" danger loading={busy === 'cancel'} disabled={!!busy}>取消下载</Button>
                  </Popconfirm>
                ) : null}
                {['READY', 'HASH_MISMATCH', 'DOWNLOAD_FAILED', 'SIDECAR_START_FAILED'].includes(st) ? (
                  <Button size="small" loading={busy === 'verify'} disabled={isBusyEngine}
                    onClick={() => act('重新校验', '/api/mu/rag-model/install/verify', 'verify')}>重新校验文件</Button>
                ) : null}
                {st === 'READY' ? (
                  <Popconfirm title={`激活 ${modelKey}？`}
                    description="将切换 RAG 检索嵌入到 bge-m3（需 sidecar 已部署且三重门探测通过）；local-hash 可随时回退。"
                    onConfirm={() => act('激活', '/api/mu/rag-model/activate', 'activate')}>
                    <Button size="small" type="primary" loading={busy === 'activate'} disabled={!!busy}>激活 bge-m3</Button>
                  </Popconfirm>
                ) : null}
                {st === 'ACTIVE' ? (
                  <Popconfirm title="回退到 local-hash 基线？"
                    description="安装保留，可随时重新激活；回退即时生效。"
                    onConfirm={() => act('回退', '/api/mu/rag-model/rollback', 'rollback')}>
                    <Button size="small" danger loading={busy === 'rollback'} disabled={!!busy}>回退到 local-hash</Button>
                  </Popconfirm>
                ) : null}
              </>
            ) : <Tag>安装与激活需平台管理员身份（当前角色只读）</Tag>}
            <Button size="small" onClick={refresh} disabled={!!busy}>刷新</Button>
          </Space>
        </>
      ) : null}
    </div>
  );
}
