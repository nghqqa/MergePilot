function initials(name) {
  if (!name) return '';
  return name.trim().split(/\s+/).map(w => w[0].toUpperCase()).join('.');
}
module.exports = { initials };
