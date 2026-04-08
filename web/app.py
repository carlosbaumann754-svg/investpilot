"""
InvestPilot - FastAPI Web Dashboard
REST API + Mobile-First Frontend fuer Trading-Steuerung.
"""

import os
import sys
import logging
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException, Depends, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from pydantic import BaseModel, validator
from typing import Optional

# PYTHONPATH sicherstellen
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config_manager import load_config, save_config, get_data_path
from app.etoro_client import EtoroClient
from web.data_access import (
    read_json_safe, write_json_safe, get_trading_status,
    set_trading_enabled, read_log_tail
)

from web.auth import (
    authenticate_user, create_partial_token, decode_partial_token,
    create_token, verify_password,
)
from web.security import security_middleware, record_failed_login, log_audit as _log_audit
from web import auth_2fa

log = logging.getLogger("WebApp")

app = FastAPI(title="InvestPilot Dashboard", version="1.0.0")

# Security Middleware registrieren
app.middleware("http")(security_middleware)

# Static files
STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ============================================================
# CACHE-BUSTING fuer statische Assets
# ============================================================
# Version-Token wird einmal beim App-Start berechnet und an /static/app.js
# + /static/style.css im HTML angehaengt. Nach einem Deploy aendert sich der
# Token → Browser holt die neue Datei, ohne dass der User hart neu laden muss.
def _compute_static_version() -> str:
    # Bevorzugt: Git-SHA von Render (automatisch gesetzt)
    sha = os.environ.get("RENDER_GIT_COMMIT")
    if sha:
        return sha[:12]
    # Fallback: max mtime von app.js + style.css (aendert sich bei jedem Deploy)
    try:
        tokens = []
        for name in ("app.js", "style.css"):
            p = STATIC_DIR / name
            if p.exists():
                tokens.append(str(int(p.stat().st_mtime)))
        return "-".join(tokens) if tokens else "dev"
    except Exception:
        return "dev"


_STATIC_VERSION = _compute_static_version()


def _render_html_with_version(filename: str) -> HTMLResponse:
    """Liest ein HTML-Template und haengt ?v=<version> an app.js/style.css.

    Verhindert Browser-Cache-Probleme nach Deploys — der User sieht
    automatisch die neue Version ohne Hard-Reload.
    """
    path = STATIC_DIR / filename
    try:
        html = path.read_text(encoding="utf-8")
    except Exception as e:
        log.error(f"HTML read error {filename}: {e}")
        return HTMLResponse("<h1>Error loading page</h1>", status_code=500)
    html = html.replace("/static/app.js", f"/static/app.js?v={_STATIC_VERSION}")
    html = html.replace("/static/style.css", f"/static/style.css?v={_STATIC_VERSION}")
    return HTMLResponse(content=html)


# ============================================================
# MODELS
# ============================================================

VALID_STRATEGIES = ("aggressive_day_trade", "balanced_growth", "conservative_etf", "custom")

class StrategyUpdate(BaseModel):
    strategy: Optional[str] = None
    stop_loss_pct: Optional[float] = None
    take_profit_pct: Optional[float] = None
    rebalance_threshold_pct: Optional[float] = None
    default_leverage: Optional[int] = None
    max_single_trade_usd: Optional[float] = None
    portfolio_targets: Optional[dict] = None

    @validator("strategy")
    def validate_strategy(cls, v):
        if v is not None and v not in VALID_STRATEGIES:
            raise ValueError(f"Strategie muss eine von {VALID_STRATEGIES} sein")
        return v

    @validator("stop_loss_pct")
    def validate_sl(cls, v):
        if v is not None and not (-50 <= v <= 0):
            raise ValueError("Stop-Loss muss zwischen -50 und 0 sein")
        return v

    @validator("take_profit_pct")
    def validate_tp(cls, v):
        if v is not None and not (0 <= v <= 100):
            raise ValueError("Take-Profit muss zwischen 0 und 100 sein")
        return v

    @validator("default_leverage")
    def validate_leverage(cls, v):
        if v is not None and v not in (1, 2, 5, 10, 25):
            raise ValueError("Leverage muss 1, 2, 5, 10 oder 25 sein")
        return v

    @validator("max_single_trade_usd")
    def validate_max_trade(cls, v):
        if v is not None and not (100 <= v <= 50000):
            raise ValueError("Max Trade muss zwischen 100 und 50000 USD sein")
        return v


# ============================================================
# AUTH DEPENDENCY (wird in Phase 3 implementiert)
# ============================================================

async def require_auth(request: Request):
    """Auth-Check. Wird in Phase 3 mit JWT ersetzt."""
    # Phase 3: hier wird JWT-Validierung eingefuegt
    from web.auth import verify_request
    return await verify_request(request)


# ============================================================
# HEALTH CHECK (kein Auth)
# ============================================================

@app.get("/health")
async def health():
    return {"status": "ok", "service": "investpilot"}


# ============================================================
# AUTH ENDPOINTS
# ============================================================

class LoginRequest(BaseModel):
    username: str
    password: str

class TwoFactorVerifyRequest(BaseModel):
    partial_token: str
    code: str
    is_recovery: bool = False

class TwoFactorConfirmRequest(BaseModel):
    code: str

class TwoFactorDisableRequest(BaseModel):
    code: str


@app.post("/api/auth/login")
async def login(req: LoginRequest, request: Request):
    """Login Stufe 1: Username/Password.

    - Wenn 2FA aus: voller JWT-Token zurueck.
    - Wenn 2FA an: partial_token + requires_2fa=True. Client muss
      dann /api/auth/verify-2fa mit dem TOTP-Code aufrufen.
    """
    ip = request.client.host if request.client else "unknown"

    # Erst Username/Password pruefen (nutzt verify_password + Username-Check)
    from web.auth import DASHBOARD_USERNAME
    if req.username != DASHBOARD_USERNAME or not verify_password(req.password):
        record_failed_login(ip, req.username)
        raise HTTPException(status_code=401, detail="Falscher Username oder Passwort")

    # 2FA-Status pruefen
    if auth_2fa.is_enabled():
        partial = create_partial_token(req.username)
        await _log_audit(req.username, "LOGIN_STAGE1_OK", f"Stage1 von {ip}, 2FA erforderlich", "INFO", ip)
        return {
            "requires_2fa": True,
            "partial_token": partial,
            "username": req.username,
        }

    # Kein 2FA — direkt vollen Token ausstellen
    full_token = create_token(req.username)
    await _log_audit(req.username, "LOGIN_SUCCESS", f"Login von {ip}", "INFO", ip)
    return {"token": full_token, "username": req.username}


@app.post("/api/auth/verify-2fa")
async def verify_2fa(req: TwoFactorVerifyRequest, request: Request):
    """Login Stufe 2: TOTP-Code (oder Recovery-Code) verifizieren.

    Tauscht partial_token + Code gegen vollen JWT-Token.
    """
    ip = request.client.host if request.client else "unknown"

    payload = decode_partial_token(req.partial_token)
    if not payload:
        raise HTTPException(status_code=401, detail="Partial-Token ungueltig oder abgelaufen")

    username = payload.get("sub", "")
    if not username:
        raise HTTPException(status_code=401, detail="Token ohne Benutzer")

    if req.is_recovery:
        ok = auth_2fa.verify_recovery_code(req.code)
        action = "LOGIN_2FA_RECOVERY"
    else:
        ok = auth_2fa.verify_totp(req.code)
        action = "LOGIN_2FA_TOTP"

    if not ok:
        record_failed_login(ip, username)
        await _log_audit(username, "LOGIN_2FA_FAIL", f"2FA fehlgeschlagen von {ip}", "WARNING", ip)
        raise HTTPException(status_code=401, detail="Falscher Code")

    full_token = create_token(username)
    await _log_audit(username, action, f"2FA OK von {ip}", "INFO", ip)
    return {"token": full_token, "username": username}


@app.get("/api/auth/2fa/status")
async def two_factor_status(user=Depends(require_auth)):
    """Aktueller 2FA-Status fuer den Settings-Tab."""
    return auth_2fa.get_status()


