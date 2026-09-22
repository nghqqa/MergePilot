// console/backend/lib/pack.mjs — read-only access to one locked evidence pack.
//
// Security invariants:
// - every file access goes through safeResolve(): the requested relative path
//   must normalize to a location strictly inside the pack directory;
// - packs are never written to;
// - SHA256SUMS verification is lazy and cached per pack (signature = file
//   count + max mtime), so a locked pack is re-verified if it ever changes.

import fs from 'node:fs';
import path from 'node:path';
import { sha256File } from './util.mjs';

// Anchor files that make an evidence directory a "run pack". Families without
// any anchor (phase/experiment packs) are intentionally NOT indexed as runs.
export const RUN_ANCHORS = [
  'delivery-ledger.json',
  'kickoff.json',
  'project/meta.json',
  'PR-METADATA.md',
];

export function listRunPacks(evidenceRoot) {
  let entries;
  try {
    entries = fs.readdirSync(evidenceRoot, { withFileTypes: true });
  } catch {
    return [];
  }
  const packs = [];
  for (const e of entries) {
    if (!e.isDirectory()) continue;
    const dir = path.join(evidenceRoot, e.name);
    if (!RUN_ANCHORS.some((a) => fs.existsSync(path.join(dir, a)))) continue;
    packs.push({ pack_id: e.name, dir });
  }
  return packs;
}

// Resolve a user-supplied relative path inside a pack, rejecting traversal.
// Throws Error with .status on rejection.
export function safeResolve(packDir, relPath) {
  if (typeof relPath !== 'string' || !relPath || relPath.includes('\0')) {
    const err = new Error('path must be a non-empty relative path');
    err.status = 400;
    throw err;
  }
  const posixPath = relPath.replace(/\\/g, '/');
  const normalized = path.posix.normalize(posixPath);
  if (
    normalized === '.' ||
    normalized.startsWith('/') ||
    /^[a-zA-Z]:/.test(normalized) ||
    normalized.split('/').some((seg) => seg === '..')
  ) {
    const err = new Error('path must stay inside the evidence pack');
    err.status = 400;
    throw err;
  }
  const rootAbs = path.resolve(packDir);
  const abs = path.resolve(packDir, normalized);
  if (abs !== rootAbs && !abs.startsWith(rootAbs + path.sep)) {
    const err = new Error('path must stay inside the evidence pack');
    err.status = 400;
    throw err;
  }
  return abs;
}

export function listPackFiles(packDir) {
  const out = [];
  const walk = (dir, prefix) => {
    let entries;
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const e of entries) {
      const rel = prefix ? `${prefix}/${e.name}` : e.name;
      if (e.isDirectory()) walk(path.join(dir, e.name), rel);
      else if (e.isFile()) {
        let size = 0;
        try {
          size = fs.statSync(path.join(dir, e.name)).size;
        } catch { /* unreadable stat → size 0, still listed */ }
        out.push({ path: rel, bytes: size });
      }
    }
  };
  walk(packDir, '');
  out.sort((a, b) => a.path.localeCompare(b.path));
  return out;
}

export function parseSha256Sums(packDir) {
  const file = path.join(packDir, 'SHA256SUMS');
  let text;
  try {
    text = fs.readFileSync(file, 'utf8');
  } catch {
    return null;
  }
  const sums = new Map();
  for (const line of text.split(/\r?\n/)) {
    const m = line.match(/^([0-9a-f]{64}) [ *](.+)$/);
    if (m) sums.set(m[2], m[1]);
  }
  return sums.size ? sums : null;
}

const verifyCache = new Map(); // pack_id -> { signature, result }

function packSignature(packDir) {
  let count = 0;
  let maxMtime = 0;
  const walk = (dir) => {
    let entries;
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const e of entries) {
      const abs = path.join(dir, e.name);
      if (e.isDirectory()) walk(abs);
      else if (e.isFile()) {
        count += 1;
        try {
          const mt = fs.statSync(abs).mtimeMs;
          if (mt > maxMtime) maxMtime = mt;
        } catch { /* ignore */ }
      }
    }
  };
  walk(packDir);
  return `${count}:${maxMtime}`;
}

export async function verifyPack(packId, packDir) {
  const signature = packSignature(packDir);
  const cached = verifyCache.get(packId);
  if (cached && cached.signature === signature) return cached.result;

  const sums = parseSha256Sums(packDir);
  const files = listPackFiles(packDir);
  const listed = new Set();
  const verified = [];
  const mismatched = [];
  if (sums) {
    for (const [rel, expected] of sums) {
      listed.add(rel);
      const abs = path.join(packDir, rel);
      let actual = null;
      try {
        if (fs.statSync(abs).isFile()) actual = await sha256File(abs);
      } catch { /* missing file → mismatch below */ }
      if (actual === expected) verified.push(rel);
      else mismatched.push({ path: rel, expected, actual });
    }
  }
  const unlisted = files.filter((f) => !listed.has(f.path)).map((f) => f.path);
  const result = {
    status: !sums ? 'no_sums' : mismatched.length ? 'mismatch' : 'verified',
    listed: listed.size,
    verified: verified.length,
    mismatched,
    unlisted_count: unlisted.length,
  };
  verifyCache.set(packId, { signature, result });
  return result;
}

// Text detection: decodable as UTF-8 without U+FFFD in the sampled head.
export function looksTextual(buf) {
  const sample = buf.subarray(0, 8192);
  if (sample.includes(0)) return false;
  return !sample.toString('utf8').includes('\uFFFD');
}
