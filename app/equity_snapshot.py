"""
InvestPilot - Daily Equity Snapshot

Speichert taeglich nach US-Boersen-Close (>= 22:30 CET) einen Schnappschuss
mit Portfolio-Wert + Benchmark-Schlusskursen (SPY, QQQ, AGG, IWM). Daraus baut
das Frontend die Monatstabelle und spaeter die Equity-Curve. IWM (Russell 2000)
ist die korrekte Small-Cap-Benchmark fuer den sp600-Motor (v37dv).

Persistenz: data/equity_history.json (Liste von Snapshots).
Wird ueber den bestehenden Gist-Backup mitgesichert.

Snapshot-Schema:
{
    "date": "2026-04-14",          # ISO-Datum (1 pro Tag, Idempotenz-Key)
    "ts":   "2026-04-14T22:35:01", # erstmaliger Zeitstempel
    "portfolio_total_value": 1234.56,  # Kontowaehrung (CHF), Cash + Invested + Unrealized
    # --- R-B13 (21.07.2026): Bestandteile fuer die Ergebnis-Bruecke ---
    "unrealized_pnl": 8087.06,     # USD  <- OHNE DAS ist keine Bruecke baubar
    "base_unrealized_pnl": 6549.61,  # CHF
    "base_realized_pnl": 0.0,      # CHF
    "cash": 694380.7,              # CHF
    "invested": 468529.32,         # USD
    "num_positions": 15,
    "base_currency": "CHF",
    # --- Benchmarks + FX ---
    "spy_close": 524.31,
    "qqq_close": 451.89,
    "agg_close": 102.14,
    "iwm_close": 198.72,           # v37dv: Small-Cap-Benchmark (korrekt fuer sp600)
    "usdchf_close": 0.8103,        # R-B13: CHF je USD — Depot CHF vs Benchmarks USD
    "source": "scheduler-daily-2230"
}

WARUM die Bestandteile (R-B13, 21.07.2026):
Frueher wurde nur der Gesamtwert gespeichert. Dadurch liess sich im Nachhinein
nicht trennen, welcher Teil einer Wertaenderung aus REALISIERTEN Trades und
welcher aus BUCHGEWINNEN stammt. Bei der 3-Monats-Analyse am 20.07. scheiterte
die Ergebnis-Bruecke genau daran (unrealized_pnl war nirgends historisiert;
brain_state.performance_snapshots deckt nur die letzten 1-2 Tage ab) — der
Restposten von -87k war ein reines Daten-Artefakt.

Ergebnis-Bruecke (ab jetzt rechenbar):
  Delta(portfolio_total_value) = realisierte Trades
                               + Delta(unrealized_pnl)
                               + Ein-/Auszahlungen + Gebuehren + FX

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


# R-B13 (21.07.2026): Bestandteile, die zusaetzlich zum Gesamtwert persistiert
# werden. Ohne sie ist im Nachhinein NICHT rekonstruierbar, welcher Teil einer
# Wertaenderung aus realisierten Trades und welcher aus Buchgewinnen stammt.
# Konkreter Anlass: Die Ergebnis-Bruecke fuer die 3-Monats-Analyse am 20.07.
# liess sich nicht bauen, weil unrealized_pnl nirgends historisiert war
# (brain_state.performance_snapshots deckt nur die letzten 1-2 Tage ab).
# 'unrealized_pnl'/'invested' sind USD, 'base_*' und total_value sind Kontowaehrung (CHF).
_COMPONENT_KEYS = (
    "unrealized_pnl", "base_unrealized_pnl", "base_realized_pnl",
    "cash", "invested", "num_positions", "base_currency",
)


def _fetch_portfolio_components() -> dict | None:
    """Portfolio-Wert PLUS Bestandteile fuer die spaetere Ergebnis-Bruecke.

    Strategie: Erst aus brain_state.performance_snapshots den juengsten Wert
    nehmen (vom letzten Trading-Zyklus, max 5 Min alt) — vermeidet einen
    Broker-API-Call und ist robust wenn die Auth-Session gerade rotiert.
    Fallback: Live-Call ueber den Broker.

    Returns dict mit mindestens {"portfolio_total_value": float} oder None.
    """
    try:
        brain = load_json("brain_state.json")
        if isinstance(brain, dict):
            snaps = brain.get("performance_snapshots") or []
            if snaps:
                latest = snaps[-1]
                tv = latest.get("total_value")
                if isinstance(tv, (int, float)) and tv > 0:
                    out = {"portfolio_total_value": float(tv)}
                    for k in _COMPONENT_KEYS:
                        if k in latest:
                            out[k] = latest[k]
                    return out
    except Exception as e:
        log.debug(f"Brain-Snapshot-Read fehlgeschlagen: {e}")

    # Fallback: Live aus Broker (eToro/IBKR)
    try:
        from app.etoro_client import EtoroClient  # noqa: F401 — fuer parse_position falls genutzt
        from app.broker_base import get_broker
        client = get_broker(readonly=True)
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
            except Exception as e:
                log.warning(
                    f"Equity-Snapshot: Position parse failed "
                    f"(pos_id={pos.get('PositionID') or pos.get('position_id')}): {e}",
                    exc_info=True,
                )
                continue
        total = credit + invested + unrealized
        if total <= 0:
            return None
        return {"portfolio_total_value": float(total), "cash": credit,
                "invested": invested, "unrealized_pnl": unrealized,
                "num_positions": len(port.get("positions", []) or [])}
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

    # R-B36 (22.07.2026): Nur ein SCHEDULER-Eintrag beendet den Tag. Vorher
    # blockierte JEDER heutige Eintrag — ein manueller Dashboard-Klick am Mittag
    # verhinderte damit den Tagesend-Snapshot (Live-Fall 21.07.: Eintrag von
    # 12:08 CEST blieb als Tageswert stehen, der Abend-Lauf wurde uebersprungen).
    # Jetzt: Scheduler-Eintrag vorhanden -> fertig fuer heute (auch fuer manuelle
    # Klicks — der Tagesend-Wert ist der kanonische). Nur manuelle Eintraege
    # vorhanden -> weiterlaufen, der Upsert unten ersetzt sie.
    heutige = [h for h in history if h.get("date") == today_iso]
    if any(str(h.get("source", "")).startswith("scheduler") for h in heutige):
        log.debug(f"Equity-Snapshot fuer {today_iso} (Scheduler) existiert bereits — skip")
        return None

    comp = _fetch_portfolio_components()
    if comp is None:
        log.warning("Equity-Snapshot abgebrochen: Portfolio-Wert nicht ermittelbar")
        return None

    snap = {
        "date": today_iso,
        "ts": datetime.now().isoformat(timespec="seconds"),
        "portfolio_total_value": round(comp["portfolio_total_value"], 2),
        "source": triggered_by,
    }
    # R-B13: Bestandteile mitschreiben -> Ergebnis-Bruecke bleibt rekonstruierbar
    for k in _COMPONENT_KEYS:
        v = comp.get(k)
        snap[k] = round(v, 2) if isinstance(v, float) else v
    # v37dv: IWM (Russell 2000 Small-Cap) ergaenzt — die KORREKTE Benchmark fuer
    # den sp600-Small-Cap-Motor (SPY/QQQ sind Large-Cap, beta-unpassend). IWM ist
    # zwar in disabled_symbols (kein Trade), wird hier aber nur als Preis-Referenz
    # via yfinance geholt -> kein Konflikt.
    # R-B13 (21.07.2026): USD/CHF mitschreiben. Das Depot wird in CHF gefuehrt,
    # die Benchmarks notieren in USD — ohne Kurs vergleicht man Aepfel mit Birnen.
    # Bei der 3-Monats-Analyse am 20.07. musste der Kurs nachtraeglich geholt
    # werden; das Ergebnis (Alpha -7 Ppkt in BEIDEN Waehrungen) war zu wichtig,
    # um es kuenftig von einem Ad-hoc-Download abhaengig zu machen.
    for sym, key in (("SPY", "spy_close"), ("QQQ", "qqq_close"),
                     ("AGG", "agg_close"), ("IWM", "iwm_close"),
                     ("CHF=X", "usdchf_close")):
        c = _fetch_latest_close(sym)
        snap[key] = round(c, 4) if c is not None else None

    # R-B36 (22.07.2026): UPSERT statt Append — der letzte Stand des Tages
    # gewinnt. Vorher konnten manueller (Dashboard) und geplanter Snapshot
    # zwei Eintraege fuer denselben Tag erzeugen; und da der manuelle den
    # Tages-Guard mitschrieb, verdraengte ein Mittags-Klick den Tagesend-Wert
    # komplett (Live-Fall 21.07.: manuell 12:08 CEST -> Abend-Lauf uebersprungen,
    # in der Monatstabelle stand ein Mittagswert).
    history = [h for h in history if h.get("date") != today_iso]
    history.append(snap)
    _save_history(history)
    log.info(
        f"Equity-Snapshot {today_iso}: Portfolio={snap['portfolio_total_value']:,.2f} "
        f"{snap.get('base_currency') or ''}, unrealisiert={snap.get('unrealized_pnl')} USD, "
        f"Pos={snap.get('num_positions')}, USD/CHF={snap.get('usdchf_close')}, "
        f"SPY={snap.get('spy_close')}, QQQ={snap.get('qqq_close')}, "
        f"AGG={snap.get('agg_close')}, IWM={snap.get('iwm_close')}"
    )

    # Guard fuer Scheduler-Skip (entlastet load_json bei jedem 5-Min-Tick).
    # R-B36: NUR Scheduler-Laeufe verbrauchen den Tages-Slot. Ein manueller
    # Dashboard-Snapshot ist eine Momentaufnahme zwischendurch — er darf den
    # geplanten Tagesend-Snapshot nicht verhindern (der Upsert oben sorgt
    # dafuer, dass der Abendwert den Mittagswert ersetzt, nicht dupliziert).
    if str(triggered_by).startswith("scheduler"):
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
        log.warning(
            f"Post-Snapshot Cloud-Backup nicht moeglich: {e}",
            exc_info=True,
        )

    return snap


def maybe_take_snapshot(triggered_by: str = "scheduler-daily-2230") -> dict | None:
    """Scheduler-Entrypoint: prueft Guard + Zeitfenster, dann snapshot."""
    if not is_snapshot_time():
        return None
    # today_iso einmal berechnen - fruehere Version hat es in take_snapshot()
    # noch einmal berechnet (harmlos, aber um Mitternacht race-anfaellig).
    today_iso = datetime.now().strftime("%Y-%m-%d")
    try:
        guard = get_data_path(DAILY_GUARD)
        if guard.exists() and guard.read_text().strip() == today_iso:
            return None
    except Exception as e:
        log.warning(f"Equity-Snapshot Guard-Check fehlgeschlagen: {e}", exc_info=True)
    return take_snapshot(triggered_by=triggered_by)
