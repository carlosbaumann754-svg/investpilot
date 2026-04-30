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

# v37t: Cash-Tolerance ist jetzt PROZENTUAL gestaffelt + Floor-Wert.
# Vorher: fix $10 -> bei $880k Konto = 0.001% = unrealistisch streng,
# spammt Alerts wegen Rundungen + Slippage + Brain-Snapshot-Latenz.
# Jetzt: Threshold = max(CASH_TOLERANCE_FLOOR_USD, % von Konto).
# Per Default 0.5% - bei $880k = $4400 Schwelle = nur echte Drifts melden.
CASH_TOLERANCE_USD = 10.0          # Legacy-Konstante, fallback bei kleinen Konten
CASH_TOLERANCE_FLOOR_USD = 50.0    # Mindest-Schwelle (Schutz bei sehr kleinen Konten)
CASH_TOLERANCE_PCT_DEFAULT = 0.5   # 0.5% des Konto-Cash als Default-Threshold


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


def reconcile(lookback_hours: int = 24,
              cash_tolerance_pct: float = CASH_TOLERANCE_PCT_DEFAULT,
              missed_fill_lookback_hours: int = 24) -> dict:
    """Hauptlogik. Returns Dict mit Diffs/Status.

    v37t/t+: zwei separate Lookback-Fenster:
    - lookback_hours: fuer Phantom-Detection (Default 24h, Cron 720h=30d)
      -> "kennt Bot diese Position ueberhaupt?"
    - missed_fill_lookback_hours: fuer Missed-Fill-Detection (Default 24h)
      -> "Bot loggte gerade einen Trade, hat IBKR die Execution?"
    Vorher waren beide gleich -> bei 720h-Lookback wurden ALLE 30-Tage-
    Trades als MISSED_FILL gemeldet weil IBKR-Session nur Session-Executions
    kennt.

    cash_tolerance_pct default 0.5%, mit FLOOR 50$ Mindestschwelle.
    """
    cutoff_pos = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    cutoff_fill = datetime.now(timezone.utc) - timedelta(hours=missed_fill_lookback_hours)

    bot_history, bot_cash = load_bot_state()
    ibkr = get_ibkr_state()

    # Filter Bot-trades — zwei getrennte Listen fuer die zwei Checks
    recent_bot = []           # fuer Phantom-Detection (langes Fenster)
    recent_for_fill = []      # fuer MISSED_FILL (kurzes Fenster)
    for t in bot_history:
        ts = t.get("timestamp")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.astimezone(timezone.utc)
            if dt >= cutoff_pos:
                recent_bot.append(t)
            if dt >= cutoff_fill:
                recent_for_fill.append(t)
        except Exception:
            continue

    drifts = []

    # 1. Cash-Drift mit prozentualer Schwelle
    cash_diff = abs(ibkr["cash"] - bot_cash)
    cash_ref = max(ibkr["cash"], bot_cash)  # groesserer Wert als Basis
    threshold = max(
        CASH_TOLERANCE_FLOOR_USD,
        cash_ref * (cash_tolerance_pct / 100.0)
    )
    if bot_cash > 0 and cash_diff > threshold:
        drifts.append({
            "type": "CASH_DRIFT",
            "bot_cash": round(bot_cash, 2),
            "ibkr_cash": round(ibkr["cash"], 2),
            "diff_usd": round(cash_diff, 2),
            "diff_pct": round((cash_diff / cash_ref) * 100, 3) if cash_ref else 0,
            "threshold_usd": round(threshold, 2),
            "tolerance_pct_setting": cash_tolerance_pct,
        })

    # 2. Phantom-Positionen (IBKR hat, Bot kennt nicht)
    # v37t-Fix: Bot schreibt aktuell "SCANNER_BUY" (nicht nur "BUY"). Plus es
    # gibt OPEN/BUY/scanner_buy/buy Variants. Match jetzt alles was BUY-aehnlich ist.
    BUY_LIKE_ACTIONS = {
        "BUY", "OPEN", "SCANNER_BUY",
        "buy", "open", "scanner_buy",
    }
    bot_known_symbols = {
        t.get("symbol")
        for t in recent_bot
        if t.get("action") in BUY_LIKE_ACTIONS
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
    # v37t+: nur recent_for_fill (24h) statt recent_bot (30d), sonst werden
    # alle historischen Trades als MISSED_FILL gemeldet (IBKR-Session zeigt
    # nur kurze Execution-Historie).
    ibkr_exec_symbols = {(e["symbol"], e["side"]) for e in ibkr["executions"]}
    for t in recent_for_fill:
        action = t.get("action", "").upper()
        # v37t-Fix: SCANNER_BUY/SELL und Compound-Actions wie STOP_LOSS_CLOSE matchen
        if action in ("BUY", "OPEN", "SCANNER_BUY"):
            ib_side = "BOT"
        elif (action in ("SELL", "CLOSE", "TP", "SL", "SCANNER_SELL")
              or "CLOSE" in action):
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
        "missed_fill_lookback_hours": missed_fill_lookback_hours,
        "bot_cash": round(bot_cash, 2),
        "ibkr_cash": round(ibkr["cash"], 2),
        "ibkr_equity": round(ibkr["equity"], 2),
        "ibkr_positions_count": len(ibkr["positions"]),
        "bot_position_lookback_trades_count": len(recent_bot),
        "bot_fill_lookback_trades_count": len(recent_for_fill),
        "ibkr_recent_executions_count": len(ibkr["executions"]),
        "drifts": drifts,
        "status": "OK" if not drifts else "DRIFT_DETECTED",
    }


