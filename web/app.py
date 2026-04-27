"""
InvestPilot - FastAPI Web Dashboard
REST API + Mobile-First Frontend fuer Trading-Steuerung.
"""

import os
import sys
import asyncio
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
from app.broker_base import get_broker
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

# Async-Lock um Read-Modify-Write Races auf config.json zu verhindern.
# save_config() schreibt zwar atomar, aber zwei concurrent Requests koennen
# beide die alte Version laden, eigene Aenderung mergen und zurueckspeichern —
# dabei geht eine der Aenderungen verloren.
_CONFIG_WRITE_LOCK = asyncio.Lock()

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
    max_single_trade_usd: Optional[float] = None  # Legacy, deprecated by v15 pct
    max_single_trade_pct_of_portfolio: Optional[float] = None
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
# ASSET META ENRICHMENT — Helper
# ============================================================
# Wird aus dem Scanner/Trader nur die instrument_id geloggt; das Frontend
# und der Ask-Tab brauchen aber symbol + name + class + sector. Statt
# an ~6 Schreib-Stellen die Daten zu ergaenzen, mappen wir sie zentral
# beim Lesen aus dem ASSET_UNIVERSE.

_ASSET_META_CACHE = {"dict": None}

def _asset_meta_dict():
    """Lazy-init reverse-lookup dict: instrument_id -> meta."""
    if _ASSET_META_CACHE["dict"] is None:
        try:
            from app.market_scanner import ASSET_UNIVERSE
            _ASSET_META_CACHE["dict"] = {
                info.get("etoro_id"): {
                    "symbol": sym,
                    "name": info.get("name", sym),
                    "asset_class": info.get("class"),
                    "sector": info.get("sector"),
                }
                for sym, info in ASSET_UNIVERSE.items()
                if info.get("etoro_id")
            }
        except Exception as e:
            log.warning(f"ASSET_META init failed: {e}")
            _ASSET_META_CACHE["dict"] = {}
    return _ASSET_META_CACHE["dict"]


def _ibkr_conid_to_etoro_id() -> dict:
    """Reverse-Lookup: IBKR conId -> etoro_id via data/ibkr_contract_cache.json.

    v36e: Erlaubt Anreicherung von Positionen die nur die IBKR-conId
    (=instrument_id im Snapshot) tragen, mit Symbol/Name aus ASSET_UNIVERSE.
    """
    try:
        from app.config_manager import load_json
        cache = load_json("ibkr_contract_cache.json") or {}
        # cache-Key ist der etoro_id als string, value enthält conId
        return {int(entry["conId"]): int(etoro_id)
                for etoro_id, entry in cache.items()
                if isinstance(entry, dict) and entry.get("conId")}
    except Exception:
        return {}


def enrich_with_asset_meta(items, id_key="instrument_id", only_missing=True):
    """Reichert eine Liste von Dicts um symbol/name/asset_class/sector an.

    Args:
        items: Liste von Dicts mit instrument_id (oder id_key)
        id_key: Name des ID-Felds (default: instrument_id)
        only_missing: True = nur anreichern wenn symbol fehlt/? ist. False = immer ueberschreiben.

    Returns:
        Die gleiche Liste (modifiziert in-place).
    """
    mapping = _asset_meta_dict()
    if not mapping:
        return items
    # v36e: zusaetzlicher conId -> etoro_id Lookup fuer IBKR-Positionen
    conid_to_etoro = _ibkr_conid_to_etoro_id()
    for t in items or []:
        if not isinstance(t, dict):
            continue
        if only_missing:
            cur = t.get("symbol")
            if cur and cur not in ("?", "unknown", ""):
                continue
        iid = t.get(id_key) or t.get("etoro_id")
        if iid is None:
            continue
        meta = mapping.get(iid)
        # IBKR-Fallback: wenn iid keine etoro_id ist, ueber conId-Cache uebersetzen
        if not meta and conid_to_etoro:
            etoro_id = conid_to_etoro.get(int(iid)) if str(iid).isdigit() else None
            if etoro_id is not None:
                meta = mapping.get(etoro_id)
        if meta:
            t.setdefault("symbol", meta["symbol"])
            t.setdefault("name", meta["name"])
            t.setdefault("asset_class", meta["asset_class"])
            t.setdefault("sector", meta["sector"])
    return items


# ============================================================
# API ENDPOINTS
# ============================================================

@app.get("/api/withdrawal/status")
async def api_withdrawal_status(user=Depends(require_auth)):
    """Status des aktiven Entnahme-Plans (Withdrawal Scheduler)."""
    try:
        from app.withdrawal_planner import get_status
        return get_status()
    except Exception as e:
        log.error(f"Withdrawal-Status: {e}", exc_info=True)
        return {"active": False, "error": f"{type(e).__name__}: {e}"}


@app.post("/api/withdrawal/plan")
async def api_withdrawal_plan(payload: dict, user=Depends(require_auth)):
    """Neuen Entnahme-Plan erstellen (ueberschreibt alten falls vorhanden).

    Payload: {"amount": float, "deadline": "YYYY-MM-DD", "strategy": "fifo", "notes": str}
    """
    try:
        from app.withdrawal_planner import create_plan
        plan = create_plan(
            target_amount_usd=float(payload.get("amount", 0)),
            deadline=str(payload.get("deadline", "")),
            strategy=str(payload.get("strategy", "fifo")),
            notes=str(payload.get("notes", "")),
        )
        return {"status": "ok", "plan": plan}
    except ValueError as e:
        return {"status": "error", "error": str(e)}
    except Exception as e:
        log.error(f"Withdrawal-Plan-Create: {e}", exc_info=True)
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


@app.delete("/api/withdrawal/plan")
async def api_withdrawal_cancel(user=Depends(require_auth)):
    """Aktiven Entnahme-Plan stornieren."""
    try:
        from app.withdrawal_planner import cancel_plan
        plan = cancel_plan()
        if plan is None:
            return {"status": "noop", "message": "Kein aktiver Plan vorhanden"}
        return {"status": "ok", "cancelled_plan": plan}
    except Exception as e:
        log.error(f"Withdrawal-Cancel: {e}", exc_info=True)
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


@app.get("/api/universe/suggestions")
async def api_universe_suggestions(user=Depends(require_auth)):
    """Auto-Disable / Re-Enable Vorschlaege vom Universe-Health-Watcher."""
    try:
        from app.universe_health_watcher import get_suggestions
        return get_suggestions()
    except Exception as e:
        log.error(f"Universe-Suggestions: {e}", exc_info=True)
        return {"error": f"{type(e).__name__}: {e}"}


@app.post("/api/universe/refresh-suggestions")
async def api_universe_refresh_suggestions(user=Depends(require_auth)):
    """Triggert Universe-Watcher manuell — counter-update + Vorschlaege.

    Wird normalerweise vom Trader/Scheduler nach jedem Universe-Health-Run
    automatisch aufgerufen. Manueller Trigger fuer Ad-hoc-Re-Evaluation.
    """
    try:
        from app.universe_health_watcher import update_counters
        result = update_counters()
        return {"status": "ok", "suggestions": result["suggestions"]}
    except Exception as e:
        log.error(f"Universe-Refresh: {e}", exc_info=True)
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


@app.post("/api/universe/disable/{symbol}")
async def api_universe_disable(symbol: str, user=Depends(require_auth)):
    """User bestaetigt einen Auto-Disable-Vorschlag fuer das Symbol."""
    try:
        from app.universe_health_watcher import confirm_disable
        return confirm_disable(symbol.upper())
    except Exception as e:
        log.error(f"Universe-Disable {symbol}: {e}", exc_info=True)
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


@app.post("/api/universe/enable/{symbol}")
async def api_universe_enable(symbol: str, user=Depends(require_auth)):
    """User bestaetigt einen Re-Enable-Vorschlag fuer das Symbol."""
    try:
        from app.universe_health_watcher import confirm_enable
        return confirm_enable(symbol.upper())
    except Exception as e:
        log.error(f"Universe-Enable {symbol}: {e}", exc_info=True)
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


@app.post("/api/universe/reset")
async def api_universe_reset(user=Depends(require_auth)):
    """Universe-Reset: leert disabled_symbols-Liste, damit beim naechsten
    Backtest alle Symbole wieder evaluiert werden.

    Use-Case: Die statisch disabled-Liste (21 Symbole seit v12-Rollout) wird
    nie automatisch ueberprueft. Dieser Endpoint erlaubt einen Ad-hoc
    Re-Check ohne Code-Change. Nach Reset:
    1. Naechster Backtest (Sonntag oder manuell) bewertet ALLE 71 Symbole
    2. Performante Symbole bleiben aktiv, schwache landen via Universe-Health
       wieder auf der Liste
    3. Backup der alten Liste in disabled_symbols_backup_<timestamp> falls Rollback noetig
    """
    try:
        config = load_config()
        old_disabled = list(config.get("disabled_symbols", []) or [])
        if not old_disabled:
            return {"status": "noop", "message": "disabled_symbols ist bereits leer"}
        # Backup mit Timestamp
        from datetime import datetime
        backup_key = f"disabled_symbols_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        config[backup_key] = old_disabled
        config["disabled_symbols"] = []
        save_config(config)
        log.info(f"Universe-Reset: {len(old_disabled)} disabled_symbols geleert. "
                 f"Backup unter '{backup_key}'.")
        return {
            "status": "ok",
            "cleared_count": len(old_disabled),
            "cleared_symbols": old_disabled,
            "backup_key": backup_key,
            "next_step": "Naechster Backtest (manuell oder Sonntag 06:00 UTC) "
                         "bewertet alle Symbole neu. Schwache Performer landen "
                         "via Universe-Health wieder auf der Liste.",
        }
    except Exception as e:
        log.error(f"Universe-Reset fehlgeschlagen: {e}", exc_info=True)
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


def _broker_status_sync():
    """Sync-Logic fuer api_broker_status — laeuft in eigenem Thread.

    ib_insync nutzt einen eigenen asyncio-Loop intern. Direkt aus einem
    FastAPI-async-Handler aufrufen kollidiert mit dem laufenden Loop ->
    Connect haengt / returnt None. Daher via asyncio.to_thread isolieren.
    """
    config = load_config()
    broker_name = (config.get("broker") or "etoro").lower()
    client = get_broker(config, readonly=True)
    connected = False
    account = None
    equity = None
    error = None
    if client.configured:
        try:
            eq = client.get_equity()
            if eq is not None:
                connected = True
                equity = float(eq)
                if broker_name == "ibkr":
                    try:
                        ib = client._get_ib()
                        accs = ib.managedAccounts()
                        account = accs[0] if accs else None
                    except Exception:
                        pass
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
        finally:
            try:
                if hasattr(client, "disconnect"):
                    client.disconnect()
            except Exception:
                pass
    mode = "paper"
    if broker_name == "etoro":
        env = (config.get("etoro", {}) or {}).get("environment", "demo")
        mode = "real" if env == "real" else "demo"
    elif broker_name == "ibkr":
        if account:
            mode = "real" if not account.startswith(("DU", "DUP")) else "paper"
    return {
        "broker": broker_name,
        "configured": bool(client.configured),
        "connected": connected,
        "account": account,
        "equity": equity,
        "mode": mode,
        "error": error,
    }