@app.post("/api/auth/2fa/setup")
async def two_factor_setup_start(user=Depends(require_auth)):
    """Startet Setup-Flow: erzeugt Secret + QR + Recovery-Codes.

    Diese Daten werden NUR EINMAL zurueckgegeben — nach diesem Call
    muss der User sie scannen/notieren. Setup ist erst nach
    /api/auth/2fa/setup/confirm aktiv.
    """
    if auth_2fa.is_enabled():
        raise HTTPException(status_code=400, detail="2FA ist bereits aktiviert. Erst deaktivieren um neu einzurichten.")
    return auth_2fa.begin_setup(user)


@app.post("/api/auth/2fa/setup/confirm")
async def two_factor_setup_confirm(req: TwoFactorConfirmRequest, user=Depends(require_auth)):
    """Bestaetigt Setup mit erstem TOTP-Code aus der Authenticator-App."""
    if auth_2fa.confirm_setup(req.code):
        await _log_audit(user, "2FA_ENABLED", "2FA erfolgreich eingerichtet", "INFO", "")
        return {"ok": True}
    raise HTTPException(status_code=400, detail="Falscher Code — bitte aus der Authenticator-App neu eingeben")


@app.post("/api/auth/2fa/disable")
async def two_factor_disable(req: TwoFactorDisableRequest, user=Depends(require_auth)):
    """Deaktiviert 2FA. Erfordert gueltigen TOTP-Code zur Bestaetigung."""
    if auth_2fa.disable(req.code):
        await _log_audit(user, "2FA_DISABLED", "2FA deaktiviert", "WARNING", "")
        return {"ok": True}
    raise HTTPException(status_code=400, detail="Falscher Code")


# ============================================================
# FRONTEND
# ============================================================

@app.get("/")
async def root():
    return _render_html_with_version("index.html")

@app.get("/login")
async def login_page():
    return _render_html_with_version("login.html")


# ============================================================
# API ENDPOINTS
# ============================================================

@app.get("/api/portfolio")
async def api_portfolio(user=Depends(require_auth)):
    """Live Portfolio-Status von eToro."""
    try:
        config = load_config()
        client = EtoroClient(config)
        if not client.configured:
            return {"error": "eToro nicht konfiguriert"}

        portfolio = client.get_portfolio()
        if not portfolio:
            return {"error": "Portfolio nicht verfuegbar"}

        credit = portfolio.get("credit", 0)
        positions = portfolio.get("positions", [])
        unrealized_pnl = portfolio.get("unrealizedPnL", 0)

        parsed = [EtoroClient.parse_position(pos) for pos in positions]
        total_invested = sum(p["invested"] for p in parsed)

        return {
            "credit": round(credit, 2),
            "invested": round(total_invested, 2),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "total_value": round(credit + total_invested + unrealized_pnl, 2),
            "num_positions": len(positions),
            "positions": parsed,
        }
    except Exception as e:
        log.error(f"Portfolio API Error: {e}")
        return {"error": str(e)}


# ============================================================
# BENCHMARK (SPY S&P 500) — In-Memory Cache mit 1h TTL
# ============================================================
_BENCHMARK_CACHE: dict = {"data": None, "ts": 0.0}


def _fetch_spy_closes(years: int = 5):
    """Holt SPY-Tagesschlusskurse via yfinance, gecached fuer 1h.

    Returns:
        dict {date: close_price} oder None bei Fehler.
    """
    import time as _time
    now_ts = _time.time()
    if _BENCHMARK_CACHE["data"] and (now_ts - _BENCHMARK_CACHE["ts"] < 3600):
        return _BENCHMARK_CACHE["data"]
    try:
        import yfinance as yf
        ticker = yf.Ticker("SPY")
        hist = ticker.history(period=f"{years}y", interval="1d")
        if hist.empty:
            return None
        closes = {}
        for date_idx, row in hist.iterrows():
            d = date_idx.to_pydatetime().replace(tzinfo=None).date()
            closes[d] = float(row["Close"])
        _BENCHMARK_CACHE["data"] = closes
        _BENCHMARK_CACHE["ts"] = now_ts
        log.info(f"SPY-Cache aktualisiert: {len(closes)} Tage")
        return closes
    except Exception as e:
        log.warning(f"SPY-Fetch fehlgeschlagen: {e}")
        return None


def _spy_return_pct(closes: dict, start_dt, end_dt) -> float | None:
    """Berechnet SPY-Rendite in % zwischen zwei Daten.

    Findet den naechstgelegenen Handelstag falls das exakte Datum
    kein Boersentag war (Wochenende, Feiertag).
    """
    if not closes:
        return None
    sorted_dates = sorted(closes.keys())
    if not sorted_dates:
        return None

    def _find_close_on_or_before(target_date):
        # Binaere Suche waere overkill — wir haben max ~1300 Tage
        candidate = None
        for d in sorted_dates:
            if d <= target_date:
                candidate = d
            else:
                break
        return candidate

    start_d = start_dt.date() if hasattr(start_dt, "date") else start_dt
    end_d = end_dt.date() if hasattr(end_dt, "date") else end_dt

    start_key = _find_close_on_or_before(start_d)
    end_key = _find_close_on_or_before(end_d)

    if start_key is None or end_key is None:
        return None
    if start_key == end_key:
        return 0.0

    start_price = closes[start_key]
    end_price = closes[end_key]
    if start_price <= 0:
        return None
    return ((end_price - start_price) / start_price) * 100


@app.get("/api/benchmark")
async def api_benchmark(user=Depends(require_auth)):
    """Liefert SPY-Returns ueber dieselben Zeitfenster wie /api/pnl-periods.

    Das Frontend berechnet Alpha (portfolio_pct - spy_pct) selbst, um
    Code-Duplikation zu vermeiden.
    """
    from datetime import datetime, timedelta
    try:
        closes = _fetch_spy_closes(years=5)
        if not closes:
            return {"error": "SPY-Daten nicht verfuegbar", "benchmark": "SPY", "periods": []}

        now = datetime.now()
        windows = [
            ("1d",   "Heute",        now - timedelta(days=1)),
            ("7d",   "7 Tage",       now - timedelta(days=7)),
            ("30d",  "30 Tage",      now - timedelta(days=30)),
            ("90d",  "3 Monate",     now - timedelta(days=90)),
            ("180d", "6 Monate",     now - timedelta(days=180)),
            ("365d", "1 Jahr",       now - timedelta(days=365)),
            ("ytd",  "Jahresanfang", datetime(now.year, 1, 1)),
            ("all",  "Gesamt",       now - timedelta(days=365 * 5)),  # max 5y SPY-Cache
        ]

        periods = []
        for key, label, start_dt in windows:
            pct = _spy_return_pct(closes, start_dt, now)
            periods.append({
                "key": key,
                "label": label,
                "spy_pct": round(pct, 2) if pct is not None else None,
            })

        # Jueengster Datenpunkt fuer Stale-Check
        latest = max(closes.keys()) if closes else None
        return {
            "benchmark": "SPY",
            "benchmark_name": "S&P 500 ETF",
            "periods": periods,
            "data_points": len(closes),
            "latest_close_date": latest.isoformat() if latest else None,
        }
    except Exception as e:
        log.error(f"Benchmark Error: {e}")
        return {"error": str(e), "benchmark": "SPY", "periods": []}


