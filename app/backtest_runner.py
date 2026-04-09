"""
Standalone Backtest-Runner — laeuft auf GitHub Actions (7 GB RAM).

Mirror zum optimizer_runner. Grund: Render Free Tier hat nur 512 MB, ein
Full-Backtest (71 Symbole x 5J History + VIX + Earnings + Full-Period-Sim
+ Walk-Forward) sprengt das zuverlaessig und killt den Web-Container
(OOM -> 502 fuer Minuten).

Workflow:
  1. Restore Brain-State + Config aus Gist (fuer disabled_symbols, config.json)
  2. run_full_backtest() ausfuehren
  3. backtest_status.json + backtest_results.json + universe_health.json
     isoliert in den Gist pushen (via backup_backtest_results)
  4. Render-Watchdog (check_and_reload_backtest_output) laedt die Files
     beim naechsten Reload-Zyklus nach

Usage:
    python -m app.backtest_runner [triggered_by]

ENV:
    GITHUB_TOKEN   Pflicht (Gist Read/Write)
"""

import logging
import os
import sys
import traceback
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [BT-RUNNER] [%(levelname)s] %(message)s",
)
log = logging.getLogger("backtest_runner")


def _write_status(**fields):
    try:
        from app.config_manager import load_json, save_json
        status = load_json("backtest_status.json") or {}
        status.update(fields)
        save_json("backtest_status.json", status)
    except Exception as e:
        log.warning(f"Status-Write fehlgeschlagen: {e}")


def _push_results():
    try:
        from app.persistence import backup_backtest_results
        ok = backup_backtest_results()
        log.info(f"Push to Gist: {'OK' if ok else 'FAILED'}")
        return ok
    except Exception as e:
        log.warning(f"Push fehlgeschlagen: {e}")
        return False


def main():
    triggered_by = sys.argv[1] if len(sys.argv) > 1 else "manual"
    started_at = datetime.now().isoformat()

    log.info("=" * 55)
    log.info(f"BACKTEST-RUNNER START (triggered_by={triggered_by})")
    log.info("=" * 55)

    _write_status(
        state="running",
        started_at=started_at,
        finished_at=None,
        triggered_by=triggered_by,
        error=None,
        mode="github-action-running",
    )
    _push_results()  # Early push so Dashboard sees "running" state

    # 1) Restore from Gist so we have the current config.json / disabled_symbols
    try:
        from app.persistence import restore_from_cloud
        restore_from_cloud()
        log.info("Cloud-Restore OK")
    except Exception as e:
        log.warning(f"Cloud-Restore fehlgeschlagen (weiter mit lokalem Stand): {e}")

    # 2) Run the backtest
    try:
        from app.backtester import run_full_backtest
        result = run_full_backtest()

        if result and "error" in result:
            raise RuntimeError(result["error"])

        metrics = (result or {}).get("full_period", {}).get("metrics", {})
        summary = (
            f"Trades={metrics.get('total_trades', 0)}, "
            f"Return={metrics.get('total_return_pct', 0):+.2f}%, "
            f"Sharpe={metrics.get('sharpe_ratio', 0):.2f}, "
            f"MaxDD={metrics.get('max_drawdown_pct', 0):.1f}%"
        )
        log.info(f"Backtest OK: {summary}")

        _write_status(
            state="done",
            started_at=started_at,
            finished_at=datetime.now().isoformat(),
            triggered_by=triggered_by,
            error=None,
            mode="github-action-done",
            summary=summary,
        )
        _push_results()
        return 0

    except Exception as e:
        tb = traceback.format_exc()
        log.error(f"Backtest fehlgeschlagen: {e}\n{tb}")
        _write_status(
            state="error",
            started_at=started_at,
            finished_at=datetime.now().isoformat(),
            triggered_by=triggered_by,
            error=f"{type(e).__name__}: {e}",
            mode="github-action-error",
            traceback=tb[-2000:],
        )
        _push_results()
        return 1


if __name__ == "__main__":
    sys.exit(main())
