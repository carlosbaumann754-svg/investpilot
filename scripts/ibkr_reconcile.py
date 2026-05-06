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


# ============================================================
# v37w: Accept-Phantom-List (Smart-Reconcile)
# ============================================================
ACCEPTED_PHANTOMS_FILE = "reconcile_accepted_phantoms.json"


def _load_accepted_phantoms() -> set[str]:
    """Liste der Symbole die als 'Initial-Position akzeptiert' markiert sind.

    Format der Datei (data/reconcile_accepted_phantoms.json):
        {
          "accepted_symbols": ["CPER", "USO"],
          "audit": [
            {"symbol": "CPER", "accepted_at": "...", "reason": "..."},
            ...
          ]
        }
    """
    try:
        from app.config_manager import load_json
        data = load_json(ACCEPTED_PHANTOMS_FILE) or {}
        return set(data.get("accepted_symbols", []) or [])
    except Exception as e:
        log.debug(f"Accept-Phantom-Liste nicht ladbar: {e}")
        return set()


def _add_accepted_phantom(symbol: str, reason: str = "manual") -> None:
    """Fuegt ein Symbol zur Accept-List hinzu (idempotent)."""
    from app.config_manager import load_json, save_json
    from datetime import datetime, timezone
    data = load_json(ACCEPTED_PHANTOMS_FILE) or {}
    accepted = set(data.get("accepted_symbols", []) or [])
    accepted.add(symbol)
    data["accepted_symbols"] = sorted(accepted)
    audit = data.setdefault("audit", [])
    audit.append({
        "symbol": symbol,
        "accepted_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
    })
    save_json(ACCEPTED_PHANTOMS_FILE, data)


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
    # Eigene clientId fuer Reconciliation, vermeidet Conflict mit Bot.
    # v37cm (02.05.): timeout auf 30s erhoeht (Default 15s war zu eng bei
    # busy IB-Gateway nach Trade-Tagen — connectAsync wartet intern auf
    # reqExecutionsAsync, das bei vielen Fills laenger dauern kann.
    # Symptom war seit dem 02.05.: jeder Reconcile-Cron Error mit leerer
    # TimeoutError-Message).
    broker = IbkrBroker({"ibkr": {"client_id": 99, "readonly": True, "timeout": 60}})
    try:
        ib = broker._get_ib()
        positions = ib.positions()
        # v37cl (01.05.2026): ib_insync API-Drift — ib.executions() returnt
        # jetzt rohe Execution-Objekte ohne .contract/.commissionReport-
        # Wrapper. ib.fills() liefert Fill-Objekte mit voller Struktur
        # (.execution + .contract + .commissionReport) wie der Code erwartet.
        # Fehler war seit Tagen: "'Execution' object has no attribute 'execution'".
        execs = ib.fills()  # alle bekannten Fills (= Execution + Contract + CommReport)
        # v37aa: pending orders fuer MISSED_FILL-Filter (heute morgen 30.04.
        # Pushover-Alarm: ROKU SELL submitted 09:10 CEST aber IBKR fillte erst
        # 10:15 Pre-Market — Reconcile-Lauf 10:13 dazwischen meldete MISSED_FILL
        # obwohl Order pending war. Loesung: pending-Symbol/Side ausschliessen
        # vom MISSED_FILL-Check.)
        try:
            ib.reqAllOpenOrders()
            ib.sleep(1.0)
        except Exception:
            pass
        open_orders = []
        cancelled_orders = []  # v37db: Cancelled-Filter fuer MISSED_FILL
        rejected_orders = []   # v37db: Rejected-Filter fuer MISSED_FILL
        try:
            # ib.openTrades() = nur unfertige (Submitted, PreSubmitted, PendingSubmit, PendingCancel)
            for t in (ib.openTrades() or []):
                if t.contract and t.order:
                    open_orders.append({
                        "symbol": t.contract.symbol,
                        "side": "BOT" if t.order.action == "BUY" else "SLD",
                        "status": t.orderStatus.status if t.orderStatus else "",
                    })
        except Exception:
            pass
        try:
            # v37db (06.05.2026): ib.trades() = ALLE Session-Trades inkl. Cancelled/Rejected/Filled.
            # Bug-Pattern davor: Bot schickt Limit-Order in Pre-Market, IBKR cancelled wenn
            # Limit nicht erreicht, Bot's trade_history.json behaelt SCANNER_BUY-Eintrag.
            # Reconcile-Cron sieht "Bot-Log + 0 Executions + nicht-mehr-pending" -> MISSED_FILL.
            # Aber: cancelled != missed. Filter cancelled/rejected hier raus, damit kein
            # False-Positive Pushover-Alert (Cry-Wolf gegen "Stille = OK"-Regel).
            for t in (ib.trades() or []):
                if not (t.contract and t.order and t.orderStatus):
                    continue
                status = t.orderStatus.status or ""
                side = "BOT" if t.order.action == "BUY" else "SLD"
                entry = {
                    "symbol": t.contract.symbol,
                    "side": side,
                    "status": status,
                }
                if status in ("Cancelled", "ApiCancelled", "Inactive"):
                    cancelled_orders.append(entry)
                elif status == "Rejected":
                    rejected_orders.append(entry)
        except Exception as e:
            log.debug(f"trades()-Sammlung fehlgeschlagen: {e}")
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
            "open_orders": open_orders,         # v37aa: fuer MISSED_FILL-Pending-Filter
            "cancelled_orders": cancelled_orders,  # v37db: fuer MISSED_FILL-Cancelled-Filter
            "rejected_orders": rejected_orders,    # v37db: fuer MISSED_FILL-Rejected-Filter
        }
    finally:
        broker.disconnect()


