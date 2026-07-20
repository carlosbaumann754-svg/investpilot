"""Cutover-Gatekeeper: realisierte Round-Trips des NEUEN Motors.

Warum es dieses Skript gibt
---------------------------
Die Frage "sind die abgeschlossenen Trades netto profitabel?" hat mehrere plausible
Abgrenzungen, die sich um >20k USD unterscheiden. Am 16.07. und 20.07.2026 wurden
versehentlich zwei verschiedene gerechnet (-8.3k vs +14.8k) — dieselben Daten, anderes
Fenster. Damit das nie wieder passiert, ist die Regel hier als Code festgenagelt.

DIE LEITMETRIK (V3, "sauber")
-----------------------------
Gezaehlt wird eine Position nur, wenn der Bot sie im Soak-Fenster SOWOHL eroeffnet
ALS AUCH geschlossen hat. Begruendung: nur dann ist das Ergebnis eine Entscheidung
des neuen Fundamental-Motors. Positionen, die vor dem Soak gekauft wurden ("geerbt"),
tragen Gewinne/Verluste, die unter alten Parametern entstanden sind — sie dem neuen
Motor gutzuschreiben waere Cherry-Picking (und genau das haette das Gate am 20.07.
faelschlich auf "gruen" gedreht).

Nebenrechnungen dienen nur der Kontrolle:
  V1 = alles was im Fenster geschlossen wurde (inkl. geerbt)  -> zu optimistisch
  V2 = nur Episoden ohne Teilverkaeufe                        -> VERZERRT, nicht nutzen
       (Teilverkaeufe passieren fast nur bei Gewinnern -> filtert Gewinner raus)
  V4 = geerbte Positionen                                     -> Altlast-Abbau, separat

Aufruf:  docker exec -i investpilot python - < scripts/gatekeeper_roundtrips.py
"""
import json, os

D = "data"
SOAK_START = "2026-07-02T22:00"   # muss zu SOAK_START in web/app.py passen

BUY_ACTIONS = {"SCANNER_BUY", "PARTIAL_SIGNAL"}
FULL_CLOSE = {"TRAILING_SL_CLOSE", "STOP_LOSS_CLOSE", "TAKE_PROFIT_CLOSE",
              "TIME_STOP_CLOSE", "EARNINGS_BLACKOUT_CLOSE"}
PARTIAL_CLOSE = {"PARTIAL_CLOSE"}
CLOSE_ACTIONS = FULL_CLOSE | PARTIAL_CLOSE


def build_episodes(trades):
    """Gruppiert Trades zu Positions-Episoden: Einstieg -> vollstaendige Schliessung.

    Teilverkaeufe werden der laufenden Episode zugerechnet (nicht als eigene Episode),
    Zukaeufe verschieben den Einstiegszeitpunkt NICHT.
    """
    id2sym, pid2sym = {}, {}
    for t in trades:
        if t.get("instrument_id") is not None and t.get("symbol"):
            id2sym.setdefault(t["instrument_id"], t["symbol"])
        if t.get("position_id") is not None and t.get("symbol"):
            pid2sym.setdefault(str(t["position_id"]), t["symbol"])

    def sym_of(t):
        return (t.get("symbol") or id2sym.get(t.get("instrument_id"))
                or pid2sym.get(str(t.get("position_id"))) or f"?id{t.get('instrument_id')}")

    episodes, open_ep = [], {}
    for t in trades:
        s, a, ts = sym_of(t), t.get("action"), str(t.get("timestamp", ""))
        if a in BUY_ACTIONS:
            ep = open_ep.setdefault(s, {"symbol": s, "entry_ts": ts, "exit_ts": None,
                                        "pnl": 0.0, "n_partials": 0, "n_buys": 0})
            ep["n_buys"] += 1
        elif a in CLOSE_ACTIONS:
            ep = open_ep.setdefault(s, {"symbol": s, "entry_ts": None, "exit_ts": None,
                                       "pnl": 0.0, "n_partials": 0, "n_buys": 0})
            if isinstance(t.get("pnl_usd"), (int, float)):
                ep["pnl"] += t["pnl_usd"]
            if a in PARTIAL_CLOSE:
                ep["n_partials"] += 1
            else:
                ep["exit_ts"] = ts
                episodes.append(ep)
                del open_ep[s]
    return episodes, list(open_ep.values())


