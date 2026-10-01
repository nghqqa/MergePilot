// console/backend/lib/diag/trace.mjs — 验收期诊断（Wave 3.7 数据源纯化审计）。
//
// 两个 env-gated 诊断面，默认完全关闭、零开销：
//   MU_QUERY_TRACE=<path>  记录本进程所有 pg 查询的 { 表名[], 调用点 file:line }——
//                          绝不记录 SQL 文本、参数、结果、Cookie 或凭据。
//   MU_ACCESS_LOG=<path>   记录每个 API 响应 { method, path, status, data_source }——
//                          服务端等价网络清单（HAR equivalent）。
// 用途：逐页面证明「MU 模式零 legacy 表查询」与「每页请求清单」。验收后关闭。
import fs from 'node:fs';
import path from 'node:path';

const TABLE_RE = /\b(?:from|join|into|update)\s+([a-zA-Z_][\w]*\.[a-zA-Z_][\w]*|[a-zA-Z_][\w]*)/gi;

function appendLine(file, obj) {
  try { fs.appendFileSync(file, JSON.stringify(obj) + '\n'); } catch { /* 诊断不反伤主流程 */ }
}

function callerFrame() {
  const err = new Error();
  const frames = (err.stack ?? '').split('\n').slice(1)
    .map((l) => l.trim())
    .filter((l) => !l.includes('trace.mjs') && !l.includes('node:'))
    .map((l) => {
      const m = l.match(/\(?([^()\s]+):(\d+):\d+\)?$/);
      return m ? `${path.basename(m[1])}:${m[2]}` : null;
    })
    .filter(Boolean);
  return frames[0] ?? 'unknown';
}

/** 对已加载的 pg 模块安装查询跟踪（幂等；仅 env 给出路径时由调用方决定启用）。 */
export function installQueryTraceOn(pgMod, filePath) {
  if (!pgMod || !filePath) return false;
  if (globalThis.__MU_TRACE_PG_INSTALLED) return true;
  const wrap = (Cls, kind) => {
    if (!Cls?.prototype) return;
    const orig = Cls.prototype.query;
    if (typeof orig !== 'function' || Cls.prototype.__muTraced) return;
    Cls.prototype.query = function traced(...args) {
      const sql = typeof args[0] === 'string' ? args[0]
        : typeof args[0]?.text === 'string' ? args[0].text : '';
      const KW = new Set(['set', 'select', 'values', 'where', 'returning', 'each', 'nothing', 'lateral']);
      const tables = [...new Set([...sql.matchAll(TABLE_RE)].map((m) => m[1])
        .filter((t) => !KW.has(t.toLowerCase())))].slice(0, 12);
      appendLine(filePath, { ts: new Date().toISOString(), kind, tables, caller: callerFrame() });
      return orig.apply(this, args);
    };
    Cls.prototype.__muTraced = true;
  };
  const Client = pgMod.Client ?? pgMod.default?.Client;
  const Pool = pgMod.Pool ?? pgMod.default?.Pool;
  wrap(Client, 'query');
  wrap(Pool, 'query');
  globalThis.__MU_TRACE_PG_INSTALLED = true;
  return true;
}

/** 访问日志：包装 sendJson（响应单点；请求上下文取自 res.req）。未启用时原样返回。 */
export function wrapSendJsonForAccessLog(sendJson, filePath) {
  if (!filePath) return sendJson;
  return (res, status, obj) => {
    try {
      const req = res?.req;
      appendLine(filePath, {
        ts: new Date().toISOString(),
        method: req?.method ?? null,
        path: (req?.url ?? '').split('?')[0].slice(0, 120),
        status,
        data_source: obj && typeof obj === 'object' ? (obj.data_source ?? obj.source ?? null) : null,
      });
    } catch { /* 诊断不反伤主流程 */ }
    return sendJson(res, status, obj);
  };
}
