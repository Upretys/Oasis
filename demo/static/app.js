/* Oasis front end. One page, driven by the URL hash:
   #/                      home (search, filters, fast facts)
   #/b/<id>/<tab>          a building: verdict | floors | area | boardroom
   #/b/<id>/quotes         the email agent's quote requests            */
'use strict';

// ---------- small helpers ----------
const $ = (s, r = document) => r.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const money = x => {
  if (x == null) return 'n/a';
  const a = Math.abs(x), s = x < 0 ? '-' : '';
  return a >= 1e6 ? `${s}$${(a / 1e6).toFixed(1)}M` : a >= 1e3 ? `${s}$${Math.round(a / 1e3)}K` : `${s}$${Math.round(a)}`;
};
const range = (lo, hi) => (lo == null || hi == null) ? 'n/a' : `${money(lo)} to ${money(hi)}`;
const num = x => x == null ? 'n/a' : Math.round(x).toLocaleString();
const flr = (a, cap) => a.floor != null ? `${cap ? 'Floor' : 'floor'} ${a.floor}` : (cap ? 'Typical floor' : 'a typical floor');
const sleep = ms => new Promise(r => setTimeout(r, ms));

const ICONS = {
  menu: '<path d="M4 7h16M4 12h16M4 17h16"/>', search: '<circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.5 4.5"/>',
  expand: '<path d="M14 4h6v6M10 20H4v-6M20 4l-7 7M4 20l7-7"/>', shrink: '<path d="M20 10h-6V4M4 14h6v6M14 10l7-7M10 14l-7 7"/>',
  user: '<circle cx="12" cy="8.5" r="3.6"/><path d="M4.5 20c1.2-3.6 4-5.4 7.5-5.4s6.3 1.8 7.5 5.4"/>',
  check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>', x: '<path d="M6 6l12 12M18 6L6 18"/>',
  alert: '<path d="M12 4l9 16H3z"/><path d="M12 10v4.5M12 17.4v.2"/>', download: '<path d="M12 4v11M7 10.5l5 5 5-5M5 20h14"/>',
  plus: '<path d="M12 5v14M5 12h14"/>', minus: '<path d="M5 12h14"/>', compass: '<circle cx="12" cy="12" r="8.5"/><path d="M15.5 8.5l-2 5-5 2 2-5z"/>',
  layers: '<path d="M12 4l9 5-9 5-9-5z"/><path d="M3 14l9 5 9-5"/>',
  spark: '<path d="M12 3v5M12 16v5M3 12h5M16 12h5M6 6l3 3M15 15l3 3M18 6l-3 3M9 15l-3 3"/>',
  chev: '<path d="M9 6l6 6-6 6"/>', back: '<path d="M15 6l-6 6 6 6"/>',
  pin: '<path d="M12 21s6.5-6 6.5-11a6.5 6.5 0 10-13 0c0 5 6.5 11 6.5 11z"/><circle cx="12" cy="10" r="2.3"/>',
  filter: '<path d="M4 7h10M18 7h2M4 17h4M12 17h8"/><circle cx="16" cy="7" r="2"/><circle cx="10" cy="17" r="2"/>',
  mail: '<rect x="3" y="5.5" width="18" height="13" rx="2"/><path d="M3.5 7l8.5 6 8.5-6"/>',
  home: '<path d="M4 11l8-7 8 7M6 10v9h12v-9"/>',
};
const ic = (n, size = 18, sw = 1.8) => `<svg class="ic" aria-hidden="true" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="${sw}" stroke-linecap="round" stroke-linejoin="round">${ICONS[n]}</svg>`;

const KIND = {
  hospital: { letter: 'H', color: '#D6404A', label: 'Hospital' }, school: { letter: 'S', color: '#2F6FEB', label: 'School' },
  grocery: { letter: 'G', color: '#1F8A4C', label: 'Grocery store' }, highway: { letter: 'R', color: '#7A56E0', label: 'Major highway' },
};
const SRC_COLOR = { cached: '#5FE0CB', live: '#FFC65C', scan: '#A9B8FF' };
const STATE_WORD = { pass: 'Pass', review: 'Review', fail: 'Fail' };
const STATE_ICON = { pass: 'check', review: 'alert', fail: 'x', for: 'check', conditional: 'alert', against: 'x' };
const STANCE_WORD = { for: 'For', conditional: 'Conditional', against: 'Against' };
const UNIT = { studio: { label: 'Studio', long: 'Studios', color: '#BFE9E1' }, '1br': { label: '1 bed', long: 'One-bedroom', color: '#FFE2A6' }, '2br': { label: '2 bed', long: 'Two-bedroom', color: '#D6CCF7' } };
const MIXES = [['balanced', 'Balanced'], ['studios', 'More studios'], ['larger', 'Larger apartments']];

async function api(path, body) {
  const res = await fetch(path, body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : undefined);
  let data = null;
  try { data = await res.json(); } catch (e) { /* not JSON */ }
  if (!res.ok) throw new Error((data && data.message) || `The server answered ${res.status}.`);
  return data;
}

function toast(msg) {
  const t = $('#toast');
  t.textContent = msg; t.hidden = false;
  clearTimeout(toast.timer); toast.timer = setTimeout(() => { t.hidden = true; }, 3600);
}

function download(name, text, type) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = document.createElement('a');
  a.href = url; a.download = name; document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

// ---------- state ----------
const S = {
  ov: null, byId: {}, view: 'home', tab: 'verdict', full: false,
  q: '', showFilters: false, near: { hospital: false, school: false, grocery: false, highway: false }, dist: 1, positiveOnly: false, showAll: false,
  sel: null, armed: false, an: null, anLoading: false, anErr: null, floorErr: null,
  floors: null, floor: null, area: null, areaErr: null, placeKind: 'all',
  plan: null, planOpen: false, planMix: 'balanced', planView: 'after', planUnit: null, planLoading: false,
  board: newBoard(), modal: false, preview: true, quotes: null,
};
function newBoard() { return { turns: [], speaking: null, running: false, done: false, token: 0, err: null }; }

const filtersActive = () => Object.values(S.near).some(Boolean) || S.positiveOnly;
const filterCount = () => Object.values(S.near).filter(Boolean).length + (S.positiveOnly ? 1 : 0);
function matches(b) {
  const q = S.q.trim().toLowerCase();
  if (q && !(`${b.name} ${b.address || ''}`.toLowerCase().includes(q))) return false;
  for (const k of Object.keys(S.near)) if (S.near[k] && !(b.near && b.near[k] != null && b.near[k] <= S.dist)) return false;
  if (S.positiveOnly && !(b.impact_low != null && b.impact_low > 0)) return false;
  return true;
}
const searching = () => S.q.trim() !== '' || filtersActive();

// ---------- routing ----------
const go = hash => { if (location.hash === hash) route(); else location.hash = hash; };
const bHash = (tab, id = S.sel && S.sel.property_id) => `#/b/${id}/${tab}`;

function route() {
  const parts = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean);
  S.modal = false;
  if (parts[0] === 'b' && S.byId[parts[1]]) {
    const b = S.byId[parts[1]], tab = parts[2] || 'verdict';
    if (!S.sel || S.sel.property_id !== b.property_id) selectBuilding(b, true);
    if (tab === 'quotes') { S.view = S.quotes ? 'quotes' : 'building'; if (!S.quotes) S.tab = 'boardroom'; }
    else { S.view = 'building'; S.tab = ['verdict', 'floors', 'area', 'boardroom'].includes(tab) ? tab : 'verdict'; }
    if (S.tab !== 'floors') S.planOpen = false;
    if (S.view === 'building' && !S.armed) {
      if (S.tab === 'area') loadArea();
      if (S.tab === 'boardroom') startBoard();
    }
  } else {
    if (S.sel) clearSelection();
    S.view = 'home';
  }
  render();
}

// ---------- selection and analysis ----------
function clearSelection() {
  S.sel = null; S.an = null; S.armed = false; S.anErr = null; S.floors = null; S.area = null; S.plan = null; S.planOpen = false;
  S.board.token++; S.board = newBoard(); S.quotes = null;
  M.clearSelection();
}

function selectBuilding(b, analyzeNow) {
  clearSelection();
  S.sel = b; S.floor = b.floor_name ?? null; S.armed = !analyzeNow;
  M.focus(b, S.armed);
  if (analyzeNow) runAnalysis(b.floor_name);
}

async function runAnalysis(floor) {
  const b = S.sel;
  S.armed = false; S.anLoading = true; S.anErr = null; S.floorErr = null;
  M.closeCallout();
  render();
  try {
    const [an, floors] = await Promise.all([
      api('/api/analyze', { property_id: b.property_id, floor_name: floor }),
      S.floors ? Promise.resolve(S.floors) : api(`/api/floors?property_id=${b.property_id}`).catch(() => null),
    ]);
    if (S.sel !== b) return;
    S.an = an; S.floors = floors; S.floor = an.floor; S.plan = an.floorplan; S.planMix = 'balanced'; S.planUnit = null;
    S.board.token++; S.board = newBoard(); S.area = S.area;
    M.drawStack();
    if (S.tab === 'boardroom') startBoard();
    if (S.tab === 'area') loadArea();
  } catch (e) {
    if (S.sel !== b) return;
    if (S.an) { S.floorErr = e.message; } else { S.anErr = e.message; }
  }
  S.anLoading = false;
  render();
}

async function loadArea() {
  if (S.area || !S.sel) return;
  const b = S.sel;
  try { const a = await api(`/api/area?property_id=${b.property_id}`); if (S.sel === b) { S.area = a; } }
  catch (e) { if (S.sel === b) S.areaErr = e.message; }
  render();
}

