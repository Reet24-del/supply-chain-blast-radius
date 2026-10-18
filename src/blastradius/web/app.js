// Chat client: streams NDJSON events from /api/chat and draws each tool's dependency paths
// next to the answer, while Claude is still writing it.

const $ = (sel, root = document) => root.querySelector(sel);
const conversation = $('#conversation');
const form = $('#ask');
const input = $('#q');
const send = $('#send');

const EXAMPLES = [
  'If npm account qix were phished, what would it reach?',
  'MAL-2025-46974 just dropped. Are we affected?',
  'Which repos install debug?',
  'Where is our next xz?',
];

// Headline numbers pulled out of tool results, in display order.
const FACTS = {
  repos_reached: 'repos reached',
  declared_directly: 'declared directly',
  hidden_transitively: 'hidden transitively',
  repos_exposed: 'repos exposed',
  ranges_accepting: 'ranges accepting',
  repos_affected: 'repos affected',
  packages_maintained: 'packages maintained',
};
const SHOWN_SCALARS = new Set(['team', 'oncall', 'channel', 'about', 'url']);

// Each company (tenant) has its own isolated graph; the choice lives in the URL so it's linkable.
const tenant = new URLSearchParams(location.search).get('tenant') || '';
let sessionId = null;
let busy = false;

try {
  cytoscape.use(cytoscapeDagre);
} catch {
  // the UMD build may have registered itself already
}

function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (v !== false && v != null) node.setAttribute(k, v === true ? '' : v);
  }
  node.append(...children.filter((c) => c != null));
  return node;
}

const fmt = (n) => (typeof n === 'number' ? n.toLocaleString() : (n ?? '?'));
const cell = (v) =>
  Array.isArray(v) ? v.join(', ') : v == null ? '' : typeof v === 'number' && !Number.isInteger(v) ? v.toFixed(4) : String(v);

function badgeClass(outcome, isError) {
  if (isError) return 'danger';
  if (outcome === 'answered') return 'ok';
  if (outcome === 'not_in_graph') return 'warn';
  if (outcome === 'answered_from_memory') return 'violet';
  if (['recorded', 'drafted', 'opened'].includes(outcome)) return 'accent';
  return '';
}

function signature(name, args) {
  const shown = Object.entries(args || {}).map(([k, v]) => `${k}=${JSON.stringify(v)}`);
  return `${name}(${shown.join(', ')})`;
}

// ---------------------------------------------------------------- one question + answer

function newTurn(question) {
  $('#empty')?.remove();
  const t = {
    text: '',
    results: [],
    calls: new Map(),
    cy: null,
    pending: false,
    md: h('div', { class: 'md' }),
    status: h('div', { class: 'status', text: 'Thinking…' }),
    foot: h('div', { class: 'foot' }),
    title: h('h3', { text: 'Evidence' }),
    graph: h('div', { class: 'graph', hidden: true }),
    callList: h('div'),
    paths: h('details', { class: 'paths', hidden: true }),
  };
  t.answer = h('article', { class: 'answer' }, t.status, t.md, t.foot);
  const evidence = h('aside', { class: 'evidence' }, t.title, t.graph, t.callList, t.paths);
  t.section = h('section', { class: 'turn' }, h('p', { class: 'q', text: question }), h('div', { class: 'row' }, t.answer, evidence));
  conversation.append(t.section);
  return t;
}

function renderText(t) {
  if (t.pending) return;
  t.pending = true;
  requestAnimationFrame(() => {
    t.pending = false;
    t.md.innerHTML = DOMPurify.sanitize(marked.parse(t.text.trim()));
  });
}

