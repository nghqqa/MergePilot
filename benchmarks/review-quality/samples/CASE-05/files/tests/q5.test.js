const { double } = require('../src/q5.js');
const assert = require('node:assert');
(async () => {
  assert.strictEqual(await double(21), 42, 'double');
  console.log('q5 passed');
})().catch(e => { console.error(e.message); process.exit(1); });
