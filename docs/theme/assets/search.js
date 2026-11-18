// Search over assets/search-index.json, which `kursal doc` writes: one record per item, with its name
// (n), kind (k), path (p), URL from the site's root (u) and summary line (s). The index is loaded on
// first use, from the root the page's `doc-root` meta tag names, so a site served from a
// subdirectory works. Results rank an exact name or path first, then names the query starts, then
// path segments it starts, then names and paths that contain it, then summaries that contain
// every word of it.
(() => {
  const input = document.getElementById('doc-search');
  const list = document.getElementById('doc-search-results');
  const status = document.getElementById('doc-search-status');
  if (!input || !list) return;
  const meta = document.querySelector('meta[name="doc-root"]');
  const root = meta && meta.content ? meta.content : '/';
  const base = root.replace(/\/$/, '');
  const order = { module: 0, struct: 1, enum: 1, tuple: 1, trait: 1, primitive: 1, builtin: 1, fn: 2, method: 3, const: 4, instance: 5 };
  const limit = 50;
  let index = null;
  let loading = null;
  let shown = [];
  let active = -1;

  const load = () => {
    if (!loading) {
      loading = fetch(root + 'assets/search-index.json')
        .then((response) => response.json())
        .then((records) => {
          index = records.map((r) => ({ ...r, name: r.n.toLowerCase(), path: r.p.toLowerCase(), text: (r.p + ' ' + r.s).toLowerCase() }));
        })
        .catch(() => { index = []; status.textContent = 'The search index could not be loaded.'; });
    }
    return loading;
  };

  const tier = (r, q, words) => {
    if (r.name === q || r.path === q || r.path.endsWith('::' + q)) return 0;
    if (r.name.startsWith(q)) return 1;
    if (r.path.includes('::' + q)) return 2;
    if (r.name.includes(q)) return 3;
    if (r.path.includes(q)) return 4;
    if (words.every((w) => r.text.includes(w))) return 5;
    return -1;
  };

  const search = (query) => {
    const q = query.trim().toLowerCase();
    if (!q || !index) return [];
    const words = q.split(/\s+/);
    const found = [];
    for (const r of index) {
      const t = tier(r, q, words);
      if (t >= 0) found.push([t, r]);
    }
    found.sort((a, b) => a[0] - b[0] || (order[a[1].k] ?? 9) - (order[b[1].k] ?? 9)
      || a[1].n.length - b[1].n.length || (a[1].p < b[1].p ? -1 : a[1].p > b[1].p ? 1 : 0));
    return found.slice(0, limit).map((pair) => pair[1]);
  };

  const select = (i) => {
    const items = list.querySelectorAll('[role="option"]');
    items.forEach((item, n) => item.setAttribute('aria-selected', n === i ? 'true' : 'false'));
    active = i;
    if (i >= 0 && items[i]) {
      input.setAttribute('aria-activedescendant', items[i].id);
      items[i].scrollIntoView({ block: 'nearest' });
    } else {
      input.removeAttribute('aria-activedescendant');
    }
  };

  const close = () => {
    list.hidden = true;
    input.setAttribute('aria-expanded', 'false');
    select(-1);
  };

  const render = () => {
    shown = search(input.value);
    list.replaceChildren();
    shown.forEach((r, i) => {
      const item = document.createElement('li');
      item.id = 'doc-search-result-' + i;
      item.setAttribute('role', 'option');
      const link = document.createElement('a');
      link.href = base + r.u;
      link.tabIndex = -1;
      const kind = document.createElement('span');
      kind.className = 'search-kind kind-' + r.k;
      kind.textContent = r.k;
      const path = document.createElement('span');
      path.className = 'search-path';
      path.textContent = r.p;
      link.append(kind, path);
      if (r.s) {
        const line = document.createElement('span');
        line.className = 'search-summary';
        line.textContent = r.s;
        link.append(line);
      }
      item.append(link);
      list.append(item);
    });
    const open = input.value.trim() !== '';
    list.hidden = !open;
    input.setAttribute('aria-expanded', open ? 'true' : 'false');
    status.textContent = open ? (shown.length === 0 ? 'No results' : shown.length + (shown.length === limit ? '+' : '') + ' results') : '';
    if (open && shown.length === 0) {
      const none = document.createElement('li');
      none.className = 'search-none';
      none.textContent = index ? 'No results' : 'Loading…';
      list.append(none);
    }
    select(shown.length > 0 ? 0 : -1);
  };

  input.addEventListener('focus', () => { load().then(() => { if (input.value.trim()) render(); }); });
  input.addEventListener('input', () => { load().then(render); });
  input.addEventListener('keydown', (event) => {
    if (event.key === 'ArrowDown' && shown.length > 0) {
      event.preventDefault();
      select((active + 1) % shown.length);
    } else if (event.key === 'ArrowUp' && shown.length > 0) {
      event.preventDefault();
      select(active <= 0 ? shown.length - 1 : active - 1);
    } else if (event.key === 'Enter' && active >= 0 && shown[active]) {
      event.preventDefault();
      window.location.href = base + shown[active].u;
    } else if (event.key === 'Escape') {
      if (input.value) { input.value = ''; render(); } else { input.blur(); }
      close();
    }
  });
  list.addEventListener('mousedown', (event) => event.preventDefault());
  input.addEventListener('blur', close);
  document.addEventListener('keydown', (event) => {
    const target = event.target;
    const typing = target instanceof HTMLElement && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName));
    if (event.key === '/' && !typing && !event.ctrlKey && !event.metaKey && !event.altKey) {
      event.preventDefault();
      input.focus();
      input.select();
    }
  });
})();
