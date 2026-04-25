# InvestPilot — Autonomer Trading Bot

## Projekt-Uebersicht
Vollautonomer Trading Bot auf der eToro Public API. Selbstlernend, Docker-containerisiert, mit Web-Dashboard.
Inkl. Risk Management, Leverage Management, Asset-Filters, Market Context, Execution Tracking, Alerting.
Inkl. Backtesting Engine, ML Scoring (Gradient Boosting), Walk-Forward Validation.
Inkl. Self-Improvement Optimizer (woechentlich, Grid-Search, Auto-ML, Rollback).
Inkl. v5 Profitabilitaets-Upgrade: Regime Filter, Trailing SL, Dynamic Sizing, MTF Confluence, Sector Rotation, Recovery Mode.
Inkl. v6 Monitoring & Q&A: Watchdog Diagnostics (3-Ebenen Health Check, Telegram Alerts), Q&A Chat (Claude API).
Inkl. v7 Intelligence-Upgrade: Sentiment-Analyse, Portfolio Hedging, ML Trade-History Training, Google Drive Backup, Enhanced Telegram Alerts, Backtester mit realistischen Filtern.
Inkl. v8 Profit-Locking & Analytics: TP-Tranchen (Partial Close), Konzentrations-Penalty, Intraday Timing Filter, Adaptive Optimizer, Equity Curve / Performance Metrics API.
Inkl. v9 Brain-Recovery (truncated Gist Fix, Stale-Lock-Recovery) und v10 GitHub-Action Optimizer (Lern-Loop laeuft vollstaendig autonom in 7-GB CI-Runner statt 512-MB Render).

**Projekt-Pfad:** `C:\Users\CarlosBaumann\OneDrive - Mattka GmbH\Desktop\Claude\investpilot`
**eToro User:** carlosbaumann777 (wird Ende Mai 2026 durch IBKR ersetzt)
**IBKR Paper-Account:** DUP108015 (aktiviert 2026-04-24, fuer Migration)

**Deployment:** Hetzner VPS CPX22 (~€10.91/mo) — migriert von Render am 2026-04-23
**VPS IP:** 178.104.236.157
**VPS URL (geplant):** https://bot.cbaumann.ch — Cloudflare DNS (NS: christina+jerry.ns.cloudflare.com), Caddy Reverse Proxy mit Lets-Encrypt
**SSH:** `ssh -i ~/.ssh/hetzner_investpilot root@178.104.236.157`
**Compose:** `/opt/investpilot/docker-compose.vps.yml` (investpilot + caddy)
**IB Gateway:** `/opt/ib-gateway/docker-compose.yml` (gnzsnz/ib-gateway:stable, Port 4002 paper)

**Alter Render-Deploy (abgeschaltet):** ~~https://investpilot-2dp2.onrender.com~~

## Code-Audit-Regime (ab 2026-04-15)

Dreistufiges System um Bugs wie den Exit-Forecast-Config-Mismatch (v14) zu
verhindern. Ersetzt die Idee eines woechentlichen Kalender-Audits, weil der
zu Ignoranz fuehrt.

### Stufe 1 — Continuous (automatisch pro Feature)
**Bei JEDEM Feature/Endpoint/groesseren Commit abarbeiten, ohne dass der
User nachfragen muss:**

1. **Config-Read-Check:** Liest der neue Code aus der Config? Wenn ja —
   benutzt er dieselbe Sektion wie der Live-Bot? (Referenz: siehe
   Config-Sektion-Map unten.) Keine Defaults wie `-2.5` raten — aus dem
   Bot-Code (trader.py, optimizer.py) ableiten.
2. **Exception-Visibility-Check:** Neue `try/except` — loggt sie als
   `log.error()` (nicht `log.info`) UND ist der Fehler irgendwo sichtbar
   (Dashboard, Telegram, Alert)? Silent-Fail = ablehnen.
3. **File-Write-Check:** Neue Writes auf `/data/*.json`? Race-safe
   (atomic via `save_json()`)? Auch in `BACKUP_FILES` (persistence.py)?
4. **Timezone-Check:** Neue `datetime.now()` — UTC-aware? Scheduler
   nutzt `datetime.now(timezone.utc)`, nie naive datetimes mischen.

Diese Checks laufen implizit bei der Code-Generierung.

### Stufe 2 — Trigger-basiert (bei Ereignissen)
**Vollen Audit (Phase 1-3, siehe unten) ausloesen wenn:**
- Optimizer hat Parameter live gesetzt (z.B. SL, TP, Kelly)
- ≥3 Feature-Commits in einer Woche passiert
- Unerwartetes Bot-Verhalten (z.B. 0 Closed Trades ueber 7 Tage, PnL-Spikes)
- Render-Deploy failed, Service crashed, Health-Check RED
- eToro/yfinance API-Response-Format veraendert (Parse-Errors im Log)

### Stufe 3 — Kalender (monatlich, NICHT woechentlich)
**Alle ~4 Wochen:** Phase 1 + 2 des vollen Audits + Dependency-Check
(Python-Lib-Versionen, yfinance Breaking-Changes, eToro API-Doku diff).
Phase 3 nur bei Bedarf.

### Phasen des vollen Audits
- **Phase 1 — Config-Konsistenz:** Alle Stellen die `stop_loss_pct`,
  `take_profit_pct`, `kelly_*`, `min_scanner_score` etc. lesen — gleiche
  Sektion wie der Live-Bot?
- **Phase 2 — Silent-Fails:** `try/except` ohne Log, ohne Dashboard-Sichtbarkeit
- **Phase 3 — Race-Conditions:** Scheduler + API + GitHub-Action gleichzeitige
  File-Writes, Timezones, Stale-Locks

### Config-Sektion-Map (Single-Source-of-Truth)
| Parameter | Sektion in config.json | Wer liest |
|---|---|---|
| `stop_loss_pct`, `take_profit_pct` | `demo_trading` | trader.py, optimizer.py |
| `min_scanner_score`, `use_ml_scoring` | `demo_trading` | trader.py, brain.py |
| `portfolio_targets` | `demo_trading` | trader.py |
| `trailing_sl_pct`, `trailing_sl_activation_pct`, `tp_tranches` | `leverage` | trader.py, leverage_manager.py |
| `max_days_stale`, `stale_pnl_threshold_pct` | `time_stop` (top-level) | trader.py |
| `max_fraction` (Kelly) | `kelly` (top-level) | risk_manager.py |
| `enabled`, `schedule`, `max_changes_per_week` | `optimizer` | optimizer.py |
| `default_years` | `backtest` | backtester.py |

**Wichtig:** `stocks` ist KEINE Live-Sektion — Fehlerquelle fuer Exit-Forecast v14.
Wenn du etwas nicht findest: `grep "key_name" app/*.py` bevor du raetst.

## Architektur

```
investpilot/
├── app/                        # Backend-Module
│   ├── etoro_client.py         # eToro REST API Client (demo + real, Key-Variante A/B)
│   ├── trader.py               # Trading Engine v2 (5-Min-Zyklen, alle Safety-Checks integriert)
│   ├── brain.py                # Selbstlernendes AI-Modul (Walk-Forward, Scoring, Regime)
│   ├── market_scanner.py       # 70+ Assets Technical Analysis + Multi-Timeframe + ML Scoring
│   ├── backtester.py           # [NEU v3] Backtesting Engine: 5J Historie, Walk-Forward, Kostenmodell
│   ├── ml_scorer.py            # [v5] ML Scoring: Gradient Boosting, 18 Features (ATR, ADX, OBV, VWAP), JSON
│   ├── optimizer.py            # [v5] Self-Improvement: Grid-Search inkl. Trailing SL, Auto-ML, Rollback
│   ├── risk_manager.py         # Risikomanagement: Position Sizing, Drawdown, Margin, Korrelation
│   ├── leverage_manager.py     # Dynamischer Hebel, eToro-Limits, Trailing SL, TP-Staffelung
│   ├── alerts.py               # Telegram/Discord Notifications, Watchdog, Kill Switch
│   ├── watchdog.py             # [v6] Bot-Diagnostics: 5 Health-Checks, Telegram Alerts
│   ├── ask.py                  # [v6] Q&A Chat: Claude API, Bot-Context, natuerliche Antworten
│   ├── market_context.py       # VIX, Fear&Greed, Makro-Events, Earnings, Saisonalitaet
│   ├── asset_filters.py        # Asset-Klassen-Filter: Zeitfenster, Crypto, Forex, Rohstoffe
│   ├── execution.py            # Slippage-Tracking, Latenz, Performance-Breakdown, Sortino
│   ├── asset_discovery.py      # Woechentliche neue Asset-Suche (40+ Queries)
│   ├── scheduler.py            # Daemon Loop (5 Min Intervall, Watchdog, Market Context)
│   ├── sentiment.py            # [v7] Sentiment-Analyse: yfinance News keyword-basiert
│   ├── hedging.py              # [v7] Portfolio Hedging: Bear-Regime Schutz, Defensive Sektoren
│   ├── events_calendar.py      # [v5+v7] Earnings Blackout + Earnings Surprise Scoring
│   ├── gdrive_backup.py        # [v7] Google Drive Backup via Service Account
│   ├── persistence.py          # GitHub Gist Cloud Backup/Restore + GDrive Fallback
│   ├── weekly_report.py        # Freitag-Reports (JSON + HTML + PDF) inkl. Backtest-Sektion
│   ├── report_pdf.py           # PDF-Generierung via ReportLab
│   └── config_manager.py       # Config/Pfad-Management (Docker + lokal)
├── web/                        # FastAPI Dashboard
│   ├── app.py                  # 40 REST Endpoints inkl. Kill Switch, Risk, Backtest, ML, Ask, Diagnostics, Equity, Metrics
│   ├── auth.py                 # Login, bcrypt, Sessions
│   ├── security.py             # Rate Limiting, Audit Log, Failed Login Tracking
│   ├── data_access.py          # JSON Read/Write, Log Tailing
│   └── static/                 # Frontend (dark mode, mobile-first, Backtest Tab mit SVG Charts)
├── Dockerfile                  # Python 3.11-slim, Port 8000
├── docker-compose.yml          # Port 8443:8000, TZ=Europe/Zurich
├── render.yaml                 # Render Web Service Config
├── deploy_nas.sh               # Synology NAS Deployment
├── entrypoint.sh               # Scheduler + Uvicorn Start
├── config.json                 # Konfiguration inkl. Risk/Leverage/Filters
└── requirements.txt            # Dependencies (inkl. scikit-learn, numpy)
```

## Neue Module (v2)