# v36 — Broker-Status Cache: Frontend-Dashboard pollt diesen Endpoint alle
# paar Sekunden. Ohne Cache erzeugt jede Anfrage eine neue IBKR-Connection
# mit random clientId — das thrasht den IB-Gateway und kollidiert mit dem
# Scheduler-Cycle (clientId=1). 60s-Cache reicht dem UI vollkommen.
_BROKER_STATUS_CACHE: dict = {"data": None, "ts": 0}
_BROKER_STATUS_TTL_SECONDS = 60


@app.get("/api/broker-status")
async def api_broker_status():
    """Liefert aktuellen Broker-Status (Name, Configured, Connected) ohne Auth.

    v36: 60s-Cache vor IBKR-Live-Call, damit Dashboard-Polling den
    Scheduler-Cycle nicht mit parallelen Connections stoert.
    """
    import asyncio, time
    now = time.time()
    cached = _BROKER_STATUS_CACHE.get("data")
    cached_ts = _BROKER_STATUS_CACHE.get("ts", 0)
    if cached is not None and (now - cached_ts) < _BROKER_STATUS_TTL_SECONDS:
        # Cache-Hit: aktuelle Daten ohne neuen IBKR-Call
        return {**cached, "_cached": True, "_age_s": int(now - cached_ts)}
    try:
        result = await asyncio.to_thread(_broker_status_sync)
        _BROKER_STATUS_CACHE["data"] = result
        _BROKER_STATUS_CACHE["ts"] = now
        return result
    except Exception as e:
        return {"broker": "?", "configured": False, "connected": False,
                "error": f"{type(e).__name__}: {e}"}


def _portfolio_from_brain_cache():
    """Lade Portfolio aus brain_state.performance_snapshots (last entry).

    Vermeidet IBKR-Live-Connect aus FastAPI-Handler (asyncio loop conflicts).
    Werte sind <5 Min alt (Bot-Cycle schreibt nach jedem Run).

    Returns dict im selben Format wie client.get_portfolio() oder None.
    """
    from app.config_manager import load_json
    brain = load_json("brain_state.json") or {}
    snaps = brain.get("performance_snapshots") or []
    if not snaps:
        return None
    last = snaps[-1]
    # Bot's snapshot speichert: total_value, cash, invested, positions etc.
    cash = float(last.get("cash") or last.get("credit") or 0)
    total = float(last.get("total_value") or last.get("portfolio_value") or 0)
    invested = float(last.get("invested") or 0)
    positions = last.get("positions", []) or []
    return {
        "credit": cash,
        "unrealizedPnL": float(last.get("unrealized_pnl") or 0),
        "positions": positions,
        "_total_value": total,
        "_invested": invested,
        "_source": f"brain_cache (snapshot {last.get('ts','?')})",
    }


@app.get("/api/portfolio")
async def api_portfolio(user=Depends(require_auth)):
    """Portfolio-Status — bei IBKR aus brain_state.cache (vermeidet Loop-Conflict).

    eToro: live via REST-API (loop-safe).
    IBKR: aus letztem Bot-Cycle-Snapshot in brain_state.json (max 5 Min alt).
    """
    try:
        config = load_config()
        broker_name = (config.get("broker") or "etoro").lower()
        client = get_broker(config, readonly=True)
        if not client.configured:
            return {"error": f"Broker '{broker_name}' nicht konfiguriert"}

        # IBKR -> brain-cache Pfad (asyncio-loop-safe)
        if broker_name == "ibkr":
            portfolio = _portfolio_from_brain_cache()
            if not portfolio:
                return {"error": "Portfolio noch nicht im brain_state — warte auf ersten Bot-Cycle"}
        else:
            portfolio = client.get_portfolio()
            if not portfolio:
                return {"error": "Portfolio nicht verfuegbar"}

        credit = portfolio.get("credit", 0)
        positions = portfolio.get("positions", [])
        unrealized_pnl = portfolio.get("unrealizedPnL", 0)

        parsed = [EtoroClient.parse_position(pos) for pos in positions]
        total_invested = sum(p["invested"] for p in parsed)

        # Symbol/Name aus ASSET_UNIVERSE anreichern (Dashboard-freundlich)
        enrich_with_asset_meta(parsed)

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
# EXIT-FORECAST — Wie nah ist jede offene Position an ihrem nächsten Trigger?
# ============================================================
# Zeigt für jede offene Position: Abstand (in %) zum nächstmöglichen Exit
# (SL, Trailing-SL, nächste TP-Tranche, Final-TP, Time-Stop).
#
# Warum: Nach der 0-Closed-Trades-Analyse (2026-04-15) war unklar ob exits
# "nah" oder "fern" sind. Dieser Endpoint macht das transparent.

EXIT_TRIGGER_PRIORITY = ["SL", "Trailing-SL", "TP-1", "TP-2", "TP-3", "TP-final", "Time-Stop"]


def _compute_exit_forecast(position: dict, config: dict, trailing_state: dict) -> dict:
    """Berechne für eine Position alle Trigger-Distanzen und den nächsten Exit.

    Args:
        position: Geparste Position (aus EtoroClient.parse_position)
        config: Volles Config-Dict
        trailing_state: trailing_sl_state.json Inhalt (persistierter SL-Level je pos_id)

    Returns:
        dict mit triggers-Liste + next_trigger
    """
    pnl_pct = position.get("pnl_pct") or 0
    pid = str(position.get("position_id", ""))
    current_price = position.get("current_price") or 0
    entry_price = position.get("entry_price") or 0
    open_time = position.get("open_time")

    # Config-Parameter — Live-Bot liest aus `demo_trading`, nicht `stocks`.
    # (Optimizer schreibt auch in demo_trading.*, siehe optimizer.py:181)
    dt_cfg = config.get("demo_trading", {})
    lev_cfg = config.get("leverage", {})
    ts_cfg = config.get("time_stop", {})

    sl_pct = dt_cfg.get("stop_loss_pct", -2.5)
    tp_final_pct = dt_cfg.get("take_profit_pct", 18)
    trail_enabled = lev_cfg.get("trailing_sl_enabled", True)
    trail_activation = lev_cfg.get("trailing_sl_activation_pct", 0.8)
    trail_pct = lev_cfg.get("trailing_sl_pct", 1.8)
    tp_tranches = lev_cfg.get("tp_tranches", [])
    ts_enabled = ts_cfg.get("enabled", True)
    ts_max_days = ts_cfg.get("max_days_stale", 10)
    ts_min_days = ts_cfg.get("min_days_open", 2)
    ts_pnl_threshold = ts_cfg.get("stale_pnl_threshold_pct", 0.5)

    # Age in days
    age_days = None
    if open_time:
        try:
            from datetime import datetime, timezone
            # Normalize ISO format (eToro liefert manchmal mit Z, manchmal mit +00:00)
            ts_clean = open_time.replace("Z", "+00:00") if isinstance(open_time, str) else None
            if ts_clean:
                dt = datetime.fromisoformat(ts_clean)
                now = datetime.now(timezone.utc)
                age_days = (now - dt).total_seconds() / 86400
        except Exception:
            age_days = None

    triggers = []

    # --- SL (hard, -2.5%) ---
    triggers.append({
        "type": "SL",
        "label": f"Stop-Loss ({sl_pct:+.1f}%)",
        "target_pct": sl_pct,
        "distance_pct": round(pnl_pct - sl_pct, 2),  # wie viel darf noch fallen
        "active": True,
        "direction": "down",
    })

    # --- Trailing-SL ---
    trail_active = trail_enabled and pnl_pct >= trail_activation
    trail_distance = None
    trail_sl_price = None
    if trail_active and pid in trailing_state:
        trail_sl_price = trailing_state[pid].get("sl_level")
        if trail_sl_price and current_price:
            # Distanz in % vom aktuellen Preis bis SL-Level
            trail_distance = round((current_price - trail_sl_price) / current_price * 100, 2)
    elif trail_active:
        # Fallback: wenn kein State gespeichert, worst-case 1.8%
        trail_distance = trail_pct
    triggers.append({
        "type": "Trailing-SL",
        "label": f"Trailing-SL (-{trail_pct:.1f}% vom Peak)",
        "target_pct": None,
        "distance_pct": trail_distance,
        "active": trail_active,
        "direction": "down",
        "sl_price": trail_sl_price,
        "activation_pct": trail_activation,
    })

    # --- TP-Tranchen (fortlaufend bis +18%) ---
    # Wir wissen nicht welche schon gefeuert haben — prüfen per PnL-Schwelle.
    # Wenn pnl_pct >= tp_target, gilt Tranche als "durchgelaufen" (sie hat
    # geschlossen oder wäre gerade am schliessen).
    for i, tr in enumerate(tp_tranches, start=1):
        target = tr.get("profit_target_pct", 0)
        already_hit = pnl_pct >= target
        triggers.append({
            "type": f"TP-{i}",
            "label": f"TP-{i} ({target:+.0f}%, {tr.get('pct_of_position', 0)}% schliessen)",
            "target_pct": target,
            "distance_pct": round(target - pnl_pct, 2) if not already_hit else 0,
            "active": not already_hit,
            "direction": "up",
        })

    # --- Final TP (+18%) ---
    triggers.append({
        "type": "TP-final",
        "label": f"Take-Profit ({tp_final_pct:+.0f}%)",
        "target_pct": tp_final_pct,
        "distance_pct": round(tp_final_pct - pnl_pct, 2),
        "active": pnl_pct < tp_final_pct,
        "direction": "up",
    })

    # --- Time-Stop ---
    ts_active = ts_enabled and age_days is not None and age_days >= ts_min_days
    ts_eligible_now = (
        ts_active
        and age_days >= ts_max_days
        and abs(pnl_pct) < ts_pnl_threshold
    )
    days_until_ts = None
    if ts_enabled and age_days is not None:
        days_until_ts = max(0, round(ts_max_days - age_days, 1))
    triggers.append({
        "type": "Time-Stop",
        "label": f"Time-Stop ({ts_max_days}d + |PnL|<{ts_pnl_threshold}%)",
        "target_pct": None,
        "distance_pct": None,  # zeitbasiert, nicht preis-basiert
        "active": ts_active,
        "days_until": days_until_ts,
        "eligible_now": ts_eligible_now,
        "in_pnl_band": abs(pnl_pct) < ts_pnl_threshold,
        "direction": "time",
    })

    # --- Nächsten Trigger bestimmen (kleinste positive distance_pct) ---
    candidates = [
        t for t in triggers
        if t.get("active")
        and t.get("distance_pct") is not None
        and t["distance_pct"] >= 0
    ]
    next_trigger = None
    if candidates:
        next_trigger = min(candidates, key=lambda t: t["distance_pct"])
        next_trigger = {
            "type": next_trigger["type"],
            "label": next_trigger["label"],
            "distance_pct": next_trigger["distance_pct"],
            "direction": next_trigger["direction"],
        }

    return {
        "position_id": position.get("position_id"),
        "instrument_id": position.get("instrument_id"),
        "pnl_pct": pnl_pct,
        "invested": position.get("invested"),
        "age_days": round(age_days, 2) if age_days is not None else None,
        "triggers": triggers,
        "next_trigger": next_trigger,
    }


