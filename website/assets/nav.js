/* 站点导航行为：移动端菜单展开/关闭、Escape 关闭并还焦点。
   依赖契约：<html class="no-js"> 由本脚本移除——脚本加载失败时
   CSS 回退为常显菜单（html.no-js .nav-links），导航仍完全可用。 */
(function () {
  'use strict';
  function initNav(doc) {
    var root = doc.documentElement;
    root.classList.remove('no-js');
    root.classList.add('js');
    var toggle = doc.querySelector('.nav-toggle');
    var nav = doc.querySelector('.nav');
    var menu = doc.getElementById('nav-menu');
    if (!toggle || !nav || !menu) return;
    if (nav.dataset.navInit === '1') return; // 双重引用时只绑一次
    nav.dataset.navInit = '1';
    function setOpen(open) {
      nav.classList.toggle('nav-open', open);
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    }
    toggle.addEventListener('click', function () {
      setOpen(!nav.classList.contains('nav-open'));
    });
    menu.addEventListener('click', function (e) {
      if (e.target && e.target.closest && e.target.closest('a')) setOpen(false);
    });
    doc.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && nav.classList.contains('nav-open')) {
        setOpen(false);
        toggle.focus();
      }
    });
  }
  if (typeof document !== 'undefined') initNav(document);
  if (typeof module !== 'undefined' && module.exports) module.exports = { initNav: initNav };
})();
