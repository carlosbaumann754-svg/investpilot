/* InvestPilot Dashboard - Frontend Logic */

const API = '';
let tradesOffset = 0;

// === AUTH ===
function getToken() { return localStorage.getItem('token'); }

function authHeaders() {
    const token = getToken();
    return {
        'Content-Type': 'application/json',
        'Authorization': token ? `Bearer ${token}` : '',
    };
}

async function apiFetch(url, opts = {}) {
    opts.headers = { ...authHeaders(), ...(opts.headers || {}) };
    const res = await fetch(API + url, opts);
    if (res.status === 401) {
        localStorage.removeItem('token');
        window.location.href = '/login';
        return null;
    }
    return res;
}

function logout() {
    localStorage.removeItem('token');
    window.location.href = '/login';
}

// === TABS ===
function switchTab(name) {
    document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
    document.querySelectorAll('.tab').forEach(el => el.classList.remove('active'));
    document.getElementById('tab-' + name).classList.add('active');
    event.target.classList.add('active');

    if (name === 'trades') loadTrades(true);
    if (name === 'brain') loadBrain();
    if (name === 'reports') loadReports();
    if (name === 'settings') loadSettings();
    if (name === 'logs') loadLogs();
}

// === TOAST ===
function showToast(msg) {
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.classList.add('show');
    setTimeout(() => t.classList.remove('show'), 3000);
}

