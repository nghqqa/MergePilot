import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, Button, Form, Input, Modal, Popconfirm, Space, Table, Tag, Tooltip, Typography } from 'antd';
import { PlusOutlined, ReloadOutlined } from '@ant-design/icons';
import { readCsrfCookie } from '../api-live.js';

// 技能（B 波 v17 mu_skill_registry 版本治理面）：注册技能 → 发布版本（完整性指纹钉死，
// 发布后不可变）→ 激活/回滚/停用。可持续迭代 = 版本历史完整保留 + 一键回滚 + 全程审计。
// 边界如实声明：
//  * 发布=登记不可变版本；激活/回滚=切换当前生效版本；停用=暂停执行，不删除历史；
//  * 技能的「执行」发生在审查执行栈；本页只做版本治理（哪个版本生效），不执行、不下发工件；
//  * 本页与「知识库 / 知识检索（试用）」相互独立——技能治理 ≠ RAG 检索试用；
//  * Skill/RAG 调用统计与留痕不在本页（后续 C 波），本页不显示任何调用数据；
//  * 数据全部来自真实 /api/mu/skills（无 fixture 演练数据）。
// 读=任意成员；写=平台管理员（manage_instance）。发布/激活/回滚操作前均展示目标技能与版本。
const muPost = (path, payload) => fetch(path, {
  method: 'POST', credentials: 'same-origin',
  headers: { 'content-type': 'application/json', 'x-csrf-token': readCsrfCookie() },
  body: JSON.stringify(payload ?? {}),
}).then(async (r) => ({ status: r.status, body: await r.json().catch(() => null) }));

// 后端 reason 机器码 → 面向人的中文（未收录的机器码不直出，统一落到通用话术）
const REASON_MAP = {
  skill_key_exists: '该技能标识已存在（同租户内不可重复）',
  version_immutable_conflict: '该版本号已发布且指纹不同——版本一经发布不可变，请递增版本号',
  skill_not_found: '技能不存在（或不在当前租户）',
  version_not_found: '版本不存在（或尚未发布）',
  csrf_required: '安全令牌缺失或已过期——请刷新页面后重试',
  unauthorized: '未登录或会话已过期——请重新登录',
  forbidden: '当前角色无权执行该操作（写操作需平台管理员）',
  not_a_member: '不是当前租户的有效成员',
};
const reasonText = (r) => {
  const code = r?.body?.error?.reason;
  if (code == null) return '网络或服务异常，请稍后重试';
  if (REASON_MAP[code]) return REASON_MAP[code];
  if (/[^\x00-\x7F]/.test(String(code))) return String(code); // 后端已给中文可读原因（如 400 格式校验）
  return `请求失败（${code}）`;
};

