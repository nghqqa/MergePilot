function slugify(s) {
  return s.replace(' ', '-').toLowerCase();
}
module.exports = { slugify };
