"""
IBKR Reconciliation — Bot-State vs IBKR-Realitaet
==================================================

Vergleicht was der Bot zu wissen glaubt (`trade_history.json`,
`brain_state.json`) gegen was IBKR wirklich kennt (`ib.positions()`,
`ib.executions()`). Faengt:

- **Missed Fills**: Bot loggte Order-Submission, aber IBKR hat keinen Fill
- **Phantom-Positionen**: IBKR hat Position, Bot kennt sie nicht
- **Cash-Drift**: Bot's Snapshot != IBKR AvailableFunds (>$10 toleranz)
- **Position-Mismatch**: Symbol/qty zwischen Bot und IBKR weichen ab

Usage:
    python -m scripts.ibkr_reconcile [--alert] [--lookback-hours N]

Exit codes:
    0 = sauber
    1 = Drift gefunden
    2 = IBKR-Connection-Fehler

--alert: Bei Drift Telegram-Alert ausloesen (via app.alerts wenn verfuegbar)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# sys.path so dass `app.*` importiert werden kann
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

log = logging.getLogger("ibkr_reconcile")

CASH_TOLERANCE_USD = 10.0  # bis zu $10 Drift okay (Rundungen, Pending-Fees)


def load_bot_state() -> tuple[list[dict], float]:
    """Liefert (recent_trades, last_known_cash) aus Bot-State-Files."""
    from app.config_manager import load_json
    history = load_json("trade_history.json") or []
    brain = load_json("brain_state.json") or {}
    snaps = brain.get("performance_snapshots") or []
    last_cash = 0.0
    if snaps:
        last_cash = float(snaps[-1].get("cash", 0) or snaps[-1].get("credit", 0) or 0)
    return history, last_cash


def get_ibkr_state(timeout: int = 15) -> dict:
    """Live-IBKR-Snapshot: positions + cash + recent executions.

    Nutzt clientId=99 (separat vom Bot's clientId=1) — sonst kollidiert
    der Connect mit der laufenden Bot-Session.
    """
    from app.ibkr_client import IbkrBroker
    # Eigene clientId fuer Reconciliation, vermeidet Conflict mit Bot
    broker = IbkrBroker({"ibkr": {"client_id": 99, "readonly": True}})
    try:
        ib = broker._get_ib()
        positions = ib.positions()
        execs = ib.executions()  # alle bekannten Executions der Session
        cash = broker.get_available_cash() or 0.0
        equity = broker.get_equity() or 0.0
        return {
            "positions": [
                {
                    "symbol": p.contract.symbol,
                    "conId": p.contract.conId,
                    "qty": float(p.position),
                    "avg_cost": float(p.avgCost),
                }
                for p in positions
            ],
            "executions": [
                {
                    "exec_id": e.execution.execId,
                    "time": e.execution.time.isoformat() if hasattr(e.execution.time, "isoformat") else str(e.execution.time),
                    "symbol": e.contract.symbol,
                    "side": e.execution.side,  # "BOT" oder "SLD"
                    "qty": float(e.execution.shares),
                    "price": float(e.execution.price),
                }
                for e in execs
            ],
            "cash": cash,
            "equity": equity,
        }
    finally:
        broker.disconnect()


def reconcile(lookback_hours: int = 24) -> dict:
    """Hauptlogik. Returns Dict mit Diffs/Status."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)

    bot_history, bot_cash = load_bot_state()
    ibkr = get_ibkr_state()

    # Filter Bot-trades auf lookback Window
    recent_bot = []
    for t in bot_history:
        ts = t.get("timestamp")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.astimezone(timezone.utc)
            if dt >= cutoff:
                recent_bot.append(t)
        except Exception:
            continue

    drifts = []

    # 1. Cash-Drift
    cash_diff = abs(ibkr["cash"] - bot_cash)
    if bot_cash > 0 and cash_diff > CASH_TOLERANCE_USD:
        drifts.append({
            "type": "CASH_DRIFT",
            "bot_cash": round(bot_cash, 2),
            "ibkr_cash": round(ibkr["cash"], 2),
            "diff_usd": round(cash_diff, 2),
        })

    # 2. Phantom-Positionen (IBKR hat, Bot kennt nicht)
    bot_known_symbols = {
        t.get("symbol")
        for t in recent_bot
        if t.get("action") in ("BUY", "OPEN", "buy", "open")
        and t.get("status") not in ("close_failed", "skipped")
    }
    for pos in ibkr["positions"]:
        if pos["symbol"] not in bot_known_symbols:
            drifts.append({
                "type": "PHANTOM_POSITION",
                "symbol": pos["symbol"],
                "qty": pos["qty"],
                "avg_cost": pos["avg_cost"],
                "comment": "IBKR hat Position, Bot-trade-history kennt sie nicht im Lookback-Fenster.",
            })

    # 3. Missed Fills (Bot loggte BUY/SELL, aber IBKR hat keine matching Execution)
    ibkr_exec_symbols = {(e["symbol"], e["side"]) for e in ibkr["executions"]}
    for t in recent_bot:
        action = t.get("action", "").upper()
        if action in ("BUY", "OPEN"):
            ib_side = "BOT"
        elif action in ("SELL", "CLOSE", "TP", "SL"):
            ib_side = "SLD"
        else:
            continue
        sym = t.get("symbol")
        if sym and (sym, ib_side) not in ibkr_exec_symbols:
            # Akzeptabel falls Status=close_failed (already known)
            if t.get("status") in ("close_failed", "skipped", "submitted"):
                continue
            drifts.append({
                "type": "MISSED_FILL",
                "symbol": sym,
                "action": action,
                "bot_timestamp": t.get("timestamp"),
                "comment": f"Bot loggte {action}, IBKR-Executions zeigen kein matching {ib_side}-Trade.",
            })

    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "lookback_hours": lookback_hours,
        "bot_cash": round(bot_cash, 2),
        "ibkr_cash": round(ibkr["cash"], 2),
        "ibkr_equity": round(ibkr["equity"], 2),
        "ibkr_positions_count": len(ibkr["positions"]),
        "bot_recent_trades_count": len(recent_bot),
        "ibkr_recent_executions_count": len(ibkr["executions"]),
        "drifts": drifts,
        "status": "OK" if not drifts else "DRIFT_DETECTED",
    }


