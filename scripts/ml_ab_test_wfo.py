"""v37h+2 (3C, 17.05.2026) — ML-Scoring A/B-Test WFO-Sharpe.

PROBLEM: ML-Training-Workflow laeuft woechentlich, traineiertes Modell wird
geladen, aber `demo_trading.use_ml_scoring=False` -> Modell wird nie genutzt.
Niemand weiss ob ML besser oder schlechter waere.

LOESUNG (3C): A/B-Test ueber die naechsten Tage durchfuehren.

Workflow:
  1. Diesem Skript einen WFO-Lauf machen mit use_ml_scoring=False (Baseline)
  2. Anderen WFO-Lauf mit use_ml_scoring=True (Treatment)
  3. Vergleiche OOS-Sharpe + Trade-Count + Win-Rate
  4. Entscheidung:
     - Treatment > Baseline + 0.5 Sharpe -> Aktivieren
     - Treatment < Baseline -> Disable ML-Training GH-Action
     - Tie -> Disable ML-Training (Komplexitaet ohne Nutzen)

Da WFO ein 10-15-Minuten-Lauf ist, machen wir A/B als manuelle Sequenz
mit GitHub-Actions-Trigger statt automatisiertem Vergleich.

Vorbereitung:
  - Letzten WFO-Lauf-Ergebnis sichern als "baseline"
  - use_ml_scoring=True setzen in config.json
  - Erneuten WFO-Lauf triggern
  - Ergebnisse vergleichen
  - Wenn schlechter: rueckgaengig + ML-GH-Action disablen
  - Wenn besser: bewusst aktiviert lassen

USAGE:
    python -m scripts.ml_ab_test_wfo --phase=prepare    # snapshot baseline
    python -m scripts.ml_ab_test_wfo --phase=compare    # nach 2. WFO-Lauf
    python -m scripts.ml_ab_test_wfo --phase=decide     # Entscheidung anwenden
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

BASELINE_FILE = Path(__file__).parent.parent / "data" / "ml_ab_test_baseline.json"
TREATMENT_FILE = Path(__file__).parent.parent / "data" / "ml_ab_test_treatment.json"
WFO_STATUS_FILE = Path(__file__).parent.parent / "data" / "wfo_status.json"
CONFIG_FILE = Path(__file__).parent.parent / "data" / "config.json"


def _load_wfo_metrics():
    """Liest aktuellen WFO-Stand und extrahiert die wichtigsten Metriken."""
    if not WFO_STATUS_FILE.exists():
        print("ERROR: wfo_status.json nicht gefunden.")
        return None
    data = json.loads(WFO_STATUS_FILE.read_text(encoding="utf-8"))
    windows = data.get("windows", []) or []
    if not windows:
        return None
    avg_sharpe = sum(w.get("oos_sharpe", 0) for w in windows) / len(windows)
    avg_trades = sum(w.get("oos_trades", 0) for w in windows) / len(windows)
    avg_winrate = sum(w.get("oos_winrate", 0) for w in windows) / len(windows)
    return {
        "captured_at": datetime.now().isoformat(),
        "n_windows": len(windows),
        "avg_oos_sharpe": round(avg_sharpe, 3),
        "avg_oos_trades": round(avg_trades, 1),
        "avg_oos_winrate": round(avg_winrate, 3),
        "raw_windows": windows,
    }


def phase_prepare():
    """Snapshot der aktuellen WFO-Metriken als Baseline (use_ml_scoring=False)."""
    metrics = _load_wfo_metrics()
    if metrics is None:
        print("Keine WFO-Daten verfuegbar.")
        return 1
    metrics["use_ml_scoring"] = False
    BASELINE_FILE.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    print(f"Baseline (ML OFF) gespeichert: {BASELINE_FILE}")
    print(f"  Avg OOS-Sharpe: {metrics['avg_oos_sharpe']}")
    print(f"  Avg Trades: {metrics['avg_oos_trades']}")
    print(f"  Avg WinRate: {metrics['avg_oos_winrate']}")
    print()
    print("Naechste Schritte:")
    print("  1. config.json: demo_trading.use_ml_scoring = True")
    print("  2. WFO-Lauf triggern (manual via Dashboard oder GH-Action)")
    print("  3. Nach Lauf: python -m scripts.ml_ab_test_wfo --phase=compare")
    return 0


def phase_compare():
    """Vergleich nach 2. WFO-Lauf (use_ml_scoring=True)."""
    if not BASELINE_FILE.exists():
        print("ERROR: Baseline nicht da. Erst --phase=prepare ausfuehren.")
        return 1
    baseline = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))

    treatment = _load_wfo_metrics()
    if treatment is None:
        print("Keine WFO-Daten verfuegbar.")
        return 1
    treatment["use_ml_scoring"] = True
    TREATMENT_FILE.write_text(json.dumps(treatment, indent=2), encoding="utf-8")

    print("=" * 55)
    print("ML A/B-Test Vergleich")
    print("=" * 55)
    print(f"Baseline  (ML OFF): Sharpe={baseline['avg_oos_sharpe']}, "
          f"Trades={baseline['avg_oos_trades']}, WinRate={baseline['avg_oos_winrate']}")
    print(f"Treatment (ML ON):  Sharpe={treatment['avg_oos_sharpe']}, "
          f"Trades={treatment['avg_oos_trades']}, WinRate={treatment['avg_oos_winrate']}")
    print()

    diff_sharpe = treatment["avg_oos_sharpe"] - baseline["avg_oos_sharpe"]
    print(f"Sharpe-Diff: {diff_sharpe:+.3f}")
    if diff_sharpe > 0.5:
        print("ENTSCHEIDUNG: Treatment ist signifikant besser -> ML AKTIVIEREN.")
        return 0
    elif diff_sharpe < -0.1:
        print("ENTSCHEIDUNG: Treatment schlechter -> ML DISABLEN.")
        return 0
    else:
        print("ENTSCHEIDUNG: Tie -> ML DISABLEN (Komplexitaet ohne Nutzen).")
        return 0


def phase_decide():
    """Wendet die Entscheidung an: config.json + GH-Action toggle."""
    if not TREATMENT_FILE.exists():
        print("ERROR: Treatment-Daten fehlen. Erst --phase=compare.")
        return 1
    baseline = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
    treatment = json.loads(TREATMENT_FILE.read_text(encoding="utf-8"))

    diff_sharpe = treatment["avg_oos_sharpe"] - baseline["avg_oos_sharpe"]
    should_enable = diff_sharpe > 0.5

    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    config.setdefault("demo_trading", {})
    config["demo_trading"]["use_ml_scoring"] = bool(should_enable)
    CONFIG_FILE.write_text(json.dumps(config, indent=2), encoding="utf-8")

    decision = "AKTIVIERT" if should_enable else "DEAKTIVIERT"
    print(f"ML-Scoring {decision} (diff_sharpe={diff_sharpe:+.3f})")
    print("Hinweis: ML-Training-GH-Action sollte entsprechend angepasst werden")
    print("  - Wenn deaktiviert: gh workflow disable 'ML Training'")
    print("  - Wenn aktiviert: GH-Action bleibt, Live-Bot nutzt jetzt ML-Score")
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--phase", required=True, choices=["prepare", "compare", "decide"])
    args = p.parse_args()
    if args.phase == "prepare":
        return phase_prepare()
    if args.phase == "compare":
        return phase_compare()
    if args.phase == "decide":
        return phase_decide()
    return 1


if __name__ == "__main__":
    sys.exit(main())