function floorList() {
  const fl = (S.floors && S.floors.floors) || [];
  const an = S.an, real = S.floors && S.floors.source === 'costar';
  return fl.map(f => {
    const analyzed = an && f.floor === an.floor;
    const best = real ? f.status === 'vacant' : (analyzed && an.headline.state === 'pass');
    return { ...f, analyzed, best };
  });
}
const STATUS_TEXT = { vacant: 'Vacant', occupied: 'Leased', no_record: 'No lease record', unknown: 'Lease status not checked' };

async function loadPlan(mix) {
  S.planLoading = true; S.planUnit = null; render();
  try { S.plan = await api('/api/floorplan', { property_id: S.sel.property_id, mix }); S.planMix = S.plan.mix; }
  catch (e) { toast(e.message); }
  S.planLoading = false; render();
}

// ---------- boardroom ----------
function finalStances() {
  const out = {};
  for (const t of S.board.turns) out[t.agent] = t;
  return out;
}
function tally() {
  const c = { for: 0, conditional: 0, against: 0 };
  Object.values(finalStances()).forEach(t => { c[t.stance]++; });
  return c;
}
function boardLine(c) {
  if (c.against >= 2) return 'The board advises against converting';
  if (c.against === 1) return 'The board is split';
  return c.for >= 2 ? 'The board supports converting' : 'The board supports it, with conditions';
}

async function startBoard(force) {
  const bd = S.board;
  if (!S.an || bd.running || (bd.done && !force)) return;
  S.board = newBoard(); S.board.token = bd.token + 1; S.board.running = true;
  const me = S.board, b = S.sel, token = me.token;
  render();
  try {
    for (const rnd of [1, 2]) {
      for (const a of S.ov.agents) {
        me.speaking = a.id; me.round = rnd; render(); scrollPanel();
        const t0 = Date.now();
        const turn = await api('/api/boardroom/turn', {
          property_id: b.property_id, floor: S.an.floor, agent: a.id, round: rnd,
          transcript: me.turns.map(t => ({ agent: t.agent, stance: t.stance, text: t.text })),
        });
        if (!turn.live) await sleep(Math.max(0, 1100 - (Date.now() - t0)));
        if (S.board !== me || me.token !== token) return;
        me.turns.push(turn); render(); scrollPanel();
      }
    }
    me.done = true;
  } catch (e) { me.err = e.message; }
  me.running = false; me.speaking = null;
  if (S.board === me) { render(); }
}
function scrollPanel() { const p = $('#panel'); if (S.tab === 'boardroom' && S.board.running) p.scrollTop = p.scrollHeight; }

// ---------- quotes ----------
async function openQuotes() {
  const b = S.sel;
  S.modal = false;
  try {
    const d = await api('/api/quotes/draft', { property_id: b.property_id, floor: S.an.floor });
    S.quotes = { stage: 'draft', subject: d.subject, body: d.body, live: d.live, contractors: d.contractors, picked: new Set(d.contractors.map(c => c.id)), willSend: d.will_send, results: null, sending: false };
    if (!S.preview) { go(bHash('quotes')); await sendQuotes(); } else go(bHash('quotes'));
  } catch (e) { toast(e.message); render(); }
}
function readDraft() {
  const s = $('#mailSubject'), t = $('#mailBody');
  if (s) S.quotes.subject = s.value;
  if (t) S.quotes.body = t.value;
}
async function sendQuotes() {
  const q = S.quotes;
  readDraft();
  if (!q.picked.size) { toast('Pick at least one contractor.'); return; }
  q.sending = true; render();
  try {
    const r = await api('/api/quotes/send', {
      subject: q.subject, body: q.body, contractor_ids: [...q.picked],
      report_html: buildReport(), plan_svg: S.plan ? planSvg(S.plan, 'after', null, true) : null,
    });
    q.results = {}; r.results.forEach(x => { q.results[x.id] = x; });
    q.mode = r.mode; q.stage = 'sent';
    toast(r.mode === 'smtp' ? 'Quote requests sent.' : 'Quote requests saved to the outbox.');
  } catch (e) { toast(e.message); }
  q.sending = false; render();
}