function handle(t, ev) {
  switch (ev.type) {
    case 'session':
      sessionId = ev.session_id;
      break;
    case 'status':
      t.status.hidden = false;
      t.status.textContent = 'Thinking…';
      break;
    case 'text':
      t.text += ev.delta;
      if (t.text.trim()) t.status.hidden = true;
      renderText(t);
      break;
    case 'tool_start':
      callCard(t, ev.id, ev.name);
      t.status.hidden = false;
      t.status.textContent = `Querying the graph: ${ev.name}…`;
      break;
    case 'tool_call':
      callCard(t, ev.id, ev.name).sig.textContent = signature(ev.name, ev.input);
      break;
    case 'tool_result':
      t.results.push(ev);
      fillCard(t, ev);
      drawPaths(t);
      t.status.textContent = 'Thinking…';
      break;
    case 'notice':
    case 'error':
      t.answer.append(h('p', { class: ev.type === 'error' ? 'notice error' : 'notice', text: ev.text || ev.message }));
      break;
    case 'done':
      t.status.hidden = true;
      break;
  }
}


// ---------------------------------------------------------------- evidence: tool calls

function callCard(t, id, name) {
  let card = t.calls.get(id);
  if (!card) {
    card = {
      sig: h('span', { class: 'sig', text: `${name}(…)` }),
      badge: h('span', { class: 'badge', text: 'running' }),
      ms: h('span', { class: 'ms' }),
      body: h('div'),
    };
    t.calls.set(id, card);
    t.callList.append(h('div', { class: 'call' }, h('div', { class: 'call-head' }, card.sig, card.badge, card.ms), card.body));
    updateTitle(t);
  }
  return card;
}

function fillCard(t, ev) {
  const card = callCard(t, ev.id, ev.name);
  const r = ev.result || {};
  card.badge.textContent = (ev.is_error ? 'error' : r.outcome || 'done').replaceAll('_', ' ');
  card.badge.className = `badge ${badgeClass(r.outcome, ev.is_error)}`;
  if (typeof r.query_ms === 'number') card.ms.textContent = `${r.query_ms} ms`;

  const facts = Object.entries(FACTS)
    .filter(([k]) => r[k] != null)
    .map(([k, label]) => h('span', { class: 'fact' }, h('b', { text: fmt(r[k]) }), ` ${label}`));
  const scalars = Object.entries(r)
    .filter(([k, v]) => SHOWN_SCALARS.has(k) && v != null && typeof v !== 'object')
    .map(([k, v]) => h('span', { class: 'fact' }, `${k}: `, h('b', { text: String(v) })));
  if (facts.length || scalars.length) card.body.append(h('div', { class: 'facts' }, ...facts, ...scalars));

  for (const [key, value] of Object.entries(r)) {
    if (Array.isArray(value) && value.length) {
      const view = listView(key, value);
      if (view) card.body.append(view);
    } else if (value && typeof value === 'object' && !Array.isArray(value)) {
      card.body.append(objectView(value));
    }
  }
  const hints = [r.error, r.suggestions?.length ? `Did you mean: ${r.suggestions.join(', ')}?` : null];
  for (const text of hints) if (text) card.body.append(h('p', { class: 'note', text }));
}

function listView(key, items) {
  if (typeof items[0] !== 'object') {
    const shown = items.slice(0, 8).map((v) => h('span', { text: String(v) }));
    const more = items.length > 8 ? h('em', { text: `+${items.length - 8} more` }) : null;
    return h('div', { class: 'chips' }, h('em', { text: `${key}:` }), ...shown, more);
  }
  // rows that carry a path are drawn in the graph; advisories also get a table for ids and severity
  if (items[0].path && key !== 'advisories') return null;
  const cols = Object.keys(items[0]).filter(
    (c) => c !== 'path' && (Array.isArray(items[0][c]) || typeof items[0][c] !== 'object' || items[0][c] === null),
  );
  const head = h('thead', {}, h('tr', {}, ...cols.map((c) => h('th', { text: c }))));
  const body = h('tbody', {}, ...items.slice(0, 5).map((it) => h('tr', {}, ...cols.map((c) => h('td', { text: cell(it[c]) })))));
  const table = h('table', { class: 'mini' }, head, body);
  return items.length > 5 ? h('div', {}, table, h('p', { class: 'note', text: `+${items.length - 5} more rows` })) : table;
}

