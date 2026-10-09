// 官网导航单一来源：清单 + 渲染 + 幂等回写。
// 用法：
//   node nav.mjs            # 将顶栏/侧栏回写到各页面的标记区域（幂等）
//   node nav.mjs --check    # 只比对不写入（exit 1 = 有漂移）
// check.mjs 导入本模块做一致性校验。页面中的标记区域：
//   <!-- nav:top --> … <!-- /nav:top -->       顶栏（每页必有）
//   <!-- nav:docs --> … <!-- /nav:docs -->     文档侧栏（仅文档布局页）
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const TOP_ITEMS = [
  { label: '首页', href: 'index.html' },
  { label: '快速开始', href: 'quickstart.html' },
  { label: '文档', href: 'docs.html' },
  { label: '下载', href: 'downloads.html' },
];

// 文档分组（顺序即侧栏顺序）；items 为 { label, href, desc }，desc 供文档索引页使用
const DOC_GROUPS = [
  {
    title: '部署',
    items: [
      { label: '快速开始', href: 'quickstart.html', desc: '从克隆到健康检查的完整部署路径。' },
      { label: 'Docker 自托管', href: 'selfhost-docker.html', desc: 'compose 拓扑、数据卷与环境变量要点。' },
      { label: '源码构建', href: 'build-from-source.html', desc: '不用官方镜像时的本地构建步骤。' },
    ],
  },
  {
    title: '使用',
    items: [
      { label: '配置', href: 'config-reference.html', desc: '.env 全部必填与可选项的生成与说明。' },
      { label: '多租户与邀请', href: 'multitenant-webhook.html', desc: 'GitHub App、webhook 入口与成员邀请。' },
      { label: '角色与权限', href: 'roles.html', desc: '5 种角色的能力矩阵与 fail-closed 设计。' },
    ],
  },
  {
    title: '维护',
    items: [
      { label: '升级与回滚', href: 'upgrade-rollback.html', desc: '版本升级流程与回滚边界。' },
      { label: '限制', href: 'limitations.html', desc: '已知限制与支持矩阵。' },
      { label: '安全', href: 'security.html', desc: '生产暴露、TLS 与密钥加固清单。' },
    ],
  },
  {
    title: '发布',
    items: [
      { label: '下载与校验', href: 'downloads.html', desc: '镜像 digest、离线 tar 与 SHA256。' },
    ],
  },
];

const DOCS_INDEX_HREF = 'docs.html';

const allDocHrefs = () => DOC_GROUPS.flatMap((g) => g.items.map((i) => i.href));
const isDocPage = (page) => allDocHrefs().includes(page) || page === DOCS_INDEX_HREF;

const esc = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');

// 顶栏：品牌 / 首页 / 快速开始 / 文档 / 下载。
// aria-current：当前页命中的顶栏项用 "page"；页面是文档但不在顶栏时，让「文档」用 "true" 标示所属栏目。
export function renderTopNav(page) {
  const isTop = TOP_ITEMS.some((t) => t.href === page);
  const links = TOP_ITEMS.map((it) => {
    const cur = it.href === page ? 'page' : (!isTop && isDocPage(page) && it.href === DOCS_INDEX_HREF ? 'true' : '');
    return `      <a href="${it.href}"${cur ? ` aria-current="${cur}"` : ''}>${it.label}</a>`;
  }).join('\n');
  return `<!-- nav:top -->
<nav class="nav" aria-label="站点导航">
  <div class="nav-inner">
    <a class="nav-brand" href="index.html">MergePilot</a>
    <div class="nav-links" id="nav-menu">
${links}
    </div>
    <button class="nav-toggle" type="button" aria-expanded="false" aria-controls="nav-menu">菜单</button>
  </div>
</nav>
<!-- /nav:top -->`;
}

// 文档侧栏：分组二级导航；当前页 aria-current="page"，所属分组标题加 is-current。
export function renderDocsNav(page) {
  const groups = DOC_GROUPS.map((g) => {
    const current = g.items.some((i) => i.href === page);
    const items = g.items.map((i) => {
      const cur = i.href === page ? ' aria-current="page"' : '';
      return `        <li><a href="${i.href}"${cur}>${esc(i.label)}</a></li>`;
    }).join('\n');
    return `      <section class="side-group${current ? ' is-current' : ''}">
        <h2 class="side-title">${esc(g.title)}</h2>
        <ul class="side-list">
${items}
        </ul>
      </section>`;
  }).join('\n');
  return `<!-- nav:docs -->
<aside class="doc-nav" aria-label="文档导航">
${groups}
</aside>
<!-- /nav:docs -->`;
}

export { TOP_ITEMS, DOC_GROUPS, DOCS_INDEX_HREF, isDocPage };

// ── CLI：回写 / 漂移检查 ──
const invokedDirectly = process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href;
if (invokedDirectly) {
  const here = path.dirname(path.resolve(process.argv[1]));
  const pages = fs.readdirSync(here).filter((f) => f.endsWith('.html'));
  const region = (html, name) => {
    const m = html.match(new RegExp(`<!-- nav:${name} -->[\\s\\S]*?<!-- /nav:${name} -->`));
    return m ? m[0].replace(/\r\n/g, '\n') : null;
  };

  if (process.argv.includes('--check')) {
    let bad = 0;
    for (const p of pages) {
      const html = fs.readFileSync(path.join(here, p), 'utf8');
      if (region(html, 'top') !== renderTopNav(p)) { console.log('DRIFT top:', p); bad++; }
      const expectDocs = isDocPage(p);
      if (expectDocs && region(html, 'docs') !== renderDocsNav(p)) { console.log('DRIFT docs:', p); bad++; }
      if (!expectDocs && region(html, 'docs')) { console.log('UNEXPECTED docs nav:', p); bad++; }
    }
    console.log(bad ? `nav drift: ${bad} page(s)` : 'nav in sync');
    process.exit(bad ? 1 : 0);
  }

  const replaceRegion = (html, name, rendered) => {
    const m = html.match(new RegExp(`<!-- nav:${name} -->[\\s\\S]*?<!-- /nav:${name} -->`));
    if (m) return html.replace(m[0], rendered);
    const legacy = /<nav class="nav"[\s\S]*?<\/nav>/;
    if (name !== 'top' || !legacy.test(html)) throw new Error(`页面缺 nav 标记：${name}`);
    return html.replace(legacy, rendered);
  };

  for (const p of pages) {
    const file = path.join(here, p);
    const before = fs.readFileSync(file, 'utf8');
    let html = replaceRegion(before, 'top', renderTopNav(p));
    const wantsDocs = isDocPage(p);
    if (wantsDocs) {
      if (region(html, 'docs')) {
        html = replaceRegion(html, 'docs', renderDocsNav(p));
      } else {
        html = html.replace(/([ \t]*)(<article class="doc">)/, `${renderDocsNav(p)}\n$1$2`);
      }
    } else {
      html = html.replace(/<!-- nav:docs -->[\s\S]*?<!-- \/nav:docs -->[ \t]*\n?/, '');
    }
    if (html !== before) { fs.writeFileSync(file, html); console.log('updated', p); }
    else { console.log('clean  ', p); }
  }
}
