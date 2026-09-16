// frontend/src/components/demo/EvidenceDrawer.jsx — right-side (bottom sheet on
// mobile) evidence panel. Fetches one evidence item of a demo case and shows the
// mandated fields: run_id / PR / SHA / file / patch·diff / test command · summary /
// controller status / Matrix event_id (if any) / evidence level / generated time.
// Long text scrolls inside dark blocks; the page never stretches.
import React, { useEffect, useState } from 'react';
import { X, Copy, Check, ShieldCheck } from 'lucide-react';
import { api } from '../../api.js';
import { LevelChip, CodeBlock, KV, HonestTriple, useScrollLock, useEsc } from './bits.jsx';

const INTEG_KEY = { 'evidence/FINALS-REWORK-LOOP-20260914': 'finalsReworkLoop', 'evidence/FINALS-DB-MIGRATION-LOOP-20260914': 'finalsDbLoop' };

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

  const integ = d && d.integrity ? d.integrity[INTEG_KEY[d.source_dir]] : null;
  const copyAll = async () => {
    if (!d) return;
    const text = [
      `${d.title}`, `case: ${d.case_id} · run_id: ${d.run_id} · ${d.pr}`, `sha: ${d.sha} (${d.sha_kind})`,
      `level: ${d.level}`, `source: ${d.source_ref}`, `generated_at: ${d.generated_at ?? '未提供'}`, '',
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
                  ['run_id', d.run_id, true],
                  ['PR', d.pr],
                  [`SHA · ${d.sha_kind || 'sha'}`, d.sha ?? '未提供', true],
                  ['证据目录', d.source_dir, true],
                  ['来源', d.source_ref, true],
                  ['生成时间', d.generated_at ?? '未提供', true],
                  ['SHA256 校验', integ ? (integ.verified ? `通过 · ${integ.files} 个文件` : '未通过') : '未提供'],
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