@app.get("/api/pnl-periods")
async def api_pnl_periods(user=Depends(require_auth)):
    """Aggregierter Gewinn/Verlust ueber mehrere Zeitfenster.

    Hybrid-Modell:
      - Fenster <= 7 Tage: realisierter PnL aus Trade-History + aktueller
        unrealisierter PnL aus offenen Positionen (zeigt was du gerade
        wirklich verdienst, inkl. laufender Trades)
      - Fenster > 7 Tage: nur realisierter PnL (sauber, deterministisch,
        wie ein Broker-Statement)

    Prozent-Basis: Equity am Anfang des Fensters
        = current_total_value - total_pnl_in_window
    """
    from datetime import datetime, timedelta
    try:
        history = read_json_safe("trade_history.json") or []

        # Aktueller Portfolio-Snapshot fuer Hybrid-Berechnung + % Basis
        current_value = 0.0
        current_unrealized = 0.0
        try:
            config = load_config()
            client = EtoroClient(config)
            if client.configured:
                portfolio = client.get_portfolio() or {}
                credit = portfolio.get("credit", 0) or 0
                positions = portfolio.get("positions", []) or []
                current_unrealized = portfolio.get("unrealizedPnL", 0) or 0
                parsed = [EtoroClient.parse_position(p) for p in positions]
                total_invested = sum(p["invested"] for p in parsed)
                current_value = credit + total_invested + current_unrealized
        except Exception as e:
            log.warning(f"PnL-Periods: Portfolio-Fetch fehlgeschlagen: {e}")

        now = datetime.now()
        windows = [
            ("1d",   "Heute",       now - timedelta(days=1),    True),
            ("7d",   "7 Tage",      now - timedelta(days=7),    True),
            ("30d",  "30 Tage",     now - timedelta(days=30),   False),
            ("90d",  "3 Monate",    now - timedelta(days=90),   False),
            ("180d", "6 Monate",    now - timedelta(days=180),  False),
            ("365d", "1 Jahr",      now - timedelta(days=365),  False),
            ("ytd",  "Jahresanfang", datetime(now.year, 1, 1),  False),
            ("all",  "Gesamt",      datetime(1970, 1, 1),       False),
        ]

        # Realisierten PnL pro Fenster aufsummieren
        realized = {key: 0.0 for key, *_ in windows}
        closes = 0
        for trade in history:
            action = str(trade.get("action", ""))
            if not action.endswith("CLOSE") and "CLOSE" not in action:
                continue
            pnl = trade.get("pnl_usd")
            if pnl is None:
                continue
            ts_str = trade.get("timestamp", "")
            if not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if ts.tzinfo is not None:
                    ts = ts.replace(tzinfo=None)
            except Exception:
                continue
            closes += 1
            for key, _label, start_dt, _hybrid in windows:
                if ts >= start_dt:
                    realized[key] += float(pnl)

        # Periods zusammenbauen mit Hybrid-Logik
        periods = []
        for key, label, _start_dt, hybrid in windows:
            r_pnl = realized[key]
            if hybrid:
                total_pnl = r_pnl + current_unrealized
                mode = "hybrid"
            else:
                total_pnl = r_pnl
                mode = "realized"

            # Equity am Anfang des Fensters fuer % Basis
            start_equity = current_value - total_pnl
            if start_equity > 0:
                pct = (total_pnl / start_equity) * 100
            else:
                pct = 0.0

            periods.append({
                "key": key,
                "label": label,
                "pnl_usd": round(total_pnl, 2),
                "pnl_pct": round(pct, 2),
                "realized_pnl": round(r_pnl, 2),
                "unrealized_pnl": round(current_unrealized, 2) if hybrid else 0,
                "mode": mode,
            })

        return {
            "periods": periods,
            "current_total_value": round(current_value, 2),
            "current_unrealized": round(current_unrealized, 2),
            "total_closes_counted": closes,
        }
    except Exception as e:
        log.error(f"PnL-Periods Error: {e}")
        return {"error": str(e)}


@app.get("/api/trades")
async def api_trades(limit: int = 50, offset: int = 0, user=Depends(require_auth)):
    """Trade-Historie (paginiert)."""
    history = read_json_safe("trade_history.json") or []
    # Neueste zuerst
    history.reverse()
    total = len(history)
    page = history[offset:offset + limit]
    return {"total": total, "offset": offset, "limit": limit, "trades": page}


@app.get("/api/brain")
async def api_brain(user=Depends(require_auth)):
    """Brain State: Scores, Regime, Regeln."""
    brain = read_json_safe("brain_state.json")
    if not brain:
        return {"error": "Brain State nicht verfuegbar"}
    return {
        "total_runs": brain.get("total_runs", 0),
        "market_regime": brain.get("market_regime", "unknown"),
        "win_rate": brain.get("win_rate", 0),
        "sharpe_estimate": brain.get("sharpe_estimate", 0),
        "instrument_scores": brain.get("instrument_scores", {}),
        "learned_rules": brain.get("learned_rules", [])[-10:],
        "best_performers": brain.get("best_performers", []),
        "worst_performers": brain.get("worst_performers", []),
        "optimization_log": brain.get("optimization_log", [])[-10:],
        "last_snapshot": brain.get("performance_snapshots", [{}])[-1] if brain.get("performance_snapshots") else None,
    }


@app.get("/api/config")
async def api_config(user=Depends(require_auth)):
    """Aktuelle Strategie-Parameter (ohne Secrets)."""
    config = load_config()
    dt = config.get("demo_trading", {})
    return {
        "strategy": dt.get("strategy", "unknown"),
        "stop_loss_pct": dt.get("stop_loss_pct", -10),
        "take_profit_pct": dt.get("take_profit_pct", 25),
        "rebalance_threshold_pct": dt.get("rebalance_threshold_pct", 5),
        "default_leverage": dt.get("default_leverage", 1),
        "max_single_trade_usd": dt.get("max_single_trade_usd", 5000),
        "portfolio_targets": dt.get("portfolio_targets", {}),
    }


@app.put("/api/config/strategy")
async def api_update_strategy(update: StrategyUpdate, user=Depends(require_auth)):
    """Strategie-Parameter aendern."""
    config = load_config()
    dt = config.setdefault("demo_trading", {})

    changes = []
    if update.strategy is not None:
        old = dt.get("strategy")
        dt["strategy"] = update.strategy
        if old != update.strategy:
            changes.append(f"Strategie: {old} -> {update.strategy}")

    if update.stop_loss_pct is not None:
        old = dt.get("stop_loss_pct")
        dt["stop_loss_pct"] = update.stop_loss_pct
        if old != update.stop_loss_pct:
            changes.append(f"SL: {old} -> {update.stop_loss_pct}")

    if update.take_profit_pct is not None:
        old = dt.get("take_profit_pct")
        dt["take_profit_pct"] = update.take_profit_pct
        if old != update.take_profit_pct:
            changes.append(f"TP: {old} -> {update.take_profit_pct}")

    if update.rebalance_threshold_pct is not None:
        dt["rebalance_threshold_pct"] = update.rebalance_threshold_pct

    if update.default_leverage is not None:
        dt["default_leverage"] = update.default_leverage

    if update.max_single_trade_usd is not None:
        old = dt.get("max_single_trade_usd")
        dt["max_single_trade_usd"] = update.max_single_trade_usd
        if old != update.max_single_trade_usd:
            changes.append(f"Max Trade: {old} -> {update.max_single_trade_usd}")

    if update.portfolio_targets is not None:
        # Validiere: Summe muss 100% sein
        total = sum(t.get("allocation_pct", 0) for t in update.portfolio_targets.values())
        if abs(total - 100) > 1:
            raise HTTPException(400, f"Allokation muss 100% ergeben (aktuell: {total}%)")
        dt["portfolio_targets"] = update.portfolio_targets

    save_config(config)

    # Audit log
    try:
        from web.security import log_audit
        await log_audit(user, "CONFIG_CHANGE", ", ".join(changes) if changes else "strategy updated")
    except Exception:
        pass

    return {"status": "ok", "changes": changes}


@app.get("/api/trading/status")
async def api_trading_status(user=Depends(require_auth)):
    """Trading-Status: laeuft es? Letzter Lauf?"""
    return get_trading_status()


@app.post("/api/trading/start")
async def api_trading_start(user=Depends(require_auth)):
    """Trading aktivieren."""
    set_trading_enabled(True)
    try:
        from web.security import log_audit
        await log_audit(user, "TRADING_START", "Trading aktiviert via Dashboard")
    except Exception:
        pass
    return {"status": "ok", "enabled": True}


