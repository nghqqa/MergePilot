// ragtrial/ingest.mjs — 文档清洗、分块（带行号/段落引用）、去重（LOCAL_RAG_TRIAL）。
//
// 输出契约：每个 chunk 必须携带 doc 内定位（chunk_index/para_index/line_start/
// line_end/char_start/char_end）与内容指纹 chunk_sha256 —— 引用缺失的行在
// 检索侧被丢弃（store.search 的 citation guard），这里不产生无引用 chunk。

import { sha256hex } from './embed.mjs';

// 清洗：CRLF→LF、去尾空白、控制字符剔除（保留 \n \t）、3+ 空行折叠。
// 返回 {text, notes}；不删除任何正文内容（引用必须可回溯原文）。
export function cleanText(raw) {
  const original = String(raw ?? '');
  let text = original.replace(/\r\n?/g, '\n');
  const beforeStrip = text;
  text = text.replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g, '');
  const notes = [];
  if (text !== beforeStrip) notes.push('control_chars_stripped');
  const collapsed = text.replace(/\n{3,}/g, '\n\n');
  if (collapsed !== text) notes.push('blank_lines_folded');
  text = collapsed.replace(/[ \t]+$/gm, '');
  const trimmed = text.replace(/^\n+/, '').replace(/\n+$/, '');
  if (trimmed !== text) notes.push('doc_edge_blank_lines_trimmed');
  return { text: trimmed, notes, original_bytes: Buffer.byteLength(original, 'utf8') };
}

// 按空行切段并保留行号；小段合并到目标长度；超长段按句子/字符二次切分。
// target/min/max 均为字符数。所有行号 1-based 闭区间，对应清洗后文本。
export function chunkDocument(text, { target = 500, max = 1200, min = 80 } = {}) {
  const lines = text.split('\n');
  const paras = []; // {startLine, endLine, text}
  let buf = [];
  let start = 1;
  for (let i = 0; i <= lines.length; i++) {
    const line = lines[i];
    const isBlank = line === undefined || line.trim() === '';
    if (!isBlank) {
      if (buf.length === 0) start = i + 1;
      buf.push(line);
      continue;
    }
    if (buf.length) {
      paras.push({ startLine: start, endLine: start + buf.length - 1, text: buf.join('\n') });
      buf = [];
    }
  }
  // 合并小段
  const merged = [];
  let cur = null;
  for (const p of paras) {
    if (!cur) { cur = { startLine: p.startLine, endLine: p.endLine, text: p.text }; continue; }
    if ((cur.text.length + p.text.length + 1) <= target) {
      cur.text += '\n' + p.text;
      cur.endLine = p.endLine;
    } else {
      merged.push(cur);
      cur = { startLine: p.startLine, endLine: p.endLine, text: p.text };
    }
  }
  if (cur) merged.push(cur);

  // 超长段二次切分（句子边界优先，退化为字符硬切）；行号随内容推进
  const split = [];
  for (const p of merged) {
    if (p.text.length <= max) { split.push(p); continue; }
    const pLines = p.text.split('\n');
    let lineCursor = p.startLine;
    let acc = [];
    let accStart = p.startLine;
    let accLen = 0;
    const flushAcc = () => {
      if (acc.length) {
        split.push({ startLine: accStart, endLine: accStart + acc.length - 1, text: acc.join('\n') });
        acc = []; accLen = 0;
      }
    };
    for (const ln of pLines) {
      if (accLen + ln.length + 1 > max && accLen >= min) { flushAcc(); accStart = lineCursor; }
      acc.push(ln); accLen += ln.length + 1;
      lineCursor++;
    }
    flushAcc();
  }

  // 尾段过小并入前段（除非独段）
  const out = [];
  for (const p of split) {
    if (out.length && p.text.length < min
        && (out[out.length - 1].text.length + p.text.length + 1) <= max) {
      out[out.length - 1].text += '\n' + p.text;
      out[out.length - 1].endLine = p.endLine;
    } else out.push(p);
  }

  // 组装最终 chunk（含 char 偏移与指纹）
  let charCursor = 0;
  return out.map((p, idx) => {
    const charStart = text.indexOf(p.text, charCursor);
    const at = charStart >= 0 ? charStart : charCursor;
    charCursor = at + p.text.length;
    return {
      chunk_index: idx,
      para_index: idx,
      line_start: p.startLine,
      line_end: p.endLine,
      char_start: at,
      char_end: at + p.text.length - 1,
      text: p.text,
      chunk_sha256: sha256hex(p.text),
    };
  });
}

// 文档内精确去重：相同 chunk_sha256 只保留首个，其余记入 duplicates。
export function dedupChunks(chunks) {
  const seen = new Set();
  const unique = [];
  const duplicates = [];
  for (const c of chunks) {
    if (seen.has(c.chunk_sha256)) { duplicates.push({ chunk_index: c.chunk_index, chunk_sha256: c.chunk_sha256 }); continue; }
    seen.add(c.chunk_sha256);
    unique.push(c);
  }
  if (duplicates.length) {
    // 重排 chunk_index 保持连续
    unique.forEach((c, i) => { c.chunk_index = i; });
  }
  return { unique, duplicates };
}

// 完整准备：清洗 → 分块 → 去重 → 嵌入（embedFn 可为 async provider）。
export async function prepareDocument({ repo, branch, doc_path, text }, embedFn) {
  if (!repo || !branch || !doc_path) throw new Error('prepareDocument: repo/branch/doc_path required');
  const cleaned = cleanText(text);
  const doc_sha256 = sha256hex(cleaned.text);
  const rawChunks = chunkDocument(cleaned.text);
  const { unique, duplicates } = dedupChunks(rawChunks);
  const vectors = unique.length ? await embedFn(unique.map((c) => c.text)) : [];
  const chunks = unique.map((c, i) => ({ ...c, embedding: vectors[i] }));
  return {
    repo, branch, doc_path,
    doc_sha256,
    content_bytes: cleaned.original_bytes,
    clean_notes: cleaned.notes,
    chunks,
    stats: { raw_chunks: rawChunks.length, chunks: chunks.length, duplicates: duplicates.length },
  };
}