// ---------- report ----------
function buildReport() {
  const a = S.an; if (!a) return '';
  const n = a.numbers, fs = finalStances();
  const rows = a.checks.map(c => `<tr><td>${esc(c.title)}</td><td class="${c.state}">${STATE_WORD[c.state]}</td><td>${esc(c.note)}</td></tr>`).join('');
  const board = S.board.done ? `<h2>Boardroom</h2><p>${esc(boardLine(tally()))}.</p><table>${S.ov.agents.map(g => fs[g.id] ? `<tr><td>${esc(g.name)}</td><td>${STANCE_WORD[fs[g.id].stance]}</td><td>${esc(fs[g.id].summary)}</td></tr>` : '').join('')}</table>
    <h3>Discussion</h3>${S.board.turns.map(t => `<p><b>${esc(S.ov.agents.find(g => g.id === t.agent).name)}</b> (round ${t.round}): ${esc(t.text)}</p>`).join('')}` : '';
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Oasis report: ${esc(a.building.name)}, ${flr(a)}</title>
<style>body{font:15px/1.55 system-ui,sans-serif;color:#0E1A1F;max-width:820px;margin:40px auto;padding:0 24px}h1{font-size:28px;margin:0}h2{margin:32px 0 8px;font-size:19px}h3{font-size:15px;margin:20px 0 6px}
table{border-collapse:collapse;width:100%}td,th{border-top:1px solid #DCE3E2;padding:8px 10px 8px 0;text-align:left;vertical-align:top}.pass{color:#0B6E61;font-weight:600}.review{color:#8A5200;font-weight:600}.fail{color:#B3261E;font-weight:600}
.muted{color:#4A5B63}img,svg{max-width:100%}ul{padding-left:20px}</style></head><body>
<p class="muted">Oasis conversion feasibility report · ${new Date().toLocaleDateString()} · ${a.source === 'live' ? 'live pipeline run' : a.source === 'saved' ? 'saved result of the last pipeline run' : 'portfolio scan result only'}</p>
<h1>${esc(a.building.name)}, ${flr(a)}</h1>
<p class="${a.headline.state}" style="font-size:20px">${esc(a.headline.label)}${a.confidence != null ? ` · ${Math.round(a.confidence * 100)}% confidence in the shape check` : ''}</p>
<h2>Checks</h2><table>${rows}</table>
<h2>The numbers</h2><table>
<tr><td>Usable apartment area</td><td>${n.viable_sqft ? num(n.viable_sqft) + ' sq ft' : 'n/a'}</td></tr>
<tr><td>Apartments</td><td>${n.total_units || 'n/a'}</td></tr>
<tr><td>Cost of renovation (at $150 to $220 a sq ft, an assumption)</td><td>${range(n.reno_low, n.reno_high)}</td></tr>
<tr><td>Extra rent a year</td><td>${range(n.uplift_low, n.uplift_high)}</td></tr>
<tr><td>Value added after construction</td><td>${range(n.impact_low, n.impact_high)}</td></tr>
<tr><td>Apartment rent nearby</td><td>${n.residential_rent ? '$' + n.residential_rent.toFixed(2) + ' a sq ft a year' : 'n/a'}</td></tr>
<tr><td>Office rent given up</td><td>${n.vacant ? '$0, the floor is vacant' : n.office_rent ? '$' + n.office_rent.toFixed(2) + ' a sq ft a year' : 'n/a'}</td></tr></table>
${a.reasoning_text ? `<h2>Reasoning</h2><p>${esc(a.reasoning_text)}</p>` : ''}
${a.notes && a.notes.length ? `<h2>Notes and limits</h2><ul>${a.notes.map(x => `<li>${esc(x)}</li>`).join('')}</ul>` : ''}
${board}
${S.plan ? `<h2>Tentative floor plan</h2>${planSvg(S.plan, 'after', null, true)}<p class="muted">Drawn from the aerial footprint scaled to the recorded floor area. A starting point for an architect, not a permit drawing.</p>` : ''}
${a.image_base64 ? `<h2>Pipeline render</h2><img alt="Floor plan render from the pipeline" src="data:image/png;base64,${a.image_base64}">` : ''}
</body></html>`;
}
function downloadReport() {
  if (!S.an) return;
  download(`oasis-report-${S.an.building.name.toLowerCase().replace(/[^a-z0-9]+/g, '-')}-${S.an.floor != null ? 'floor-' + S.an.floor : 'typical-floor'}.html`, buildReport(), 'text/html');
  toast('Report downloaded.');
}

// ---------- floor plan drawing ----------
function planSvg(plan, view, picked, standalone) {
  const pts = plan.outline_ft, xs = pts.map(p => p[0]), ys = pts.map(p => p[1]);
  const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
  const pad = 14, w = maxX - minX, h = maxY - minY, W = 760, H = Math.max(240, Math.round(W * h / w));
  const k = Math.min((W - 2 * pad) / w, (H - 2 * pad) / h);
  const X = x => (pad + (x - minX) * k + (W - 2 * pad - w * k) / 2).toFixed(1), Y = y => (H - pad - (y - minY) * k - (H - 2 * pad - h * k) / 2).toFixed(1);
  const poly = ring => ring.map(p => `${X(p[0])},${Y(p[1])}`).join(' ');
  let body = `<polygon points="${poly(pts)}" fill="#F6F8F8" stroke="#0E1A1F" stroke-width="3" stroke-linejoin="round"/>`;
  if (view === 'after') {
    (plan.core_ft || []).forEach(r => { body += `<polygon points="${poly(r)}" fill="#D5DDDF" stroke="#0E1A1F" stroke-width="1"/>`; });
    plan.units.forEach((u, i) => {
      const ux = u.polygon_ft.map(p => p[0]), uy = u.polygon_ft.map(p => p[1]);
      const cx = (Math.min(...ux) + Math.max(...ux)) / 2, cy = (Math.min(...uy) + Math.max(...uy)) / 2;
      const bw = (Math.max(...ux) - Math.min(...ux)) * k, bh = (Math.max(...uy) - Math.min(...uy)) * k;
      const spec = UNIT[u.type] || { label: u.type, color: '#DDD' };
      body += `<polygon class="unit${picked === i ? ' on' : ''}" data-act="unit" data-i="${i}" points="${poly(u.polygon_ft)}" fill="${spec.color}" stroke="#0E1A1F" stroke-width="1.2" stroke-linejoin="round"><title>${esc(spec.label)}, ${num(u.area_sqft)} sq ft</title></polygon>`;
      if (bw > 44 && bh > 18) body += `<text x="${X(cx)}" y="${(+Y(cy) + 4).toFixed(1)}" text-anchor="middle" font-family="Inter, sans-serif" font-size="11" font-weight="600" fill="#0E1A1F" pointer-events="none">${esc(spec.label)}</text>`;
    });
    if (plan.core_ft && plan.core_ft.length) {
      const c = plan.core_ft[0], cx = c.reduce((s, p) => s + p[0], 0) / c.length, cy = c.reduce((s, p) => s + p[1], 0) / c.length;
      body += `<text x="${X(cx)}" y="${Y(cy)}" text-anchor="middle" font-family="Inter, sans-serif" font-size="12" font-weight="600" fill="#0E1A1F" pointer-events="none">Core</text>`;
    }
  } else {
    body += `<text x="${W / 2}" y="${H / 2}" text-anchor="middle" font-family="Inter, sans-serif" font-size="14" fill="#4A5B63">Open office floor, ${num(plan.floor_sqft)} sq ft</text>`;
  }
  const label = `Tentative floor plan with ${plan.units.length} apartments around a central core`;
  return `<svg ${standalone ? 'xmlns="http://www.w3.org/2000/svg" ' : ''}role="img" aria-label="${label}" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">${body}</svg>`;
}

// ---------- sidebar rendering ----------
function render() {
  $('#layout').classList.toggle('full', S.full);
  const fb = $('#filterBtn');
  fb.classList.toggle('on', S.showFilters || filtersActive());
  fb.setAttribute('aria-expanded', String(S.showFilters));
  fb.innerHTML = ic('filter', 20) + (filterCount() ? `<span class="badge">${filterCount()}</span>` : '');
  $('#panel').innerHTML = S.view === 'home' ? homePanel() : S.view === 'quotes' ? quotesPanel() : buildingPanel();
  $('#mapui').innerHTML = mapUi();
  const pressed = $('.floorlist .floor[aria-pressed="true"]');
  if (pressed) pressed.scrollIntoView({ block: 'nearest' });
  M.sync();
}

function buildingRow(b) {
  const near = Object.keys(S.near).filter(k => S.near[k]).map(k => `<span class="row" style="gap:5px"><span class="glyph" style="width:18px;height:18px;font-size:10px;background:${KIND[k].color}">${KIND[k].letter}</span>${b.near[k].toFixed(1)} mi</span>`).join('');
  const state = b.impact_low == null ? (b.verdict ? 'Screened' : 'Not analyzed yet') : b.impact_low > 0 ? 'Adds value' : 'Loses value';
  const val = b.impact_low == null ? '' : `<span class="val ${b.impact_low > 0 ? 'pos' : 'neg'}">${range(b.impact_low, b.impact_high)}</span>`;
  return `<button class="item" data-act="open" data-id="${b.property_id}"><span class="dot" style="background:${SRC_COLOR[b.source]}"></span>
    <span class="grow"><span class="name">${esc(b.name)}</span><span class="sub"><span>${state}</span>${near}</span></span>${val}${ic('chev', 16)}</button>`;
}

function filterCard() {
  const hasPlaces = S.ov.sources.places;
  return `<div class="card outline col" style="gap:10px">
    <div class="row between"><h2 class="h2">Filter by what is nearby</h2>${filtersActive() ? '<button class="linkbtn" data-act="clear-filters">Clear</button>' : ''}</div>
    ${hasPlaces ? `<div class="chips">${Object.keys(KIND).map(k => `<button class="chip" data-act="near" data-kind="${k}" aria-pressed="${S.near[k]}">${S.near[k] ? ic('check', 14, 2.6) : `<span class="dot" style="background:${KIND[k].color}"></span>`}${KIND[k].label}</button>`).join('')}</div>
    <label class="col small muted" style="gap:4px"><span id="distLabel">Within ${S.dist} mile${S.dist === 1 ? '' : 's'} of each</span>
      <input type="range" id="dist" min="0.25" max="3" step="0.25" value="${S.dist}" style="width:100%;accent-color:var(--teal);height:28px"></label>`
    : '<div class="notice info">Nearby places could not be loaded from OpenStreetMap, so distance filters are off.</div>'}
    <div class="chips"><button class="chip" data-act="positive" aria-pressed="${S.positiveOnly}">${S.positiveOnly ? ic('check', 14, 2.6) : ''}Adds value only</button></div>
  </div>`;
}

function homePanel() {
  const st = S.ov.stats;
  let html = S.showFilters ? filterCard() : '';
  if (searching()) {
    const list = S.ov.buildings.filter(matches).sort((a, b) => (b.impact_low ?? -1e12) - (a.impact_low ?? -1e12));
    html += `<div class="row between"><h2 class="h2">Matching buildings</h2><span class="muted small">${list.length} of ${S.ov.buildings.length}</span></div>
      <div class="list" id="results">${list.length ? list.map(buildingRow).join('') : '<div class="notice info">No building matches. Widen the distance or remove a filter.</div>'}</div>`;
    return html;
  }
  const top = S.showAll ? S.ov.top : S.ov.top.slice(0, 4);
  html += `<div><div class="eyebrow">Fast facts</div><h1 style="font-size:34px;margin-top:4px">Houston</h1>
      <p class="muted" style="margin-top:6px">Office buildings screened for conversion to apartments.</p></div>
    <div class="grid2">
      <div class="card"><div class="stat-num">${st.scanned}</div><div class="stat-label">office buildings scanned</div></div>
      <div class="card"><div class="stat-num">${st.positive}</div><div class="stat-label">where converting adds value</div></div>
      <div class="card"><div class="stat-num">${st.pretested}</div><div class="stat-label">analyzed in full, open instantly</div></div>
      <div class="card"><div class="stat-num">$0</div><div class="stat-label">office income lost on a vacant floor</div></div>
    </div>
    <div class="col"><div class="row between"><h2 class="h2">Top candidates</h2>${S.ov.top.length > 4 ? `<button class="linkbtn" data-act="show-all">${S.showAll ? 'Show fewer' : `View all ${S.ov.top.length}`}</button>` : ''}</div>
      <div class="list">${top.map(buildingRow).join('')}</div></div>
    <p class="small muted spacer">Aerial footprints from Vexcel. Leases and rents from a CoStar snapshot. Places from OpenStreetMap.</p>`;
  return html;
}

function head(crumb) {
  return `<div><div class="crumb"><a href="#/">Houston</a><span class="muted"> / ${esc(crumb)}</span></div><h1 class="bname">${esc(S.sel.name)}</h1></div>`;
}
function tabs() {
  return `<nav class="tabs" aria-label="Building analysis">${[['verdict', 'Verdict'], ['floors', 'Floors'], ['area', 'Area'], ['boardroom', 'Boardroom']].map(([id, label]) =>
    `<a class="tab" href="${bHash(id)}" ${S.tab === id ? 'aria-current="page"' : ''}>${label}</a>`).join('')}</nav>`;
}
function verdictCard(a, compact) {
  const pct = a.confidence != null ? Math.round(a.confidence * 100) : null;
  return `<div class="verdict ${a.headline.state}">
    <div class="row between"><span class="label">${ic(STATE_ICON[a.headline.state], 24, 2.6)}${esc(a.headline.label)}</span>${pct != null ? `<span class="pct">${pct}%</span>` : ''}</div>
    ${pct != null ? `<div class="bar" role="img" aria-label="Confidence ${pct} percent"><span style="width:${pct}%"></span></div>` : ''}
    <div class="small muted">${pct != null ? 'Confidence in the shape check' : 'No confidence score for this building'} · ${a.checks_passed} of ${a.checks_total} checks passed</div>
    ${compact ? `<div class="grid2 small">${a.checks.map(c => `<span class="row" style="gap:6px"><span class="cicon ${c.state}" style="width:20px;height:20px">${ic(STATE_ICON[c.state], 12, 2.6)}</span>${esc(c.title)}</span>`).join('')}</div>` : ''}
  </div>`;
}
function sourceNote(a) {
  if (a.source === 'live') return '';
  return `<div class="notice info">${ic('alert', 18)}<span>${a.source === 'saved'
    ? 'Showing the saved result of the last real pipeline run. The CoStar data kit is not on this machine, so it was not re-run.'
    : 'Only the portfolio scan covered this building. Add the CoStar data kit to run the full analysis.'}</span></div>`;
}

function buildingPanel() {
  const b = S.sel, a = S.an;
  if (S.armed) {
    return `${head('Selected')}<p class="muted">Run the checks on this building to see whether its floors can become apartments, and what it would cost.</p>
      <div class="btns"><button class="btn amber" data-act="analyze">${ic('spark')}Analyze this building</button></div>
      <div class="btns"><a class="btn" href="#/">${ic('back')}Back to the map</a></div>`;
  }
  if (!a) {
    return `${head(b.floor_name != null ? `Floor ${b.floor_name}` : 'Analysis')}${S.anErr
      ? `<div class="notice err">${ic('alert', 18)}<span>${esc(S.anErr)}</span></div><div class="btns"><button class="btn" data-act="analyze">Try again</button><a class="btn" href="#/">Back to the map</a></div>`
      : `<div class="loading"><span class="spinner"></span>${b.source === 'live' ? 'Fetching the aerial footprint and running the checks…' : 'Running the checks…'}</div>`}`;
  }
  const crumb = flr(a, true);
  if (S.tab === 'floors') return S.planOpen ? planPanel() : floorsPanel();
  if (S.tab === 'area') return head('Area') + tabs() + areaPanel();
  if (S.tab === 'boardroom') return head(`${crumb} / Boardroom`) + tabs() + boardPanel();
  return `${head(crumb)}${tabs()}${verdictCard(a)}
    <ul class="checks">${a.checks.map(c => `<li><span class="cicon ${c.state}">${ic(STATE_ICON[c.state], 15, 2.4)}</span>
      <span style="flex:1;min-width:0"><span class="t">${esc(c.title)}</span><span class="n">${esc(c.note)}</span></span><span class="cword ${c.state}">${STATE_WORD[c.state]}</span></li>`).join('')}</ul>
    ${sourceNote(a)}
    <div class="col spacer">
      <div class="btns"><button class="btn primary" data-act="report">${ic('download')}Download detailed report</button></div>
      <div class="btns"><a class="btn" href="${bHash('boardroom')}">${ic('spark')}Hear the boardroom debate</a></div>
    </div>`;
}

function floorsPanel() {
  if (S.an.source === 'scan') {
    return `${head('Floors')}${tabs()}<div class="notice info">${ic('layers', 18)}<span>This building was only screened by the portfolio scan, so its floors are not listed and no floor plan can be drawn yet. Add the CoStar data kit to analyze it floor by floor.</span></div>
      <div class="btns spacer"><a class="btn" href="${bHash('boardroom')}">${ic('spark')}Hear the boardroom debate</a></div>`;
  }
  const a = S.an, fl = floorList().slice().reverse(), real = S.floors && S.floors.source === 'costar';
  const sel = fl.find(f => f.floor === S.floor), same = S.floor === a.floor, n = a.numbers;
  return `${head('Floors')}${tabs()}
    <p class="muted">${real ? 'Teal floors are vacant, so they convert without giving up office rent.' : `Only floor ${a.floor} has been analyzed so far, so it is the one marked Best.`} Click a floor on the building or pick one here.</p>
    <div class="row between"><h2 class="h2">Floors</h2><span class="tag ${real ? 'pass' : 'neutral'}">${real ? 'Real lease history' : `${fl.length} floors, lease data not loaded`}</span></div>
    <div class="floorlist">${fl.map(f => `<button class="floor" data-act="floor" data-floor="${f.floor}" aria-pressed="${f.floor === S.floor}">
      <span class="fn">Floor ${f.floor}</span>${f.best ? '<span class="tag pass">Best</span>' : ''}<span class="fs">${f.analyzed ? 'Analyzed' : STATUS_TEXT[f.status]}</span>
      ${f.analyzed && a.confidence != null ? `<b>${Math.round(a.confidence * 100)}%</b>` : ''}</button>`).join('') || '<div class="notice info">This building was only screened, so its floors are not listed yet.</div>'}</div>
    ${same ? `<div class="card grid3">
        <div><div class="stat-num sm">${n.viable_sqft ? num(n.viable_sqft) : 'n/a'}</div><div class="stat-label">usable sq ft</div></div>
        <div><div class="stat-num sm">${n.total_units || 'n/a'}</div><div class="stat-label">apartments fit</div></div>
        <div><div class="stat-num sm">${n.vacant ? '$0' : n.office_rent ? '$' + n.office_rent.toFixed(0) : 'n/a'}</div><div class="stat-label">${n.vacant ? 'office rent lost' : 'office rent a sq ft'}</div></div></div>`
      : `<div class="notice info">${ic('layers', 18)}<span>Floor ${S.floor} shares the same floor plate as floor ${a.floor}${sel && sel.status !== 'unknown' ? ` and is ${STATUS_TEXT[sel.status].toLowerCase()}` : ''}. Its rent and lease figures need their own analysis.</span></div>`}
    ${S.floorErr ? `<div class="notice">${ic('alert', 18)}<span>${esc(S.floorErr)}</span></div>` : ''}
    <div class="col spacer">
      <div class="btns"><button class="btn primary" data-act="plan-open" ${S.plan ? '' : 'disabled'}>${ic('layers')}Create tentative floor plan</button></div>
      ${S.plan ? '' : '<p class="small muted">No floor area is on record for this building, so a plan can’t be drawn.</p>'}
      <div class="btns">${same ? `<a class="btn" href="${bHash('boardroom')}">${ic('spark')}Take ${flr(a)} to the boardroom</a>`
        : `<button class="btn" data-act="analyze-floor" ${S.anLoading ? 'disabled' : ''}>${S.anLoading ? '<span class="spinner"></span>' : ic('spark')}Analyze floor ${S.floor}</button>`}</div>
    </div>`;
}

function planPanel() {
  const p = S.plan, counts = p.unit_counts || {};
  return `${head(`Floor ${S.floor} / Floor plan`)}${tabs()}
    <div><h2 class="h2">Tentative floor plan</h2><p class="muted" style="margin-top:6px">Drawn from the real floor outline and the distance from windows to the core. A starting point for an architect, not a permit drawing.</p></div>
    <div class="col"><b style="font-size:13.5px">Unit mix</b><div class="chips">${MIXES.map(([id, label]) => `<button class="chip" data-act="mix" data-mix="${id}" aria-pressed="${S.planMix === id}">${S.planMix === id ? ic('check', 14, 2.6) : ''}${label}</button>`).join('')}</div></div>
    <ul class="checks">${Object.keys(UNIT).filter(t => counts[t]).map(t => `<li style="align-items:center"><span class="swatch" style="width:14px;height:14px;background:${UNIT[t].color};border:1px solid var(--ink)"></span><span class="t" style="flex:1">${UNIT[t].long}</span><b>${counts[t]}</b></li>`).join('')}
      <li style="align-items:center"><span class="swatch" style="width:14px;height:14px;background:#D5DDDF;border:1px solid var(--ink)"></span><span class="t" style="flex:1">Usable apartment area</span><b>${num(p.viable_sqft)} sq ft</b></li></ul>
    <div class="notice">${ic('alert', 18)}<span>Column positions are not in the data, so walls may need to move on site.</span></div>
    <div class="col spacer">
      <div class="btns"><button class="btn primary" data-act="plan-download">${ic('download')}Download floor plan</button></div>
      <div class="btns"><button class="btn" data-act="plan-regen" ${S.planLoading ? 'disabled' : ''}>${S.planLoading ? '<span class="spinner"></span>' : ic('spark')}Regenerate</button><button class="btn" data-act="plan-close">${ic('back')}Back to floors</button></div>
    </div>`;
}

function spark(vals, color) {
  const v = vals.filter(x => x != null); if (v.length < 2) return '';
  const lo = Math.min(...v), hi = Math.max(...v), pts = v.map((x, i) => `${(i / (v.length - 1) * 100).toFixed(1)},${(28 - (hi === lo ? 14 : (x - lo) / (hi - lo) * 24)).toFixed(1)}`).join(' ');
  return `<svg aria-hidden="true" viewBox="0 0 100 32" preserveAspectRatio="none" style="width:100%;height:32px;display:block"><polyline points="${pts}" fill="none" stroke="${color}" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" vector-effect="non-scaling-stroke"/></svg>`;
}

function areaPanel() {
  const n = S.an.numbers, ar = S.area;
  let html = `<div class="row between"><h2 class="h2">Market</h2><span class="tag neutral">From the analysis</span></div>
    <div class="grid3">
      <div class="tile"><div class="l">Apartment rent</div><div class="v">${n.residential_rent ? '$' + n.residential_rent.toFixed(2) : 'n/a'}</div><div class="l">a sq ft a year</div></div>
      <div class="tile"><div class="l">Office rent here</div><div class="v">${n.vacant ? 'Vacant' : n.office_rent ? '$' + n.office_rent.toFixed(2) : 'n/a'}</div><div class="l">${n.vacant ? 'no rent today' : 'a sq ft a year'}</div></div>
      <div class="tile"><div class="l">Cap rate</div><div class="v">${n.cap_rate ? n.cap_rate + '%' : 'n/a'}</div><div class="l">${n.cap_rate_source && /default|assum/.test(n.cap_rate_source) ? 'assumed' : n.cap_rate ? 'from a sale' : ''}</div></div>
    </div>`;
  if (n.comps && n.comps.length) html += `<ul class="places">${n.comps.map(c => `<li><span class="glyph" style="background:#B26A00">A</span><span style="flex:1;min-width:0"><b style="display:block">${esc(c.name)}</b><span class="small muted">Apartments used for the rent comparison</span></span><b>${(c.km * 0.621).toFixed(1)} mi</b></li>`).join('')}</ul>`;
  if (!ar) return html + (S.areaErr ? `<div class="notice err">${esc(S.areaErr)}</div>` : '<div class="loading"><span class="spinner"></span>Loading the neighbourhood…</div>');

  const d = ar.demographics;
  html += `<div class="row between"><h2 class="h2">Who lives here</h2>${d.available ? `<span class="tag neutral">Census ${d.year}</span>` : ''}</div>`;
  if (d.available) {
    const L = d.latest, rent = d.series.map(s => s.median_rent), inc = d.series.map(s => s.median_income);
    html += `<div class="grid2">
      <div class="tile"><div class="l">People in this tract</div><div class="v">${num(L.population)}</div></div>
      <div class="tile"><div class="l">Median age</div><div class="v">${L.median_age ?? 'n/a'}</div></div>
      <div class="tile"><div class="l">Household income</div><div class="v">${money(L.median_income)}</div>${spark(inc, '#2F6FEB')}<div class="l">${d.series[0].year} to ${d.year}</div></div>
      <div class="tile"><div class="l">Median rent a month</div><div class="v">${L.median_rent ? '$' + num(L.median_rent) : 'n/a'}</div>${spark(rent, '#0B6E61')}<div class="l">${d.series[0].year} to ${d.year}</div></div></div>
      ${d.renter_share != null ? `<div class="row small"><span class="muted" style="width:130px">Households renting</span><span class="meter"><span style="width:${Math.round(d.renter_share * 100)}%"></span></span><b>${Math.round(d.renter_share * 100)}%</b></div>` : ''}`;
  } else html += `<div class="notice info">${ic('alert', 18)}<span>${esc(d.reason)}</span></div>`;

  const pl = ar.places;
  html += `<h2 class="h2">Places nearby</h2>`;
  if (!pl.available) return html + `<div class="notice info">${esc(pl.reason)}</div>`;
  const kinds = Object.keys(KIND);
  html += `<div class="chips"><button class="chip" data-act="place-kind" data-kind="all" aria-pressed="${S.placeKind === 'all'}">All</button>${kinds.map(k =>
    `<button class="chip" data-act="place-kind" data-kind="${k}" aria-pressed="${S.placeKind === k}"><span class="dot" style="background:${S.placeKind === k ? '#fff' : KIND[k].color}"></span>${KIND[k].label.replace(' store', '').replace('Major ', '')}</button>`).join('')}</div>`;
  const rows = [];
  kinds.forEach(k => { if (S.placeKind === 'all' || S.placeKind === k) pl.kinds[k].items.slice(0, S.placeKind === 'all' ? 2 : 6).forEach(p => rows.push({ ...p, kind: k })); });
  rows.sort((x, y) => x.miles - y.miles);
  html += rows.length ? `<ul class="places">${rows.map(p => `<li><span class="glyph" style="background:${KIND[p.kind].color}">${KIND[p.kind].letter}</span>
      <span style="flex:1;min-width:0"><b style="display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(p.name)}</b><span class="small muted">${KIND[p.kind].label}</span></span><b>${p.miles.toFixed(1)} mi</b></li>`).join('')}</ul>`
    : '<div class="notice info">Nothing of this kind within a mile and a half.</div>';
  return html + `<p class="small muted">Places from OpenStreetMap${pl.fetched ? `, fetched ${pl.fetched}` : ''}. Distances are straight-line.</p>`;
}

function avatar(id, size, speaking) {
  const g = S.ov.agents.find(x => x.id === id);
  return `<span class="av ${id}${speaking ? ' speaking' : ''}" aria-hidden="true" style="width:${size}px;height:${size}px;font-size:${Math.round(size * 0.34)}px">${g.initials}</span>`;
}
const stanceTag = s => `<span class="tag ${s}">${ic(STATE_ICON[s], 12, 2.6)}${STANCE_WORD[s]}</span>`;

function proceedBar() {
  return `<div class="card col" style="gap:10px"><b style="font-size:14.5px">Proceed with this conversion?</b>
    <div class="btns"><button class="btn primary" data-act="proceed">${ic('check')}Yes, proceed</button><button class="btn" data-act="decline">No, not now</button></div></div>`;
}

function boardPanel() {
  const bd = S.board, fs = finalStances(), c = tally(), total = c.for + c.conditional + c.against;
  const scripted = bd.turns.length && !bd.turns.some(t => t.live);
  let html = `<div class="row between"><h2 class="h2">Boardroom discussion</h2>${bd.running
    ? `<span class="row small" style="color:var(--red);font-weight:600;gap:6px"><span class="livedot"></span>Live · round ${bd.round || 1} of 2</span>`
    : bd.done ? '<span class="tag neutral">Finished</span>' : ''}</div>
    <div class="seats">${S.ov.agents.map(g => `<div class="seat">${avatar(g.id, 44, bd.speaking === g.id)}<span>${esc(g.name)}</span>${fs[g.id] ? stanceTag(fs[g.id].stance) : '<span class="tag neutral">Listening</span>'}</div>`).join('')}</div>`;
  if (scripted) html += `<div class="notice info">${ic('alert', 18)}<span>No Anthropic API key is set, so these statements are written by rule from the analysis numbers. Add ANTHROPIC_API_KEY to .env for live AI agents.</span></div>`;
  html += `<div class="col" style="gap:10px" aria-live="polite">${bd.turns.map(t => `<div class="say">${avatar(t.agent, 34)}<div class="bubble ${t.agent}">
      <span class="who">${esc(S.ov.agents.find(g => g.id === t.agent).name)}</span>${stanceTag(t.stance)}<p style="margin-top:4px">${esc(t.text)}</p></div></div>`).join('')}
    ${bd.speaking ? `<div class="row muted small">${avatar(bd.speaking, 34)}${esc(S.ov.agents.find(g => g.id === bd.speaking).name)} is ${bd.turns.length ? 'replying' : 'thinking'}…</div>` : ''}</div>`;
  if (bd.err) html += `<div class="notice err">${ic('alert', 18)}<span>${esc(bd.err)}</span></div><div class="btns"><button class="btn" data-act="board-again">Try again</button></div>`;
  if (total) html += `<div><div class="row between small"><b>${bd.done ? esc(boardLine(c)) : 'Where the board stands'}</b><span class="muted">${c.for} for · ${c.conditional} conditional · ${c.against} against</span></div>
      <div class="tally" role="img" aria-label="${c.for} for, ${c.conditional} conditional, ${c.against} against">${['for', 'conditional', 'against'].map(k => c[k] ? `<span class="${k}" style="flex:${c[k]}"></span>` : '').join('')}</div></div>`;
  if (bd.done) {
    html += `<h2 class="h2">What each agent argued</h2><ul class="argue">${S.ov.agents.map(g => `<li>${avatar(g.id, 34)}<span style="flex:1;min-width:0">
        <span class="row between"><b>${esc(g.name)}</b>${stanceTag(fs[g.id].stance)}</span><span class="small muted" style="display:block;margin-top:2px">${esc(fs[g.id].summary)}</span></span></li>`).join('')}</ul>
      ${verdictCard(S.an, true)}
      <div class="btns"><button class="btn" data-act="report">${ic('download')}Download detailed report</button><button class="btn" data-act="board-again">Run it again</button></div>
      ${proceedBar()}`;
  }
  return html;
}

function quotesPanel() {
  const q = S.quotes, sent = q.stage === 'sent';
  const step = (state, title, sub) => `<li><span class="sdot ${state}">${state === 'done' ? ic('check', 14, 2.6) : ''}</span><span><b style="display:block${state === 'todo' ? ';color:var(--muted)' : ''}">${title}</b><span class="small muted">${sub}</span></span></li>`;
  const statusTag = c => {
    if (!sent) return '';
    const r = q.results[c.id];
    return r ? `<span class="tag ${r.status === 'failed' ? 'fail' : 'pass'}">${r.status === 'sent' ? 'Sent' : r.status === 'failed' ? 'Failed' : 'In outbox'}</span>` : '<span class="tag neutral">Skipped</span>';
  };
  return `${head(`${flr(S.an, true)} / Quotes`)}
    <div class="verdict ${sent ? 'pass' : 'review'}" style="flex-direction:row;align-items:center;gap:12px">
      <span class="cicon ${sent ? 'pass' : 'review'}" style="width:40px;height:40px;background:#fff">${ic(sent ? 'check' : 'mail', 20, 2.2)}</span>
      <div><div class="label" style="font-size:18px">${sent ? (q.mode === 'smtp' ? 'Quote requests sent' : 'Quote requests saved') : 'Check the email before it goes'}</div>
      <div class="small muted">${sent ? (q.mode === 'smtp' ? 'The email agent wrote to your verified contractors.' : 'Demo mode: each email was written to output/outbox, not sent.') : 'Edit anything on the right, then send.'}</div></div></div>
    <ol class="steps">
      ${step('done', 'Email drafted', q.live ? 'Written by the email agent from the report' : 'Written from the report and floor plan')}
      ${step(sent ? 'done' : 'now', sent ? (q.mode === 'smtp' ? 'Sent to contractors' : 'Saved to the outbox') : 'Your review', sent ? `${Object.keys(q.results).length} emails, just now` : 'Nothing has been sent yet')}
      ${step(sent ? 'now' : 'todo', 'Waiting for replies', sent && q.mode !== 'smtp' ? 'Set up SMTP in .env to send for real' : 'Replies arrive in your inbox')}
      ${step('todo', 'Compare quotes', 'Side by side, lowest first')}
    </ol>
    <div class="row between"><h2 class="h2">${sent ? 'Contractors contacted' : 'Verified contractors'}</h2>${q.contractors.some(c => c.sample) ? '<span class="tag sample">Sample list</span>' : ''}</div>
    <ul class="contractors">${q.contractors.map(c => `<li>${sent ? `<span class="cbox">${ic('user', 18)}</span>` : `<input type="checkbox" data-act="pick" data-id="${c.id}" id="pick-${c.id}" ${q.picked.has(c.id) ? 'checked' : ''}>`}
      <label for="pick-${c.id}" style="flex:1;min-width:0"><b class="row" style="gap:6px">${esc(c.company)}<span class="row small" style="gap:3px;color:var(--teal)">${ic('check', 12, 2.8)}Verified</span></b><span class="small muted">${esc(c.trade)}</span></label>${statusTag(c)}</li>`).join('')}</ul>
    <div class="col spacer">
      ${sent ? `<div class="btns"><a class="btn primary" href="${bHash('verdict')}">Back to the building</a><a class="btn" href="#/">${ic('home')}Back to the map</a></div>`
        : `<div class="btns"><button class="btn primary" data-act="send" ${q.sending ? 'disabled' : ''}>${q.sending ? '<span class="spinner"></span>' : ic('mail')}Send to ${q.picked.size} contractor${q.picked.size === 1 ? '' : 's'}</button></div>
           <div class="btns"><a class="btn" href="${bHash('boardroom')}">Cancel</a></div>`}
    </div>`;
}

// ---------- map overlays ----------
function mapUi() {
  if (!M.ok) return `<div class="map-fallback"><div><b>The 3D map could not start in this browser.</b><br>Everything else works: pick a building from the list on the left.</div></div>`;
  const a = S.an, fab = (act, icon, label) => `<button class="glass fab" data-act="${act}" aria-label="${label}">${ic(icon, 20)}</button>`;
  let html = `<div class="m-tl">${S.full ? fab('toggle-full', 'menu', 'Show the sidebar') : ''}${fab('toggle-full', S.full ? 'shrink' : 'expand', S.full ? 'Shrink the map' : 'Expand the map to full width')}
    ${S.full ? `<label class="glass m-search">${ic('search')}<span class="sr">Search buildings</span><input type="search" id="q2" placeholder="Search an address or building" value="${esc(S.q)}" autocomplete="off"></label>` : ''}</div>
    <div class="m-br">${fab('rotate', 'compass', 'Rotate the map')}${fab('zoom-in', 'plus', 'Zoom in')}${fab('zoom-out', 'minus', 'Zoom out')}${S.sel ? fab('recenter', 'pin', 'Centre on the building') : ''}</div>`;

  if (S.view === 'home') {
    const active = Object.keys(S.near).filter(k => S.near[k]);
    html += `<div class="glass m-tc">${active.length ? active.map(k => `<span class="glyph" style="width:20px;height:20px;font-size:11px;background:${KIND[k].color}">${KIND[k].letter}</span>`).join('') + `Buildings within ${S.dist} mi of ${active.map(k => 'a ' + KIND[k].label.toLowerCase()).join(' and ')}`
      : `<span style="color:#5FE0CB">${ic('pin', 16)}</span>Click any building to analyze it`}</div>
      <div class="glass m-bl"><span><span class="legend-dot" style="background:${SRC_COLOR.cached}"></span>Analyzed in full</span>${S.ov.buildings.some(b => b.source === 'live') ? `<span><span class="legend-dot" style="background:${SRC_COLOR.live}"></span>Untested, runs live</span>` : ''}<span><span class="legend-dot" style="background:${SRC_COLOR.scan}"></span>Screened only</span></div>`;
    if (S.full && searching()) {
      const list = S.ov.buildings.filter(matches).slice(0, 6);
      html += `<div class="m-tr" style="left:16px;right:auto;top:76px;width:320px"><span class="eyebrow">${list.length ? 'Matches' : 'No matches'}</span><div class="list">${list.map(buildingRow).join('')}</div></div>`;
    }
    return html;
  }

  if (a && (S.tab === 'verdict' || S.tab === 'boardroom') && S.view === 'building' && !S.modal) {
    const n = a.numbers;
    html += `<div class="m-tr"><div class="row between"><span class="eyebrow">The numbers</span></div>
      <div class="money"><div class="l">Total cost of renovation</div><div class="v">${n.reno_low ? range(n.reno_low, n.reno_high) : 'Not available'}</div><div class="s">${n.viable_sqft ? `${num(n.viable_sqft)} sq ft at $150 to $220 a sq ft` : 'Needs the full analysis'}</div></div>
      <div class="hr"></div>
      <div class="money"><div class="l">Expected return</div><div class="v">${n.uplift_low != null ? `${money((n.uplift_low + n.uplift_high) / 2)} a year` : 'Not available'}</div><div class="s">${n.uplift_low != null ? `Extra rent, ${range(n.uplift_low, n.uplift_high)}` : 'Needs the full analysis'}</div></div>
      <div class="card" style="padding:10px 12px;background:${n.impact_low > 0 ? 'var(--teal-soft)' : n.impact_high < 0 ? 'var(--red-soft)' : 'var(--amber-soft)'}"><div class="small muted">Value added after construction</div><div class="stat-num sm">${range(n.impact_low, n.impact_high)}</div></div></div>`;
  }
  if (a && a.source !== 'scan' && S.tab === 'floors' && S.view === 'building') {
    html += `<div class="glass m-tc"><span style="color:#5FE0CB">${ic('layers', 16)}</span>Click a floor to select it</div>
      <div class="glass m-bl"><span><span class="swatch" style="background:#45D3BC;margin-right:7px"></span>Best to convert</span><span><span class="swatch" style="background:#DDA033;margin-right:7px"></span>Other floors</span><span><span class="swatch" style="background:#fff;margin-right:7px"></span>Selected</span></div>`;
    if (S.planOpen && S.plan) {
      const p = S.plan, u = S.planUnit != null ? p.units[S.planUnit] : null, rent = a.numbers.residential_rent;
      html += `<div class="shade"></div><div class="sheet"><div class="row between" style="flex-wrap:wrap;gap:12px">
        <div><h2 style="font-size:20px">Floor ${S.floor}, tentative plan</h2><div class="muted" style="font-size:13.5px">${p.units.length} apartments · ${num(p.viable_sqft)} usable sq ft of ${num(p.floor_sqft)}</div></div>
        <div class="row"><div class="seg" role="group" aria-label="View"><button data-act="plan-view" data-view="after" aria-pressed="${S.planView === 'after'}">After</button><button data-act="plan-view" data-view="before" aria-pressed="${S.planView === 'before'}">Office today</button></div>
        <button class="icon-btn" data-act="plan-close" aria-label="Close floor plan">${ic('x', 20)}</button></div></div>
        <div class="plan">${S.planLoading ? '<div class="loading"><span class="spinner"></span>Drawing the plan…</div>' : planSvg(p, S.planView, S.planUnit)}</div>
        <div class="row" style="flex-wrap:wrap;gap:6px 16px;font-size:13px;color:var(--muted)">${Object.keys(UNIT).filter(t => p.unit_counts[t]).map(t => `<span class="row" style="gap:7px"><span class="swatch" style="width:14px;height:14px;background:${UNIT[t].color};border:1px solid var(--ink)"></span>${UNIT[t].long}</span>`).join('')}
          <span class="row" style="gap:7px"><span class="swatch" style="width:14px;height:14px;background:#D5DDDF;border:1px solid var(--ink)"></span>Core</span>
          <b style="color:var(--ink)" aria-live="polite">${u ? `${(UNIT[u.type] || {}).long || u.type}: ${num(u.area_sqft)} sq ft${rent ? `, about $${num(u.area_sqft * rent / 12)} a month` : ''}` : 'Click an apartment to see its size and rent.'}</b></div></div>`;
    }
  }
  if (a && S.tab === 'area' && S.view === 'building' && S.area && S.area.places.available) {
    const k = S.area.places.kinds;
    html += `<div class="m-tr" style="width:240px"><span class="eyebrow">Within 1 mile</span><div class="grid2 small">${Object.keys(KIND).map(x => `<span class="row" style="gap:6px"><span class="glyph" style="width:20px;height:20px;font-size:11px;background:${KIND[x].color}">${KIND[x].letter}</span>${k[x].within_1mi} ${KIND[x].label.replace(' store', '').replace('Major ', '').toLowerCase()}${k[x].within_1mi === 1 ? '' : 's'}</span>`).join('')}</div></div>
      <div class="glass m-bl">Rings mark half a mile and one mile from the building</div>`;
  }
  if (S.view === 'quotes' && S.quotes) {
    const q = S.quotes, sent = q.stage === 'sent';
    html += `<div class="shade" style="background:rgba(13,25,32,.45)"></div><div class="sheet narrow">
      <div class="row between"><h2 style="font-size:18px">${sent ? 'The email that went out' : 'Quote request email'}</h2>${q.live ? `<span class="tag pass">${ic('spark', 14)}Written by the email agent</span>` : ''}</div>
      <div class="mail-meta"><span class="muted">To</span><span>${q.picked.size} verified contractor${q.picked.size === 1 ? '' : 's'}, one email each</span>
        <label class="muted" for="mailSubject">Subject</label>${sent ? `<b>${esc(q.subject)}</b>` : `<input class="field" id="mailSubject" value="${esc(q.subject)}">`}</div>
      ${sent ? `<div class="mail-body">${esc(q.body.replace('{contractor}', '[each contact’s name]'))}</div>` : `<label class="sr" for="mailBody">Email body</label><textarea class="field" id="mailBody">${esc(q.body)}</textarea><p class="small muted">{contractor} is replaced with each contact’s name.</p>`}
      <div class="row" style="flex-wrap:wrap"><span class="attach">${ic('download', 16)}oasis-report.html</span>${S.plan ? `<span class="attach">${ic('layers', 16)}tentative-floor-plan.svg</span>` : ''}</div></div>`;
  }
  if (S.modal && a) {
    html += `<div class="modal-wrap"><div class="modal" role="dialog" aria-modal="true" aria-labelledby="mt">
      <div class="row between" style="align-items:flex-start"><h2 id="mt">Proceed with converting ${flr(a)}?</h2><button class="icon-btn" data-act="modal-close" aria-label="Close" style="margin:-8px -8px 0 0">${ic('x', 20)}</button></div>
      <p class="muted" style="font-size:15px">If you say yes, the email agent asks verified local contractors from your database for quotes. You sign and pay nothing.</p>
      <div class="card col" style="gap:10px">${['Writes one email to each verified contractor', `Attaches the report${S.plan ? ' and the tentative floor plan' : ''}`, 'You compare the quotes when they come back'].map(t => `<span class="row" style="gap:10px;color:var(--ink)"><span style="color:var(--teal)">${ic('check', 18, 2.4)}</span>${t}</span>`).join('')}</div>
      <label class="row" style="gap:10px;min-height:44px"><input type="checkbox" id="preview" ${S.preview ? 'checked' : ''} style="width:20px;height:20px;accent-color:var(--teal)">Let me read the email before it is sent</label>
      <div class="btns" style="flex-wrap:wrap"><button class="btn primary" data-act="proceed-yes">${ic('check')}Yes, request quotes</button><button class="btn" data-act="modal-close">Not now</button></div></div></div>`;
  }
  return html;
}

// ---------- the map ----------
const M = {
  ok: false, map: null, ready: false, callout: null, tip: null, markers: [], hiddenId: null, osmHeight: 0, lastKey: '',

  init() {
    try {
      this.map = new maplibregl.Map({ container: 'map', style: 'https://tiles.openfreemap.org/styles/dark', center: [-95.47, 29.83], zoom: 9.4, pitch: 45, bearing: -12, attributionControl: { compact: true } });
      this.ok = true;
    } catch (e) { this.ok = false; return; }
    const map = this.map;
    map.on('load', () => {
      const firstSymbol = (map.getStyle().layers.find(l => l.type === 'symbol') || {}).id;
      map.addLayer({ id: 'oasis-3d', type: 'fill-extrusion', source: 'openmaptiles', 'source-layer': 'building', minzoom: 12.5,
        paint: { 'fill-extrusion-color': ['interpolate', ['linear'], ['coalesce', ['get', 'render_height'], 6], 0, '#22323A', 60, '#2F444E', 200, '#3F5863'],
          'fill-extrusion-height': ['coalesce', ['get', 'render_height'], 6], 'fill-extrusion-base': ['coalesce', ['get', 'render_min_height'], 0], 'fill-extrusion-opacity': 0.92 } }, firstSymbol);
      const empty = { type: 'FeatureCollection', features: [] };
      ['pts', 'halo', 'rings', 'stack'].forEach(id => map.addSource(id, { type: 'geojson', data: empty }));
      map.addLayer({ id: 'halo-glow', type: 'circle', source: 'halo', paint: { 'circle-color': '#F5A524', 'circle-blur': 0.9, 'circle-opacity': 0.55, 'circle-pitch-alignment': 'map',
        'circle-radius': ['interpolate', ['exponential', 2], ['zoom'], 10, 14, 14, 44, 17, 190] } });
      map.addLayer({ id: 'halo-ring', type: 'circle', source: 'halo', paint: { 'circle-color': 'rgba(0,0,0,0)', 'circle-stroke-color': '#F5A524', 'circle-stroke-width': 2.2, 'circle-pitch-alignment': 'map',
        'circle-radius': ['interpolate', ['exponential', 2], ['zoom'], 10, 9, 14, 28, 17, 120] } });
      map.addLayer({ id: 'rings', type: 'line', source: 'rings', paint: { 'line-color': '#FFFFFF', 'line-opacity': 0.55, 'line-width': 1.4, 'line-dasharray': [3, 3] } });
      map.addLayer({ id: 'stack', type: 'fill-extrusion', source: 'stack', paint: { 'fill-extrusion-color': ['get', 'color'], 'fill-extrusion-height': ['get', 'top'], 'fill-extrusion-base': ['get', 'base'], 'fill-extrusion-opacity': 1 } });
      map.addLayer({ id: 'pts-match', type: 'circle', source: 'pts', filter: ['==', ['get', 'ring'], true], paint: { 'circle-radius': 15, 'circle-color': 'rgba(0,0,0,0)', 'circle-stroke-color': '#FFFFFF', 'circle-stroke-width': 2 } });
      map.addLayer({ id: 'pts', type: 'circle', source: 'pts', paint: { 'circle-radius': ['interpolate', ['linear'], ['zoom'], 8, 5, 12, 8, 16, 10], 'circle-color': ['get', 'color'],
        'circle-stroke-color': '#0D1920', 'circle-stroke-width': 2, 'circle-opacity': ['get', 'op'], 'circle-stroke-opacity': ['get', 'op'] } });

      let onStack = false;
      map.on('click', 'stack', e => { onStack = true; const f = e.features[0]; if (f && S.an) { S.floor = f.properties.floor; S.floorErr = null; if (S.tab !== 'floors' || S.view !== 'building') go(bHash('floors')); else render(); } });
      map.on('click', 'pts', e => { if (onStack) { onStack = false; return; } const b = S.byId[e.features[0].properties.id]; if (!b) return;
        if (S.sel && S.sel.property_id === b.property_id && S.an) return; selectBuilding(b, false); S.view = 'building'; S.tab = 'verdict'; history.replaceState(null, '', bHash('verdict', b.property_id)); render(); });
      map.on('click', () => { onStack = false; });
      ['pts', 'stack'].forEach(l => { map.on('mouseenter', l, () => { map.getCanvas().style.cursor = 'pointer'; }); map.on('mouseleave', l, () => { map.getCanvas().style.cursor = ''; if (this.tip) { this.tip.remove(); this.tip = null; } }); });
      map.on('mousemove', 'pts', e => { const p = e.features[0].properties; if (!this.tip) this.tip = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 12, className: 'hovertip' }).addTo(map);
        this.tip.setLngLat(e.features[0].geometry.coordinates).setText(p.name); });
      map.on('mousemove', 'stack', e => { const p = e.features[0].properties; if (!this.tip) this.tip = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 12, className: 'hovertip' }).addTo(map);
        this.tip.setLngLat(e.lngLat).setText(`Floor ${p.floor}`); });
      map.on('idle', () => this.hideOsmTwin());
      this.ready = true; this.lastKey = '';
      this.sync(); if (S.sel) { this.focus(S.sel, S.armed); this.drawStack(); }
    });
  },

  set(id, features) { if (this.ready) this.map.getSource(id).setData({ type: 'FeatureCollection', features }); },

  sync() {
    if (!this.ready || !S.ov) return;
    const key = JSON.stringify([S.view, S.tab, S.q, S.near, S.dist, S.positiveOnly, S.sel && S.sel.property_id, S.floor, S.placeKind, !!S.area, !!S.an, S.full, S.floors && S.floors.floors.length]);
    if (key === this.lastKey) return;
    const resized = this.lastFull !== S.full; this.lastFull = S.full; this.lastKey = key;
    if (resized) setTimeout(() => this.map.resize(), 30);
    const home = S.view === 'home', filtering = home && searching();
    this.set('pts', S.ov.buildings.map(b => {
      const m = !filtering || matches(b), isSel = S.sel && S.sel.property_id === b.property_id;
      return { type: 'Feature', geometry: { type: 'Point', coordinates: [b.longitude, b.latitude] },
        properties: { id: b.property_id, name: b.name, color: SRC_COLOR[b.source], op: isSel && S.an && S.an.source !== 'scan' ? 0 : m ? 1 : 0.18, ring: filtering && m && filtersActive() } };
    }));
    this.colorStack();
    this.drawPlaces();
    if (home && !S.sel && this.wasBuilding) this.overview();
    this.wasBuilding = !home;
  },

  overview() { if (this.ready) this.map.flyTo({ center: [-95.47, 29.83], zoom: 9.4, pitch: 45, bearing: -12, duration: 900 }); },

  focus(b, armed) {
    if (!this.ready) return;
    this.set('halo', [{ type: 'Feature', geometry: { type: 'Point', coordinates: [b.longitude, b.latitude] }, properties: {} }]);
    this.map.flyTo({ center: [b.longitude, b.latitude], zoom: 16.2, pitch: 62, bearing: -24, duration: 1400, essential: true });
    this.closeCallout();
    if (armed) {
      const el = document.createElement('div');
      el.style.cssText = 'display:flex;align-items:center;gap:10px';
      el.innerHTML = `<span>${esc(b.name)}</span><button class="btn amber" data-act="analyze">${ic('spark', 16)}Analyze</button>`;
      this.callout = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 46, className: 'callout', anchor: 'bottom' }).setLngLat([b.longitude, b.latitude]).setDOMContent(el).addTo(this.map);
    }
  },
  closeCallout() { if (this.callout) { this.callout.remove(); this.callout = null; } },

  clearSelection() {
    this.closeCallout(); this.set('halo', []); this.set('stack', []); this.set('rings', []);
    this.markers.forEach(m => m.remove()); this.markers = [];
    if (this.ready && this.hiddenId != null) { this.map.setFilter('oasis-3d', null); }
    this.hiddenId = null; this.osmHeight = 0; this.ring = null; this.areaZoomed = null;
  },

  footprint() {
    const b = S.sel, a = S.an;
    if (a && a.footprint && a.footprint.length > 3) return a.footprint;
    const side = Math.sqrt((b.typical_floor_sqft || 20000) * 0.0929) / 2, dLat = side / 111320, dLon = side / (111320 * Math.cos(b.latitude * Math.PI / 180));
    return [[b.longitude - dLon, b.latitude - dLat], [b.longitude + dLon, b.latitude - dLat], [b.longitude + dLon, b.latitude + dLat], [b.longitude - dLon, b.latitude + dLat], [b.longitude - dLon, b.latitude - dLat]];
  },

  drawStack() { if (this.ready && S.sel && S.an) { this.ring = this.footprint(); this.colorStack(); } },

  colorStack() {
    if (!this.ready || !S.sel || !S.an || !this.ring) return;
    const fl = floorList(); if (!fl.length || S.an.source === 'scan') { this.set('stack', []); return; }
    const h = Math.max(3.9, (this.osmHeight + 1.5) / fl.length);
    this.set('stack', fl.map((f, i) => ({ type: 'Feature', geometry: { type: 'Polygon', coordinates: [this.ring] },
      properties: { floor: f.floor, base: i * h + 0.25, top: (i + 1) * h - 0.25, color: f.floor === S.floor ? '#FFFFFF' : f.best ? '#45D3BC' : '#DDA033' } })));
  },

  hideOsmTwin() {
    if (!S.sel || !S.an || S.an.source === 'scan' || this.hiddenId != null || this.map.getZoom() < 14 || this.map.isMoving()) return;
    const pt = this.map.project([S.sel.longitude, S.sel.latitude]);
    const feats = this.map.queryRenderedFeatures([[pt.x - 6, pt.y - 6], [pt.x + 6, pt.y + 6]], { layers: ['oasis-3d'] });
    if (!feats.length) { this.hiddenId = -1; return; }
    const tallest = feats.reduce((m, f) => Math.max(m, f.properties.render_height || 0), 0);
    const ids = [...new Set(feats.map(f => f.id).filter(id => id != null))];
    this.hiddenId = ids.length ? ids[0] : -1;
    if (ids.length) this.map.setFilter('oasis-3d', ['!', ['in', ['id'], ['literal', ids]]]);
    else this.osmHeight = tallest;
    this.colorStack();
  },

  drawPlaces() {
    this.markers.forEach(m => m.remove()); this.markers = [];
    const show = S.view === 'building' && S.tab === 'area' && S.sel && S.area && S.area.places.available;
    if (!show) {
      this.set('rings', []);
      if (this.areaZoomed && S.sel) { this.areaZoomed = null; ACT.recenter(); }
      return;
    }
    const c = [S.sel.longitude, S.sel.latitude];
    this.set('rings', [0.5, 1].map(mi => ({ type: 'Feature', properties: {}, geometry: { type: 'LineString', coordinates: Array.from({ length: 65 }, (_, i) => {
      const t = i / 64 * 2 * Math.PI, d = mi * 1609.34; return [c[0] + d * Math.cos(t) / (111320 * Math.cos(c[1] * Math.PI / 180)), c[1] + d * Math.sin(t) / 111320]; }) } })));
    Object.keys(KIND).forEach(k => { if (S.placeKind !== 'all' && S.placeKind !== k) return;
      S.area.places.kinds[k].items.slice(0, S.placeKind === 'all' ? 3 : 6).forEach(p => {
        const el = document.createElement('div'); el.className = 'pin'; el.style.background = KIND[k].color; el.textContent = KIND[k].letter; el.title = `${p.name}, ${p.miles.toFixed(1)} mi`;
        this.markers.push(new maplibregl.Marker({ element: el }).setLngLat([p.lon, p.lat]).setPopup(new maplibregl.Popup({ offset: 16, closeButton: false, className: 'hovertip' }).setText(`${p.name} · ${p.miles.toFixed(1)} mi`)).addTo(this.map));
      }); });
    const key = `${S.sel.property_id}`; if (this.areaZoomed !== key) { this.areaZoomed = key; this.map.flyTo({ center: c, zoom: 13.6, pitch: 50, duration: 1000 }); }
  },
};

