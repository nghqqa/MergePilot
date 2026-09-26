#!/usr/bin/env node
// fxv/exec/worker-fixer.mjs — Fixer 独立进程。stdin 契约：
//   {repo_url, base_head_sha, finding:{file, pattern, replacement}, workspace}
// 行为：按 base_head_sha 精确取树 → 应用 finding 修复规则（数据驱动，无自由推理）→
//   git diff 产出 patch_text；patch_digest = sha256("rule@head")（与立案绑定同式）。
// 空差异 → {ok:false, reason:'EMPTY_PATCH'}。绝不由"fixer 自述"充当验证。
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

const git = (cwd, ...a) => execFileSync('git', ['-c', 'user.email=fxv@local', '-c', 'user.name=fxv-fixer', '-c', 'core.autocrlf=false', ...a], { cwd, encoding: 'utf8' });

async function main() {
  const input = JSON.parse(fs.readFileSync(0, 'utf8'));
  const { repo_url, base_head_sha, finding, workspace } = input;
  if (!repo_url || !base_head_sha || !finding?.file || !finding?.pattern || !finding?.replacement) {
    console.log(JSON.stringify({ ok: false, reason: 'BAD_INPUT' })); process.exit(2);
  }
  const ruleDigest = crypto.createHash('sha256')
    .update(`${base_head_sha}|${finding.file}|${finding.pattern}|${finding.replacement}`).digest('hex');
  fs.mkdirSync(workspace, { recursive: true });
  git(workspace, 'init', '-q', '-b', 'fxv');
  git(workspace, 'fetch', '-q', repo_url, base_head_sha);
  git(workspace, 'checkout', '-q', 'FETCH_HEAD');
  const target = path.join(workspace, finding.file);
  if (!target.startsWith(path.resolve(workspace))) { console.log(JSON.stringify({ ok: false, reason: 'PATH_ESCAPE' })); process.exit(2); }
  const orig = fs.readFileSync(target, 'utf8');
  if (!orig.includes(finding.pattern)) { console.log(JSON.stringify({ ok: false, reason: 'PATTERN_NOT_FOUND_AT_HEAD' })); process.exit(3); }
  fs.writeFileSync(target, orig.replace(finding.pattern, finding.replacement));
  let patch = '';
  try { patch = git(workspace, 'diff', 'FETCH_HEAD', '--', finding.file); } catch { patch = ''; }
  if (!patch.trim()) { console.log(JSON.stringify({ ok: false, reason: 'EMPTY_PATCH' })); process.exit(4); }
  console.log(JSON.stringify({ ok: true, patch_text: patch, patch_digest: ruleDigest, workspace, changed_file: finding.file }));
  process.exit(0);
}
main().catch((e) => { console.log(JSON.stringify({ ok: false, reason: 'FIXER_ERROR', detail: String(e?.message || e) })); process.exit(1); });
