"""Rotation gegen Halten — Aufloesung des Widerspruchs (R-B27, 21.07.2026).

DER WIDERSPRUCH
===============
Juni 2026, Backtest:   Rotation +12.3 %  gegen  Halten +11.9 %  -> praktisch gleichauf.
                       Auf dieser Basis wurde entschieden, KEINE Rotation zu bauen.

Juli 2026, Zerfallskurve (R-B26): Der Vorsprung der Top-15 lebt genau einen Monat
                       (+1.078 %, t=3.46), ab Monat 2 nichts mehr. Das spricht klar
                       FUER monatliche Neuauswahl.

Beides kann nicht stimmen. Diese Auswertung klaert, welches Ergebnis warum zustande kam.

ERSTER VERDACHT: DIE VOREINSTELLUNGEN SIND VERSCHIEDEN
------------------------------------------------------
    run_backtest      (Rotation): deployment=0.90, sl_pct=-5
    run_backtest_hold (Halten)  : deployment=0.70, sl_pct=-8

Wer beide mit Voreinstellungen laufen laesst, vergleicht nicht zwei Strategien,
sondern zwei verschiedene Kapitaleinsaetze mit zwei verschiedenen Stopps. Halten
haette dann mit 20 Prozentpunkten WENIGER Kapital im Markt fast dieselbe Rendite
erzielt — pro eingesetztem Franken waere es sogar besser gewesen, und der
Rotations-Vorteil bliebe unsichtbar.

ZWEITER VERDACHT: DER BOT ROTIERT LAENGST
------------------------------------------
Der Live-Bot besetzt frei werdende Plaetze mit den dann besten Namen. Feuern die
Exits haeufig, entsteht daraus eine faktische Rotation — nur exit-getrieben statt
kalendergetrieben. Dann waere der Befund aus R-B26 bereits eingepreist und es gibt
schlicht nichts zu tun.

Deshalb wird hier zusaetzlich die tatsaechliche HALTEDAUER gemessen. Liegt sie bei
rund einem Monat, rotiert der Bot bereits im Takt des Vorteils.

AUFRUF
------
    python scripts/rotation_vs_halten.py
"""
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

START = "2017-01-01"
END = "2026-07-01"
TOP_N = 15


def _kennzahlen(res: dict, label: str) -> dict:
    # run_backtest/-_hold liefern nur Rohdaten; die Kennzahlen rechnet _metrics.
    from app import signal_stack_backtester as bt
    m = bt._metrics(res.get("monthly_pct") or [], res.get("equity_final", 1.0),
                    res.get("trades") or [])
    res = {**res, **m}
    trades = res.get("trades") or []
    tage = [t["days"] for t in trades if t.get("days")]
    gruende = {}
    for t in trades:
        gruende[t["reason"]] = gruende.get(t["reason"], 0) + 1
    return {
        "label": label,
        "rendite_pct": res.get("total_return_pct"),
        "sharpe": res.get("sharpe_ann"),
        "max_dd_pct": res.get("max_drawdown_pct"),
        "win_rate_pct": res.get("win_rate_pct"),
        "avg_trade_pct": res.get("avg_trade_pct"),
        "n_trades": len(trades),
        "haltedauer_median": statistics.median(tage) if tage else None,
        "haltedauer_mittel": round(statistics.mean(tage), 1) if tage else None,
        "gruende": dict(sorted(gruende.items(), key=lambda kv: -kv[1])),
    }


def _zeile(k: dict) -> str:
    return (f"  {k['label']:<34} | {_f(k['rendite_pct'], 1):>9} | "
            f"{_f(k['sharpe'], 2):>7} | {_f(k['max_dd_pct'], 1):>7} | "
            f"{_f(k['avg_trade_pct'], 2):>7} | {k['n_trades']:>6} | "
            f"{_f(k['haltedauer_median'], 0):>7}")


