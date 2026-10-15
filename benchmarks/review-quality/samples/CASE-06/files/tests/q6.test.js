const { slugify } = require('../src/q6.js');
const assert = require('node:assert');
assert.strictEqual(slugify('a b c'), 'a-b-c', 'three');
console.log('q6 passed');