function objectView(obj) {
  const rows = Object.entries(obj)
    .filter(([, v]) => v != null && v !== '' && !(Array.isArray(v) && !v.length))
    .map(([k, v]) => {
      const text = Array.isArray(v) ? v.slice(0, 6).join(', ') + (v.length > 6 ? ' …' : '') : String(v);
      const value = /^https:\/\//.test(text) ? h('a', { href: text, target: '_blank', rel: 'noopener', text }) : text;
      return h('tr', {}, h('th', { text: k }), h('td', {}, value));
    });
  return h('table', { class: 'mini' }, h('tbody', {}, ...rows));
}

// ---------------------------------------------------------------- evidence: paths

function collectPaths(results) {
  const out = [];
  for (const { result } of results) {
    for (const value of Object.values(result || {})) {
      if (!Array.isArray(value)) continue;
      for (const item of value) if (item && Array.isArray(item.path) && item.path.length > 1) out.push(item.path);
    }
  }
  return out;
}

function updateTitle(t, pathCount = collectPaths(t.results).length) {
  const n = t.calls.size;
  const detail = `· ${n} tool call${n === 1 ? '' : 's'}${pathCount ? ` · ${pathCount} paths` : ''}`;
  t.title.replaceChildren('Evidence ', h('span', { text: detail }));
}

function pathItem(path) {
  const hops = [];
  path.forEach((id, i) => {
    if (i) hops.push(h('span', { class: 'sep', text: '›' }));
    const role = i === 0 ? 'repo' : i === path.length - 1 ? 'target' : '';
    hops.push(h('span', { class: `hop ${role}`, text: id }));
  });
  return h('li', {}, ...hops);
}

function graphStyle() {
  const css = getComputedStyle(document.documentElement);
  const v = (name) => css.getPropertyValue(name).trim();
  return [
    {
      selector: 'node',
      style: {
        label: 'data(label)',
        shape: 'round-rectangle',
        width: (n) => Math.max(40, n.data('label').length * 6.6 + 18),
        height: 22,
        'font-size': 10.5,
        'font-family': 'ui-monospace, Menlo, monospace',
        'text-valign': 'center',
        'text-halign': 'center',
        'background-color': v('--node-dep'),
        color: v('--node-dep-text'),
        'border-width': 1,
        'border-color': v('--border'),
      },
    },
    { selector: 'node.repo', style: { 'background-color': v('--node-repo'), color: '#fff', 'border-width': 0, 'font-weight': 'bold' } },
    { selector: 'node.target', style: { 'background-color': v('--node-target'), color: '#fff', 'border-width': 0, 'font-weight': 'bold' } },
    {
      selector: 'edge',
      style: {
        width: 1.2,
        'line-color': v('--edge'),
        'target-arrow-color': v('--edge'),
        'target-arrow-shape': 'triangle',
        'arrow-scale': 0.7,
        'curve-style': 'bezier',
      },
    },
  ];
}

function drawPaths(t) {
  const paths = collectPaths(t.results).slice(0, 120);
  updateTitle(t, paths.length);
  if (!paths.length) return;

  t.paths.hidden = false;
  t.paths.replaceChildren(h('summary', { text: `All ${paths.length} paths, hop by hop` }), h('ol', {}, ...paths.map(pathItem)));

  // One node per package version; a node that ends any path is drawn as the target.
  const rank = { dep: 0, repo: 1, target: 2 };
  const roles = new Map();
  const edges = new Map();
  for (const p of paths) {
    p.forEach((id, i) => {
      const role = i === 0 ? 'repo' : i === p.length - 1 ? 'target' : 'dep';
      if (!roles.has(id) || rank[role] > rank[roles.get(id)]) roles.set(id, role);
      if (i) edges.set(`${p[i - 1]} -> ${id}`, { source: p[i - 1], target: id });
    });
  }
  t.graph.hidden = false;
  t.cy?.destroy();
  t.cy = cytoscape({
    container: t.graph,
    elements: [
      ...[...roles].map(([id, role]) => ({ data: { id, label: id }, classes: role })),
      ...[...edges].map(([id, e]) => ({ data: { id, ...e } })),
    ],
    style: graphStyle(),
    layout: { name: 'dagre', rankDir: 'LR', nodeSep: 8, rankSep: 46, edgeSep: 6 },
    autoungrabify: true,
    boxSelectionEnabled: false,
    wheelSensitivity: 0.25,
    minZoom: 0.1,
    maxZoom: 3,
  });
  t.cy.fit(undefined, 16);
}

