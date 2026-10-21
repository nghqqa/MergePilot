import React from 'react';
import { Table, Tag, Tooltip, Typography } from 'antd';
import { RBAC_ROLES, RBAC_ACTIONS, RBAC_GRANTS, RBAC_ROLE_NOTES } from '../rbac-matrix.js';

const ROLE_LABEL = {
  contributor: 'contributor', reviewer: 'reviewer', maintainer: 'maintainer',
  platform_admin: 'platform_admin', auditor: 'auditor',
};

// 角色权限矩阵（静态展示面）——数据来自 src/rbac-matrix.js，与后端 authz.mjs
// 由契约测试逐项锁定。仅回答"谁默认能做什么"；当前登录者的实际动作面以
// /api/mu/session 返回的 actions 为准（页头已按其渲染）。
export function RoleMatrixPanel() {
  const columns = [
    { title: '动作', dataIndex: 'label', width: 210,
      render: (v, r) => (
        <span>
          {v}
          {r.binding
            ? <Tooltip title="还需仓库处于 active Binding（GitHub App 授权连接有效）">
                <Tag style={{ marginLeft: 6 }}>需绑定</Tag>
              </Tooltip>
            : null}
        </span>
      ) },
    ...RBAC_ROLES.map((role) => ({
      title: ROLE_LABEL[role], dataIndex: role, align: 'center', width: 96,
      onHeaderCell: () => ({ title: RBAC_ROLE_NOTES[role] ?? role }),
      render: (has) => (has
        ? <Typography.Text type="success" style={{ fontWeight: 600 }}>✓</Typography.Text>
        : <Typography.Text type="secondary">—</Typography.Text>),
    })),
  ];
  const rows = RBAC_ACTIONS.map((a) => ({
    key: a.id, label: a.label,
    ...Object.fromEntries(RBAC_ROLES.map((role) => [role, RBAC_GRANTS[role].includes(a.id)])),
  }));
  return (
    <div style={{ marginBottom: 12 }}>
      <Typography.Title level={5} style={{ marginTop: 0 }}>权限矩阵（5 角色 × 13 动作）</Typography.Title>
      <Table rowKey="key" size="small" pagination={false} dataSource={rows} columns={columns} scroll={{ x: true }} />
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        展示面与后端授权（authz）由契约测试锁定一致；✓ = 角色默认动作面，标「需绑定」的动作还要求仓库
        Binding 处于 active。授权判定一律由后端执行——本表不改变任何权限。
      </Typography.Text>
    </div>
  );
}