### Risk Manager (`app/risk_manager.py`)
- **Dynamisches Position Sizing**: Max 2% Risiko pro Trade (konfigurierbar)
- **Drawdown-Stops**: Taeglich -5%, Woechentlich -10% (Auto-Pause)
- **Korrelationscheck**: Max Positionen pro Asset-Klasse, Max Allokation pro Klasse
- **Margin-Ueberwachung**: Min 20% Puffer, Auto-Deleverage bei Engpass
- **Exposure-Berechnung**: Invested x Leverage pro Klasse/Instrument
- **Emergency Kill Switch**: Schliesst alle Positionen sofort
- **Overnight/Weekend-Risiko**: Schliesst gehebelte Positionen vor Marktschluss
- **Weekend-Gebuehren-Check**: Schliesst Positionen wenn 3x-Overnight > Rendite
- **Transaktionskosten**: eToro-Spreads in alle Kalkulationen eingerechnet

### Leverage Manager (`app/leverage_manager.py`)
- **eToro Max-Hebel**: Forex 30x, Indices 20x, Commodities 10x, Stocks 5x, Crypto 2x
- **Dynamische Hebel-Selektion**: Basierend auf Volatilitaet, Signal, Regime, VIX
- **Trailing Stop-Loss**: Bewegt sich mit Kurs, aktiviert ab +1% Gewinn
- **Take-Profit Staffelung**: 50% bei TP1, 30% bei TP2, 20% laufen lassen
- **Risk/Reward Check**: Kein Trade unter 1:2 Ratio
- **Short-Support**: Validierung, Marktregime-Check, SL-Pflicht
- **Leverage Logging**: Effektive Exposure, Max-Loss pro Trade

### Alerts (`app/alerts.py`)
- **Telegram/Discord**: Trade-Notifications, Fehler, Drawdown-Warnungen
- **Granulare Telegram-Steuerung**: notify_trades, notify_stop_loss, notify_regime_change, notify_daily_summary, notify_weekly_report, notify_optimizer
- **Regime Halt Alerts**: Telegram-Benachrichtigung wenn Regime-Filter Trading stoppt/freigibt
- **Weekly Report Summary**: Telegram-Zusammenfassung nach Weekly Report Erstellung
- **Optimizer Alerts**: Telegram-Benachrichtigung bei Optimizer-Ergebnis (Aenderungen, Rollback, No Change)
- **Daily Summary**: Automatisch um 21:00
- **Watchdog**: Ueberwacht Bot-Aktivitaet, Alert bei Ausfall (Critical Alert via Telegram)
- **Telegram Commands**: /killswitch, /status, /start remote ausfuehrbar
- **Graceful Degradation**: Wenn TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID nicht gesetzt, werden Alerts still uebersprungen

### Market Context (`app/market_context.py`)
- **VIX-Monitoring**: Low/Normal/Elevated/High Fear, Position-Reduktion
- **Fear & Greed Index**: Kontra-Indikator (Extreme = Signal)
- **Makro-Kalender**: NFP, FOMC, CPI, EZB, SNB — Position-Reduktion
- **Earnings-Fenster**: 3 Tage vor/1 Tag nach Earnings kein Handel
- **BTC-Dominanz**: Altcoin-Filter bei hoher BTC-Dominanz
- **Saisonalitaet**: Gold Q4, Oil Sommer/Winter, NatGas Winter

### Asset Filters (`app/asset_filters.py`)
- **Handelszeiten je Klasse**: US 15:30-22:00, DAX 9:00-17:30, Crypto 24/7
- **Opening/Closing Buffer**: 30 Min nach Open, 15 Min vor Close kein Handel
- **Forex Sessions**: Tokyo/London/New York — optimale Session je Paar
- **Crypto-Filter**: Stablecoins/NFTs ausschliessen, Volatilitaetsfilter, Weekend-Reduktion
- **Rohstoff-Rollover**: Warnung bei Quartalswechsel
- **Index Overnight**: Gehebelte Index-Positionen vor Schluss schliessen

### Execution Tracking (`app/execution.py`)
- **Slippage-Tracking**: Erwarteter vs tatsaechlicher Preis
- **Latenz-Monitoring**: Signal-zu-Order Zeit in ms
- **Performance-Breakdown**: Nach Uhrzeit, Wochentag, Asset-Klasse, Symbol
- **Sortino Ratio**: Berechnung (nur Downside-Volatilitaet)
- **Execution Stats API**: 7-Tage Statistiken, P95 Latenz

## Neue Module (v3)

### Backtesting Engine (`app/backtester.py`)
- **Historische Daten**: 5 Jahre OHLCV via yfinance, 18 repraesentative Assets
- **Scanner-Replikation**: Exakte Nachbildung der `score_asset()` Logik auf historischen Bars
- **Trade-Simulation**: Positionsmanagement mit SL/TP, Score-Threshold
- **Transaktionskosten**: Spread 0.15%, Overnight 0.01%/Nacht, Slippage 0.05%
- **Walk-Forward Validation**: 80/20 Split, In-Sample + Out-of-Sample Metriken
- **Metriken**: Total Return, Annual Return, Sharpe Ratio, Max Drawdown, Win Rate, Profit Factor
- **Output**: `backtest_results.json` mit Equity Curve, Monthly Returns, Best/Worst Trades

### ML Scoring (`app/ml_scorer.py`)
- **Modell**: GradientBoostingClassifier (100 Trees, Depth 4, LR 0.1, Subsample 0.8)
- **18 Features**: RSI, MACD (3), Bollinger Position, Momentum (5d/20d), Volatilitaet, Volume Trend, SMA-Vergleiche, Golden Cross, RSI Slope, Price vs SMA20%, ATR%, ADX, OBV Slope, VWAP Deviation%
- **Label**: Binaer — Preis steigt >1% in naechsten 5 Tagen
- **Walk-Forward Training**: 80/20 Split, Accuracy/Precision/Recall/F1
- **JSON-Serialisierung**: Kein Pickle — Feature Importances + Thresholds als JSON (Docker-sicher)
- **Integration**: `market_scanner.score_asset(use_ml=True)` — ML-Score 0-100 → -100/+100 Mapping
- **Safety Default**: `use_ml_scoring: false` — manuell aktivieren nach Backtest-Validierung

### Self-Improvement Optimizer (`app/optimizer.py`)
- **Woechentlicher Auto-Lauf**: Sonntag 02:00 via Scheduler
- **Parameter Grid-Search**: min_score, SL, TP, Trailing SL Kombinationen per Walk-Forward getestet
- **Volatilitaets-basierte SL/TP**: Pro Asset-Klasse berechnet (Crypto -8%, Aktien -4%, Forex -2%)
- **Kosten-Filter**: Trades muessen min 1.5x Kosten erwarten (`min_expected_return_pct`)
- **ML Auto-Vergleich**: ML vs Fixed Weights, automatische Aktivierung wenn OOS Sharpe +0.3 besser
- **Safety Guards**: Rollback bei -5% Wochen-Drawdown, Max 1 grosse Aenderung/Woche
- **History**: Alle Laeufe in `optimization_history.json` (letzte 52 Wochen)
- **Dashboard**: Optimizer-Sektion im Backtest-Tab, manueller Trigger + Rollback Button

## Neue Features (v5 — Profitabilitaets-Upgrade)

### Regime Filter (`market_scanner.py`, `trader.py`)
- **VIX-basiertes Scoring**: Score-Penalties bei elevated/high_fear VIX
- **Marktregime-Penalty**: Bear -10, Sideways -3 auf Scanner-Score
- **Regime Halt**: Kompletter Kauf-Stopp wenn VIX > 35 (konfigurierbar)
- Config: `regime_filter.enabled`, `vix_halt_threshold`, `bear_score_penalty` etc.

### Trailing Stop-Loss Wiring (`trader.py`, `backtester.py`)
- **Live-Trading**: `leverage_manager.check_trailing_stop_losses()` in SL/TP Loop verdrahtet
- **Backtester**: Trailing SL Simulation (Aktivierung + Trail), neuer Exit Reason `TRAILING_SL`
- **Optimizer**: Trailing SL Parameter (pct, activation_pct) im Grid-Search

### Extended ML Features (`ml_scorer.py`, `market_scanner.py`)
- **18 statt 14 Features**: +ATR% (Average True Range), +ADX (Trendstaerke), +OBV Slope, +VWAP Deviation%
- `analyze_single_asset()` liefert alle 18 Features im Return-Dict
- `train_model()` uebergibt High/Low-Daten fuer ATR/ADX Berechnung

### Dynamic Position Sizing (`risk_manager.py`, `trader.py`)
- `calculate_dynamic_position_size()`: Score-basierte Skalierung (50%-150% der Basisgroesse)
- Reference Score 30 = 100%, Score 45 = 150%, Score 15 = 50%
- Config: `risk_management.dynamic_sizing_enabled`, `dynamic_sizing_reference_score`

### Multi-Timeframe Confluence (`market_scanner.py`)
- **`calculate_confluence_score()`**: 1H (50%) + 15M (30%) + 5M (20%) = -100 bis +100
- Confirming TFs: +20% Score-Boost, Conflicting TFs: -30% Penalty
- `enrich_with_mtf()` jetzt in `scan_all_assets()` verdrahtet (war vorher nie aufgerufen!)
- Config: `multi_timeframe.enabled`, `top_n`, `min_confluence_score`

### Sector Rotation (`market_scanner.py`)
- **Sektor-Feld** in ASSET_UNIVERSE: tech, finance, health, consumer, growth
- `calculate_sector_strength()`: Durchschnittsscore pro Sektor
- `apply_sector_rotation()`: Starker Sektor +15%, schwacher Sektor -15%

### Drawdown Recovery Mode (`risk_manager.py`, `trader.py`)
- **Aktiviert** bei Weekly Drawdown zwischen -3% und Kill-Switch (-10%)
- **Einschraenkungen**: Positionsgroessen halbiert, Min Score 30, kein Leverage
- Config: `recovery_mode_threshold_pct`, `recovery_mode_min_score`, `recovery_mode_max_leverage`

### Expanded Backtest Assets (`backtester.py`)
- `download_history()` nutzt jetzt volles ASSET_UNIVERSE (64+ Assets) statt 18
- Batch-Download (10 pro Batch, 2s Pause) fuer Rate Limiting

### Dashboard v5 (`web/app.py`, `index.html`, `app.js`)
- Regime Status Card: VIX Level, Halt, Recovery Mode Badges
- API: `/api/regime`, `/api/trailing-sl`, `/api/sectors`

## Neue Features (v6 — Monitoring & Q&A)

### Watchdog Diagnostics (`app/watchdog.py`)
- **5 Health-Checks**: Zyklen-Aktivitaet, Trade-Erfolgsrate, Error-Patterns, Margin-Gesundheit, Drawdown
- **Zyklen-Check**: Alert wenn letzter Zyklus >30 Min her (Bot haengt/abgestuerzt)
- **Trade-Erfolg**: Erkennt wenn >50% der CLOSE-Calls fehlschlagen (haette close_position-Bug gefangen)
- **Error-Pattern**: Erkennt wiederholte Fehlermuster in Logs (>3x gleicher Fehler)
- **Margin-Check**: Warnung bei <20%, Kritisch bei <10%
- **Drawdown-Check**: Ueberwacht Tages/Wochen-Drawdown vs Limits
- **Telegram-Alert**: Automatisch bei Problemen via `/api/diagnostics/alert` (kein Auth, fuer cron-job.org)
- **Dashboard-Widget**: Watchdog-Card im Dashboard-Tab mit Status-Badge + Details
- Config: `watchdog.enabled`, `check_interval_minutes`, `max_cycle_silence_minutes`, `close_error_threshold_pct`

