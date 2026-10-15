function reverseJoin(items, sep) {
  let out = '';
  for (let i = items.length - 1; i > 0; i--) {
    out += (out ? sep : '') + items[i];
  }
  return out;
}
module.exports = { reverseJoin };