def stats(eps):
    n = len(eps)
    if not n:
        return {"n": 0, "net": 0.0, "win_rate": 0.0, "avg_win": 0.0, "avg_loss": 0.0, "pf": 0.0}
    wins = [e["pnl"] for e in eps if e["pnl"] > 0]
    losses = [e["pnl"] for e in eps if e["pnl"] <= 0]
    gross_w, gross_l = sum(wins), abs(sum(losses))
    return {
        "n": n, "net": sum(e["pnl"] for e in eps),
        "win_rate": len(wins) / n * 100,
        "avg_win": (gross_w / len(wins)) if wins else 0.0,
        "avg_loss": (-gross_l / len(losses)) if losses else 0.0,
        "pf": (gross_w / gross_l) if gross_l else float("inf"),
    }


def main():
    with open(os.path.join(D, "trade_history.json")) as f:
        trades = [t for t in json.load(f) if isinstance(t, dict)]
    trades.sort(key=lambda t: str(t.get("timestamp", "")))

    episodes, still_open = build_episodes(trades)
    closed = [e for e in episodes if e["exit_ts"] and e["exit_ts"] >= SOAK_START]
    clean = [e for e in closed if e["entry_ts"] and e["entry_ts"] >= SOAK_START]
    inherited = [e for e in closed if not e["entry_ts"] or e["entry_ts"] < SOAK_START]

    s_clean, s_all, s_inh = stats(clean), stats(closed), stats(inherited)

    print("=" * 72)
    print(f"CUTOVER-GATEKEEPER  (Soak-Start {SOAK_START})")
    print("=" * 72)
    print(f"\n>>> LEITMETRIK V3 — Round-Trips VOLLSTAENDIG im Soak (neuer Motor):")
    print(f"    n              : {s_clean['n']}")
    print(f"    NETTO          : {s_clean['net']:+,.0f} USD")
    print(f"    Trefferquote   : {s_clean['win_rate']:.1f}%")
    print(f"    Ø Gewinn       : {s_clean['avg_win']:+,.0f}")
    print(f"    Ø Verlust      : {s_clean['avg_loss']:+,.0f}")
    print(f"    Profit-Faktor  : {s_clean['pf']:.2f}   (Ziel > 1.0; WFO-Baseline 1.71)")
    print(f"\n    GATE (netto > 0): {'ERFUELLT' if s_clean['net'] > 0 else 'NICHT ERFUELLT'}")

    print(f"\n--- Kontrollrechnungen (NICHT das Gate) ---")
    print(f"    V1 alle Closes im Fenster : {s_all['net']:+,.0f} USD (n={s_all['n']})")
    print(f"    V4 geerbte Positionen     : {s_inh['net']:+,.0f} USD (n={s_inh['n']})")

    print(f"\n--- Detail Leitmetrik ---")
    for e in sorted(clean, key=lambda x: x["exit_ts"]):
        print(f"    {e['entry_ts'][:10]} -> {e['exit_ts'][:10]}  {e['symbol']:<8s} {e['pnl']:>+11,.0f}")

    # Offene Positionen aus brain_state lesen (autoritativ) — die Episoden-Rekonstruktion
    # enthaelt Alt-Motor-Leichen (Symbole, deren Schliessung vor dem Historien-Beginn liegt).
    print(f"\n--- Offene Positionen (unrealisiert, zaehlen NICHT ins Gate) ---")
    try:
        with open(os.path.join(D, "brain_state.json")) as f:
            snaps = json.load(f).get("performance_snapshots") or []
        pos = snaps[-1].get("positions") or []
        print(f"    {len(pos)} live: {', '.join(sorted(str(p.get('symbol')) for p in pos))}")
    except Exception as e:
        print(f"    brain_state nicht lesbar ({e}); Episoden-Schaetzung: {len(still_open)}")


if __name__ == "__main__":
    main()
