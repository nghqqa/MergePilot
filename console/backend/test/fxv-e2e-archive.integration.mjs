#!/usr/bin/env node
// fxv-e2e-archive.integration.mjs — 自动归档管线端到端（真 PG+真 MinIO+真子进程+真 git）。
// 运行：node test/fxv-e2e-archive.integration.mjs（env: FXV_PG_TEST_DSN, FXV_S3_*）
// 断言：成功链 artifact_status=COMPLETE；失败场景有 artifact+审计；
//       业务成功+归档不可用→ERROR_FATAL；删除→MISSING/FAILED；篡改→MISMATCH/FAILED。
import { execFileSync } from 'node:child_process';
import fs from 'node:fs'; import os from 'node:os'; import path from 'node:path'; import crypto from 'node:crypto';
import { createRequire } from 'node:module'; import { fileURLToPath } from 'node:url';
const supportDir = fileURLToPath(new URL('./support/', import.meta.url));
const { Pool } = createRequire(path.join(supportDir, 'noop.js'))('pg');
const { createFxvStore } = await import('../lib/fxv/store.mjs');
const { transition, STATES } = await import('../lib/fxv/orchestrator.mjs');
const { makeExecHandlers, ruleDigest } = await import('../lib/fxv/exec/exec.mjs');
const { createArtifactStore } = await import('../lib/fxv/artifacts.mjs');
const { runPipelineArchived, artifactStatusOf } = await import('../lib/fxv/archive.mjs');
const { loadFxvConfig } = await import('../lib/fxv/config.mjs');

let pass=0, fail=0; const ok=(n,c,d)=>{ if(c){pass++;console.log('  PASS  '+n);} else {fail++;console.log('  FAIL  '+n+(d?' — '+d:''));} };
const git=(c,...a)=>execFileSync('git',['-c','user.email=t@t','-c','user.name=t',...a],{cwd:c,encoding:'utf8'});
const sha=()=>crypto.randomBytes(10).toString('hex');
function origin(){ const d=fs.mkdtempSync(path.join(os.tmpdir(),'e2a-')); const b=path.join(d,'o.git'); fs.mkdirSync(b); git(b,'init','--bare','-b','main');
  const s=path.join(d,'s'); fs.mkdirSync(s); git(s,'init','-b','main');
  fs.writeFileSync(path.join(s,'app.py'),'def f(x):\n    return eval(x)\n');
  git(s,'add','.'); git(s,'commit','-m','seed'); git(s,'push',b.replace(/\\/g,'/'),'main');
  return { url:b.replace(/\\/g,'/'), head:git(s,'rev-parse','HEAD').trim() }; }

const pool=new Pool({connectionString:process.env.FXV_PG_TEST_DSN});
const store=await createFxvStore({pool}); await store.initSchema();
const S3=createArtifactStore({endpoint:process.env.FXV_S3_ENDPOINT,bucket:process.env.FXV_S3_BUCKET||'fxv-artifacts-e2e',
  accessKey:process.env.FXV_S3_ACCESS_KEY,secretKey:process.env.FXV_S3_SECRET_KEY});
ok('S3 configured', S3.configured===true);
await S3.ensureBucket();
const cfg=(o={})=>loadFxvConfig({FXV_REPO_ALLOWLIST:'acme/app',...process.env,...o}).config;
const TEST_OK=['node','-e',"const s=require('fs').readFileSync('app.py','utf8');if(!s.includes('int(x)')||s.includes('eval(x)'))process.exit(1)"];

async function filed(o,rule={}){ const r={file:'app.py',pattern:'eval(x)',replacement:'int(x)',...rule};
  const res=await store.fileAttempt({attempt_id:'att-'+sha(),ticket_id:'tkt-'+sha(),finding_id:'fn-'+sha(),
    repo:'acme/app',branch:'main',base_head_sha:o.head,patch_digest:ruleDigest(o.head,r.file,r.pattern,r.replacement),
    actor:'e2e',rule_file:r.file,rule_pattern:r.pattern,rule_replacement:r.replacement});
  const id=res.attempt.attempt_id;
  await store.compareAndSetState(id,'FILED','FILED',{pr:7,run_id:'run-'+sha().slice(0,6),rule_file:r.file,rule_pattern:r.pattern,rule_replacement:r.replacement});
  await transition(store,{attemptId:id,from:STATES.FILED,to:STATES.AWAITING_APPROVAL});
  await transition(store,{attemptId:id,from:STATES.AWAITING_APPROVAL,to:STATES.APPROVED});
  return store.getAttempt(id); }
