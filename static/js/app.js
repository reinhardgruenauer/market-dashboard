/* ── RG Trading · Mega-Cap-Barometer Frontend ───────────────── */

let DATA = null;
let market = 'nq';
let sortBy = 'w';
let selected = null;
try { const m = localStorage.getItem('rg_mk'); if (m === 'nq' || m === 'es') market = m; } catch (e) {}

const MK = {
  nq: { key: 'nq100', idx: 'Nasdaq 100', fut: 'NQ' },
  es: { key: 'sp500', idx: 'S&P 500',    fut: 'ES' },
};
const PHASE = {
  PRE:    ['Vorbörse', ''],
  REG:    ['US-Handel läuft', 'reg'],
  POST:   ['Nachbörse', ''],
  EU:     ['Europa-Session · Futures live', ''],
  CLOSED: ['Wochenende', ''],
};

const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt = (v, d = 2) => (v ?? 0).toLocaleString('de-AT', { minimumFractionDigits: d, maximumFractionDigits: d });
const sgn = (v, d = 2) => (v >= 0 ? '+' : '−') + fmt(Math.abs(v), d);
const cls = v => (v >= 0 ? 'up' : 'dn');

function labelClass(score) {
  if (score <= -15) return 'dn';
  if (score < 15) return 'neu';
  return 'up';
}

/* ── Daten laden ─────────────────────────────────── */
async function load(force = false) {
  const btn = $('refreshBtn');
  btn.disabled = true;
  try {
    const res = await fetch(force ? '/api/data?force=true' : '/api/data');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    DATA = await res.json();
    $('errbar').hidden = true;
    render();
  } catch (err) {
    console.error(err);
    const e = $('errbar');
    e.textContent = 'Daten konnten nicht geladen werden. Es wird beim nächsten Intervall automatisch erneut versucht.';
    e.hidden = false;
  } finally {
    btn.disabled = false;
  }
}

/* ── Tacho ───────────────────────────────────────── */
function gauge(el, v) {
  const cx = 120, cy = 118, r = 96;
  const segs = [[-100,-50,'#ef5a5a'],[-50,-15,'#b9505a'],[-15,15,'#e8a33a'],[15,50,'#1fa77b'],[50,100,'#22c38e']];
  const ang = x => Math.PI * (1 - (x + 100) / 200);
  const pt = (a, rr) => [cx + rr * Math.cos(a), cy - rr * Math.sin(a)];
  let h = '';
  segs.forEach(([a, b, col]) => {
    const [x1, y1] = pt(ang(a), r), [x2, y2] = pt(ang(b), r);
    h += `<path d="M${x1} ${y1} A${r} ${r} 0 0 1 ${x2} ${y2}" stroke="${col}" stroke-width="16" fill="none" opacity=".9"/>`;
  });
  [-100, -50, 0, 50, 100].forEach(t => {
    const [x, y] = pt(ang(t), r + 16);
    h += `<text x="${x}" y="${y + 3}" fill="#8b95a7" font-size="9" text-anchor="middle" font-family="Inter">${t > 0 ? '+' + t : t}</text>`;
  });
  const [nx, ny] = pt(ang(Math.max(-100, Math.min(100, v))), r - 22);
  h += `<line x1="${cx}" y1="${cy}" x2="${nx}" y2="${ny}" stroke="#f1cf74" stroke-width="4" stroke-linecap="round"/>`;
  h += `<circle cx="${cx}" cy="${cy}" r="8" fill="#f1cf74"/><circle cx="${cx}" cy="${cy}" r="3.5" fill="#121826"/>`;
  el.innerHTML = h;
}

