function initials(name) {
  return name.trim().split(/\s+/).map(w => w[0].toUpperCase()).join('.');
}
module.exports = { initials };
