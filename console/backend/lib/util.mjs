// console/backend/lib/util.mjs — shared read-only helpers for the console adapter.

import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

export function readJsonSafe(absPath) {
  try {
    return JSON.parse(fs.readFileSync(absPath, 'utf8'));
  } catch {
    return null;
  }
}

export function readTextSafe(absPath, maxBytes = 2 * 1024 * 1024) {
  try {
    const buf = fs.readFileSync(absPath);
    return buf.subarray(0, maxBytes).toString('utf8');
  } catch {
    return null;
  }
}

export function existsFile(absPath) {
  try {
    return fs.statSync(absPath).isFile();
  } catch {
    return false;
  }
}

export function sha256File(absPath) {
  return new Promise((resolve, reject) => {
    const h = crypto.createHash('sha256');
    const s = fs.createReadStream(absPath);
    s.on('error', reject);
    s.on('data', (c) => h.update(c));
    s.on('end', () => resolve(h.digest('hex')));
  });
}

// Parse an ISO-ish timestamp seen in evidence packs ("2026-09-19T09:30:18.187502+00:00",
// "2026-09-18T16:51:32Z"). Returns null when unknown/unparseable — callers must
// keep null explicit instead of guessing.
export function parseTs(v) {
  if (typeof v !== 'string' || !v.trim()) return null;
  const d = new Date(v);
  return Number.isNaN(d.getTime()) ? null : d;
}

export function isoOrNull(d) {
  return d instanceof Date && !Number.isNaN(d.getTime()) ? d.toISOString() : null;
}

export function fmtDurationMs(ms) {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return null;
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

// Full 40-hex SHA validation — evidence packs always carry full SHAs; short
// forms are accepted only where explicitly noted by the caller.
export const SHA40_RE = /^[0-9a-f]{40}$/;
