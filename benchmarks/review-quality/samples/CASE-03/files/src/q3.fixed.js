function pct(part, total) {
  if (!total) return 0;
  return Math.round((part / total) * 100);
}
module.exports = { pct };
