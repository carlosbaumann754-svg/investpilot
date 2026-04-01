"""
InvestPilot - FastAPI Web Dashboard
REST API + Mobile-First Frontend fuer Trading-Steuerung.
"""

import os
import sys
import logging
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
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

from web.auth import authenticate_user
from web.security import security_middleware, record_failed_login, log_audit as _log_audit

log = logging.getLogger("WebApp")

app = FastAPI(title="InvestPilot Dashboard", version="1.0.0")

# Security Middleware registrieren
app.middleware("http")(security_middleware)

# Static files
STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


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

@app.post("/api/auth/login")
async def login(req: LoginRequest, request: Request):
    """Login mit Username/Password, gibt JWT Token zurueck."""
    ip = request.client.host if request.client else "unknown"
    token = authenticate_user(req.username, req.password)

    if not token:
        record_failed_login(ip, req.username)
        raise HTTPException(status_code=401, detail="Falscher Username oder Passwort")

    await _log_audit(req.username, "LOGIN_SUCCESS", f"Login von {ip}", "INFO", ip)
    return {"token": token, "username": req.username}


# ============================================================
# FRONTEND
# ============================================================

@app.get("/")
async def root():
    return FileResponse(str(STATIC_DIR / "index.html"))

@app.get("/login")
async def login_page():
    return FileResponse(str(STATIC_DIR / "login.html"))


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
