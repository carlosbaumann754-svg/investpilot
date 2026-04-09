"""
Kelly-Cap-Sweep Runner — laeuft auf GitHub Actions (7 GB RAM).

Misst empirisch wie sich verschiedene Kelly-Cap-Werte auf Rendite, Sharpe
und Max-Drawdown auswirken, statt linear hochzurechnen. Hintergrund: v12
laeuft mit kelly_fraction=0.01 (1%). Backtest v12.1 zeigt MaxDD 0.7% IS /
0.5% OOS — Headroom riesig. Vor dem Hochbumpen wollen wir wissen ob die
Skalierung nichtlinear wird (z.B. wegen overlap-bedingter Korrelation) oder
sich tatsaechlich linear verhaelt.

Methode:
  1. simulate_trades EINMAL ausfuehren (Trades sind kelly-unabhaengig)
  2. walk_forward_validate EINMAL fuer In/Out-Sample Splits
  3. Fuer jeden kelly_frac in SWEEP: re-score Metriken via calculate_metrics
     mit override position_sizing
  4. Ergebnisse in kelly_sweep_results.json + Gist push

Sweep-Werte: 0.01, 0.02, 0.04, 0.08
  - 0.01 = aktueller Live-Cap (Baseline)
  - 0.02 = Woche 1 v12-Plan
  - 0.04 = Woche 3 v12-Plan
  - 0.08 = aggressives Ziel (max das je sinnvoll waere bei MaxDD<8% Hard Gate)

Usage:
    python -m app.kelly_sweep_runner [triggered_by]

ENV:
    GITHUB_TOKEN   Pflicht (Gist Read/Write)
"""

import logging
import sys
import traceback
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [KELLY-SWEEP] [%(levelname)s] %(message)s",
)
log = logging.getLogger("kelly_sweep_runner")

SWEEP_KELLY_FRACTIONS = [0.01, 0.02, 0.04, 0.08]


def _write_status(**fields):
    try:
        from app.config_manager import load_json, save_json
        status = load_json("kelly_sweep_status.json") or {}
        status.update(fields)
        status["updated_at"] = datetime.now().isoformat()
        save_json("kelly_sweep_status.json", status)
    except Exception as e:
        log.warning(f"Status-Write fehlgeschlagen: {e}")


def _push_results():
    try:
        from app.persistence import backup_kelly_sweep_results
        ok = backup_kelly_sweep_results()
        log.info(f"Push to Gist: {'OK' if ok else 'FAILED'}")
        return ok
    except Exception as e:
        log.warning(f"Push fehlgeschlagen: {e}")
        return False


def _score_one(trades, kelly_frac, max_concurrent):
    """Re-score eine Trade-Liste mit gegebenem Kelly-Cap."""
    from app.backtester import calculate_metrics, build_equity_curve
    pos = {"kelly_fraction": float(kelly_frac), "max_concurrent": int(max_concurrent)}
    metrics = calculate_metrics(trades, position_sizing=pos)
    curve = build_equity_curve(trades, kelly_fraction=float(kelly_frac))
    # build_equity_curve returns [[date_str, equity_value], ...] starting at 10000
    final_equity = (curve[-1][1] / 10000.0) if curve else 1.0
    return {
        "kelly_fraction": float(kelly_frac),
        "total_return_pct": metrics.get("total_return_pct", 0),
        "annual_return_pct": metrics.get("annual_return_pct", 0),
        "sharpe_ratio": metrics.get("sharpe_ratio", 0),
        "max_drawdown_pct": metrics.get("max_drawdown_pct", 0),
        "win_rate_pct": metrics.get("win_rate_pct", 0),
        "profit_factor": metrics.get("profit_factor", 0),
        "total_trades": metrics.get("total_trades", 0),
        "total_costs_pct": metrics.get("total_costs_pct", 0),
        "final_equity_factor": final_equity,
    }