@app.get("/api/exit-forecast")
async def api_exit_forecast(user=Depends(require_auth)):
    """Für jede offene Position: Abstand zum nächsten Exit-Trigger."""
    try:
        config = load_config()
        client = get_broker(config, readonly=True)
        if not client.configured:
            return {"error": "eToro nicht konfiguriert", "positions": []}

        portfolio = client.get_portfolio()
        if not portfolio:
            return {"error": "Portfolio nicht verfuegbar", "positions": []}

        parsed = [EtoroClient.parse_position(p) for p in portfolio.get("positions", [])]

        # Trailing-SL-State einmalig laden
        try:
            from app.leverage_manager import _load_trailing_state
            trailing_state = _load_trailing_state()
        except Exception as e:
            log.warning(f"Trailing-State laden fehlgeschlagen: {e}")
            trailing_state = {}

        forecasts = [_compute_exit_forecast(p, config, trailing_state) for p in parsed]

        # Symbol/Name anreichern fuer Dashboard-Anzeige
        enrich_with_asset_meta(forecasts)

        # Nach Dringlichkeit sortieren (kleinste distance_pct zuerst)
        def _sort_key(f):
            nt = f.get("next_trigger")
            return nt["distance_pct"] if nt else 999
        forecasts.sort(key=_sort_key)

        return {
            "count": len(forecasts),
            "positions": forecasts,
            "config_summary": {
                # Live-Bot liest aus demo_trading.* (siehe trader.py). Der
                # Optimizer schreibt auch dorthin. 'stocks' gibt's in der
                # Live-Config gar nicht — alte Fehlquelle fuer Diskrepanz.
                "sl_pct": config.get("demo_trading", {}).get("stop_loss_pct", -2.5),
                "tp_pct": config.get("demo_trading", {}).get("take_profit_pct", 18),
                "trail_activation": config.get("leverage", {}).get("trailing_sl_activation_pct", 0.8),
                "trail_pct": config.get("leverage", {}).get("trailing_sl_pct", 1.8),
                "tp_tranches": config.get("leverage", {}).get("tp_tranches", []),
                "time_stop": config.get("time_stop", {"max_days_stale": 10}),
            },
        }
    except Exception as e:
        log.error(f"Exit-Forecast API Error: {e}")
        return {"error": str(e), "positions": []}


# ============================================================
# BENCHMARK (Multi: SPY, QQQ, AGG + 60/40-Mix) — In-Memory Cache 1h TTL
# ============================================================
# Pro Symbol ein eigener Cache-Slot, damit ein Fail (z.B. AGG) nicht den
# Rest invalidiert. yfinance kann sporadisch 401/429 liefern.
_BENCHMARK_CACHE: dict = {}  # {symbol: {"data": {date: close}, "ts": float}}

# Symbole, die wir tracken. SPY = S&P 500 Tracker, QQQ = Nasdaq-100,
# AGG = US Aggregate Bond Index. 60/40 wird im Endpoint berechnet
# (0.6*SPY + 0.4*AGG) — klassisches Privat-Anleger-Portfolio.
BENCHMARK_SYMBOLS = ["SPY", "QQQ", "AGG"]


def _fetch_ticker_closes(symbol: str, years: int = 5):
    """Holt Tagesschlusskurse fuer ein Symbol via yfinance, 1h Cache.

    Returns:
        dict {date: close_price} oder None bei Fehler.
    """
    import time as _time
    now_ts = _time.time()
    cached = _BENCHMARK_CACHE.get(symbol)
    if cached and cached.get("data") and (now_ts - cached.get("ts", 0) < 3600):
        return cached["data"]
    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period=f"{years}y", interval="1d")
        if hist.empty:
            return None
        closes = {}
        for date_idx, row in hist.iterrows():
            d = date_idx.to_pydatetime().replace(tzinfo=None).date()
            closes[d] = float(row["Close"])
        _BENCHMARK_CACHE[symbol] = {"data": closes, "ts": now_ts}
        log.info(f"{symbol}-Cache aktualisiert: {len(closes)} Tage")
        return closes
    except Exception as e:
        log.warning(f"{symbol}-Fetch fehlgeschlagen: {e}")
        return None


def _fetch_spy_closes(years: int = 5):
    """Backwards-compat Wrapper — Equity-Snapshot-Job nutzt das noch."""
    return _fetch_ticker_closes("SPY", years=years)


