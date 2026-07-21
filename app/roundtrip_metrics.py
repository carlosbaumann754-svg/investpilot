"""R-B12 (20.07.2026) — Round-Trip-Oekonomie des AKTIVEN Motors.

WARUM DIESES MODUL EXISTIERT
----------------------------
Der WFO-Drift-Watchdog misst den Profit-Faktor ueber das GESAMTE Buch, in
PROZENT, ueber ein rollendes Fenster. Das hat drei blinde Flecken:

  1. Prozent ignoriert Positionsgroesse — ein +9%-Teilverkauf ueber 123 USD
     wiegt so viel wie ein -8%-Stop ueber 6'393 USD.
  2. Kein Einstiegs-Filter — Positionen, die VOR dem aktuellen Motor/Regime
     gekauft wurden ("geerbt"), tragen Gewinne aus alten Parametern bei.
  3. Teil-Closes verwaessern die Zaehlung (fast nur Gewinner haben welche).

Real gemessen am 20.07.2026: Watchdog sah PF 2.02 ("gesund"), die sauberen
Round-Trips des neuen Motors lagen bei PF 0.38 (netto -7'534 USD). Genau die
Luecke, die dieses Modul schliesst.

DIE REGEL (bewusst streng)
--------------------------
Eine Position zaehlt nur, wenn der Bot sie im Bewertungsfenster SOWOHL
eroeffnet ALS AUCH vollstaendig geschlossen hat — nur dann ist das Ergebnis
eine Entscheidung des aktiven Motors. Gerechnet wird in USD (pnl_usd), nicht
in Prozent.

SINGLE SOURCE OF TRUTH: Sowohl scripts/gatekeeper_roundtrips.py als auch der
Watchdog importieren von hier. Zwei getrennte Implementierungen waren exakt
die Ursache des Bugs, den dieses Modul adressiert — nicht wiederholen.

USAGE:
    from app.roundtrip_metrics import clean_roundtrip_stats
    s = clean_roundtrip_stats(trade_history, since_iso="2026-07-02T22:00")
    # s = {"n": 9, "net_usd": -7534.0, "pf": 0.38, "win_rate": 66.7, ...}
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)


AUDIT_METADATA = {
    "purpose": "Round-Trip-Oekonomie des aktiven Motors (USD, nur im Fenster eroeffnete UND geschlossene Positionen) — Basis fuer das Watchdog-Zweitsignal + scripts/gatekeeper_roundtrips.py",
    "config_section": None,
    "state_files": ["trade_history.json"],
    "self_tests": [],
    "scheduler_hooks": [],
    "health_check": None,
    "added_in": "R-B12 (20.07.2026)",
}

# Muss zu SOAK_START in web/app.py passen. Wird von der Config
# (wfo_drift_watchdog.roundtrip_since) ueberschrieben, wenn dort gesetzt.
DEFAULT_REGIME_START = "2026-07-21T12:00"

BUY_ACTIONS = {"SCANNER_BUY", "PARTIAL_SIGNAL"}
FULL_CLOSE_ACTIONS = {
    "TRAILING_SL_CLOSE", "STOP_LOSS_CLOSE", "TAKE_PROFIT_CLOSE",
    "TIME_STOP_CLOSE", "EARNINGS_BLACKOUT_CLOSE",
}
PARTIAL_CLOSE_ACTIONS = {"PARTIAL_CLOSE"}
CLOSE_ACTIONS = FULL_CLOSE_ACTIONS | PARTIAL_CLOSE_ACTIONS


def _symbol_resolver(trades: list) -> callable:
    """Baut einen Symbol-Aufloeser.

    Teil-Closes tragen kein 'symbol' — wohl aber instrument_id/position_id.
    Ohne Aufloesung fielen sie aus der Episoden-Zuordnung.
    """
    id2sym: dict = {}
    pid2sym: dict = {}
    for t in trades:
        sym = t.get("symbol")
        if not sym:
            continue
        if t.get("instrument_id") is not None:
            id2sym.setdefault(t["instrument_id"], sym)
        if t.get("position_id") is not None:
            pid2sym.setdefault(str(t["position_id"]), sym)

    def resolve(t: dict) -> str:
        return (t.get("symbol")
                or id2sym.get(t.get("instrument_id"))
                or pid2sym.get(str(t.get("position_id")))
                or f"?id{t.get('instrument_id')}")

    return resolve


def build_episodes(trade_history: list) -> tuple[list[dict], list[dict]]:
    """Gruppiert Trades zu Positions-Episoden.

    Eine Episode laeuft vom oeffnenden Kauf bis zur vollstaendigen Schliessung.
    Teil-Closes gehen in die laufende Episode ein (keine eigene Episode),
    Zukaeufe verschieben den Einstiegszeitpunkt NICHT (sonst wuerde ein
    Nachkauf eine geerbte Position faelschlich "frisch" machen).

    Returns (closed_episodes, still_open).
    """
    trades = [t for t in (trade_history or []) if isinstance(t, dict)]
    trades.sort(key=lambda t: str(t.get("timestamp", "")))
    resolve = _symbol_resolver(trades)

    closed: list[dict] = []
    open_eps: dict = {}

    for t in trades:
        action = (t.get("action") or "").upper()
        sym = resolve(t)
        ts = str(t.get("timestamp", ""))

        if action in BUY_ACTIONS:
            ep = open_eps.setdefault(sym, {
                "symbol": sym, "entry_ts": ts, "exit_ts": None,
                "pnl_usd": 0.0, "invested_usd": 0.0, "n_partials": 0, "n_buys": 0,
            })
            ep["n_buys"] += 1
            # R-B18 (21.07.2026): Einsatz mitfuehren -> Prozent-Rendite je
            # Episode. Der USD-PF ist oekonomisch richtig, aber statistisch
            # NICHT mit dem Backtest vergleichbar (der gewichtet gleich).
            # Gemessen am 21.07.: USD-PF 0.38 vs Prozent-PF 0.45 auf denselben
            # 9 Trades. Fuer Alarm-Entscheidungen zaehlt die Prozent-Basis.
            amt = t.get("amount_usd")
            if isinstance(amt, (int, float)) and amt > 0:
                ep["invested_usd"] += float(amt)
        elif action in CLOSE_ACTIONS:
            # Close ohne bekannten Einstieg (Historie beginnt mittendrin) ->
            # entry_ts None; wird spaeter als "geerbt" klassifiziert.
            ep = open_eps.setdefault(sym, {
                "symbol": sym, "entry_ts": None, "exit_ts": None,
                "pnl_usd": 0.0, "invested_usd": 0.0, "n_partials": 0, "n_buys": 0,
            })
            pnl = t.get("pnl_usd")
            if isinstance(pnl, (int, float)):
                ep["pnl_usd"] += float(pnl)
            if action in PARTIAL_CLOSE_ACTIONS:
                ep["n_partials"] += 1
            else:
                ep["exit_ts"] = ts
                closed.append(ep)
                del open_eps[sym]

    return closed, list(open_eps.values())


def _pf(vals: list) -> Optional[float]:
    """Profit-Faktor. None wenn verlustfrei (statt inf) — JSON/Vergleiche."""
    gw = sum(v for v in vals if v > 0)
    gl = abs(sum(v for v in vals if v <= 0))
    return round(gw / gl, 3) if gl > 0 else None


def _stats(episodes: list[dict]) -> dict:
    n = len(episodes)
    if n == 0:
        return {"n": 0, "net_usd": 0.0, "win_rate": None, "avg_win": None,
                "avg_loss": None, "pf": None, "pf_pct": None, "avg_ret_pct": None,
                "gross_win": 0.0, "gross_loss": 0.0}
    usd = [e["pnl_usd"] for e in episodes]
    wins = [v for v in usd if v > 0]
    losses = [v for v in usd if v <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))

    # R-B18: Prozent-Renditen je Episode (Ergebnis / Einsatz). NUR diese Basis
    # ist mit dem gleichgewichtenden Backtest — und damit mit der
    # Referenzverteilung — vergleichbar.
    pcts = [e["pnl_usd"] / e["invested_usd"] * 100
            for e in episodes if e.get("invested_usd")]

    return {
        "n": n,
        "net_usd": round(gross_win - gross_loss, 2),
        "win_rate": round(len(wins) / n * 100, 1),
        "avg_win": round(gross_win / len(wins), 2) if wins else None,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else None,
        # Oekonomisch richtig (so viel Geld ist geflossen) -> Gatekeeper.
        "pf": _pf(usd),
        # Statistisch vergleichbar -> Alarm-Entscheidungen.
        "pf_pct": _pf(pcts) if pcts else None,
        "avg_ret_pct": round(sum(pcts) / len(pcts), 3) if pcts else None,
        "n_pct": len(pcts),
        "gross_win": round(gross_win, 2),
        "gross_loss": round(gross_loss, 2),
    }


def clean_roundtrip_stats(trade_history: list,
                          since_iso: Optional[str] = None) -> dict:
    """Kennzahlen der Round-Trips, die im Fenster eroeffnet UND geschlossen wurden.

    since_iso: ISO-Zeitstempel des Regime-/Soak-Starts. Default:
               DEFAULT_REGIME_START.

    Returns dict mit n / net_usd / pf / win_rate / avg_win / avg_loss plus
    'inherited' (Kontrollzahl: im Fenster geschlossene, aber VORHER gekaufte
    Positionen) und 'episodes' (Detail, chronologisch nach Ausstieg).
    """
    since = since_iso or DEFAULT_REGIME_START
    closed, still_open = build_episodes(trade_history)

    in_window = [e for e in closed if e["exit_ts"] and e["exit_ts"] >= since]
    clean = [e for e in in_window if e["entry_ts"] and e["entry_ts"] >= since]
    inherited = [e for e in in_window
                 if not e["entry_ts"] or e["entry_ts"] < since]

    out = _stats(clean)
    out["since"] = since
    out["inherited"] = _stats(inherited)
    out["all_in_window"] = _stats(in_window)
    out["episodes"] = sorted(clean, key=lambda e: e["exit_ts"])
    out["n_still_open"] = len(still_open)
    return out