def main():
    triggered_by = sys.argv[1] if len(sys.argv) > 1 else "manual"
    started_at = datetime.now().isoformat()

    log.info("=" * 55)
    log.info(f"KELLY-SWEEP-RUNNER START (triggered_by={triggered_by})")
    log.info(f"  Sweep: {SWEEP_KELLY_FRACTIONS}")
    log.info("=" * 55)

    _write_status(
        state="running", phase="init",
        message="Runner gestartet",
        started_at=started_at, finished_at=None,
        triggered_by=triggered_by, error=None,
        sweep_values=SWEEP_KELLY_FRACTIONS,
        mode="github-action-running",
    )
    _push_results()

    try:
        from app.persistence import restore_from_cloud
        restore_from_cloud()
        log.info("Cloud-Restore OK")
    except Exception as e:
        log.warning(f"Cloud-Restore fehlgeschlagen: {e}")

    try:
        from app.config_manager import load_config, save_json
        from app.backtester import (
            download_history,
            download_vix_history,
            _fetch_historical_earnings_dates,
            _build_earnings_blackout_set,
            simulate_trades,
            ASSET_UNIVERSE,
        )

        config = load_config()
        max_concurrent = (config.get("demo_trading") or {}).get("max_positions", 20)

        # 1) Download history
        _write_status(state="running", phase="download",
                      message="Lade 5J Historie + VIX + Earnings...")
        _push_results()

        histories = download_history(years=5)
        if not histories:
            raise RuntimeError("Keine historischen Daten")

        vix_history = download_vix_history(years=5)
        earnings_blackouts = {}
        mc_cfg = config.get("market_context", {})
        buf_b = mc_cfg.get("earnings_buffer_days_before", 3)
        buf_a = mc_cfg.get("earnings_buffer_days_after", 1)
        for sym in histories.keys():
            info = ASSET_UNIVERSE.get(sym, {})
            if info.get("class") in ("crypto", "forex", "commodities", "indices"):
                continue
            edates = _fetch_historical_earnings_dates(sym)
            if edates:
                earnings_blackouts[sym] = _build_earnings_blackout_set(
                    sym, edates, buf_b, buf_a)

        sim_kwargs = {
            "use_realistic_filters": True,
            "vix_history": vix_history,
            "earnings_blackouts": earnings_blackouts,
        }

        # 2) Simulate trades ONCE (Trades sind kelly-unabhaengig)
        _write_status(state="running", phase="simulate",
                      message="Simuliere Trades (full period)...")
        _push_results()

        all_trades = simulate_trades(histories, config, **sim_kwargs)
        log.info(f"Full-period trades: {len(all_trades)}")

        # 3) Walk-forward Split (80/20) — manuell, weil walk_forward_validate
        # die rohen Trades nicht exposed (nur Metriken). Wir brauchen die
        # Trades selbst um sie pro Kelly-Cap erneut zu scoren.
        _write_status(state="running", phase="walk_forward",
                      message="Walk-Forward (train/test split)...")
        _push_results()

        train_pct = 0.80
        train_histories = {}
        test_histories = {}
        for sym, hist in histories.items():
            n = len(hist)
            split = int(n * train_pct)
            if split < 100 or (n - split) < 30:
                continue
            train_histories[sym] = hist.iloc[:split]
            test_histories[sym] = hist.iloc[split:]

        train_trades = simulate_trades(train_histories, config, **sim_kwargs) \
            if train_histories else []
        test_trades = simulate_trades(test_histories, config, **sim_kwargs) \
            if test_histories else []
        log.info(f"WF train_trades={len(train_trades)} test_trades={len(test_trades)}")

        # 4) Re-score per Kelly-Fraction
        _write_status(state="running", phase="rescore",
                      message="Re-score Metriken pro Kelly-Cap...")
        _push_results()

        sweep_rows = []
        for k in SWEEP_KELLY_FRACTIONS:
            full = _score_one(all_trades, k, max_concurrent)
            train = _score_one(train_trades, k, max_concurrent) if train_trades else {}
            test = _score_one(test_trades, k, max_concurrent) if test_trades else {}
            sweep_rows.append({
                "kelly_fraction": k,
                "full_period": full,
                "in_sample": train,
                "out_of_sample": test,
            })
            log.info(
                f"  k={k:.2f}  fullRet={full['total_return_pct']:+.2f}%  "
                f"sharpe={full['sharpe_ratio']:.2f}  "
                f"maxDD={full['max_drawdown_pct']:.2f}%"
            )

        results = {
            "timestamp": datetime.now().isoformat(),
            "sweep": sweep_rows,
            "config_kelly_baseline": (config.get("kelly_sizing") or {}).get("max_fraction"),
            "max_concurrent": max_concurrent,
            "n_full_trades": len(all_trades),
            "n_train_trades": len(train_trades),
            "n_test_trades": len(test_trades),
        }
        save_json("kelly_sweep_results.json", results)

        # Build human-readable summary table
        summary_lines = ["Kelly-Sweep Resultate:", ""]
        summary_lines.append(
            f"{'k':>6} {'Return%':>10} {'Sharpe':>8} {'MaxDD%':>8} {'Trades':>7}"
        )
        for row in sweep_rows:
            f = row["full_period"]
            summary_lines.append(
                f"{row['kelly_fraction']:>6.2f} "
                f"{f['total_return_pct']:>+10.2f} "
                f"{f['sharpe_ratio']:>8.2f} "
                f"{f['max_drawdown_pct']:>8.2f} "
                f"{f['total_trades']:>7d}"
            )
        summary = "\n".join(summary_lines)
        log.info("\n" + summary)

        _write_status(
            state="done",
            phase="done",
            message="Kelly-Sweep abgeschlossen",
            started_at=started_at,
            finished_at=datetime.now().isoformat(),
            triggered_by=triggered_by,
            error=None,
            mode="github-action-done",
            summary=summary,
            results=results,
        )
        _push_results()
        return 0

    except Exception as e:
        tb = traceback.format_exc()
        log.error(f"Kelly-Sweep fehlgeschlagen: {e}\n{tb}")
        _write_status(
            state="error",
            phase="error",
            message=f"{type(e).__name__}: {e}",
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
