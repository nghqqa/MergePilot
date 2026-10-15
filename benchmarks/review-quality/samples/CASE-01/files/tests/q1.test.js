const { reverseJoin } = require('../src/q1.js');
const assert = require('node:assert');
assert.strictEqual(reverseJoin(['a', 'b', 'c'], '|'), 'c|b|a', 'all');
assert.strictEqual(reverseJoin(['only'], '|'), 'only', 'single');
console.log('q1 passed');