### Q&A Chat (`app/ask.py`)
- **Claude API Integration**: Fragen zum Bot in natuerlicher Sprache beantworten
- **Context-Sammlung**: Portfolio (live), Trade-History, Decision-Log, Brain-State, Scanner, Risk-State
- **Modell**: Claude Haiku (schnell, guenstig: ~$0.01/Anfrage)
- **Dashboard-Tab**: "Ask" Tab mit Chat-Interface (Frage + Antwort-History)
- **Beispielfragen**: "Warum wurde NVDA verkauft?", "Welcher Trade hat am meisten verloren?", "Warum kauft der Bot nichts?"
- Config: `ask.enabled`, `ask.model`, `ask.max_tokens`
- Env: `ANTHROPIC_API_KEY` (Pflicht)

## Kern-Module (v1, aktualisiert)

### eToro Client (`app/etoro_client.py`)
- REST API Client fuer Demo + Real
- Auto-Detection Key-Variante A vs B
- Methoden: `get_portfolio()`, `buy()`, `sell()`, `close_position()`, `search_instrument()`
- **BUGFIX**: close_position() hat jetzt Environment-Prefix (war 403)

### Trading Engine (`app/trader.py`) — v2
- `run_trading_cycle()` integriert alle neuen Module (Graceful Degradation)
- **Vor jedem Trade**: Drawdown-Check, Position-Sizing, Korrelation, Margin, Asset-Filter, R/R
- **Market Context**: VIX/Sentiment/Makro reduzieren Positionsgroessen
- **Earnings-Filter**: Kein Aktienhandel im Earnings-Fenster
- **Overnight-Check**: Gehebelte Positionen abends pruefen
- **Telegram Kill Switch**: Remote Emergency Stop
- **Execution Tracking**: Slippage + Latenz fuer jeden Trade

### Trade Brain (`app/brain.py`) — v2
- 7-Schritt-Zyklus (war 5): + Walk-Forward + Parameter-Analyse
- **Walk-Forward Validation**: Regelaenderungen werden auf Out-of-Sample Daten getestet
- **Max-Cap pro Instrument**: 25% (konfigurierbar, verhindert Uebergewichtung)
- **Decision Context Logging**: Jeder Trade-Entscheid mit Marktkontext gespeichert
- **Parameter-Performance**: Analyse welche Kombinationen in welchen Regimes funktionieren
- **Sortino Ratio**: Im Report enthalten

### Market Scanner (`app/market_scanner.py`) — v5
- 70+ Assets mit Sektor-Tags: tech, finance, health, consumer, growth
- **Multi-Timeframe**: 1H Trend + 15M Entry + 5M SL — Confluence Score (-100 bis +100)
- **MTF Confluence**: Confirming +20% Boost, Conflicting -30% Penalty
- **Sector Rotation**: Starke Sektoren +15%, schwache -15%
- **Regime Filter**: VIX/Marktregime Score-Penalties
- **v5 Indikatoren**: ATR%, ADX, OBV Slope, VWAP Deviation in analyze_single_asset()

## Dashboard Endpoints

### Ohne Auth
- `GET /health` — Health Check

### Mit Auth (JWT)
- `POST /api/auth/login` — JWT Login
- `GET /api/portfolio` — Live eToro Portfolio
- `GET /api/trades` — Trade-Historie (paginiert)
- `GET /api/brain` — Brain State, Scores, Regeln
- `GET /api/config` — Strategie-Parameter
- `PUT /api/config/strategy` — Strategie aendern
- `GET/POST /api/trading/status|start|stop` — Trading Steuerung
- **`POST /api/trading/killswitch`** — [NEU] Emergency Kill Switch
- **`GET /api/risk`** — [NEU] Risiko-Zusammenfassung
- **`GET /api/exposure`** — [NEU] Effektive Exposure je Klasse
- **`GET /api/market-context`** — [NEU] VIX, Fear&Greed, Events
- **`GET /api/execution-stats`** — [NEU] Slippage, Latenz Stats
- **`GET /api/performance-breakdown`** — [NEU] Breakdown nach Zeit/Asset
- **`GET /api/backtest`** — [NEU v3] Letzte Backtest-Ergebnisse (pollt Gist-Watchdog)
- **`POST /api/backtest/run`** — [v12] Dispatcht GitHub Action (backtest.yml). Laeuft auf 7-GB-Runner statt Render 512 MB (vermeidet OOM->502)
- **`GET /api/backtest/status`** — [NEU v12] Status des GH-Action-Backtest-Laufs (pollt Gist-Watchdog)
- **`GET /api/ml-model`** — [NEU v3] ML-Modell Info (Feature Importances, Accuracy)
- **`POST /api/ml-model/train`** — [NEU v3] ML-Modell trainieren
- **`GET /api/optimizer`** — [NEU v4] Optimizer Status und History
- **`POST /api/optimizer/run`** — [NEU v4] Optimization manuell starten
- **`POST /api/optimizer/rollback`** — [NEU v4] Letzte Optimierung rueckgaengig machen
- **`GET /api/regime`** — [NEU v5] VIX-Level, Marktregime, Recovery Mode, Trading Halt
- **`GET /api/trailing-sl`** — [NEU v5] Aktive Trailing Stop-Loss Levels
- **`GET /api/sectors`** — [NEU v5] Sektor-Staerke Daten
- **`GET /api/diagnostics`** — [NEU v6] Bot-Gesundheitspruefung (5 Checks, Auth)
- **`GET /api/diagnostics/alert`** — [NEU v6] Watchdog mit Telegram-Alert (kein Auth, fuer cron-job.org)
- **`POST /api/ask`** — [NEU v6] Q&A Chat via Claude API (Auth)
- **`GET /api/equity-curve`** — [NEU v8] Taegliche Equity-Curve + Drawdown
- **`GET /api/performance-metrics`** — [NEU v8] Sharpe, Sortino, Win Rate, Profit Factor
- **`GET /api/position-correlations`** — [NEU v8] Sektor-Verteilung + Konzentrations-Score
- `GET /api/logs` — Scheduler Logs
- `GET/POST /api/weekly-report` — Weekly Report
- `GET /api/weekly-report/pdf|pdfs` — PDF Reports
- `GET/POST /api/discovery` — Asset Discovery

## Konfiguration

### config.json Sektionen
- **etoro**: API Keys, Username, Environment
- **demo_trading**: Strategie, Portfolio-Targets, SL/TP, Leverage
- **risk_management**: [NEU] Drawdown-Limits, Position-Sizing, Margin, Exposure
- **leverage**: [NEU] Hebel-Defaults, Trailing SL, TP-Staffelung, Short-Regeln
- **asset_filters**: [NEU] Zeitfenster, Buffer, Crypto/Forex-Filter
- **market_context**: [NEU] VIX-Thresholds, Fear&Greed, Earnings-Filter
- **backtest**: [NEU v3] Default Years (5), Default Symbols
- **demo_trading.use_ml_scoring**: [NEU v3] ML Scoring aktivieren (default: false)
- **optimizer**: [NEU v4] Schedule, Rollback-Threshold, Max Changes/Woche
- **asset_class_params**: [NEU v4] SL/TP pro Asset-Klasse (Stocks, Crypto, Forex etc.)
- **min_expected_return_pct**: [NEU v4] Kosten-Filter Schwelle
- **regime_filter**: [NEU v5] VIX Halt Threshold, Bear/Sideways/Fear Penalties
- **multi_timeframe**: [NEU v5] Enabled, Top N, Min Confluence Score
- **risk_management.dynamic_sizing_enabled**: [NEU v5] Score-basierte Positionsgroesse
- **risk_management.recovery_mode_***: [NEU v5] Recovery Mode Thresholds
- **alerts**: Telegram/Discord Config, Email
- **watchdog**: [NEU v6] Enabled, Check-Intervall, Silence-Threshold, Error-Rate-Threshold
- **ask**: [NEU v6] Enabled, Modell, Max Tokens
- **strategies**: Core/Growth/Dividend/Tactical Targets

### Umgebungsvariablen
- `ETORO_PUBLIC_KEY`, `ETORO_PRIVATE_KEY`, `ETORO_DEMO_PRIVATE_KEY`, `ETORO_ENVIRONMENT`
- `JWT_SECRET`, `ADMIN_USER`, `ADMIN_PASSWORD_HASH`
- `GITHUB_TOKEN` (Cloud Backup)
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (Alerts)
- `DISCORD_WEBHOOK_URL` (Alerts)
- `ANTHROPIC_API_KEY` [NEU v6] (Q&A Chat)
- `GDRIVE_SERVICE_ACCOUNT_JSON` [NEU v7] (Google Drive Backup — JSON-Key)
- `GDRIVE_FOLDER_ID` [NEU v7] (Google Drive Backup — Ziel-Ordner)

## Deployment
- **Docker:** `docker-compose up -d` (Port 8443)
- **Render:** Push to Git, auto-deploy via `render.yaml`
- **NAS:** `./deploy_nas.sh` (SSH zu Synology)

## Daten-Dateien
- `brain_state.json` — Brain-Learnings (Snapshots, Scores, Regeln)
- `trade_history.json` — Alle ausgefuehrten Trades
- `risk_state.json` — [NEU] Drawdown-Tracking, Tages/Wochen-P/L
- `execution_log.json` — [NEU] Slippage + Latenz pro Trade
- `market_context.json` — [NEU] VIX, Fear&Greed, Events Cache
- `trailing_sl_state.json` — [NEU] Trailing Stop-Loss Levels
- `decision_log.json` — [NEU] Trade-Entscheid-Kontext
- `alert_state.json` — [NEU] Watchdog Heartbeat, Alert-Counter
- `backtest_results.json` — [NEU v3] Backtest-Ergebnisse, Equity Curve, Monthly Returns
- `ml_model.json` — [NEU v3] Trainiertes ML-Modell (Feature Importances, Thresholds)
- `optimization_history.json` — [NEU v4] Optimierungs-Laeufe, Parameter-Aenderungen, Rollbacks
- `scanner_state.json` — Scanner-Cache
- `discovery_result.json` — Letzte Asset-Discovery
- `weekly_report.json` — Letzter Weekly Report
- `audit.db` — SQLite Security Database

## Neue Features (v7 — Intelligence-Upgrade)

### Sentiment-Analyse (`app/sentiment.py`)
- **yfinance News**: Keyword-basierte Sentiment-Bewertung (positiv/negativ/neutral)
- **Score-Range**: -1.0 bis +1.0, konfigurierbar Threshold (-0.5 default)
- **4h Cache**: Vermeidet ueberfluessige API-Calls
- **Integration**: Sentiment < Threshold → Trade wird uebersprungen
- Config: `market_context.use_sentiment_filter`, `sentiment_block_threshold`

