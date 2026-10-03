#!/usr/bin/env node
// scripts/seed-skills.mjs — 技能注册表幂等铺底（v17 mu.skill_registry）。
//
// 用途：把平台既有 6 个真实技能登记进技能治理控制台（每租户一份命名空间）。
// 语义（与 /api/mu/skills 完全同构的幂等）：
//  * 已存在的 skill_key → 跳过（不动 display_name/状态/active 版本）；
//  * 已存在的 (skill, version) 且指纹相同 → 跳过；不同指纹绝不覆盖（不可变合同）；
//  * 新技能首版发布即自动激活（与 API 行为一致）；
//  * 仅对新建行写审计（MU_SKILL_REGISTERED / MU_SKILL_VERSION_PUBLISHED，
//    actor_user_id=NULL 系统铺底，detail 键白名单 {skill_key, version}）。
//
// 指纹口径（诚实声明）：manifest_sha256 = 登记时点实现工件的 sha256，钉死于首次铺底：
//  * 5 个 MCP 技能 → skill-mcp-server.mjs @ main b46add4410f（2026-10-03）
//    = sha256:da492579f8f927716810b5c8266e2c48f5706ddfb7b05e12bb77a7d4bc02b688
//  * rag.retrieve → console/backend/lib/ragtrial/api.mjs @ 同上
//    = sha256:92b01b7cdc331e9103ae9f2b6e2c1965d0197b5152ab31daad681b3348dc594d
//  实现演进后【不要改本脚本指纹】——在控制台发布递增版本（旧版本不可变）。
//
// 运行（须 v17 已迁移；MU 库任意连接方式）：
//   CONSOLE_PG_DSN=postgres://... node scripts/seed-skills.mjs [--dry-run]
// 新租户加入后需重跑一次（脚本对全部现存租户铺底）。
import crypto from 'node:crypto';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
// pg 解析双路径：仓库内走 test/support junction；容器内（/app 布局）走 /app/node_modules
let pgMod;
try { pgMod = createRequire(path.join(HERE, '../console/backend/test/support/noop.js'))('pg'); }
catch { pgMod = createRequire(path.join(HERE, 'package.json'))('pg'); }
const { Pool } = pgMod;

const DRY = process.argv.includes('--dry-run');
const DSN = process.env.CONSOLE_PG_DSN;
if (!DSN) { console.error('需要 CONSOLE_PG_DSN 环境变量'); process.exit(2); }

// 6 个平台技能（5 个 MCP 注册表真实实现 + rag.retrieve 试用执行面）
const MCP_SHA = 'da492579f8f927716810b5c8266e2c48f5706ddfb7b05e12bb77a7d4bc02b688';
const RAG_SHA = '92b01b7cdc331e9103ae9f2b6e2c1965d0197b5152ab31daad681b3348dc594d';
const ART_MCP = 'skill-mcp-server.mjs @ main b46add4410f';
const ART_RAG = 'console/backend/lib/ragtrial/api.mjs @ main b46add4410f';
const SKILLS = [
  { key: 'skill_case_retrieval', name: '案例检索', desc: '从 CASE 库检索相似历史案例，供审查引用', sha: MCP_SHA, art: ART_MCP },
  { key: 'skill_diff_parse', name: 'diff 解析', desc: '解析 PR diff，产出变更结构与文件级上下文', sha: MCP_SHA, art: ART_MCP },
  { key: 'skill_risk_classify', name: '风险分类', desc: '对发现项做风险分级（P0-P3）辅助排序', sha: MCP_SHA, art: ART_MCP },
  { key: 'skill_sast_scan', name: 'SAST 扫描', desc: '静态应用安全扫描（规则引擎域）', sha: MCP_SHA, art: ART_MCP },
  { key: 'skill_test_runner', name: '测试运行', desc: '在隔离环境运行测试套件并回收结果（MCP）', sha: MCP_SHA, art: ART_MCP },
  { key: 'rag.retrieve', name: '审查检索（RAG 试用）', desc: '知识库语义检索（试用执行面；调用留痕为 C 波）', sha: RAG_SHA, art: ART_RAG },
];

const pool = new Pool({ connectionString: DSN });
let created = 0, skippedSkill = 0, published = 0, skippedVer = 0, tenants = 0;

// 前置：v17 已迁移
const v = (await pool.query(`SELECT max(version) AS v FROM mu.schema_migrations`)).rows[0]?.v;
if (Number(v) < 17) { console.error(`ABORT: schema=${v}（需 ≥17——先跑 v17 迁移）`); process.exit(1); }

const tenantRows = (await pool.query(`SELECT tenant_id FROM mu.tenant ORDER BY created_at`)).rows;
for (const { tenant_id: T } of tenantRows) {
  tenants++;
  for (const s of SKILLS) {
    // ① 注册（幂等：同 key 跳过）
    const reg = await pool.query(
      `INSERT INTO mu.skill (tenant_id, skill_key, display_name, description, created_by)
       VALUES ($1,$2,$3,$4,NULL)
       ON CONFLICT (tenant_id, skill_key) DO NOTHING RETURNING skill_id`,
      [T, s.key, s.name, s.desc]);
    if (reg.rowCount === 0) { skippedSkill++; }
    else {
      created++;
      if (!DRY) await pool.query(
        `INSERT INTO mu.audit_event (tenant_id, actor_user_id, kind, detail)
         VALUES ($1, NULL, 'MU_SKILL_REGISTERED', $2::jsonb)`, [T, JSON.stringify({ skill_key: s.key })]);
      if (!DRY) console.log(`  + ${s.key}（${s.name}）`);
    }
    const skillId = reg.rows[0]?.skill_id
      ?? (await pool.query(`SELECT skill_id FROM mu.skill WHERE tenant_id=$1 AND skill_key=$2`, [T, s.key])).rows[0].skill_id;
    // ② 发布 1.0.0（幂等：同版本存在即跳过——绝不覆盖，同 API 不可变合同）
    const ver = await pool.query(
      `INSERT INTO mu.skill_version (tenant_id, skill_id, version, changelog, manifest_sha256, artifact_ref, created_by)
       VALUES ($1,$2,'1.0.0',$3,$4,$5,NULL)
       ON CONFLICT (skill_id, version) DO NOTHING RETURNING version_id`,
      [T, skillId, '初始登记（平台铺底）', s.sha, s.art]);
    if (ver.rowCount === 0) { skippedVer++; }
    else {
      published++;
      if (!DRY) await pool.query(
        `UPDATE mu.skill SET current_version='1.0.0', updated_at=now()
          WHERE skill_id=$1 AND current_version IS NULL`, [skillId]);
      if (!DRY) await pool.query(
        `INSERT INTO mu.audit_event (tenant_id, actor_user_id, kind, detail)
         VALUES ($1, NULL, 'MU_SKILL_VERSION_PUBLISHED', $2::jsonb)`,
        [T, JSON.stringify({ skill_key: s.key, version: '1.0.0' })]);
    }
  }
}

await pool.end();
console.log(`${DRY ? '[dry-run] ' : ''}seed-skills: 租户=${tenants} 注册=${created}（跳过 ${skippedSkill}）发布=${published}（跳过 ${skippedVer}）`);
process.exit(0);
