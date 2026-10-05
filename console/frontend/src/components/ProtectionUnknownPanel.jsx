import React from 'react';
import { Link } from 'react-router-dom';
import { ExternalLink } from 'lucide-react';
import { Button } from 'antd';
import { PROTECTION_UNKNOWN_STATES, protectionUnknownKind } from '../anomalies.js';

// ProtectionUnknownPanel — 保护状态未知的四分子态面板（共享组件）。
// 使用场景：
//   1. 审查工作台异常区（汇总：全租户保护未知 PR 列表，逐条给子态）
//   2. MU PR 详情（单 PR：结合"是否有进行中 run"推导 checking/undetermined）
// 红线：未知≠未受保护（fail-closed）；每个子态必须给出 原因/影响/下一步操作——
//       不允许只显示状态文字而没有操作路径。
// 后端缺口（如实呈现）：mu.pull_request 只落 unknown/known_clean/blocked，无细分原因码——
//   非检查中态如实显示"原因未细分"，并列出三种可能原因各自的核对与处理路径；
//   后端补 reason 码后本组件自动收敛为精确子态（protectionUnknownKind 单点替换）。

const GITHUB_SETTINGS_HINT = 'https://docs.github.com/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches';

function NextAction({ stateKey, next, onRetry, onOpenDetail }) {
  if (!next) return null;
  if (next.kind === 'github_settings') {
    return (
      <a className="btn btn-sm" href={GITHUB_SETTINGS_HINT} target="_blank" rel="noopener noreferrer">
        <ExternalLink size={12} strokeWidth={1.75} aria-hidden /> {next.label}
      </a>
    );
  }
  if (next.kind === 'multiuser') {
    return <Link className="btn btn-sm" to="/multiuser"
      title="在组织与接入页核对 GitHub App 权限（administration:read）">{next.label}</Link>;
  }
  if (next.kind === 'retry') {
    return <Button size="small" onClick={onRetry} disabled={!onRetry}
      title={onRetry ? '重新检查保护状态（随审查自动重探）' : '当前上下文不提供重试入口'}>{next.label}</Button>;
  }
  // detail：有 onOpenDetail 给按钮；否则只留下方 hint 文字（避免与 hint 重复）
  return onOpenDetail ? <Button size="small" onClick={onOpenDetail}>{next.label}</Button> : null;
}

function StateCard({ stateKey, extra, onRetry, onOpenDetail }) {
  const s = PROTECTION_UNKNOWN_STATES[stateKey];
  if (!s) return null;
  return (
    <div className={`pu-state pu-tone-${s.tone}`}>
      <div className="pu-state-head">
        <span className={`badge badge-${s.tone}`}><span className="badge-dot" aria-hidden />{s.label}</span>
        {extra}
      </div>
      <dl className="pu-facts">
        <div><dt>原因</dt><dd>{s.cause}</dd></div>
        <div><dt>影响</dt><dd>{s.impact}</dd></div>
        <div><dt>下一步</dt><dd>
          <NextAction stateKey={stateKey} next={s.next} onRetry={onRetry} onOpenDetail={onOpenDetail} />
          <div className="muted" style={{ fontSize: 12 }}>{s.next?.hint}</div>
        </dd></div>
      </dl>
    </div>
  );
}

/**
 * 单 PR 视图：推导子态（checking / undetermined）并展开事实与操作。
 * @param pr      MU PR 行（branch_protection_status）
 * @param hasActiveRun  是否有进行中的审查 run（REVIEWING 等）
 * @param onRetry 重探动作（可选）
 */
export function ProtectionUnknownCard({ pr, hasActiveRun = false, onRetry }) {
  const stateKey = protectionUnknownKind(pr, hasActiveRun);
  const s = PROTECTION_UNKNOWN_STATES[stateKey];
  return (
    <div className="pu-wrap" role="note" aria-label="保护状态未知详情">
      <div className="pu-note">
        保护状态未知 ≠ 未受保护——合并资格 fail-closed 恒为未知，本面板不猜测保护有无。
        {stateKey === 'undetermined' ? ' 当前部署未记录细分原因码，以下按可能原因逐项排查：' : ''}
      </div>
      <StateCard stateKey={stateKey} />
      {stateKey === 'undetermined'
        ? s.candidates.map((c) => <StateCard key={c} stateKey={c} extra={<span className="muted" style={{ fontSize: 12 }}>可能原因</span>} />)
        : null}
      {stateKey === 'checking' && onRetry ? (
        <Button size="small" onClick={onRetry}>立即重探</Button>
      ) : null}
    </div>
  );
}

/**
 * 汇总视图：工作台异常区——全部保护未知的 PR（按 PR 去重后），逐条给子态与去向。
 * 列表封顶展示（默认 8 条）：首屏保持紧凑，余量以计数收口。
 * @param items [{repo, pr, updated_at, hasActiveRun}]（MU 域 branch_protection_status='unknown' 的行）
 * @param limit 展示上限（默认 8）
 */
export function ProtectionUnknownSummary({ items = [], onOpenPr, limit = 8 }) {
  if (items.length === 0) return null;
  const shown = items.slice(0, limit);
  const rest = items.length - shown.length;
  return (
    <ul className="pu-list">
      {shown.map((it) => {
        const stateKey = protectionUnknownKind(it, it.hasActiveRun);
        const s = PROTECTION_UNKNOWN_STATES[stateKey];
        return (
          <li key={`${it.repo}#${it.pr}`} className="pu-list-item">
            <span className="mono">{it.repo} #{it.pr}</span>
            <span className={`badge badge-${s?.tone ?? 'warn'}`} title={s?.cause}>
              <span className="badge-dot" aria-hidden />{s?.label ?? '原因未细分'}
            </span>
            {onOpenPr ? (
              <Button size="small" type="link" onClick={() => onOpenPr(it)}>打开详情</Button>
            ) : null}
          </li>
        );
      })}
      {rest > 0 ? (
        <li className="pu-list-item muted" style={{ fontSize: 12 }}>
          …另有 {rest} 个 PR 保护状态未知——点击上方「{items.length}」计数在下方列表查看全部。
        </li>
      ) : null}
    </ul>
  );
}