// ---------------------------------------------------------------- chat plumbing

async function ask(question) {
  question = question.trim();
  if (!question || busy) return;
  busy = true;
  send.disabled = true;
  input.value = '';
  autosize();
  const t = newTurn(question);
  t.section.scrollIntoView({ behavior: 'smooth', block: 'start' });
  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: question, session_id: sessionId, tenant }),
    });
    if (!res.ok || !res.body) {
      const detail = (await res.json().catch(() => ({}))).detail;
      throw new Error(typeof detail === 'string' ? detail : `Request failed (HTTP ${res.status}).`);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let nl;
      while ((nl = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, nl).trim();
        buffer = buffer.slice(nl + 1);
        if (line) handle(t, JSON.parse(line));
      }
    }
  } catch (err) {
    // fetch rejects with a TypeError when the server can't be reached at all
    handle(t, { type: 'error', message: err instanceof TypeError ? "Couldn't reach the server." : err.message });
  } finally {
    t.status.hidden = true;
    busy = false;
    send.disabled = false;
    input.focus();
  }
}

async function loadHealth() {
  try {
    const res = await fetch(`/api/health?tenant=${encodeURIComponent(tenant)}`);
    const s = await res.json();
    if (!res.ok) {
      $('#stats').textContent = s.detail || 'Graph unavailable.';
      return;
    }
    showTenants(s.tenants || []);
    if (!s.nodes.Repo) {
      $('#stats').textContent = 'The graph is empty. Load it with blast-ingest (see the README).';
      return;
    }
    $('#stats').textContent =
      `${fmt(s.nodes.Repo)} repos · ${fmt(s.edges.DEPENDS_ON)} dependency edges`;
    $('#model').textContent = `${s.model.split('/').pop()} on FalkorDB`;
    const setup = $('#setup');
    if (setup) setup.hidden = s.llm_ready;
  } catch {
    $('#stats').textContent = 'Graph unavailable. Is FalkorDB running?';
  }
}

function showTenants(names) {
  if (!names.length) return; // only the default graph: no picker
  const select = $('#tenant');
  select.replaceChildren(
    h('option', { value: '', text: 'Default' }),
    ...names.map((name) => h('option', { value: name, text: name })),
  );
  select.value = tenant;
  $('#tenant-picker').hidden = false;
}

function autosize() {
  input.style.height = 'auto';
  input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
}

$('#examples').append(
  ...EXAMPLES.map((q) => {
    const b = h('button', { type: 'button', text: q });
    b.addEventListener('click', () => ask(q));
    return b;
  }),
);
form.addEventListener('submit', (e) => {
  e.preventDefault();
  ask(input.value);
});
input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    ask(input.value);
  }
});
input.addEventListener('input', autosize);
$('#tenant').addEventListener('change', (e) => {
  const value = e.target.value;
  location.search = value ? `?tenant=${encodeURIComponent(value)}` : '';
});
$('#new').addEventListener('click', () => {
  if (!busy) location.reload();
});
const greet = $('#greet');
if (greet) {
  const hour = new Date().getHours();
  greet.textContent = hour < 12 ? 'Good morning' : hour < 18 ? 'Good afternoon' : 'Good evening';
}
loadHealth();
