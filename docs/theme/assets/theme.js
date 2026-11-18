(() => {
  const root = document.documentElement;
  const toggle = document.querySelector('.theme-toggle');
  const current = () => root.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
  const label = () => { const next = current() === 'dark' ? 'light' : 'dark'; const text = 'Switch to ' + next + ' mode'; toggle.dataset.next = next; toggle.setAttribute('aria-label', text); toggle.title = text; };
  toggle.addEventListener('click', () => { const next = current() === 'dark' ? 'light' : 'dark'; root.setAttribute('data-theme', next); try { localStorage.setItem('kursal-doc-theme', next); } catch (e) {} label(); });
  label();
})();
