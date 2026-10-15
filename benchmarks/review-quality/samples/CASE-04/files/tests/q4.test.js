const { inRange } = require('../src/q4.js');
const assert = require('node:assert');
assert.strictEqual(inRange(5, 1, 10), true, 'mid');
assert.strictEqual(inRange(1, 1, 10), true, 'min');
assert.strictEqual(inRange(10, 1, 10), true, 'max');
console.log('q4 passed');
