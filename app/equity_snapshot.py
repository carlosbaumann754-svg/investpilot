"""
InvestPilot - Daily Equity Snapshot

Speichert taeglich nach US-Boersen-Close (>= 22:30 CET) einen Schnappschuss
mit Portfolio-Wert + Benchmark-Schlusskursen (SPY, QQQ, AGG). Daraus baut das
Frontend die Monatstabelle und spaeter die Equity-Curve.

Persistenz: data/equity_history.json (Liste von Snapshots).
Wird ueber den bestehenden Gist-Backup mitgesichert.

Snapshot-Schema:
{
    "date": "2026-04-14",          # ISO-Datum (1 pro Tag, Idempotenz-Key)
    "ts":   "2026-04-14T22:35:01", # erstmaliger Zeitstempel
    "portfolio_total_value": 1234.56,  # USD, Cash + Invested + Unrealized
    "spy_close": 524.31,
    "qqq_close": 451.89,
    "agg_close": 102.14,
    "source": "scheduler-daily-2230"
}

Berechnung Monatszeile (frontend):
- Erster und letzter Snapshot des Kalendermonats
- pct = (last - first) / first * 100 fuer jedes Asset
- Alpha = portfolio_pct - benchmark_pct
"""

import logging
import os
from datetime import datetime, time as dt_time

from app.config_manager import load_json, save_json, get_data_path

log = logging.getLogger("EquitySnapshot")

EQUITY_FILE = "equity_history.json"
DAILY_GUARD = "equity_snapshot_last.flag"
# US-Markt schliesst 22:00 CET. 22:30 = sicherer Puffer fuer yfinance EOD-Daten.
SNAPSHOT_HOUR = 22
SNAPSHOT_MINUTE = 30
# Maximale Historie (5 Jahre = ~1300 Trading-Tage). Aelteres rotieren wir raus,
# damit die JSON nicht ins Unendliche waechst (Gist hat 1 MB Soft-Limit).
MAX_HISTORY_DAYS = 1825


def _load_history() -> list:
    data = load_json(EQUITY_FILE)
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("snapshots"), list):
        # Tolerantes Format-Migration falls jemand das mal anders gespeichert hat
        return data["snapshots"]
    return []


def _save_history(snapshots: list) -> None:
    if len(snapshots) > MAX_HISTORY_DAYS:
        snapshots = snapshots[-MAX_HISTORY_DAYS:]
    save_json(EQUITY_FILE, snapshots)


def _today_already_recorded(snapshots: list, today_iso: str) -> bool:
    return any(s.get("date") == today_iso for s in snapshots)


def _fetch_latest_close(symbol: str) -> float | None:
    """Letzter Tagesschlusskurs via Web-App-Cache (1h TTL).

    Wir benutzen denselben Cache wie /api/benchmark, damit Snapshot und
    UI-Vergleich auf identischer Datenquelle laufen.
    """
    try:
        # Lazy-Import: web.app importiert FastAPI etc. — nur wenn wir wirklich
        # snapshot machen. Im Render-Container ist das immer verfuegbar.
        from web.app import _fetch_ticker_closes
    except Exception as e:
        log.warning(f"Kann _fetch_ticker_closes nicht importieren: {e}")
        return None
    closes = _fetch_ticker_closes(symbol, years=5)
    if not closes:
        return None
    try:
        latest_date = max(closes.keys())
        return float(closes[latest_date])
    except Exception:
        return None