def main() -> int:
    from app import edgar_client, sp600_universe
    from app import signal_stack_backtester as bt
    from app.config_manager import load_config, save_json

    cfg = load_config()
    lev = cfg.get("leverage", {}) or {}
    dt = cfg.get("demo_trading", {}) or {}

    # LIVE-Exit-Config — beide Modi bekommen exakt dieselbe.
    tp = dt.get("take_profit_pct")
    tp = None if (tp is None or tp >= 100) else float(tp)   # 999 = aus
    exits = {
        "sl_pct": dt.get("stop_loss_pct", -8),
        "tp_pct": tp,
        "trail_act_pct": lev.get("trailing_sl_activation_pct"),
        "trail_pct": lev.get("trailing_sl_pct"),
        "tranches": lev.get("tp_tranches") or None,
    }
    print("Live-Exit-Config (identisch fuer beide Modi):")
    print("  " + json.dumps(exits, default=str))
    print()

    disabled = set(cfg.get("disabled_symbols") or [])
    symbols = [s for s in sp600_universe.get_symbols() if s not in disabled]
    facts = edgar_client.load_facts()
    print("Lade Kurshistorie...", flush=True)
    price_hist = bt.load_price_history(symbols, START, END)
    print("Praeberechne Rankings...", flush=True)
    picks = bt.precompute_monthly_picks(price_hist, facts, START, END)
    print()

    ergebnisse = []

    # ------------------------------------------------------------------
    # TEIL 1: Der Juni-Vergleich, so wie er damals lief (Voreinstellungen)
    # ------------------------------------------------------------------
    print("=" * 92)
    print("TEIL 1 — der Juni-Vergleich mit VOREINSTELLUNGEN (der mutmassliche Fehler)")
    print("=" * 92)
    print(f"  {'Variante':<34} | {'Rendite':>9} | {'Sharpe':>7} | "
          f"{'MaxDD':>7} | {'O-Trade':>7} | {'Trades':>6} | {'Halte-d':>7}")
    print("  " + "-" * 96)

    r_alt = bt.run_backtest(price_hist, facts, symbols, START, END,
                            top_n=TOP_N, picks_by_month=picks)  # deployment 0.90, sl -5
    h_alt = bt.run_backtest_hold(price_hist, facts, symbols, START, END,
                                 top_n=TOP_N, picks_by_month=picks)  # 0.70, sl -8
    for k in (_kennzahlen(r_alt, "Rotation (Vorgabe 0.90 / SL-5)"),
              _kennzahlen(h_alt, "Halten   (Vorgabe 0.70 / SL-8)")):
        print(_zeile(k)); ergebnisse.append(k)

    # ------------------------------------------------------------------
    # TEIL 2: Fairer Vergleich — identische Parameter
    # ------------------------------------------------------------------
    print()
    print("=" * 92)
    print("TEIL 2 — FAIRER VERGLEICH: identische Live-Exits, identischer Kapitaleinsatz")
    print("=" * 92)
    print(f"  {'Variante':<34} | {'Rendite':>9} | {'Sharpe':>7} | "
          f"{'MaxDD':>7} | {'O-Trade':>7} | {'Trades':>6} | {'Halte-d':>7}")
    print("  " + "-" * 96)

    for dep in (0.70, 0.90):
        rot = bt.run_backtest(price_hist, facts, symbols, START, END,
                              top_n=TOP_N, deployment=dep, picks_by_month=picks,
                              **exits)
        hold = bt.run_backtest_hold(price_hist, facts, symbols, START, END,
                                    top_n=TOP_N, deployment=dep, picks_by_month=picks,
                                    **exits)
        for k in (_kennzahlen(rot, f"Rotation (Einsatz {dep:.0%})"),
                  _kennzahlen(hold, f"Halten   (Einsatz {dep:.0%})")):
            print(_zeile(k)); ergebnisse.append(k)

    # ------------------------------------------------------------------
    # TEIL 3: Rotiert der Bot laengst? — Haltedauern + Exit-Gruende
    # ------------------------------------------------------------------
    print()
    print("=" * 92)
    print("TEIL 3 — ROTIERT DER BOT BEREITS? (Halten-Modus mit Live-Exits, Einsatz 90 %)")
    print("=" * 92)
    hold_live = bt.run_backtest_hold(price_hist, facts, symbols, START, END,
                                     top_n=TOP_N, deployment=0.90,
                                     picks_by_month=picks, **exits)
    k = _kennzahlen(hold_live, "Halten (Live-Config)")
    tage = [t["days"] for t in (hold_live.get("trades") or []) if t.get("days")]
    if tage:
        tage_sort = sorted(tage)
        print(f"  Haltedauer Median : {statistics.median(tage):.0f} Handelstage "
              f"(~{statistics.median(tage) / 21:.1f} Monate)")
        print(f"  Haltedauer Mittel : {statistics.mean(tage):.1f} Handelstage")
        print(f"  25 %% / 75 %% Quantil: {tage_sort[len(tage_sort)//4]:.0f} / "
              f"{tage_sort[3*len(tage_sort)//4]:.0f} Handelstage")
        unter_21 = sum(1 for d in tage if d <= 21) / len(tage) * 100
        print(f"  Anteil <= 21 Tage : {unter_21:.1f} %  "
              "(innerhalb des Vorteils-Fensters geschlossen)")
    print()
    print("  Exit-Gruende:")
    for grund, n in k["gruende"].items():
        print(f"    {grund:<16} {n:>5}  ({n / max(1, k['n_trades']) * 100:.1f} %)")

    save_json("rotation_vs_halten.json", {
        "start": START, "ende": END, "exits": exits,
        "ergebnisse": ergebnisse,
        "haltedauer_median_tage": statistics.median(tage) if tage else None,
        "anteil_bis_21_tage_pct": round(unter_21, 1) if tage else None,
    })
    print()
    print("Gespeichert: data/rotation_vs_halten.json")
    return 0


def _f(v, n):
    return "n/a" if v is None else f"{v:.{n}f}"


if __name__ == "__main__":
    raise SystemExit(main())
