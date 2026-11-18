(() => {
  const root = document.documentElement;
  const toggle = document.querySelector('.theme-toggle');
  const system = window.matchMedia('(prefers-color-scheme: dark)');
  const current = () => root.getAttribute('data-theme') || (system.matches ? 'dark' : 'light');
  const label = () => { const next = current() === 'dark' ? 'light' : 'dark'; const text = 'Switch to ' + next + ' mode'; toggle.dataset.next = next; toggle.setAttribute('aria-label', text); toggle.title = text; };
  toggle.addEventListener('click', () => { const next = current() === 'dark' ? 'light' : 'dark'; root.setAttribute('data-theme', next); try { localStorage.setItem('kursal-doc-theme', next); } catch (e) {} label(); });
  if (system.addEventListener) system.addEventListener('change', label);
  label();
})();