export default function SkillsPage() {
  const [skills, setSkills] = useState(null);
  const [err, setErr] = useState(null);
  const [msg, setMsg] = useState(null);
  const [canManage, setCanManage] = useState(false);
  const [regOpen, setRegOpen] = useState(false);
  const [verSkill, setVerSkill] = useState(null); // 当前发布版本的技能
  const [verList, setVerList] = useState(null);   // 版本历史弹窗
  const [histErr, setHistErr] = useState(false);  // 版本历史加载失败（可重试，不永久「加载中」）
  const [histSkill, setHistSkill] = useState(null);
  const [busy, setBusy] = useState(null);         // 在飞动作（UI loading 展示）
  const busyRef = useRef(false);                  // 同步防重入（state 异步，双击同帧拦不住）
  const [form] = Form.useForm();
  const [verForm] = Form.useForm();

  const refresh = useCallback(async () => {
    try {
      const [sk, sess] = await Promise.all([
        fetch('/api/mu/skills', { credentials: 'same-origin' }).then(async (r) => ({ status: r.status, body: await r.json().catch(() => null) })),
        fetch('/api/mu/session', { credentials: 'same-origin' }).then((r) => r.json().catch(() => null)),
      ]);
      if (sk.status === 200) { setSkills(sk.body?.skills ?? []); setErr(null); }
      else { setSkills([]); setErr(sk.body?.error?.reason ?? `HTTP ${sk.status}`); }
      setCanManage((sess?.actions ?? []).includes('manage_instance'));
    } catch { setSkills([]); setErr('network'); }
  }, []);
  useEffect(() => { refresh(); }, [refresh]);

  const register = async () => {
    let v;
    try { v = await form.validateFields(); } catch { return; } // 校验失败：内联提示，不发请求
    if (busyRef.current) return; // 校验 await 期间的同帧双击在此收口
    busyRef.current = true; setBusy('register');
    try {
      const r = await muPost('/api/mu/skills', v);
      if (r.status === 200) { setRegOpen(false); form.resetFields(); setMsg({ type: 'success', text: `技能 ${v.skill_key} 已注册` }); await refresh(); }
      else setMsg({ type: 'error', text: `注册失败：${reasonText(r)}` });
    } catch { setMsg({ type: 'error', text: '注册失败：网络错误，请重试' }); }
    finally { busyRef.current = false; setBusy(null); }
  };
  const publish = async () => {
    if (!verSkill) return;
    let v;
    try { v = await verForm.validateFields(); } catch { return; }
    if (busyRef.current) return;
    busyRef.current = true; setBusy('publish');
    const target = verSkill; // 快照（关弹窗后仍用于「前往版本历史激活」）
    try {
      const r = await muPost(`/api/mu/skills/${encodeURIComponent(target.skill_key)}/versions`, v);
      if (r.status === 200) {
        const cur = r.body?.current_version ?? null;
        setVerSkill(null); verForm.resetFields();
        // 是否自动激活以服务端响应真值为准（current_version/activated），不用打开弹窗时的过期快照推断
        if (r.body?.idempotent) {
          setMsg({ type: 'info', text: `版本 ${v.version} 此前已发布（同指纹幂等，未重复写入）` });
        } else if (r.body?.activated) {
          setMsg({ type: 'success', text: `版本 ${v.version} 已发布并激活为当前版本（当前生效 v${cur}）` });
        } else {
          setMsg({ type: 'success',
            text: `版本 ${v.version} 已发布（未自动切换——当前生效 v${cur ?? '—'}）`,
            hist: { ...target, current_version: cur } });
        }
        await refresh();
      } else setMsg({ type: 'error', text: `发布失败：${reasonText(r)}` });
    } catch { setMsg({ type: 'error', text: '发布失败：网络错误，请重试' }); }
    finally { busyRef.current = false; setBusy(null); }
  };
  const activate = async (skill, version) => {
    if (busyRef.current) return;
    busyRef.current = true; setBusy(`activate:${skill.skill_key}:${version}`);
    let r = null;
    try {
      r = await muPost(`/api/mu/skills/${encodeURIComponent(skill.skill_key)}/activate`, { version });
      if (r.status === 200) {
        if (r.body?.idempotent) setMsg({ type: 'info', text: `版本 ${version} 已是当前生效版本（幂等，未变更）` });
        else setMsg({ type: 'success', text: r.body?.rollback ? `已回滚到 ${version}——当前生效版本已切换` : `已激活 ${version}——当前生效版本已切换` });
      } else setMsg({ type: 'error', text: `激活失败：${reasonText(r)}` });
    } catch { setMsg({ type: 'error', text: '激活失败：网络错误，请重试' }); }
    finally { busyRef.current = false; setBusy(null); }
    await refresh();
    // 回滚/激活后立即刷新——当前生效版本随刷新更新（下方列表与历史弹窗同步）
    if (histSkill?.skill_key === skill.skill_key) await showHistory({ ...skill, current_version: r?.body?.current_version ?? skill.current_version });
  };
  const toggle = async (skill) => {
    if (busyRef.current) return;
    busyRef.current = true; setBusy(`toggle:${skill.skill_key}`);
    const action = skill.state === 'active' ? 'disable' : 'enable';
    const verb = action === 'disable' ? '停用' : '启用';
    try {
      const r = await muPost(`/api/mu/skills/${encodeURIComponent(skill.skill_key)}/${action}`);
      if (r.status === 200) setMsg({ type: 'success', text: `${skill.display_name} 已${verb}${r.body?.idempotent ? '（状态未变，幂等）' : ''}` });
      else setMsg({ type: 'error', text: `${verb}失败：${reasonText(r)}` });
    } catch { setMsg({ type: 'error', text: `${verb}失败：网络错误，请重试` }); }
    finally { busyRef.current = false; setBusy(null); }
    await refresh();
  };
  const showHistory = async (skill) => {
    setHistSkill(skill);
    setVerList(null);
    setHistErr(false);
    try {
      const r = await fetch(`/api/mu/skills/${encodeURIComponent(skill.skill_key)}/versions`, { credentials: 'same-origin' })
        .then((x) => x.json().catch(() => null));
      if (r && Array.isArray(r.versions)) setVerList(r.versions);
      else setHistErr(true);
    } catch { setHistErr(true); }
  };

  return (
    <div>
      <Typography.Title level={1} style={{ fontSize: 24, marginBottom: 4 }}>技能</Typography.Title>
      <Typography.Paragraph type="secondary">
        技能版本治理：发布=登记不可变版本（一经发布不可变）；激活/回滚=切换当前生效版本；停用=暂停执行，不删除历史。
        技能执行发生在审查执行栈，本页决定「哪个版本生效」。
        与「知识库 / 知识检索（试用）」相互独立；调用统计与留痕不在本页。
      </Typography.Paragraph>
      <Space style={{ marginBottom: 12 }} wrap>
        <Button size="small" icon={<ReloadOutlined />} onClick={refresh}>刷新</Button>
        {canManage ? <Button size="small" type="primary" icon={<PlusOutlined />} onClick={() => setRegOpen(true)}>注册技能</Button>
          : <Tag>管理需平台管理员身份（当前角色只读）</Tag>}
      </Space>
      {/* 结果反馈常驻播报容器：role=status + aria-live=polite（读屏可感知，成功/失败一致） */}
      <div role="status" aria-live="polite">
        {msg ? (
          <Alert style={{ marginBottom: 12 }} type={msg.type} showIcon message={msg.text}
            action={msg.hist ? <Button size="small" onClick={() => showHistory(msg.hist)}>前往版本历史激活</Button> : null} />
        ) : null}
      </div>
      {err ? <Alert type="warning" showIcon message="技能列表不可用"
          description={`未登录、无成员关系或多用户模式未启用（${err}）——本页不回退演示数据。`} />
        : skills === null ? <span>加载中…</span>
        : skills.length === 0 ? <Alert type="info" showIcon message="暂无注册技能"
            description={canManage ? '点击「注册技能」添加第一个技能（如 rag.retrieve 审查检索技能）。' : '请联系平台管理员注册。'} />
        : (
          <div className="table-scroll">
            <Table rowKey="skill_key" size="small" dataSource={skills}
              columns={[
                { title: '技能', render: (_, s) => (<Space direction="vertical" size={0}>
                    <b>{s.display_name}</b><code style={{ fontSize: 12 }}>{s.skill_key}</code></Space>) },
                { title: '说明', dataIndex: 'description', ellipsis: true },
                { title: '当前版本', dataIndex: 'current_version',
                  render: (v) => v ? <Tag color="green">v{v}</Tag> : <Tag>未发布</Tag> },
                { title: '版本数', dataIndex: 'version_count', width: 80 },
                { title: '状态', dataIndex: 'state', width: 80,
                  render: (v) => <Tooltip title={v === 'active'
                    ? '启用=审查执行栈可调用该技能。停用=暂停执行，不删除历史。'
                    : '停用=审查执行栈不再调用；当前生效版本保留，重新启用即恢复。'}>
                    <Tag color={v === 'active' ? 'green' : 'red'}>{v === 'active' ? '启用' : '停用'}</Tag>
                  </Tooltip> },
                { title: '更新时间', dataIndex: 'updated_at', width: 150,
                  render: (v) => <span style={{ fontSize: 12 }}>{String(v ?? '').slice(0, 16).replace('T', ' ')}</span> },
                ...(canManage ? [{ title: '操作', width: 230, render: (_, s) => (
                  <Space size={4} wrap>
                    <Button size="small" disabled={busy !== null}
                      onClick={() => { setVerSkill(s); verForm.resetFields(); }}>发布新版本</Button>
                    <Button size="small" onClick={() => showHistory(s)}>版本历史</Button>
                    <Popconfirm title={`${s.state === 'active' ? '停用' : '启用'}技能 ${s.display_name}（${s.skill_key}）？`}
                      okText="确定" cancelText="取消"
                      okButtonProps={{ disabled: busy !== null }}
                      onConfirm={() => toggle(s)}>
                      <Button size="small" danger={s.state === 'active'}>{s.state === 'active' ? '停用' : '启用'}</Button>
                    </Popconfirm>
                  </Space>) }] : [{ title: '操作', width: 100, render: (_, s) => (
                  <Button size="small" onClick={() => showHistory(s)}>版本历史</Button>) }]),
              ]} />
          </div>
        )}

      <Modal title="注册技能" open={regOpen} onCancel={() => { if (busy === null) setRegOpen(false); }}
        onOk={register} okText="注册" cancelText="取消" confirmLoading={busy !== null}>
        <Form form={form} layout="vertical">
          <Form.Item name="skill_key" label="技能标识（小写字母/数字/._-）"
            rules={[{ required: true }, { pattern: /^[a-z0-9][a-z0-9._-]{1,63}$/, message: '格式：如 rag.retrieve' }]}>
            <Input placeholder="rag.retrieve" />
          </Form.Item>
          <Form.Item name="display_name" label="显示名" rules={[{ required: true }]}>
            <Input placeholder="审查检索技能" />
          </Form.Item>
          <Form.Item name="description" label="说明"><Input.TextArea rows={2} placeholder="该技能做什么（可后补）" /></Form.Item>
        </Form>
      </Modal>

      <Modal title={`发布新版本 · ${verSkill?.display_name ?? ''}（${verSkill?.skill_key ?? ''}）`} open={!!verSkill}
        onCancel={() => { if (busy === null) setVerSkill(null); }} onOk={publish} okText="发布" cancelText="取消"
        confirmLoading={busy !== null}>
        <Form form={verForm} layout="vertical">
          <Form.Item name="version" label="版本号（语义化，如 1.0.1）"
            rules={[{ required: true }, { pattern: /^\d+\.\d+\.\d+$/, message: '格式：主.次.补丁，如 1.0.1' }]}>
            <Input placeholder="1.0.1" />
          </Form.Item>
          <Form.Item name="changelog" label="本次改了什么" rules={[{ required: true }]}>
            <Input.TextArea rows={3} placeholder="例：改进了检索排序，修复空查询崩溃" />
          </Form.Item>
          <Form.Item name="manifest_sha256" label="技能包完整性指纹（由构建工具生成，用于防篡改核对；非密钥）"
            rules={[{ required: true }, { pattern: /^[0-9a-f]{64}$/, message: '须为 64 位十六进制（sha256）' }]}
            extra={<Typography.Text type="secondary" style={{ fontSize: 12 }}>64 位十六进制（sha256），一经发布不可变。</Typography.Text>}>
            <Input placeholder="9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08" />
          </Form.Item>
          <Form.Item name="artifact_ref" label="工件位置（引用，可选）">
            <Input placeholder="skills/rag-retrieve/1.0.1/" />
          </Form.Item>
          <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 0 }}>
            版本一经发布不可修改（同版本号不同指纹将被拒绝）；发布只登记版本——首版自动生效，其余需在「版本历史」中手动激活。
          </Typography.Paragraph>
        </Form>
      </Modal>

      <Modal title={`版本历史 · ${histSkill?.display_name ?? ''}（${histSkill?.skill_key ?? ''}）`} open={!!histSkill}
        onCancel={() => { setHistSkill(null); setVerList(null); setHistErr(false); }} footer={null} width={760}>
        {histErr ? <Alert type="error" showIcon message="版本历史加载失败"
            action={<Button size="small" onClick={() => histSkill && showHistory(histSkill)}>重试</Button>} />
        : verList === null ? <span>加载中…</span> : verList.length === 0 ? <Alert type="info" showIcon message="尚未发布任何版本" />
        : (
          <>
            <div className="table-scroll">
              <Table rowKey="version" size="small" pagination={false} dataSource={verList}
                rowClassName={(r) => (histSkill?.current_version === r.version ? 'ant-table-row-selected' : '')}
                columns={[
                  { title: '版本', dataIndex: 'version',
                    render: (v) => <Space><b>v{v}</b>{histSkill?.current_version === v ? <Tag color="green">当前生效</Tag> : null}</Space> },
                  { title: '改动说明', dataIndex: 'changelog', ellipsis: true },
                  { title: '发布者', dataIndex: 'published_by', width: 120,
                    render: (v) => <span style={{ fontSize: 12 }}>{v ?? '—'}</span> },
                  { title: '指纹', dataIndex: 'manifest_sha256', width: 110,
                    render: (v) => <code style={{ fontSize: 11 }}>{String(v ?? '').slice(0, 12)}…</code> },
                  { title: '发布时间', dataIndex: 'created_at', width: 140,
                    render: (v) => <span style={{ fontSize: 12 }}>{String(v ?? '').slice(0, 19).replace('T', ' ')}</span> },
                  ...(canManage && histSkill ? [{ title: '操作', width: 110, render: (_, r) => (
                      histSkill.current_version === r.version ? null : (
                        <Popconfirm title={`切换 ${histSkill.skill_key} 到 v${r.version}？`}
                          description={histSkill.current_version ? `当前生效 v${histSkill.current_version}——将回滚到 v${r.version}` : `将激活 v${r.version}`}
                          okText="确定" cancelText="取消"
                          okButtonProps={{ disabled: busy !== null }}
                          onConfirm={() => activate(histSkill, r.version)}>
                          <Button size="small">{histSkill.current_version ? '回滚到此' : '激活'}</Button>
                        </Popconfirm>) ) }] : []),
                ]} />
            </div>
            <Typography.Paragraph type="secondary" style={{ fontSize: 12, margin: '8px 0 0' }}>
              「激活/回滚」只切换当前生效版本（版本历史永不改写）；「启用/停用」控制技能整体是否被审查执行栈调用，与版本无关。
            </Typography.Paragraph>
          </>
        )}
      </Modal>
    </div>
  );
}
