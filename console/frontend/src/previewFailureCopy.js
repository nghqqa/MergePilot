// previewFailureCopy.js — 预览失败消息契约（纯函数）：中文原因 + 影响/下一步 + 技术详情。
// 背景（2026-10-17）：合并预览失败只显示「HTTP409：remote_unreachable」——未说明无法读取
// 哪项远端信息，也没有恢复建议。远端不可达不得解释为冲突、CI 失败或 head 已变化。

export function previewFailureCopy({ status, reason, detail, what }) {
  const whatText = what ?? '预览';
  if (reason === 'remote_unreachable') {
    return {
      lead: `当前无法读取远端 PR 信息——${whatText}未执行，确认保持阻塞。`,
      next: '下一步：检查连接器/仓库访问条件，恢复后重新预览。',
      tech: `HTTP ${status ?? '—'} · ${reason}${detail ? ` · ${detail}` : ''}`,
    };
  }
  return {
    lead: `预览未完成（HTTP ${status ?? '—'}）：${reason ?? '未知原因'}`,
    next: '可稍后重试；持续失败请检查数据源与连接器。',
    tech: detail ? String(detail) : null,
  };
}