// === FORMATTING ===
function fmtUsd(v) {
    if (v == null) return '--';
    return '$' + v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function fmtPct(v) {
    if (v == null) return '--';
    return (v >= 0 ? '+' : '') + v.toFixed(2) + '%';
}

function pnlClass(v) { return v >= 0 ? 'positive' : 'negative'; }

function fmtTime(iso) {
    if (!iso) return '--';
    const d = new Date(iso);
    return d.toLocaleDateString('de-CH', { day: '2-digit', month: '2-digit' }) +
           ' ' + d.toLocaleTimeString('de-CH', { hour: '2-digit', minute: '2-digit' });
}

// === DASHBOARD ===
async function loadDashboard() {
    try {
        const [portfolioRes, brainRes, statusRes] = await Promise.all([
            apiFetch('/api/portfolio'),
            apiFetch('/api/brain'),
            apiFetch('/api/trading/status'),
        ]);

        if (portfolioRes) {
            const p = await portfolioRes.json();
            if (!p.error) {
                document.getElementById('total-value').textContent = fmtUsd(p.total_value);
                const pnlEl = document.getElementById('total-pnl');
                pnlEl.textContent = `P/L: ${fmtUsd(p.unrealized_pnl)} (${fmtPct(p.invested > 0 ? p.unrealized_pnl / p.invested * 100 : 0)})`;
                pnlEl.className = 'card-sub ' + pnlClass(p.unrealized_pnl);
                document.getElementById('cash-value').textContent = fmtUsd(p.credit);
                document.getElementById('invested-value').textContent = fmtUsd(p.invested);
                document.getElementById('num-positions').textContent = p.num_positions;

                const tbody = document.getElementById('positions-table');
                tbody.innerHTML = '';
                (p.positions || []).forEach(pos => {
                    const tr = document.createElement('tr');
                    tr.innerHTML = `
                        <td>#${pos.instrument_id}</td>
                        <td>${fmtUsd(pos.invested)}</td>
                        <td class="${pnlClass(pos.pnl)}">${fmtUsd(pos.pnl)}</td>
                        <td class="${pnlClass(pos.pnl_pct)}">${fmtPct(pos.pnl_pct)}</td>
                        <td>${pos.leverage}x</td>
                    `;
                    tbody.appendChild(tr);
                });
            }
        }

        if (brainRes) {
            const b = await brainRes.json();
            if (!b.error) {
                const regimeBadge = document.getElementById('brain-regime');
                const regimeMap = { bull: 'badge-green', bear: 'badge-red', sideways: 'badge-orange', unknown: 'badge-blue' };
                regimeBadge.className = 'badge ' + (regimeMap[b.market_regime] || 'badge-blue');
                regimeBadge.textContent = (b.market_regime || 'unknown').toUpperCase();
                document.getElementById('brain-stats').textContent =
                    `Win: ${b.win_rate?.toFixed(1) || 0}% | Sharpe: ${b.sharpe_estimate?.toFixed(2) || 0}`;
            }
        }

        if (statusRes) {
            const s = await statusRes.json();
            const toggle = document.getElementById('trading-toggle');
            const label = document.getElementById('toggle-label');
            const badge = document.getElementById('trading-status-badge');
            toggle.checked = s.enabled;
            label.textContent = s.enabled ? 'ON' : 'OFF';
            badge.className = 'badge ' + (s.enabled ? 'badge-green' : 'badge-red');
            badge.textContent = s.enabled ? 'AKTIV' : 'GESTOPPT';
            document.getElementById('last-run').textContent =
                s.last_run ? `Letzter Lauf: ${s.last_run}` : 'Noch kein Lauf';
        }
    } catch (err) {
        console.error('Dashboard load error:', err);
    }
}

// === TRADING TOGGLE ===
async function toggleTrading(enabled) {
    const endpoint = enabled ? '/api/trading/start' : '/api/trading/stop';
    await apiFetch(endpoint, { method: 'POST' });
    document.getElementById('toggle-label').textContent = enabled ? 'ON' : 'OFF';
    showToast(enabled ? 'Trading aktiviert' : 'Trading gestoppt');
}

// === TRADES ===
async function loadTrades(reset = false) {
    if (reset) tradesOffset = 0;
    const res = await apiFetch(`/api/trades?limit=50&offset=${tradesOffset}`);
    if (!res) return;
    const data = await res.json();
    const tbody = document.getElementById('trades-table');
    if (reset) tbody.innerHTML = '';

    (data.trades || []).forEach(t => {
        const tr = document.createElement('tr');
        const actionClass = t.action === 'BUY' ? 'badge-green' :
                            t.action.includes('STOP_LOSS') ? 'badge-red' :
                            t.action.includes('TAKE_PROFIT') ? 'badge-purple' : 'badge-blue';
        tr.innerHTML = `
            <td>${fmtTime(t.timestamp)}</td>
            <td><span class="badge ${actionClass}">${t.action}</span></td>
            <td>${t.symbol || '#' + (t.instrument_id || '?')}</td>
            <td>${t.amount_usd ? fmtUsd(t.amount_usd) : (t.pnl_usd ? fmtUsd(t.pnl_usd) : '--')}</td>
            <td>${t.leverage || 1}x</td>
        `;
        tbody.appendChild(tr);
    });
    tradesOffset += 50;
}

function loadMoreTrades() { loadTrades(false); }

// === BRAIN ===
async function loadBrain() {
    const res = await apiFetch('/api/brain');
    if (!res) return;
    const b = await res.json();
    if (b.error) return;

    document.getElementById('brain-regime-detail').textContent = (b.market_regime || 'unknown').toUpperCase();
    document.getElementById('brain-runs').textContent = b.total_runs || 0;
    document.getElementById('brain-winrate').textContent = (b.win_rate?.toFixed(1) || '0') + '%';
    document.getElementById('brain-sharpe').textContent = b.sharpe_estimate?.toFixed(2) || '0';
    document.getElementById('brain-rules').textContent = (b.learned_rules || []).length;

    // Scores table
    const tbody = document.getElementById('scores-table');
    tbody.innerHTML = '';
    const scores = b.instrument_scores || {};
    Object.entries(scores).forEach(([iid, s]) => {
        const scoreColor = s.score > 0 ? 'var(--green)' : 'var(--red)';
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td>#${iid}</td>
            <td style="color:${scoreColor}; font-weight:700">${s.score}</td>
            <td>${fmtPct(s.avg_return_pct)}</td>
            <td>${s.consistency}%</td>
            <td class="${pnlClass(s.trend)}">${s.trend >= 0 ? '+' : ''}${s.trend}</td>
        `;
        tbody.appendChild(tr);
    });

    // Rules list
    const rulesEl = document.getElementById('rules-list');
    const rules = b.learned_rules || [];
    if (rules.length === 0) {
        rulesEl.innerHTML = '<span style="color:var(--text-dim)">Noch keine Regeln gelernt (min. 5 Laeufe)</span>';
    } else {
        rulesEl.innerHTML = rules.map(r =>
            `<div style="margin-bottom:8px; padding:8px; background:var(--bg-input); border-radius:8px">
                <span class="badge badge-purple">${r.type}</span>
                <div style="margin-top:4px">${r.reason}</div>
                <div style="color:var(--text-dim); font-size:11px">${fmtTime(r.created)} | Conf: ${((r.confidence || 0) * 100).toFixed(0)}%</div>
            </div>`
        ).join('');
    }
}

// === STRATEGY PRESETS ===
const STRATEGY_PRESETS = {
    aggressive_day_trade: {
        desc: 'Hohes Risiko, hohe Rendite. Enge SL/TP, 2x Leverage, haeufiges Rebalancing.',
        stop_loss_pct: -3, take_profit_pct: 5, rebalance_threshold_pct: 2,
        default_leverage: 2, max_single_trade_usd: 3000,
    },
    balanced_growth: {
        desc: 'Mittleres Risiko. Breite Streuung, moderater Leverage, langfristiges Wachstum.',
        stop_loss_pct: -8, take_profit_pct: 15, rebalance_threshold_pct: 5,
        default_leverage: 1, max_single_trade_usd: 5000,
    },
    conservative_etf: {
        desc: 'Niedriges Risiko. ETF-lastig, kein Leverage, seltenes Rebalancing.',
        stop_loss_pct: -15, take_profit_pct: 25, rebalance_threshold_pct: 10,
        default_leverage: 1, max_single_trade_usd: 10000,
    },
    custom: {
        desc: 'Eigene Parameter frei konfigurieren.',
    },
};

function onStrategyPreset(name) {
    const preset = STRATEGY_PRESETS[name];
    if (!preset) return;
    document.getElementById('strategy-desc').textContent = preset.desc || '';
    if (name === 'custom') return; // Don't overwrite fields
    document.getElementById('cfg-sl').value = preset.stop_loss_pct;
    document.getElementById('cfg-tp').value = preset.take_profit_pct;
    document.getElementById('cfg-rebalance').value = preset.rebalance_threshold_pct;
    document.getElementById('cfg-leverage').value = preset.default_leverage;
    document.getElementById('cfg-max-trade').value = preset.max_single_trade_usd;
}

// === SETTINGS ===
async function loadSettings() {
    const res = await apiFetch('/api/config');
    if (!res) return;
    const cfg = await res.json();

    // Strategy selector
    const stratSelect = document.getElementById('cfg-strategy');
    const knownStrategies = Object.keys(STRATEGY_PRESETS);
    if (knownStrategies.includes(cfg.strategy)) {
        stratSelect.value = cfg.strategy;
    } else {
        stratSelect.value = 'custom';
    }
    const preset = STRATEGY_PRESETS[stratSelect.value];
    document.getElementById('strategy-desc').textContent = preset?.desc || '';

    document.getElementById('cfg-sl').value = cfg.stop_loss_pct;
    document.getElementById('cfg-tp').value = cfg.take_profit_pct;
    document.getElementById('cfg-rebalance').value = cfg.rebalance_threshold_pct;
    document.getElementById('cfg-leverage').value = cfg.default_leverage;
    document.getElementById('cfg-max-trade').value = cfg.max_single_trade_usd || 5000;

    // Allocation editor
    const editor = document.getElementById('allocation-editor');
    const targets = cfg.portfolio_targets || {};
    editor.innerHTML = '';
    Object.entries(targets).forEach(([sym, t]) => {
        editor.innerHTML += `
            <div style="display:flex; align-items:center; gap:8px; margin-bottom:8px">
                <span style="width:60px; font-weight:600">${sym}</span>
                <input type="number" id="alloc-${sym}" value="${t.allocation_pct}" step="1" min="0" max="100"
                    style="flex:1; padding:10px; background:var(--bg-input); border:1px solid var(--border); border-radius:8px; color:var(--text); font-size:16px">
                <span style="color:var(--text-dim)">%</span>
            </div>
        `;
    });
}

async function saveSettings(e) {
    e.preventDefault();

    const update = {
        strategy: document.getElementById('cfg-strategy').value,
        stop_loss_pct: parseFloat(document.getElementById('cfg-sl').value),
        take_profit_pct: parseFloat(document.getElementById('cfg-tp').value),
        rebalance_threshold_pct: parseFloat(document.getElementById('cfg-rebalance').value),
        default_leverage: parseInt(document.getElementById('cfg-leverage').value),
        max_single_trade_usd: parseFloat(document.getElementById('cfg-max-trade').value),
    };

    const res = await apiFetch('/api/config/strategy', {
        method: 'PUT',
        body: JSON.stringify(update),
    });

    if (res && res.ok) {
        showToast('Strategie gespeichert');
    } else {
        const err = await res?.json();
        showToast('Fehler: ' + (err?.detail || 'Unbekannt'));
    }
}

// === LOGS ===
async function loadLogs() {
    const res = await apiFetch('/api/logs?lines=200');
    if (!res) return;
    const data = await res.json();
    const viewer = document.getElementById('log-viewer');

    viewer.innerHTML = (data.lines || []).map(line => {
        let cls = 'log-info';
        if (line.includes('[ERROR]')) cls = 'log-error';
        else if (line.includes('[WARNING]')) cls = 'log-warn';
        return `<span class="${cls}">${line}</span>`;
    }).join('\n');

    viewer.scrollTop = viewer.scrollHeight;
}

// === REPORTS ===
async function loadReports() {
    // Lade letzten Report
    try {
        const res = await apiFetch('/api/weekly-report');
        if (res) {
            const r = await res.json();
            if (r.performance) {
                document.getElementById('report-summary').style.display = 'block';
                const perf = r.performance;
                const ret = perf.total_return_pct || 0;
                const retEl = document.getElementById('rpt-return');
                retEl.textContent = fmtPct(ret);
                retEl.className = 'card-value ' + pnlClass(ret);
                retEl.style.fontSize = '20px';
                document.getElementById('rpt-winrate').textContent = (perf.win_rate?.toFixed(1) || '0') + '%';
                document.getElementById('rpt-trades').textContent = r.weekly_trades?.total_trades || 0;

                const sugEl = document.getElementById('rpt-suggestions');
                const sugs = r.suggestions || [];
                if (sugs.length === 0) {
                    sugEl.innerHTML = '<span style="color:var(--green)">Alles OK - keine Verbesserungen noetig</span>';
                } else {
                    sugEl.innerHTML = sugs.map(s => {
                        const color = s.prioritaet === 'HOCH' ? 'var(--red)' : s.prioritaet === 'MITTEL' ? 'var(--orange)' : 'var(--green)';
                        return `<div style="margin-bottom:6px;padding:6px;background:var(--bg-input);border-radius:6px;border-left:3px solid ${color}">
                            <span style="color:${color};font-weight:bold;font-size:11px">${s.prioritaet}</span>
                            <span style="color:var(--text-dim);font-size:11px"> ${s.bereich}</span>
                            <div style="margin-top:2px">${s.vorschlag}</div>
                            <div style="color:var(--accent);font-size:11px;margin-top:2px">${s.aktion}</div>
                        </div>`;
                    }).join('');
                }
            }
        }
    } catch (e) { console.error('Report load:', e); }

    // Lade Discovery Ergebnisse
    try {
        const res = await apiFetch('/api/discovery');
        if (res) {
            const d = await res.json();
            if (d.new_found > 0) {
                document.getElementById('discovery-results').style.display = 'block';
                document.getElementById('disc-found').textContent = d.new_found;
                document.getElementById('disc-evaluated').textContent = d.evaluated;
                document.getElementById('disc-added').textContent = d.added;

                const tbody = document.getElementById('disc-top-table');
                tbody.innerHTML = '';
                (d.top_10 || []).forEach(a => {
                    const scoreColor = a.score >= 15 ? 'var(--green)' : a.score >= 0 ? 'var(--text)' : 'var(--red)';
                    const tr = document.createElement('tr');
                    tr.innerHTML = `
                        <td style="font-weight:600">${a.symbol}</td>
                        <td>${a.name}</td>
                        <td><span class="badge badge-blue">${a.class}</span></td>
                        <td style="color:${scoreColor};font-weight:700">${a.score?.toFixed(1) || '--'}</td>
                    `;
                    tbody.appendChild(tr);
                });
            }
        }
    } catch (e) { console.error('Discovery load:', e); }

    // Lade PDF-Liste
    try {
        const res = await apiFetch('/api/weekly-report/pdfs');
        if (res) {
            const data = await res.json();
            if (data.pdfs && data.pdfs.length > 0) {
                document.getElementById('pdf-list-card').style.display = 'block';
                document.getElementById('pdf-list').innerHTML = data.pdfs.map(p =>
                    `<div style="display:flex;justify-content:space-between;align-items:center;padding:8px;border-bottom:1px solid var(--border)">
                        <span>${p.filename}</span>
                        <span style="color:var(--text-dim);font-size:12px">${p.size_kb} KB</span>
                    </div>`
                ).join('');
            }
        }
    } catch (e) { console.error('PDF list load:', e); }
}

async function generateReport() {
    showToast('Report wird generiert...');
    try {
        const res = await apiFetch('/api/weekly-report/send', { method: 'POST' });
        if (res) {
            const data = await res.json();
            if (data.error) {
                showToast('Fehler: ' + data.error);
            } else {
                showToast('Report generiert! ' + data.trades_this_week + ' Trades diese Woche');
                loadReports();
            }
        }
    } catch (e) {
        showToast('Report-Fehler: ' + e.message);
    }
}

async function downloadReportPdf() {
    showToast('PDF wird erstellt...');
    try {
        const res = await apiFetch('/api/weekly-report/pdf');
        if (res && res.ok) {
            const blob = await res.blob();
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = 'InvestPilot_Report.pdf';
            a.click();
            URL.revokeObjectURL(url);
            showToast('PDF heruntergeladen');
        } else {
            showToast('PDF nicht verfuegbar');
        }
    } catch (e) {
        showToast('PDF-Fehler: ' + e.message);
    }
}

async function runDiscovery() {
    showToast('Asset Discovery gestartet... (kann 2-3 Min. dauern)');
    try {
        const res = await apiFetch('/api/discovery/run', { method: 'POST' });
        if (res) {
            const data = await res.json();
            if (data.error) {
                showToast('Fehler: ' + data.error);
            } else {
                showToast(`Discovery: ${data.new_found} neue, ${data.added} hinzugefuegt`);
                loadReports();
            }
        }
    } catch (e) {
        showToast('Discovery-Fehler: ' + e.message);
    }
}

// === INIT ===
(function init() {
    if (!getToken()) {
        window.location.href = '/login';
        return;
    }

    loadDashboard();

    // Auto-refresh
    setInterval(loadDashboard, 60000);
    setInterval(() => {
        if (document.getElementById('tab-logs').classList.contains('active')) {
            loadLogs();
        }
    }, 30000);
})();
