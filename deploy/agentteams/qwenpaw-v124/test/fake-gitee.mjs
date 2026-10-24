// fake-gitee.mjs — 隔离测试用最小 Gitee API 假服务（仅适配器白名单面）。
// 端点：/api/v5/user、/api/v5/user/repos、/api/v5/repos/:owner/:repo、
//       .../pulls/:n、.../pulls/:n/files
// 所有 GET 放行（access_token 任意值）；绑定/审查面零副作用。
// 内容：PR #1 = app.py 将 eval(x) 改为 int(x) 的补丁（确定性规则链可命中）。
import http from 'node:http';

const REPO = { id: 99001, full_name: 'iso/test', default_branch: 'main',
  html_url: 'https://gitee.example/iso/test', private: false };
const USER = { login: 'iso-operator', id: 150000001, name: 'ISO Operator' };
const HEAD_SHA = 'c0ffee00c0ffee00c0ffee00c0ffee00c0ffee0';
const BASE_SHA = 'c0ffee00c0ffee00c0ffee00c0ffee00c0ffee0';
const PR = {
  number: 1, state: 'open', title: 'iso: eval→int 替换（四角色验收样本）',
  html_url: 'https://gitee.example/iso/test/pull/1',
  head: { sha: HEAD_SHA, ref: 'feature/eval-int', repo: { id: 99001 } },
  base: { sha: BASE_SHA, ref: 'main', repo: { id: 99001 } },
};
const FILES = [{
  filename: 'app.py', status: 'changed', additions: 1, deletions: 0,
  patch: { diff: '@@ -1,2 +1,3 @@\n def f(x):\n+    API_KEY = "sk-live-abc123def456ghi789jkl012"\n     return int(x)\n' },
}];

const json = (res, code, obj) => { const b = JSON.stringify(obj); res.writeHead(code, {
  'content-type': 'application/json', 'content-length': Buffer.byteLength(b) }); res.end(b); };

http.createServer((req, res) => {
  const u = new URL(req.url, 'http://x');
  const p = u.pathname.replace(/\/+$/, '') || '/';
  console.log(new Date().toISOString(), req.method, p);
  if (p === '/api/v5/user') return json(res, 200, USER);
  if (p === '/api/v5/user/repos') return json(res, 200, [REPO]);
  if (p === '/api/v5/repos/iso/test') return json(res, 200, REPO);
  if (p === '/api/v5/repos/iso/test/pulls/1') return json(res, 200, PR);
  if (p === '/api/v5/repos/iso/test/pulls/1/files')
    return json(res, 200, FILES.slice(Number(u.searchParams.get('page') ?? 1) - 1, Number(u.searchParams.get('per_page') ?? 100)));
  return json(res, 404, { message: 'not found', path: p });
}).listen(19999, () => console.log('fake-gitee listening :19999'));
