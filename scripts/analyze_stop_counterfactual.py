"""Counterfactual-Analyse der Stop-Loss-Closes — Vorbereitung Post-Soak-Exit-Rekalibrierung.

Liest NUR Daten (trade_history.json + yfinance-Kurse) — KEIN Eingriff, kein Trading,
kein Soak-Einfluss. Beantwortet die Kernfrage aus docs/POST_SOAK_EXIT_RECALIBRATION_PLAN.md:
Haben sich die ausgestoppten Aktien NACH dem Stop wieder erholt (= Stop verfrueht,
SL zu eng -> weiten) oder fielen sie weiter (= Stop war korrektes Risk-Management)?

Methodik: Pro Stop-Close den Kursverlauf der naechsten N Handelstage holen und pruefen,
ob der Kurs wieder ueber den Exit- bzw. den Einstiegspreis stieg.

Aufruf (auf dem VPS, jederzeit — am aussagekraeftigsten nach >=30 Trades):
  docker exec investpilot python scripts/analyze_stop_counterfactual.py
  docker exec investpilot python scripts/analyze_stop_counterfactual.py --since 2026-06-11 --horizon 15 --include-trailing
"""
import argparse
import sys
from datetime import datetime, timedelta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-06-11", help="nur Closes ab diesem Datum (Default: Soak-Start)")
    ap.add_argument("--horizon", type=int, default=15, help="Handelstage nach dem Exit, die geprueft werden")
    ap.add_argument("--include-trailing", action="store_true", help="auch TRAILING-Closes einbeziehen")
    args = ap.parse_args()

    from app.config_manager import load_json
    try:
        import yfinance as yf
    except ImportError:
        print("yfinance nicht verfuegbar"); return 1

    th = [t for t in (load_json("trade_history.json") or []) if isinstance(t, dict)]
    reasons = ("STOP_LOSS",) + (("TRAILING",) if args.include_trailing else ())

    def is_target(t):
        a = str(t.get("action", "")).upper(); st = str(t.get("status", "")).lower()
        if "FAILED" in a or st in ("close_failed", "skipped", "submitted", "failed"):
            return False
        return (any(r in a for r in reasons) and t.get("pnl_pct") is not None
                and t.get("avg_fill_price"))

    closes = [t for t in th if str(t.get("timestamp", "")) >= args.since and is_target(t)]
    if not closes:
        print("Keine passenden Stop-Closes seit %s." % args.since); return 0

    print("Counterfactual-Analyse: %d Stop-Close(s) seit %s, Horizont %d Handelstage\n"
          % (len(closes), args.since, args.horizon))
    hdr = "%-6s %-11s %8s %8s %6s | %8s %8s %8s | %s"
    print(hdr % ("Sym", "Exit-Datum", "Exit", "Entry", "PnL%", "MaxDanach", "vsExit%", "vsEntry%", "Urteil"))
    print("-" * 100)

    prem_entry = prem_exit = correct = incomplete = usable = 0
    for t in sorted(closes, key=lambda x: x.get("timestamp", "")):
        sym = t["symbol"]; exit_px = float(t["avg_fill_price"]); pnl = float(t["pnl_pct"])
        entry_px = exit_px / (1 + pnl / 100.0) if pnl != -100 else None
        exit_dt = datetime.fromisoformat(str(t["timestamp"])[:19])
        start = exit_dt.date()
        end = (exit_dt + timedelta(days=args.horizon * 2 + 6)).date()
        try:
            h = yf.Ticker(sym).history(start=start.isoformat(), end=end.isoformat(), interval="1d")
            post = [float(c) for c in h["Close"].dropna().tolist()]
        except Exception as e:
            print("%-6s  yfinance-Fehler: %s" % (sym, e)); continue
        # erster Bar ~ Exit-Tag -> die Tage DANACH bewerten
        post = post[1:1 + args.horizon] if len(post) > 1 else []
        if not post:
            incomplete += 1
            print(hdr % (sym, start.isoformat(), "%.2f" % exit_px, "%.2f" % (entry_px or 0),
                         "%.1f" % pnl, "-", "-", "-", "Fenster leer (zu frisch)"))
            continue
        usable += 1
        mx = max(post)
        vs_exit = (mx / exit_px - 1) * 100
        vs_entry = (mx / entry_px - 1) * 100 if entry_px else None
        if entry_px and mx >= entry_px:
            verdict = "VERFRUEHT (ueber Entry zurueck)"; prem_entry += 1
        elif mx > exit_px:
            verdict = "teilw. erholt (ueber Exit)"; prem_exit += 1
        else:
            verdict = "korrekt (fiel weiter)"; correct += 1
        print(hdr % (sym, start.isoformat(), "%.2f" % exit_px, "%.2f" % (entry_px or 0),
                     "%.1f" % pnl, "%.2f" % mx, "%+.1f" % vs_exit,
                     ("%+.1f" % vs_entry if vs_entry is not None else "-"), verdict))

    print("-" * 100)
    print("\nZUSAMMENFASSUNG (auswertbar: %d):" % usable)
    print("  VERFRUEHT (ueber Entry erholt) : %d  -> Stop zu eng, These haette getragen" % prem_entry)
    print("  teilweise erholt (ueber Exit)  : %d  -> haette weniger Verlust gegeben" % prem_exit)
    print("  korrekt (fiel weiter)          : %d  -> Stop war richtig (echtes Risk-Mgmt)" % correct)
    if incomplete:
        print("  Fenster zu frisch (<Horizont)  : %d  -> spaeter erneut laufen" % incomplete)
    if usable:
        pct = prem_entry / usable * 100
        print("\n  -> %.0f%% der ausgewerteten Stops waren VERFRUEHT (Aktie kam ueber Entry zurueck)." % pct)
        print("     Hoher Anteil  -> SL weiten ist Hebel #1 im Rekalibrierungs-Plan.")
        print("     Niedriger Anteil -> Stops waren korrekt, NICHT weiten (echtes Risk-Mgmt).")
    print("\n(Reine Lese-Analyse — keine Live-Aenderung. Siehe docs/POST_SOAK_EXIT_RECALIBRATION_PLAN.md)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