// ---------- events ----------
const ACT = {
  'toggle-full'() { S.full = !S.full; render(); if (S.full) { const q2 = $('#q2'); if (q2 && searching()) q2.focus(); } },
  'toggle-filters'() { S.showFilters = !S.showFilters; if (S.showFilters && S.view !== 'home') { go('#/'); return; } render(); },
  'clear-filters'() { Object.keys(S.near).forEach(k => { S.near[k] = false; }); S.positiveOnly = false; render(); },
  near(el) { S.near[el.dataset.kind] = !S.near[el.dataset.kind]; render(); },
  positive() { S.positiveOnly = !S.positiveOnly; render(); },
  'show-all'() { S.showAll = !S.showAll; render(); },
  open(el) { if (S.full) S.full = false; go(bHash('verdict', el.dataset.id)); },
  analyze() { runAnalysis(S.sel.floor_name); },
  'analyze-floor'() { runAnalysis(S.floor); },
  report() { downloadReport(); },
  floor(el) { S.floor = +el.dataset.floor; S.floorErr = null; render(); },
  'plan-open'() { if (S.plan) { S.planOpen = true; S.planView = 'after'; render(); } },
  'plan-close'() { S.planOpen = false; render(); },
  'plan-view'(el) { S.planView = el.dataset.view; S.planUnit = null; render(); },
  'plan-regen'() { loadPlan(S.planMix); },
  'plan-download'() { download(`floor-${S.floor}-tentative-plan.svg`, planSvg(S.plan, 'after', null, true), 'image/svg+xml'); toast('Floor plan downloaded.'); },
  mix(el) { if (el.dataset.mix !== S.planMix) loadPlan(el.dataset.mix); },
  unit(el) { S.planUnit = +el.dataset.i; render(); },
  'place-kind'(el) { S.placeKind = el.dataset.kind; render(); },
  'board-again'() { startBoard(true); },
  proceed() { S.modal = true; render(); const y = $('[data-act="proceed-yes"]'); if (y) y.focus(); },
  decline() { toast('Okay. Nothing was sent.'); go(bHash('verdict')); },
  'modal-close'() { S.modal = false; render(); },
  'proceed-yes'() { S.preview = $('#preview').checked; openQuotes(); },
  pick(el) { readDraft(); if (el.checked) S.quotes.picked.add(el.dataset.id); else S.quotes.picked.delete(el.dataset.id); render(); },
  send() { sendQuotes(); },
  profile() { const p = $('#profilePop'), open = p.hidden; p.hidden = !open; $('#profileBtn').setAttribute('aria-expanded', String(open)); },
  rotate() { M.map.easeTo({ bearing: M.map.getBearing() + 45, duration: 600 }); },
  'zoom-in'() { M.map.zoomIn(); }, 'zoom-out'() { M.map.zoomOut(); },
  recenter() { if (S.sel) M.map.flyTo({ center: [S.sel.longitude, S.sel.latitude], zoom: 16.2, pitch: 62, bearing: -24, duration: 900 }); },
};

