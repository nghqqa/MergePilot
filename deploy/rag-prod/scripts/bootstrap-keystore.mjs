#!/usr/bin/env node
// deploy/rag-prod/scripts/bootstrap-keystore.mjs — 评估栈合成 keystore 引导。
// 生成【合成测试密钥】（随机 secret，短期有效期）写入 keystore-local/（gitignored），
// 供 /api/rag-trial/machine/query 的 RUN_BINDING_AUTH 验签测试使用。
// 绝不生成/复制真实凭据；目录绝不提交。
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', 'keystore-local');
fs.mkdirSync(DIR, { recursive: true });
const kid = `rk-eval-synthetic-${new Date().toISOString().slice(0, 14).replace(/[-T:]/g, '')}`;
const secret = crypto.randomBytes(32).toString('hex');
const key = {
  key_id: kid,
  secret,
  algorithm: 'hmac-sha256-full',
  scope: ['rag-trial:machine:query'],
  created_at: new Date().toISOString(),
  expires_at: new Date(Date.now() + 7 * 86400_000).toISOString(),
  revoked: false,
  note: 'SYNTHETIC EVAL KEY — 本地评估栈专用，非真实凭据',
};
const f = path.join(DIR, `${kid}.key.json`);
fs.writeFileSync(f, JSON.stringify(key, null, 2));
console.log(JSON.stringify({ ok: true, key_file: f, key_id: kid, secret_ref: `${kid} (secret in file, not echoed)` }));
