// frontend/src/components/ProbeCompare.jsx — 修复前后探针对照（数据来自真实原始探针输出）
import React from 'react';
import { SourceRef } from './ui.jsx';

export default function ProbeCompare({ fc }) {
  const p = fc.probe;
  return (
    <div>
      <p className="muted small" style={{ marginTop: 0 }}>{fc.headline}</p>
      <div className="panel" style={{ padding: 0, overflowX: 'auto' }}>
        <table className="probe-table">
          <thead>
            <tr>
              <th>请求</th>
              <th>修复前（before fix）</th>
              <th>修复后（after fix）</th>
              <th>结论</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td className="mono">{p.request_traversal}</td>
              <td>
                <span className="code-200">{p.before.traversal_status}</span>{' '}
                {p.before.leaked_outside_secret && <span className="small">泄露 TOP-SECRET-OUTSIDE-BASE（仓库内置演示占位串）</span>}
              </td>
              <td><span className="code-404">{p.after.traversal_status}</span> <span className="small">{"detail: Not found"} · 无泄露</span></td>
              <td style={{ color: 'var(--ok)' }}>越界被拒绝 ✓</td>
            </tr>
            <tr>
              <td className="mono">{p.request_legit}</td>
              <td><span className="code-200 ok">{p.before.legit_status}</span></td>
              <td><span className="code-200 ok">{p.after.legit_status}</span></td>
              <td style={{ color: 'var(--ok)' }}>合法文件无回归 ✓</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div className="grid2" style={{ marginTop: 14 }}>
        <div className="panel" style={{ marginBottom: 0 }}>
          <h3>补丁摘要</h3>
          <p className="small muted">{fc.patch_summary}</p>
          <p className="small"><span className="faint">文件：</span><span className="mono">{fc.file}</span></p>
          <SourceRef>{fc.patch_source}</SourceRef>
        </div>
        <div className="panel" style={{ marginBottom: 0 }}>
          <h3>测试与验证结论</h3>
          <dl className="kv small">
            <dt>测试结果</dt><dd className="muted">{fc.tests}</dd>
            <dt>Fixer 结果</dt><dd className="mono" style={{ color: 'var(--ok)' }}>{fc.fixer_result}</dd>
            <dt>Verifier 独立验证</dt><dd className="mono" style={{ color: 'var(--ok)' }}>{fc.verifier_result}</dd>
            <dt>PR 状态</dt><dd style={{ color: 'var(--warn)' }}>{fc.pr_state}</dd>
          </dl>
        </div>
      </div>

      <details style={{ marginTop: 12 }}>
        <summary className="small muted" style={{ cursor: 'pointer' }}>探针原始输出（verifier 独立采集，只读证据）</summary>
        <div className="grid2" style={{ marginTop: 8 }}>
          <pre className="tl-detail">{p.raw.before}</pre>
          <pre className="tl-detail">{p.raw.after}</pre>
        </div>
      </details>
      <SourceRef>{p.source.before} · {p.source.after} · 执行者：{p.source.runner}</SourceRef>
    </div>
  );
}