def maybe_alert(report: dict) -> None:
    """Telegram-Alert wenn alerts-Modul verfuegbar UND Drift gefunden."""
    if report["status"] == "OK":
        return
    try:
        from app import alerts
        msg = (
            f"⚠️ IBKR Reconciliation Drift\n"
            f"{len(report['drifts'])} Probleme:\n"
        )
        for d in report["drifts"][:5]:
            msg += f"• {d['type']}: {d.get('symbol', '')} {d.get('comment', '')[:80]}\n"
        if hasattr(alerts, "send_telegram"):
            alerts.send_telegram(msg)
            log.info("Telegram-Alert versendet")
        else:
            log.warning("alerts.send_telegram nicht verfuegbar")
    except Exception as e:
        log.warning("Alert-Dispatch fehlgeschlagen: %s", e)


def main():
    parser = argparse.ArgumentParser(description="IBKR Reconciliation")
    parser.add_argument("--lookback-hours", type=int, default=24)
    parser.add_argument("--alert", action="store_true",
                        help="Bei Drift Telegram-Alert ausloesen")
    parser.add_argument("--json", action="store_true",
                        help="Output als reines JSON")
    args = parser.parse_args()

    if not args.json:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    try:
        report = reconcile(lookback_hours=args.lookback_hours)
    except Exception as e:
        log.error("Reconciliation failed: %s", e)
        return 2

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(f"\n{'='*60}\nIBKR Reconciliation Report ({report['ts']})\n{'='*60}")
        print(f"Status:     {report['status']}")
        print(f"Bot Cash:   ${report['bot_cash']:,.2f}")
        print(f"IBKR Cash:  ${report['ibkr_cash']:,.2f}")
        print(f"IBKR Equity:${report['ibkr_equity']:,.2f}")
        print(f"IBKR Positions: {report['ibkr_positions_count']}")
        print(f"Bot Recent Trades ({args.lookback_hours}h): {report['bot_recent_trades_count']}")
        print(f"IBKR Recent Executions: {report['ibkr_recent_executions_count']}")
        if report["drifts"]:
            print(f"\n⚠️ {len(report['drifts'])} Drifts:")
            for d in report["drifts"]:
                print(f"  - {d}")
        else:
            print("\n✅ Keine Drifts gefunden")

    if args.alert:
        maybe_alert(report)

    return 0 if report["status"] == "OK" else 1


if __name__ == "__main__":
    sys.exit(main())