def _fetch_portfolio_total_value() -> float | None:
    """Aktueller Portfolio-Wert.

    Strategie: Erst aus brain_state.performance_snapshots den juengsten Wert
    nehmen (vom letzten Trading-Zyklus, max 5 Min alt) — vermeidet einen
    eToro-API-Call und ist robust wenn die Auth-Session gerade rotiert.
    Fallback: Live-Call ueber EtoroClient.
    """
    try:
        brain = load_json("brain_state.json")
        if isinstance(brain, dict):
            snaps = brain.get("performance_snapshots") or []
            if snaps:
                latest = snaps[-1]
                tv = latest.get("total_value")
                if isinstance(tv, (int, float)) and tv > 0:
                    return float(tv)
    except Exception as e:
        log.debug(f"Brain-Snapshot-Read fehlgeschlagen: {e}")

    # Fallback: Live aus eToro
    try:
        from app.etoro_client import EtoroClient
        client = EtoroClient()
        port = client.get_portfolio()
        if not port:
            return None
        credit = float(port.get("credit", 0) or 0)
        unrealized = float(port.get("unrealizedPnL", 0) or 0)
        invested = 0.0
        for pos in port.get("positions", []) or []:
            try:
                parsed = EtoroClient.parse_position(pos)
                invested += float(parsed.get("invested", 0) or 0)
            except Exception:
                continue
        total = credit + invested + unrealized
        return float(total) if total > 0 else None
    except Exception as e:
        log.warning(f"Live-Portfolio-Fetch fehlgeschlagen: {e}")
        return None


def is_snapshot_time() -> bool:
    """True wenn jetzt >= 22:30 CET an einem Tag, an dem noch nicht
    snapshotted wurde. Wird vom Scheduler alle 5 Min gepollt."""
    now = datetime.now()
    cutoff = dt_time(SNAPSHOT_HOUR, SNAPSHOT_MINUTE)
    if now.time() < cutoff:
        return False
    # Nicht am Wochenende oder Feiertag — yfinance hat dann keinen frischen
    # Close. Aber: Demo-Modus kann 24/7 traden, also nehmen wir die letzten
    # verfuegbaren Markt-Closes (yfinance liefert eh den letzten Trading-Day).
    # -> wir nehmen Wochenend-Snapshots NICHT, sonst doppeln sich die Returns.
    if now.weekday() >= 5:
        return False
    return True


def take_snapshot(triggered_by: str = "scheduler-daily-2230") -> dict | None:
    """Erstellt und persistiert genau einen Snapshot pro Tag (idempotent).

    Returns:
        Den geschriebenen Snapshot oder None falls bereits vorhanden / Fehler.
    """
    today_iso = datetime.now().strftime("%Y-%m-%d")
    history = _load_history()

    if _today_already_recorded(history, today_iso):
        log.debug(f"Equity-Snapshot fuer {today_iso} existiert bereits — skip")
        return None

    portfolio_value = _fetch_portfolio_total_value()
    if portfolio_value is None:
        log.warning("Equity-Snapshot abgebrochen: Portfolio-Wert nicht ermittelbar")
        return None

    snap = {
        "date": today_iso,
        "ts": datetime.now().isoformat(timespec="seconds"),
        "portfolio_total_value": round(portfolio_value, 2),
        "source": triggered_by,
    }
    for sym in ("SPY", "QQQ", "AGG"):
        c = _fetch_latest_close(sym)
        snap[f"{sym.lower()}_close"] = round(c, 4) if c is not None else None

    history.append(snap)
    _save_history(history)
    log.info(
        f"Equity-Snapshot {today_iso}: Portfolio=${snap['portfolio_total_value']:,.2f}, "
        f"SPY={snap.get('spy_close')}, QQQ={snap.get('qqq_close')}, AGG={snap.get('agg_close')}"
    )

    # Guard fuer Scheduler-Skip (entlastet load_json bei jedem 5-Min-Tick)
    try:
        get_data_path(DAILY_GUARD).write_text(today_iso)
    except Exception:
        pass

    # Sofortiges Cloud-Backup, damit der Snapshot beim naechsten Render-Restart
    # nicht verloren geht (Persistent Disk ist da, aber doppelt haelt besser).
    try:
        from app.persistence import backup_to_cloud
        backup_to_cloud()
    except Exception as e:
        log.debug(f"Post-Snapshot Cloud-Backup nicht moeglich: {e}")

    return snap


def maybe_take_snapshot(triggered_by: str = "scheduler-daily-2230") -> dict | None:
    """Scheduler-Entrypoint: prueft Guard + Zeitfenster, dann snapshot."""
    if not is_snapshot_time():
        return None
    today_iso = datetime.now().strftime("%Y-%m-%d")
    try:
        guard = get_data_path(DAILY_GUARD)
        if guard.exists() and guard.read_text().strip() == today_iso:
            return None
    except Exception:
        pass
    return take_snapshot(triggered_by=triggered_by)