def reconcile(lookback_hours: int = 24,
              cash_tolerance_pct: float = CASH_TOLERANCE_PCT_DEFAULT,
              missed_fill_lookback_hours: int = 3) -> dict:
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
    # v37w (Smart-Reconcile): zusaetzlich Initial-Positions-Whitelist beruecksichtigen
    # (Positionen die der Bot nicht selbst gekauft hat, sondern beim Account-Setup
    # uebernommen wurden — z.B. CPER, USO im DUP108015 Paper-Account).
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

    # v37w: Whitelist akzeptierter Initial-Positions laden
    accepted_phantoms = _load_accepted_phantoms()

    for pos in ibkr["positions"]:
        sym = pos["symbol"]
        if sym in bot_known_symbols:
            continue  # Bot kennt die Position aus Lookback
        if sym in accepted_phantoms:
            continue  # User hat als Initial-Position akzeptiert
        drifts.append({
            "type": "PHANTOM_POSITION",
            "symbol": sym,
            "qty": pos["qty"],
            "avg_cost": pos["avg_cost"],
            "comment": ("IBKR hat Position, Bot-trade-history kennt sie nicht "
                        "im Lookback-Fenster. Falls Initial-Position: via "
                        "--accept-phantom akzeptieren."),
        })

    # 3. Missed Fills (Bot loggte BUY/SELL, aber IBKR hat keine matching Execution)
    # v37t+: nur recent_for_fill (24h) statt recent_bot (30d).
    # v37aa: pending-Order-Symbole ausschliessen — Bot kann Order submitted haben,
    # IBKR fillt aber erst Pre/Post-Market spaeter. Heute morgen 30.04. Beispiel:
    # ROKU SELL 09:10 -> IBKR-Fill 10:15 -> Reconcile 10:13 dazwischen meldete
    # MISSED_FILL false-positive.
    ibkr_exec_symbols = {(e["symbol"], e["side"]) for e in ibkr["executions"]}
    pending_symbols = {(o["symbol"], o["side"]) for o in ibkr.get("open_orders", [])
                       if o.get("status") in ("Submitted", "PreSubmitted",
                                              "PendingSubmit", "PendingCancel")}
    # v37db (06.05.2026): Cancelled + Rejected Orders sind KEIN MISSED_FILL.
    # Bug-Reproduktion 06.05.: Bot's SLV-Limit-Order (Pre-Market 13:10) wurde
    # von IBKR cancelled (Limit 70.75 nicht erreicht). Reconcile sah Bot-Log +
    # 0 Executions + nicht-mehr-pending -> meldete MISSED_FILL alle 30 Min.
    # Cry-Wolf-Risk gegen "Stille = OK"-Regel. Fix: cancelled/rejected hier
    # excluden (gleiche Approximation wie pending_symbols: Symbol+Side-Match).
    cancelled_rejected_symbols = (
        {(o["symbol"], o["side"]) for o in ibkr.get("cancelled_orders", [])}
        | {(o["symbol"], o["side"]) for o in ibkr.get("rejected_orders", [])}
    )
    # v37cd: Bot-interne Logging-Actions die NIE eine IBKR-Execution haben.
    # Wenn diese als MISSED_FILL gemeldet werden -> False-Positive.
    NON_EXECUTION_ACTIONS = {
        "PARTIAL_SIGNAL",       # nur Score-Logging, kein Trade
        "SIGNAL_LOGGED",        # Watchlist-Logging
        "RISK_BLOCK",           # Trade abgelehnt durch Risk-Manager
        "INSIDER_BLOCK",        # Insider-Filter-Block (Shadow oder real)
        "EARNINGS_BLOCK",       # Earnings-Blackout pre-Trade
        "REGIME_BLOCK",         # Regime-Filter-Block
    }
    for t in recent_for_fill:
        action = t.get("action", "").upper()
        # v37cd: Skip Bot-internal logging-Actions die nie zu Order werden
        if action in NON_EXECUTION_ACTIONS:
            continue
        # v37t-Fix: SCANNER_BUY/SELL und Compound-Actions wie STOP_LOSS_CLOSE matchen
        if action in ("BUY", "OPEN", "SCANNER_BUY"):
            ib_side = "BOT"
        elif (action in ("SELL", "CLOSE", "TP", "SL", "SCANNER_SELL", "MANUAL_SELL")
              or "CLOSE" in action):
            ib_side = "SLD"
        else:
            continue
        sym = t.get("symbol")
        if sym and (sym, ib_side) not in ibkr_exec_symbols:
            # Akzeptabel falls Status bereits known als nicht-fill
            # v37db (06.05.2026): "cancelled" + "rejected" hinzugefuegt — Bot-side
            # Status-Update setzt diese, Reconcile soll dann nicht MISSED_FILL melden.
            # Symbol-Mapping zwischen Bot-internal (z.B. "SILVER") und IBKR-Ticker
            # ("SLV") macht ib.trades()-basierten Filter unzuverlaessig — der Bot's
            # Status-Field ist der robustere Single-Source-of-Truth.
            if t.get("status") in ("close_failed", "skipped", "submitted",
                                    "blocked", "failed",
                                    "cancelled", "rejected"):
                continue
            # v37aa: Order pending bei IBKR? -> kein MISSED_FILL
            if (sym, ib_side) in pending_symbols:
                continue
            # v37db: Order cancelled/rejected bei IBKR? -> kein MISSED_FILL
            if (sym, ib_side) in cancelled_rejected_symbols:
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
    parser.add_argument("--missed-fill-lookback-hours", type=int, default=3,
                        help="Lookback fuer MISSED_FILL-Detection. Default 3h. "
                             "Sollte KURZ bleiben (max ~6-8h) weil IBKR-Session-"
                             "Executions nach Daily-Restart 03:00 UTC verloren "
                             "gehen. Trades aelter als der letzte Restart sind "
                             "naturgemaess nicht mehr in der Live-Session sichtbar "
                             "und wuerden False-Positives erzeugen.")
    parser.add_argument("--alert", action="store_true",
                        help="Bei Drift Multi-Channel-Alert ausloesen")
    parser.add_argument("--json", action="store_true",
                        help="Output als reines JSON")
    parser.add_argument("--accept-phantom", nargs="+", metavar="SYMBOL",
                        help="Akzeptiert eine oder mehrere PHANTOM_POSITIONs als "
                             "Initial-Position (v37w). Fuegt Symbol(e) in "
                             "data/reconcile_accepted_phantoms.json. "
                             "Beispiel: --accept-phantom CPER USO")
    parser.add_argument("--list-accepted", action="store_true",
                        help="Listet aktuelle Accept-Phantom-Liste und exit.")
    args = parser.parse_args()

    # v37w: Accept-Phantom-Modus (nicht-reconcile)
    if args.accept_phantom:
        for sym in args.accept_phantom:
            _add_accepted_phantom(sym.upper(), reason="cli-accept")
            print(f"[OK] {sym.upper()} als Initial-Position akzeptiert.")
        return 0
    if args.list_accepted:
        accepted = _load_accepted_phantoms()
        if not accepted:
            print("(Liste leer)")
        else:
            print(f"Accepted-Phantom-Liste ({len(accepted)} Symbole):")
            for s in sorted(accepted):
                print(f"  {s}")
        return 0

    if not args.json:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    try:
        report = reconcile(
            lookback_hours=args.lookback_hours,
            cash_tolerance_pct=args.cash_tolerance_pct,
            missed_fill_lookback_hours=args.missed_fill_lookback_hours,
        )
    except Exception as e:
        # v37cm: repr() statt %s — bare Exceptions wie TimeoutError() haben
        # leeren __str__, vorher loggte das 'Reconciliation failed: ' ohne
        # Diagnose-Info. Jetzt sieht man den Exception-Type explizit.
        log.error("Reconciliation failed: %r", e)
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
