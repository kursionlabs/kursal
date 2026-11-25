// The page head picks the theme before first paint: the saved choice, else the system's.
// This keeps the toggle's label and the browser's theme colour in step and saves a switch,
// under the same key as kursal.dev.
(() => {
  const root = document.documentElement;
  const toggle = document.querySelector('[data-theme-toggle]');
  const themeColor = document.querySelector('meta[name="theme-color"]');
  const update = () => {
    const dark = root.dataset.theme === 'dark';
    const next = `Switch to ${dark ? 'light' : 'dark'} mode`;
    toggle.setAttribute('aria-label', next);
    toggle.title = next;
    toggle.setAttribute('aria-pressed', String(dark));
    if (themeColor) themeColor.content = dark ? '#0F1424' : '#F3F4F8';
  };
  toggle.addEventListener('click', () => {
    root.dataset.theme = root.dataset.theme === 'dark' ? 'light' : 'dark';
    try { localStorage.setItem('kursal-theme', root.dataset.theme); } catch (e) {}
    update();
  });
  update();
})();
