/* ── Market Dashboard Frontend ────────────────────────── */

let sp500Chart = null;
let nq100Chart = null;
let isManualRefresh = false;

const CHART_COLORS = [
    '#3b82f6', '#22c55e', '#f59e0b', '#ef4444', '#8b5cf6',
    '#ec4899', '#14b8a6', '#f97316', '#06b6d4', '#a855f7'
];

async function fetchData(force = false) {
    const url = force ? '/api/data?force=true' : '/api/data';
    const res = await fetch(url);
    return await res.json();
}

function formatChange(pct) {
    const sign = pct >= 0 ? '+' : '';
    return `${sign}${pct.toFixed(2)}%`;
}

function updateFuturesBar(data) {
    const esData = data.futures.ES;
    const nqData = data.futures.NQ;

    document.getElementById('es-price').textContent = esData.current ? esData.current.toLocaleString('de-DE', {minimumFractionDigits: 2}) : '—';
    document.getElementById('nq-price').textContent = nqData.current ? nqData.current.toLocaleString('de-DE', {minimumFractionDigits: 2}) : '—';

    const esChange = document.getElementById('es-change');
    const nqChange = document.getElementById('nq-change');

    esChange.textContent = esData.change_pct ? formatChange(esData.change_pct) : '—';
    esChange.className = 'change ' + (esData.change_pct >= 0 ? 'positive' : 'negative');

    nqChange.textContent = nqData.change_pct ? formatChange(nqData.change_pct) : '—';
    nqChange.className = 'change ' + (nqData.change_pct >= 0 ? 'positive' : 'negative');
}

function updateProbability(containerId, prob) {
    const container = document.getElementById(containerId);
    const circle = container.querySelector('.prob-circle');
    const pctEl = circle.querySelector('.pct');
    const signalEl = circle.querySelector('.signal');
    const infoEl = container.querySelector('.prob-info');

    const isLong = prob.long_pct >= 50;
    circle.className = 'prob-circle ' + (isLong ? 'long' : 'short');
    pctEl.textContent = (isLong ? prob.long_pct : prob.short_pct).toFixed(0) + '%';
    signalEl.textContent = prob.signal;

    infoEl.innerHTML = `
        <span style="color: var(--green);">${prob.up_count} steigend</span> /
        <span style="color: var(--red);">${prob.down_count} fallend</span><br>
        Gewichtete Wahrscheinlichkeit
    `;
}