def maybe_alert(report: dict) -> None:
    """Multi-Channel-Alert (Pushover/Telegram/Discord) wenn Drift gefunden.

    v37k: vorher Telegram-only (send_telegram direkt), jetzt via send_alert()
    Dispatcher → routet automatisch ueber alle aktivierten Channels (Pushover
    + Telegram + Discord). Drift = WARNING-Level (rotes Banner in Pushover).
    """
    if report["status"] == "OK":
        return
    try:
        from app.alerts import send_alert
        msg = f"IBKR Reconciliation Drift — {len(report['drifts'])} Probleme:"
        for d in report["drifts"][:5]:
            msg += f"\n• {d['type']}: {d.get('symbol', '')} {d.get('comment', '')[:80]}"
        send_alert(msg, level="WARNING")
        log.info("Reconciliation-Alert versendet (Multi-Channel)")
    except Exception as e:
        log.warning("Alert-Dispatch fehlgeschlagen: %s", e)


def main():
    parser = argparse.ArgumentParser(description="IBKR Reconciliation")
    parser.add_argument("--lookback-hours", type=int, default=24,
                        help="Lookback fuer recent Bot-Trades. Default 24h "
                             "(Cron sollte 720 = 30 Tage nutzen fuer Position-"
                             "Lookback).")
    parser.add_argument("--cash-tolerance-pct", type=float,
                        default=CASH_TOLERANCE_PCT_DEFAULT,
                        help="Prozentualer Cash-Drift-Threshold. Default 0.5%% "
                             "(bei $880k Konto = $4400 Schwelle). Floor: $50.")
    parser.add_argument("--missed-fill-lookback-hours", type=int, default=24,
                        help="Lookback fuer MISSED_FILL-Detection. Default 24h. "
                             "Sollte kurz bleiben weil IBKR-Session-Executions "
                             "nur kurze Historie haben.")
    parser.add_argument("--alert", action="store_true",
                        help="Bei Drift Multi-Channel-Alert ausloesen")
    parser.add_argument("--json", action="store_true",
                        help="Output als reines JSON")
    args = parser.parse_args()

    if not args.json:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    try:
        report = reconcile(
            lookback_hours=args.lookback_hours,
            cash_tolerance_pct=args.cash_tolerance_pct,
            missed_fill_lookback_hours=args.missed_fill_lookback_hours,
        )
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
        print(f"Bot Recent Trades ({args.lookback_hours}h Pos / {args.missed_fill_lookback_hours}h Fill): "
              f"{report['bot_position_lookback_trades_count']} pos / "
              f"{report['bot_fill_lookback_trades_count']} fill")
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