document.addEventListener('click', e => {
  const el = e.target.closest('[data-act]');
  if (!$('#profilePop').hidden && !e.target.closest('#profilePop') && !(el && el.dataset.act === 'profile')) { $('#profilePop').hidden = true; $('#profileBtn').setAttribute('aria-expanded', 'false'); }
  if (!el || el.disabled) return;
  if (el.tagName === 'INPUT' && el.type === 'checkbox') return;  // handled on change
  ACT[el.dataset.act] && ACT[el.dataset.act](el);
});
document.addEventListener('change', e => { const el = e.target; if (el.dataset && el.dataset.act === 'pick') ACT.pick(el); });
document.addEventListener('input', e => {
  const el = e.target;
  if (el.id === 'q' || el.id === 'q2') {
    S.q = el.value; if (el.id === 'q2') $('#q').value = S.q;
    if (el.id === 'q' && S.view !== 'home') { go('#/'); return; }
    const pos = el.selectionStart; render();
    if (el.id === 'q2') { const n = $('#q2'); n.focus(); n.setSelectionRange(pos, pos); }
  } else if (el.id === 'dist') {
    S.dist = +el.value; $('#distLabel').textContent = `Within ${S.dist} mile${S.dist === 1 ? '' : 's'} of each`;
    clearTimeout(ACT.distT); ACT.distT = setTimeout(() => { const had = document.activeElement && document.activeElement.id === 'dist'; render(); if (had) $('#dist').focus(); }, 180);
  }
});
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  if (S.modal) ACT['modal-close'](); else if (S.planOpen) ACT['plan-close'](); else if (!$('#profilePop').hidden) ACT.profile();
});
window.addEventListener('hashchange', route);