function buildStockTable(tableId, stocks) {
    const tbody = document.querySelector(`#${tableId} tbody`);
    tbody.innerHTML = '';

    stocks.forEach(s => {
        const changeClass = s.change_pct >= 0 ? 'positive' : 'negative';
        const changeColor = s.change_pct >= 0 ? 'var(--green)' : 'var(--red)';
        const barWidth = Math.min(Math.abs(s.change_pct) * 10, 100);

        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td>
                <span class="symbol">${s.symbol}</span><br>
                <span class="name">${s.name}</span>
            </td>
            <td class="price">${s.current ? s.current.toLocaleString('de-DE', {minimumFractionDigits: 2}) : '—'}</td>
            <td class="change-cell" style="color: ${changeColor}">
                ${formatChange(s.change_pct)}
                <div class="mini-bar">
                    <div class="mini-bar-fill ${changeClass}" style="width: ${barWidth}%"></div>
                </div>
            </td>
            <td class="weight">${s.weight.toFixed(2)}%</td>
        `;
        tbody.appendChild(tr);
    });
}

function normalizeChartData(stocks) {
    const datasets = [];

    stocks.forEach((s, i) => {
        if (!s.prices || s.prices.length === 0) return;

        const basePrice = s.prices[0];
        const normalized = s.prices.map(p => ((p - basePrice) / basePrice) * 100);

        datasets.push({
            label: s.symbol,
            data: normalized,
            borderColor: CHART_COLORS[i % CHART_COLORS.length],
            backgroundColor: 'transparent',
            borderWidth: 2,
            pointRadius: 0,
            pointHoverRadius: 4,
            tension: 0.3,
        });
    });

    let labels = [];
    stocks.forEach(s => {
        if (s.labels && s.labels.length > labels.length) {
            labels = s.labels;
        }
    });

    return { labels, datasets };
}

function createChart(canvasId, stocks, existingChart) {
    if (existingChart) existingChart.destroy();

    const { labels, datasets } = normalizeChartData(stocks);
    const ctx = document.getElementById(canvasId).getContext('2d');

    const step = Math.max(1, Math.floor(labels.length / 12));
    const displayLabels = labels.map((l, i) => i % step === 0 ? l : '');

    return new Chart(ctx, {
        type: 'line',
        data: { labels: displayLabels, datasets },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: {
                mode: 'index',
                intersect: false,
            },
            plugins: {
                legend: {
                    position: 'bottom',
                    labels: {
                        color: '#94a3b8',
                        font: { size: 11, family: 'Inter' },
                        usePointStyle: true,
                        pointStyle: 'circle',
                        padding: 12,
                    }
                },
                tooltip: {
                    backgroundColor: '#1e293b',
                    titleColor: '#e2e8f0',
                    bodyColor: '#94a3b8',
                    borderColor: '#334155',
                    borderWidth: 1,
                    padding: 10,
                    callbacks: {
                        label: function(ctx) {
                            return `${ctx.dataset.label}: ${ctx.parsed.y >= 0 ? '+' : ''}${ctx.parsed.y.toFixed(2)}%`;
                        }
                    }
                }
            },
            scales: {
                x: {
                    grid: { color: 'rgba(255,255,255,0.04)' },
                    title: {
                        display: true,
                        text: 'Uhrzeit (MEZ)',
                        color: '#64748b',
                        font: { size: 11, family: 'Inter' },
                    },
                    ticks: {
                        color: '#64748b',
                        font: { size: 10 },
                        maxRotation: 0,
                    }
                },
                y: {
                    grid: { color: 'rgba(255,255,255,0.04)' },
                    title: {
                        display: true,
                        text: 'Veränderung %',
                        color: '#64748b',
                        font: { size: 11, family: 'Inter' },
                    },
                    ticks: {
                        color: '#64748b',
                        font: { size: 10 },
                        callback: v => (v >= 0 ? '+' : '') + v.toFixed(1) + '%'
                    },
                    position: 'right',
                }
            }
        }
    });
}

function buildNewsSection(newsContainerId, stocks) {
    const container = document.getElementById(newsContainerId);
    container.innerHTML = '';

    stocks.forEach(s => {
        if (!s.news || s.news.length === 0) return;

        s.news.forEach(n => {
            const div = document.createElement('div');
            div.className = 'news-item';
            div.innerHTML = `
                <div class="news-symbol">${s.symbol} — ${s.name}</div>
                <a href="${n.link}" target="_blank" rel="noopener">${n.title}</a>
                <div class="news-meta">${n.source ? n.source + ' · ' : ''}${n.published || ''}</div>
            `;
            container.appendChild(div);
        });
    });

    if (container.children.length === 0) {
        container.innerHTML = '<div style="color: var(--text-secondary); padding: 10px;">Keine Nachrichten verfügbar</div>';
    }
}

function buildCalendar(events) {
    const container = document.getElementById('calendar-events');
    container.innerHTML = '';

    if (!events || events.length === 0) {
        container.innerHTML = `
            <div class="calendar-empty">
                Keine hochrelevanten Wirtschaftsereignisse (3 Sterne) für heute geplant.
            </div>
        `;
        return;
    }

    events.forEach(ev => {
        const div = document.createElement('div');
        div.className = 'calendar-event';

        // Impact stars
        const stars = ev.impact === 'high' ? '&#9733;&#9733;&#9733;' : '&#9733;&#9733;';
        const impactClass = ev.impact === 'high' ? 'impact-high' : 'impact-medium';

        // Actual value styling
        let actualHtml = '—';
        if (ev.actual && ev.actual !== '') {
            actualHtml = `<span class="cal-actual">${ev.actual}</span>`;
        }

        // Country flag emoji based on currency
        const flagMap = {
            'USD': '&#127482;&#127480;',
            'EUR': '&#127466;&#127482;',
            'GBP': '&#127468;&#127463;',
            'JPY': '&#127471;&#127477;',
            'CAD': '&#127464;&#127462;',
            'AUD': '&#127462;&#127482;',
            'NZD': '&#127475;&#127487;',
            'CHF': '&#127464;&#127469;',
            'CNY': '&#127464;&#127475;',
        };
        const flag = flagMap[ev.currency] || '';

        div.innerHTML = `
            <div class="cal-time">${ev.time || '—'}</div>
            <div class="cal-country">${flag} ${ev.country}</div>
            <div class="cal-title">${ev.title}</div>
            <div class="cal-impact ${impactClass}">${stars}</div>
            <div class="cal-values">
                <div class="cal-val-group">
                    <span class="cal-val-label">Prognose</span>
                    <span class="cal-val">${ev.forecast || '—'}</span>
                </div>
                <div class="cal-val-group">
                    <span class="cal-val-label">Vorher</span>
                    <span class="cal-val">${ev.previous || '—'}</span>
                </div>
                <div class="cal-val-group">
                    <span class="cal-val-label">Aktuell</span>
                    ${actualHtml}
                </div>
            </div>
        `;
        container.appendChild(div);
    });
}

async function loadDashboard(force = false) {
    const loading = document.getElementById('loading');
    const refreshBtn = document.getElementById('refresh-btn');

    loading.classList.remove('hidden');
    if (refreshBtn) refreshBtn.disabled = true;

    try {
        const data = await fetchData(force);

        // Update timestamp
        document.getElementById('timestamp').textContent = `Letzte Aktualisierung: ${data.timestamp}`;

        // Futures bar
        updateFuturesBar(data);

        // Economic calendar
        buildCalendar(data.calendar);

        // S&P 500 section
        updateProbability('sp500-prob', data.sp500.probability);
        buildStockTable('sp500-table', data.sp500.stocks);
        sp500Chart = createChart('sp500-chart', data.sp500.stocks, sp500Chart);
        buildNewsSection('sp500-news', data.sp500.stocks);

        // Nasdaq-100 section
        updateProbability('nq100-prob', data.nq100.probability);
        buildStockTable('nq100-table', data.nq100.stocks);
        nq100Chart = createChart('nq100-chart', data.nq100.stocks, nq100Chart);
        buildNewsSection('nq100-news', data.nq100.stocks);

    } catch (err) {
        console.error('Error loading data:', err);
        alert('Fehler beim Laden der Daten. Bitte erneut versuchen.');
    } finally {
        loading.classList.add('hidden');
        if (refreshBtn) refreshBtn.disabled = false;
    }
}

// Manual refresh always forces fresh data
function manualRefresh() {
    loadDashboard(true);
}

// Auto-refresh every 60 seconds (uses cache)
setInterval(() => loadDashboard(false), 60000);

// Initial load
document.addEventListener('DOMContentLoaded', () => loadDashboard(false));