/* ── Hilfen für Kursverläufe ─────────────────────── */
function pctPath(s) {
  const base = s.prev_close || (s.prices && s.prices[0]) || 0;
  if (!base || !s.prices || !s.prices.length) return [];
  return s.prices.map(p => (p / base - 1) * 100);
}
function refPct(s) {
  if (DATA.phase === 'REG' && s.vwap > 0 && s.prev_close > 0) return (s.vwap / s.prev_close - 1) * 100;
  return 0;
}
function spark(path, ref, w = 200, h = 46) {
  if (path.length < 2) path = path.length ? [path[0], path[0]] : [0, 0];
  const mn = Math.min(...path, ref) - 0.05, mx = Math.max(...path, ref) + 0.05;
  const X = i => i / (path.length - 1) * w, Y = v => h - 2 - (v - mn) / (mx - mn) * (h - 4);
  const d = path.map((v, i) => (i ? 'L' : 'M') + X(i).toFixed(1) + ' ' + Y(v).toFixed(1)).join(' ');
  const col = path[path.length - 1] >= 0 ? '#22c38e' : '#ef5a5a';
  const id = 'g' + Math.random().toString(36).slice(2, 8);
  return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    <defs><linearGradient id="${id}" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="${col}" stop-opacity=".28"/><stop offset="1" stop-color="${col}" stop-opacity="0"/></linearGradient></defs>
    <line x1="0" x2="${w}" y1="${Y(ref)}" y2="${Y(ref)}" stroke="#56607a" stroke-dasharray="3 3" stroke-width="1"/>
    <path d="${d} L${w} ${h} L0 ${h} Z" fill="url(#${id})"/>
    <path d="${d}" fill="none" stroke="${col}" stroke-width="1.8" vector-effect="non-scaling-stroke"/></svg>`;
}

/* ── Rendern ─────────────────────────────────────── */
function render() {
  if (!DATA) return;
  const [phTxt, phCls] = PHASE[DATA.phase] || ['–', ''];
  const b = $('phaseBadge'); b.textContent = '● ' + phTxt; b.className = 'badge ' + phCls;
  $('stamp').textContent = DATA.timestamp || '';
  const lastMode = DATA.phase === 'EU' || DATA.phase === 'CLOSED';
  const ib = $('infobar');
  ib.hidden = !lastMode;
  ib.innerHTML = DATA.phase === 'CLOSED'
    ? '<b>Wochenende:</b> Kacheln und Tachos zeigen die letzte US-Sitzung (inkl. Nachbörse) im Vergleich zum Schluss davor.'
    : '<b>Europa-Session:</b> US-Aktien handeln erst ab ca. 10:00 MEZ (vorbörslich). Bis dahin zeigen Kacheln und Tachos die <b>letzte US-Sitzung inkl. Nachbörse</b>. NQ &amp; ES Futures sind live.';

  // Tachos
  [['NQ', 'nq100'], ['ES', 'sp500']].forEach(([k, key]) => {
    const bar = DATA[key].barometer;
    gauge($('g' + k), bar.score);
    const c = labelClass(bar.score);
    $('v' + k).textContent = (bar.score > 0 ? '+' : '') + bar.score; $('v' + k).className = 'gauge-val mono ' + c;
    $('l' + k).textContent = bar.label; $('l' + k).className = 'gauge-lab ' + c;
    $(k.toLowerCase() + 'Wsum').textContent = 'Top 10 ≈ ' + fmt(bar.weight_sum, 0) + ' % Indexgewicht';
    $('s' + k).innerHTML = `${lastMode ? 'Letzte US-Sitzung' : 'Gewichtete Veränderung'} der Top 10: <b class="${cls(bar.weighted_change)}">${sgn(bar.weighted_change)} %</b>`;
  });
  $('cardNQ').classList.toggle('focus', market === 'nq');
  $('cardES').classList.toggle('focus', market === 'es');

  // Kontext
  const f = DATA.futures, v = DATA.vol || {};
  const k = (t, d, pts) => {
    if (!d || !d.current) return `<div class="k"><div class="t">${t}</div><div class="v mono">–</div><div class="c">keine Daten</div></div>`;
    const ch = pts ? d.current - (d.prev_close || d.current) : d.change_pct;
    const good = pts ? -ch : ch;
    return `<div class="k"><div class="t">${t}</div><div class="v mono">${fmt(d.current, d.current > 1000 ? 0 : 2)}</div><div class="c ${cls(good)}">${sgn(ch)}${pts ? ' Pkt' : ' %'}</div></div>`;
  };
  $('ctx').innerHTML = k('NQ Future', f.NQ) + k('ES Future', f.ES) + k('VIX (→ ES)', v.VIX, true) + k('VXN (→ NQ)', v.VXN, true);

  const M_ = MK[market], sec = DATA[M_.key], bar = sec.barometer;
  const n = bar.count || 10;
  $('bUp').textContent = `${bar.up_count} von ${n}`; $('bUpBar').style.width = (bar.up_count / n * 100) + '%';
  $('refLabel').textContent = 'Über ' + (bar.reference === 'VWAP' ? 'Tages-VWAP' : (lastMode ? 'Schluss davor' : 'Vortagesschluss'));
  $('bRef').textContent = `${bar.above_count} von ${n}`; $('bRefBar').style.width = (bar.above_count / n * 100) + '%';

  // Kalender
  const evs = DATA.calendar || [];
  $('calendar').innerHTML = evs.length ? evs.map(e => `<div class="ev">
      <b class="mono">${esc(e.time || '–')}</b><span>${esc(e.country)}</span><span>${esc(e.title)}</span>
      <span class="vals"><span>Prognose <b>${esc(e.forecast || '–')}</b></span><span>Vorher <b>${esc(e.previous || '–')}</b></span><span>Aktuell <b class="${e.actual ? 'neu' : ''}">${esc(e.actual || '–')}</b></span></span>
    </div>`).join('') : '<div class="empty">Heute keine hochrelevanten Termine.</div>';

  // Kacheln
  const aboveMap = Object.fromEntries((bar.stocks || []).map(r => [r.symbol, r]));
  const list = sec.stocks.map(s => ({ ...s, _a: aboveMap[s.symbol] }));
  if (!selected || !list.find(s => s.symbol === selected)) selected = list[0] && list[0].symbol;
  $('tilesH').textContent = 'Die 10 Schwergewichte im ' + M_.idx;
  $('wInfo').textContent = `Gewicht = Anteil im ${M_.idx} (Stand ${sec.weights_stand || '–'}).`;
  const arr = [...list];
  if (sortBy === 'w') arr.sort((a, b) => b.weight - a.weight);
  if (sortBy === 'c') arr.sort((a, b) => (b.change_pct || 0) - (a.change_pct || 0));
  if (sortBy === 'i') arr.sort((a, b) => Math.abs((b.change_pct || 0) * b.weight) - Math.abs((a.change_pct || 0) * a.weight));
  const refWord = bar.reference === 'VWAP' ? 'VWAP' : 'Vortag';
  $('tiles').innerHTML = arr.map(s => {
    const ch = s.change_pct || 0, sg = s._a ? (s._a.sign ?? (s._a.above ? 1 : -1)) : Math.sign(ch);
    return `<div class="tile ${s.symbol === selected ? 'sel' : ''}" data-t="${esc(s.symbol)}">
      <div class="top"><div><div class="tk">${esc(s.symbol)}</div><div class="nm">${esc(s.name)}</div></div><span class="pill ${cls(ch)} mono">${sgn(ch)} %</span></div>
      <div class="px mono">${s.current ? fmt(s.current) + ' $' : '–'}</div>
      ${spark(pctPath(s), refPct(s))}
      <div class="meta"><b class="${sg > 0 ? 'up' : sg < 0 ? 'dn' : 'neu'}">${sg > 0 ? '▲ über ' + refWord : sg < 0 ? '▼ unter ' + refWord : '= unverändert'}</b><span>Gew. ${fmt(s.weight, 1)} %</span></div>
    </div>`;
  }).join('');
  document.querySelectorAll('.tile').forEach(el => el.onclick = () => { selected = el.dataset.t; render(); });

  // Einfluss
  $('conH').textContent = 'Wer zieht den ' + M_.fut + '?';
  const con = (bar.stocks || []).map(r => ({ t: r.symbol, p: r.contrib_pts })).sort((a, b) => b.p - a.p);
  const mx = Math.max(market === 'nq' ? 10 : 3, ...con.map(x => Math.abs(x.p)));
  $('contrib').innerHTML = con.map(x => {
    const w = Math.abs(x.p) / mx * 50, left = x.p >= 0 ? 50 : 50 - w;
    return `<div class="r"><b class="mono" style="font-size:11.5px">${esc(x.t)}</b><div class="track"><div class="mid"></div><div class="fill" style="left:${left}%;width:${w}%;background:${x.p >= 0 ? '#22c38e' : '#ef5a5a'}"></div></div><span class="mono ${cls(x.p)}" style="text-align:right">${sgn(x.p, 1)}</span></div>`;
  }).join('') || '<p class="hint">Noch keine Daten.</p>';

  renderDetail(list.find(s => s.symbol === selected));
}

function renderDetail(s) {
  if (!s) return;
  const ch = s.change_pct || 0;
  $('dTitle').textContent = `${s.name} (${s.symbol})`;
  $('dInfo').innerHTML = `<span class="${cls(ch)}">${sgn(ch)} %</span> · ${DATA.phase === 'EU' || DATA.phase === 'CLOSED' ? 'Schluss davor' : 'Vortag'} ${s.prev_close ? fmt(s.prev_close) + ' $' : '–'}${s.vwap ? ' · VWAP ' + fmt(s.vwap) + ' $' : ''}`;
  const W = 600, H = 230, p = pctPath(s), labels = s.labels || [];
  if (p.length < 2) { $('dChart').innerHTML = `<text x="300" y="115" fill="#8b95a7" font-size="13" text-anchor="middle" font-family="Inter">Noch kein Kursverlauf für heute</text>`; }
  else {
    const vw = s.vwap > 0 && s.prev_close > 0 ? (s.vwap / s.prev_close - 1) * 100 : null;
    const vals = vw !== null ? [...p, 0, vw] : [...p, 0];
    const mn = Math.min(...vals) - 0.15, mx = Math.max(...vals) + 0.15;
    const X = i => i / (p.length - 1) * W, Y = v => H - 22 - (v - mn) / (mx - mn) * (H - 34);
    const d = p.map((v, i) => (i ? 'L' : 'M') + X(i).toFixed(1) + ' ' + Y(v).toFixed(1)).join(' ');
    let g = '';
    for (let i = 0; i < 5; i++) { const v = mn + (mx - mn) * i / 4; g += `<line x1="0" x2="${W}" y1="${Y(v)}" y2="${Y(v)}" stroke="#232c3f"/><text x="4" y="${Y(v) - 3}" fill="#8b95a7" font-size="10" font-family="JetBrains Mono">${sgn(v)}%</text>`; }
    g += `<line x1="0" x2="${W}" y1="${Y(0)}" y2="${Y(0)}" stroke="#8b95a7" stroke-dasharray="5 4"/>`;
    if (vw !== null) g += `<line x1="0" x2="${W}" y1="${Y(vw)}" y2="${Y(vw)}" stroke="#4f8fe8" stroke-dasharray="2 3"/>`;
    const step = Math.max(1, Math.floor(labels.length / 6));
    labels.forEach((l, i) => { if (i % step === 0 && i < p.length) g += `<text x="${Math.min(W - 30, X(i))}" y="${H - 6}" fill="#8b95a7" font-size="10" font-family="Inter">${esc(l)}</text>`; });
    g += `<path d="${d} L${W} ${H - 18} L0 ${H - 18} Z" fill="rgba(212,168,67,.10)"/><path d="${d}" fill="none" stroke="#d4a843" stroke-width="2.2" vector-effect="non-scaling-stroke"/>`;
    $('dChart').innerHTML = g;
  }
  $('dLegend').innerHTML = `<span><i style="height:2px;background:#d4a843"></i>Kurs (${DATA.phase === 'EU' || DATA.phase === 'CLOSED' ? 'letzte US-Sitzung' : 'heute'}, MEZ)</span><span><i style="border-top:1px dashed #8b95a7"></i>${DATA.phase === 'EU' || DATA.phase === 'CLOSED' ? 'Schluss davor' : 'Vortagesschluss'}</span>${s.vwap ? '<span><i style="border-top:1px dashed #4f8fe8"></i>Tages-VWAP</span>' : ''}`;
  const news = s.news || [];
  $('dNews').innerHTML = news.length ? news.map(n => `<div class="it"><a href="${esc(n.link)}" target="_blank" rel="noopener">${esc(n.title)}</a><div class="m">${esc(n.source)}${n.source && n.published ? ' · ' : ''}${esc(n.published)}</div></div>`).join('') : '<div class="it m" style="color:var(--muted)">Keine aktuellen Nachrichten.</div>';
}

/* ── Bedienung ───────────────────────────────────── */
function seg(id, attr, fn) {
  document.querySelectorAll('#' + id + ' button').forEach(b => b.onclick = () => {
    document.querySelectorAll('#' + id + ' button').forEach(x => x.classList.remove('on'));
    b.classList.add('on'); fn(b.dataset[attr]);
  });
}
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('#marketSeg button').forEach(b => b.classList.toggle('on', b.dataset.m === market));
  seg('marketSeg', 'm', v => { market = v; selected = null; try { localStorage.setItem('rg_mk', v); } catch (e) {} render(); });
  seg('sortSeg', 'o', v => { sortBy = v; render(); });
  $('refreshBtn').onclick = () => load(true);
  load(false);
  setInterval(() => load(false), 60000);
});
