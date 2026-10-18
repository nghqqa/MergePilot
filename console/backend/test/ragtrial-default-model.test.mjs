// ragtrial-default-model.test.mjs — review-aux/机器通道缺省模型解析回归
// （2026-10-18 试点验证缺口）：缺省必须取 env 配置的语义嵌入模型
// （RAGTRIAL_EMBED_MODEL_ID），不能落到首注册的 local-hash-v1（词法模型）——
// 否则语义索引查询被静默路由到词法通道（18700 试点实测缺陷）。
// 运行：cd console/backend && node --test test/ragtrial-default-model.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { resolveRequestModelId } from '../lib/ragtrial/api.mjs';

test('显式 model_id 优先于 env 缺省', () => {
  assert.equal(resolveRequestModelId({ RAGTRIAL_EMBED_MODEL_ID: 'bge-m3' }, 'other-model'), 'other-model');
});

test('缺省取 RAGTRIAL_EMBED_MODEL_ID', () => {
  assert.equal(resolveRequestModelId({ RAGTRIAL_EMBED_MODEL_ID: 'bge-m3' }, undefined), 'bge-m3');
});

test('env 未设置 → null（由 store 解析默认活跃模型，行为同前）', () => {
  assert.equal(resolveRequestModelId({}, undefined), null);
  assert.equal(resolveRequestModelId({ RAGTRIAL_EMBED_MODEL_ID: '' }, undefined), null);
  assert.equal(resolveRequestModelId({ RAGTRIAL_EMBED_MODEL_ID: '   ' }, undefined), null);
});

test('env 值两端空白被裁剪', () => {
  assert.equal(resolveRequestModelId({ RAGTRIAL_EMBED_MODEL_ID: '  bge-m3  ' }, undefined), 'bge-m3');
});

test('显式空串不算有效指定 → 落到 env 缺省', () => {
  assert.equal(resolveRequestModelId({ RAGTRIAL_EMBED_MODEL_ID: 'bge-m3' }, ''), 'bge-m3');
});
