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
    if (name === 'reports') { loadReports(); loadLastRunTimestamps(); }
    if (name === 'backtest') { loadBacktest(); loadOptimizer(); loadKellySweep(); loadLastRunTimestamps(); }
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

// === BENCHMARK (Bot vs. SPY) ===
let _lastPnlPeriods = null; // Cache fuer Alpha-Berechnung

function renderBenchmark(benchData) {
    const grid = document.getElementById('benchmark-grid');
    const meta = document.getElementById('benchmark-meta');
    if (!grid) return;

    if (!benchData || benchData.error || !Array.isArray(benchData.periods)) {
        grid.innerHTML = '<div class="pnl-period-cell"><div class="pnl-label">Benchmark nicht verfuegbar</div></div>';
        if (meta) meta.textContent = benchData?.error || 'Benchmark-Daten konnten nicht geladen werden';
        return;
    }

    // Portfolio-Returns aus dem Cache ziehen (gleiche Window-Keys)
    const portfolioByKey = {};
    if (_lastPnlPeriods && Array.isArray(_lastPnlPeriods.periods)) {
        _lastPnlPeriods.periods.forEach(p => { portfolioByKey[p.key] = p.pnl_pct; });
    }

    // Benchmarks-Liste aus Backend (kann SPY/QQQ/60-40 enthalten)
    const benches = Array.isArray(benchData.benchmarks) && benchData.benchmarks.length
        ? benchData.benchmarks
        : [{ key: 'spy', label: 'SPY', name: 'S&P 500 ETF' }];

    const cellWithVal = (val) => {
        if (val == null) return '<span class="bench-value">--</span>';
        const cls = val >= 0 ? 'positive' : 'negative';
        return `<span class="bench-value ${cls}">${fmtPct(val)}</span>`;
    };

    grid.innerHTML = benchData.periods.map(p => {
        const bot = portfolioByKey[p.key];
        const botRow = `<div class="bench-cell-row" style="border-bottom:1px solid var(--border);padding-bottom:3px;margin-bottom:3px;"><span class="bench-label" style="font-weight:600;">Bot</span>${cellWithVal(bot)}</div>`;

        const benchRows = benches.map(b => {
            const v = p[`${b.key}_pct`];
            const a = (v != null && bot != null) ? (bot - v) : null;
            const aCls = a == null ? '' : (a >= 0 ? 'positive' : 'negative');
            const aTxt = a == null ? '--' : fmtPct(a);
            return `
                <div class="bench-cell-row"><span class="bench-label">${b.label}</span>${cellWithVal(v)}</div>
                <div class="bench-cell-row bench-alpha" style="opacity:0.85;"><span class="bench-label">&nbsp;α</span><span class="bench-value ${aCls}">${aTxt}</span></div>
            `;
        }).join('');

        return `
            <div class="pnl-period-cell" style="text-align:left;">
                <div class="pnl-label" style="text-align:center;margin-bottom:4px;">${p.label}</div>
                ${botRow}
                ${benchRows}
            </div>
        `;
    }).join('');

    if (meta) {
        const stale = benchData.latest_close_date ? `Stand: ${benchData.latest_close_date}` : '';
        const benchNames = benches.map(b => `${b.label}=${b.name}`).join(' | ');
        meta.textContent = `${benchNames} | α = Bot − Benchmark | ${stale}`;
    }
}

// === EQUITY HISTORY (Monatstabelle Bot vs Multi-Benchmark) ===
function renderEquityHistory(data) {
    const status = document.getElementById('equity-history-status');
    const table = document.getElementById('equity-history-table');
    const tbody = document.getElementById('equity-history-tbody');
    const meta = document.getElementById('equity-history-meta');
    if (!status || !tbody) return;

    if (!data || data.error) {
        status.textContent = 'Equity-History nicht verfuegbar' + (data?.error ? `: ${data.error}` : '');
        if (table) table.style.display = 'none';
        if (meta) meta.textContent = '';
        return;
    }

    const total = data.snapshots_total || 0;
    const minReq = data.min_required || 5;

    if (!data.ready || !Array.isArray(data.monthly) || data.monthly.length === 0) {
        // Progress-Anzeige solange noch nicht genug Snapshots gesammelt
        const pct = Math.min(100, Math.round((total / minReq) * 100));
        status.innerHTML = `Daten werden gesammelt: <strong>${total} / ${minReq}</strong> Tages-Snapshots `
            + `(${pct}%). Erste Monatszeile erscheint sobald genug Daten vorliegen. `
            + `Snapshots laufen taeglich ~22:30 CET nach Boersenschluss.`;
        if (table) table.style.display = 'none';
        if (meta) {
            meta.textContent = total > 0
                ? `Erster Snapshot: ${data.first_date} | Letzter: ${data.last_date}`
                : 'Noch keine Snapshots vorhanden — naechster Lauf heute Abend.';
        }
        return;
    }

    // Daten vorhanden -> Tabelle rendern
    status.innerHTML = `<strong>${total}</strong> Tages-Snapshots gesammelt | `
        + `<strong>${data.monthly.length}</strong> Monats-Zeile(n) berechnet`;
    if (table) table.style.display = '';

    const cellPct = (v) => {
        if (v == null) return '<td>--</td>';
        const cls = v >= 0 ? 'positive' : 'negative';
        return `<td class="${cls}">${fmtPct(v)}</td>`;
    };

    // Neueste Monate oben
    const rows = [...data.monthly].reverse();
    tbody.innerHTML = rows.map(r => {
        const monthLabel = r.month + (r.days_in_month ? ` <span style="opacity:0.6;">(${r.days_in_month}d)</span>` : '');
        return `
            <tr>
                <td>${monthLabel}</td>
                ${cellPct(r.bot_pct)}
                ${cellPct(r.spy_pct)}
                ${cellPct(r.alpha_spy)}
                ${cellPct(r.qqq_pct)}
                ${cellPct(r.alpha_qqq)}
                ${cellPct(r.mix6040_pct)}
                ${cellPct(r.alpha_mix6040)}
            </tr>
        `;
    }).join('');

    if (meta) {
        meta.textContent = `Zeitraum: ${data.first_date} bis ${data.last_date} | `
            + `α = Bot − Benchmark | (Xd) = Anzahl Snapshots im Monat`;
    }
}

async function loadEquityHistory() {
    try {
        const res = await apiFetch('/api/equity-history');
        if (!res) return;
        const data = await res.json();
        renderEquityHistory(data);
    } catch (e) {
        console.error('equity-history load:', e);
    }
}

// === EXIT FORECAST (Abstand jeder offenen Position zum naechsten Exit) ===
function renderExitForecast(data) {
    const status = document.getElementById('exit-forecast-status');
    const table = document.getElementById('exit-forecast-table');
    const tbody = document.getElementById('exit-forecast-tbody');
    const meta = document.getElementById('exit-forecast-meta');
    if (!status || !tbody) return;

    if (!data || data.error) {
        status.textContent = 'Exit-Forecast nicht verfuegbar' + (data?.error ? `: ${data.error}` : '');
        if (table) table.style.display = 'none';
        if (meta) meta.textContent = '';
        return;
    }

    const positions = Array.isArray(data.positions) ? data.positions : [];
    if (positions.length === 0) {
        status.textContent = 'Keine offenen Positionen.';
        if (table) table.style.display = 'none';
        if (meta) meta.textContent = '';
        return;
    }

    status.innerHTML = `<strong>${positions.length}</strong> offene Position(en), sortiert nach Dringlichkeit (naechster Trigger zuerst).`;
    if (table) table.style.display = '';

    const arrow = (dir) => dir === 'up' ? '↑' : (dir === 'down' ? '↓' : '⏱');
    const distFmt = (v) => v == null ? '--' : `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`;

    tbody.innerHTML = positions.map(p => {
        const nt = p.next_trigger;
        const ntLabel = nt ? `${arrow(nt.direction)} ${nt.type}` : '--';
        const ntDist = nt ? distFmt(nt.distance_pct) : '--';
        const pnlCls = (p.pnl_pct || 0) >= 0 ? 'positive' : 'negative';

        // Alle Trigger als kompakte Liste
        const allTriggers = (p.triggers || []).map(t => {
            let txt;
            if (t.type === 'Time-Stop') {
                if (t.eligible_now) {
                    txt = `${t.type}: <strong>JETZT</strong>`;
                } else if (t.days_until != null) {
                    txt = `${t.type}: ${t.days_until}d${t.in_pnl_band ? ' (im PnL-Band)' : ''}`;
                } else {
                    txt = `${t.type}: --`;
                }
            } else if (t.distance_pct == null) {
                txt = `${t.type}: --`;
            } else if (!t.active) {
                txt = `<span style="opacity:0.4;text-decoration:line-through;">${t.type}</span>`;
            } else {
                const dCls = t.direction === 'up' ? 'positive' : (t.direction === 'down' ? 'negative' : '');
                txt = `${t.type}: <span class="${dCls}">${distFmt(t.distance_pct)}</span>`;
            }
            return txt;
        }).join(' · ');

        const ageTxt = p.age_days != null ? `${p.age_days.toFixed(1)}d` : '--';

        // Asset-Kennung: Ticker mit Hover-Tooltip (voller Name)
        const ticker = p.symbol || ('#' + (p.instrument_id || '?'));
        const title = p.name ? `title="${p.name}"` : '';
        const assetCell = `<span ${title} style="${p.name ? 'cursor:help;border-bottom:1px dotted var(--text-dim);' : ''}">${ticker}</span>`;
        return `
            <tr>
                <td>${assetCell}</td>
                <td>${ageTxt}</td>
                <td class="${pnlCls}">${distFmt(p.pnl_pct)}</td>
                <td><strong>${ntLabel}</strong></td>
                <td><strong>${ntDist}</strong></td>
                <td style="font-size:11px;line-height:1.6;white-space:normal;word-break:break-word;min-width:220px;">${allTriggers}</td>
            </tr>
        `;
    }).join('');

    if (meta && data.config_summary) {
        const c = data.config_summary;
        const sl = c.sl_pct ?? -2.5;
        const tp = c.tp_pct ?? 18;
        const trailPct = c.trail_pct ?? 1.8;
        const trailAct = c.trail_activation ?? 0.8;
        const tsDays = c.time_stop?.max_days_stale ?? 10;
        const tranches = (c.tp_tranches || []).map(t => `+${t.profit_target_pct}%`).join(', ') || '--';
        meta.textContent = `Config: SL ${sl}% | TP-Final +${tp}% | Trailing -${trailPct}% ab +${trailAct}% | Tranchen: ${tranches} | Time-Stop ${tsDays}d`;
    }
}

async function loadExitForecast() {
    try {
        const res = await apiFetch('/api/exit-forecast');
        if (!res) return;
        const data = await res.json();
        renderExitForecast(data);
    } catch (e) {
        console.error('exit-forecast load:', e);
    }
}

// === P&L MULTI-PERIOD ===
function renderPnlPeriods(data) {
    const grid = document.getElementById('pnl-periods-grid');
    const meta = document.getElementById('pnl-periods-meta');
    if (!grid || !data || !Array.isArray(data.periods)) return;
    _lastPnlPeriods = data; // fuer Benchmark-Alpha-Berechnung

    grid.innerHTML = data.periods.map(p => {
        const cls = (p.pnl_usd || 0) >= 0 ? 'positive' : 'negative';
        const usdTxt = (p.pnl_usd == null) ? '--' : fmtUsd(p.pnl_usd);
        const pctTxt = (p.pnl_pct == null) ? '' : fmtPct(p.pnl_pct);
        const modeIcon = p.mode === 'hybrid' ? '*' : '';
        return `
            <div class="pnl-period-cell">
                <div class="pnl-label">${p.label}${modeIcon}</div>
                <div class="pnl-usd ${cls}">${usdTxt}</div>
                <div class="pnl-pct ${cls}">${pctTxt}</div>
            </div>
        `;
    }).join('');

    if (meta) {
        meta.textContent = `* = inkl. laufende Positionen | ${data.total_closes_counted || 0} abgeschlossene Trades insgesamt`;
    }
}

