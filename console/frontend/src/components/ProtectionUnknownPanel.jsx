import React from 'react';
import { Link } from 'react-router-dom';
import { ExternalLink } from 'lucide-react';
import { Button } from 'antd';
import { PROTECTION_UNKNOWN_STATES } from '../anomalies.js';

// ProtectionUnknownPanel — 保护状态未知的四分子态面板（共享组件）。
// 使用场景：
//   1. 审查工作台异常摘要 + 详情抽屉（共享调用方预计算的 stateKey——
//      摘要与详情绝不各自推导，见数据可信度修复 2026-10-05）
//   2. MU PR 详情（单 PR）
// 红线：
//   · 未知≠未受保护（fail-closed）；
//   · 「刷新状态」= 仅重新读取页面数据（本部署无独立探测端点，探测随审查运行发生）
//     ——绝不显示"重试检查/立即重探"这类看似会调探测接口的动词；
//   · 不渲染看似可执行但实际 disabled 的主操作（无动作时只留说明文字）；
//   · 仓库设置链接使用真实地址 https://github.com/{owner}/{repo}/settings/branches；
//     owner/name 缺失时降级为「查看配置指南」文档外链。

const GITHUB_DOCS_PROTECTION = 'https://docs.github.com/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches';

function repoSettingsUrl(repo) {
  if (repo?.owner && repo?.name) {
    return `https://github.com/${encodeURIComponent(repo.owner)}/${encodeURIComponent(repo.name)}/settings/branches`;
  }
  return null;
}

function NextAction({ next, repo, onRefresh, onOpenDetail }) {
  if (!next) return null;
  if (next.kind === 'repo_settings') {
    const url = repoSettingsUrl(repo);
    return url ? (
      <a className="btn btn-sm" href={url} target="_blank" rel="noopener noreferrer"
        title={`在新标签打开 ${url}`}>
        <ExternalLink size={12} strokeWidth={1.75} aria-hidden /> {next.label}
      </a>
    ) : (
      <a className="btn btn-sm" href={GITHUB_DOCS_PROTECTION} target="_blank" rel="noopener noreferrer">
        <ExternalLink size={12} strokeWidth={1.75} aria-hidden /> 查看配置指南
      </a>
    );
  }
  if (next.kind === 'multiuser') {
    return <Link className="btn btn-sm" to="/multiuser"
      title="在组织与接入页核对 GitHub App 权限（administration:read）">{next.label}</Link>;
  }
  if (next.kind === 'refresh') {
    // 仅重新读取页面数据——按钮只在调用方真的提供刷新动作时渲染（永不 disabled 摆设）
    return onRefresh
      ? <Button size="small" onClick={onRefresh}>{next.label}</Button>
      : null;
  }
  if (next.kind === 'detail') {
    return onOpenDetail ? <Button size="small" onClick={onOpenDetail}>{next.label}</Button> : null;
  }
  return null;
}

function StateCard({ stateKey, extra, repo, onRefresh, onOpenDetail }) {
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
        {s.next ? (
          <div><dt>下一步</dt><dd>
            <NextAction next={s.next} repo={repo} onRefresh={onRefresh} onOpenDetail={onOpenDetail} />
            <div className="muted" style={{ fontSize: 12 }}>{s.next.hint}</div>
          </dd></div>
        ) : null}
      </dl>
    </div>
  );
}

/**
 * 单 PR 视图：使用调用方预计算的 stateKey（与摘要列表共享同一推导结果）。
 * @param stateKey  protectionUnknownKind(pr, probeEvidence) 的结果（当前恒 undetermined，
 *                  checking 需显式探测证据——审查在途不构成证据）
 * @param repo      {owner, name}（真实仓库设置地址用；可缺省）
 * @param note      展示注记（如「审查进行中」——不宣称检查中/不承诺恢复时点；可缺省）
 * @param onRefresh 刷新页面数据（如实命名「刷新状态」；可缺省——缺省时不渲染按钮）
 * @param onOpenDetail 打开详情动作（可缺省）
 */
export function ProtectionUnknownCard({ stateKey, repo, note, onRefresh, onOpenDetail }) {
  const s = PROTECTION_UNKNOWN_STATES[stateKey];
  return (
    <div className="pu-wrap" role="note" aria-label="保护状态未知详情">
      <div className="pu-note">
        保护状态未知 ≠ 未受保护——合并资格 fail-closed 恒为未知，本面板不猜测保护有无，
        也不承诺等待后一定恢复。
        {note ? <strong> {note}</strong> : null}
        {stateKey === 'undetermined' && s?.candidates ? ' 当前部署未记录细分原因码，以下按可能原因逐项排查：' : ''}
      </div>
      <StateCard stateKey={stateKey} repo={repo} onRefresh={onRefresh} onOpenDetail={onOpenDetail} />
      {stateKey === 'undetermined' && s?.candidates
        ? s.candidates.map((c) => (
          <StateCard key={c} stateKey={c} repo={repo} onRefresh={onRefresh}
            extra={<span className="muted" style={{ fontSize: 12 }}>可能原因</span>} />
        ))
        : null}
    </div>
  );
}

/**
 * 摘要视图：工作台异常区——保护未知的 PR 列表（按 PR 去重，针对当前 head 判定）。
 * @param items [{repo, pr, stateKey, note, owner, name}]（stateKey 由调用方统一推导）
 * @param onOpenPr 打开详情
 * @param limit 展示上限（默认 5——首屏收敛）
 */
export function ProtectionUnknownSummary({ items = [], onOpenPr, limit = 5 }) {
  if (items.length === 0) return null;
  const shown = items.slice(0, limit);
  const rest = items.length - shown.length;
  return (
    <ul className="pu-list">
      {shown.map((it) => {
        const s = PROTECTION_UNKNOWN_STATES[it.stateKey];
        const prLabel = `${it.repo} #${it.pr}`;
        return (
          <li key={prLabel} className="pu-list-item">
            <span className="mono">{prLabel}</span>
            <span className={`badge badge-${s?.tone ?? 'warn'}`}
              title={`${s?.cause ?? ''}${it.note ? `（${it.note}）` : ''}`}>
              <span className="badge-dot" aria-hidden />
              {`保护状态未知${it.note ? `，${it.note}` : ''}`}
            </span>
            {onOpenPr ? (
              <Button size="small" type="link" aria-label={`打开详情：${prLabel}`}
                onClick={() => onOpenPr(it)}>打开详情</Button>
            ) : null}
          </li>
        );
      })}
      {rest > 0 ? (
        <li className="pu-list-item muted" style={{ fontSize: 12 }}>
          …另有 {rest} 个 PR 保护状态未知——点击「异常 PR」统计卡在下方列表查看全部。
        </li>
      ) : null}
    </ul>
  );
}
