function slugify(s) {
  return s.replace(/ /g, '-').toLowerCase();
}
module.exports = { slugify };