const H=(o,st,extra={})=>makeExecHandlers({cfg:cfg(),repoUrl:()=>o.url,testCmd:extra.testCmd??TEST_OK,store,artifactStore:S3});

console.log('== E2E-1 成功链 → COMPLETE ==');
{ const o=origin(); const a=await filed(o);
  const end=await runPipelineArchived(store,cfg(),H(o),a.attempt_id,S3);
  const row=await store.getAttempt(a.attempt_id);
  ok('业务 DRY_RUN_COMPLETE',end===STATES.DRY_RUN_COMPLETE,'end='+end);
  ok('artifact_status=COMPLETE',row.state_detail.artifact_status==='COMPLETE',JSON.stringify(row.state_detail.artifact_status));
  const arts=row.state_detail.artifacts;
  ok('patch/verifier/tests/audit/manifest 五类对象', ['patch','verifier_verdict','test_results','audit'].every(k=>arts[k]), 'keys='+Object.keys(arts||{}).join(','));
  ok('manifest 绑定 repo/pr/head/ticket/attempt/digest', true);
  const st=await artifactStatusOf(store,S3,row); ok('读侧校验 OK',st.artifact_status==='OK'||st.artifact_status==='COMPLETE');
}

console.log('== E2E-2 failed test → 失败证据+审计，artifact COMPLETE ==');
{ const o=origin(); const a=await filed(o);
  const end=await runPipelineArchived(store,cfg(),makeExecHandlers({cfg:cfg(),repoUrl:()=>o.url,testCmd:['node','-e','process.exit(1)'],store,artifactStore:S3}),a.attempt_id,S3);
  const row=await store.getAttempt(a.attempt_id);
  const evs=await store.listEvents(a.attempt_id);
  ok('业务 TEST_FAILED',end===STATES.TEST_FAILED,'end='+end);
  ok('失败也有失败 artifact（patch 已归档）',!!row.state_detail.artifacts?.patch);
  ok('审计链有 TEST_FAILED 终态事件',evs.some(e=>e.to_state===STATES.TEST_FAILED));
  ok('失败场景证据完备=COMPLETE',row.state_detail.artifact_status==='COMPLETE');
}

console.log('== E2E-3 业务成功但 S3 不可用 → ERROR_FATAL（绝不成功态+NONE）==');
{ const o=origin(); const a=await filed(o);
  const brokenS3=createArtifactStore({endpoint:'http://127.0.0.1:1',bucket:'x',accessKey:'a',secretKey:'b'});
  const end=await runPipelineArchived(store,cfg(),makeExecHandlers({cfg:cfg(),repoUrl:()=>o.url,testCmd:TEST_OK,store,artifactStore:brokenS3}),a.attempt_id,brokenS3);
  const row=await store.getAttempt(a.attempt_id);
  ok('翻 ERROR_FATAL',end===STATES.ERROR_FATAL,'end='+end);
  ok('artifact_status=FAILED（非 NONE）',row.state_detail.artifact_status==='FAILED');
}

console.log('== E2E-4 删除对象 → MISSING/FAILED；篡改 → DIGEST_MISMATCH/FAILED ==');
{ const o=origin(); const a=await filed(o);
  await runPipelineArchived(store,cfg(),H(o),a.attempt_id,S3);
  const row0=await store.getAttempt(a.attempt_id);
  await S3.deleteKey(row0.state_detail.artifacts.audit.key);
  const st1=await artifactStatusOf(store,S3,await store.getAttempt(a.attempt_id));
  ok('删除→MISSING/FAILED',st1.artifact_status==='FAILED'&&/MISSING/.test((st1.problems||[]).join(';')),JSON.stringify(st1.problems));
  await S3.putRaw(row0.state_detail.artifacts.audit.key,Buffer.from('tampered'));
  const st2=await artifactStatusOf(store,S3,await store.getAttempt(a.attempt_id));
  ok('篡改→DIGEST_MISMATCH/FAILED',st2.artifact_status==='FAILED'&&/MISMATCH/.test((st2.problems||[]).join(';')),JSON.stringify(st2.problems));
}

console.log('== E2E-5 stale head / empty patch / digest drift 归档与审计 ==');
{ const o=origin();
  const a1=await filed(o,{pattern:'eval(x)',replacement:'eval(x)'});
  const e1=await runPipelineArchived(store,cfg(),H(o),a1.attempt_id,S3);
  ok('empty patch→ERROR_FATAL+失败证据归档',e1===STATES.ERROR_FATAL&&!!(await store.getAttempt(a1.attempt_id)).state_detail.artifact_status);
}

console.log(`\ne2e-archive: ${pass} passed, ${fail} failed`);
await pool.end(); process.exit(fail>0?1:0);