### Portfolio Hedging (`app/hedging.py`)
- **Bear-Regime Schutz**: Positionsgroessen automatisch reduziert (default x0.5)
- **Defensive Sektoren**: health, consumer, bonds, commodities bevorzugt
- **Integration**: Multiplikator auf ctx_multiplier in trader.py
- Config: `hedging.enabled`, `bear_position_multiplier`, `defensive_sectors`

### ML Trade-History Training (`app/ml_scorer.py`)
- **Eigene Trades als Trainingsdaten**: RandomForest auf 11 Features (Scanner Score, RSI, MACD, Sektor, VIX, F&G)
- **predict_score()**: Gibt Wahrscheinlichkeit 0-1 zurueck, multipliziert Scanner-Score
- **Auto-Training**: Im Optimizer ab 50+ abgeschlossenen Trades
- **Dual-Model Support**: Erkennt automatisch Price-History (18 dim) vs Trade-History (11 dim) Modell
- Config: `demo_trading.use_ml_scoring: true` aktiviert ML-Scoring

### Google Drive Backup (`app/gdrive_backup.py`)
- **Service Account**: Kein OAuth noetig, nur JSON-Key + Folder-ID
- **Inkrementell**: SHA256-Hash Vergleich, nur geaenderte Dateien hochladen
- **Restore**: Automatisch beim Start als Fallback nach GitHub Gist
- **Dateien**: brain_state, trade_history, config, risk_state, ml_model etc.
- Env: `GDRIVE_SERVICE_ACCOUNT_JSON`, `GDRIVE_FOLDER_ID`