// === TOOLTIP CLICK HANDLER (Touch-friendly) ===
document.addEventListener('click', (e) => {
    const tip = e.target.closest('.tip');
    // Schliesse alle anderen aktiven Tooltips
    document.querySelectorAll('.tip.active').forEach(el => {
        if (el !== tip) el.classList.remove('active');
    });
    if (tip) {
        e.stopPropagation();
        tip.classList.toggle('active');
    }
});

// === DASHBOARD ===
async function loadDashboard() {
    try {
        const [portfolioRes, brainRes, statusRes, regimeRes, trailRes, sectorRes, pnlPeriodsRes, benchmarkRes] = await Promise.all([
            apiFetch('/api/portfolio'),
            apiFetch('/api/brain'),
            apiFetch('/api/trading/status'),
            apiFetch('/api/regime'),
            apiFetch('/api/trailing-sl'),
            apiFetch('/api/sectors'),
            apiFetch('/api/pnl-periods'),
            apiFetch('/api/benchmark'),
        ]);

        // P&L Multi-Period Card (muss VOR Benchmark gerendert werden,
        // weil Benchmark die Portfolio-Returns aus dem Cache liest)
        if (pnlPeriodsRes) {
            try {
                const pp = await pnlPeriodsRes.json();
                renderPnlPeriods(pp);
            } catch(e) { console.error('pnl-periods render:', e); }
        }

        // Benchmark Card (Bot vs. Multi-Benchmark: SPY/QQQ/60-40)
        if (benchmarkRes) {
            try {
                const bench = await benchmarkRes.json();
                renderBenchmark(bench);
            } catch(e) { console.error('benchmark render:', e); }
        }

        // Equity-History Monatstabelle (Bot vs. Benchmarks im Zeitverlauf)
        // Eigener Roundtrip — laeuft non-blocking, falls Endpoint langsam ist.
        loadEquityHistory();

        // Exit-Forecast (Abstand jeder offenen Position zum naechsten Trigger)
        // Eigener Roundtrip — hoelt Portfolio neu fuer Trailing-State-Berechnung.
        loadExitForecast();

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
                    // Ticker als Asset-Kennung; Hover-Tooltip mit vollem Namen
                    const ticker = pos.symbol || ('#' + pos.instrument_id);
                    const title = pos.name ? `title="${pos.name}"` : '';
                    const assetCell = `<span ${title} style="${pos.name ? 'cursor:help;border-bottom:1px dotted var(--text-dim);' : ''}">${ticker}</span>`;
                    // v37z: Manueller Sell-Button — bei Cutover-Phase wertvoll
                    // (heute morgen ROKU-Beispiel: nicht mehr in IBKR-App muessen)
                    const sellBtn = pos.symbol
                        ? `<button onclick="manualSell('${pos.symbol}', ${pos.pnl_pct || 0})"
                                   title="Position sofort verkaufen (Confirm-Dialog erscheint)"
                                   class="btn-secondary"
                                   style="font-size:11px;padding:3px 8px;cursor:pointer;">
                             Verkaufen
                          </button>`
                        : '<span style="color:#666;font-size:11px;">--</span>';
                    const tr = document.createElement('tr');
                    tr.innerHTML = `
                        <td>${assetCell}</td>
                        <td>${fmtUsd(pos.invested)}</td>
                        <td class="${pnlClass(pos.pnl)}">${fmtUsd(pos.pnl)}</td>
                        <td class="${pnlClass(pos.pnl_pct)}">${fmtPct(pos.pnl_pct)}</td>
                        <td>${pos.leverage}x</td>
                        ${trailTd}
                        <td>${sellBtn}</td>
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
                    setBadge('regime-market-value', r.market_regime || '--',
                        r.market_regime === 'bear' ? 'badge-red' : r.market_regime === 'bull' ? 'badge-green' : 'badge-orange');
                    setVal('regime-fg-value', r.fear_greed_index != null ? r.fear_greed_index : '--');
                    setBadge('regime-recovery-value', r.recovery_mode ? 'AKTIV' : 'Nein',
                        r.recovery_mode ? 'badge-orange' : 'badge-green');
                    setBadge('regime-halt-value', r.trading_halted ? 'JA' : 'Nein',
                        r.trading_halted ? 'badge-red' : 'badge-green');
                    setBadge('regime-filter-value', r.buy_allowed === false ? 'BLOCKIERT' : 'OK',
                        r.buy_allowed === false ? 'badge-red' : 'badge-green');
                }
            }
        }

        // v12 Feature Status + Universe Health (unabhaengig vom restlichen Load)
        loadV12Status();
        loadNewsSources();

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
                            // Farbkodierung entfernt: Kachel zeigt Scanner-Universum, kein Portfolio-Risiko
                            return `<span class="badge badge-blue">${name} ${pct.toFixed(0)}% (${data.count})</span>`;
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
// v37bb: in-memory cache mit Filter/Sort (statt rein server-paginated)
let _allLoadedTrades = [];
let _tradesSortField = 'timestamp';
let _tradesSortDesc = true;

async function loadTrades(reset = false) {
    if (reset) {
        tradesOffset = 0;
        _allLoadedTrades = [];
    }
    const res = await apiFetch(`/api/trades?limit=50&offset=${tradesOffset}`);
    if (!res) return;
    const data = await res.json();
    const newTrades = data.trades || [];
    _allLoadedTrades = _allLoadedTrades.concat(newTrades);
    tradesOffset += 50;
    renderTrades();
}

function renderTrades() {
    const tbody = document.getElementById('trades-table');
    tbody.innerHTML = '';

    // Filter anwenden
    const filterAction = document.getElementById('trades-filter-action')?.value || '';
    const filterSymbol = (document.getElementById('trades-filter-symbol')?.value || '').toUpperCase().trim();

    let filtered = _allLoadedTrades.filter(t => {
        const action = t.action || '';
        if (filterAction === 'BUY' && !(action === 'BUY' || action === 'SCANNER_BUY')) return false;
        if (filterAction === 'CLOSE' && !action.includes('CLOSE') && action !== 'MANUAL_SELL') return false;
        if (filterAction === 'STOP_LOSS_CLOSE' && action !== 'STOP_LOSS_CLOSE') return false;
        if (filterAction === 'TAKE_PROFIT_CLOSE' && action !== 'TAKE_PROFIT_CLOSE') return false;
        if (filterAction === 'EARNINGS_BLACKOUT_CLOSE' && action !== 'EARNINGS_BLACKOUT_CLOSE') return false;
        if (filterAction === 'MANUAL_SELL' && action !== 'MANUAL_SELL') return false;
        if (filterAction === 'FAILED' && !action.includes('FAILED')) return false;
        if (filterSymbol && !(t.symbol || '').toUpperCase().includes(filterSymbol)) return false;
        return true;
    });

    // Sort anwenden
    filtered.sort((a, b) => {
        const av = a[_tradesSortField] ?? 0;
        const bv = b[_tradesSortField] ?? 0;
        let cmp;
        if (typeof av === 'string') cmp = av.localeCompare(bv);
        else cmp = (av || 0) - (bv || 0);
        return _tradesSortDesc ? -cmp : cmp;
    });

    const countEl = document.getElementById('trades-filter-count');
    if (countEl) countEl.textContent = `${filtered.length} / ${_allLoadedTrades.length} Trades`;

    filtered.forEach(t => {
        const tr = document.createElement('tr');
        const actionClass = t.action === 'BUY' || t.action === 'SCANNER_BUY' ? 'badge-green' :
                            t.action.includes('STOP_LOSS') ? 'badge-red' :
                            t.action.includes('TAKE_PROFIT') ? 'badge-purple' :
                            t.action.includes('FAILED') ? 'badge-red' :
                            t.action === 'MANUAL_SELL' ? 'badge-orange' :
                            t.action === 'EARNINGS_BLACKOUT_CLOSE' ? 'badge-orange' :
                            'badge-blue';
        const ticker = t.symbol || ('#' + (t.instrument_id || '?'));
        const fullName = t.name && t.name !== t.symbol ? t.name : '';
        const assetCell = fullName
            ? `<div style="font-weight:600;">${ticker}</div>
               <div style="font-size:11px;color:var(--text-dim);">${fullName}</div>`
            : `<div style="font-weight:600;">${ticker}</div>`;
        tr.innerHTML = `
            <td>${fmtTime(t.timestamp)}</td>
            <td><span class="badge ${actionClass}">${t.action}</span></td>
            <td>${assetCell}</td>
            <td>${t.amount_usd ? fmtUsd(t.amount_usd) : (t.pnl_usd ? fmtUsd(t.pnl_usd) : '--')}</td>
            <td>${t.leverage || 1}x</td>
        `;
        tbody.appendChild(tr);
    });
}

function applyTradesFilter() {
    renderTrades();
}

function sortTrades(field) {
    if (_tradesSortField === field) {
        _tradesSortDesc = !_tradesSortDesc;
    } else {
        _tradesSortField = field;
        _tradesSortDesc = true;
    }
    renderTrades();
}

function loadMoreTrades() { loadTrades(false); }

// === BRAIN ===
async function loadBrain() {
    // Meta-Labeler-Status parallel zum Brain-Load ziehen
    loadMetaLabelerStatus();

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
        default_leverage: 2, max_single_trade_pct_of_portfolio: 0.15,
    },
    balanced_growth: {
        desc: 'Mittleres Risiko. Breite Streuung, moderater Leverage, langfristiges Wachstum.',
        stop_loss_pct: -8, take_profit_pct: 15, rebalance_threshold_pct: 5,
        default_leverage: 1, max_single_trade_pct_of_portfolio: 0.10,
    },
    conservative_etf: {
        desc: 'Niedriges Risiko. ETF-lastig, kein Leverage, seltenes Rebalancing.',
        stop_loss_pct: -15, take_profit_pct: 25, rebalance_threshold_pct: 10,
        default_leverage: 1, max_single_trade_pct_of_portfolio: 0.05,
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
    document.getElementById('cfg-max-trade-pct').value = preset.max_single_trade_pct_of_portfolio;
}

// === SETTINGS ===
async function loadSettings() {
    load2FAStatus(); // parallel
    loadDisabledSymbols(); // parallel
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
    document.getElementById('cfg-max-trade-pct').value = cfg.max_single_trade_pct_of_portfolio ?? 0.15;

    // Effektiv-Anzeige aus /api/risk (v15_sizing)
    try {
        const r = await apiFetch('/api/risk');
        if (r) {
            const j = await r.json();
            const s = j.v15_sizing;
            if (s) {
                const fmt = (v) => '$' + Number(v).toLocaleString('en-US', {maximumFractionDigits: 0});
                document.getElementById('cfg-max-trade-effective').textContent = fmt(s.max_single_trade_usd);
                document.getElementById('cfg-portfolio-value').textContent = fmt(s.portfolio_value_usd);
            }
        }
    } catch {}

    // Allocation editor
    const editor = document.getElementById('allocation-editor');
    const status = document.getElementById('allocation-status');
    const clearBtn = document.getElementById('btn-clear-targets');
    const targets = cfg.portfolio_targets || {};
    editor.innerHTML = '';
    const count = Object.keys(targets).length;
    if (count === 0) {
        if (status) status.innerHTML = '<span style="color:var(--ok,#22c55e);font-weight:600;">v15-Modus aktiv</span> — Bot steuert autonom via Scanner, Kelly-Sizing und Momentum. Keine fixen Targets gesetzt.';
        if (clearBtn) clearBtn.style.display = 'none';
    } else {
        if (status) status.innerHTML = `<span style="color:var(--warn,#f59e0b);font-weight:600;">Legacy-Targets aktiv</span> — ${count} feste Ziel-Gewichte. Der Bot versucht bei Kaltstart/Rebalance auf diese Gewichte zu steuern (nicht reiner v15-Modus).`;
        if (clearBtn) clearBtn.style.display = 'inline-block';
        Object.entries(targets).forEach(([sym, t]) => {
            editor.innerHTML += `
                <div style="display:flex; align-items:center; gap:8px; margin-bottom:8px">
                    <span style="width:60px; font-weight:600">${sym}</span>
                    <input type="number" id="alloc-${sym}" value="${t.allocation_pct}" step="1" min="0" max="100"
                        style="flex:1; padding:10px; background:var(--bg-input); border:1px solid var(--border); border-radius:8px; color:var(--text); font-size:16px" readonly>
                    <span style="color:var(--text-dim)">%</span>
                </div>
            `;
        });
    }
}

async function clearPortfolioTargets() {
    if (!confirm('Portfolio-Targets wirklich leeren?\n\nDer Bot steuert danach nur noch via Scanner/Kelly/Momentum (v15-Modus). Bestehende Positionen bleiben unangetastet, nur der Rebalance-/Kaltstart-Pfad wird deaktiviert.\n\nReversibel: Targets koennen jederzeit neu gesetzt werden.')) return;
    const res = await apiFetch('/api/config/strategy', {
        method: 'PUT',
        body: JSON.stringify({ portfolio_targets: {} }),
    });
    if (res && res.ok) {
        showToast('Portfolio-Targets geleert — v15-Modus aktiv');
        loadSettings();
    } else {
        const err = await res?.json();
        showToast('Fehler: ' + (err?.detail || 'Unbekannt'));
    }
}

async function saveSettings(e) {
    e.preventDefault();

    const update = {
        strategy: document.getElementById('cfg-strategy').value,
        stop_loss_pct: parseFloat(document.getElementById('cfg-sl').value),
        take_profit_pct: parseFloat(document.getElementById('cfg-tp').value),
        rebalance_threshold_pct: parseFloat(document.getElementById('cfg-rebalance').value),
        default_leverage: parseInt(document.getElementById('cfg-leverage').value),
        max_single_trade_pct_of_portfolio: parseFloat(document.getElementById('cfg-max-trade-pct').value),
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
    // v37ci: Confirm gegen Mobile-Tap
    if (!confirm('Weekly Report jetzt generieren + senden?\n\nLaeuft sonst automatisch jeden Freitag 20:00 CEST. Manueller Run schickt sofort eine neue Email/Notification.')) return;
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
    // v37ci: Confirm gegen Mobile-Tap
    if (!confirm('Asset Discovery jetzt manuell starten?\n\nLaeuft sonst automatisch jeden Freitag 19:00 CEST. Manueller Run dauert 2-10 Min auf GitHub Actions, blockiert evtl. Auto-Run-Slot.')) return;
    showToast('Asset Discovery gestartet (laeuft auf GitHub Actions, ~2-10 Min)');
    try {
        const res = await apiFetch('/api/discovery/run', { method: 'POST' });
        if (!res || !res.ok) {
            const err = await res?.json();
            showToast('Start fehlgeschlagen: ' + (err?.detail || 'Unbekannt'));
            return;
        }
        const startData = await res.json();
        if (startData.status === 'already_running') {
            showToast('Discovery laeuft bereits im Hintergrund');
        }
        // Status-Polling bis done oder error (max 15 Min = 90 * 10s)
        const MAX_POLLS = 90;
        for (let i = 0; i < MAX_POLLS; i++) {
            await new Promise(r => setTimeout(r, 10000));
            const sRes = await apiFetch('/api/discovery/status');
            if (!sRes || !sRes.ok) continue;
            const s = await sRes.json();
            if (s.state === 'done') {
                const r = s.result || {};
                showToast(`Discovery: ${r.new_found || 0} neue, ${r.added || 0} hinzugefuegt`);
                loadReports();
                break;
            } else if (s.state === 'error') {
                showToast('Discovery Fehler: ' + (s.error || 'Unbekannt'));
                console.error('Discovery error:', s);
                break;
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
    // v37ci: Confirm gegen versehentlichen Mobile-Tap
    if (!confirm('Backtest jetzt manuell starten?\n\nLaeuft sonst automatisch jeden Sonntag 08:00 CEST. Manueller Run dauert mehrere Minuten und blockiert ggf. naechsten Auto-Run-Slot.')) return;
    const btn = document.getElementById('btn-run-backtest');
    btn.disabled = true;
    btn.textContent = 'Backtest dispatching...';
    showToast('Backtest wird auf GitHub Actions gestartet...');

    try {
        const res = await apiFetch('/api/backtest/run', { method: 'POST' });
        if (!res || !res.ok) {
            const err = await res?.json();
            showToast('Start fehlgeschlagen: ' + (err?.detail || 'Unbekannt'));
            btn.disabled = false;
            btn.textContent = 'Run Backtest';
            return;
        }
        const startData = await res.json();
        if (startData.status === 'already_running') {
            showToast('Backtest laeuft bereits auf GitHub Actions');
        } else {
            showToast('Backtest laeuft auf GitHub Actions (~5-15 Min)');
        }

        // Status-Polling bis done oder error (max 20 min = 120 * 10s)
        const MAX_POLLS = 120;
        for (let i = 0; i < MAX_POLLS; i++) {
            await new Promise(r => setTimeout(r, 10000));
            const sRes = await apiFetch('/api/backtest/status');
            if (!sRes || !sRes.ok) continue;
            const s = await sRes.json();
            if (s.state === 'running') {
                const mode = s.mode || '';
                btn.textContent = mode.includes('dispatching')
                    ? 'Dispatching...'
                    : 'Backtest laeuft auf GH Actions...';
            } else if (s.state === 'done') {
                showToast('Backtest abgeschlossen: ' + (s.summary || ''));
                // Frische Ergebnisse laden
                try {
                    const bRes = await apiFetch('/api/backtest');
                    if (bRes && bRes.ok) {
                        const bData = await bRes.json();
                        if (bData && !bData.error) renderBacktestResults(bData);
                    }
                } catch {}
                break;
            } else if (s.state === 'error') {
                showToast('Backtest Fehler: ' + (s.error || 'Unbekannt'));
                console.error('Backtest error:', s);
                break;
            }
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
    btn.textContent = 'Training startet...';
    showToast('ML-Training wird gestartet...');

    try {
        const res = await apiFetch('/api/ml-model/train', { method: 'POST' });
        if (!res || !res.ok) {
            const err = await res?.json();
            showToast('Start fehlgeschlagen: ' + (err?.detail || 'Unbekannt'));
            btn.disabled = false;
            btn.textContent = 'ML-Modell trainieren';
            return;
        }
        const startData = await res.json();
        if (startData.status === 'already_running') {
            showToast('Training laeuft bereits im Hintergrund');
        }
        // Status-Polling bis done oder error (max 25 min = 150 * 10s)
        // GH Action dauert typ. 5-15 Min (v12 offload analog Backtest).
        const MAX_POLLS = 150;
        for (let i = 0; i < MAX_POLLS; i++) {
            await new Promise(r => setTimeout(r, 10000));
            const sRes = await apiFetch('/api/ml-model/train/status');
            if (!sRes || !sRes.ok) continue;
            const s = await sRes.json();
            if (s.state === 'running') {
                btn.textContent = s.phase === 'dispatching' ? 'GH Action startet...'
                                : s.phase === 'init' ? 'Runner initialisiert...'
                                : s.phase === 'download' ? 'Lade Historie...'
                                : s.phase === 'train' ? 'Trainiere Modell...'
                                : 'Training laeuft...';
            } else if (s.state === 'done') {
                showToast('ML-Modell trainiert!');
                if (s.model_info) renderMLModel(s.model_info);
                // Sicherheitshalber auch /api/ml-model neu laden
                try {
                    const mRes = await apiFetch('/api/ml-model');
                    if (mRes && mRes.ok) {
                        const mData = await mRes.json();
                        if (mData && !mData.error) renderMLModel(mData);
                    }
                } catch {}
                break;
            } else if (s.state === 'error') {
                showToast('ML Training Fehler: ' + (s.error || 'Unbekannt'));
                console.error('ML training error:', s);
                break;
            }
        }
    } catch (e) {
        showToast('ML Training Fehler: ' + e.message);
    } finally {
        btn.disabled = false;
        btn.textContent = 'ML-Modell trainieren';
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

// === KELLY SWEEP ===

async function loadKellySweep() {
    try {
        const res = await apiFetch('/api/kelly-sweep');
        if (!res || !res.ok) return;
        const data = await res.json();
        if (data.message) return; // Noch kein Sweep gelaufen
        renderKellySweep(data);
    } catch (e) {
        console.error('Kelly Sweep load error:', e);
    }
}

function renderKellySweep(data) {
    // Kelly Sweep Ergebnisse werden im Kelly-Sweep-Results-Card angezeigt
    const card = document.getElementById('kelly-sweep-card');
    if (!card) return;
    card.style.display = 'block';

    const results = data.results || data.sweep_results || [];
    if (!results.length) return;

    const tbody = document.getElementById('kelly-sweep-table');
    if (!tbody) return;
    tbody.innerHTML = '';

    for (const r of results) {
        const k = r.kelly_fraction || r.k || '?';
        const ret = r.total_return_pct != null ? r.total_return_pct.toFixed(1) + '%' : '--';
        const sharpe = r.sharpe_ratio != null ? r.sharpe_ratio.toFixed(2) : '--';
        const dd = r.max_drawdown_pct != null ? '-' + r.max_drawdown_pct.toFixed(1) + '%' : '--';
        const tr = document.createElement('tr');
        const isCurrent = parseFloat(k) === 0.04;
        tr.style.fontWeight = isCurrent ? 'bold' : 'normal';
        tr.style.background = isCurrent ? 'rgba(0,200,120,0.08)' : '';
        tr.innerHTML = `<td>${k}</td><td>${ret}</td><td>${sharpe}</td><td>${dd}</td>`;
        tbody.appendChild(tr);
    }

    if (data.timestamp) {
        const tsEl = document.getElementById('kelly-sweep-timestamp');
        if (tsEl) tsEl.textContent = 'Sweep: ' + fmtTime(data.timestamp);
    }
}

async function runKellySweep() {
    // v37ci: Confirm — Kelly-Sweep ist Hard-Gate-relevant
    if (!confirm('Kelly Sweep jetzt starten?\n\nDauert ca. 10-15 Min auf GitHub Actions. Berechnet die optimale Risiko-Stufe fuer Position-Sizing aus den letzten Trades. Empfohlen alle 4-8 Wochen.\n\nFortfahren?')) return;
    const btn = document.getElementById('btn-run-kelly-sweep');
    btn.disabled = true;
    btn.textContent = 'Sweep dispatching...';
    showToast('Kelly Sweep wird auf GitHub Actions gestartet...');

    try {
        const res = await apiFetch('/api/kelly-sweep/run', { method: 'POST' });
        if (!res || !res.ok) {
            const err = await res?.json();
            showToast('Start fehlgeschlagen: ' + (err?.detail || 'Unbekannt'));
            btn.disabled = false;
            btn.textContent = 'Kelly Sweep starten';
            return;
        }
        const startData = await res.json();
        if (startData.status === 'already_running') {
            showToast('Kelly Sweep laeuft bereits auf GitHub Actions');
        } else {
            showToast('Kelly Sweep laeuft auf GitHub Actions (~5-15 Min)');
        }

        // Status-Polling bis done oder error (max 20 min = 120 * 10s)
        const MAX_POLLS = 120;
        for (let i = 0; i < MAX_POLLS; i++) {
            await new Promise(r => setTimeout(r, 10000));
            const sRes = await apiFetch('/api/kelly-sweep/status');
            if (!sRes || !sRes.ok) continue;
            const s = await sRes.json();
            if (s.state === 'running') {
                const mode = s.mode || '';
                btn.textContent = mode.includes('dispatching')
                    ? 'Dispatching...'
                    : 'Kelly Sweep laeuft...';
            } else if (s.state === 'done') {
                showToast('Kelly Sweep abgeschlossen!');
                loadKellySweep();
                loadLastRunTimestamps();
                break;
            } else if (s.state === 'error') {
                showToast('Kelly Sweep Fehler: ' + (s.error || 'Unbekannt'));
                console.error('Kelly Sweep error:', s);
                break;
            }
        }
    } catch (e) {
        showToast('Kelly Sweep Fehler: ' + e.message);
    } finally {
        btn.disabled = false;
        btn.textContent = 'Kelly Sweep starten';
    }
}

// === LAST-RUN TIMESTAMPS ===

async function loadLastRunTimestamps() {
    // Lade Status-Endpunkte parallel und zeige "Letzter Lauf: DD.MM. HH:MM"
    const endpoints = [
        { id: 'btn-backtest-lastrun', url: '/api/backtest/status', label: 'Letzter Lauf' },
        { id: 'btn-ml-lastrun', url: '/api/ml-model/train/status', label: 'Letztes Training' },
        { id: 'btn-kelly-lastrun', url: '/api/kelly-sweep/status', label: 'Letzter Sweep' },
        { id: 'btn-discovery-lastrun', url: '/api/discovery/status', label: 'Letzte Suche' },
        { id: 'btn-optimizer-lastrun', url: '/api/optimizer/status', label: 'Letzter Lauf' },
    ];

    for (const ep of endpoints) {
        try {
            const el = document.getElementById(ep.id);
            if (!el) continue;
            const res = await apiFetch(ep.url);
            if (!res || !res.ok) { el.textContent = '--'; continue; }
            const s = await res.json();
            if (s.state === 'done' && s.finished_at) {
                el.textContent = ep.label + ': ' + fmtTime(s.finished_at);
                el.style.color = 'var(--green)';
            } else if (s.state === 'running') {
                el.textContent = ep.label + ': laeuft...';
                el.style.color = 'var(--orange)';
            } else if (s.state === 'error') {
                el.textContent = ep.label + ': Fehler';
                el.style.color = 'var(--red)';
            } else {
                el.textContent = ep.label + ': --';
            }
        } catch {
            // Silently skip
        }
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

// === v15 SIZING + CASH-DCA ===
async function loadV15Sizing() {
    try {
        const res = await apiFetch('/api/risk');
        if (!res) return;
        const data = await res.json();

        // Sizing-Card
        const s = data.v15_sizing || {};
        const fmtUsd = (v) => (v === null || v === undefined) ? '--' : '$' + Number(v).toLocaleString('en-US', {maximumFractionDigits: 0});
        const setTxt = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };

        setTxt('v15-max-pos', s.max_positions ?? '--');
        setTxt('v15-current-pos', (s.current_positions ?? '--') + (s.max_positions ? ' / ' + s.max_positions : ''));
        setTxt('v15-max-trade', fmtUsd(s.max_single_trade_usd));
        setTxt('v15-pct', s.pct_of_portfolio != null ? (Number(s.pct_of_portfolio) * 100).toFixed(1) + '%' : '--');
        const floor = s.floor_usd != null ? fmtUsd(s.floor_usd) : '--';
        const cap = s.hard_cap_usd != null ? fmtUsd(s.hard_cap_usd) : 'kein';
        setTxt('v15-floor-cap', floor + ' / ' + cap);
        setTxt('v15-tier', s.tier_threshold_usd != null ? '≤ ' + fmtUsd(s.tier_threshold_usd) : '--');

        // DCA-Card
        const d = data.v15_cash_dca || {};
        const badge = document.getElementById('v15-dca-badge');
        if (badge) {
            if (d.dca_active) {
                badge.textContent = 'AKTIV';
                badge.className = 'badge badge-orange';
            } else {
                badge.textContent = 'INAKTIV';
                badge.className = 'badge badge-green';
            }
        }
        setTxt('v15-dca-budget', fmtUsd(d.remaining_budget_usd));
        setTxt('v15-dca-cycles', d.remaining_cycles ?? '--');
        setTxt('v15-dca-per-cycle', d.per_cycle_usd != null ? fmtUsd(d.per_cycle_usd) : '--');
        setTxt('v15-dca-progress', d.progress_pct != null ? d.progress_pct + '%' : '--');

        const planEl = document.getElementById('v15-dca-plan');
        if (planEl) {
            if (d.dca_active && d.total_deposit_usd) {
                const consumed = d.consumed_usd || 0;
                planEl.textContent = 'Plan: ' + fmtUsd(consumed) + ' / ' + fmtUsd(d.total_deposit_usd) +
                    ' deployed' + (d.plan_created_at ? ' (seit ' + d.plan_created_at.slice(0, 10) + ')' : '');
            } else {
                planEl.textContent = 'Kein aktiver DCA-Plan. Naechster Trigger bei Einzahlung > 500 USD.';
            }
        }
    } catch (e) {
        console.warn('v15 sizing load failed:', e);
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
    loadV15Sizing();
    loadBrokerStatus();
    loadWithdrawalStatus();
    loadWfoStatus();
    loadSurvivorship();
    loadCostModelStatus();
    loadCutoverReadiness();
    loadEarningsWatchlist();

    // Auto-refresh
    setInterval(loadDashboard, 60000);
    setInterval(loadWatchdog, 300000); // Watchdog alle 5 Min
    setInterval(loadV15Sizing, 60000); // v15 Sizing/DCA alle 1 Min
    setInterval(loadBrokerStatus, 60000); // Broker-Badge alle 1 Min
    setInterval(loadWithdrawalStatus, 120000); // Entnahme-Plan alle 2 Min
    setInterval(loadWfoStatus, 120000); // WFO-Status alle 2 Min
    setInterval(loadSurvivorship, 300000); // Survivorship alle 5 Min (selten geaendert)
    setInterval(loadCostModelStatus, 600000); // Cost-Model alle 10 Min (statisch)
    setInterval(loadCutoverReadiness, 300000); // Cutover-Readiness alle 5 Min
    setInterval(loadEarningsWatchlist, 900000); // Earnings-Watchlist alle 15 Min (calendar-Daten aendern sich kaum)
    setInterval(() => {
        if (document.getElementById('tab-logs').classList.contains('active')) {
            loadLogs();
        }
    }, 30000);
})();

/**
 * Withdrawal Planner — Status laden + Form-Handling.
 */
// =====================================================================
// SURVIVORSHIP-BIAS-AUDIT (E4)
// =====================================================================
async function surveyRunNow() {
    const btn = document.getElementById('surv-run-btn');
    const msg = document.getElementById('surv-run-msg');
    if (!confirm('Survivorship-Audit JETZT starten?\n\n' +
                 '• ~50 yfinance-Calls fuer alle Universe-Symbole\n' +
                 '• Runtime ca. 30-60 Sek\n' +
                 '• Aktualisiert die Bias-Schaetzung in der Card')) return;
    btn.disabled = true;
    msg.textContent = 'Starte...';
    try {
        const r = await apiFetch('/api/survivorship/run', {method: 'POST'});
        const d = await r.json();
        if (d.ok) {
            msg.textContent = '✓ ' + d.message;
            setTimeout(loadSurvivorship, 60000);
            setTimeout(loadSurvivorship, 90000);
        } else {
            msg.textContent = '✗ ' + (d.error || 'Fehler');
        }
    } catch (e) {
        msg.textContent = '✗ ' + e.message;
    } finally {
        setTimeout(() => { btn.disabled = false; }, 2000);
    }
}


function _survNextSundayUtc() {
    const now = new Date();
    // Erster Sonntag in der Zukunft, 13:00 UTC
    const next = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(),
                                    now.getUTCDate(), 13, 0, 0));
    while (next.getUTCDay() !== 0 || next < now) {
        next.setUTCDate(next.getUTCDate() + 1);
    }
    return next.toISOString().slice(0, 10);
}


async function loadSurvivorshipHistory() {
    try {
        const r = await apiFetch('/api/survivorship/history');
        if (!r.ok) return;
        const d = await r.json();
        const block = document.getElementById('surv-history-block');
        const runs = d.runs || [];
        if (!block) return;
        if (runs.length < 1) {
            block.style.display = 'none';
            return;
        }
        block.style.display = 'block';
        document.getElementById('surv-hist-count').textContent = runs.length;
        const tbody = document.getElementById('surv-hist-tbody');
        tbody.innerHTML = '';
        runs.slice(-6).reverse().forEach(r => {
            const tr = document.createElement('tr');
            tr.style.borderBottom = '1px solid rgba(255,255,255,0.05)';
            const deadColor = (r.dead && r.dead > 0) ? '#f87171' : 'inherit';
            const biasColor = (r.sharpe_reduction_point == null) ? 'inherit'
                : (r.sharpe_reduction_point < 0.30 ? '#34d399'
                : (r.sharpe_reduction_point < 0.50 ? '#fbbf24' : '#f87171'));
            tr.innerHTML =
                '<td style="padding:4px 4px;">' + ((r.timestamp || '').slice(0, 16)) +
                ' <span style="opacity:0.6;font-size:0.85em;">(' + (r.trigger || '?') + ')</span></td>' +
                '<td style="text-align:right;padding:4px 4px;">' + (r.universe_size ?? '--') + '</td>' +
                '<td style="text-align:right;padding:4px 4px;color:#34d399;">' + (r.alive ?? '--') + '</td>' +
                '<td style="text-align:right;padding:4px 4px;color:' + deadColor + ';font-weight:' + (r.dead && r.dead > 0 ? '600' : 'normal') + ';">' + (r.dead ?? '--') + '</td>' +
                '<td style="text-align:right;padding:4px 4px;color:' + biasColor + ';">' +
                    (r.sharpe_reduction_point != null ? r.sharpe_reduction_point.toFixed(3) : '--') + '</td>' +
                '<td style="text-align:right;padding:4px 4px;font-weight:600;">' +
                    (r.wfo_corrected_point != null ? r.wfo_corrected_point.toFixed(2) : '--') + '</td>';
            tbody.appendChild(tr);
        });
    } catch (e) {
        console.warn('Survivorship history load failed:', e);
    }
}


async function loadSurvivorship() {
    try {
        const r = await apiFetch('/api/survivorship/summary');
        if (!r.ok) return;
        const d = await r.json();
        const summary = document.getElementById('surv-state-summary');
        const content = document.getElementById('surv-content');

        if (d.state === 'not_run_yet' || !d.generated_at) {
            summary.innerHTML = 'Noch nicht ausgefuehrt — klicke "Audit jetzt laufen lassen"';
            content.style.display = 'none';
            return;
        }

        summary.innerHTML = 'Letzter Audit: <strong>' +
            (d.generated_at || '').slice(0, 16) + '</strong>';
        content.style.display = 'block';

        document.getElementById('surv-alive').textContent = d.live_alive ?? '--';
        document.getElementById('surv-dead').textContent = d.live_dead ?? '--';
        document.getElementById('surv-suspicious').textContent = d.live_suspicious ?? '--';
        document.getElementById('surv-excluded').textContent = d.historical_excluded ?? '--';
        document.getElementById('surv-known').textContent =
            (d.historical_excluded || 0) + (d.historical_in_universe || 0);
        document.getElementById('surv-rate').textContent = d.exclusion_rate_pct ?? '--';
        document.getElementById('surv-corr-min').textContent =
            (d.estimated_sharpe_reduction_min ?? '--').toString();
        document.getElementById('surv-corr-max').textContent =
            (d.estimated_sharpe_reduction_max ?? '--').toString();
        document.getElementById('surv-corr-point').textContent =
            (d.estimated_sharpe_reduction_point ?? '--').toString();
        document.getElementById('surv-generated').textContent =
            (d.generated_at || '').slice(0, 16);
        const nextRunEl = document.getElementById('surv-next-run');
        if (nextRunEl) nextRunEl.textContent = _survNextSundayUtc();

        // History-Tabelle nachladen
        loadSurvivorshipHistory();

        if (d.wfo_correction) {
            document.getElementById('surv-wfo-block').style.display = 'block';
            document.getElementById('surv-wfo-raw').textContent =
                d.wfo_correction.wfo_mean_oos_sharpe;
            document.getElementById('surv-wfo-corrected').textContent =
                d.wfo_correction.corrected_point_estimate;
            document.getElementById('surv-wfo-min').textContent =
                d.wfo_correction.corrected_min;
            document.getElementById('surv-wfo-max').textContent =
                d.wfo_correction.corrected_max;
        }
    } catch (e) {
        console.warn('Survivorship load failed:', e);
    }
}


// =====================================================================
// WALK-FORWARD-OPTIMIZATION (E1)
// =====================================================================
async function wfoRunNow() {
    const btn = document.getElementById('wfo-run-btn');
    const msg = document.getElementById('wfo-run-msg');
    if (!confirm('WFO-Lauf JETZT starten?\n\n' +
                 '• 144 Backtests (24 Param-Kombos x 6 Windows)\n' +
                 '• Runtime ca. 10-15 Min im Hintergrund\n' +
                 '• Bot tradet weiter, kein Live-Risiko\n' +
                 '• Status-Card aktualisiert sich live')) return;
    btn.disabled = true;
    msg.textContent = 'Starte...';
    try {
        const r = await apiFetch('/api/wfo/run', {method: 'POST'});
        const d = await r.json();
        if (d.ok) {
            msg.textContent = '✓ ' + d.message;
            setTimeout(loadWfoStatus, 1000);
        } else {
            msg.textContent = '✗ ' + (d.error || 'Unbekannter Fehler');
            btn.disabled = false;
        }
    } catch (e) {
        msg.textContent = '✗ ' + e.message;
        btn.disabled = false;
    }
}


function _wfoNextAutoRun() {
    // Erster Sonntag des naechsten Monats 12:00 UTC
    const now = new Date();
    let y = now.getUTCFullYear(), m = now.getUTCMonth();
    const cand = (yy, mm) => {
        const d = new Date(Date.UTC(yy, mm, 1, 12, 0, 0));
        // erster Sonntag finden
        while (d.getUTCDay() !== 0) d.setUTCDate(d.getUTCDate() + 1);
        return d;
    };
    let next = cand(y, m);
    if (next < now) next = cand(y, m + 1);
    return next.toISOString().slice(0, 10);
}


async function loadWfoStatus() {
    try {
        const r = await apiFetch('/api/wfo/status');
        if (!r.ok) return;
        const d = await r.json();
        const summary = document.getElementById('wfo-state-summary');
        const idle = document.getElementById('wfo-idle-block');
        const running = document.getElementById('wfo-running-block');
        const done = document.getElementById('wfo-done-block');
        const errBlock = document.getElementById('wfo-error-block');
        const runBtn = document.getElementById('wfo-run-btn');
        idle.style.display = running.style.display = done.style.display = errBlock.style.display = 'none';
        // Run-Button nur deaktivieren waehrend running
        if (runBtn) runBtn.disabled = (d.state === 'running');

        if (d.state === 'idle' || !d.state) {
            summary.innerHTML = 'Status: <strong>idle</strong> &middot; bereit fuer ersten Run';
            idle.style.display = 'block';
            const cfg = d.config || {};
            document.getElementById('wfo-next-run').textContent = d.next_run_planned || '--';
            document.getElementById('wfo-approach').textContent = cfg.approach || '--';
            document.getElementById('wfo-param-count').textContent = cfg.param_combinations || '--';
            document.getElementById('wfo-window-count').textContent = cfg.windows_planned || '--';
        } else if (d.state === 'running') {
            const phase = d.phase || 'running';
            summary.innerHTML = 'Status: <strong style="color:#fbbf24;">RUNNING</strong> &middot; Phase: ' + phase;
            running.style.display = 'block';
            document.getElementById('wfo-current-window').textContent =
                (d.current_window != null ? d.current_window : '0');
            document.getElementById('wfo-total-windows').textContent =
                (d.windows_total || (d.windows || []).length || '?');
        } else if (d.state === 'done') {
            const agg = d.aggregate || {};
            const meanOos = agg.mean_oos_sharpe;
            const meanIs = agg.mean_is_sharpe;
            const decay = (meanIs && meanOos) ? (meanOos / meanIs * 100) : null;
            summary.innerHTML = 'Status: <strong style="color:#34d399;">DONE</strong> &middot; ' +
                'Mean OOS-Sharpe <strong>' + (meanOos != null ? meanOos.toFixed(2) : '--') + '</strong>' +
                (decay != null ? ' (Retention ' + decay.toFixed(0) + '% vs IS)' : '');
            done.style.display = 'block';
            const tbody = document.getElementById('wfo-windows-tbody');
            tbody.innerHTML = '';
            (d.windows || []).forEach(w => {
                const isS = w.is_score, oos = w.oos_score;
                const dec = (isS && oos) ? (oos / isS * 100).toFixed(0) + '%' : '--';
                const tr = document.createElement('tr');
                tr.style.borderBottom = '1px solid rgba(255,255,255,0.05)';
                tr.innerHTML = '<td style="padding:6px 4px;">W' + w.idx + ' (' +
                    (w.test_start || '').slice(0,7) + ')</td>' +
                    '<td style="text-align:right;padding:6px 4px;">' + (isS != null ? isS.toFixed(2) : '--') + '</td>' +
                    '<td style="text-align:right;padding:6px 4px;font-weight:600;">' + (oos != null ? oos.toFixed(2) : '--') + '</td>' +
                    '<td style="text-align:right;padding:6px 4px;">' + dec + '</td>' +
                    '<td style="text-align:right;padding:6px 4px;opacity:0.7;">' + (w.oos_trades || 0) + '</td>';
                tbody.appendChild(tr);
            });
            document.getElementById('wfo-aggregate-summary').innerHTML =
                'Letzter Run: <strong>' + (d.last_run || '').slice(0, 16) + '</strong>';
        } else if (d.state === 'error') {
            summary.innerHTML = 'Status: <strong style="color:#f87171;">ERROR</strong>';
            errBlock.style.display = 'block';
            document.getElementById('wfo-error-msg').textContent = d.error || 'Unbekannter Fehler';
        }

        // History laden + anzeigen wenn >= 2 Runs
        loadWfoHistory();
    } catch (e) {
        console.warn('WFO status load failed:', e);
    }
}


async function loadWfoHistory() {
    try {
        const r = await apiFetch('/api/wfo/history');
        if (!r.ok) return;
        const d = await r.json();
        const block = document.getElementById('wfo-history-block');
        const runs = d.runs || [];
        if (!block) return;
        if (runs.length < 1) {
            block.style.display = 'none';
            return;
        }
        block.style.display = 'block';
        document.getElementById('wfo-hist-count').textContent = runs.length;
        document.getElementById('wfo-hist-next').textContent = _wfoNextAutoRun();
        const tbody = document.getElementById('wfo-hist-tbody');
        tbody.innerHTML = '';
        // Letzte 6 Runs anzeigen, jüngste oben
        runs.slice(-6).reverse().forEach(r => {
            const tr = document.createElement('tr');
            tr.style.borderBottom = '1px solid rgba(255,255,255,0.05)';
            const sharpeColor = (r.mean_oos_sharpe == null) ? 'inherit'
                : (r.mean_oos_sharpe >= 2.5 ? '#34d399' : (r.mean_oos_sharpe >= 2.0 ? '#fbbf24' : '#f87171'));
            const decayColor = (r.sharpe_decay_pct == null) ? 'inherit'
                : (r.sharpe_decay_pct >= 70 ? '#34d399' : (r.sharpe_decay_pct >= 50 ? '#fbbf24' : '#f87171'));
            tr.innerHTML =
                '<td style="padding:4px 4px;">' + (r.ts || '--') +
                ' <span style="opacity:0.6;font-size:0.85em;">(' + (r.trigger || '?') + ')</span></td>' +
                '<td style="text-align:right;padding:4px 4px;color:' + sharpeColor + ';font-weight:600;">' +
                    (r.mean_oos_sharpe != null ? r.mean_oos_sharpe.toFixed(2) : '--') + '</td>' +
                '<td style="text-align:right;padding:4px 4px;color:' + decayColor + ';">' +
                    (r.sharpe_decay_pct != null ? r.sharpe_decay_pct.toFixed(0) + '%' : '--') + '</td>' +
                '<td style="text-align:right;padding:4px 4px;opacity:0.85;">' +
                    (r.oos_stability_std != null ? r.oos_stability_std.toFixed(2) : '--') + '</td>' +
                '<td style="text-align:right;padding:4px 4px;opacity:0.7;">' +
                    (r.mean_oos_trades != null ? Math.round(r.mean_oos_trades) : '--') + '</td>';
            tbody.appendChild(tr);
        });
    } catch (e) {
        console.warn('WFO history load failed:', e);
    }
}


async function loadWithdrawalStatus() {
    const statusEl = document.getElementById('wd-status');
    const detailsEl = document.getElementById('wd-details');
    const barEl = document.getElementById('wd-progress-bar');
    const fillEl = document.getElementById('wd-progress-fill');
    const noPlanEl = document.getElementById('wd-no-plan');
    const activeEl = document.getElementById('wd-active');
    if (!statusEl) return;
    try {
        const resp = await fetch('/api/withdrawal/status');
        const s = await resp.json();
        if (!s.active) {
            statusEl.textContent = 'Kein aktiver Plan';
            detailsEl.textContent = '';
            barEl.style.display = 'none';
            noPlanEl.style.display = 'block';
            activeEl.style.display = 'none';
        } else {
            statusEl.textContent = `$${s.withdrawn_so_far_usd.toLocaleString()} / $${s.target_amount_usd.toLocaleString()} (${s.progress_pct}%)`;
            barEl.style.display = 'block';
            fillEl.style.width = Math.min(100, s.progress_pct) + '%';
            const daysColor = s.days_left < 7 ? 'color:#f59e0b;' : '';
            detailsEl.innerHTML =
                `Deadline: <b>${s.deadline}</b> (<span style="${daysColor}">${s.days_left} Tage</span>) · ` +
                `Strategie: ${s.strategy} · ` +
                `Empf. Tagesrate: $${s.recommended_daily_liquidation_usd.toLocaleString()}<br>` +
                (s.notes ? `<i>${s.notes}</i>` : '');
            noPlanEl.style.display = 'none';
            activeEl.style.display = 'block';
        }
    } catch (e) {
        statusEl.textContent = `❌ Status-Fetch failed: ${e}`;
    }
}

async function withdrawalCreate() {
    const amount = parseFloat(document.getElementById('wd-amount').value);
    const deadline = document.getElementById('wd-deadline').value;
    const notes = document.getElementById('wd-notes').value;
    if (!amount || !deadline) {
        alert('Bitte Zielbetrag und Deadline ausfuellen');
        return;
    }
    // v37ci: Confirm-Dialog gegen versehentlichen Mobile-Tap
    if (!confirm(
        `Entnahme-Plan anlegen?\n\n` +
        `Zielbetrag: ${amount.toLocaleString()} USD\n` +
        `Deadline: ${deadline}\n` +
        `${notes ? 'Notiz: ' + notes + '\n' : ''}` +
        `\nDer Bot beginnt schrittweise Cash aufzubauen — ist umkehrbar via "Plan stornieren".`
    )) return;
    try {
        const resp = await fetch('/api/withdrawal/plan', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({amount, deadline, strategy: 'fifo', notes}),
        });
        const r = await resp.json();
        if (r.status === 'ok') {
            await loadWithdrawalStatus();
        } else {
            alert(`Fehler: ${r.error}`);
        }
    } catch (e) {
        alert(`Request failed: ${e}`);
    }
}

async function withdrawalCancel() {
    if (!confirm('Aktiven Plan wirklich stornieren?')) return;
    try {
        const resp = await fetch('/api/withdrawal/plan', {method: 'DELETE'});
        await resp.json();
        await loadWithdrawalStatus();
    } catch (e) {
        alert(`Cancel failed: ${e}`);
    }
}

/**
 * Universe-Reset: Leert disabled_symbols-Liste mit Bestaetigung.
 * Zeigt Resultat als Status-Text neben dem Button.
 */
async function universeReset() {
    const statusEl = document.getElementById('universe-reset-status');
    if (!confirm(
        'Universe-Reset durchfuehren?\n\n' +
        'Die disabled_symbols-Liste wird geleert. Beim naechsten Backtest ' +
        'werden alle Symbole neu bewertet. Schwache landen via Universe-Health ' +
        'wieder auf der Liste. Backup wird automatisch angelegt.'
    )) return;
    statusEl.textContent = 'läuft...';
    try {
        const resp = await fetch('/api/universe/reset', {method: 'POST'});
        const r = await resp.json();
        if (r.status === 'ok') {
            statusEl.style.color = '#10b981';
            statusEl.textContent = `✅ ${r.cleared_count} Symbole geleert · Backup: ${r.backup_key}`;
        } else if (r.status === 'noop') {
            statusEl.style.color = '#f59e0b';
            statusEl.textContent = `ℹ️ ${r.message}`;
        } else {
            statusEl.style.color = '#ef4444';
            statusEl.textContent = `❌ ${r.error || 'Unbekannter Fehler'}`;
        }
    } catch (e) {
        statusEl.style.color = '#ef4444';
        statusEl.textContent = `❌ Request failed: ${e}`;
    }
}

/**
 * Broker-Badge im Header — zeigt aktuellen Broker (IBKR), Modus
 * (Paper/Demo/Real) und Connection-Status (gruene LED = ok, gelb =
 * configured aber not connected, rot = error).
 */
async function loadBrokerStatus() {
    const el = document.getElementById('broker-badge');
    if (!el) return;
    try {
        const resp = await fetch('/api/broker-status');
        const s = await resp.json();
        const broker = (s.broker || '?').toUpperCase();
        const mode = (s.mode || '').toUpperCase();
        const account = s.account ? ` · ${s.account}` : '';
        let statusClass = 'broker-badge-ok';
        let title = `${broker} ${mode}${account}`;
        if (s.error) {
            statusClass = 'broker-badge-error';
            title += ` · ERROR: ${s.error}`;
        } else if (!s.connected) {
            statusClass = 'broker-badge-warn';
            title += ` · not connected`;
        }
        if (s.equity != null) {
            title += ` · Equity $${Math.round(s.equity).toLocaleString()}`;
        }
        // REAL-Modus = oranger Border (Warnsignal)
        const realClass = mode === 'REAL' ? ' broker-badge-real' : '';
        el.className = `broker-badge ${statusClass}${realClass}`;
        el.title = title;
        el.querySelector('.broker-badge-text').textContent = `${broker} · ${mode}`;
    } catch (e) {
        el.className = 'broker-badge broker-badge-error';
        el.title = `Fetch failed: ${e}`;
        el.querySelector('.broker-badge-text').textContent = 'API ?';
    }
}

// === 2FA / TOTP ===
async function load2FAStatus() {
    const res = await apiFetch('/api/auth/2fa/status');
    if (!res) return;
    const data = await res.json();
    const statusEl = document.getElementById('twofa-status');
    const setupSection = document.getElementById('twofa-setup-section');
    const wizard = document.getElementById('twofa-setup-wizard');
    const disableSection = document.getElementById('twofa-disable-section');
    const remEl = document.getElementById('twofa-recovery-remaining');
    if (!statusEl) return;

    wizard.style.display = 'none';
    if (data.enabled) {
        statusEl.textContent = `Aktiviert seit ${data.setup_at ? new Date(data.setup_at).toLocaleDateString('de-CH') : '?'}`;
        statusEl.style.color = 'var(--green)';
        setupSection.style.display = 'none';
        disableSection.style.display = 'block';
        if (remEl) remEl.textContent = data.recovery_codes_remaining;
    } else {
        statusEl.textContent = 'Aktuell nicht aktiviert. Empfohlen fuer mehr Sicherheit.';
        statusEl.style.color = 'var(--text-dim)';
        setupSection.style.display = 'block';
        disableSection.style.display = 'none';
    }
}

async function start2FASetup() {
    const res = await apiFetch('/api/auth/2fa/setup', { method: 'POST' });
    if (!res) return;
    if (!res.ok) {
        const err = await res.json();
        showToast(err.detail || 'Setup fehlgeschlagen');
        return;
    }
    const data = await res.json();
    document.getElementById('twofa-secret').textContent = data.secret;
    document.getElementById('twofa-qr-img').src = 'data:image/svg+xml;base64,' + data.qr_svg_b64;
    const codesEl = document.getElementById('twofa-recovery-codes');
    codesEl.innerHTML = data.recovery_codes.map(c => `<div>${c}</div>`).join('');

    document.getElementById('twofa-setup-section').style.display = 'none';
    document.getElementById('twofa-setup-wizard').style.display = 'block';
}

async function confirm2FASetup() {
    const code = document.getElementById('twofa-confirm-code').value.trim();
    if (code.length !== 6) {
        showToast('Bitte 6-stelligen Code eingeben');
        return;
    }
    const res = await apiFetch('/api/auth/2fa/setup/confirm', {
        method: 'POST',
        body: JSON.stringify({ code }),
    });
    if (!res) return;
    if (!res.ok) {
        const err = await res.json();
        showToast(err.detail || 'Code falsch — bitte den AKTUELLEN Code aus der App eingeben');
        return;
    }
    showToast('2FA aktiviert! Beim naechsten Login wird der Code abgefragt.');
    document.getElementById('twofa-confirm-code').value = '';
    load2FAStatus();
}

function cancel2FASetup() {
    document.getElementById('twofa-setup-wizard').style.display = 'none';
    document.getElementById('twofa-setup-section').style.display = 'block';
    document.getElementById('twofa-confirm-code').value = '';
}

async function disable2FA() {
    const code = document.getElementById('twofa-disable-code').value.trim();
    if (code.length !== 6) {
        showToast('Bitte aktuellen 6-stelligen TOTP-Code eingeben');
        return;
    }
    if (!confirm('2FA wirklich deaktivieren? Damit ist dein Account nur noch durch das Passwort geschuetzt.')) {
        return;
    }
    const res = await apiFetch('/api/auth/2fa/disable', {
        method: 'POST',
        body: JSON.stringify({ code }),
    });
    if (!res) return;
    if (!res.ok) {
        const err = await res.json();
        showToast(err.detail || 'Code falsch');
        return;
    }
    showToast('2FA deaktiviert');
    document.getElementById('twofa-disable-code').value = '';
    load2FAStatus();
}

// === v12 FEATURE STATUS ===
function _flagBadge(label, on) {
    return `<span class="badge ${on ? 'badge-green' : 'badge-blue'}" style="opacity:${on ? 1 : 0.5};">${label}${on ? '' : ' · off'}</span>`;
}

async function loadV12Status() {
    const res = await apiFetch('/api/v12-status');
    if (!res) return;
    let data;
    try { data = await res.json(); } catch (e) { return; }
    if (!data || data.error) return;

    // Badge-Reihe oben
    const badges = document.getElementById('v12-badges');
    if (badges) {
        badges.innerHTML = [
            _flagBadge('Kelly', data.kelly_sizing?.enabled),
            _flagBadge('Meta-Labeler', data.meta_labeler?.enabled),
            _flagBadge('Time-Stop', data.time_stop?.enabled),
            _flagBadge('VIX-TS', data.vix_term_structure?.enabled),
            _flagBadge('Hedging', data.hedging?.enabled),
            _flagBadge('Regime-Strat', data.regime_strategies?.enabled),
            _flagBadge('Trail-SL', data.trailing_sl?.enabled),
        ].join('');
    }

    const setTxt = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };

    // Universum
    const u = data.universe || {};
    setTxt('v12-universe-value',
        (u.active != null && u.total != null)
            ? `${u.active} / ${u.total}  (−${u.disabled_count})`
            : '--');

    // Kelly
    const k = data.kelly_sizing || {};
    if (k.enabled) {
        const kind = k.half_kelly ? 'Half-Kelly' : 'Full-Kelly';
        const cap = (k.max_fraction != null) ? (k.max_fraction * 100).toFixed(1) + '%' : '--';
        setTxt('v12-kelly-value', `${kind} · Cap ${cap}`);
    } else {
        setTxt('v12-kelly-value', 'aus');
    }

    // Meta-Labeler
    const m = data.meta_labeler || {};
    if (m.enabled) {
        const mode = m.shadow_mode ? 'Shadow' : 'LIVE';
        const prec = m.precision != null ? m.precision.toFixed(0) + '%' : '--';
        setTxt('v12-meta-value', `${mode} · ${m.shadow_log_size || 0}/${m.min_trades_to_activate || 50} · P=${prec}`);
    } else {
        setTxt('v12-meta-value', 'aus');
    }

    // Time-Stop
    const t = data.time_stop || {};
    if (t.enabled) {
        setTxt('v12-timestop-value', `${t.max_days_stale || '?'}d · ${t.exits_last_7d || 0} Exits/7d`);
    } else {
        setTxt('v12-timestop-value', 'aus');
    }

    // VIX Term Structure
    const vts = data.vix_term_structure || {};
    if (vts.enabled) {
        const mult = vts.panic_dip_multiplier != null ? (vts.panic_dip_multiplier * 100).toFixed(0) + '%' : '--';
        setTxt('v12-vts-value', `Panic-Dip · ${mult}`);
    } else {
        setTxt('v12-vts-value', 'aus');
    }

    // Hedging
    const h = data.hedging || {};
    if (h.enabled) {
        const bm = h.bear_position_multiplier != null ? (h.bear_position_multiplier * 100).toFixed(0) + '%' : '--';
        setTxt('v12-hedging-value', `Bear-Multi ${bm}`);
    } else {
        setTxt('v12-hedging-value', 'aus');
    }

    // Regime-Strategies
    const rs = data.regime_strategies || {};
    if (rs.enabled) {
        const bull = rs.bull_momentum_boost != null ? '+' + rs.bull_momentum_boost : '?';
        const bear = rs.bear_non_defensive_penalty != null ? rs.bear_non_defensive_penalty : '?';
        setTxt('v12-regime-value', `Bull ${bull} · Bear ${bear}`);
    } else {
        setTxt('v12-regime-value', 'aus');
    }

    // Trailing SL
    const tsl = data.trailing_sl || {};
    if (tsl.enabled) {
        setTxt('v12-tsl-value', `${tsl.trail_pct ?? '?'}% @ ${tsl.activation_pct ?? '?'}%`);
    } else {
        setTxt('v12-tsl-value', 'aus');
    }

    // Universe Health (aus dem gleichen Payload, ohne extra Call)
    renderUniverseHealth({
        timestamp: u.health_last_update,
        ok: u.health_ok,
        bad: u.health_bad || [],
        total: u.total,
    });
}

function renderUniverseHealth(h) {
    const summary = document.getElementById('uh-summary');
    const badList = document.getElementById('uh-bad-list');
    if (!summary || !badList) return;

    if (!h || h.timestamp == null) {
        summary.textContent = 'Noch kein Scan durchgefuehrt.';
        badList.innerHTML = '';
        return;
    }

    const when = fmtTime(h.timestamp);
    const okCount = h.ok != null ? h.ok : '?';
    const badCount = (h.bad || []).length;
    summary.innerHTML = `Letzter Check: <strong>${when}</strong> · OK: <strong>${okCount}</strong> · Probleme: <strong class="${badCount > 0 ? 'negative' : 'positive'}">${badCount}</strong>`;

    if (badCount === 0) {
        badList.innerHTML = '<span class="badge badge-green">Alle Symbole liefern Daten</span>';
    } else {
        badList.innerHTML = (h.bad || [])
            .map(s => `<span class="badge badge-red">${s}</span>`)
            .join('');
    }
}

// === META-LABELER STATUS (Brain Tab) ===
async function loadMetaLabelerStatus() {
    // Wir nutzen den gleichen v12-Endpoint
    const res = await apiFetch('/api/v12-status');
    if (!res) return;
    let data;
    try { data = await res.json(); } catch (e) { return; }
    if (!data || data.error) return;

    const m = data.meta_labeler || {};
    const badge = document.getElementById('meta-status-badge');
    if (badge) {
        if (!m.enabled) {
            badge.className = 'badge badge-blue';
            badge.textContent = 'DEAKTIVIERT';
        } else if (m.ready_to_activate) {
            badge.className = 'badge badge-green';
            badge.textContent = 'BEREIT FUER LIVE';
        } else if (m.shadow_mode) {
            badge.className = 'badge badge-orange';
            badge.textContent = m.trained ? 'SHADOW (trainiert)' : 'SHADOW (sammelt Daten)';
        } else {
            badge.className = 'badge badge-green';
            badge.textContent = 'LIVE';
        }
    }

    const setTxt = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };
    setTxt('meta-shadow-count', `${m.shadow_log_size || 0} / ${m.min_trades_to_activate || 50}`);
    setTxt('meta-precision',
        m.precision != null ? m.precision.toFixed(1) + '%' : '--');
    setTxt('meta-samples', m.samples_total != null ? m.samples_total : '--');

    const pct = m.progress_pct || 0;
    const bar = document.getElementById('meta-progress-bar');
    if (bar) {
        bar.style.width = pct + '%';
        bar.style.background = pct >= 100 ? 'var(--green)' : 'var(--orange)';
    }
    setTxt('meta-progress-label', `${pct}%`);

    if (m.trained_at) {
        setTxt('meta-trained-at', `Zuletzt trainiert: ${fmtTime(m.trained_at)}`);
    } else {
        setTxt('meta-trained-at', 'Noch nicht trainiert');
    }
}

// === DISABLED SYMBOLS EDITOR (Settings Tab) ===
async function loadDisabledSymbols() {
    const res = await apiFetch('/api/v12-status');
    if (!res) return;
    let data;
    try { data = await res.json(); } catch (e) { return; }
    if (!data || data.error) return;

    const u = data.universe || {};
    const ta = document.getElementById('ds-textarea');
    if (ta) ta.value = (u.disabled_symbols || []).join('\n');
    const setTxt = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };
    setTxt('ds-count', u.disabled_count != null ? u.disabled_count : '--');
    setTxt('ds-total', u.total != null ? u.total : '--');
    setTxt('ds-active', u.active != null ? u.active : '--');
}

async function saveDisabledSymbols() {
    const ta = document.getElementById('ds-textarea');
    if (!ta) return;
    const symbols = ta.value
        .split('\n')
        .map(s => s.trim().toUpperCase())
        .filter(s => s.length > 0);

    const res = await apiFetch('/api/disabled-symbols', {
        method: 'PUT',
        body: JSON.stringify({ disabled_symbols: symbols }),
    });
    if (!res || !res.ok) {
        const err = res ? await res.json().catch(() => ({})) : {};
        showToast('Fehler: ' + (err.detail || 'Speichern fehlgeschlagen'));
        return;
    }
    const data = await res.json();
    showToast(`Gespeichert: ${data.count} Symbols blockiert`);
    loadDisabledSymbols();
    loadV12Status();
}

// === NEWS SOURCES STATUS ===
async function loadNewsSources() {
    const res = await apiFetch('/api/news-sources');
    if (!res) return;
    let data;
    try { data = await res.json(); } catch (e) { return; }
    if (!data || data.error) return;

    const sources = data.sources || {};
    const primaryEl = document.getElementById('news-primary');
    const badgesEl = document.getElementById('news-badges');

    if (primaryEl) {
        primaryEl.innerHTML = `Aktiv: <strong>${data.primary_label || '--'}</strong>`;
    }

    if (badgesEl) {
        const items = [
            ['Finnhub', sources.finnhub],
            ['Claude Haiku', sources.anthropic_haiku],
            ['VADER', sources.vader],
            ['Yahoo Finance', sources.yfinance],
        ];
        badgesEl.innerHTML = items.map(([label, on]) =>
            `<span class="badge ${on ? 'badge-green' : 'badge-blue'}" style="opacity:${on ? 1 : 0.5};">${label}${on ? '' : ' · off'}</span>`
        ).join('');
    }
}

/**
 * E2 Cost-Model: Status-Card laden.
 * Zeigt Per-Asset-Klasse Default-Kosten + Calibrator-Status.
 */
async function loadCostModelStatus() {
    try {
        const r = await fetch('/api/cost_model/status');
        if (!r.ok) return;
        const data = await r.json();
        if (data.error) {
            const sEl = document.getElementById('cost-model-summary');
            if (sEl) sEl.textContent = 'Fehler: ' + data.error;
            return;
        }

        const summaryEl = document.getElementById('cost-model-summary');
        const defaultsBlock = document.getElementById('cost-model-defaults-block');
        const calibBlock = document.getElementById('cost-model-calibration-block');
        const tbody = document.getElementById('cost-model-defaults-tbody');
        if (!summaryEl || !tbody) return;

        const cal = data.calibration || {};
        const overridesCount = cal.overrides_active_count || 0;
        const fills = cal.total_fills_analyzed || 0;
        summaryEl.innerHTML = `<strong>${data.model_version || 'E2'}</strong> &middot; ` +
            (overridesCount > 0
                ? `<span style="color:#10b981;">${overridesCount}/6 Klassen empirisch kalibriert</span>`
                : `<span style="opacity:0.85;">Defaults aktiv</span> (Calibrator sammelt noch Daten)`);

        // Defaults-Tabelle
        const diagByClass = {};
        for (const d of cal.diagnostics_per_class || []) diagByClass[d.asset_class] = d;
        tbody.innerHTML = (data.defaults_per_class || []).map(row => {
            const diag = diagByClass[row.asset_class] || {};
            let badge = '<span style="opacity:0.5;">--</span>';
            if (diag.override_active) {
                badge = `<span style="color:#10b981;">✓ ${diag.sample_count} Fills</span>`;
            } else if (diag.sample_count > 0) {
                badge = `<span style="color:#fbbf24;">${diag.sample_count}/20</span>`;
            }
            return `
                <tr style="border-bottom:1px solid rgba(255,255,255,0.04);">
                    <td style="padding:5px 4px;text-transform:capitalize;">${row.asset_class}</td>
                    <td style="text-align:right;padding:5px 4px;">${row.spread_pct.toFixed(3)}%</td>
                    <td style="text-align:right;padding:5px 4px;">${row.slippage_buffer_pct.toFixed(3)}%</td>
                    <td style="text-align:right;padding:5px 4px;opacity:0.75;">${row.overnight_5d_pct.toFixed(3)}%</td>
                    <td style="text-align:right;padding:5px 4px;font-weight:600;">${row.total_round_trip_pct.toFixed(3)}%</td>
                    <td style="text-align:center;padding:5px 4px;font-size:11px;">${badge}</td>
                </tr>`;
        }).join('');
        defaultsBlock.style.display = '';

        // Calibrator-Status
        document.getElementById('cost-model-fills').textContent = fills;
        document.getElementById('cost-model-window').textContent = cal.age_window_days || 90;
        const lastRunEl = document.getElementById('cost-model-last-run');
        if (cal.generated_at) {
            try {
                lastRunEl.textContent = new Date(cal.generated_at).toLocaleString('de-CH', {
                    day: '2-digit', month: '2-digit', year: 'numeric',
                    hour: '2-digit', minute: '2-digit',
                });
            } catch (e) {
                lastRunEl.textContent = cal.generated_at;
            }
        } else {
            lastRunEl.textContent = 'noch nie';
        }
        const notesEl = document.getElementById('cost-model-notes');
        if (cal.notes && cal.notes.length > 0) {
            notesEl.innerHTML = cal.notes.map(n => `&middot; ${n}`).join('<br>');
        } else {
            notesEl.innerHTML = '';
        }
        calibBlock.style.display = '';
    } catch (e) {
        console.error('loadCostModelStatus failed:', e);
    }
}

/**
 * v37p: Cutover-Readiness Card — zentrale Health-Uebersicht.
 * Aggregiert Hard-Gates + Submodule-Status auf einen Blick.
 */
/**
 * v37z: Manueller Sell einer einzelnen Position via Position-Card-Button.
 * Mit Confirm-Dialog (Position kann irreversibel geschlossen werden).
 */
async function manualSell(symbol, pnlPct) {
    const pnlStr = pnlPct >= 0 ? `+${pnlPct.toFixed(2)}%` : `${pnlPct.toFixed(2)}%`;
    const confirmed = confirm(
        `Position ${symbol} sofort verkaufen?\n\n` +
        `Aktueller PnL: ${pnlStr}\n\n` +
        `Diese Aktion ist NICHT umkehrbar. Die Position wird zum aktuellen ` +
        `Marktpreis geschlossen (Slippage moeglich).`
    );
    if (!confirmed) return;

    showToast(`${symbol} wird verkauft...`);
    try {
        const res = await apiFetch(`/api/positions/${symbol}/sell`, { method: 'POST' });
        if (res && res.ok) {
            const data = await res.json();
            if (data.ok) {
                const pnlFinal = data.pnl_pct >= 0 ? `+${data.pnl_pct.toFixed(2)}%` : `${data.pnl_pct.toFixed(2)}%`;
                showToast(`${symbol} geschlossen — PnL ${pnlFinal}`);
                // Dashboard refresh in 2s damit Position aus Liste verschwindet
                setTimeout(loadDashboard, 2000);
            } else {
                showToast(`Fehler: ${data.error || 'unbekannt'}`);
            }
        } else if (res) {
            const errData = await res.json().catch(() => ({}));
            showToast(`Fehler ${res.status}: ${errData.detail || 'Server-Error'}`);
        }
    } catch (e) {
        showToast(`Fehler: ${e.message}`);
    }
}


/**
 * v37aa: Toggle Earnings-Exemption fuer ein Symbol.
 * Aus heute morgen-Erlebnis: User wollte ROKU bewusst halten, musste aber
 * SSH+CLI nutzen weil kein UI-Button existierte. Jetzt 1-Klick.
 */
async function toggleEarningsExempt(symbol, addExempt) {
    const action = addExempt ? 'EXEMPT setzen (Position halten)' : 'EXEMPT entfernen (Filter wieder aktiv)';
    const msg = addExempt
        ? `${symbol} auf Earnings-Exempt-Liste setzen?\n\n` +
          `Bot wird die Position bei naechstem Earnings NICHT automatisch ` +
          `schliessen. Nach dem Earnings wird die Exemption automatisch ` +
          `entfernt (one-shot).\n\nFortfahren?`
        : `${symbol} von Earnings-Exempt-Liste entfernen?\n\n` +
          `Bot wird die Position beim naechsten Earnings wieder via Filter ` +
          `pruefen und ggf. automatisch schliessen.\n\nFortfahren?`;
    if (!confirm(msg)) return;

    showToast(`${symbol}: ${action}...`);
    try {
        const method = addExempt ? 'POST' : 'DELETE';
        const res = await apiFetch(`/api/earnings/exempt/${symbol}`, { method });
        if (res && res.ok) {
            const data = await res.json();
            if (data.ok) {
                showToast(`${symbol} ${addExempt ? 'auf Exempt-Liste' : 'von Exempt-Liste entfernt'} (${data.exemptions_now.length} Symbole exempt)`);
                setTimeout(loadEarningsWatchlist, 500);
            } else {
                showToast(`Fehler: ${data.error || 'unbekannt'}`);
            }
        } else if (res) {
            const errData = await res.json().catch(() => ({}));
            showToast(`Fehler ${res.status}: ${errData.detail || 'Server-Error'}`);
        }
    } catch (e) {
        showToast(`Fehler: ${e.message}`);
    }
}


/**
 * v37z: Earnings-Watchlist - listet kommende Earnings naechste 7 Tage
 * fuer alle offenen Positionen, mit Filter-Trigger-Vorhersage.
 */
async function loadEarningsWatchlist() {
    try {
        const r = await fetch('/api/earnings/watchlist');
        if (!r.ok) return;
        const data = await r.json();
        if (data.error) return;

        const card = document.getElementById('earnings-watchlist-card');
        const tbody = document.getElementById('earnings-watchlist-tbody');
        const summary = document.getElementById('earnings-watchlist-summary');
        if (!card || !tbody) return;

        const wl = data.watchlist || [];
        if (wl.length === 0) {
            summary.innerHTML = '<span style="opacity:0.7;">Keine Earnings in den naechsten 7 Tagen.</span>';
            tbody.innerHTML = '';
            return;
        }

        const wouldExit = data.would_exit_count || 0;
        const exempt = data.exempt_count || 0;
        const filterActive = data.filter_active;

        summary.innerHTML =
            `<strong>${wl.length}</strong> Earnings in den naechsten 7 Tagen &middot; ` +
            (filterActive
                ? `<span style="color:#34d399;">Filter aktiv</span>`
                : `<span style="color:#fbbf24;">Filter aus</span>`) +
            ` &middot; ${wouldExit} wuerden geschlossen` +
            (exempt > 0 ? ` &middot; <span style="color:#fbbf24;">${exempt} exempt</span>` : '');

        tbody.innerHTML = wl.map(e => {
            let action;
            let toggleBtn;
            if (e.is_exempt) {
                action = `<span style="color:#fbbf24;" title="${e.exempt_reason || ''}">EXEMPT${e.exempt_auto_cleanup ? ' (one-shot)' : ''}</span>`;
                toggleBtn = `<button onclick="toggleEarningsExempt('${e.symbol}', false)"
                                     class="btn-secondary"
                                     style="font-size:10px;padding:2px 6px;cursor:pointer;"
                                     title="Exemption entfernen — Filter wird wieder aktiv">
                               Filter aktivieren
                            </button>`;
            } else if (e.would_exit) {
                action = `<span style="color:#f87171;" title="${e.reason || ''}">WUERDE SCHLIESSEN</span>`;
                toggleBtn = `<button onclick="toggleEarningsExempt('${e.symbol}', true)"
                                     class="btn-secondary"
                                     style="font-size:10px;padding:2px 6px;cursor:pointer;"
                                     title="Position halten — Filter ueberspringt dieses Symbol (one-shot)">
                               Halten
                            </button>`;
            } else {
                action = `<span style="opacity:0.7;">halten</span>`;
                toggleBtn = '';
            }
            const daysClass = e.days_until <= 1 ? 'color:#f87171;font-weight:600;' : '';
            return `
                <tr style="border-bottom:1px solid rgba(255,255,255,0.04);">
                    <td style="padding:5px 4px;font-weight:600;">${e.symbol}</td>
                    <td style="padding:5px 4px;${daysClass}">${e.earnings_date}</td>
                    <td style="text-align:right;padding:5px 4px;${daysClass}">${e.days_until}d</td>
                    <td style="text-align:right;padding:5px 4px;">${e.position_pct}%</td>
                    <td style="text-align:right;padding:5px 4px;">${e.vola_pct_30d ?? '?'}%</td>
                    <td style="text-align:center;padding:5px 4px;font-size:11px;">${action}</td>
                    <td style="text-align:center;padding:5px 4px;">${toggleBtn}</td>
                </tr>`;
        }).join('');
    } catch (e) {
        console.error('loadEarningsWatchlist failed:', e);
    }
}


async function loadCutoverReadiness() {
    try {
        const r = await fetch('/api/cutover/readiness');
        if (!r.ok) return;
        const d = await r.json();

        // Headline + Overall-Badge
        const badge = document.getElementById('cutover-overall-badge');
        const headline = document.getElementById('cutover-headline');
        if (!badge || !headline) return;

        const colors = {
            green:  { bg: 'rgba(16,185,129,0.18)',  fg: '#34d399', label: 'BEREIT'   },
            yellow: { bg: 'rgba(245,158,11,0.18)', fg: '#fbbf24', label: 'IN ARBEIT'},
            red:    { bg: 'rgba(239,68,68,0.18)',   fg: '#f87171', label: 'OFFEN'    },
        };
        const c = colors[d.overall_status] || colors.yellow;
        badge.style.background = c.bg;
        badge.style.color = c.fg;
        badge.textContent = c.label + ' · ' + (d.summary?.green || 0) + '/' + (d.summary?.total || 0);

        const days = d.days_to_cutover ?? '?';
        headline.innerHTML =
            `Cutover am <strong>${d.cutover_date}</strong> &middot; ` +
            `noch <strong>${days} Tage</strong> &middot; ` +
            `<span style="color:#34d399;">${d.summary?.green || 0} gruen</span>` +
            (d.summary?.yellow ? ` &middot; <span style="color:#fbbf24;">${d.summary.yellow} gelb</span>` : '') +
            (d.summary?.red    ? ` &middot; <span style="color:#f87171;">${d.summary.red} rot</span>` : '');

        // Hard-Gates Liste
        const list = document.getElementById('cutover-gates-list');
        list.innerHTML = (d.hard_gates || []).map(g => {
            const gc = colors[g.status] || colors.yellow;
            const dot = `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${gc.fg};margin-right:8px;flex-shrink:0;"></span>`;
            return `
                <div style="display:flex;align-items:flex-start;gap:6px;padding:6px 8px;background:rgba(255,255,255,0.02);border-radius:4px;">
                    ${dot}
                    <div style="flex:1;min-width:0;">
                        <div style="font-weight:600;">#${g.nr} ${g.title}</div>
                        <div style="font-size:11px;opacity:0.8;line-height:1.4;margin-top:2px;">${g.detail || ''}</div>
                    </div>
                </div>`;
        }).join('');

        // Sub-Modules
        const sub = d.submodules || {};
        const po = sub.pushover || {};
        document.getElementById('cutover-sub-pushover').innerHTML =
            (po.enabled && po.configured)
                ? '<span style="color:#34d399;">aktiv</span>'
                : '<span style="color:#fbbf24;">inaktiv</span>';

        const bk = sub.backups || {};
        document.getElementById('cutover-sub-backups').innerHTML =
            bk.configured ? `<span style="color:#34d399;">${bk.count} Archives</span>`
                          : '<span style="color:#fbbf24;">noch keine</span>';

        const ins = sub.insider_shadow || {};
        document.getElementById('cutover-sub-insider').innerHTML =
            ins.active
                ? `${ins.tracked} Candidates &middot; ${ins.would_block_pct}% wuerde-blocken`
                : '<span style="opacity:0.7;">sammelt Daten</span>';

        const cm = sub.cost_model || {};
        const lr = cm.last_run ? new Date(cm.last_run).toLocaleDateString('de-CH') : '--';
        document.getElementById('cutover-sub-cost-model').innerHTML =
            cm.fills_analyzed > 0
                ? `${cm.fills_analyzed} Fills &middot; ${cm.overrides_active}/6 Klassen kalibriert`
                : `<span style="opacity:0.7;">Defaults aktiv (last: ${lr})</span>`;
    } catch (e) {
        console.error('loadCutoverReadiness failed:', e);
    }
}

async function costModelCalibrate() {
    const btn = document.getElementById('cost-model-btn');
    const msg = document.getElementById('cost-model-msg');
    if (btn) btn.disabled = true;
    if (msg) msg.textContent = 'Calibrator laeuft...';
    try {
        const r = await fetch('/api/cost_model/calibrate', { method: 'POST' });
        const data = await r.json();
        if (data.ok) {
            msg.innerHTML = `<span style="color:#10b981;">✓ ${data.fills_analyzed} Fills analysiert, ${data.overrides_active} Override(s) aktiv</span>`;
            setTimeout(loadCostModelStatus, 500);
        } else {
            msg.innerHTML = `<span style="color:#f87171;">Fehler: ${data.error || 'unbekannt'}</span>`;
        }
    } catch (e) {
        msg.innerHTML = `<span style="color:#f87171;">Fehler: ${e.message}</span>`;
    } finally {
        if (btn) btn.disabled = false;
        setTimeout(() => { if (msg) msg.textContent = ''; }, 8000);
    }
}
