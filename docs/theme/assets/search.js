// Search over assets/search-index.json, which `kursal doc` writes: one record per item, with its name
// (n), kind (k), path (p), URL from the site's root (u) and summary line (s). The index is loaded on
// first use, from the root the page's `doc-root` meta tag names, so a site served from a
// subdirectory works.
//
// A query without `::` is matched against names, one with `::` against paths; a linear type's `!` is
// ignored on both sides. Results rank, in tiers: an exact match, then a match the name or path starts
// with, each written in the query's case before any other case; then a path segment the query
// starts; then names and paths that contain it; then paths and summaries that contain every word of
// it. Within a tier, types and traits come before the other kinds, then shorter paths first.
//
// `prepare` and `rank` are pure, and Node loads them as a module to test the ranking.
(() => {
  const types = new Set(['struct', 'enum', 'tuple', 'trait', 'primitive', 'builtin']);
  const unmark = (text) => text.replace(/!/g, '');

  const prepare = (records) => records.map((r) => {
    const name = unmark(r.n);
    const path = unmark(r.p);
    return { ...r, key: { name, path, lowerName: name.toLowerCase(), lowerPath: path.toLowerCase(),
      text: (r.p + ' ' + r.s).toLowerCase() } };
  });

  const tier = (key, query, lower, words) => {
    const scoped = query.includes('::');
    const exact = (name, path, q) => (scoped ? path === q || path.endsWith('::' + q) : name === q);
    const starts = (name, path, q) => (scoped ? path.startsWith(q) || path.includes('::' + q) : name.startsWith(q));
    if (exact(key.name, key.path, query)) return 0;
    if (exact(key.lowerName, key.lowerPath, lower)) return 1;
    if (starts(key.name, key.path, query)) return 2;
    if (starts(key.lowerName, key.lowerPath, lower)) return 3;
    if (key.lowerPath.includes('::' + lower)) return 4;
    if (key.lowerName.includes(lower) || key.lowerPath.includes(lower)) return 5;
    if (words.every((w) => key.text.includes(w))) return 6;
    return -1;
  };

  const rank = (index, text, limit) => {
    const query = unmark(text.trim());
    if (!query) return [];
    const lower = query.toLowerCase();
    const words = text.trim().toLowerCase().split(/\s+/);
    const found = [];
    for (const r of index) {
      const t = tier(r.key, query, lower, words);
      if (t >= 0) found.push([t, types.has(r.k) ? 0 : 1, r]);
    }
    found.sort((a, b) => a[0] - b[0] || a[1] - b[1] || a[2].p.length - b[2].p.length
      || (a[2].p < b[2].p ? -1 : a[2].p > b[2].p ? 1 : 0) || (a[2].k < b[2].k ? -1 : a[2].k > b[2].k ? 1 : 0));
    return found.slice(0, limit).map((entry) => entry[2]);
  };

  if (typeof module === 'object' && module.exports) {
    module.exports = { prepare, rank };
    return;
  }

  const input = document.getElementById('doc-search');
  const list = document.getElementById('doc-search-results');
  const status = document.getElementById('doc-search-status');
  if (!input || !list) return;
  const meta = document.querySelector('meta[name="doc-root"]');
  const root = meta && meta.content ? meta.content : '/';
  const base = root.replace(/\/$/, '');
  const limit = 50;
  let index = null;
  let loading = null;
  let shown = [];
  let active = -1;

  const load = () => {
    if (!loading) {
      loading = fetch(root + 'assets/search-index.json')
        .then((response) => response.json())
        .then((records) => { index = prepare(records); })
        .catch(() => { index = []; status.textContent = 'The search index could not be loaded.'; });
    }
    return loading;
  };

  const search = (query) => (index ? rank(index, query, limit) : []);

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