@app.post("/api/trading/stop")
async def api_trading_stop(user=Depends(require_auth)):
    """Trading deaktivieren."""
    set_trading_enabled(False)
    try:
        from web.security import log_audit
        await log_audit(user, "TRADING_STOP", "Trading deaktiviert via Dashboard")
    except Exception:
        pass
    return {"status": "ok", "enabled": False}


@app.get("/api/logs")
async def api_logs(lines: int = 100, user=Depends(require_auth)):
    """Letzte N Zeilen des Trading-Logs."""
    return {"lines": read_log_tail(lines)}


@app.get("/api/weekly-report")
async def api_weekly_report(user=Depends(require_auth)):
    """Letzter Weekly Report (oder neu generieren)."""
    report = read_json_safe("weekly_report.json")
    if report:
        return report
    # Noch kein Report vorhanden - on-demand generieren
    try:
        from app.weekly_report import generate_weekly_report
        return generate_weekly_report()
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/weekly-report/send")
async def api_send_weekly_report(user=Depends(require_auth)):
    """Weekly Report manuell ausloesen und senden."""
    try:
        from app.weekly_report import send_weekly_report
        report = send_weekly_report()
        return {"status": "ok", "trades_this_week": report.get("weekly_trades", {}).get("total_trades", 0)}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/discovery")
async def api_discovery(user=Depends(require_auth)):
    """Letzte Asset Discovery Ergebnisse."""
    result = read_json_safe("discovery_result.json")
    if result:
        return result
    return {"new_found": 0, "evaluated": 0, "added": 0, "message": "Noch keine Discovery gelaufen"}


@app.post("/api/discovery/run")
async def api_run_discovery(user=Depends(require_auth)):
    """Asset Discovery manuell ausloesen."""
    try:
        from app.asset_discovery import run_weekly_discovery
        result = run_weekly_discovery()
        return {"status": "ok", **result}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/weekly-report/pdf")