def _ticker_return_pct(closes: dict, start_dt, end_dt) -> float | None:
    """Berechnet Tagesschluss-Rendite in % zwischen zwei Daten.

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


# Backwards-compat Alias — equity_snapshot.py & alte Calls erwarten _spy_return_pct
_spy_return_pct = _ticker_return_pct


@app.get("/api/benchmark")
async def api_benchmark(user=Depends(require_auth)):
    """Liefert Multi-Benchmark-Returns (SPY/QQQ/AGG/60-40) ueber dieselben
    Zeitfenster wie /api/pnl-periods.

    Das Frontend berechnet Alpha pro Benchmark (portfolio_pct - bench_pct)
    selbst — vermeidet Code-Duplikation und die Portfolio-Daten kommen eh
    aus /api/pnl-periods.
    """
    from datetime import datetime, timedelta
    try:
        closes_by_symbol = {sym: _fetch_ticker_closes(sym, years=5) for sym in BENCHMARK_SYMBOLS}
        # Hauptbenchmark MUSS verfuegbar sein, AGG/QQQ duerfen fehlen
        if not closes_by_symbol.get("SPY"):
            return {"error": "SPY-Daten nicht verfuegbar", "benchmarks": [], "periods": []}

        now = datetime.now()
        windows = [
            ("1d",   "Heute",        now - timedelta(days=1)),
            ("7d",   "7 Tage",       now - timedelta(days=7)),
            ("30d",  "30 Tage",      now - timedelta(days=30)),
            ("90d",  "3 Monate",     now - timedelta(days=90)),
            ("180d", "6 Monate",     now - timedelta(days=180)),
            ("365d", "1 Jahr",       now - timedelta(days=365)),
            ("ytd",  "Jahresanfang", datetime(now.year, 1, 1)),
            ("all",  "Gesamt",       now - timedelta(days=365 * 5)),
        ]

        periods = []
        for key, label, start_dt in windows:
            cell = {"key": key, "label": label}
            for sym in BENCHMARK_SYMBOLS:
                pct = _ticker_return_pct(closes_by_symbol.get(sym) or {}, start_dt, now)
                cell[f"{sym.lower()}_pct"] = round(pct, 2) if pct is not None else None

            # 60/40 = 0.6*SPY + 0.4*AGG. Beide muessen verfuegbar sein.
            spy_pct = cell.get("spy_pct")
            agg_pct = cell.get("agg_pct")
            if spy_pct is not None and agg_pct is not None:
                cell["mix6040_pct"] = round(0.6 * spy_pct + 0.4 * agg_pct, 2)
            else:
                cell["mix6040_pct"] = None
            # Backwards-compat fuer alte Frontends, die noch p.spy_pct erwarten
            cell["spy_pct"] = cell.get("spy_pct")
            periods.append(cell)

        # Stale-Check ueber juengste Datenquelle
        latest = None
        for sym, closes in closes_by_symbol.items():
            if closes:
                m = max(closes.keys())
                if latest is None or m > latest:
                    latest = m

        return {
            "benchmarks": [
                {"key": "spy",       "label": "SPY",   "name": "S&P 500 ETF"},
                {"key": "qqq",       "label": "QQQ",   "name": "Nasdaq-100 ETF"},
                {"key": "mix6040",   "label": "60/40", "name": "60% SPY + 40% AGG (klassisch)"},
            ],
            "periods": periods,
            "data_points": {sym: (len(c) if c else 0) for sym, c in closes_by_symbol.items()},
            "latest_close_date": latest.isoformat() if latest else None,
        }
    except Exception as e:
        log.error(f"Benchmark Error: {e}")
        return {"error": str(e), "benchmarks": [], "periods": []}


# ============================================================
# EQUITY HISTORY (Daily Snapshots -> Monatstabelle)
# ============================================================
MIN_SNAPSHOTS_FOR_TABLE = 5  # erste Monatszeile sobald genug Daten da sind


def _aggregate_monthly(snapshots: list) -> list:
    """Baut Monats-Buckets aus Daily-Snapshots.

    Pro Kalendermonat: erster + letzter Snapshot. Daraus pct-Returns fuer
    Portfolio + alle Benchmarks. 60/40 = 0.6*SPY + 0.4*AGG.
    """
    if not snapshots:
        return []

    # Sortieren nach Datum (defensiv — sollte schon sortiert sein)
    sorted_snaps = sorted(snapshots, key=lambda s: s.get("date", ""))

    # Gruppieren {YYYY-MM: [snaps...]}
    by_month: dict = {}
    for s in sorted_snaps:
        d = s.get("date", "")
        if len(d) < 7:
            continue
        ym = d[:7]
        by_month.setdefault(ym, []).append(s)

    def _pct(first, last, key):
        a = first.get(key)
        b = last.get(key)
        if a in (None, 0) or b is None:
            return None
        try:
            a, b = float(a), float(b)
            if a == 0:
                return None
            return round((b - a) / a * 100, 2)
        except Exception:
            return None

    rows = []
    for ym in sorted(by_month.keys()):
        snaps = by_month[ym]
        first, last = snaps[0], snaps[-1]
        bot = _pct(first, last, "portfolio_total_value")
        spy = _pct(first, last, "spy_close")
        qqq = _pct(first, last, "qqq_close")
        agg = _pct(first, last, "agg_close")
        mix = round(0.6 * spy + 0.4 * agg, 2) if (spy is not None and agg is not None) else None

        def _alpha(b, x):
            return round(b - x, 2) if (b is not None and x is not None) else None

        rows.append({
            "month": ym,
            "days_in_month": len(snaps),
            "bot_pct": bot,
            "spy_pct": spy,
            "qqq_pct": qqq,
            "mix6040_pct": mix,
            "alpha_spy": _alpha(bot, spy),
            "alpha_qqq": _alpha(bot, qqq),
            "alpha_mix6040": _alpha(bot, mix),
            "first_date": first.get("date"),
            "last_date": last.get("date"),
        })
    return rows


@app.get("/api/equity-history")
async def api_equity_history(user=Depends(require_auth)):
    """Liefert Daily-Snapshots + Monats-Aggregation fuer den Equity-Verlauf.

    Datenquelle: data/equity_history.json (geschrieben durch
    app/equity_snapshot.py taeglich um >= 22:30 CET).

    Frontend zeigt Tabelle erst ab MIN_SNAPSHOTS_FOR_TABLE Tagen — vorher
    nur Progress-Hinweis "X / Y Tage gesammelt".
    """
    try:
        from app.config_manager import load_json as _load_json
        snaps = _load_json("equity_history.json") or []
        if not isinstance(snaps, list):
            snaps = []

        ready = len(snaps) >= MIN_SNAPSHOTS_FOR_TABLE
        monthly = _aggregate_monthly(snaps) if ready else []

        first_iso = snaps[0]["date"] if snaps else None
        last_iso = snaps[-1]["date"] if snaps else None

        return {
            "ready": ready,
            "snapshots_total": len(snaps),
            "min_required": MIN_SNAPSHOTS_FOR_TABLE,
            "first_date": first_iso,
            "last_date": last_iso,
            "monthly": monthly,
            # Daily-Reihen werden hier mitgesendet, damit ein spaeterer
            # Equity-Curve-Chart ohne weiteren Roundtrip auskommt.
            "daily": snaps,
        }
    except Exception as e:
        log.error(f"Equity-History Error: {e}")
        return {"error": str(e), "ready": False, "snapshots_total": 0, "monthly": [], "daily": []}


@app.post("/api/equity-history/snapshot-now")
async def api_equity_snapshot_now(user=Depends(require_auth)):
    """Manueller Trigger fuer einen Snapshot — nuetzlich zum Testen oder um
    bei einem verpassten 22:30-Slot nachzuholen. Idempotent (max 1/Tag)."""
    try:
        from app.equity_snapshot import take_snapshot
        snap = take_snapshot(triggered_by="manual-dashboard")
        if snap is None:
            return {"ok": False, "message": "Snapshot fuer heute existiert bereits oder Portfolio-Wert nicht ermittelbar"}
        return {"ok": True, "snapshot": snap}
    except Exception as e:
        log.error(f"Snapshot-Now Error: {e}")
        return {"ok": False, "error": str(e)}


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
            client = get_broker(config, readonly=True)
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
    """Trade-Historie (paginiert). Angereichert mit Symbol-Namen."""
    history = read_json_safe("trade_history.json") or []
    history.reverse()  # Neueste zuerst
    enrich_with_asset_meta(history)
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
    async with _CONFIG_WRITE_LOCK:
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
                changes.append(f"Max Trade USD (legacy): {old} -> {update.max_single_trade_usd}")

        if update.max_single_trade_pct_of_portfolio is not None:
            # Hard-Bounds: 0 < pct <= 0.5 (50% pro Trade absolute Obergrenze)
            pct = update.max_single_trade_pct_of_portfolio
            if not (0 < pct <= 0.5):
                raise HTTPException(400, "max_single_trade_pct_of_portfolio muss in (0, 0.5] liegen")
            old = dt.get("max_single_trade_pct_of_portfolio")
            dt["max_single_trade_pct_of_portfolio"] = pct
            if old != pct:
                changes.append(f"Max Trade %: {old} -> {pct}")

        if update.portfolio_targets is not None:
            if not update.portfolio_targets:
                # Leeres Dict = v15-Modus: Bot steuert via Scanner/Kelly/Percent-Sizing
                old_count = len(dt.get("portfolio_targets", {}) or {})
                dt["portfolio_targets"] = {}
                changes.append(f"Portfolio-Targets geleert (war {old_count} Symbole) -> v15-Modus")
            else:
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


class KellyUpdate(BaseModel):
    max_fraction: float

    @validator("max_fraction")
    def validate_max_fraction(cls, v):
        # Hard safety bounds: 0 < k <= 0.15 (15% = absolute ceiling, weit jenseits
        # des 8% MaxDD-Hard-Gates bei unserem Backtest-Profil)
        if not (0 < v <= 0.15):
            raise ValueError("kelly.max_fraction muss in (0, 0.15] liegen")
        return v


@app.put("/api/config/kelly")
async def api_update_kelly(update: KellyUpdate, user=Depends(require_auth)):
    """Kelly-Sizing max_fraction live aendern (persistiert in data/config.json)."""
    async with _CONFIG_WRITE_LOCK:
        config = load_config()
        ks = config.setdefault("kelly_sizing", {})
        old = ks.get("max_fraction")
        ks["max_fraction"] = update.max_fraction
        save_config(config)

    try:
        from web.security import log_audit
        await log_audit(user, "CONFIG_CHANGE",
                        f"kelly.max_fraction: {old} -> {update.max_fraction}")
    except Exception:
        pass

    return {"status": "ok", "old": old, "new": update.max_fraction}


@app.post("/api/config/v15-sync")
async def api_sync_v15_config(user=Depends(require_auth)):
    """Synchronisiert die v15-Sizing/DCA/Tier-Keys aus der Git-Seed-Config
    in die Live-Config auf /data/config.json.

    Hintergrund (Render Persistent Disk Gotcha): Beim Deploy wird die
    Git-Version von config.json nicht auf die persistente /data-Kopie
    kopiert (idempotent). Neue Config-Keys aus dem Repo greifen daher erst
    nach manuellem Sync. Dieser Endpoint patcht nur die v15-Keys und
    persistiert via save_config().
    """
    try:
        import json as _json
        from pathlib import Path as _P

        # Lade Git-Seed: Repo-Root/data/config.json (nicht /data/!)
        # Render hat das Repo-File unter /app/data/config.json gemounted
        seed_paths = [
            _P("/app/data/config.json"),
            _P(__file__).parent.parent / "data" / "config.json",
        ]
        seed = None
        for p in seed_paths:
            if p.exists():
                with open(p, "r", encoding="utf-8") as f:
                    seed = _json.load(f)
                break
        if not seed:
            raise HTTPException(status_code=500, detail="Seed-Config nicht gefunden")

        async with _CONFIG_WRITE_LOCK:
            live = load_config() or {}

            applied = {}

            # demo_trading: pct/floor/cap Keys
            dt_seed = seed.get("demo_trading", {}) or {}
            dt_live = live.setdefault("demo_trading", {})
            for key in ("max_single_trade_pct_of_portfolio",
                        "max_single_trade_usd_floor",
                        "max_single_trade_usd_hard_cap"):
                if key in dt_seed:
                    old = dt_live.get(key, "MISSING")
                    dt_live[key] = dt_seed[key]
                    applied[f"demo_trading.{key}"] = {"old": old, "new": dt_seed[key]}

            # portfolio_sizing: Tier-Map
            ps_seed = seed.get("portfolio_sizing")
            if ps_seed is not None:
                old = live.get("portfolio_sizing", "MISSING")
                live["portfolio_sizing"] = ps_seed
                applied["portfolio_sizing"] = {"old": old, "new": ps_seed}

            # deposit_handling: DCA-Konfig
            dh_seed = seed.get("deposit_handling")
            if dh_seed is not None:
                old = live.get("deposit_handling", "MISSING")
                live["deposit_handling"] = dh_seed
                applied["deposit_handling"] = {"old": old, "new": dh_seed}

            # _live_freeze: Audit-Block uebernehmen
            if "_live_freeze" in seed:
                live["_live_freeze"] = seed["_live_freeze"]
                applied["_live_freeze"] = "synced"

            save_config(live)

        try:
            from web.security import log_audit
            await log_audit(user, "CONFIG_CHANGE",
                            f"v15-sync: {len(applied)} Keys synchronisiert")
        except Exception:
            pass

        return {"status": "ok", "applied": applied, "count": len(applied)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/config/strategy-audit")
async def api_config_strategy_audit(user=Depends(require_auth)):
    """Vergleicht die Strategy-kritischen Keys zwischen Git-Seed und Live-Config.

    Zweck: Aufdecken der Config-Drift auf der Render Persistent Disk. Der
    v15-sync-Endpoint synchronisiert nur v15-Keys; andere Strategie-Parameter
    (Kelly, min_scanner_score, SL/TP, use_ml_scoring) koennen zwischen
    Git-Seed und /data/config.json divergieren.

    Read-only: aendert nichts, gibt nur einen Diff zurueck.
    """
    try:
        import json as _json
        from pathlib import Path as _P

        # Seed laden (gleiche Logik wie v15-sync)
        seed_paths = [
            _P("/app/data/config.json"),
            _P(__file__).parent.parent / "data" / "config.json",
        ]
        seed = None
        seed_source = None
        for p in seed_paths:
            if p.exists():
                with open(p, "r", encoding="utf-8") as f:
                    seed = _json.load(f)
                seed_source = str(p)
                break
        if not seed:
            raise HTTPException(status_code=500, detail="Seed-Config nicht gefunden")

        live = load_config() or {}

        # Kritische Strategy-Keys definieren: (path_list, label)
        checks = [
            (["demo_trading", "strategy"], "Strategie"),
            (["demo_trading", "min_scanner_score"], "Min Scanner Score"),
            (["demo_trading", "stop_loss_pct"], "Stop-Loss %"),
            (["demo_trading", "take_profit_pct"], "Take-Profit %"),
            (["demo_trading", "default_leverage"], "Default Leverage"),
            (["demo_trading", "max_positions"], "Max Positionen (legacy)"),
            (["demo_trading", "max_single_trade_usd"], "Max Trade USD (legacy)"),
            (["demo_trading", "max_single_trade_pct_of_portfolio"], "Max Trade % (v15)"),
            (["demo_trading", "use_ml_scoring"], "ML-Scoring aktiv"),
            (["demo_trading", "rebalance_threshold_pct"], "Rebalance-Threshold"),
            (["kelly_sizing", "enabled"], "Kelly aktiv"),
            (["kelly_sizing", "max_fraction"], "Kelly max_fraction (k)"),
            (["kelly_sizing", "half_kelly"], "Half-Kelly"),
            (["kelly_sizing", "min_trades"], "Kelly min_trades"),
            (["regime_filter", "enabled"], "Regime-Filter aktiv"),
            (["multi_timeframe", "enabled"], "Multi-Timeframe aktiv"),
            (["multi_timeframe", "min_confluence_score"], "MTF min_confluence_score"),
            (["vix_term_structure", "enabled"], "VIX Term Structure"),
        ]

        def walk(d, path):
            cur = d
            for k in path:
                if not isinstance(cur, dict) or k not in cur:
                    return "__MISSING__"
                cur = cur[k]
            return cur

        MISSING = "__MISSING__"
        report = []
        drift_count = 0
        for path, label in checks:
            seed_val = walk(seed, path)
            live_val = walk(live, path)
            match = (seed_val == live_val)
            if not match:
                drift_count += 1
            report.append({
                "key": ".".join(path),
                "label": label,
                "seed": None if seed_val == MISSING else seed_val,
                "live": None if live_val == MISSING else live_val,
                "seed_missing": seed_val == MISSING,
                "live_missing": live_val == MISSING,
                "match": match,
            })

        # Letzter Backtest — mit welcher Config lief er?
        try:
            bt_history = read_json_safe("backtest_results.json") or {}
            bt_config = bt_history.get("config_used") or {}
            bt_timestamp = bt_history.get("timestamp")
        except Exception:
            bt_config = {}
            bt_timestamp = None

        # Live-Freeze Info
        freeze = live.get("_live_freeze") or seed.get("_live_freeze") or {}

        return {
            "status": "drift_detected" if drift_count > 0 else "in_sync",
            "drift_count": drift_count,
            "total_checks": len(checks),
            "seed_source": seed_source,
            "seed_live_freeze": freeze,
            "last_backtest": {
                "timestamp": bt_timestamp,
                "config_used": bt_config,
            },
            "diff": report,
        }
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Strategy-Audit Error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


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


@app.get("/api/email-config-check")
async def api_email_config_check(user=Depends(require_auth)):
    """Prueft ob SMTP-Konfiguration fuer Weekly-Report-Email vorhanden ist.
    Gibt nur Boolsche Werte + maskierte Hinweise zurueck — KEINE Passwoerter.
    """
    import os as _os
    smtp_server = _os.environ.get("SMTP_SERVER", "smtp.gmail.com")
    smtp_port = _os.environ.get("SMTP_PORT", "587")
    smtp_email = _os.environ.get("SMTP_EMAIL", "")
    smtp_password = _os.environ.get("SMTP_PASSWORD", "")
    recipient = _os.environ.get("ALERT_RECIPIENT", "")

    def _mask_email(e):
        if not e or "@" not in e:
            return None
        local, domain = e.split("@", 1)
        if len(local) <= 2:
            return f"{local[:1]}*@{domain}"
        return f"{local[:2]}{'*' * (len(local)-2)}@{domain}"

    ready = bool(smtp_email and smtp_password and recipient)
    return {
        "ready": ready,
        "smtp_server": smtp_server,
        "smtp_port": smtp_port,
        "smtp_email_set": bool(smtp_email),
        "smtp_email_masked": _mask_email(smtp_email),
        "smtp_password_set": bool(smtp_password),
        "smtp_password_length": len(smtp_password) if smtp_password else 0,
        "recipient_set": bool(recipient),
        "recipient_masked": _mask_email(recipient),
        "missing": [
            key for key, val in [
                ("SMTP_EMAIL", smtp_email),
                ("SMTP_PASSWORD", smtp_password),
                ("ALERT_RECIPIENT", recipient),
            ] if not val
        ],
    }


@app.get("/api/weekly-report/maintenance-preview")
async def api_weekly_maintenance_preview(user=Depends(require_auth)):
    """Preview-Endpoint: Ruft nur den Wartungs-Block live auf (ohne Cache).

    Zweck: Entwicklungs-/Debug-Endpoint um neue Maintenance-Checks zu
    verifizieren ohne auf den Freitag-Cron zu warten oder eine Email
    auszuloesen.
    """
    try:
        from datetime import datetime as _dt
        from app.weekly_report import _maintenance_block
        items = _maintenance_block()
        return {
            "generated_at": _dt.now().isoformat(),
            "count": len(items),
            "items": [
                {"name": n, "status": s, "detail": d, "severity": sev}
                for (n, s, d, sev) in items
            ],
        }
    except Exception as e:
        log.error(f"Maintenance-Preview Error: {e}", exc_info=True)
        return {"error": str(e)}


@app.get("/api/discovery")
async def api_discovery(user=Depends(require_auth)):
    """Letzte Asset Discovery Ergebnisse."""
    try:
        from app.persistence import check_and_reload_discovery_output
        check_and_reload_discovery_output()
    except Exception as e:
        log.debug(f"check_and_reload_discovery_output skipped: {e}")

    result = read_json_safe("discovery_result.json")
    if result:
        return result
    return {"new_found": 0, "evaluated": 0, "added": 0, "message": "Noch keine Discovery gelaufen"}


def _trigger_github_action_discovery(username: str):
    """Triggert den Manual-Discovery-Workflow auf GitHub Actions.

    Mirror zu _trigger_github_action_backtest/_trigger_github_action_ml_training.
    Entkoppelt Discovery von Render damit yfinance-Rate-Limits / viele API-Calls
    den Trading-Server nicht beeintraechtigen. Ergebnisse kommen via Gist zurueck
    und der Watchdog appliziert die neuen Symbole in den Live-ASSET_UNIVERSE.
    """
    from datetime import datetime
    from app.config_manager import save_json

    initial_status = {
        "state": "running",
        "phase": "dispatching",
        "message": "GitHub Action wird gestartet...",
        "started_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "finished_at": None,
        "triggered_by": username,
        "action": None,
        "error": None,
        "mode": "github-action-dispatching",
    }
    try:
        save_json("discovery_status.json", initial_status)
    except Exception:
        pass

    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        log.error("Discovery-Trigger: GITHUB_TOKEN fehlt")
        initial_status["state"] = "error"
        initial_status["error"] = "GITHUB_TOKEN fehlt — Workflow nicht ausloesbar"
        initial_status["finished_at"] = datetime.now().isoformat()
        initial_status["updated_at"] = datetime.now().isoformat()
        try:
            save_json("discovery_status.json", initial_status)
        except Exception:
            pass
        return

    repo = os.environ.get("GITHUB_REPO", "carlosbaumann754-svg/investpilot")
    workflow_file = os.environ.get("DISCOVERY_WORKFLOW_FILE", "asset_discovery.yml")
    ref = os.environ.get("DISCOVERY_WORKFLOW_REF", "master")
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
            log.info(f"Discovery-Workflow getriggert (repo={repo}, ref={ref})")
            initial_status["mode"] = "github-action-running"
            initial_status["action"] = "dispatched"
            initial_status["message"] = "GitHub Action gestartet, warte auf Runner..."
        else:
            log.error(f"Discovery-Dispatch HTTP {resp.status_code}: {resp.text[:200]}")
            initial_status["state"] = "error"
            initial_status["error"] = (
                f"workflow_dispatch HTTP {resp.status_code}: {resp.text[:160]}"
            )
            initial_status["finished_at"] = datetime.now().isoformat()
    except Exception as e:
        log.exception("Discovery Workflow-Dispatch fehlgeschlagen")
        initial_status["state"] = "error"
        initial_status["error"] = f"dispatch: {type(e).__name__}: {e}"
        initial_status["finished_at"] = datetime.now().isoformat()

    initial_status["updated_at"] = datetime.now().isoformat()
    try:
        save_json("discovery_status.json", initial_status)
    except Exception:
        pass


@app.post("/api/discovery/run")
async def api_run_discovery(background_tasks: BackgroundTasks, user=Depends(require_auth)):
    """Asset Discovery manuell ausloesen — offloaded auf GitHub Actions."""
    try:
        from datetime import datetime
        from app.config_manager import load_json, save_json

        STALE_LOCK_MINUTES = 60
        status = load_json("discovery_status.json") or {}
        if status.get("state") == "running":
            started = status.get("started_at") or status.get("updated_at")
            is_stale = False
            if started:
                try:
                    started_dt = datetime.fromisoformat(started)
                    age_min = (datetime.now() - started_dt).total_seconds() / 60
                    if age_min > STALE_LOCK_MINUTES:
                        is_stale = True
                        log.warning(
                            f"Stale Discovery-Lock erkannt ({age_min:.0f} Min alt)"
                        )
                        status["state"] = "error"
                        status["error"] = (
                            f"Lauf abgebrochen (Lock stale nach {age_min:.0f} Min)"
                        )
                        status["finished_at"] = datetime.now().isoformat()
                        status["updated_at"] = datetime.now().isoformat()
                        save_json("discovery_status.json", status)
                except Exception:
                    pass

            if not is_stale:
                return {
                    "status": "already_running",
                    "message": f"Discovery laeuft bereits seit {started}",
                    "started_at": started,
                }

        background_tasks.add_task(_trigger_github_action_discovery, user)

        try:
            from web.security import log_audit
            await log_audit(user, "DISCOVERY_RUN_STARTED",
                            "GitHub Action dispatched")
        except Exception:
            pass

        return {
            "status": "started",
            "message": ("Discovery laeuft auf GitHub Actions. "
                        "Dauer ~2-10 Min. Status ueber /api/discovery/status."),
            "started_at": datetime.now().isoformat(),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/discovery/status")
async def api_discovery_status(user=Depends(require_auth)):
    """Status des letzten/laufenden Discovery-GitHub-Action-Laufs."""
    try:
        from app.persistence import check_and_reload_discovery_output
        check_and_reload_discovery_output()
    except Exception as e:
        log.debug(f"check_and_reload_discovery_output skipped: {e}")

    from app.config_manager import load_json
    status = load_json("discovery_status.json")
    if not status:
        return {"state": "idle", "message": "Noch kein Discovery-Lauf gestartet"}
    return status


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
        client = get_broker(config, readonly=True)
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
        from app.risk_manager import (
            get_risk_summary, calculate_exposure, check_margin_safety,
            resolve_max_positions, resolve_max_single_trade_usd, detect_cash_deposit,
        )
        from app.etoro_client import EtoroClient
        from app.config_manager import load_config

        summary = get_risk_summary()

        config = load_config()
        broker_name = (config.get("broker") or "etoro").lower()
        client = get_broker(config, readonly=True)
        if client.configured:
            # IBKR: aus brain-cache lesen (vermeidet asyncio loop conflict)
            # eToro: live REST-API (loop-safe)
            if broker_name == "ibkr":
                portfolio = _portfolio_from_brain_cache()
            else:
                portfolio = client.get_portfolio()
            if portfolio:
                from app.etoro_client import EtoroClient as EC
                positions = [EC.parse_position(p) for p in portfolio.get("positions", [])]
                credit = portfolio.get("credit", 0)
                # IBKR brain-cache hat _total_value direkt, sonst aus credit + invested
                total = portfolio.get("_total_value") or (credit + sum(p["invested"] for p in positions))

                exposure = calculate_exposure(positions)
                margin_ok, margin_reason, exposure_detail = check_margin_safety(total, positions, config)

                summary["exposure"] = exposure_detail
                summary["margin_ok"] = margin_ok
                summary["margin_reason"] = margin_reason

                # v15: Prozent-basierte Sizing + Cash-DCA Metriken
                try:
                    dt = (config or {}).get("demo_trading", {}) or {}
                    ps = (config or {}).get("portfolio_sizing", {}) or {}
                    tiers = ps.get("max_positions_by_capital") or {}
                    tier_threshold = None
                    try:
                        for k, _v in sorted(((float(k), int(v)) for k, v in tiers.items()), key=lambda x: x[0]):
                            if total <= k:
                                tier_threshold = k
                                break
                    except Exception:
                        tier_threshold = None

                    summary["v15_sizing"] = {
                        "portfolio_value_usd": round(total, 2),
                        "max_positions": resolve_max_positions(total, config),
                        "current_positions": len(positions),
                        "max_single_trade_usd": resolve_max_single_trade_usd(total, config),
                        "pct_of_portfolio": dt.get("max_single_trade_pct_of_portfolio"),
                        "floor_usd": dt.get("max_single_trade_usd_floor", 50),
                        "hard_cap_usd": dt.get("max_single_trade_usd_hard_cap"),
                        "tier_threshold_usd": tier_threshold,
                    }

                    dca = detect_cash_deposit(credit, config)
                    # Für Progress-Anzeige: zusaetzlich Raw-State ziehen
                    try:
                        from app.config_manager import load_json
                        raw_state = load_json("cash_dca_state.json") or {}
                        plan = raw_state.get("active_plan") or {}
                        if plan:
                            dca["total_deposit_usd"] = plan.get("total_deposit_usd")
                            dca["consumed_usd"] = plan.get("consumed_usd", 0)
                            total_dep = float(plan.get("total_deposit_usd") or 0)
                            consumed = float(plan.get("consumed_usd") or 0)
                            dca["progress_pct"] = round(100.0 * consumed / total_dep, 1) if total_dep > 0 else 0
                    except Exception:
                        pass
                    summary["v15_cash_dca"] = dca
                except Exception as e:
                    summary["v15_error"] = str(e)

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
        client = get_broker(config, readonly=True)
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
    """Letzte Backtest-Ergebnisse. Pollt vorher den Gist-Watchdog, damit
    frische Ergebnisse eines laufenden GitHub-Action-Backtests sofort
    sichtbar werden."""
    try:
        from app.persistence import check_and_reload_backtest_output
        check_and_reload_backtest_output()
    except Exception as e:
        log.debug(f"check_and_reload_backtest_output skipped: {e}")

    result = read_json_safe("backtest_results.json")
    if result:
        return result
    return {"error": "Noch kein Backtest gelaufen. Starte einen ueber 'Run Backtest'."}


def _trigger_github_action_backtest(username: str):
    """
    Triggert den Manual-Backtest-Workflow auf GitHub Actions (v12).

    Vorteil ggue. lokaler Ausfuehrung:
    - Laeuft auf einem 7-GB-RAM Runner statt Render Free Tier 512 MB
    - OOMs koennen den Web-Container nicht mehr toeten (= keine 502)
    - Voller Walk-Forward ohne Memory-Safeguards-Abbruch
    - Ergebnisse werden via Gist gepusht (check_and_reload_backtest_output)

    Mirror zu _trigger_github_action_optimizer.
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
        save_json("backtest_status.json", initial_status)
    except Exception:
        pass

    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        log.error("Backtest-Trigger: GITHUB_TOKEN fehlt")
        initial_status["state"] = "error"
        initial_status["error"] = "GITHUB_TOKEN fehlt — Workflow nicht ausloesbar"
        initial_status["finished_at"] = datetime.now().isoformat()
        try:
            save_json("backtest_status.json", initial_status)
        except Exception:
            pass
        return

    repo = os.environ.get("GITHUB_REPO", "carlosbaumann754-svg/investpilot")
    workflow_file = os.environ.get("BACKTEST_WORKFLOW_FILE", "backtest.yml")
    ref = os.environ.get("BACKTEST_WORKFLOW_REF", "master")
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
            log.info(f"Backtest-Workflow getriggert (repo={repo}, ref={ref})")
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
        log.exception("Backtest Workflow-Dispatch fehlgeschlagen")
        initial_status["state"] = "error"
        initial_status["error"] = f"dispatch: {type(e).__name__}: {e}"
        initial_status["finished_at"] = datetime.now().isoformat()

    try:
        save_json("backtest_status.json", initial_status)
    except Exception:
        pass


