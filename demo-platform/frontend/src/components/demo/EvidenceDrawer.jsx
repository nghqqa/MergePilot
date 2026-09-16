// frontend/src/components/demo/EvidenceDrawer.jsx — right-side (bottom sheet on
// mobile) evidence panel. Fetches one evidence item of a demo case and shows the
// mandated fields: run_id / PR / SHA / file / patch·diff / test command · summary /
// controller status / Matrix event_id (if any) / evidence level / generated time.
// Long text scrolls inside dark blocks; the page never stretches.
import React, { useEffect, useState } from 'react';
import { X, Copy, Check, ShieldCheck } from 'lucide-react';
import { api } from '../../api.js';
import { LevelChip, CodeBlock, KV, HonestTriple, useScrollLock, useEsc } from './bits.jsx';

export default function EvidenceDrawer({ caseId, evidenceId, onClose }) {
  const open = !!(caseId && evidenceId);
  const [d, setD] = useState(null);
  const [err, setErr] = useState(null);
  const [copied, setCopied] = useState(false);
  useScrollLock(open);
  useEsc(onClose, open);

  useEffect(() => {
    if (!open) return;
    let alive = true;
    setD(null); setErr(null);
    api(`/api/demo/cases/${caseId}/evidence?id=${encodeURIComponent(evidenceId)}`)
      .then((r) => alive && setD(r))
      .catch((e) => alive && setErr(e));
    return () => { alive = false; };
  }, [caseId, evidenceId, open]);

  if (!open) return null;

  // Case-level meta lives under `case` in the evidence payload (demoEvidence).
  const meta = d && d.case ? d.case : {};
  // SHA256 row: item-level hash (per-file vs the evidence dir's SHA256SUMS) first;
  // repo read-only references are honestly "不适用" instead of "未提供".
  const hashInfo = d && d.hash;
  let integText;
  if (hashInfo && hashInfo.exists) {
    integText = hashInfo.hash_verified === true ? `通过 · ${(hashInfo.sha256 || '').slice(0, 16)}…`
      : hashInfo.hash_verified === false ? '未通过（与 SUMS 不一致）'
      : '未命中 SUMS 条目';
  } else if (hashInfo && hashInfo.dir) {
    integText = '不适用 —— 仓库只读引用（不在证据目录内）';
  } else {
    integText = meta.source_dir ? '按证据目录整体校验（见全部证据页）' : '未提供';
  }
  const copyAll = async () => {
    if (!d) return;
    const text = [
      `${d.title}`, `case: ${d.case_id} · run_id: ${meta.run_id ?? '未提供'} · ${meta.pr ?? ''}`, `sha: ${meta.sha ?? '未提供'} (${meta.sha_kind ?? ''})`,
      `level: ${d.level}`, `source: ${d.source_ref}`, `generated_at: ${meta.generated_at ?? '未提供'}`, '',
      ...(d.fields || []).map(([k, v]) => `${k}: ${v}`), '',
      ...(d.blocks || []).flatMap((b) => [`--- ${b.title} ---`, b.text, '']),
    ].join('\n');
    try { await navigator.clipboard.writeText(text); } catch { /* clipboard unavailable */ }
    setCopied(true); setTimeout(() => setCopied(false), 1400);
  };

  return (
    <>
      <div className="drawer-backdrop" onClick={onClose} aria-hidden="true" />
      <aside className="drawer" role="dialog" aria-modal="true" aria-label="证据详情">
        <div className="drawer-head">
          <div style={{ flex: 1, minWidth: 0 }}>
            <div className="dt">{d ? d.title : err ? '证据加载失败' : '加载证据…'}</div>
            <div className="dsub2 mono">{evidenceId} · {caseId}</div>
            {d && <div style={{ marginTop: 6, display: 'flex', gap: 6, flexWrap: 'wrap' }}><LevelChip level={d.level} /></div>}
          </div>
          <button className="iconbtn" data-tip={copied ? '已复制' : '复制全部'} aria-label="复制全部" onClick={copyAll} disabled={!d}>
            {copied ? <Check size={15} /> : <Copy size={15} />}
          </button>
          <button className="iconbtn" data-tip="关闭 (Esc)" aria-label="关闭" onClick={onClose}><X size={15} /></button>
        </div>
        <div className="drawer-body">
          {err && <div className="notice amber">证据接口不可达：{err.message}</div>}
          {!d && !err && <div className="dstate"><span className="spin">◌</span> 读取锁定证据目录…</div>}
          {d && (
            <>
              <section className="drawer-sec">
                <div className="st">追溯标识</div>
                <KV rows={[
                  ['run_id', meta.run_id, true],
                  ['PR', meta.pr],
                  [`SHA · ${meta.sha_kind || 'sha'}`, meta.sha ?? '未提供', true],
                  ['证据目录', meta.source_dir, true],
                  ['来源', d.source_ref, true],
                  ['生成时间', meta.generated_at ?? '未提供', true],
                  ['SHA256 校验', integText],
                ]} />
              </section>

              <section className="drawer-sec">
                <div className="st">关键字段</div>
                <KV rows={(d.fields || []).map(([k, v]) => [k, v, /sha|id|command|digest|event/i.test(k)])} />
              </section>

              {(d.blocks || []).map((b) => (
                <section className="drawer-sec" key={b.title}>
                  <div className="st">{b.lang === 'diff' ? 'patch / diff' : b.lang === 'log' ? '测试输出' : b.lang === 'sql' ? 'SQL' : b.lang === 'json' ? '结构化记录' : '原文'}</div>
                  <CodeBlock title={b.title} text={b.text} lang={b.lang} max={360} />
                </section>
              ))}

              {d.note && <div className="notice gray" style={{ marginBottom: 14 }}>{d.note}</div>}

              <section className="drawer-sec">
                <div className="st" style={{ display: 'flex', gap: 6, alignItems: 'center' }}><ShieldCheck size={12} /> 诚实性标注</div>
                <HonestTriple real={d.real} controlled={d.controlled} not_executed={d.not_executed} />
              </section>
            </>
          )}
        </div>
      </aside>
    </>
  );
}
