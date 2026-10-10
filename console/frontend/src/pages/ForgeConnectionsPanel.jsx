// console/frontend/src/pages/ForgeConnectionsPanel.jsx — Gitee 仓库连接管理面板（Gitee 首版 G-4）。
// 职责：连接列表（状态/实例/凭据引用）、创建登记、probe 实测、撤销。
// 纪律：
//  * 不显示令牌值——列表只含 credential_ref 引用与 credential_present 布尔；
//  * 状态语义准确：pending=待 probe 验证；valid=授权已验证；denied=无权限；
//    unreachable=暂不可达；revoked=已撤销（恢复须重新登记——probe 不复活 revoked）；
//  * 仓库接入可用 ≠ Gitee 登录可用（本面板只管仓库连接，不涉及登录方式）。
import React, { useCallback, useEffect, useState } from 'react';
import { Button, Typography } from 'antd';

const STATUS_LABEL = {
  pending: '待验证（probe 后生效）',
  valid: '有效（授权已验证）',
  denied: '无权限（令牌被拒）',
  expired_revoked: '已过期/被撤销（平台侧）',
  unreachable: '暂不可达（网络/超时）',
  revoked: '已撤销（重登记后可用）',
};

async function forgeApi(p, { method = 'GET', body = null, csrf = null } = {}) {
  const res = await fetch(p, {
    method,
    credentials: 'same-origin',
    headers: { ...(csrf ? { 'x-csrf-token': csrf } : {}),
      ...(body ? { 'content-type': 'application/json' } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  let json = null; try { json = await res.json(); } catch { /* */ }
  return { status: res.status, json };
}

function readCsrf() {
  return document.cookie.match(/(?:^|;\s*)mp_csrf=([^;,]+)/)?.[1] ?? null;
}

export default function ForgeConnectionsPanel() {
  const [rows, setRows] = useState(null);
  const [msg, setMsg] = useState(null); // {kind:'ok'|'err', text}
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    const r = await forgeApi('/api/mu/forge/connections');
    setRows(r.status === 200 ? (r.json?.connections ?? []) : null);
    if (r.status !== 200) setMsg({ kind: 'err', text: `连接列表加载失败（${r.status}）` });
  }, []);
  useEffect(() => { load(); }, [load]);

  const act = async (fn, okText) => {
    setBusy(true); setMsg(null);
    const r = await fn().catch((e) => ({ status: 0, json: { error: { reason: String(e) } } }));
    setBusy(false);
    if (r.status === 200 || r.status === 409 /* 业务态如实展示 */) {
      setMsg({ kind: r.status === 200 ? 'ok' : 'err',
        text: r.status === 200 ? okText : (r.json?.error?.message ?? r.json?.error?.reason ?? '操作未完成') });
    } else {
      setMsg({ kind: 'err', text: r.json?.error?.reason === 'action_not_granted'
        ? '无权限（该操作需平台管理员）' : (r.json?.error?.reason ?? `失败（${r.status}）`) });
    }
    await load();
  };

  const csrf = readCsrf();

  return (
    <section className="section">
      <div className="section-head"><h3>Gitee 仓库连接</h3></div>
      <Typography.Paragraph type="secondary" style={{ fontSize: 13, marginBottom: 8 }}>
        仓库接入的凭据经部署环境注入（本页不显示、不接收令牌值）；
        「验证」以 Gitee API 实测为准——凭据已配置不等于授权可用。
        仓库接入可用不等于 Gitee 登录可用。
      </Typography.Paragraph>
      {rows === null ? (
        <p className="section-note">连接列表不可用（未登录或非 multiuser 模式）。</p>
      ) : rows.length === 0 ? (
        <p className="section-note">尚无连接——点击「登记 Gitee 连接」开始（登记后须验证生效）。</p>
      ) : (
        <div className="kv-grid">
          {rows.map((c) => (
            <div className="kv" key={c.connection_id} style={{ gridColumn: '1 / -1' }}>
              <div className="kv-label mono">{c.instance_id} · {c.forge_kind}</div>
              <div className="kv-value">
                {STATUS_LABEL[c.status] ?? c.status}
                {c.status_reason ? `（${c.status_reason}）` : ''}
                {' · '}凭据引用 <code>{c.credential_ref}</code>
                {c.credential_present === false ? '（部署 env 未配置）' : ''}
                {' · '}验真模式 {c.webhook_mode}
                {c.verified_at ? ` · 验证于 ${new Date(c.verified_at).toLocaleString()}` : ''}
              </div>
              <div style={{ display: 'flex', gap: 8, marginTop: 6, flexWrap: 'wrap' }}>
                <button type="button" className="btn btn-sm" disabled={busy || c.status === 'revoked'}
                  onClick={() => act(() => forgeApi(`/api/mu/forge/connections/${c.connection_id}/probe`,
                    { method: 'POST', csrf }), '已验证（以实测结果为准）')}>
                  验证
                </button>
                <button type="button" className="btn btn-sm" disabled={busy || c.status === 'revoked'}
                  onClick={() => act(() => forgeApi(`/api/mu/forge/connections/${c.connection_id}/revoke`,
                    { method: 'POST', csrf }), '已撤销（重登记后可恢复）')}>
                  撤销
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
      <div style={{ marginTop: 10, display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
        <button type="button" className="btn btn-sm" disabled={busy}
          onClick={() => act(() => forgeApi('/api/mu/forge/connections',
            { method: 'POST', csrf, body: { instance_id: 'gitee-cloud', webhook_mode: 'signature' } }),
            '连接已登记（待验证）')}>
          登记 Gitee 连接
        </button>
        {msg ? (
          <Typography.Text type={msg.kind === 'ok' ? 'success' : 'danger'} style={{ fontSize: 13 }}>
            {msg.text}
          </Typography.Text>
        ) : null}
      </div>
      <p className="section-note" style={{ marginTop: 8 }}>
        首版边界：Gitee 云端仓库只读接入（审查+dry-run）；检查与保护读取本接入未提供
        （合并资格按未知 fail-closed，不构成「未受保护」结论）；不提供评论/状态写回与补丁推送。
      </p>
    </section>
  );
}
