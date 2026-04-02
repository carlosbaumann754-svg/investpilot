# InvestPilot — Autonomer Trading Bot

## Projekt-Uebersicht
Vollautonomer Trading Bot auf der eToro Public API. Selbstlernend, Docker-containerisiert, mit Web-Dashboard.
Inkl. Risk Management, Leverage Management, Asset-Filters, Market Context, Execution Tracking, Alerting.
Inkl. Backtesting Engine, ML Scoring (Gradient Boosting), Walk-Forward Validation.

**Projekt-Pfad:** `C:\Users\CarlosBaumann\OneDrive - Mattka GmbH\Desktop\Claude\investpilot`
**eToro User:** carlosbaumann777
**Deployment:** Render (Free Tier) + Synology NAS

## Architektur

```
investpilot/
├── app/                        # Backend-Module
│   ├── etoro_client.py         # eToro REST API Client (demo + real, Key-Variante A/B)
│   ├── trader.py               # Trading Engine v2 (5-Min-Zyklen, alle Safety-Checks integriert)
│   ├── brain.py                # Selbstlernendes AI-Modul (Walk-Forward, Scoring, Regime)
│   ├── market_scanner.py       # 70+ Assets Technical Analysis + Multi-Timeframe + ML Scoring
│   ├── backtester.py           # [NEU v3] Backtesting Engine: 5J Historie, Walk-Forward, Kostenmodell
│   ├── ml_scorer.py            # [NEU v3] ML Scoring: Gradient Boosting, 14 Features, JSON-Serialisierung
│   ├── risk_manager.py         # Risikomanagement: Position Sizing, Drawdown, Margin, Korrelation
│   ├── leverage_manager.py     # Dynamischer Hebel, eToro-Limits, Trailing SL, TP-Staffelung
│   ├── alerts.py               # Telegram/Discord Notifications, Watchdog, Kill Switch
│   ├── market_context.py       # VIX, Fear&Greed, Makro-Events, Earnings, Saisonalitaet
│   ├── asset_filters.py        # Asset-Klassen-Filter: Zeitfenster, Crypto, Forex, Rohstoffe
│   ├── execution.py            # Slippage-Tracking, Latenz, Performance-Breakdown, Sortino
│   ├── asset_discovery.py      # Woechentliche neue Asset-Suche (40+ Queries)
│   ├── scheduler.py            # Daemon Loop (5 Min Intervall, Watchdog, Market Context)
│   ├── persistence.py          # GitHub Gist Cloud Backup/Restore (14 Dateien)
│   ├── weekly_report.py        # Freitag-Reports (JSON + HTML + PDF) inkl. Backtest-Sektion
│   ├── report_pdf.py           # PDF-Generierung via ReportLab
│   └── config_manager.py       # Config/Pfad-Management (Docker + lokal)
├── web/                        # FastAPI Dashboard
│   ├── app.py                  # 34 REST Endpoints inkl. Kill Switch, Risk, Backtest, ML
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
- **Daily Summary**: Automatisch um 21:00
- **Watchdog**: Ueberwacht Bot-Aktivitaet, Alert bei Ausfall
- **Telegram Commands**: /killswitch, /status, /start remote ausfuehrbar

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
- **14 Features**: RSI, MACD (3), Bollinger Position, Momentum (5d/20d), Volatilitaet, Volume Trend, SMA-Vergleiche, Golden Cross, RSI Slope, Price vs SMA20%
- **Label**: Binaer — Preis steigt >1% in naechsten 5 Tagen
- **Walk-Forward Training**: 80/20 Split, Accuracy/Precision/Recall/F1
- **JSON-Serialisierung**: Kein Pickle — Feature Importances + Thresholds als JSON (Docker-sicher)
- **Integration**: `market_scanner.score_asset(use_ml=True)` — ML-Score 0-100 → -100/+100 Mapping
- **Safety Default**: `use_ml_scoring: false` — manuell aktivieren nach Backtest-Validierung

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

### Market Scanner (`app/market_scanner.py`) — v2
- 70+ Assets: 35 Aktien, 12 ETFs, 10 Crypto, 5 Commodities, 5 Forex, 4 Indizes
- **Multi-Timeframe**: 1H Trend + 15M Entry + 5M Stop-Loss
- **MTF Score-Bonus**: 15% wenn alle Timeframes aligned

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
- **`GET /api/backtest`** — [NEU v3] Letzte Backtest-Ergebnisse
- **`POST /api/backtest/run`** — [NEU v3] Backtest ausfuehren (async)
- **`GET /api/ml-model`** — [NEU v3] ML-Modell Info (Feature Importances, Accuracy)
- **`POST /api/ml-model/train`** — [NEU v3] ML-Modell trainieren
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
- **alerts**: Telegram/Discord Config, Email
- **strategies**: Core/Growth/Dividend/Tactical Targets

### Umgebungsvariablen
- `ETORO_PUBLIC_KEY`, `ETORO_PRIVATE_KEY`, `ETORO_DEMO_PRIVATE_KEY`, `ETORO_ENVIRONMENT`
- `JWT_SECRET`, `ADMIN_USER`, `ADMIN_PASSWORD_HASH`
- `GITHUB_TOKEN` (Cloud Backup)
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (Alerts)
- `DISCORD_WEBHOOK_URL` (Alerts)

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
- `scanner_state.json` — Scanner-Cache
- `discovery_result.json` — Letzte Asset-Discovery
- `weekly_report.json` — Letzter Weekly Report
- `audit.db` — SQLite Security Database

## Legacy-Dateien (Root)
Vorgaenger der modularen Version, koennen aufgeraeumt werden:
- `demo_trader.py`, `trade_brain.py`, `investpilot.py`
- `*.log`, `*.bat`