### Enhanced Telegram Alerts (`app/alerts.py`)
- **Granulare Steuerung**: notify_trades, notify_stop_loss, notify_regime_change, notify_daily_summary, notify_weekly_report, notify_optimizer
- **Regime Halt/Resume**: Automatische Benachrichtigung bei Regime-Wechsel
- **Stop-Loss Details**: Unterscheidet STOP_LOSS vs TRAILING_SL mit P/L-Details
- **Weekly Report Summary**: Kompakte Zusammenfassung nach Report-Erstellung
- **Optimizer Results**: Aenderungen, Rollback, No Change Benachrichtigung
- Env: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`

### Backtester mit realistischen Filtern (`app/backtester.py`)
- **VIX Regime Filter**: Historische VIX-Daten → Score-Penalties + Halt in Backtest
- **Earnings Blackout**: Historische Earnings-Termine → kein Trading im Blackout-Fenster
- **Sektor Konzentration**: Max Positionen/Sektor auch im Backtest simuliert
- **Verbesserter Trailing SL**: Intraday Highs fuer realistischere Simulation
- **Walk-Forward**: Alle Filter durchgereicht an simulate_trades()

### Events Calendar Enhanced (`app/events_calendar.py`)
- **Earnings Surprise Scoring**: `adjust_score_for_earnings()` — Score-Boost/Penalty basierend auf letztem Earnings-Ergebnis
- **Historische Earnings-Daten**: Fuer Backtester verfuegbar

### Optimizer Enhanced (`app/optimizer.py`)
- **Realistische Filter**: VIX + Earnings Blackout Daten automatisch heruntergeladen
- **ML Trade-History**: Auto-Training ab 50 Trades im Optimizer-Lauf
- **Telegram Integration**: Ergebnis-Benachrichtigung nach jedem Lauf

## Neue Features (v8 — Profit-Locking & Analytics)

### Profit-Locking / Partial Close (`app/trader.py`, `app/backtester.py`)
- **TP-Tranchen in SL/TP Loop**: Prueft ob offene Positionen eine Gewinn-Tranche erreicht haben
- **Gestaffelter Ausstieg**: 50% bei +3%, 30% bei +6%, 20% bei +10% (konfigurierbar via `leverage.tp_tranches`)
- **State Tracking**: `partial_close_state.json` speichert welche Tranchen pro Position ausgeloest wurden
- **Backtester**: `simulate_trades()` simuliert Partial Closes mit Exit Reason `PARTIAL_CLOSE`
- **Cleanup**: State wird automatisch bereinigt wenn Positionen geschlossen werden

### Enhanced Correlation / Konzentrations-Penalty (`app/risk_manager.py`, `app/trader.py`)
- **`get_portfolio_concentration_score()`**: Herfindahl-Index-basierter Score (0-100)
- **Automatische Positionsreduktion**: Bei Score > Threshold (default 70) werden neue Positionen um 30% reduziert
- Config: `risk_management.concentration_penalty_enabled`, `concentration_threshold`, `concentration_size_reduction`

### Intraday Timing Filter (`app/trader.py`)
- **Volatilitaets-Schutz**: Keine Kaeufe in ersten 30 Min nach Open (15:30-16:00 CET)
- **Liquiditaets-Schutz**: Keine Kaeufe in letzten 30 Min vor Close (21:30-22:00 CET)
- Config: `intraday_timing.enabled`, `avoid_first_minutes`, `avoid_last_minutes`

### Adaptive Optimizer (`app/optimizer.py`)
- **Bi-weekly statt weekly**: Optimierung laeuft alle 14 Tage statt jeden Sonntag
- **Konfigurierbares Intervall**: `optimizer.optimization_interval_days` (default: 14)
- **Prueft `optimization_history.json`**: Nur Lauf wenn >13 Tage seit letzter Optimierung

### Dashboard Performance Endpoints (`web/app.py`)
- **`GET /api/equity-curve`**: Taegliche Equity-Curve mit Drawdown-Prozent
- **`GET /api/performance-metrics`**: Sharpe, Sortino, Max Drawdown, Win Rate, Profit Factor, Avg Win/Loss
- **`GET /api/position-correlations`**: Sektor-Verteilung, Konzentrations-Score fuer offene Positionen

### Neue Daten-Dateien
- `partial_close_state.json` — Tracking welche TP-Tranchen pro Position ausgeloest wurden

### Neue Config-Keys
- `risk_management.concentration_penalty_enabled` — Konzentrations-Penalty aktivieren (default: true)
- `risk_management.concentration_threshold` — Score ab dem Penalty greift (default: 70)
- `risk_management.concentration_size_reduction` — Reduktionsfaktor (default: 0.7 = 30% kleiner)
- `intraday_timing.enabled` — Timing-Filter aktivieren (default: true)
- `intraday_timing.avoid_first_minutes` — Minuten nach Open ohne Kaeufe (default: 30)
- `intraday_timing.avoid_last_minutes` — Minuten vor Close ohne Kaeufe (default: 30)
- `optimizer.optimization_interval_days` — Tage zwischen Optimierungslaeufen (default: 14)

## v9 — Brain-Recovery & Subprocess-Optimizer (2026-04-07)

### Persistence Hardening (`app/persistence.py`)
- **`_fetch_gist_file_content(file_entry, token)`**: Helper, der bei `truncated=true` (GitHub liefert nur ~743KB content im API-Response) automatisch ueber `raw_url` mit Authorization-Header den vollen Inhalt nachlaedt. Loest stillschweigenden Datenverlust bei >700KB Brain-Files.
- **Intelligenter Restore in `restore_from_cloud()`**: Statt `is_empty`-Check vergleicht jetzt `gist.total_runs` vs `local.total_runs` — restore wenn Gist mehr Runs hat. Schuetzt vor OOM-Reset, wo der Scheduler 1 Dummy-Cycle schreibt bevor Restore laeuft. Trade-history: Restore wenn Gist mehr Trades hat.
- **`optimizer_status.json`**: Neu in BACKUP_FILES, damit Subprocess-Status persistent ist.

### Subprocess-Isolation Optimizer (`app/optimizer_runner.py` NEU + `web/app.py`)
- **Neuer Standalone-Runner**: `python -m app.optimizer_runner [triggered_by]` startet den Optimizer als komplett separaten Python-Prozess.
- **`/api/optimizer/run`**: Spawnt `subprocess.Popen([sys.executable, "-m", "app.optimizer_runner", username], start_new_session=True)`. Wenn der Optimizer das 512 MB Render-Limit sprengt, wird NUR der Subprocess vom OOM-Killer getroffen — der Web/Scheduler-Container ueberlebt.
- **Status-Tracking**: Subprocess schreibt waehrend des Laufs in `optimizer_status.json` (state, pid, started_at, finished_at, action, error). Felder `mode: "subprocess"` + `pid` sind neu.
- **`scheduler.py`**: Sonntags-Auto-Optimizer per Default DEAKTIVIERT, gegated hinter `ENABLE_SUNDAY_AUTO_OPTIMIZER=1` Env-Var. Damit kein Sonntag-Brain-Reset mehr.

### Neue Admin-Endpoints (`web/app.py`)
Alle erfordern Auth (`require_auth`).
- **`POST /api/admin/force-backup`**: Erzwingt sofortiges `backup_to_cloud()` ohne abzuwarten. Nuetzlich nach manuellem Restore.
- **`GET /api/admin/gist-inspect`**: Liefert Metadaten des aktuellen Gist-HEADs ohne Schreibzugriff. Zeigt `size`, `truncated`, `raw_url_present`, `content_len_in_api`, `fetched_content_len`, plus geparste Brain-Werte (`total_runs`, `market_regime`, `win_rate`, `sharpe_estimate`, `instrument_scores`-Anzahl, `learned_rules`, `performance_snapshots`). Plus Vergleich mit lokalem Brain-State.
- **`GET /api/admin/gist-history`**: Iteriert die letzten 30 Gist-Revisionen und zeigt `total_runs` pro Revision. Hilft eine alte gute Revision (vor Reset) zu finden. Liefert Full-SHA und Short-SHA.
- **`POST /api/admin/force-restore-brain-from-sha?sha=<full_40>&confirm=YES_OVERWRITE&files=brain_state.json`**: Notfall-Restore aus einer SPEZIFISCHEN Gist-Revision (per SHA). Unterstuetzt mehrere Files via Komma-Trennung. Pflicht: Full-40-char SHA.
- **`POST /api/admin/force-restore-brain?confirm=YES_OVERWRITE`**: Force-Restore aus aktuellem Gist-HEAD ohne `is_empty`-Check.

### Neue Files
- `app/optimizer_runner.py` — Standalone Subprocess Entry-Point
- `optimizer_status.json` — Persistent Subprocess-Status (mit `mode`, `pid`, `started_at`, `finished_at`, `action`, `error`)

### Neue Env-Vars
- `ENABLE_SUNDAY_AUTO_OPTIMIZER=1` — Re-aktiviert den Sonntag-Auto-Optimizer (Default: aus)

### Recovery-Workflow nach Brain-Reset
1. `GET /api/admin/gist-history` — finde Revision mit hohem `total_runs`
2. `POST /api/admin/force-restore-brain-from-sha?sha=<sha>&confirm=YES_OVERWRITE` — restore
3. `POST /api/admin/force-backup` — push restored state als neuer Gist-HEAD
4. Scheduler nimmt automatisch wieder Trading auf, brain.total_runs zaehlt von restored Wert weiter

## v10 — GitHub-Action Optimizer (2026-04-07)

### Hintergrund
Die v9 Subprocess-Isolation hat sich auf Render Free Tier (512 MB) als unzureichend erwiesen: Der OOM-Killer arbeitet auf cgroup-Ebene und reisst trotz `start_new_session=True` den ganzen Container mit. v10 verlegt den Optimizer-Lauf in eine **GitHub Action** (7 GB RAM, kostenlos), waehrend der Trading-Server unberuehrt weiterlaeuft.

### Architektur
```
GitHub Actions (Sonntag 03:00 UTC)         Render (24/7)
+----------------------------------+      +-------------------------+
| 1. Checkout repo                 |      | trader.py: liest        |
| 2. restore_for_optimizer()       |      | config.json,            |
|    - holt brain_state.json,      |<---->| optimized_params,       |
|      trade_history.json, ...     | Gist | brain_state.json        |
| 3. run_weekly_optimization()     |      |                         |
|    - volles Grid-Search          |      | persistence.py          |
|    - skip_inline_backup=1        |      | restore_from_cloud():   |
| 4. backup_optimizer_results()    |      |  - skipt NO_RESTORE_FILES|
|    - PUSH NUR config.json,       |      |                         |
|      optimization_history,       |      | trader liest neue       |
|      ml_model, optimizer_status  |      | Params beim naechsten   |
|    - KEIN Push von brain/trades  |      | Cycle (alle 5 Min)      |
+----------------------------------+      +-------------------------+
```

### Persistence (`app/persistence.py`)
- **`NO_RESTORE_FILES`**: Set mit Dateien, die zwar gesichert werden duerfen, aber NIEMALS aus der Cloud restored. Aktuell: `optimizer_status.json`. Verhindert dass nach OOM-Restart eine alte "running" PID den Optimizer-Slot blockiert (v9-Bug).
- **`OPTIMIZER_OUTPUT_FILES`**: Liste der Dateien, die der Optimizer modifiziert (`config.json`, `optimization_history.json`, `optimizer_status.json`, `ml_model.json`, `backtest_results.json`).
- **`restore_for_optimizer()`**: CI-Variante von `restore_from_cloud()`. Holt ALLE Backup-Files (ausser NO_RESTORE_FILES) ohne `should_restore`-Heuristik — der CI-Runner hat per Definition kein lokales State zu schuetzen.
- **`backup_optimizer_results()`**: Push NUR der `OPTIMIZER_OUTPUT_FILES`. Vermeidet Race-Condition mit Trading-Server-Updates: ohne diese Trennung wuerden wir `brain_state.json` / `trade_history.json` auf den Stand zu Optimizer-Start zurueckdrehen.

### Optimizer Runner (`app/optimizer_runner.py`)
- **CI-Mode-Erkennung**: Aktiv wenn `INVESTPILOT_OPTIMIZER_CI=1` ODER `triggered_by` mit `github-action` beginnt.
- **CI-Pipeline**:
  1. `restore_for_optimizer()` — holt Brain-State + Trade-Historie aus Gist
  2. Setzt `INVESTPILOT_SKIP_INLINE_BACKUP=1` damit der Optimizer NICHT inline `backup_to_cloud()` ruft
  3. `run_weekly_optimization()` — voller Grid-Search ohne Memory-Safeguard-Risiko
  4. `backup_optimizer_results()` — isolierter Push der Output-Files
- **Legacy-Subprocess-Mode**: Bleibt erhalten fuer lokale Tests, wird auf Render aber nicht mehr verwendet.

### Optimizer (`app/optimizer.py`)
- **`INVESTPILOT_SKIP_INLINE_BACKUP=1`**: Konditional in `run_weekly_optimization()`. Im CI-Mode wird der inline `backup_to_cloud()` uebersprungen, weil der Runner stattdessen `backup_optimizer_results()` aufruft.

### Web-Endpoint Update (`web/app.py`)
- **`POST /api/optimizer/run`**: Triggert jetzt nicht mehr `subprocess.Popen`, sondern den GitHub Workflow per REST API (`POST /repos/{owner}/{repo}/actions/workflows/optimizer.yml/dispatches`). Stale-Lock-Recovery (60 Min) bleibt aktiv.
- **`_trigger_github_action_optimizer(username)`** ersetzt `_run_optimizer_background()`.
- Status-File wird mit `mode: "github-action-running"` markiert. Der Workflow ueberschreibt es spaeter via Gist-Push.

### GitHub Action (`.github/workflows/optimizer.yml`)
- **Trigger**: `cron: '0 3 * * 0'` (Sonntag 03:00 UTC) + `workflow_dispatch` (manuell + REST API).
- **Concurrency-Group**: `investpilot-optimizer` — verhindert parallele Laeufe.
- **Timeout**: 45 Min (volles Grid + ML + Walk-Forward).
- **Step 1**: Checkout, Python 3.11, `pip install -r requirements.txt`.
- **Step 2**: `python -m app.optimizer_runner github-action-${triggered_by}` mit `INVESTPILOT_OPTIMIZER_CI=1`.
- **Step 3**: Upload `optimizer_status.json` + `optimization_history.json` + `data/logs/` als Artifact (14 Tage Retention) — auch bei Failure.

### Neue Secrets (GitHub Repo)
**Einmalig anzulegen** unter Settings → Secrets → Actions:
- **`INVESTPILOT_GIST_TOKEN`** (Pflicht): PAT mit Scopes `gist` + `actions:write`. Wird im Workflow als `GITHUB_TOKEN` env exportiert.
- **`TELEGRAM_BOT_TOKEN`** (optional): Wenn gesetzt, sendet der Optimizer bei Abschluss einen Telegram-Alert.
- **`TELEGRAM_CHAT_ID`** (optional): Ziel-Chat fuer Telegram-Alerts.

### Neue Env-Vars (Render)
- **`GITHUB_REPO`** (optional, default `carlosbaumann754-svg/investpilot`): Repo fuer Workflow-Dispatch.
- **`OPTIMIZER_WORKFLOW_FILE`** (optional, default `optimizer.yml`): Workflow-Filename.
- **`OPTIMIZER_WORKFLOW_REF`** (optional, default `master`): Branch.
- **Voraussetzung**: Bestehender `GITHUB_TOKEN` braucht jetzt zusaetzlich `actions:write` Scope. Falls nicht vorhanden: PAT neu erstellen.

### Lern-Loop (vollstaendig)
1. **Mo–Sa**: Trading-Server tradet, lernt im Brain (Win-Rate, Instrumente, Sektoren, Regime)
2. **Sa Nacht**: Letzte Trades fuellen `brain_state.json` + `trade_history.json` im Gist
3. **So 03:00 UTC**: GitHub Action startet automatisch
4. **So 03:00–03:25 UTC**: Optimizer testet 600+ Param-Kombinationen, findet beste Werte
5. **So 03:25 UTC**: Push von `config.json` + `optimization_history.json` in Gist
6. **So 03:30 UTC**: Trading-Server liest beim naechsten Cycle neue Params (via `restore_from_cloud()`)
7. **Ab Sonntag-Cycle**: Bot tradet mit optimierten Werten

### Race-Condition-Schutz
| Risiko | v10-Loesung |
|--------|------------|
| GH-Action ueberschreibt brain_state.json mit altem Stand | `backup_optimizer_results()` pusht NICHT brain_state |
| Optimizer-Status zombieert nach Render-OOM | `NO_RESTORE_FILES = {optimizer_status.json}` |
| Parallele Optimizer-Laeufe | `concurrency: investpilot-optimizer` im Workflow |
| Stale Status auf Dashboard | 60-Min Stale-Lock-Recovery in `/api/optimizer/run` (v9 bleibt aktiv) |

### Vorteile vs v9
- ✅ 7 GB RAM statt 512 MB → kein Memory-Safeguard-Abbruch noetig
- ✅ Container-OOM unmoeglich → Trading-Server bleibt 100% online
- ✅ Vollkommen autonom: Sonntags-Cron + Auto-Push, kein Mensch im Loop
- ✅ Reproduzierbar: Jeder Run hat GitHub Actions Log + Artifact
- ✅ Skaliert mit v5+ Grid (648 Combos) ohne weitere Aenderungen

## v11 — Persistent Disk Migration (2026-04-09)

### Hintergrund
Render Container-Filesystems sind ephemer: nach jedem Redeploy war
`/app/data/` weg → Brain-State, Trade-History und 2FA mussten via
Cloud-Restore (Gist) wiederhergestellt werden. Ein 10 GB Persistent
Disk wird jetzt auf `/data` gemountet, sodass Daten Redeploy-stabil
auf der Disk leben.

### DATA_DIR Resolution (`app/config_manager.py`)
`_resolve_data_dir()` priorisiert:
1. `INVESTPILOT_DATA_DIR` Env-Var (expliziter Override)
2. `/data` falls Mount existiert (Auto-Detect Persistent Disk)
3. `<repo>/data` Fallback (lokal / CI)

### Disk-Bootstrap (`_bootstrap_from_image_seed()`)
Wird beim Modul-Import ausgefuehrt, sobald `DATA_DIR != /app/data` und
`/app/data` existiert:
- **Immer**: `mkdir DATA_DIR/logs/` — kritisch, weil `scheduler.py`
  beim Import einen `FileHandler` auf `logs/scheduler.log` oeffnet und
  sonst beim Boot crash-loopt.
- **Idempotent**: Wenn `config.json` noch nicht in `DATA_DIR` liegt,
  werden ALLE `*.json` Seed-Dateien aus `/app/data` einmalig kopiert,
  damit FastAPI sofort startfaehig ist (Cloud-Restore aus Gist
  ueberlagert anschliessend `brain_state` etc.)

### Reihenfolge beim Container-Start
1. `config_manager` import → `_resolve_data_dir()` → `_bootstrap_from_image_seed()`
2. FastAPI startet (PID 1) → kann sofort `config.json` lesen
3. Scheduler Subprocess startet (PID 8) → Cloud-Restore via Gist →
   `brain_state.json`, `trade_history.json`, `auth_2fa.json` etc.
4. Trading-Zyklen alle 5 Min, Backup-Loop alle X Min Push in Gist

### Render-Setup
- Disk Name: investpilot-data
- Mount Path: `/data`
- Size: 10 GB ($2.50/Monat)
- WICHTIG: Disk-Attach disabled Zero-Downtime Deploys

### Bekannte Edge-Cases
- `auth_2fa.json` wird NUR via Cloud-Restore wiederhergestellt — wenn
  2FA vor Phase B (DATA_DIR-Fix) eingerichtet wurde, lag die Datei
  auf `/app/data` und ist nach Disk-Mount weg → 2FA muss neu
  eingerichtet werden.
- `audit.db` wird beim ersten FastAPI-Start auf `/data` frisch
  initialisiert (kein Restore aus Gist, da SQLite nicht in
  `BACKUP_FILES`).

### Render Auto-Deploy ist flaky
Push triggert nicht zuverlaessig einen Build. Nach `git push` ggf.
manuell "Manual Deploy → Deploy latest commit" im Render Dashboard
ausloesen.

## v12 — Game-Changer Paket (2026-04-09)

Grosses Feature-Bundle mit Ziel: In ~3 Wochen live-ready Trading-Maschine.
Alle Features sind modular, mit Feature-Flags, und ohne Breaking Changes
fuer bestehende Flows.

### Phase 1 — Exit-Disziplin & Signal-Qualitaet
- **Time-Stop Exit** (`app/trader.py` `check_stop_loss_take_profit`):
  Schliesst Positionen die laenger als `max_days_stale` (10 Tage)
  offen sind und < `stale_pnl_threshold_pct` (0.5%) bewegt haben.
  Schutz vor Opportunitaetskosten durch tote Positionen.
  Config: `time_stop.*`. Position-Open-Time wird aus eToro-API-Feldern
  gelesen; Fallback ueber `trade_history.json` Lookup via position_id.
- **Asymmetric R/R Tuning** (`data/config.json`):
  `stop_loss_pct: -2.5`, `take_profit_pct: 18`, Asset-Class-Parameter
  aktualisiert auf 1:3 R/R (z.B. stocks -3/+9, crypto -5/+15,
  commodities -6/+18, forex -1.5/+4.5). "Winners run, losers cut."
- **LLM-Sentiment via Claude Haiku** (`app/sentiment.py` komplett neu):
  Model `claude-haiku-4-5-20251001`, 4h TTL-Cache, JSON-Output
  (score/label/confidence/rationale). Keyword-Fallback bei fehlendem
  SDK/Key. Ersetzt fehleranfaelliges Keyword-Matching.

### Phase 2 — Meta-Labeling (Lopez de Prado)
- **`app/meta_labeler.py`** (NEU, ~330 Zeilen):
  - `train_meta_labeler()`: GradientBoosting (150 estimators, depth 3)
    trainiert auf Scanner-BUY Subset der Trade-History.
  - `meta_predict(signal_context, config)`: Gibt `p_win` + Decision
    (`take` / `skip` / `shadow_take` / `shadow_skip`) zurueck.
  - 12-dim Feature-Vektor: scanner_score, rsi, macd_hist, momentum_5d,
    momentum_20d, volatility, volume_trend, regime_code, vix_level,
    fear_greed, sector_code, asset_class_code.
  - **Shadow Mode first**: Initial blockt nichts, loggt nur Entscheidungen
    in `meta_labeling_shadow.json` (Rotation bei 1000 Eintraegen).
  - **Auto-Activation**: `check_and_maybe_activate()` flippt
    `shadow_mode=false` sobald auf matured Trades
    `precision >= min_precision_to_activate` (0.65) erreicht ist bei
    `min_trades_to_activate` (50).
  - Retrain: Taeglich um ~03:15 via `app/scheduler.py`.
- **Gate im Trader**: Vor jedem `client.buy()` wird `meta_predict()`
  konsultiert. Bei `decision="skip"` wird der Trade uebersprungen
  (nach Aktivierung) bzw. nur geloggt (im Shadow Mode).
- Config: `meta_labeling.*`. Persistiert in `meta_model.json` +
  `meta_labeling_shadow.json` (in BACKUP_FILES).

### Phase 3 — Kelly Position Sizing
- **`app/risk_manager.py`** neue Funktionen:
  - `_kelly_stats_from_history()`: Berechnet (winrate, avg_win_pct,
    avg_loss_pct, n_trades) aus Trade-History.
  - `calculate_kelly_position_size()`: Formel `f* = (p*b - q) / b`,
    dann Half-Kelly, dann Hard-Cap bei `max_fraction`. Score-Modulation
    im Bereich [0.5, 1.25].
- **Staffel-Cap** (Fat-Tail-Schutz):
  Woche 1: `max_fraction=0.01` (1%) → Woche 2: 0.015 → Woche 3: 0.02.
  Manuelle Erhoehung nach Validierung.
- Aktiviert in `trader.py` als Ersatz fuer Dynamic Sizing, wenn
  genuegend Trade-History (`min_trades=20`) vorhanden ist.
- Config: `kelly_sizing.*`.

### Phase 4 — Regime Intelligence
- **Phase 4.1 — VIX Term Structure** (`app/market_context.py`
  `fetch_vix_term_structure()`):
  Pullt `^VIX9D`, `^VIX`, `^VIX3M` via yfinance und klassifiziert die
  Kurve (`contango` / `backwardation` / `short_term_stress` / `flat`).
  Setzt `panic_dip_buy_signal = is_backwardation and vix >= 22 and ratio > 1.20`.
  Integriert in `update_full_context()`.
  **Panic-Dip Override**: In `trader.py` erlaubt der Override einen
  reduzierten Trade trotz Regime-Halt wenn
  `panic_dip_buy_signal = True` (Position * 0.6).
  Config: `vix_term_structure.*`.
- **Phase 4.2 — Regime-spezifische Strategie-Profile**
  (`app/market_scanner.py` `apply_regime_strategy_modifier()`):
  - **Bull**: Momentum-Signale verstaerken
    (`mom_strength * bull_momentum_boost`), Counter-Trend MR dampen
    wenn Preis unter SMA20.
  - **Sideways**: Mean-Reversion-Signale verstaerken
    (`mr_strength * sideways_mr_boost`), ueberdehnte Momentum-Trades
    (`mom_strength > 8 and boll > 0.8`) penalisieren.
  - **Bear**: Non-defensive Sektoren erhalten `bear_non_defensive_penalty`
    (-10). Nur sehr starke MR-Setups (`mr_strength > 10`) bekommen
    +3 Boost.
  - Aufruf NACH `score_asset()` in `scan_all_assets()` hinter
    Feature-Flag `regime_strategies.enabled` (Default: **false**).
  - Aktivierung erst nach Backtest-Validierung via GitHub Action Optimizer.
  - Config: `regime_strategies.*`.

### Hard Gates fuer Live-Gang (~30.04/01.05.2026)
Bot darf nur mit echtem Geld live gehen wenn ALLE Kriterien erfuellt:
- Sharpe Ratio > 1.0
- Max Drawdown < 8%
- Winrate > 50%
- Profit Factor > 1.3
- >= 60 Trades in Demo-Historie

### Neue Backup-Dateien
`app/persistence.py` BACKUP_FILES erweitert um:
- `meta_model.json`
- `meta_labeling_shadow.json`
- `partial_close_state.json`

### Neue Config-Sektionen (`data/config.json`)
- `time_stop`, `meta_labeling`, `kelly_sizing`, `vix_term_structure`,
  `regime_strategies`, `hedging`
- `leverage.trailing_sl_*` + `leverage.tp_tranches`

## v12.1 — Backtest Position-Sizing Fix (2026-04-09)

**Bug:** Erster v12-Backtest-Run lieferte Rendite **+1'696'401'623'234%**
(1.7 Billionen Prozent) und Kosten **588.9%**. Architektur war OK
(GH-Action-Offload funktioniert), aber `calculate_metrics()`,
`build_equity_curve()` und `calc_monthly_returns()` kompoundierten jeden
einzelnen Trade als ob 100% des Kapitals deployed waeren. Bei 1326 Trades
ueber 5 Jahre × ~1.5% Avg-Return → (1.015)^1326 ≈ 4×10^8 → exakt der Bug.

**Root Cause:** `calculate_metrics(trades)` hatte zwar bereits einen
optionalen `position_sizing`-Pfad (Trade-Returns × kelly_fraction), aber
KEINE der 5 Aufrufstellen hat ihn gesetzt. Live-Bot deployt
`kelly_sizing.max_fraction = 0.01` (1% pro Trade).

**Fix (`app/backtester.py` + `app/optimizer.py`):**
- Neuer Helper `_build_position_sizing_from_config(config)` extrahiert
  `kelly_fraction` aus `kelly_sizing.max_fraction` (Default 0.01)
- `build_equity_curve(trades, kelly_fraction=1.0)` skaliert Trade-Returns
- `calc_monthly_returns(trades, kelly_fraction=1.0)` skaliert ebenfalls
- `calculate_metrics()` skaliert `total_costs_pct` mit kelly_fraction
  (Gebuehren werden nur auf den deployten Slice gezahlt, nicht aufs
  gesamte Portfolio)
- Wiring in `walk_forward_validate`, `quick_walk_forward`,
  `run_full_backtest` und `optimizer._evaluate_combo_worker`

**Sharpe-Ratio bleibt unveraendert** — er ist scale-invariant
(`mean(r*k)/std(r*k) = mean(r)/std(r)`). Optimizer-Ranking war daher
nie kaputt, nur die gemeldeten Returns/MaxDD/Costs.

## v13 — Multi-Benchmark + Equity-History (2026-04-14)

**Hintergrund:** Bisher hatte das Dashboard nur Punkt-zu-Punkt Vergleiche
"Bot vs SPY" ueber 8 Zeitfenster. Drei Luecken:
- Nur **eine** Vergleichsgroesse — kein QQQ (Tech) oder 60/40 (Misch)
- Keine Monatshistorie wie im Backtest — User konnte nicht sehen, ob die
  Live-Performance der Backtest-Curve folgt
- Keine persistente Equity-Curve fuer spaetere Chart-Visualisierung

### Phase 1: Multi-Benchmark (`web/app.py` + Frontend)
- `_BENCHMARK_CACHE` jetzt `{symbol: {data, ts}}` (Per-Symbol 1h-Cache)
- Generisches `_fetch_ticker_closes(symbol, years=5)` ersetzt SPY-spezifisch
- `BENCHMARK_SYMBOLS = ["SPY", "QQQ", "AGG"]`, 60/40 = 0.6*SPY + 0.4*AGG
- `/api/benchmark` Response neu: pro Periode `spy_pct/qqq_pct/agg_pct/mix6040_pct`,
  zusaetzlich `benchmarks: [{key,label,name}]` fuer dynamisches Frontend
- Backwards-compat: `_fetch_spy_closes()` + `_spy_return_pct` Aliase
- Frontend `renderBenchmark()` iteriert ueber Benchmark-Liste, rendert pro
  Periode: Bot-Zeile + (Benchmark/Alpha) × N. Card-Tooltip erklaert alle 3.

### Phase 2: Daily Equity Snapshots (`app/equity_snapshot.py` NEU)
- Werktags >= 22:30 CET: Snapshot {date, ts, portfolio_total_value,
  spy_close, qqq_close, agg_close, source}
- Idempotent via Daily-Guard `equity_snapshot_last.flag`
- Portfolio-Wert: erst aus `brain_state.performance_snapshots[-1]`,
  Fallback Live ueber EtoroClient
- Benchmark-Closes ueber `_fetch_ticker_closes()` Cache (gleiche Quelle wie /api/benchmark)
- Persistenz: `data/equity_history.json` (max 1825 Eintraege, Auto-Rotation)
- Sofortiges `backup_to_cloud()` nach jedem Snapshot
- Scheduler: `maybe_take_snapshot(...)` als 5-Min-Tick-Hook in `scheduler_loop()`
- `BACKUP_FILES` (`app/persistence.py`) erweitert um `equity_history.json`

### API: `/api/equity-history` (`web/app.py`)
- `_aggregate_monthly()` baut Monats-Buckets (erster + letzter Snapshot pro
  Kalendermonat), liefert `bot_pct/spy_pct/qqq_pct/mix6040_pct + alpha_*`
- `MIN_SNAPSHOTS_FOR_TABLE = 5` — vorher `ready: false` mit Progress-Info
- `daily`-Reihen mitgesendet (fuer spaeteren Equity-Curve-Chart)
- `POST /api/equity-history/snapshot-now` — manueller Trigger fuers Dashboard

### Frontend: Equity-History Card (`web/static/index.html` + `app.js`)
- Neue Card unter "Bot vs Markt" mit Tabelle:
  Monat | Bot % | SPY % | α SPY | QQQ % | α QQQ | 60/40 % | α 60/40
- **Tooltips auf jeder Header-Spalte** (User-Praeferenz)
- Vor 5 Snapshots: Progress-Hinweis "X / Y Tage gesammelt"
- `loadEquityHistory()` laeuft non-blocking nach Benchmark-Render

### Datenfluss
```
22:30 CET Werktag
  -> scheduler tick (5 Min)
    -> equity_snapshot.maybe_take_snapshot()
      -> brain_state.performance_snapshots[-1].total_value
      -> _fetch_ticker_closes(SPY/QQQ/AGG)  [1h cache]
      -> equity_history.json append
      -> backup_to_cloud()  [Gist]

