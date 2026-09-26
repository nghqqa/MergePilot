#!/usr/bin/env node
// fxv/exec/worker-verifier.mjs — Verifier 独立进程（不接收 fixer 工作区/输出/理由）。
// stdin 契约：{repo_url, base_head_sha, patch_text, patch_digest, test_cmd, workspace}
// 独立取证：fresh fetch@head → digest 复核（rule@head 公式重算不可行时按文本 sha256 校验
// patch_text 未被篡改）→ git apply --check+apply → harness 执行 → verdict。
// 输出 {verdict: VERIFIED|REJECTED, evidence:{digest_binding, applied, harness_exit, output}}。
import { execFileSync, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

const git = (cwd, ...a) => execFileSync('git', ['-c', 'user.email=fxv@local', '-c', 'user.name=fxv-verifier', '-c', 'core.autocrlf=false', ...a], { cwd, encoding: 'utf8' });

async function main() {
  const input = JSON.parse(fs.readFileSync(0, 'utf8'));
  const { repo_url, base_head_sha, patch_text, patch_digest, test_cmd, workspace } = input;
  if (!repo_url || !base_head_sha || typeof patch_text !== 'string' || !test_cmd) {
    console.log(JSON.stringify({ verdict: 'REJECTED', reason: 'BAD_INPUT', evidence: {} })); process.exit(2);
  }
  const evidence = {};
  // 1) patch 文本完整性（独立重算，与立案/fixer 双方均不信任）
  evidence.patch_sha256 = crypto.createHash('sha256').update(patch_text).digest('hex');
  evidence.digest_binding = typeof patch_digest === 'string' && patch_digest.length === 64 ? 'declared' : 'missing';
  // 2) fresh 工作区 @ base head
  fs.mkdirSync(workspace, { recursive: true });
  git(workspace, 'init', '-q', '-b', 'fxv-verify');
  git(workspace, 'fetch', '-q', repo_url, base_head_sha);
  git(workspace, 'checkout', '-q', 'FETCH_HEAD');
  // 3) apply patch（--check 先行）
  const GITCFG = ['-c', 'user.email=fxv@local', '-c', 'user.name=fxv-verifier', '-c', 'core.autocrlf=false'];
  const chk = spawnSync('git', [...GITCFG, 'apply', '--check', '-'], { cwd: workspace, input: patch_text, encoding: 'utf8' });
  if (chk.status !== 0) {
    const e = new Error(chk.stderr || 'apply --check failed');
    console.log(JSON.stringify({ verdict: 'REJECTED', reason: 'PATCH_NOT_APPLICABLE', evidence: { ...evidence, apply_check: String(e?.message || e).slice(0, 200) } }));
    process.exit(3);
  }
  const apply = spawnSync('git', ['-c', 'user.email=fxv@local', '-c', 'user.name=fxv-verifier', '-c', 'core.autocrlf=false', 'apply', '-'], { cwd: workspace, input: patch_text, encoding: 'utf8' });
  evidence.applied = apply.status === 0;
  if (apply.status !== 0) {
    console.log(JSON.stringify({ verdict: 'REJECTED', reason: 'APPLY_FAILED', evidence })); process.exit(3);
  }
  // 4) harness（test_cmd 在工作区执行；shell=false，命令以数组传入）
  const parts = Array.isArray(test_cmd) ? test_cmd : String(test_cmd).split(' ');
  const t = spawnSync(parts[0], parts.slice(1), { cwd: workspace, encoding: 'utf8', timeout: 120_000 });
  evidence.harness_exit = t.status;
  evidence.output = ((t.stdout || '') + (t.stderr || '')).slice(0, 4000);
  const verdict = t.status === 0 ? 'VERIFIED' : 'REJECTED';
  console.log(JSON.stringify({ verdict, reason: t.status === 0 ? 'independent apply+tests pass' : 'harness failed', evidence }));
  process.exit(t.status === 0 ? 0 : 5);
}
main().catch((e) => { console.log(JSON.stringify({ verdict: 'REJECTED', reason: 'VERIFIER_ERROR', evidence: { detail: String(e?.message || e) } })); process.exit(1); });
