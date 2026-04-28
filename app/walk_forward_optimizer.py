"""
app/walk_forward_optimizer.py — Walk-Forward-Optimization (E1, vorgezogen aus Q1)

Was:
   Statt einmal auf 4 Jahren historischer Daten zu optimieren (klassisch
   ueberfittet), splitten wir die Historie in viele Rolling-Windows. Pro
   Window: optimiere auf Train-Daten, evaluiere ehrlich auf Test-Daten.
   Aggregiere alle OOS-Scores -> echte Sharpe-Schaetzung.

Warum:
   Der bisherige Optimizer (app/optimizer.py) macht ein 80/20 Train/Test-Split
   EINMAL ueber die ganze Historie. Das fuehrt zu zwei Problemen:
     1. "Lucky Window" Bias - das eine 80%-Train-Window kann zufaellig gut
        sein. Mit 4 Rolling-Windows + 4 OOS-Tests ist der Mittelwert robust.
     2. Regime-Bias - Bull-2023 in Train kann Bear-2024 in Test perfekt
        slappen lassen. Mit Rolling-Windows sehen wir beides ehrlich.

   Real-Money-Cutover ist 28.05.2026. WFO sagt uns *vorher*: ist Sharpe 3.5
   nur Lucky-Backtest oder echte Edge?

Roadmap-Plan:
   Di-Mi (28.-30.04.): Phase 1 - Framework (DIESE DATEI) + Rolling-Windows
   Do-Fr (01.-02.05.): Phase 2 - Pro-Window Backtest-Runs + Aggregations-Stats
   Sa  (03.05.):       Erster vollstaendiger Run gegen historische Daten
   So  (04.05.):       Resultat-Analyse
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

import pandas as pd

log = logging.getLogger(__name__)


# ============================================================
# OPTION A — DREI HYPOTHESEN (Carlos' Entscheidung 2026-04-28)
# ============================================================
# Schmal-und-ehrlich Grid (24 Kombinationen statt 3'000 im Optimizer).
# Jede Parameter-Achse testet eine konkrete Hypothese:
#
#   stop_loss_pct   "Welche SL-Tiefe haelt die Wahrheit zwischen Whipsaw
#                    und zu spaeten Exits?" -> 3 SL-Werte
#   take_profit_pct "Wo schlagen Insider-Mover am haeufigsten zu? Knapp
#                    oder ausgereizt?"     -> 4 TP-Werte
#   min_scanner_score "Wieviel Scanner-Edge braucht ein Trade minimum?"
#                                          -> 2 Score-Schwellen
#
# Total: 3 * 4 * 2 = 24 Kombinationen pro Window
#        x 6 Windows = 144 Backtests Runtime ~10-15 Min
#
# Why nicht alle Optimizer-Parameter (Option B, 3000 Kombos):
#   Bonferroni-Korrektur — bei 3000 Tests ist die effektive Sharpe-Schwelle
#   um Faktor sqrt(N) hoeher. Sharpe 3.5 in B == Sharpe ~1.8-2.2 ehrliche
#   Schaetzung. In A == Sharpe ~3.0-3.2. Wir wollen die ehrliche Zahl.
#
# Aenderungen NUR via git commit (mit Begruendung) — analog Strategy A
# fuer disabled_symbols. Quant-Disziplin = Edge.
WFO_PARAM_GRID: dict[str, list] = {
    "stop_loss_pct":     [-3.0, -4.0, -5.0],
    "take_profit_pct":   [9, 12, 15, 18],
    "min_scanner_score": [40, 50],
}


def total_param_combinations() -> int:
    """Helper: Anzahl Grid-Kombinationen (fuer Logging + UI)."""
    n = 1
    for vals in WFO_PARAM_GRID.values():
        n *= len(vals)
    return n


# ============================================================
# WINDOW-KONFIGURATION
# ============================================================

@dataclass
class WFOConfig:
    """Konfiguration eines WFO-Laufs.

    Standardwerte folgen Roadmap-Plan: 24 Monate Train, 6 Monate Test, 6 Monate
    Step. Bei 4 Jahren Historie ergibt das ~3 Windows. Bei 5 Jahren ~5 Windows.

    Trade-offs der Standardwerte (Why):
    - 24Mo Train: lang genug fuer min. 1 voller Marktzyklus (Bull/Bear-Wechsel)
      und genug Sample-Size fuer 1300+ Trades. Kuerzer waere zu volatil.
    - 6Mo Test: lang genug um Regime-Wechsel im OOS zu sehen, kurz genug
      um schnell zum naechsten Window zu kommen.
    - 6Mo Step: Non-Overlapping Test-Windows (jeder Test-Bereich wird genau
      einmal geprueft). Verhindert Doppel-Zaehlen.
    """
    train_months: int = 24
    test_months: int = 6
    step_months: int = 6
    min_train_trades: int = 200  # weniger -> Window verworfen (zu wenig Sample)


@dataclass
class Window:
    """Ein einzelnes Rolling-Window (Train + Test Zeitraum)."""
    idx: int                       # 0-basierte Window-Nummer
    train_start: pd.Timestamp
    train_end: pd.Timestamp        # exklusiv
    test_start: pd.Timestamp       # = train_end
    test_end: pd.Timestamp         # exklusiv
    # Resultate werden in Phase 2 befuellt
    best_params: Optional[dict] = None
    is_score: Optional[float] = None     # In-Sample (auf Train) Score
    oos_score: Optional[float] = None    # Out-of-Sample (auf Test) Score
    oos_trades: int = 0
    oos_metrics: dict = field(default_factory=dict)


# ============================================================
# PHASE 1: ROLLING-WINDOWS GENERIEREN
# ============================================================

def build_windows(
    history_start: pd.Timestamp,
    history_end: pd.Timestamp,
    cfg: WFOConfig,
) -> list[Window]:
    """Generiert alle Rolling-Windows zwischen history_start und history_end.

    Logik: starte mit Train-Window beginnend bei history_start. Schiebe um
    step_months vor. Hoere auf wenn Test-Window-Ende > history_end.

    Args:
        history_start: erstes verfuegbares Datum in den Histories
        history_end:   letztes verfuegbares Datum
        cfg:           Window-Groessen-Konfiguration

    Returns:
        Liste von Windows in chronologischer Reihenfolge.
    """
    windows: list[Window] = []
    cursor = history_start
    idx = 0
    train_delta = pd.DateOffset(months=cfg.train_months)
    test_delta = pd.DateOffset(months=cfg.test_months)
    step_delta = pd.DateOffset(months=cfg.step_months)

    while True:
        train_start = cursor
        train_end = train_start + train_delta
        test_start = train_end
        test_end = test_start + test_delta
        if test_end > history_end:
            break
        windows.append(Window(
            idx=idx,
            train_start=train_start,
            train_end=train_end,
            test_start=test_start,
            test_end=test_end,
        ))
        cursor = cursor + step_delta
        idx += 1

    return windows


def slice_histories(
    histories: dict[str, pd.DataFrame],
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> dict[str, pd.DataFrame]:
    """Schneidet alle Symbol-Histories auf den angegebenen Zeitraum.

    Liefert nur Symbole zurueck die im Zeitraum >= 30 Tage Daten haben
    (verhindert Edge-Cases fuer frisch-gelistete Aktien).
    """
    sliced = {}
    for sym, df in histories.items():
        if df is None or df.empty:
            continue
        # DataFrame-Index ist DatetimeIndex
        try:
            mask = (df.index >= start) & (df.index < end)
            piece = df.loc[mask]
            if len(piece) >= 30:
                sliced[sym] = piece
        except Exception as e:
            log.debug("slice_histories: skip %s (%s)", sym, e)
    return sliced


# ============================================================
# PHASE 2: PRO-WINDOW BACKTEST  (Stub — wird in Mi/Do umgesetzt)
# ============================================================

# TODO Phase 2 (Mittwoch-Donnerstag):
# def run_window_backtest(window: Window, histories, base_config) -> Window:
#     """Optimiere auf Train, evaluiere auf Test, schreibe Resultat in Window."""
#     ...


# ============================================================
# AGGREGATION (Stub — Donnerstag-Freitag)
# ============================================================

# TODO Phase 2:
# def aggregate_oos_results(windows: list[Window]) -> dict:
#     """Aggregiert OOS-Scores: Mean OOS-Sharpe, Decay-Curve, Stabilitaet."""
#     ...


# ============================================================
# STATUS-FILE FUER DASHBOARD (data/wfo_status.json)
# ============================================================

WFO_STATUS_FILENAME = "wfo_status.json"


def read_status() -> dict:
    """Liest den letzten WFO-Status fuer das Dashboard.

    Returns:
        dict mit 'state' ('idle'/'running'/'done'/'error'), 'last_run',
        'windows', 'aggregate', 'config'. Default: idle wenn nie gelaufen.
    """
    try:
        from app.config_manager import load_json
        data = load_json(WFO_STATUS_FILENAME)
        if data:
            return data
    except Exception as e:
        log.debug("WFO read_status: %s", e)
    return {
        "state": "idle",
        "last_run": None,
        "next_run_planned": "2026-05-03 (Sa, erster vollstaendiger Lauf)",
        "windows": [],
        "aggregate": None,
        "config": {
            "param_grid": WFO_PARAM_GRID,
            "param_combinations": total_param_combinations(),
            "windows_planned": 6,  # bei 5J Historie + 24/6/6 Mo Default-Cfg
            "approach": "Option A (Elite-Pfad, schmal-und-ehrlich, 3 Hypothesen)",
        },
    }


def write_status(state: str, **payload) -> None:
    """Schreibt aktuellen WFO-Status fuer Dashboard-Polling.

    Wird in Phase 2 vom run_window_backtest und aggregate_oos_results
    aufgerufen (state-Transitionen: idle -> running -> done/error).
    """
    try:
        from app.config_manager import save_json
        cur = read_status() or {}
        cur["state"] = state
        cur["last_run"] = datetime.now().isoformat()
        cur.update(payload)
        save_json(WFO_STATUS_FILENAME, cur)
    except Exception as e:
        log.warning("WFO write_status fehlgeschlagen: %s", e)


# ============================================================
# CLI fuer Phase 1 — Smoke-Test des Window-Builders
# ============================================================

if __name__ == "__main__":
    # Smoke-Test: 5 Jahre Historie -> erwartete Window-Anzahl
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    cfg = WFOConfig()
    history_start = pd.Timestamp("2021-01-01")
    history_end = pd.Timestamp("2026-04-27")
    windows = build_windows(history_start, history_end, cfg)
    print(f"WFOConfig: train={cfg.train_months}Mo / test={cfg.test_months}Mo / step={cfg.step_months}Mo")
    print(f"History: {history_start.date()} -> {history_end.date()} ({(history_end-history_start).days} Tage)")
    print(f"Windows: {len(windows)}")
    for w in windows:
        print(f"  W{w.idx}: train {w.train_start.date()}..{w.train_end.date()} "
              f"-> test {w.test_start.date()}..{w.test_end.date()}")