@app.post("/api/backtest/run")
async def api_run_backtest(background_tasks: BackgroundTasks, user=Depends(require_auth)):
    """Backtest im Hintergrund auf GitHub Actions starten (Render Free Tier
    kann den Full-Backtest nicht ausfuehren ohne OOM -> 502). Mirror zum
    Optimizer-Pattern."""
    try:
        from datetime import datetime
        from app.config_manager import load_json, save_json

        # Stale-Lock-Recovery analog zu /api/optimizer/run
        STALE_LOCK_MINUTES = 60
        status = load_json("backtest_status.json") or {}
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
                            f"Stale Backtest-Lock erkannt ({age_min:.0f} Min alt) "
                            f"— vermutlich Workflow-Timeout. Reset auf error."
                        )
                        status["state"] = "error"
                        status["error"] = (
                            f"Lauf abgebrochen (Lock stale nach {age_min:.0f} Min)"
                        )
                        status["finished_at"] = datetime.now().isoformat()
                        save_json("backtest_status.json", status)
                except Exception:
                    pass

            if not is_stale:
                return {
                    "status": "already_running",
                    "message": f"Backtest laeuft bereits seit {started}",
                    "started_at": started,
                }

        background_tasks.add_task(_trigger_github_action_backtest, user)

        try:
            from web.security import log_audit
            await log_audit(user, "BACKTEST_RUN_STARTED",
                            "GitHub Action dispatched")
        except Exception:
            pass

        return {
            "status": "started",
            "message": ("Backtest laeuft auf GitHub Actions. "
                        "Dauer ~5-15 Min. Status ueber /api/backtest/status."),
            "started_at": datetime.now().isoformat(),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/backtest/status")
async def api_backtest_status(user=Depends(require_auth)):
    """Status des letzten/laufenden Backtest-GitHub-Action-Laufs.

    Pollt vor dem Lesen den Gist (check_and_reload_backtest_output), damit
    Ergebnisse des GH-Action-Runners zeitnah sichtbar werden ohne auf den
    naechsten periodischen Watchdog-Zyklus zu warten.
    """
    try:
        from app.persistence import check_and_reload_backtest_output
        check_and_reload_backtest_output()
    except Exception as e:
        log.debug(f"check_and_reload_backtest_output skipped: {e}")

    from app.config_manager import load_json
    status = load_json("backtest_status.json")
    if not status:
        return {"state": "idle", "message": "Noch kein Backtest-Lauf gestartet"}
    return status


@app.get("/api/ml-model")
async def api_ml_model(user=Depends(require_auth)):
    """ML-Modell Status und Feature Importances."""
    try:
        from app.persistence import check_and_reload_ml_training_output
        check_and_reload_ml_training_output()
    except Exception as e:
        log.debug(f"check_and_reload_ml_training_output skipped: {e}")
    try:
        from app.ml_scorer import get_model_info, is_model_trained
        info = get_model_info()
        if info:
            info["is_active"] = is_model_trained()
            return info
        return {"error": "Kein ML-Modell trainiert", "is_active": False}
    except ImportError:
        return {"error": "ML Module nicht verfuegbar", "is_active": False}


def _trigger_github_action_ml_training(username: str):
    """
    Triggert den Manual-ML-Training-Workflow auf GitHub Actions.

    Vorteil ggue. lokaler Ausfuehrung:
    - Laeuft auf einem 7-GB-RAM Runner statt Render Free Tier 512 MB
    - download_history(years=5) + RandomForest kann den Web-Container
      nicht mehr OOMen (= keine 502)
    - Ergebnisse (inkl. joblib-Weights base64-encoded) werden via Gist
      gepusht (check_and_reload_ml_training_output)

    Mirror zu _trigger_github_action_backtest / _trigger_github_action_optimizer.
    """
    from datetime import datetime
    from app.config_manager import save_json

    initial_status = {
        "state": "running",
        "phase": "dispatching",
        "message": "GitHub Action wird gestartet...",
        "started_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "finished_at": None,
        "triggered_by": username,
        "action": None,
        "error": None,
        "mode": "github-action-dispatching",
    }
    try:
        save_json("ml_training_status.json", initial_status)
    except Exception:
        pass

    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        log.error("ML-Training-Trigger: GITHUB_TOKEN fehlt")
        initial_status["state"] = "error"
        initial_status["error"] = "GITHUB_TOKEN fehlt — Workflow nicht ausloesbar"
        initial_status["finished_at"] = datetime.now().isoformat()
        initial_status["updated_at"] = datetime.now().isoformat()
        try:
            save_json("ml_training_status.json", initial_status)
        except Exception:
            pass
        return

    repo = os.environ.get("GITHUB_REPO", "carlosbaumann754-svg/investpilot")
    workflow_file = os.environ.get("ML_TRAINING_WORKFLOW_FILE", "ml_training.yml")
    ref = os.environ.get("ML_TRAINING_WORKFLOW_REF", "master")
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
            log.info(f"ML-Training-Workflow getriggert (repo={repo}, ref={ref})")
            initial_status["mode"] = "github-action-running"
            initial_status["action"] = "dispatched"
            initial_status["message"] = "GitHub Action gestartet, warte auf Runner..."
        else:
            log.error(f"ML-Training-Dispatch HTTP {resp.status_code}: {resp.text[:200]}")
            initial_status["state"] = "error"
            initial_status["error"] = (
                f"workflow_dispatch HTTP {resp.status_code}: {resp.text[:160]}"
            )
            initial_status["finished_at"] = datetime.now().isoformat()
    except Exception as e:
        log.exception("ML-Training Workflow-Dispatch fehlgeschlagen")
        initial_status["state"] = "error"
        initial_status["error"] = f"dispatch: {type(e).__name__}: {e}"
        initial_status["finished_at"] = datetime.now().isoformat()

    initial_status["updated_at"] = datetime.now().isoformat()
    try:
        save_json("ml_training_status.json", initial_status)
    except Exception:
        pass


@app.post("/api/ml-model/train")
async def api_train_ml(background_tasks: BackgroundTasks, user=Depends(require_auth)):
    """ML-Modell neu trainieren — offloaded auf GitHub Actions (v12 pattern).

    Antwortet sofort mit 202 und dispatcht eine GH Action (7 GB RAM, weil
    Render Free Tier mit 512 MB bei download_history(years=5) zuverlaessig
    OOMed). Frontend pollt /api/ml-model/train/status fuer Fortschritt.
    """
    try:
        from datetime import datetime
        from app.config_manager import load_json, save_json

        # Stale-Lock-Recovery analog zu /api/backtest/run und /api/optimizer/run
        STALE_LOCK_MINUTES = 60
        status = load_json("ml_training_status.json") or {}
        if status.get("state") == "running":
            started = status.get("started_at") or status.get("updated_at")
            is_stale = False
            if started:
                try:
                    started_dt = datetime.fromisoformat(started)
                    age_min = (datetime.now() - started_dt).total_seconds() / 60
                    if age_min > STALE_LOCK_MINUTES:
                        is_stale = True
                        log.warning(
                            f"Stale ML-Training-Lock erkannt ({age_min:.0f} Min alt) "
                            f"— vermutlich Workflow-Timeout. Reset auf error."
                        )
                        status["state"] = "error"
                        status["error"] = (
                            f"Lauf abgebrochen (Lock stale nach {age_min:.0f} Min)"
                        )
                        status["finished_at"] = datetime.now().isoformat()
                        status["updated_at"] = datetime.now().isoformat()
                        save_json("ml_training_status.json", status)
                except Exception:
                    pass

            if not is_stale:
                return {
                    "status": "already_running",
                    "message": f"ML-Training laeuft bereits seit {started}",
                    "started_at": started,
                }

        background_tasks.add_task(_trigger_github_action_ml_training, user)

        try:
            from web.security import log_audit
            await log_audit(user, "ML_TRAIN_STARTED",
                            "GitHub Action dispatched")
        except Exception:
            pass

        return {
            "status": "started",
            "message": ("ML-Training laeuft auf GitHub Actions. "
                        "Dauer ~5-15 Min. Status ueber /api/ml-model/train/status."),
            "started_at": datetime.now().isoformat(),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/ml-model/train/status")
async def api_train_ml_status(user=Depends(require_auth)):
    """Status des aktuellen/letzten ML-Training-GitHub-Action-Laufs.

    Pollt vor dem Lesen den Gist (check_and_reload_ml_training_output), damit
    Ergebnisse des GH-Action-Runners zeitnah sichtbar werden ohne auf den
    naechsten periodischen Watchdog-Zyklus zu warten.
    """
    try:
        from app.persistence import check_and_reload_ml_training_output
        check_and_reload_ml_training_output()
    except Exception as e:
        log.debug(f"check_and_reload_ml_training_output skipped: {e}")

    from app.config_manager import load_json
    status = load_json("ml_training_status.json")
    if not status:
        return {"state": "idle", "message": "Noch kein Training gestartet"}
    return status


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


# ── Kelly Sweep ──────────────────────────────────────────────────────────


def _trigger_github_action_kelly_sweep(username: str):
    """Triggert den Kelly-Sweep-Workflow auf GitHub Actions."""
    from datetime import datetime
    from app.config_manager import save_json

    initial_status = {
        "state": "running",
        "phase": "dispatching",
        "message": "Kelly Sweep GitHub Action wird gestartet...",
        "started_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "finished_at": None,
        "triggered_by": username,
        "error": None,
        "mode": "github-action-dispatching",
    }
    try:
        save_json("kelly_sweep_status.json", initial_status)
    except Exception:
        pass

    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        log.error("Kelly-Sweep-Trigger: GITHUB_TOKEN fehlt")
        initial_status["state"] = "error"
        initial_status["error"] = "GITHUB_TOKEN fehlt — Workflow nicht ausloesbar"
        initial_status["finished_at"] = datetime.now().isoformat()
        initial_status["updated_at"] = datetime.now().isoformat()
        try:
            save_json("kelly_sweep_status.json", initial_status)
        except Exception:
            pass
        return

    repo = os.environ.get("GITHUB_REPO", "carlosbaumann754-svg/investpilot")
    workflow_file = "kelly_sweep.yml"
    ref = os.environ.get("KELLY_SWEEP_WORKFLOW_REF", "master")
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
            log.info(f"Kelly-Sweep-Workflow getriggert (repo={repo}, ref={ref})")
            initial_status["mode"] = "github-action-running"
            initial_status["message"] = "GitHub Action gestartet, warte auf Runner..."
        else:
            log.error(
                f"Kelly-Sweep-Dispatch HTTP {resp.status_code}: {resp.text[:200]}"
            )
            initial_status["state"] = "error"
            initial_status["error"] = (
                f"workflow_dispatch HTTP {resp.status_code}: {resp.text[:160]}"
            )
            initial_status["finished_at"] = datetime.now().isoformat()
    except Exception as e:
        log.exception("Kelly-Sweep Workflow-Dispatch fehlgeschlagen")
        initial_status["state"] = "error"
        initial_status["error"] = f"dispatch: {type(e).__name__}: {e}"
        initial_status["finished_at"] = datetime.now().isoformat()

    initial_status["updated_at"] = datetime.now().isoformat()
    try:
        save_json("kelly_sweep_status.json", initial_status)
    except Exception:
        pass


@app.post("/api/kelly-sweep/run")
async def api_run_kelly_sweep(
    background_tasks: BackgroundTasks, user=Depends(require_auth)
):
    """Kelly Sweep auf GitHub Actions starten."""
    try:
        from datetime import datetime
        from app.config_manager import load_json, save_json

        STALE_LOCK_MINUTES = 60
        status = load_json("kelly_sweep_status.json") or {}
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
                            f"Stale Kelly-Sweep-Lock ({age_min:.0f} Min alt)"
                        )
                        status["state"] = "error"
                        status["error"] = (
                            f"Lauf abgebrochen (Lock stale nach {age_min:.0f} Min)"
                        )
                        status["finished_at"] = datetime.now().isoformat()
                        status["updated_at"] = datetime.now().isoformat()
                        save_json("kelly_sweep_status.json", status)
                except Exception:
                    pass

            if not is_stale:
                return {
                    "status": "already_running",
                    "message": f"Kelly Sweep laeuft bereits seit {started}",
                    "started_at": started,
                }

        background_tasks.add_task(_trigger_github_action_kelly_sweep, user)

        try:
            from web.security import log_audit
            await log_audit(
                user, "KELLY_SWEEP_STARTED", "GitHub Action dispatched"
            )
        except Exception:
            pass

        return {
            "status": "started",
            "message": "Kelly Sweep laeuft auf GitHub Actions (~5-15 Min).",
            "started_at": datetime.now().isoformat(),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/kelly-sweep/status")
async def api_kelly_sweep_status(user=Depends(require_auth)):
    """Status des letzten/laufenden Kelly-Sweep-Laufs."""
    try:
        from app.persistence import check_and_reload_kelly_sweep_output
        check_and_reload_kelly_sweep_output()
    except Exception:
        pass
    from app.config_manager import load_json
    status = load_json("kelly_sweep_status.json")
    if not status:
        return {"state": "idle", "message": "Noch kein Kelly Sweep gelaufen"}
    return status


@app.get("/api/kelly-sweep")
async def api_kelly_sweep_results(user=Depends(require_auth)):
    """Letzte Kelly Sweep Ergebnisse."""
    try:
        from app.persistence import check_and_reload_kelly_sweep_output
        check_and_reload_kelly_sweep_output()
    except Exception:
        pass
    result = read_json_safe("kelly_sweep_results.json")
    if result:
        return result
    return {"message": "Noch kein Kelly Sweep gelaufen"}


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
# ADMIN: NAMED SNAPSHOTS (Point-in-Time Restore Points)
# ============================================================

@app.post("/api/admin/snapshot")
async def api_admin_create_snapshot(payload: dict, user=Depends(require_auth)):
    """Erzeugt einen benannten Point-in-Time-Snapshot im Backup-Gist.

    Body: {"name": "pre_disk_migration", "note": "optional"}
    """
    try:
        from app.persistence import create_named_snapshot
        name = (payload.get("name") or "").strip()
        note = payload.get("note", "") or ""
        if not name:
            raise HTTPException(status_code=400, detail="name required")
        result = create_named_snapshot(name, note)
        try:
            from web.security import log_audit
            await log_audit(user, "ADMIN_CREATE_SNAPSHOT",
                            f"name={name} result={result.get('success', False)}")
        except Exception:
            pass
        if "error" in result:
            raise HTTPException(status_code=500, detail=result["error"])
        return {"status": "ok", **result}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/admin/snapshot/list")
async def api_admin_list_snapshots(user=Depends(require_auth)):
    """Listet alle Named-Snapshots im Backup-Gist (neueste zuerst)."""
    try:
        from app.persistence import list_named_snapshots
        snapshots = list_named_snapshots()
        return {"status": "ok", "count": len(snapshots), "snapshots": snapshots}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/snapshot/restore")
async def api_admin_restore_snapshot(payload: dict, user=Depends(require_auth)):
    """Stellt einen Named-Snapshot wieder her.

    Body: {"filename": "snapshot_pre_disk_migration_20260409_120000.json"}
    """
    try:
        from app.persistence import restore_named_snapshot
        filename = (payload.get("filename") or "").strip()
        if not filename:
            raise HTTPException(status_code=400, detail="filename required")
        result = restore_named_snapshot(filename)
        try:
            from web.security import log_audit
            await log_audit(user, "ADMIN_RESTORE_SNAPSHOT",
                            f"filename={filename} result={result.get('success', False)}")
        except Exception:
            pass
        if "error" in result:
            raise HTTPException(status_code=500, detail=result["error"])
        return {"status": "ok", **result}
    except HTTPException:
        raise
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
        client = get_broker(config, readonly=True)
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
            "fear_greed_index": ctx.get("fear_greed_index"),
            "fear_greed_class": ctx.get("fear_greed_class"),
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
# NEWS SOURCES STATUS (Finnhub / Anthropic / VADER / yfinance)
# ============================================================

@app.get("/api/news-sources")
async def api_news_sources(user=Depends(require_auth)):
    """Welche Sentiment-/News-Quellen sind aktuell live?"""
    try:
        from app import sentiment as _sent
        status = _sent.get_sources_status()
        # Ermittle primaere Quelle (die erste aktive in Prioritaetsreihenfolge)
        priority = ["finnhub", "anthropic_haiku", "vader", "yfinance"]
        primary = next((s for s in priority if status.get(s)), "none")
        labels = {
            "finnhub": "Finnhub (News + Sentiment API)",
            "anthropic_haiku": "Claude Haiku LLM",
            "vader": "VADER (lokal)",
            "yfinance": "Yahoo Finance (Fallback)",
            "none": "Keine Quelle aktiv",
        }
        return {
            "sources": status,
            "primary": primary,
            "primary_label": labels.get(primary, primary),
        }
    except Exception as e:
        return {"error": str(e)}


# ============================================================
# v12 FEATURE STATUS (Universe, Kelly, Meta-Labeler, Time-Stop, ...)
# ============================================================

@app.get("/api/v12-status")
async def api_v12_status(user=Depends(require_auth)):
    """Kompakter Status aller v12-Features fuer das Dashboard.

    Liefert je Section:
      - enabled: bool
      - kompakte Kennzahlen (Konfig + laufzeit-Metriken)
    """
    try:
        from datetime import datetime, timedelta
        config = load_config()

        # --- Universe ---
        try:
            from app.market_scanner import ASSET_UNIVERSE
            total_universe = len(ASSET_UNIVERSE)
        except Exception:
            total_universe = None
        disabled = list(config.get("disabled_symbols") or [])
        active_universe = (total_universe - len(disabled)) if total_universe else None

        # --- Universe Health (letzter yfinance Download-Report) ---
        # Die Datei wird vom Backtester (app/backtester.py) geschrieben:
        # Keys: generated_at, total_requested, ok_count, error_count, report
        # (report ist dict {symbol: {status: "ok" | "insufficient_data" | ...}})
        uh = read_json_safe("universe_health.json") or {}
        uh_report = uh.get("report") or uh.get("symbols") or {}  # fallback auf alten Key
        uh_bad = [
            s for s, d in uh_report.items()
            if isinstance(d, dict) and d.get("status") not in (None, "ok")
        ]
        uh_summary = {
            "timestamp": uh.get("generated_at") or uh.get("timestamp"),
            "ok": uh.get("ok_count"),
            "total": uh.get("total_requested"),
            "errors": uh.get("error_count"),
        }

        # --- Kelly Sizing ---
        kelly_cfg = config.get("kelly_sizing", {}) or {}

        # --- Meta-Labeler ---
        meta_cfg = config.get("meta_labeling", {}) or {}
        meta_info = read_json_safe("meta_model.json") or {}
        shadow_log = read_json_safe("meta_labeling_shadow.json") or []
        shadow_count = len(shadow_log) if isinstance(shadow_log, list) else 0
        min_trades = int(meta_cfg.get("min_trades_to_activate", 50) or 50)
        min_prec = float(meta_cfg.get("min_precision_to_activate", 0.65) or 0.65) * 100
        meta_precision = meta_info.get("precision")
        meta_progress_pct = min(100, round(shadow_count / max(min_trades, 1) * 100)) \
            if min_trades else 0
        meta_ready_to_activate = (
            shadow_count >= min_trades
            and meta_precision is not None
            and meta_precision >= min_prec
        )

        # --- Time-Stop ---
        ts_cfg = config.get("time_stop", {}) or {}
        trades = read_json_safe("trade_history.json") or []
        time_stop_exits = 0
        try:
            cutoff = (datetime.now() - timedelta(days=7)).isoformat()
            for t in trades:
                if not isinstance(t, dict):
                    continue
                reason = (t.get("exit_reason") or t.get("action") or "")
                if "time_stop" in str(reason).lower():
                    ts = t.get("timestamp") or t.get("exit_date") or ""
                    if ts >= cutoff:
                        time_stop_exits += 1
        except Exception:
            pass

        # --- VIX Term Structure / Hedging / Regime-Strategies (flags only) ---
        vts_cfg = config.get("vix_term_structure", {}) or {}
        hedge_cfg = config.get("hedging", {}) or {}
        regime_cfg = config.get("regime_strategies", {}) or {}

        # --- Trailing SL (lives in leverage section) ---
        lev = config.get("leverage", {}) or {}

        return {
            "universe": {
                "total": total_universe,
                "active": active_universe,
                "disabled_count": len(disabled),
                "disabled_symbols": disabled,
                "health_last_update": uh_summary.get("timestamp"),
                "health_ok": uh_summary.get("ok"),
                "health_bad": uh_bad,
            },
            "kelly_sizing": {
                "enabled": bool(kelly_cfg.get("enabled")),
                "half_kelly": bool(kelly_cfg.get("half_kelly")),
                "max_fraction": kelly_cfg.get("max_fraction"),
                "min_trades": kelly_cfg.get("min_trades"),
                "min_position_usd": kelly_cfg.get("min_position_usd"),
            },
            "meta_labeler": {
                "enabled": bool(meta_cfg.get("enabled")),
                "shadow_mode": bool(meta_cfg.get("shadow_mode")),
                "trained": bool(meta_info),
                "trained_at": meta_info.get("trained_at"),
                "precision": meta_precision,
                "recall": meta_info.get("recall"),
                "f1": meta_info.get("f1"),
                "samples_total": meta_info.get("samples_total"),
                "shadow_log_size": shadow_count,
                "min_trades_to_activate": min_trades,
                "min_precision_to_activate": min_prec,
                "progress_pct": meta_progress_pct,
                "ready_to_activate": meta_ready_to_activate,
            },
            "time_stop": {
                "enabled": bool(ts_cfg.get("enabled")),
                "max_days_stale": ts_cfg.get("max_days_stale"),
                "stale_pnl_threshold_pct": ts_cfg.get("stale_pnl_threshold_pct"),
                "min_days_open": ts_cfg.get("min_days_open"),
                "exits_last_7d": time_stop_exits,
            },
            "vix_term_structure": {
                "enabled": bool(vts_cfg.get("enabled")),
                "panic_dip_override": bool(vts_cfg.get("panic_dip_override_enabled")),
                "panic_dip_multiplier": vts_cfg.get("panic_dip_position_multiplier"),
                "panic_dip_ratio": vts_cfg.get("panic_dip_ratio"),
            },
            "hedging": {
                "enabled": bool(hedge_cfg.get("enabled")),
                "bear_position_multiplier": hedge_cfg.get("bear_position_multiplier"),
                "defensive_sectors": hedge_cfg.get("defensive_sectors") or [],
            },
            "regime_strategies": {
                "enabled": bool(regime_cfg.get("enabled")),
                "bull_momentum_boost": regime_cfg.get("bull_momentum_boost"),
                "sideways_mr_boost": regime_cfg.get("sideways_mr_boost"),
                "bear_non_defensive_penalty": regime_cfg.get("bear_non_defensive_penalty"),
            },
            "trailing_sl": {
                "enabled": bool(lev.get("trailing_sl_enabled")),
                "activation_pct": lev.get("trailing_sl_activation_pct"),
                "trail_pct": lev.get("trailing_sl_pct"),
            },
        }
    except Exception as e:
        log.error(f"v12-status Fehler: {e}", exc_info=True)
        return {"error": str(e)}


@app.get("/api/universe-health")
async def api_universe_health(user=Depends(require_auth)):
    """Rohdaten aus universe_health.json (yfinance-Download-Status pro Symbol)."""
    data = read_json_safe("universe_health.json") or {}
    return data


class DisabledSymbolsUpdate(BaseModel):
    disabled_symbols: list[str]


@app.put("/api/disabled-symbols")
async def api_update_disabled_symbols(
    update: DisabledSymbolsUpdate,
    user=Depends(require_auth),
):
    """Universe-Filter pflegen. Ueberschreibt die komplette Liste."""
    try:
        async with _CONFIG_WRITE_LOCK:
            config = load_config()
            new_list = sorted({s.strip().upper() for s in update.disabled_symbols if s and s.strip()})
            old_list = sorted(config.get("disabled_symbols") or [])
            config["disabled_symbols"] = new_list
            save_config(config)

        # Audit log
        try:
            from web.security import log_audit
            added = set(new_list) - set(old_list)
            removed = set(old_list) - set(new_list)
            parts = []
            if added:
                parts.append(f"+{','.join(sorted(added))}")
            if removed:
                parts.append(f"-{','.join(sorted(removed))}")
            await log_audit(
                user,
                "DISABLED_SYMBOLS_CHANGE",
                " ".join(parts) if parts else "no-op",
            )
        except Exception:
            pass

        return {
            "status": "ok",
            "disabled_symbols": new_list,
            "count": len(new_list),
        }
    except Exception as e:
        log.error(f"disabled-symbols Update Fehler: {e}", exc_info=True)
        raise HTTPException(500, str(e))


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

        # Trade-History anreichern: instrument_id -> symbol/name (sonst sieht
        # Claude nur anonyme IDs und kann die Frage nicht beantworten)
        raw_history = read_json_safe("trade_history.json") or []
        enrich_with_asset_meta(raw_history)

        # Daten sammeln
        context_data = {
            "trade_history": raw_history,
            "decision_log": read_json_safe("decision_log.json") or [],
            "brain_state": read_json_safe("brain_state.json") or {},
            "risk_state": read_json_safe("risk_state.json") or {},
            "scanner_state": read_json_safe("scanner_state.json") or {},
        }

        # Portfolio live abfragen
        try:
            client = get_broker(config, readonly=True)
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


# ============================================================
# v31-v35 Insider-Signal Endpoints
# ============================================================
# Daten-Layer fuer "CEOWatcher-Aequivalent". Alle Endpoints sind
# READ-ONLY und unterliegen keinem Auth (gleiche Policy wie /api/portfolio).
# Aktivierung der Insider-Logik im Bot selbst weiterhin via Config-Flag
# scanner.insider_signal_enabled (DEFAULT FALSE).

@app.get("/api/insider/scores")
def api_insider_scores():
    """Insider-Score fuer alle Symbole im aktuellen Bot-Universum.

    Liefert pro Symbol: base_score (v31), full_score (v32+v33 alle Filter aktiv),
    delta. So sehen wir "was wuerde der Bot mit Filtern aktiv anders bewerten".
    """
    try:
        from app.insider_signals import compute_insider_score
        from app import finnhub_client
        from app.market_scanner import ASSET_UNIVERSE
    except Exception as e:
        return {"error": f"import: {e}"}

    if not finnhub_client.is_available():
        return {"error": "Finnhub nicht verfuegbar — API-Key fehlt"}

    out = []
    # Nur stocks/etf — Crypto/Forex haben keine Insider
    for sym, meta in ASSET_UNIVERSE.items():
        cls = (meta.get("class") or "").lower()
        if cls not in ("stocks", "stock", "etf", "etfs"):
            continue
        try:
            txs = finnhub_client.fetch_insider_transactions(sym)
        except Exception:
            continue
        if not txs:
            continue
        base = compute_insider_score(sym, transactions=txs)
        full = compute_insider_score(
            sym, transactions=txs,
            quality_filter=True, detect_novelty=True, detect_contrarian=False,
        )
        out.append({
            "symbol": sym,
            "name": meta.get("name", sym),
            "base_score_v31": base,
            "full_score_v32_v33": full,
            "delta": full - base,
            "n_transactions": len(txs),
        })

    out.sort(key=lambda x: x["full_score_v32_v33"], reverse=True)
    return {"updated_at": __import__("datetime").datetime.utcnow().isoformat(), "scores": out}


@app.get("/api/insider/discovery")
def api_insider_discovery():
    """Top-Kandidaten ausserhalb unseres Universums mit High-Conviction Cluster-Buys."""
    try:
        from app.insider_discovery import get_latest_discovery
        return get_latest_discovery()
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/insider/discovery/run")
def api_insider_discovery_run():
    """Manueller Trigger fuer einen Discovery-Scan (sonst taeglich via Scheduler)."""
    try:
        from app.insider_discovery import run_discovery
        return run_discovery(min_score=2, max_per_run=80)
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/insider/top-insiders")
def api_insider_top():
    """Top-N Insider nach historischer Hit-Rate (v34 — anfangs leer)."""
    try:
        from app.insider_tracker import get_top_insiders
        return {"top": get_top_insiders(n=15, min_trades=3)}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/insider/shadow-report")
def api_insider_shadow_report():
    """Shadow-Report: vergleicht aktuelle Bot-Trade-Kandidaten mit Insider-Filter.

    Was haette der Bot mit aktivem Insider-Signal anders entschieden?
    - Aktuelle Top-BUY-Kandidaten aus Brain-State
    - Insider-Score (v32+v33) pro Symbol
    - Empfehlung: KEEP (score >= 0), DOWNGRADE (score < 0), BOOST (score >= 3)
    """
    try:
        from app.insider_signals import compute_insider_score
        from app import finnhub_client
        from app.config_manager import get_data_path
    except Exception as e:
        return {"error": f"import: {e}"}

    if not finnhub_client.is_available():
        return {"error": "Finnhub nicht verfuegbar"}

    # Brain-State lesen — letzte Scanner-Top-Picks
    brain_path = get_data_path("brain_state.json")
    if not brain_path.exists():
        return {"error": "brain_state.json nicht vorhanden"}
    try:
        import json
        brain = json.loads(brain_path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"error": f"brain_state read: {e}"}

    # Brain-State hat instrument_scores als dict {symbol: {score, ...}} ODER
    # scanner_results/top_signals als Liste — flexibel parsen
    candidates = []
    inst_scores = brain.get("instrument_scores") or {}
    if isinstance(inst_scores, dict):
        for sym, info in inst_scores.items():
            if isinstance(info, dict):
                candidates.append({"symbol": sym, "score": info.get("score") or info.get("total_score")})
        candidates.sort(key=lambda x: (x.get("score") or -999), reverse=True)
    if not candidates:
        candidates = brain.get("scanner_results") or brain.get("top_signals") or []
    if not candidates:
        return {"error": "Keine Scanner-Kandidaten im Brain-State", "brain_keys": list(brain.keys())}

    report = []
    for cand in candidates[:20]:
        sym = cand.get("symbol") or cand.get("ticker")
        if not sym:
            continue
        try:
            txs = finnhub_client.fetch_insider_transactions(sym)
            score = compute_insider_score(
                sym, transactions=txs,
                quality_filter=True, detect_novelty=True, detect_contrarian=False,
            )
        except Exception:
            score = 0
        if score >= 3:
            recommendation = "BOOST"
        elif score < 0:
            recommendation = "DOWNGRADE"
        else:
            recommendation = "KEEP"
        report.append({
            "symbol": sym,
            "scanner_score": cand.get("score") or cand.get("total_score"),
            "insider_score": score,
            "insider_recommendation": recommendation,
        })
    return {"updated_at": __import__("datetime").datetime.utcnow().isoformat(),
            "candidates": report}
