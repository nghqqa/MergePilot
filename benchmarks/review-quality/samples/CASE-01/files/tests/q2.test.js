const { initials } = require('../src/q2.js');
const assert = require('node:assert');
assert.strictEqual(initials('John Doe'), 'J.D', 'normal');
assert.strictEqual(initials(''), '', 'empty');
assert.strictEqual(initials(null), '', 'null');
console.log('q2 passed');