// ---------- start ----------
(async function start() {
  $('#menuBtn').innerHTML = ic('menu', 22); $('#searchIcon').innerHTML = ic('search'); $('#profileBtn').innerHTML = ic('user', 22);
  $('#panel').innerHTML = '<div class="loading"><span class="spinner"></span>Loading Houston…</div>';
  try { S.ov = await api('/api/overview'); }
  catch (e) { $('#panel').innerHTML = `<div class="notice err">Could not load the building list. ${esc(e.message)}</div>`; return; }
  S.ov.buildings.forEach(b => { S.byId[b.property_id] = b; });
  const src = S.ov.sources, line = (ok, title, yes, no) => `<div class="src-row"><span style="color:${ok ? 'var(--teal)' : 'var(--amber-txt)'}">${ic(ok ? 'check' : 'alert', 18, 2.2)}</span><div><b>${title}</b><span>${ok ? yes : no}</span></div></div>`;
  $('#profilePop').innerHTML = `<b style="font-size:15px">Demo account</b><span class="eyebrow">Data sources</span>
    ${line(src.costar, 'CoStar lease and rent data', 'Loaded, analyses run live', 'Not on this machine, showing saved results')}
    ${line(src.vexcel, 'Vexcel aerial footprints', 'Key set, new buildings fetch live', 'No key, cached footprints only')}
    ${line(src.agents, 'AI agents', 'Live Claude agents', 'No key, boardroom is written by rule')}
    ${line(src.places, 'Nearby places', 'OpenStreetMap, cached', 'Could not be loaded')}
    ${line(src.census, 'Census demographics', 'Key set', 'Add CENSUS_API_KEY to turn on')}`;
  M.init();
  route();
})();