Dashboard-Open
  -> /api/benchmark         [Multi-Bench Perioden, 1h Cache]
  -> /api/equity-history    [Monatstabelle + Daily Reihen]
```

### Neue Dateien
- `app/equity_snapshot.py` (~200 LOC)
- `data/equity_history.json` (entsteht beim ersten 22:30-Lauf)
- `data/equity_snapshot_last.flag` (Tagesguard)

### Folge-Phase (noch nicht enthalten)
- Equity-Curve **Chart** (Liniendiagramm) — Daten werden bereits gesammelt
  (`daily`-Feld der API-Response), Chart-Rendering ab >= 90 Snapshots sinnvoll
- Drawdown-Vergleich Bot vs Benchmarks
- Sharpe/Vola-Vergleich pro Monat

## v14 — Exit-Forecast Card (2026-04-15)

**Hintergrund:** Nach der 0-Closed-Trades-Diagnose (5 offene Positionen, alle
zwischen +2.3% und +3.9%) war klar: Keine Position hatte einen Trigger
ausgeloest — aber vom Dashboard aus war nicht sichtbar, WIE nah/fern jeder
Trigger war. Diese Card macht das transparent.

### Backend (`web/app.py`)
- `GET /api/exit-forecast` — pro offener Position alle 7 Trigger + naechster
- Helper `_compute_exit_forecast(pos, config, trailing_state)` berechnet:
  - **SL** (hard, -2.5% aus `stocks.stop_loss_pct`)
  - **Trailing-SL** (aktiv ab +0.8%, -1.8% Trail vom Peak; Distanz aus
    `trailing_sl_state.json` Pro-Position-SL-Level vs. current_price)
  - **TP-1/2/3** (Tranchen aus `leverage.tp_tranches`: +4/+8/+15%,
    `active=false` wenn PnL >= target = bereits durchgelaufen)
  - **TP-final** (+18% aus `stocks.take_profit_pct`)
  - **Time-Stop** (ab 10d + |PnL|<0.5% — zeitbasiert, nicht preis-basiert;
    zeigt `days_until` + `eligible_now`)
- `next_trigger` = kleinstes positives `distance_pct` aller aktiven Trigger
- Response inkl. `config_summary` (aktuelle Parameter fuers Meta-Label)
- Sortierung: naechster Trigger zuerst (Dringlichkeits-Ranking)

### Frontend (`index.html` + `app.js`)
- Neue Card unter "Equity-Verlauf" mit Tabelle:
  Asset | Alter | PnL % | Naechster Trigger (Pfeil↑/↓/⏱ + Typ) | Abstand | Alle Trigger
- **Tooltip auf jeder Header-Spalte** (User-Praeferenz)
- "Alle Trigger"-Spalte zeigt kompakt alle 7 Ausstiege mit Distanz,
  durchlaufene Tranchen durchgestrichen grau
- Meta-Zeile: komplette Exit-Config auf einen Blick

### Warum das nuetzlich ist
Beantwortet in einem Blick: "Warum schliesst der Bot gerade nichts?" —
sichtbar welche Position z.B. 0.13% vor TP-1 steht oder welche vom Time-Stop
noch X Tage entfernt ist. Parameter-Tuning-Entscheidungen (TP-1 auf +2.5%
senken?) sind datengetrieben statt aus dem Bauch.

## v17 — Broker-Abstraktion (W2, 2026-04-25)

**Hintergrund:** Damit der Bot konfigurations-gesteuert zwischen eToro und
IBKR umschalten kann, brauchen alle Konsumenten (`trader.py`, `brain.py`,
`web/app.py`, etc.) ein gemeinsames Interface. Bisher war `EtoroClient`
direkt importiert — Broker-Wechsel haette ueberall Code-Aenderungen erfordert.

### Architektur — Adapter-Pattern, nicht-breaking

- **`app/broker_base.py`** (NEU): Abstract Base `BrokerBase` mit dem
  vollstaendigen eToro-API-Surface (10 Methoden + `broker_name` Property).
  Plus Factory `get_broker(config) -> BrokerBase`.
- **`app/etoro_client.py`**: `EtoroClient` erbt jetzt formal von
  `BrokerBase` — kein Verhalten geaendert, alle bestehenden Imports
  funktionieren weiter.
- **`app/ibkr_client.py`**: `IbkrBroker(BrokerBase)` Klasse hinzugefuegt.
  Read-Operations (Portfolio, Equity, Cash, P/L, Search, Instruments) sind
  LIVE und gegen Paper-Account DUP108015 verifiziert. Write-Operations
  (`buy/sell/close_position`) sind bewusst `NotImplementedError`-Stubs mit
  klarer W3-TODO-Begruendung.
- **`config.json`**: Neue Top-Level-Keys `broker: "etoro"` (default,
  backwards-compat) und `ibkr: {host, port, client_id, timeout, readonly}`.

### Warum Read-Live + Write-Stub?

eToro's `buy(instrument_id, amount_usd)` ist API-asymmetrisch zu IBKR's
`placeOrder(Contract, MarketOrder(quantity))`. Eine ehrliche Implementierung
braucht:
1. Live-Quote-Lookup fuer `qty = floor(amount_usd / price)`
2. Contract-Resolution: `instrument_id` (eToro-int) -> IBKR `conId` + `symbol` + `exchange` + `currency`
3. Order-Tracking: IBKR liefert Order-Status asynchron, anders als eToro's
   sofortige Response

Das wird in W3 ausgebaut. Bis dahin: **`broker = "etoro"` in `config.json`
belassen** — der Bot tradet weiter wie gewohnt. `IbkrBroker` ist verfuegbar
fuer Read-Only-Experimente (Portfolio-Abfragen, Search) ohne Live-Trading.

### Migration-Pfad

Bestehende 8 Importer (`trader.py:1343`, `web/app.py:372` u.a. — siehe
`grep "from app.etoro_client"`) sind nicht angefasst. Nach und nach koennen
sie von `EtoroClient(config)` auf `get_broker(config)` migriert werden.
Beispiel:
```python
# Alt:
from app.etoro_client import EtoroClient
client = EtoroClient(config)

