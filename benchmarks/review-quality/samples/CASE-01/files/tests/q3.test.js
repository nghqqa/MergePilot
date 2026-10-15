const { pct } = require('../src/q3.js');
const assert = require('node:assert');
assert.strictEqual(pct(3, 9), 33, 'normal');
assert.strictEqual(pct(3, 0), 0, 'zero total');
console.log('q3 passed');
