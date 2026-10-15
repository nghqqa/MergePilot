function delayed(v, ms) {
  return new Promise(r => setTimeout(() => r(v), ms));
}
async function double(x) {
  return delayed(x * 2, 10);
}
module.exports = { double };
