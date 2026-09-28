// console/backend/lib/multiuser/webhookVerify.mjs — GitHub webhook 验签（Wave 2B）。
// 合同：原始 body 字节 HMAC-SHA256、常量时间比较、全 fail-closed、secret 仅 env。
import crypto from 'node:crypto';

export function verifyWebhookSignature(rawBody, signatureHeader, secret) {
  if (!secret) return { ok: false, reason: 'webhook_secret_not_configured' };
  if (typeof rawBody !== 'string' || rawBody.length === 0) return { ok: false, reason: 'empty_body' };
  if (typeof signatureHeader !== 'string' || !signatureHeader.startsWith('sha256=')) {
    return { ok: false, reason: 'missing_signature' };
  }
  const expect = crypto.createHmac('sha256', secret).update(rawBody, 'utf8').digest('hex');
  const got = signatureHeader.slice('sha256='.length);
  if (!/^[0-9a-f]{64}$/.test(got)) return { ok: false, reason: 'malformed_signature' };
  const a = Buffer.from(expect, 'hex');
  const b = Buffer.from(got, 'hex');
  if (a.length !== b.length || !crypto.timingSafeEqual(a, b)) {
    return { ok: false, reason: 'bad_signature' };
  }
  return { ok: true };
}

// 时间/重放窗：GitHub 事件时间戳（header X-GitHub-Hook-Installation-Target-...不可靠，
// 用请求到达时刻与 delivery 记录共同约束；此处对 body 内 timestamp 类字段不做信任，
// 仅由 delivery 去重表承担一次性语义）
export function readRawBody(req, limit = 2 * 1024 * 1024) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on('data', (c) => {
      size += c.length;
      if (size > limit) { reject(Object.assign(new Error('body too large'), { status: 413 })); return; }
      chunks.push(c);
    });
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    req.on('error', reject);
  });
}