async def api_weekly_report_pdf(user=Depends(require_auth)):
    """Letzten PDF-Report herunterladen oder on-demand generieren."""
    from pathlib import Path
    import glob as glob_mod

    bericht_dir = Path(__file__).parent.parent / "Bericht"
    bericht_dir.mkdir(parents=True, exist_ok=True)

    # Neuestes PDF finden
    pdfs = sorted(bericht_dir.glob("InvestPilot_Report_*.pdf"), reverse=True)
    if pdfs:
        return FileResponse(
            str(pdfs[0]),
            media_type="application/pdf",
            filename=pdfs[0].name,
        )

    # Kein PDF vorhanden - on-demand generieren
    try:
        from app.weekly_report import generate_weekly_report
        from app.report_pdf import generate_pdf
        report = generate_weekly_report()
        pdf_path = generate_pdf(report, output_dir=bericht_dir)
        return FileResponse(
            str(pdf_path),
            media_type="application/pdf",
            filename=pdf_path.name,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PDF-Erstellung fehlgeschlagen: {e}")


# ============================================================
# KILL SWITCH & RISK ENDPOINTS
# ============================================================

@app.post("/api/trading/killswitch")
async def api_killswitch(user=Depends(require_auth)):
    """EMERGENCY: Alle Positionen sofort schliessen, Trading deaktivieren."""
    try:
        from app.risk_manager import emergency_close_all
        from app.etoro_client import EtoroClient
        from app.config_manager import load_config

        config = load_config()
        client = EtoroClient(config)
        result = emergency_close_all(client, f"Dashboard Kill Switch von {user}")

        try:
            from web.security import log_audit
            await log_audit(user, "KILL_SWITCH", f"Emergency Close: {result}")
        except Exception:
            pass

        try:
            from app.alerts import alert_emergency
            alert_emergency(f"Dashboard Kill Switch von {user}", result.get("closed", 0))
        except Exception:
            pass

        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/risk")
async def api_risk(user=Depends(require_auth)):
    """Aktuelle Risiko-Zusammenfassung."""
    try:
        from app.risk_manager import get_risk_summary, calculate_exposure, check_margin_safety
        from app.etoro_client import EtoroClient
        from app.config_manager import load_config

        summary = get_risk_summary()

        config = load_config()
        client = EtoroClient(config)
        if client.configured:
            portfolio = client.get_portfolio()
            if portfolio:
                from app.etoro_client import EtoroClient as EC
                positions = [EC.parse_position(p) for p in portfolio.get("positions", [])]
                credit = portfolio.get("credit", 0)
                total = credit + sum(p["invested"] for p in positions)

                exposure = calculate_exposure(positions)
                margin_ok, margin_reason, exposure_detail = check_margin_safety(total, positions, config)

                summary["exposure"] = exposure_detail
                summary["margin_ok"] = margin_ok
                summary["margin_reason"] = margin_reason

        return summary
    except ImportError:
        return {"error": "Risk Manager nicht verfuegbar"}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/exposure")
async def api_exposure(user=Depends(require_auth)):
    """Effektive Marktexposure (Kapital x Hebel) je Asset-Klasse."""
    try:
        from app.risk_manager import calculate_exposure
        from app.leverage_manager import get_leverage_summary
        from app.etoro_client import EtoroClient
        from app.config_manager import load_config

        config = load_config()
        client = EtoroClient(config)
        if not client.configured:
            return {"error": "eToro nicht konfiguriert"}

        portfolio = client.get_portfolio()
        if not portfolio:
            return {"error": "Portfolio nicht verfuegbar"}

        from app.etoro_client import EtoroClient as EC
        positions = [EC.parse_position(p) for p in portfolio.get("positions", [])]

        exposure = calculate_exposure(positions)
        leverage = get_leverage_summary(positions)

        return {
            "exposure": exposure,
            "leverage": leverage,
            "portfolio_value": portfolio.get("credit", 0) + sum(p["invested"] for p in positions),
        }
    except ImportError:
        return {"error": "Module nicht verfuegbar"}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/market-context")
async def api_market_context(user=Depends(require_auth)):
    """Aktueller Marktkontext (VIX, Fear&Greed, Makro-Events)."""
    try:
        from app.market_context import get_current_context
        return get_current_context()
    except ImportError:
        return {"error": "Market Context nicht verfuegbar"}


@app.get("/api/execution-stats")
async def api_execution_stats(days: int = 7, user=Depends(require_auth)):
    """Execution-Qualitaets-Statistiken (Slippage, Latenz)."""
    try:
        from app.execution import get_execution_stats
        return get_execution_stats(days)
    except ImportError:
        return {"error": "Execution Tracker nicht verfuegbar"}


@app.get("/api/performance-breakdown")
async def api_performance_breakdown(days: int = 30, user=Depends(require_auth)):
    """Performance-Breakdown nach Zeit, Tag, Asset, Strategie."""
    try:
        from app.execution import get_performance_breakdown
        history = read_json_safe("trade_history.json") or []
        return get_performance_breakdown(history, days)
    except ImportError:
        return {"error": "Execution Tracker nicht verfuegbar"}


# ============================================================
# BACKTEST & ML ENDPOINTS
# ============================================================

@app.get("/api/backtest")
async def api_backtest(user=Depends(require_auth)):
    """Letzte Backtest-Ergebnisse."""
    result = read_json_safe("backtest_results.json")
    if result:
        return result
    return {"error": "Noch kein Backtest gelaufen. Starte einen ueber 'Run Backtest'."}


@app.post("/api/backtest/run")
async def api_run_backtest(user=Depends(require_auth)):
    """Backtest manuell starten (kann 1-3 Minuten dauern)."""
    try:
        from app.backtester import run_full_backtest
        result = run_full_backtest()
        if "error" in result:
            raise HTTPException(status_code=500, detail=result["error"])

        try:
            from web.security import log_audit
            metrics = result.get("full_period", {}).get("metrics", {})
            await log_audit(user, "BACKTEST_RUN",
                            f"Return={metrics.get('total_return_pct', 0):+.1f}%, "
                            f"Sharpe={metrics.get('sharpe_ratio', 0):.2f}")
        except Exception:
            pass

        return {"status": "ok", "results": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/ml-model")
async def api_ml_model(user=Depends(require_auth)):
    """ML-Modell Status und Feature Importances."""
    try:
        from app.ml_scorer import get_model_info, is_model_trained
        info = get_model_info()
        if info:
            info["is_active"] = is_model_trained()
            return info
        return {"error": "Kein ML-Modell trainiert", "is_active": False}
    except ImportError:
        return {"error": "ML Module nicht verfuegbar", "is_active": False}


@app.post("/api/ml-model/train")
async def api_train_ml(user=Depends(require_auth)):
    """ML-Modell neu trainieren."""
    try:
        from app.backtester import download_history
        from app.ml_scorer import train_model

        histories = download_history(years=5)
        if not histories:
            raise HTTPException(status_code=500, detail="Keine historischen Daten")

        result = train_model(histories)
        if "error" in result:
            raise HTTPException(status_code=500, detail=result["error"])

        return {"status": "ok", "model_info": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# OPTIMIZER
# ============================================================

@app.get("/api/optimizer")
async def api_optimizer(user=Depends(require_auth)):
    """Optimizer Status und History."""
    history = read_json_safe("optimization_history.json")
    if history:
        return history
    return {"runs": [], "last_run": None}


def _trigger_github_action_optimizer(username: str):
    """
    Triggert den Optimizer-Workflow auf GitHub Actions (v10).

    Vorteil ggue. dem alten Subprocess-Modell:
    - Laeuft auf einem 7-GB-RAM Runner statt Render Free Tier 512 MB
    - Container-OOMs koennen den Trading-Server nicht mehr toeten
    - Volles Grid-Search ohne Memory-Safeguard-Abbruch
    - Ergebnisse werden via isoliertem Gist-Push uebernommen (keine Race
      mit Trading-Server-Updates)

    ENV:
        GITHUB_TOKEN              PAT mit gist+actions:write scope (Pflicht)
        GITHUB_REPO               "owner/repo" (optional, default carlosbaumann754-svg/investpilot)
        OPTIMIZER_WORKFLOW_FILE   Workflow-Filename (optional, default optimizer.yml)
        OPTIMIZER_WORKFLOW_REF    Branch/Ref (optional, default master)
    """
    from datetime import datetime
    from app.config_manager import save_json

    initial_status = {
        "state": "running",
        "started_at": datetime.now().isoformat(),
        "finished_at": None,
        "triggered_by": username,
        "action": None,
        "error": None,
        "mode": "github-action-dispatching",
    }
    try:
        save_json("optimizer_status.json", initial_status)
    except Exception:
        pass

    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        log.error("Optimizer-Trigger: GITHUB_TOKEN fehlt")
        initial_status["state"] = "error"
        initial_status["error"] = "GITHUB_TOKEN fehlt — Workflow nicht ausloesbar"
        initial_status["finished_at"] = datetime.now().isoformat()
        try:
            save_json("optimizer_status.json", initial_status)
        except Exception:
            pass
        return

    repo = os.environ.get("GITHUB_REPO", "carlosbaumann754-svg/investpilot")
    workflow_file = os.environ.get("OPTIMIZER_WORKFLOW_FILE", "optimizer.yml")
    ref = os.environ.get("OPTIMIZER_WORKFLOW_REF", "master")
    url = f"https://api.github.com/repos/{repo}/actions/workflows/{workflow_file}/dispatches"

    try:
        import requests
        resp = requests.post(
            url,
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github.v3+json",
            },
            json={
                "ref": ref,
                "inputs": {"triggered_by": username},
            },
            timeout=15,
        )
        if resp.status_code in (201, 204):
            log.info(f"Optimizer-Workflow getriggert (repo={repo}, ref={ref})")
            initial_status["mode"] = "github-action-running"
            initial_status["action"] = "dispatched"
        else:
            log.error(f"Workflow-Dispatch HTTP {resp.status_code}: {resp.text[:200]}")
            initial_status["state"] = "error"
            initial_status["error"] = (
                f"workflow_dispatch HTTP {resp.status_code}: {resp.text[:160]}"
            )
            initial_status["finished_at"] = datetime.now().isoformat()
    except Exception as e:
        log.exception("Workflow-Dispatch fehlgeschlagen")
        initial_status["state"] = "error"
        initial_status["error"] = f"dispatch: {type(e).__name__}: {e}"
        initial_status["finished_at"] = datetime.now().isoformat()

    try:
        save_json("optimizer_status.json", initial_status)
    except Exception:
        pass


@app.post("/api/optimizer/run")
async def api_run_optimizer(background_tasks: BackgroundTasks, user=Depends(require_auth)):
    """Weekly Optimization im Hintergrund starten (non-blocking, vermeidet Render 100s Proxy-Timeout)."""
    try:
        from datetime import datetime
        from app.config_manager import load_json, save_json

        # Abbruch wenn bereits ein Lauf aktiv ist — aber Stale-Lock-Recovery:
        # Wenn letzter Lauf > 60 Min als "running" markiert ist, war das vermutlich
        # ein Prozess-Kill (OOM, Render-Redeploy, Crash). Markiere als error und
        # erlaube neuen Lauf.
        STALE_LOCK_MINUTES = 60
        status = load_json("optimizer_status.json") or {}
        if status.get("state") == "running":
            started = status.get("started_at")
            is_stale = False
            if started:
                try:
                    started_dt = datetime.fromisoformat(started)
                    age_min = (datetime.now() - started_dt).total_seconds() / 60
                    if age_min > STALE_LOCK_MINUTES:
                        is_stale = True
                        log.warning(
                            f"Stale Optimizer-Lock erkannt ({age_min:.0f} Min alt) "
                            f"— vermutlich Prozess-Kill. Reset auf error."
                        )
                        status["state"] = "error"
                        status["error"] = (
                            f"Prozess abgebrochen (Lock stale nach {age_min:.0f} Min)"
                        )
                        status["finished_at"] = datetime.now().isoformat()
                        save_json("optimizer_status.json", status)
                except Exception:
                    pass

            if not is_stale:
                return {
                    "status": "already_running",
                    "message": f"Optimizer laeuft bereits seit {started}",
                    "started_at": started,
                }

        background_tasks.add_task(_trigger_github_action_optimizer, user)

        try:
            from web.security import log_audit
            await log_audit(user, "OPTIMIZER_RUN_STARTED", "Background task scheduled")
        except Exception:
            pass

        return {
            "status": "started",
            "message": "Optimizer laeuft im Hintergrund. Pruefe /api/optimizer/status fuer Fortschritt.",
            "started_at": datetime.now().isoformat(),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/optimizer/status")
async def api_optimizer_status(user=Depends(require_auth)):
    """Status des letzten/laufenden Optimizer-Background-Laufs."""
    from app.config_manager import load_json
    status = load_json("optimizer_status.json")
    if not status:
        return {"state": "idle", "message": "Noch kein Optimizer-Lauf gestartet"}
    return status


@app.post("/api/optimizer/rollback")
async def api_rollback(user=Depends(require_auth)):
    """Letzte Optimierung rueckgaengig machen."""
    try:
        from app.optimizer import rollback_optimization
        success, msg = rollback_optimization()

        try:
            from web.security import log_audit
            await log_audit(user, "OPTIMIZER_ROLLBACK", msg)
        except Exception:
            pass

        if success:
            return {"status": "ok", "message": msg}
        raise HTTPException(status_code=400, detail=msg)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/force-backup")
async def api_admin_force_backup(user=Depends(require_auth)):
    """Triggert sofort einen Cloud-Backup (schiebt lokalen Stand als Gist HEAD)."""
    try:
        from app.persistence import backup_to_cloud
        ok = backup_to_cloud()
        try:
            from web.security import log_audit
            await log_audit(user, "ADMIN_FORCE_BACKUP", f"success={ok}")
        except Exception:
            pass
        return {"status": "ok" if ok else "failed", "success": ok}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# ADMIN: GIST INSPECT / FORCE RESTORE (Emergency Recovery)
# ============================================================

def _gist_inspect_raw():
    """Lade Gist-Inhalt und gib rohes dict der Dateien zurueck."""
    import json
    from app.persistence import (_find_backup_gist, _get_token, _headers,
                                  GITHUB_API, _fetch_gist_file_content)
    import requests

    token = _get_token()
    if not token:
        raise HTTPException(status_code=500, detail="GITHUB_TOKEN nicht gesetzt")

    gist_id = _find_backup_gist(token)
    if not gist_id:
        raise HTTPException(status_code=404, detail="Kein Backup-Gist gefunden")

    resp = requests.get(
        f"{GITHUB_API}/gists/{gist_id}",
        headers=_headers(token),
        timeout=15,
    )
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Gist-Fetch fehlgeschlagen: HTTP {resp.status_code}",
        )
    return gist_id, resp.json()


@app.get("/api/admin/gist-inspect")
async def api_admin_gist_inspect(user=Depends(require_auth)):
    """
    Zeige Metadaten des GitHub-Gist-Backups ohne etwas zu schreiben.
    Fuer Notfall-Diagnose (z.B. Brain-Reset nach OOM).
    """
    import json
    from app.config_manager import load_json
    from app.persistence import _fetch_gist_file_content, _get_token

    gist_id, gist_data = _gist_inspect_raw()
    files = gist_data.get("files", {})
    token = _get_token()

    out = {
        "gist_id": gist_id[:8] + "...",
        "updated_at": gist_data.get("updated_at"),
        "files": {},
        "local": {},
    }

    # Gist brain_state — raw_url fuer truncated files
    brain_file = files.get("brain_state.json")
    if brain_file:
        meta_row = {
            "size": brain_file.get("size"),
            "truncated": brain_file.get("truncated", False),
            "raw_url_present": bool(brain_file.get("raw_url")),
            "content_len_in_api": len(brain_file.get("content", "") or ""),
        }
        try:
            content = _fetch_gist_file_content(brain_file, token) or "{}"
            meta_row["fetched_content_len"] = len(content)
            brain = json.loads(content)
            meta_row.update({
                "total_runs": brain.get("total_runs"),
                "regime": brain.get("market_regime"),
                "win_rate": brain.get("win_rate"),
                "sharpe": brain.get("sharpe_estimate"),
                "instruments_learned": len(brain.get("instrument_scores", {})),
                "learned_rules": len(brain.get("learned_rules", [])),
                "snapshots": len(brain.get("performance_snapshots", [])),
            })
        except Exception as e:
            meta_row["error"] = str(e)
        out["files"]["brain_state.json"] = meta_row

    # Meta
    meta_file = files.get("_backup_meta.json")
    if meta_file:
        try:
            out["backup_meta"] = json.loads(meta_file.get("content", "{}"))
        except Exception:
            pass

    # Local brain_state fuer Vergleich
    local_brain = load_json("brain_state.json") or {}
    out["local"]["brain_state.json"] = {
        "total_runs": local_brain.get("total_runs"),
        "market_regime": local_brain.get("market_regime"),
        "win_rate": local_brain.get("win_rate"),
        "sharpe": local_brain.get("sharpe_estimate"),
        "instruments_learned": len(local_brain.get("instrument_scores", {})),
        "learned_rules": len(local_brain.get("learned_rules", [])),
        "snapshots": len(local_brain.get("performance_snapshots", [])),
    }

    # Liste aller Dateien im Gist
    out["all_files"] = sorted(files.keys())

    return out


@app.get("/api/admin/gist-history")
async def api_admin_gist_history(user=Depends(require_auth)):
    """
    Durchlaufe Gist-Revision-History und zeige brain_state.total_runs pro Revision.
    Hilft, eine alte gute Revision (vor Reset) zu finden.
    """
    import json
    from app.persistence import (_find_backup_gist, _get_token, _headers,
                                  GITHUB_API, _fetch_gist_file_content)
    import requests

    token = _get_token()
    if not token:
        raise HTTPException(status_code=500, detail="GITHUB_TOKEN nicht gesetzt")
    gist_id = _find_backup_gist(token)
    if not gist_id:
        raise HTTPException(status_code=404, detail="Kein Backup-Gist gefunden")

    # Aktuellen Gist + History-SHAs laden
    resp = requests.get(f"{GITHUB_API}/gists/{gist_id}", headers=_headers(token), timeout=15)
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Gist-Fetch {resp.status_code}")
    history = resp.json().get("history", [])

    results = []
    # Nur die letzten 30 Revisionen pruefen (Rate-Limit)
    for entry in history[:30]:
        sha = entry.get("version")
        committed_at = entry.get("committed_at")
        row = {"sha": sha, "sha_short": sha[:10] if sha else None,
               "committed_at": committed_at,
               "total_runs": None, "regime": None, "error": None}
        try:
            r = requests.get(
                f"{GITHUB_API}/gists/{gist_id}/{sha}",
                headers=_headers(token),
                timeout=15,
            )
            if r.status_code != 200:
                row["error"] = f"HTTP {r.status_code}"
                results.append(row)
                continue
            files_dict = r.json().get("files", {})
            brain_file = files_dict.get("brain_state.json")
            if brain_file:
                content = _fetch_gist_file_content(brain_file, token)
                if content:
                    brain = json.loads(content)
                    row["total_runs"] = brain.get("total_runs")
                    row["regime"] = brain.get("market_regime")
                    row["win_rate"] = brain.get("win_rate")
                    row["instruments_learned"] = len(brain.get("instrument_scores", {}))
                    row["snapshots"] = len(brain.get("performance_snapshots", []))
                    row["learned_rules"] = len(brain.get("learned_rules", []))
        except Exception as e:
            row["error"] = str(e)
        results.append(row)

    return {
        "gist_id": gist_id[:8] + "...",
        "total_revisions": len(history),
        "revisions": results,
    }


@app.post("/api/admin/force-restore-brain-from-sha")
async def api_admin_force_restore_brain_from_sha(
    sha: str = "",
    confirm: str = "",
    files: str = "brain_state.json",
    user=Depends(require_auth),
):
    """
    NOTFALL: Stelle bestimmte Dateien aus einer SPEZIFISCHEN Gist-Revision wieder her.
    Params:
      sha=<gist_version_sha>  (Pflicht)
      confirm=YES_OVERWRITE   (Pflicht)
      files=comma,separated   (default: brain_state.json)
    """
    import json
    from app.config_manager import save_json, load_json
    from app.persistence import (_find_backup_gist, _get_token, _headers,
                                  GITHUB_API, _fetch_gist_file_content)
    import requests

    if confirm != "YES_OVERWRITE":
        raise HTTPException(status_code=400, detail="?confirm=YES_OVERWRITE noetig")
    if not sha:
        raise HTTPException(status_code=400, detail="?sha=<gist_version> noetig")

    token = _get_token()
    if not token:
        raise HTTPException(status_code=500, detail="GITHUB_TOKEN fehlt")
    gist_id = _find_backup_gist(token)
    if not gist_id:
        raise HTTPException(status_code=404, detail="Kein Backup-Gist")

    resp = requests.get(f"{GITHUB_API}/gists/{gist_id}/{sha}", headers=_headers(token), timeout=20)
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Revision-Fetch {resp.status_code}")

    gist_files = resp.json().get("files", {})
    target_files = [f.strip() for f in files.split(",") if f.strip()]

    restored, skipped, errors = [], [], []
    for filename in target_files:
        if filename not in gist_files:
            skipped.append({"file": filename, "reason": "nicht in Revision"})
            continue
        file_entry = gist_files[filename]
        content = _fetch_gist_file_content(file_entry, token)
        if not content:
            skipped.append({"file": filename, "reason": "leer"})
            continue
        try:
            data = json.loads(content)
            old_local = load_json(filename)
            save_json(filename, data)
            entry = {"file": filename}
            if isinstance(data, dict):
                entry["restored_total_runs"] = data.get("total_runs")
                entry["restored_regime"] = data.get("regime")
            if isinstance(old_local, dict):
                entry["old_total_runs"] = old_local.get("total_runs")
            restored.append(entry)
        except Exception as e:
            errors.append({"file": filename, "error": str(e)})

    try:
        from web.security import log_audit
        await log_audit(user, "ADMIN_RESTORE_FROM_SHA", f"sha={sha[:10]} restored={restored}")
    except Exception:
        pass

    return {"status": "ok", "sha": sha[:10], "restored": restored,
            "skipped": skipped, "errors": errors}


@app.post("/api/admin/force-restore-brain")
async def api_admin_force_restore_brain(
    confirm: str = "",
    files: str = "brain_state.json",
    user=Depends(require_auth),
):
    """
    NOTFALL: Erzwinge Restore einzelner Dateien aus dem GitHub-Gist,
    OHNE die is_empty-Pruefung. Ueberschreibt lokale Dateien.

    Params:
      confirm=YES_OVERWRITE  (Pflicht)
      files=comma,separated,list  (default: brain_state.json)
    """
    import json
    from app.config_manager import save_json, load_json

    if confirm != "YES_OVERWRITE":
        raise HTTPException(
            status_code=400,
            detail="Sicherheitsabfrage: ?confirm=YES_OVERWRITE noetig",
        )

    target_files = [f.strip() for f in files.split(",") if f.strip()]
    if not target_files:
        raise HTTPException(status_code=400, detail="Keine Dateien angegeben")

    gist_id, gist_data = _gist_inspect_raw()
    gist_files = gist_data.get("files", {})

    restored = []
    skipped = []
    errors = []

    for filename in target_files:
        if filename not in gist_files:
            skipped.append({"file": filename, "reason": "nicht im Gist"})
            continue
        content = gist_files[filename].get("content", "")
        if not content:
            skipped.append({"file": filename, "reason": "leerer Inhalt"})
            continue
        try:
            data = json.loads(content)
            # Sicherheits-Snapshot des alten lokalen Zustands
            old_local = load_json(filename)
            save_json(filename, data)
            entry = {"file": filename}
            if isinstance(data, dict):
                entry["restored_total_runs"] = data.get("total_runs")
                entry["restored_regime"] = data.get("regime")
            if isinstance(old_local, dict):
                entry["old_total_runs"] = old_local.get("total_runs")
            restored.append(entry)
        except Exception as e:
            errors.append({"file": filename, "error": str(e)})

    try:
        from web.security import log_audit
        await log_audit(
            user,
            "ADMIN_FORCE_RESTORE_BRAIN",
            f"restored={[r['file'] for r in restored]} skipped={skipped} errors={errors}",
        )
    except Exception:
        pass

    return {
        "status": "ok",
        "gist_id": gist_id[:8] + "...",
        "restored": restored,
        "skipped": skipped,
        "errors": errors,
    }


# ============================================================
# PDF REPORTS
# ============================================================

@app.get("/api/weekly-report/pdfs")
async def api_list_pdfs(user=Depends(require_auth)):
    """Liste aller verfuegbaren PDF-Reports."""
    from pathlib import Path

    bericht_dir = Path(__file__).parent.parent / "Bericht"
    if not bericht_dir.exists():
        return {"pdfs": []}

    pdfs = sorted(bericht_dir.glob("InvestPilot_Report_*.pdf"), reverse=True)
    return {
        "pdfs": [
            {"filename": p.name, "size_kb": p.stat().st_size // 1024}
            for p in pdfs[:20]
        ]
    }


# ============================================================
# V8: PERFORMANCE / EQUITY / CORRELATION ENDPOINTS
# ============================================================

@app.get("/api/equity-curve")
async def api_equity_curve(user=Depends(require_auth)):
    """Taegliche Equity-Curve basierend auf Trade-History."""
    try:
        from datetime import datetime as _dt, timedelta as _td
        from collections import defaultdict

        history = read_json_safe("trade_history.json") or []
        if not history:
            return {"dates": [], "equity": [], "drawdown_pct": []}

        # Portfolio-Startwert schaetzen (erster Trade Invest-Betrag x5 als Heuristik)
        first_invest = 0
        for t in history:
            if t.get("amount_usd"):
                first_invest = t["amount_usd"]
                break
        start_equity = max(first_invest * 5, 10000)

        # Taegliche PnL aggregieren
        daily_pnl = defaultdict(float)
        for t in history:
            ts = t.get("timestamp", "")[:10]
            if not ts:
                continue
            pnl = t.get("pnl_usd", 0) or 0
            daily_pnl[ts] += pnl

        if not daily_pnl:
            return {"dates": [], "equity": [], "drawdown_pct": []}

        sorted_dates = sorted(daily_pnl.keys())
        equity_values = []
        drawdown_values = []
        current_equity = start_equity
        peak_equity = start_equity

        for d in sorted_dates:
            current_equity += daily_pnl[d]
            equity_values.append(round(current_equity, 2))
            peak_equity = max(peak_equity, current_equity)
            dd_pct = ((current_equity - peak_equity) / peak_equity * 100) if peak_equity > 0 else 0
            drawdown_values.append(round(dd_pct, 2))

        return {
            "dates": sorted_dates,
            "equity": equity_values,
            "drawdown_pct": drawdown_values,
            "start_equity": start_equity,
        }
    except Exception as e:
        log.error(f"Equity Curve Error: {e}")
        return {"error": str(e)}


@app.get("/api/performance-metrics")
async def api_performance_metrics(user=Depends(require_auth)):
    """Berechne Performance-Metriken aus Trade-History."""
    try:
        import math
        history = read_json_safe("trade_history.json") or []

        # Nur abgeschlossene Trades mit PnL
        closed_trades = [t for t in history if t.get("pnl_pct") is not None
                         and t.get("action", "").endswith("CLOSE")]

        if not closed_trades:
            return {"error": "Keine abgeschlossenen Trades fuer Metriken"}

        pnls = [t["pnl_pct"] for t in closed_trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]

        win_rate = len(wins) / len(pnls) * 100 if pnls else 0
        avg_win = sum(wins) / len(wins) if wins else 0
        avg_loss = sum(losses) / len(losses) if losses else 0
        profit_factor = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else 999

        # Sharpe Ratio (annualisiert, angenommen 252 Handelstage)
        mean_return = sum(pnls) / len(pnls)
        std_return = (sum((p - mean_return) ** 2 for p in pnls) / len(pnls)) ** 0.5
        sharpe = (mean_return / std_return * math.sqrt(252)) if std_return > 0 else 0

        # Sortino Ratio (nur Downside-Volatilitaet)
        downside_returns = [p for p in pnls if p < 0]
        downside_std = (sum(p ** 2 for p in downside_returns) / max(len(downside_returns), 1)) ** 0.5
        sortino = (mean_return / downside_std * math.sqrt(252)) if downside_std > 0 else 0

        # Max Drawdown
        cumulative = 0
        peak = 0
        max_dd = 0
        for p in pnls:
            cumulative += p
            peak = max(peak, cumulative)
            dd = cumulative - peak
            max_dd = min(max_dd, dd)

        return {
            "total_trades": len(closed_trades),
            "win_rate_pct": round(win_rate, 1),
            "avg_win_pct": round(avg_win, 2),
            "avg_loss_pct": round(avg_loss, 2),
            "profit_factor": round(profit_factor, 2),
            "sharpe_ratio": round(sharpe, 2),
            "sortino_ratio": round(sortino, 2),
            "max_drawdown_pct": round(max_dd, 2),
            "total_return_pct": round(sum(pnls), 2),
            "mean_return_pct": round(mean_return, 3),
        }
    except Exception as e:
        log.error(f"Performance Metrics Error: {e}")
        return {"error": str(e)}


@app.get("/api/position-correlations")
async def api_position_correlations(user=Depends(require_auth)):
    """Sektor-Verteilung und Konzentrations-Score fuer offene Positionen."""
    try:
        from app.config_manager import load_config
        from app.etoro_client import EtoroClient

        config = load_config()
        client = EtoroClient(config)
        if not client.configured:
            return {"error": "eToro nicht konfiguriert"}

        portfolio = client.get_portfolio()
        if not portfolio:
            return {"error": "Portfolio nicht verfuegbar"}

        from app.etoro_client import EtoroClient as EC
        positions = [EC.parse_position(p) for p in portfolio.get("positions", [])]

        # Sektoren anreichern
        try:
            from app.market_scanner import ASSET_UNIVERSE
            for p in positions:
                for sym, info in ASSET_UNIVERSE.items():
                    if info["etoro_id"] == p["instrument_id"]:
                        p["sector"] = info.get("sector", "unknown")
                        p["asset_class"] = info.get("class", "unknown")
                        break
        except ImportError:
            pass

        # Sektor-Aggregation
        sectors = {}
        total_invested = sum(p["invested"] for p in positions) or 1
        for p in positions:
            sec = p.get("sector", "unknown") or "unknown"
            if sec not in sectors:
                sectors[sec] = {"count": 0, "invested": 0, "allocation_pct": 0}
            sectors[sec]["count"] += 1
            sectors[sec]["invested"] += p["invested"]

        for sec in sectors:
            sectors[sec]["allocation_pct"] = round(sectors[sec]["invested"] / total_invested * 100, 1)
            sectors[sec]["invested"] = round(sectors[sec]["invested"], 2)

        # Konzentrations-Score
        concentration_score = 0
        try:
            from app.risk_manager import get_portfolio_concentration_score
            concentration_score = get_portfolio_concentration_score(positions, config)
        except ImportError:
            pass

        return {
            "sectors": sectors,
            "concentration_score": concentration_score,
            "total_positions": len(positions),
            "total_invested": round(total_invested, 2),
        }
    except Exception as e:
        log.error(f"Position Correlations Error: {e}")
        return {"error": str(e)}


# ============================================================
# V5: REGIME, TRAILING SL, SECTORS
# ============================================================

@app.get("/api/regime")
async def api_regime(user=Depends(require_auth)):
    """Aktueller Regime-Status: VIX, Marktregime, Recovery Mode, Trading Halt."""
    try:
        from app.market_context import get_current_context
        from app.risk_manager import check_recovery_mode
        config = load_config()
        ctx = get_current_context()
        rf = config.get("regime_filter", {})

        vix = ctx.get("vix_level")
        vix_halt = rf.get("vix_halt_threshold", 35)

        brain = read_json_safe("brain_state.json") or {}

        recovery_active, recovery_restrictions = check_recovery_mode(config)

        return {
            "vix_level": vix,
            "vix_regime": ctx.get("vix_regime", "unknown"),
            "market_regime": brain.get("market_regime", "unknown"),
            "trading_halted": vix is not None and vix > vix_halt,
            "vix_halt_threshold": vix_halt,
            "recovery_mode": recovery_active,
            "recovery_restrictions": recovery_restrictions if recovery_active else None,
            "regime_filter_enabled": rf.get("enabled", False),
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/trailing-sl")
async def api_trailing_sl(user=Depends(require_auth)):
    """Aktive Trailing Stop-Loss Levels."""
    state = read_json_safe("trailing_sl_state.json")
    if not state:
        return {"positions": []}
    positions = []
    for pos_id, data in state.items():
        positions.append({
            "position_id": pos_id,
            "sl_level": data.get("sl_level"),
            "peak_price": data.get("peak_price"),
            "activated": data.get("activated", False),
        })
    return {"positions": positions}


@app.get("/api/sectors")
async def api_sectors(user=Depends(require_auth)):
    """Sektor-Staerke basierend auf letztem Scan."""
    scanner_state = read_json_safe("scanner_state.json")
    scan_results = scanner_state.get("last_results", []) if scanner_state else []
    if not scan_results:
        return {"sectors": {}, "message": "Kein Scan verfuegbar"}
    try:
        from app.market_scanner import calculate_sector_strength, ASSET_UNIVERSE
        strength = calculate_sector_strength(scan_results)
        # Enrich with count and allocation_pct for dashboard
        sector_count = {}
        for r in scan_results:
            sec = ASSET_UNIVERSE.get(r.get("symbol", ""), {}).get("sector")
            if sec:
                sector_count[sec] = sector_count.get(sec, 0) + 1
        total = sum(sector_count.values()) or 1
        sectors = {}
        for sec, avg_score in strength.items():
            cnt = sector_count.get(sec, 0)
            sectors[sec] = {
                "avg_score": round(avg_score, 1),
                "count": cnt,
                "allocation_pct": round(cnt / total * 100, 1),
            }
        return {"sectors": sectors}
    except Exception as e:
        return {"error": str(e)}


# ============================================================
# WATCHDOG / DIAGNOSTICS
# ============================================================

@app.get("/api/diagnostics")
async def api_diagnostics(user=Depends(require_auth)):
    """Bot-Gesundheitspruefung: Zyklen, Trade-Erfolg, Error-Patterns."""
    try:
        from app.watchdog import run_diagnostics

        brain = read_json_safe("brain_state.json") or {}
        trades = read_json_safe("trade_history.json") or []
        risk = read_json_safe("risk_state.json") or {}
        log_lines = read_log_tail(200)

        result = run_diagnostics(
            trade_history=trades,
            brain_state=brain,
            risk_state=risk,
            log_lines=log_lines,
        )
        return result
    except Exception as ex:
        log.error(f"Diagnostics Fehler: {ex}")
        return {"status": "error", "error": str(ex), "checks": {}, "issues": [str(ex)]}


@app.get("/api/diagnostics/alert")
async def api_diagnostics_alert():
    """Watchdog-Check mit Telegram-Alert bei Problemen (kein Auth - fuer cron-job.org)."""
    try:
        from app.watchdog import run_diagnostics, format_telegram_alert
        from app.alerts import send_alert

        brain = read_json_safe("brain_state.json") or {}
        trades = read_json_safe("trade_history.json") or []
        risk = read_json_safe("risk_state.json") or {}
        log_lines = read_log_tail(200)

        result = run_diagnostics(
            trade_history=trades,
            brain_state=brain,
            risk_state=risk,
            log_lines=log_lines,
        )

        # Nur bei Problemen Telegram senden
        if result["status"] in ("error", "warning"):
            config = load_config()
            msg = format_telegram_alert(result)
            try:
                send_alert(msg, level="WARNING" if result["status"] == "warning" else "ERROR",
                           config=config)
            except Exception as alert_err:
                log.warning(f"Diagnostics Alert senden fehlgeschlagen: {alert_err}")

        return {"status": result["status"], "issues_count": len(result["issues"])}
    except Exception as ex:
        log.error(f"Diagnostics Alert Fehler: {ex}")
        return {"status": "error", "error": str(ex)}


# ============================================================
# Q&A ASK
# ============================================================

class AskRequest(BaseModel):
    question: str


@app.post("/api/ask")
async def api_ask(req: AskRequest, user=Depends(require_auth)):
    """Beantworte Fragen zum Bot mit Claude API."""
    if not req.question or len(req.question.strip()) < 3:
        raise HTTPException(400, "Frage zu kurz")

    try:
        from app.ask import ask_question

        config = load_config()

        # Daten sammeln
        context_data = {
            "trade_history": read_json_safe("trade_history.json") or [],
            "decision_log": read_json_safe("decision_log.json") or [],
            "brain_state": read_json_safe("brain_state.json") or {},
            "risk_state": read_json_safe("risk_state.json") or {},
            "scanner_state": read_json_safe("scanner_state.json") or {},
        }

        # Portfolio live abfragen
        try:
            client = EtoroClient(config)
            credit = client.get_credit()
            positions = client.get_portfolio()
            parsed = [EtoroClient.parse_position(p) for p in positions]
            total_invested = sum(p["invested"] for p in parsed)
            unrealized = sum(p["pnl"] for p in parsed)
            context_data["portfolio"] = {
                "total_value": round(credit + total_invested + unrealized, 2),
                "credit": round(credit, 2),
                "invested": round(total_invested, 2),
                "unrealized_pnl": round(unrealized, 2),
                "num_positions": len(positions),
                "positions": parsed,
            }
        except Exception:
            context_data["portfolio"] = None

        result = ask_question(req.question, context_data, config)
        return result
    except Exception as ex:
        log.error(f"Ask Fehler: {ex}")
        return {"error": f"Fehler: {str(ex)}"}
