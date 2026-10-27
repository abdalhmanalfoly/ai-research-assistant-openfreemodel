/* Classic (non-module) script loaded in <head> to set the theme before first paint (avoids a flash). */
(function () {
  try {
    var pref = localStorage.getItem('ara.theme') || 'system';
    var dark = pref === 'dark' || (pref === 'system' && window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
    document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light');
    if (localStorage.getItem('ara.sidebarCollapsed') === '1') document.documentElement.setAttribute('data-sidebar', 'collapsed');
  } catch (e) { /* storage blocked: keep defaults */ }
})();
