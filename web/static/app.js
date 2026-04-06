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
    if (name === 'backtest') { loadBacktest(); loadOptimizer(); }
    if (name === 'settings') loadSettings();
    if (name === 'logs') loadLogs();
    if (name === 'ask') document.getElementById('ask-input').focus();
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
        const [portfolioRes, brainRes, statusRes, regimeRes, trailRes, sectorRes] = await Promise.all([
            apiFetch('/api/portfolio'),
            apiFetch('/api/brain'),
            apiFetch('/api/trading/status'),
            apiFetch('/api/regime'),
            apiFetch('/api/trailing-sl'),
            apiFetch('/api/sectors'),
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

                // Parse trailing SL data for position enrichment
                let trailData = {};
                if (trailRes) {
                    try {
                        const td = await trailRes.json();
                        (td.active || []).forEach(t => { trailData[t.position_id] = t; });
                    } catch(e) {}
                }

                const tbody = document.getElementById('positions-table');
                tbody.innerHTML = '';
                (p.positions || []).forEach(pos => {
                    const trail = trailData[pos.position_id];
                    const trailTd = trail
                        ? `<td class="badge-green" style="font-size:11px;">${fmtUsd(trail.sl_level)}</td>`
                        : '<td style="color:#666;">--</td>';
                    const tr = document.createElement('tr');
                    tr.innerHTML = `
                        <td>#${pos.instrument_id}</td>
                        <td>${fmtUsd(pos.invested)}</td>
                        <td class="${pnlClass(pos.pnl)}">${fmtUsd(pos.pnl)}</td>
                        <td class="${pnlClass(pos.pnl_pct)}">${fmtPct(pos.pnl_pct)}</td>
                        <td>${pos.leverage}x</td>
                        ${trailTd}
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

        // Regime Status
        if (regimeRes) {
            const r = await regimeRes.json();
            const el = document.getElementById('regime-status');
            if (el && !r.error) {
                let html = '';
                if (r.vix_level != null) {
                    const vixClass = r.vix_regime === 'high_fear' ? 'badge-red' : r.vix_regime === 'elevated' ? 'badge-orange' : 'badge-green';
                    html += `<span class="badge ${vixClass}">VIX ${r.vix_level?.toFixed(1)}</span> `;
                }
                if (r.trading_halted) {
                    html += '<span class="badge badge-red">REGIME HALT</span> ';
                }
                if (r.recovery_mode) {
                    html += '<span class="badge badge-orange">RECOVERY</span> ';
                }
                if (!r.trading_halted && !r.recovery_mode) {
                    html += '<span class="badge badge-green">NORMAL</span>';
                }
                el.innerHTML = html;

                // Regime Detail Fields
                const det = document.getElementById('regime-details');
                if (det) {
                    det.style.display = 'block';
                    const setVal = (id, val) => { const e = document.getElementById(id); if (e) e.textContent = val; };
                    const setBadge = (id, val, cls) => { const e = document.getElementById(id); if (e) { e.textContent = val; e.className = 'badge ' + cls; } };
                    setVal('regime-vix-value', r.vix_level != null ? r.vix_level.toFixed(1) : '--');
                    setBadge('regime-market-value', r.brain_regime || '--',
                        r.brain_regime === 'bear' ? 'badge-red' : r.brain_regime === 'bull' ? 'badge-green' : 'badge-orange');
                    setVal('regime-fg-value', r.fear_greed != null ? r.fear_greed : '--');
                    setBadge('regime-recovery-value', r.recovery_mode ? 'AKTIV' : 'Nein',
                        r.recovery_mode ? 'badge-orange' : 'badge-green');
                    setBadge('regime-halt-value', r.trading_halted ? 'JA' : 'Nein',
                        r.trading_halted ? 'badge-red' : 'badge-green');
                    setBadge('regime-filter-value', r.buy_allowed === false ? 'BLOCKIERT' : 'OK',
                        r.buy_allowed === false ? 'badge-red' : 'badge-green');
                }
            }
        }

        // Sector Strength
        if (sectorRes) {
            try {
                const s = await sectorRes.json();
                const card = document.getElementById('sector-card');
                const badges = document.getElementById('sector-badges');
                if (card && badges && s.sectors) {
                    card.style.display = 'block';
                    badges.innerHTML = Object.entries(s.sectors)
                        .map(([name, data]) => {
                            const pct = data.allocation_pct || 0;
                            const cls = pct > 30 ? 'badge-red' : pct > 20 ? 'badge-orange' : 'badge-blue';
                            return `<span class="badge ${cls}">${name} ${pct.toFixed(0)}% (${data.count})</span>`;
                        }).join('');
                }
            } catch(e) {}
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

// === KILL SWITCH ===
async function killSwitch() {
    const confirmed = confirm(
        'ACHTUNG: Kill Switch aktivieren?\n\n' +
        'Dies schliesst ALLE offenen Positionen sofort\n' +
        'und stoppt den Trading-Bot komplett.\n\n' +
        'Bist du sicher?'
    );
    if (!confirmed) return;

    showToast('Kill Switch wird aktiviert...');
    try {
        const res = await apiFetch('/api/trading/killswitch', { method: 'POST' });
        if (res && res.ok) {
            const data = await res.json();
            const closed = data.closed_positions || 0;
            showToast(`KILL SWITCH AKTIV - ${closed} Positionen geschlossen`);
            document.getElementById('trading-toggle').checked = false;
            document.getElementById('toggle-label').textContent = 'OFF';
            const badge = document.getElementById('trading-status-badge');
            badge.className = 'badge badge-red';
            badge.textContent = 'GESTOPPT';
            loadDashboard();
        } else {
            const err = await res?.json();
            showToast('Kill Switch Fehler: ' + (err?.detail || 'Unbekannt'));
        }
    } catch (e) {
        showToast('Kill Switch Fehler: ' + e.message);
    }
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

// === BACKTEST ===
async function loadBacktest() {
    try {
        const [btRes, mlRes] = await Promise.all([
            apiFetch('/api/backtest'),
            apiFetch('/api/ml-model'),
        ]);

        if (btRes) {
            const bt = await btRes.json();
            if (!bt.error) renderBacktestResults(bt);
        }

        if (mlRes) {
            const ml = await mlRes.json();
            if (!ml.error) renderMLModel(ml);
        }
    } catch (e) {
        console.error('Backtest load:', e);
    }
}

function renderBacktestResults(bt) {
    const fp = bt.full_period || {};
    const m = fp.metrics || {};

    // Show metrics cards
    document.getElementById('bt-metrics-card').style.display = 'block';
    const retEl = document.getElementById('bt-return');
    retEl.textContent = fmtPct(m.total_return_pct);
    retEl.className = 'card-value ' + pnlClass(m.total_return_pct);
    retEl.style.fontSize = '20px';
    document.getElementById('bt-sharpe').textContent = m.sharpe_ratio?.toFixed(2) || '--';
    document.getElementById('bt-maxdd').textContent = m.max_drawdown_pct ? '-' + m.max_drawdown_pct.toFixed(1) + '%' : '--';
    document.getElementById('bt-winrate').textContent = m.win_rate_pct ? m.win_rate_pct.toFixed(1) + '%' : '--';
    document.getElementById('bt-trades').textContent = m.total_trades || '--';
    document.getElementById('bt-pf').textContent = m.profit_factor?.toFixed(2) || '--';
    document.getElementById('bt-avgdays').textContent = m.avg_trade_days ? m.avg_trade_days.toFixed(1) + 'd' : '--';
    document.getElementById('bt-costs').textContent = m.total_costs_pct ? m.total_costs_pct.toFixed(1) + '%' : '--';
    document.getElementById('bt-timestamp').textContent = bt.timestamp ? 'Backtest: ' + fmtTime(bt.timestamp) : '';

    // Walk-Forward table
    if (bt.in_sample && bt.out_of_sample) {
        document.getElementById('bt-walkforward-card').style.display = 'block';
        const wfBody = document.getElementById('bt-wf-table');
        wfBody.innerHTML = '';
        const is = bt.in_sample.metrics || {};
        const os = bt.out_of_sample.metrics || {};
        const rows = [
            ['Zeitraum', bt.in_sample.period || '--', bt.out_of_sample.period || '--'],
            ['Rendite', fmtPct(is.total_return_pct), fmtPct(os.total_return_pct)],
            ['Sharpe', is.sharpe_ratio?.toFixed(2) || '--', os.sharpe_ratio?.toFixed(2) || '--'],
            ['Max DD', is.max_drawdown_pct ? '-' + is.max_drawdown_pct.toFixed(1) + '%' : '--', os.max_drawdown_pct ? '-' + os.max_drawdown_pct.toFixed(1) + '%' : '--'],
            ['Win Rate', is.win_rate_pct ? is.win_rate_pct.toFixed(1) + '%' : '--', os.win_rate_pct ? os.win_rate_pct.toFixed(1) + '%' : '--'],
            ['Trades', is.total_trades || '--', os.total_trades || '--'],
            ['Profit Factor', is.profit_factor?.toFixed(2) || '--', os.profit_factor?.toFixed(2) || '--'],
        ];
        rows.forEach(([label, isVal, osVal]) => {
            const tr = document.createElement('tr');
            tr.innerHTML = `<td style="font-weight:600">${label}</td><td>${isVal}</td><td>${osVal}</td>`;
            wfBody.appendChild(tr);
        });
    }

    // Equity Curve SVG
    const curve = fp.equity_curve || [];
    if (curve.length > 2) {
        document.getElementById('bt-equity-card').style.display = 'block';
        document.getElementById('bt-equity-chart').innerHTML = renderEquityCurveSVG(curve);
    }

    // Monthly Returns
    const monthly = bt.monthly_returns || {};
    if (Object.keys(monthly).length > 0) {
        document.getElementById('bt-monthly-card').style.display = 'block';
        document.getElementById('bt-monthly-table').innerHTML = renderMonthlyHeatmap(monthly);
    }

    // Best / Worst trades
    if (bt.best_trades || bt.worst_trades) {
        document.getElementById('bt-trades-cards').style.display = 'grid';
        renderTradeTable('bt-best-table', bt.best_trades || []);
        renderTradeTable('bt-worst-table', bt.worst_trades || []);
    }
}

function renderTradeTable(id, trades) {
    const tbody = document.getElementById(id);
    tbody.innerHTML = '';
    trades.forEach(t => {
        const tr = document.createElement('tr');
        const color = t.pnl_net_pct >= 0 ? 'var(--green)' : 'var(--red)';
        tr.innerHTML = `
            <td style="font-weight:600">${t.symbol}</td>
            <td style="color:${color};font-weight:700">${fmtPct(t.pnl_net_pct)}</td>
            <td>${t.days_held}d</td>
            <td style="font-size:11px">${t.exit_reason}</td>
        `;
        tbody.appendChild(tr);
    });
}

function renderEquityCurveSVG(curve) {
    const W = 700, H = 250, PAD = 40;
    const values = curve.map(c => c[1]);
    const minV = Math.min(...values) * 0.98;
    const maxV = Math.max(...values) * 1.02;
    const rangeV = maxV - minV || 1;

    const scaleX = (i) => PAD + (i / (values.length - 1)) * (W - PAD * 2);
    const scaleY = (v) => H - PAD - ((v - minV) / rangeV) * (H - PAD * 2);

    let path = `M ${scaleX(0)} ${scaleY(values[0])}`;
    for (let i = 1; i < values.length; i++) {
        path += ` L ${scaleX(i)} ${scaleY(values[i])}`;
    }

    // Start value line
    const startY = scaleY(10000);

    // Grid lines
    let gridLines = '';
    const steps = 5;
    for (let i = 0; i <= steps; i++) {
        const v = minV + (rangeV / steps) * i;
        const y = scaleY(v);
        gridLines += `<line x1="${PAD}" y1="${y}" x2="${W - PAD}" y2="${y}" stroke="#252839" stroke-width="1"/>`;
        gridLines += `<text x="${PAD - 5}" y="${y + 4}" fill="#94a3b8" font-size="10" text-anchor="end">${Math.round(v).toLocaleString()}</text>`;
    }

    // Date labels
    let dateLabels = '';
    const labelCount = Math.min(6, curve.length);
    for (let i = 0; i < labelCount; i++) {
        const idx = Math.floor(i * (curve.length - 1) / (labelCount - 1));
        const x = scaleX(idx);
        const date = curve[idx][0];
        dateLabels += `<text x="${x}" y="${H - 5}" fill="#94a3b8" font-size="10" text-anchor="middle">${date.substring(0, 7)}</text>`;
    }

    return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;max-height:300px">
        ${gridLines}
        <line x1="${PAD}" y1="${startY}" x2="${W - PAD}" y2="${startY}" stroke="#60a5fa" stroke-width="1" stroke-dasharray="4,4" opacity="0.5"/>
        <path d="${path}" fill="none" stroke="#60a5fa" stroke-width="2"/>
        ${dateLabels}
        <text x="${PAD}" y="15" fill="#94a3b8" font-size="11">Equity ($)</text>
    </svg>`;
}

function renderMonthlyHeatmap(monthly) {
    const months = Object.keys(monthly).sort();
    if (months.length === 0) return '';

    // Group by year
    const years = {};
    months.forEach(m => {
        const [y, mo] = m.split('-');
        if (!years[y]) years[y] = {};
        years[y][parseInt(mo)] = monthly[m];
    });

    const moNames = ['Jan', 'Feb', 'Mar', 'Apr', 'Mai', 'Jun', 'Jul', 'Aug', 'Sep', 'Okt', 'Nov', 'Dez'];

    let html = '<thead><tr><th></th>';
    moNames.forEach(n => html += `<th style="padding:4px 6px;font-size:11px">${n}</th>`);
    html += '<th style="padding:4px 6px;font-weight:700">Jahr</th></tr></thead><tbody>';

    Object.keys(years).sort().forEach(year => {
        html += `<tr><td style="font-weight:700;padding:4px 8px">${year}</td>`;
        let yearTotal = 0;
        for (let m = 1; m <= 12; m++) {
            const val = years[year][m];
            if (val !== undefined) {
                yearTotal += val;
                const bg = val >= 0 ? `rgba(16,185,129,${Math.min(Math.abs(val) / 10, 0.8)})` :
                                       `rgba(239,68,68,${Math.min(Math.abs(val) / 10, 0.8)})`;
                const color = Math.abs(val) > 3 ? '#fff' : 'var(--text)';
                html += `<td style="padding:4px 6px;text-align:center;background:${bg};color:${color};border-radius:4px">${val.toFixed(1)}</td>`;
            } else {
                html += '<td style="padding:4px 6px;text-align:center;color:var(--text-dim)">-</td>';
            }
        }
        const ybg = yearTotal >= 0 ? 'var(--green)' : 'var(--red)';
        html += `<td style="padding:4px 8px;font-weight:700;color:${ybg}">${yearTotal.toFixed(1)}%</td></tr>`;
    });

    html += '</tbody>';
    return html;
}

function renderMLModel(ml) {
    if (ml.error && !ml.test_accuracy) return;

    document.getElementById('bt-ml-card').style.display = 'block';
    document.getElementById('ml-accuracy').textContent = ml.test_accuracy ? ml.test_accuracy.toFixed(1) + '%' : '--';
    document.getElementById('ml-precision').textContent = ml.test_precision ? ml.test_precision.toFixed(1) + '%' : '--';
    document.getElementById('ml-recall').textContent = ml.test_recall ? ml.test_recall.toFixed(1) + '%' : '--';
    document.getElementById('ml-f1').textContent = ml.test_f1 ? ml.test_f1.toFixed(1) + '%' : '--';
    document.getElementById('ml-trained-at').textContent = ml.trained ? 'Trainiert: ' + fmtTime(ml.trained) : '';

    // Feature importances bar chart (SVG)
    const fi = ml.feature_importances || {};
    const entries = Object.entries(fi).slice(0, 10);
    if (entries.length > 0) {
        const maxVal = Math.max(...entries.map(e => e[1]));
        const barH = 22, gap = 4;
        const svgH = entries.length * (barH + gap) + 10;

        let bars = '';
        entries.forEach(([name, val], i) => {
            const y = i * (barH + gap);
            const w = maxVal > 0 ? (val / maxVal) * 400 : 0;
            bars += `
                <text x="120" y="${y + 15}" fill="#94a3b8" font-size="11" text-anchor="end">${name}</text>
                <rect x="130" y="${y + 2}" width="${w}" height="${barH - 4}" fill="#60a5fa" rx="3"/>
                <text x="${135 + w}" y="${y + 15}" fill="#e2e8f0" font-size="10">${(val * 100).toFixed(1)}%</text>
            `;
        });

        document.getElementById('ml-features-chart').innerHTML =
            `<svg viewBox="0 0 600 ${svgH}" style="width:100%;height:auto">${bars}</svg>`;
    }
}

async function runBacktest() {
    const btn = document.getElementById('btn-run-backtest');
    btn.disabled = true;
    btn.textContent = 'Backtest laeuft...';
    showToast('Backtest gestartet (kann 1-3 Minuten dauern)...');

    try {
        const res = await apiFetch('/api/backtest/run', { method: 'POST' });
        if (res && res.ok) {
            const data = await res.json();
            showToast('Backtest abgeschlossen!');
            if (data.results) renderBacktestResults(data.results);
        } else {
            const err = await res?.json();
            showToast('Backtest Fehler: ' + (err?.detail || 'Unbekannt'));
        }
    } catch (e) {
        showToast('Backtest Fehler: ' + e.message);
    } finally {
        btn.disabled = false;
        btn.textContent = 'Run Backtest';
    }
}

async function trainML() {
    const btn = document.getElementById('btn-train-ml');
    btn.disabled = true;
    btn.textContent = 'Training laeuft...';
    showToast('ML-Modell wird trainiert...');

    try {
        const res = await apiFetch('/api/ml-model/train', { method: 'POST' });
        if (res && res.ok) {
            const data = await res.json();
            showToast('ML-Modell trainiert!');
            if (data.model_info) renderMLModel(data.model_info);
        } else {
            const err = await res?.json();
            showToast('ML Training Fehler: ' + (err?.detail || 'Unbekannt'));
        }
    } catch (e) {
        showToast('ML Training Fehler: ' + e.message);
    } finally {
        btn.disabled = false;
        btn.textContent = 'Train ML Model';
    }
}

// === OPTIMIZER ===

async function loadOptimizer() {
    try {
        const res = await apiFetch('/api/optimizer');
        if (!res || !res.ok) return;
        const data = await res.json();
        renderOptimizer(data);
    } catch (e) {
        console.error('Optimizer load error:', e);
    }
}

function renderOptimizer(data) {
    const runs = data.runs || [];
    if (!runs.length) {
        showToast('Noch keine Optimierung gelaufen');
        return;
    }

    const last = runs[runs.length - 1];
    const details = last.details || {};

    // Status card
    const card = document.getElementById('opt-status-card');
    card.style.display = 'block';

    const actionEl = document.getElementById('opt-action');
    actionEl.textContent = last.action || '--';
    actionEl.className = last.action === 'optimized' ? 'card-value positive' : 'card-value';

    const ts = last.timestamp ? new Date(last.timestamp) : null;
    document.getElementById('opt-time').textContent = ts
        ? ts.toLocaleDateString('de-DE') + ' ' + ts.toLocaleTimeString('de-DE', {hour:'2-digit',minute:'2-digit'})
        : '--';

    const gs = details.grid_search || {};
    document.getElementById('opt-tested').textContent = gs.total_tested || '--';
    document.getElementById('opt-sharpe').textContent = gs.best_oos_sharpe != null
        ? gs.best_oos_sharpe.toFixed(2) : '--';

    // Changes
    const changes = last.details?.changes || {};
    const changesTable = document.getElementById('opt-changes-table');
    const changesCard = document.getElementById('opt-changes-card');

    if (Object.keys(changes).length > 0) {
        changesCard.style.display = 'block';
        changesTable.innerHTML = '';
        for (const [key, val] of Object.entries(changes)) {
            const tr = document.createElement('tr');
            tr.innerHTML = `<td>${key}</td>
                <td style="color:var(--text-dim)">${val.old}</td>
                <td style="color:var(--green);font-weight:bold">${val.new}</td>`;
            changesTable.appendChild(tr);
        }
    }

    // History
    if (runs.length > 1) {
        const histCard = document.getElementById('opt-history-card');
        histCard.style.display = 'block';
        const histTable = document.getElementById('opt-history-table');
        histTable.innerHTML = '';

        for (const run of runs.slice().reverse().slice(0, 10)) {
            const tr = document.createElement('tr');
            const rts = run.timestamp ? new Date(run.timestamp).toLocaleDateString('de-DE') : '?';
            const rchanges = run.details?.changes || {};
            const changeList = Object.entries(rchanges)
                .map(([k,v]) => `${k}: ${v.old} → ${v.new}`).join(', ') || 'keine';
            const badge = run.action === 'optimized' ? 'badge-green'
                : run.action === 'rollback' ? 'badge-red' : 'badge-blue';
            tr.innerHTML = `<td>${rts}</td>
                <td><span class="badge ${badge}">${run.action}</span></td>
                <td style="font-size:12px">${changeList}</td>`;
            histTable.appendChild(tr);
        }
    }
}

async function runOptimizer() {
    const btn = document.getElementById('btn-run-optimizer');
    btn.disabled = true;
    btn.textContent = 'Optimierung laeuft...';
    showToast('Optimizer gestartet (kann 5-10 Minuten dauern)...');

    try {
        const res = await apiFetch('/api/optimizer/run', { method: 'POST' });
        if (res && res.ok) {
            const data = await res.json();
            showToast('Optimierung abgeschlossen: ' + (data.result?.action || 'done'));
            if (data.result) {
                loadOptimizer();
                loadBacktest();
            }
        } else {
            const err = await res?.json();
            showToast('Optimizer Fehler: ' + (err?.detail || 'Unbekannt'));
        }
    } catch (e) {
        showToast('Optimizer Fehler: ' + e.message);
    } finally {
        btn.disabled = false;
        btn.textContent = 'Optimizer starten';
    }
}

async function rollbackOptimizer() {
    if (!confirm('Letzte Optimierung rueckgaengig machen?')) return;

    try {
        const res = await apiFetch('/api/optimizer/rollback', { method: 'POST' });
        if (res && res.ok) {
            const data = await res.json();
            showToast('Rollback erfolgreich');
            loadOptimizer();
        } else {
            const err = await res?.json();
            showToast('Rollback Fehler: ' + (err?.detail || 'Unbekannt'));
        }
    } catch (e) {
        showToast('Rollback Fehler: ' + e.message);
    }
}

// === WATCHDOG ===
async function loadWatchdog() {
    try {
        const res = await apiFetch('/api/diagnostics');
        if (!res) return;
        const data = await res.json();

        const badge = document.getElementById('watchdog-badge');
        const details = document.getElementById('watchdog-details');

        if (data.status === 'healthy') {
            badge.textContent = 'HEALTHY';
            badge.className = 'badge badge-green';
        } else if (data.status === 'warning') {
            badge.textContent = 'WARNING';
            badge.className = 'badge badge-orange';
        } else {
            badge.textContent = 'ERROR';
            badge.className = 'badge badge-red';
        }

        if (data.issues && data.issues.length > 0) {
            details.innerHTML = data.issues.map(i => '• ' + i).join('<br>');
        } else {
            details.textContent = 'Alle Checks bestanden';
        }
    } catch (e) {
        document.getElementById('watchdog-badge').textContent = 'OFFLINE';
        document.getElementById('watchdog-badge').className = 'badge badge-red';
    }
}

// === ASK (Q&A Chat) ===
async function askQuestion() {
    const input = document.getElementById('ask-input');
    const question = input.value.trim();
    if (!question) return;

    const btn = document.getElementById('ask-btn');
    btn.disabled = true;
    btn.textContent = 'Denke...';
    input.disabled = true;

    // Frage anzeigen
    const history = document.getElementById('ask-history');
    const qCard = document.createElement('div');
    qCard.className = 'card';
    qCard.style.borderLeft = '3px solid var(--blue)';
    qCard.innerHTML = '<div class="card-sub" style="color:var(--blue);margin-bottom:4px;">Deine Frage</div>' +
        '<div>' + question.replace(/</g, '&lt;') + '</div>';
    history.appendChild(qCard);

    input.value = '';

    try {
        const res = await apiFetch('/api/ask', {
            method: 'POST',
            body: JSON.stringify({ question }),
        });

        const data = await res.json();
        const aCard = document.createElement('div');
        aCard.className = 'card';
        aCard.style.borderLeft = '3px solid var(--green)';

        if (data.error) {
            aCard.style.borderLeftColor = 'var(--red)';
            aCard.innerHTML = '<div class="card-sub" style="color:var(--red);margin-bottom:4px;">Fehler</div>' +
                '<div>' + data.error + '</div>';
        } else {
            const answer = (data.answer || '').replace(/</g, '&lt;').replace(/\n/g, '<br>');
            const tokens = data.tokens_used ? ' (' + data.tokens_used + ' Tokens)' : '';
            aCard.innerHTML = '<div class="card-sub" style="color:var(--green);margin-bottom:4px;">Antwort' + tokens + '</div>' +
                '<div style="line-height:1.6;">' + answer + '</div>';
        }
        history.appendChild(aCard);
        aCard.scrollIntoView({ behavior: 'smooth' });
    } catch (e) {
        showToast('Fehler: ' + e.message);
    }

    btn.disabled = false;
    btn.textContent = 'Fragen';
    input.disabled = false;
    input.focus();
}

// === INIT ===
(function init() {
    if (!getToken()) {
        window.location.href = '/login';
        return;
    }

    loadDashboard();
    loadWatchdog();

    // Auto-refresh
    setInterval(loadDashboard, 60000);
    setInterval(loadWatchdog, 300000); // Watchdog alle 5 Min
    setInterval(() => {
        if (document.getElementById('tab-logs').classList.contains('active')) {
            loadLogs();
        }
    }, 30000);
})();
