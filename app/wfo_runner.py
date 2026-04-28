"""
Standalone WFO-Runner — laeuft auf GitHub Actions (7 GB RAM).

Mirror zu backtest_runner / ml_training_runner. Grund: WFO ist computationally
heavier als Backtest (144 Backtests statt 1) und sollte den Live-Trading-
Container nicht blockieren. GitHub Actions Runner hat 7 GB RAM und ist
vollstaendig isoliert.

Workflow:
  1. Restore Brain-State + Config aus Gist (fuer disabled_symbols, config.json)
  2. run_walk_forward() laeuft alle 6 Windows durch
  3. wfo_status.json + wfo_history.json (append zur time-series) werden in
     den Gist gepusht (via backup_wfo_results)
  4. Bot-Watchdog (check_and_reload_wfo_output) laedt die Files +
     prueft Hard-Gates -> Telegram-Alert bei Anomalie

Usage:
    python -m app.wfo_runner [triggered_by] [--years N]

ENV:
    GITHUB_TOKEN   Pflicht (Gist Read/Write)
"""

from __future__ import annotations

import logging
import sys
import traceback
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [WFO-RUNNER] [%(levelname)s] %(message)s",
)
log = logging.getLogger("wfo_runner")


def _write_status(**fields):
    try:
        from app.config_manager import load_json, save_json
        status = load_json("wfo_status.json") or {}
        status.update(fields)
        status["updated_at"] = datetime.now().isoformat()
        save_json("wfo_status.json", status)
    except Exception as e:
        log.warning(f"Status-Write fehlgeschlagen: {e}")


def _append_history(status_snapshot: dict):
    """Append diesen WFO-Run zur Time-Series wfo_history.json.

    History ist eine Liste von dicts mit timestamp + aggregate + best-params
    pro Run. So koennen wir ueber Monate hinweg den Sharpe-Trend beobachten
    und Strategy-Drift detektieren.
    """
    try:
        from app.config_manager import load_json, save_json
        hist = load_json("wfo_history.json") or {"runs": []}
        if not isinstance(hist, dict):
            hist = {"runs": []}
        runs = hist.get("runs", [])

        agg = status_snapshot.get("aggregate") or {}
        windows = status_snapshot.get("windows") or []

        # Nur "best_params" Counter behalten — welche Params wurden in diesem Run
        # am haeufigsten gewaehlt? (3 Hypothesen-Stabilitaet ueber Zeit)
        param_summary: dict[str, dict] = {}
        for w in windows:
            bp = w.get("best_params") or {}
            for k, v in bp.items():
                param_summary.setdefault(k, {})
                key = str(v)
                param_summary[k][key] = param_summary[k].get(key, 0) + 1

        runs.append({
            "timestamp": datetime.now().isoformat(),
            "trigger": status_snapshot.get("trigger") or "unknown",
            "windows_total": len(windows),
            "mean_oos_sharpe": agg.get("mean_oos_sharpe"),
            "mean_is_sharpe": agg.get("mean_is_sharpe"),
            "sharpe_decay_pct": agg.get("sharpe_decay_pct"),
            "oos_stability_std": agg.get("oos_stability_std"),
            "mean_oos_trades": agg.get("mean_oos_trades"),
            "mean_oos_max_dd": agg.get("mean_oos_max_dd"),
            "param_summary": param_summary,
        })
        # Nur die letzten 60 Eintraege behalten (5 Jahre x 12 Monate)
        if len(runs) > 60:
            runs = runs[-60:]
        hist["runs"] = runs
        hist["updated_at"] = datetime.now().isoformat()
        save_json("wfo_history.json", hist)
        log.info(f"WFO-History: {len(runs)} Runs total")
    except Exception as e:
        log.warning(f"History-Append fehlgeschlagen: {e}")


def _push_results():
    try:
        from app.persistence import backup_wfo_results
        ok = backup_wfo_results()
        log.info(f"Push to Gist: {'OK' if ok else 'FAILED'}")
        return ok
    except Exception as e:
        log.exception(f"backup_wfo_results Fehler: {e}")
        return False


def main():
    triggered_by = sys.argv[1] if len(sys.argv) > 1 else "manual"
    years = 5
    if "--years" in sys.argv:
        i = sys.argv.index("--years")
        try:
            years = int(sys.argv[i + 1])
        except (IndexError, ValueError):
            pass

    log.info("=" * 60)
    log.info(f"WFO-Runner gestartet (trigger={triggered_by}, years={years})")
    log.info("=" * 60)

    # Restore Brain/Config aus Gist (analog backtest_runner)
    try:
        from app.persistence import restore_from_cloud_with_gdrive
        restore_from_cloud_with_gdrive()
    except Exception as e:
        log.warning(f"Restore from cloud fehlgeschlagen (non-fatal): {e}")

    _write_status(state="running", phase="starting",
                  trigger=triggered_by,
                  message=f"GH-Action WFO-Run gestartet (years={years})")

    try:
        from app.walk_forward_optimizer import run_walk_forward
        result = run_walk_forward(years=years)

        # Trigger im Status persistieren fuer History-Append
        from app.config_manager import load_json, save_json
        status = load_json("wfo_status.json") or {}
        status["trigger"] = triggered_by
        save_json("wfo_status.json", status)

        # History anhaengen
        _append_history(status)

        agg = result.get("aggregate") or {}
        log.info("=" * 60)
        log.info(f"WFO done. Mean OOS Sharpe: {agg.get('mean_oos_sharpe')}")
        log.info(f"           Sharpe Decay:    {agg.get('sharpe_decay_pct')}%")
        log.info(f"           Stability:       {agg.get('oos_stability_std')}")
        log.info("=" * 60)

        _push_results()
        return 0
    except Exception as e:
        log.exception(f"WFO-Run gescheitert: {e}")
        _write_status(state="error",
                      error=f"{type(e).__name__}: {e}",
                      traceback=traceback.format_exc()[:2000])
        _push_results()  # auch im Error-Fall pushen, damit Bot den Status sieht
        return 1


if __name__ == "__main__":
    sys.exit(main())
