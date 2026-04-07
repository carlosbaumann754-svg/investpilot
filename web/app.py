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


def _run_optimizer_background(username: str):
    """Background-Worker fuer Optimizer-Lauf. Schreibt Status-Datei."""
    from datetime import datetime
    from app.config_manager import save_json

    status = {
        "state": "running",
        "started_at": datetime.now().isoformat(),
        "finished_at": None,
        "triggered_by": username,
        "action": None,
        "error": None,
    }
    try:
        save_json("optimizer_status.json", status)
    except Exception:
        pass

    try:
        from app.optimizer import run_weekly_optimization
        result = run_weekly_optimization()
        status["state"] = "done"
        status["action"] = result.get("action", "unknown") if isinstance(result, dict) else "unknown"
    except Exception as e:
        log.exception("Optimizer Background-Lauf fehlgeschlagen")
        status["state"] = "error"
        status["error"] = str(e)

    status["finished_at"] = datetime.now().isoformat()
    try:
        save_json("optimizer_status.json", status)
    except Exception:
        pass


@app.post("/api/optimizer/run")
async def api_run_optimizer(background_tasks: BackgroundTasks, user=Depends(require_auth)):
    """Weekly Optimization im Hintergrund starten (non-blocking, vermeidet Render 100s Proxy-Timeout)."""
    try:
        from datetime import datetime
        from app.config_manager import load_json

        # Abbruch wenn bereits ein Lauf aktiv ist
        status = load_json("optimizer_status.json") or {}
        if status.get("state") == "running":
            started = status.get("started_at")
            return {
                "status": "already_running",
                "message": f"Optimizer laeuft bereits seit {started}",
                "started_at": started,
            }

        background_tasks.add_task(_run_optimizer_background, user)

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