# Neu (broker-agnostic):
from app.broker_base import get_broker
client = get_broker(config)
```

### Tests

`tests/test_broker_base.py` mit 9 Tests, alle gruen:
- Interface-Compliance (EtoroClient + IbkrBroker erfuellen ABC)
- Factory-Routing (etoro/ibkr/default/case-insensitive/unknown)
- Write-Op-Stubs werfen `NotImplementedError` mit "W3"-Marker
- Config-Override fuer host/port/client_id

### Naechster Schritt (W3)

`IbkrBroker.buy/sell/close_position` echt implementieren:
- Contract-Resolution-Cache (eToro-instrument_id -> IBKR `Contract`)
- Live-Quote-Service fuer Amount-zu-Quantity-Mapping
- Async-Order-Tracking-Adapter (IBKR -> eToro-aehnliche synchrone Response)

## v16 — IBKR API-Connection live (W2 Start, 2026-04-25)

**Hintergrund:** Erste echte ib_insync-Verbindung vom `investpilot` Container
zum `ib-gateway` Container am VPS. Login bei Paper-Account DUP108015 erfolgreich,
alle 3 Data-Farms (usfarm Market, ushmds HMDS, secdefil SecDef) gruen,
server version 176.

### Bug-Hunt-Story

Symptom: `ib_insync.connect("ib-gateway", 4002)` -> `Connected -> Disconnected`
in 4ms, dann `TimeoutError`. Klassisches API-Reject ohne Logmessage.

**Falsche Hypothesen (verworfen):**
- `ctciAutoEncrypt=true` in jts.ini (war bereits `false`)
- `TrustedIPs=127.0.0.1` zu restriktiv (Patch in IBC `TrustedTwsApiClientIPs=172.18.0.2`
  hilft nicht, weil Verbindung eh nie 4002 direkt nutzt)

**Echte Ursache — Image-Architektur (`gnzsnz/ib-gateway:stable`):**
- IB Gateway lauscht intern **nur auf 127.0.0.1:4002** (strict localhost)
- Ein **socat**-Daemon im Container exposed Port `0.0.0.0:4004` und forwarded
  zu `127.0.0.1:4002`
- Damit sieht IBG die Connection als "lokal" und akzeptiert sie

**Fix:** ib_insync MUSS zu Port **4004** connecten, NICHT 4002.
```python
ib.connect("ib-gateway", 4004, clientId=1, timeout=15)
```

### Neue Datei: `app/ibkr_client.py`

Stub-Modul mit:
- `IBG_HOST`, `IBG_PORT=4004`, `IBG_CLIENT_ID`, `IBG_TIMEOUT` als Konstanten
  (env-var-overridable)
- `connect(host, port, client_id, timeout, readonly)` — Wrapper um `ib_insync.IB`
- `healthcheck() -> dict` — schneller Connectivity-Check ohne Side-Effects
- CLI-Modus: `python -m app.ibkr_client` -> JSON-Output mit Status

Vollstaendige Doku im Modul-Docstring, inkl. Port-Architektur-Erklaerung
damit zukuenftige Entwickler nicht in dieselbe 4002-Falle laufen.

### Persistente Settings am VPS

In `/opt/ib-gateway/` Container:
- `IBC config.ini.tmpl`: `TrustedTwsApiClientIPs=172.18.0.2` gepatcht
  (Belt-and-Suspenders, falls jemand mal 4002 direkt nutzen will)
- `Jts/jts.ini`: `TrustedIPs=127.0.0.1,172.18.0.2` (wird von IBC bei jedem
  Restart aus Template neu generiert)

**Bekannte Persistenz-Grenze:** Bei `docker compose down + up` (Container-Recreate
ohne Volume-Mount) gehen die Patches verloren weil sie im Image-FS liegen.
Saubere Loesung waere Volume-Mount fuer `/home/ibgateway/Jts/` oder Custom-Image
mit angepasstem `IBC config.ini.tmpl` — TODO fuer W3.

### Naechster Schritt (W2)

`app/broker_base.py` als abstrakte Broker-Klasse, `etoro_client.py` als
`EtoroBroker`-Implementation, `IbkrBroker`-Implementation in
`app/ibkr_client.py` ausbauen (Order-Submit, Portfolio-Pull, Trade-History).

## v15 — Tooling: Image-Size Guards fuer Claude Code (2026-04-25)

**Hintergrund:** Claude Code Sessions im InvestPilot-Projekt scheiterten
wiederholt mit `400 invalid_request_error: "Could not process image"` wenn
groessere Screenshots an die Anthropic-API gesendet wurden. Zwei Quellen:
(1) Live-Browser-Screenshots in 4K-Aufloesung via Chrome-MCP, (2) TWS
Desktop-Screenshots (`scr8.png`, `scrA.png` etc.), die ueber `Read` /
`Desktop_Commander__read_file` geladen wurden. API-Limit fuer Bilder ist
faktisch ~5 MB / Bild — 4K-PNGs sprengen das regelmaessig.

Reine Memory-/Prompt-Anweisung ("nutze keine Screenshots") griff nicht
zuverlaessig — das Modell hielt sich nicht durchgaengig daran.

### Architektur: Source-Control statt Post-Processing
Anthropic-API-Bilder lassen sich aus einem PostToolUse-Hook nicht
zuverlaessig modifizieren (Bytes laufen am Hook-stdout-Channel vorbei).
Statt nachtraeglicher Kompression: Quelle limitieren, BEVOR der Tool-Call
das Bild produziert.

### Phase 1: Browser-Viewport-Guard (`hooks/viewport_guard.py`)
- PreToolUse-Hook auf `mcp__Claude_in_Chrome__.*` und
  `mcp__plugin_chrome-devtools-mcp_chrome-devtools__.*`
- Bei Screenshot-Tools: prueft `hooks/.viewport_state.json` —
  wurde der Browser in den letzten 300s auf <= 1280x960 resized?
  - Ja → silent allow
  - Nein → block mit Instruktion an Claude: `resize_window(1280, 960)`
    zuerst, danach Screenshot retry
- Bei Resize-Tools: parst `width`/`height` aus tool_input. Wenn beides
  <= 1280/960 → State-Timestamp setzen.
- Fail-open: Hook-Crash → exit 0 → Tool laeuft normal
- TTL = 5 Min: lang genug fuer mehrere Screenshot-Sequenzen, kurz genug
  damit zwischenzeitliches Maximize nicht stundenlang nachwirkt

### Phase 2: Image-Resize-Guard (`hooks/image_resize_guard.py`)
- PreToolUse-Hook auf `Read` und `mcp__Desktop_Commander__read_file`
- Prueft `tool_input.file_path`: ist es ein Bild
  (`.png/.jpg/.jpeg/.webp/.bmp/.gif`) UND > 1 MB ODER > 1280x960?
- Wenn ja: **resized die Datei in-place** via Pillow vor dem Read:
  1. Backup einmalig nach `<name>.<ext>.orig` (falls noch nicht vorhanden)
  2. Thumbnail auf max 1280x960 (LANCZOS, Aspect-preserved)
  3. Erst PNG (lossless) probieren. Wenn immer noch > 1 MB → JPEG q=85
- Tool liest danach die bereits kleine Datei → API-Limit wird nie
  ueberschritten
- Fail-open bei jedem Exception → Original geht durch
- Test bestaetigt: 2560x1440 PNG (483 KB) → 1280x720 (134 KB)

### Settings-Eintrag (`.claude/settings.json`)
```json
"hooks": {
  "PreToolUse": [
    { "matcher": "mcp__Claude_in_Chrome__.*|mcp__plugin_chrome-devtools-mcp_chrome-devtools__.*",
      "hooks": [{ "type": "command", "command": "python .../hooks/viewport_guard.py" }] },
    { "matcher": "Read|mcp__Desktop_Commander__read_file",
      "hooks": [{ "type": "command", "command": "python .../hooks/image_resize_guard.py" }] }
  ]
}
```

### Neue Dateien
- `hooks/viewport_guard.py` (~120 LOC)
- `hooks/image_resize_guard.py` (~140 LOC)
- `hooks/.viewport_state.json` (Auto-generiert)
- `hooks/viewport_guard.log`, `hooks/image_resize_guard.log` (Audit-Logs)
- `*.orig` Backup-Dateien neben jedem resized Image (einmalig pro Datei)

### Bekannte Grenzen
- `Desktop_Commander__write_file` mit Bild-Bytes ist nicht abgedeckt
  (selten relevant)
- Andere MCP-Server, die direkt Bild-Content im Response liefern (ohne
  Datei-Umweg), umgehen den Hook — kein bekannter Fall im InvestPilot-Setup
- Hook greift erst nach Session-Restart (Claude Code laedt
  `settings.json` nur beim Start)

### Voraussetzungen
- Python 3.x mit Pillow auf dem Host (verifiziert: 3.14.3 + Pillow 12.1.1)

## Live-Gang Strategie (Target: 2026-04-27)

### Kapital-Plan
- **Start:** 2'000 CHF am 27.04.2026
- **DCA-Plan:** +1'800 CHF monatlich (Monatsanfang)
- **Trajektorie ohne Rendite:**
  - Monat 3 (Jul): 7'400 CHF
  - Monat 6 (Okt): 12'800 CHF
  - Monat 12 (Apr 2027): 23'600 CHF
  - Monat 24 (Apr 2028): 45'200 CHF
- **Broker:** eToro (bleibt bis 10k+ wegen Kapital-Effizienz, dann IBKR-Evaluation gemaess Roadmap Phase 4)

### Sizing-Strategie: prozent-basiert statt fix
**Kein `max_single_trade_usd` Fix-Wert** — Config skaliert automatisch mit Portfolio-Wert:

```json
{
  "demo_trading": {
    "max_single_trade_pct_of_portfolio": 0.15,
    "max_single_trade_usd_floor": 50,
    "max_single_trade_usd_hard_cap": null
  },
  "portfolio_sizing": {
    "max_positions_by_capital": {
      "3000":   6,
      "10000": 10,
      "30000": 15,
      "999999": 20
    }
  },
  "kelly_sizing": {
    "max_fraction": 0.04
  }
}
```

**Auto-Skalierung Beispiele:**

| Kapital | 15% Cap / Trade | Max Positionen | ⌀ Size |
|---------|-----------------|-----------------|--------|
| 2'000 | 300 | 6 | ~333 |
| 7'400 | 1'110 | 10 | ~740 |
| 12'800 | 1'920 | 15 | ~853 |
| 23'600 | 3'540 | 15 | ~1'573 |

→ **Config-Datei muss nie wieder angefasst werden** wenn Kapital waechst.

### Cash-Deposit-DCA
Wenn am 1. des Monats 1'800 CHF Cash reinkommt, wuerde der Bot beim naechsten
Scanner-Tick alles auf einmal deployen → Market-Timing-Risiko (alles zum
Monatshoch gekauft). Loesung:

```json
{
  "deposit_handling": {
    "dca_on_new_cash": true,
    "dca_spread_cycles": 5,
    "min_new_cash_trigger_usd": 500
  }
}
```

**Logik:** Neuer Cash > 500 USD detected (Delta zum letzten Scan) → Deploy
ueber 5 Scanner-Zyklen staffeln. Bei 5-Min-Scheduler = 25 Min Staffel-Fenster.
Optional: `dca_spread_cycles` spaeter auf Stunden/Tage ausdehnen wenn
Hypothese bestaetigt.

### Pre-Live-Checklist (27.04.-Target)

**Muss-Features (15.-18.04.):**
- [x] Audit-Fix Waves 1-3 (2026-04-15)
- [ ] **Phase 2/5: Fruehwarnsignale** (`macro_signals.py`):
  - Yield Curve Inversion (2Y/10Y via FRED API — kostenlos)
  - Credit Spread (HYG vs IEF Ratio via yfinance)
  - Marktbreite (% SP500-Aktien ueber SMA200 via yfinance)
  - Integration ins Regime-System als weiterer Filter neben VIX/F&G
- [ ] **Phase 1/1: Prozent-basierte Sizing + Cash-DCA** (risk_manager.py +
  Config-Migration)
- [ ] **Phase 1/2: Backtest-History** (analog `optimization_history.json`)
- [ ] **Phase 1/4: Wartungs-Block Weekly Report** (Cron-Status, Backup-Alter,
  ML-Modell-Alter)

**Nicht-Blocker (spaeter):**
- Monte-Carlo-Simulation (Phase 1/3) — braucht 60+ echte Trade-Daten
- Bots Nr. 2-6, Multi-Strategy-Router (Phase 3) — erst wenn Bot 1 live
  profitabel
- IBKR-Broker-Wechsel (Phase 4) — erst bei 10k+ CHF

**Go-Live-Prozedur (26.04. abends):**
1. Finaler Backtest (Dashboard-Button, GH Action): alle 4 Hard-Gates
   erfuellt? (Sharpe >1.0, MaxDD <8%, Winrate >50%, PF >1.3)
2. Finaler Kelly-Sweep (Dashboard-Button, GH Action): bestaetigt
   `max_fraction = 0.04`
3. Kill-Switch-Drill: Trading via Dashboard pausieren + reaktivieren
4. Telegram-Alert-Test: Manueller Trigger eines CLOSE_FAILED-Events
5. eToro Env-Switch: `ETORO_ENVIRONMENT=real` in Render
6. `trading_enabled.flag = true`
7. Erste 48h: Dashboard alle 2h checken, Alerts monitoren

**Abbruch-Kriterien (Live wird pausiert/deaktiviert):**
- Drawdown > 5% in den ersten 2 Wochen (Hard-Stop)
- 3+ Close-Failures in 24h
- Scheduler-Crash / Health RED > 30 Min
- Ungeplante Position-Groesse > 20% Portfolio (Sizing-Bug)

## Legacy-Dateien (Root)
Vorgaenger der modularen Version, koennen aufgeraeumt werden:
- `demo_trader.py`, `trade_brain.py`, `investpilot.py`
- `*.log`, `*.bat`
