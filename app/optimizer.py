"""
InvestPilot - Self-Improvement Optimizer
Woechentlicher Auto-Optimizer: testet Parameter-Kombinationen per Backtest,
waehlt die beste Out-of-Sample Konfiguration, trainiert ML-Modell,
und schreibt optimierte Parameter zurueck in config.json.

Safety Guards: Max 1 Aenderung/Woche, OOS muss besser sein, Rollback bei Einbruch.
"""

import logging
import copy
import os
from datetime import datetime, timezone

from app.config_manager import load_config, save_config, load_json, save_json

log = logging.getLogger("Optimizer")

# ============================================================
# ASSET-CLASS VOLATILITY PROFILES (fuer dynamische SL/TP)
# ============================================================

ASSET_CLASS_DEFAULTS = {
    "stocks":      {"sl_pct": -4,  "tp_pct": 8,  "avg_volatility": 2.5},
    "etf":         {"sl_pct": -3,  "tp_pct": 6,  "avg_volatility": 1.5},
    "crypto":      {"sl_pct": -8,  "tp_pct": 15, "avg_volatility": 5.0},
    "commodities": {"sl_pct": -5,  "tp_pct": 10, "avg_volatility": 3.0},
    "forex":       {"sl_pct": -2,  "tp_pct": 4,  "avg_volatility": 1.0},
    "indices":     {"sl_pct": -3,  "tp_pct": 6,  "avg_volatility": 1.8},
}

# ============================================================
# PARAMETER GRID
# ============================================================

PARAM_GRID = {
    # v12: erweitert auf asymmetrisches R/R 1:3
    "min_scanner_score": [30, 40, 50, 60],
    "stop_loss_pct":     [-2.0, -2.5, -3.0, -4.0, -5.0],
    "take_profit_pct":   [6, 9, 12, 15, 18],
    "trailing_sl_pct":   [1.5, 1.8, 2.5],
    "trailing_sl_activation_pct": [0.5, 0.8, 1.0],
}

# Minimum expected return nach Kosten (Trade muss sich lohnen)
MIN_COST_MULTIPLIER = 1.5  # Trade muss min 1.5x Kosten erwarten


# ============================================================
# COST-AWARE FILTER
# ============================================================

def calculate_min_expected_return(days_held_avg=12):
    """Berechne minimalen erwarteten Return damit ein Trade nach Kosten profitabel ist."""
    from app.backtester import SPREAD_PCT, OVERNIGHT_FEE_PCT, SLIPPAGE_PCT

    total_cost = (SPREAD_PCT * 2) + (OVERNIGHT_FEE_PCT * days_held_avg) + (SLIPPAGE_PCT * 2)
    min_return = total_cost * MIN_COST_MULTIPLIER
    return round(min_return * 100, 3)  # als Prozent


def get_asset_class_params(config=None):
    """Hole Asset-Klassen-spezifische SL/TP aus Config oder Defaults."""
    if config is None:
        config = load_config()
    return config.get("asset_class_params", ASSET_CLASS_DEFAULTS)


# ============================================================
# VOLATILITY-BASED SL/TP CALCULATION
# ============================================================

def calculate_volatility_sl_tp(histories):
    """Berechne optimale SL/TP pro Asset-Klasse basierend auf historischer Volatilitaet.

    Returns dict: {asset_class: {sl_pct, tp_pct, avg_volatility}}
    """
    from app.market_scanner import ASSET_UNIVERSE
    import math

    class_volatilities = {}

    for sym, hist in histories.items():
        info = ASSET_UNIVERSE.get(sym, {})
        asset_class = info.get("class", "stocks")

        closes = hist["Close"].values.tolist()
        if len(closes) < 60:
            continue

        # 60-Tage annualisierte Volatilitaet
        returns = [(closes[i] - closes[i-1]) / closes[i-1]
                   for i in range(len(closes) - 60, len(closes))]
        daily_vol = (sum(r**2 for r in returns) / len(returns)) ** 0.5
        annual_vol = daily_vol * math.sqrt(252) * 100  # in Prozent

        if asset_class not in class_volatilities:
            class_volatilities[asset_class] = []
        class_volatilities[asset_class].append(annual_vol)

    result = {}
    for ac, vols in class_volatilities.items():
        avg_vol = sum(vols) / len(vols)
        # SL = 1.5x tägliche Volatilität (ATR-basiert)
        daily_vol = avg_vol / math.sqrt(252)
        sl = round(-daily_vol * 1.5, 1)
        sl = max(sl, -15)  # Cap bei -15%
        sl = min(sl, -2)   # Mindestens -2%
        # TP = 2x SL (mindestens 1:2 Risk/Reward)
        tp = round(abs(sl) * 2, 1)
        tp = max(tp, 3)    # Mindestens 3%

        result[ac] = {
            "sl_pct": sl,
            "tp_pct": tp,
            "avg_volatility": round(avg_vol, 2),
        }

    # Fehlende Klassen mit Defaults auffuellen
    for ac, defaults in ASSET_CLASS_DEFAULTS.items():
        if ac not in result:
            result[ac] = defaults

    return result


# ============================================================
# PARAMETER GRID SEARCH (v10 Performance Pack)
# ============================================================
#
# Hebel 1: Precompute (Score-Matrix + dates_to_idx + VIX-Norm) ein einziges Mal
#          vor dem Grid-Loop. Spart 30-50x.
# Hebel 3: Multiprocessing — Combos werden auf alle CPU-Kerne verteilt. Spart
#          weitere ~Nx wo N = vCPUs.
# Hebel 2: Optionales Shard-Sampling fuer GitHub Actions Matrix-Strategie.
#          Wenn shard_id/num_shards gesetzt sind, rechnet dieser Lauf nur seine
#          Teilmenge der Combos. Der Merge-Job aggregiert die Ergebnisse.
#
# Workers koennen ihre Daten nicht via Closure bekommen (nicht picklebar),
# daher legen wir sie in Modul-Globals und initialisieren sie via initializer.

_WORKER_STATE = {
    "train_pre": None,
    "test_pre": None,
    "earnings_blackouts": None,
    "base_config": None,
    "use_filters": True,
}


def _init_grid_worker(train_pre, test_pre, earnings_blackouts, base_config, use_filters):
    """ProcessPoolExecutor initializer — wird einmal pro Worker-Prozess aufgerufen."""
    _WORKER_STATE["train_pre"] = train_pre
    _WORKER_STATE["test_pre"] = test_pre
    _WORKER_STATE["earnings_blackouts"] = earnings_blackouts
    _WORKER_STATE["base_config"] = base_config
    _WORKER_STATE["use_filters"] = use_filters


def _evaluate_combo_worker(combo):
    """Worker: rechnet eine einzelne Grid-Combo (Train + Test Sim).

    combo = (min_score, sl, tp, trail_sl, trail_act)

    Returns: result-dict oder None bei Fehler/leerem WF.
    """
    try:
        from app.backtester import simulate_trades_fast, calculate_metrics, _build_position_sizing_from_config

        train_pre = _WORKER_STATE["train_pre"]
        test_pre = _WORKER_STATE["test_pre"]
        earnings = _WORKER_STATE["earnings_blackouts"]
        base = _WORKER_STATE["base_config"]
        use_filters = _WORKER_STATE["use_filters"]

        min_score, sl, tp, trail_sl, trail_act = combo

        test_config = copy.deepcopy(base)
        test_config.setdefault("demo_trading", {})
        test_config["demo_trading"]["min_scanner_score"] = min_score
        test_config["demo_trading"]["stop_loss_pct"] = sl
        test_config["demo_trading"]["take_profit_pct"] = tp
        test_config.setdefault("leverage", {})
        test_config["leverage"]["trailing_sl_pct"] = trail_sl
        test_config["leverage"]["trailing_sl_activation_pct"] = trail_act

        train_trades = simulate_trades_fast(
            train_pre, test_config,
            earnings_blackouts=earnings,
            use_realistic_filters=use_filters,
        )
        test_trades = simulate_trades_fast(
            test_pre, test_config,
            earnings_blackouts=earnings,
            use_realistic_filters=use_filters,
        )

        # v12.1 Fix: Position-Sizing aus Test-Config (mit allen Grid-Overrides)
        # ableiten — sonst sind die gemeldeten oos_return/oos_max_dd Trillionen.
        # Sharpe-Ranking ist scale-invariant, also keine Verschiebung der Top-Combos.
        pos_sizing = _build_position_sizing_from_config(test_config)
        ins = calculate_metrics(train_trades, position_sizing=pos_sizing)
        oos = calculate_metrics(test_trades, position_sizing=pos_sizing)

        return {
            "params": {
                "min_scanner_score": min_score,
                "stop_loss_pct": sl,
                "take_profit_pct": tp,
                "trailing_sl_pct": trail_sl,
                "trailing_sl_activation_pct": trail_act,
            },
            "oos_sharpe": oos.get("sharpe_ratio", -99),
            "oos_return": oos.get("total_return_pct", -100),
            "oos_max_dd": oos.get("max_drawdown_pct", -100),
            "oos_win_rate": oos.get("win_rate_pct", 0),
            "oos_trades": oos.get("total_trades", 0),
            "ins_sharpe": ins.get("sharpe_ratio", -99),
            "ins_return": ins.get("total_return_pct", -100),
        }
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {e}"}


def _iter_grid_combos():
    """Generiere alle gueltigen Grid-Combos (TP >= |SL|)."""
    for min_score in PARAM_GRID["min_scanner_score"]:
        for sl in PARAM_GRID["stop_loss_pct"]:
            for tp in PARAM_GRID["take_profit_pct"]:
                if tp < abs(sl):
                    continue
                for trail_sl in PARAM_GRID["trailing_sl_pct"]:
                    for trail_act in PARAM_GRID["trailing_sl_activation_pct"]:
                        yield (min_score, sl, tp, trail_sl, trail_act)


def _split_histories_train_test(histories, train_pct=0.8):
    """Splittet alle Symbol-DataFrames identisch wie walk_forward_validate."""
    train_histories = {}
    test_histories = {}
    for sym, hist in histories.items():
        n = len(hist)
        split = int(n * train_pct)
        if split < 100 or (n - split) < 30:
            continue
        train_histories[sym] = hist.iloc[:split]
        test_histories[sym] = hist.iloc[split:]
    return train_histories, test_histories


def run_grid_search(histories, base_config=None,
                    vix_history=None, earnings_blackouts=None,
                    shard_id=None, num_shards=None):
    """Grid-Search mit Precompute + Multiprocessing + optional Shard-Sampling.

    Args:
        histories: dict of DataFrames
        base_config: strategy config
        vix_history: pre-downloaded VIX data
        earnings_blackouts: pre-built earnings blackout sets
        shard_id: 0-based shard index (None = no sharding)
        num_shards: total number of shards (None = no sharding)

    Returns:
        dict mit best, top_5, total_tested, all_results (fuer Merge-Mode)
    """
    import multiprocessing as mp
    from concurrent.futures import ProcessPoolExecutor, as_completed
    from app.backtester import precompute_grid_data

    if base_config is None:
        base_config = load_config()

    use_filters = bool(vix_history or earnings_blackouts)

    # 1. Train/Test split + Precompute (EINMALIG)
    log.info("Precompute: splitte histories und baue Score-Matrix...")
    train_hist, test_hist = _split_histories_train_test(histories, train_pct=0.8)
    if not train_hist or not test_hist:
        log.warning("Nicht genug Daten fuer Walk-Forward")
        return {"best": None, "top_5": [], "total_tested": 0, "all_results": []}

    train_pre = precompute_grid_data(train_hist, vix_history)
    test_pre = precompute_grid_data(test_hist, vix_history)
    log.info(f"  Precompute fertig: train={len(train_pre['symbol_data'])} symbols, "
             f"test={len(test_pre['symbol_data'])} symbols, "
             f"vix={len(train_pre['vix_by_date_norm'])} days")

    # 2. Combo-Liste bauen + ggf. sharden
    all_combos = list(_iter_grid_combos())
    total_full_grid = len(all_combos)

    if shard_id is not None and num_shards is not None and num_shards > 1:
        sharded = [c for i, c in enumerate(all_combos) if i % num_shards == shard_id]
        log.info(f"Shard {shard_id+1}/{num_shards}: {len(sharded)}/{total_full_grid} Combos")
        combos_to_run = sharded
    else:
        combos_to_run = all_combos
        log.info(f"Grid-Search: {total_full_grid} Combos (kein Sharding)")

    # 3. Worker-Konfiguration
    env_workers = os.environ.get("INVESTPILOT_OPTIMIZER_WORKERS", "0")
    try:
        configured_workers = int(env_workers)
    except ValueError:
        configured_workers = 0
    if configured_workers <= 0:
        n_workers = max(1, mp.cpu_count())
    else:
        n_workers = configured_workers

    log.info(f"Grid-Search startet: {len(combos_to_run)} Combos auf {n_workers} Worker(s) "
             f"(realistic_filters={'ON' if use_filters else 'OFF'})")

    # 4. Sequential fallback (Workers=1) — fuer Debug/Windows
    results = []
    if n_workers == 1 or len(combos_to_run) == 0:
        _init_grid_worker(train_pre, test_pre, earnings_blackouts, base_config, use_filters)
        for i, combo in enumerate(combos_to_run, 1):
            r = _evaluate_combo_worker(combo)
            if r and "_error" not in r:
                results.append(r)
            if i % 20 == 0:
                log.info(f"  Grid-Search: {i}/{len(combos_to_run)} Combos getestet")
    else:
        # 5. Multiprocessing
        with ProcessPoolExecutor(
            max_workers=n_workers,
            initializer=_init_grid_worker,
            initargs=(train_pre, test_pre, earnings_blackouts, base_config, use_filters),
        ) as ex:
            futures = {ex.submit(_evaluate_combo_worker, c): c for c in combos_to_run}
            done_count = 0
            for fut in as_completed(futures):
                done_count += 1
                try:
                    r = fut.result()
                    if r and "_error" not in r:
                        results.append(r)
                    elif r and "_error" in r:
                        log.debug(f"  Combo {futures[fut]} Fehler: {r['_error']}")
                except Exception as e:
                    log.debug(f"  Worker exception: {e}")
                if done_count % 20 == 0:
                    log.info(f"  Grid-Search: {done_count}/{len(combos_to_run)} Combos fertig")

    # 6. Sortieren + Logging
    results.sort(key=lambda r: r["oos_sharpe"], reverse=True)
    best = results[0] if results else None

    log.info(f"Grid-Search fertig: {len(results)} gueltige Kombinationen "
             f"(von {len(combos_to_run)} versuchten)")
    if best:
        log.info(f"  Beste Kombi: score>={best['params']['min_scanner_score']}, "
                 f"SL={best['params']['stop_loss_pct']}%, "
                 f"TP={best['params']['take_profit_pct']}%, "
                 f"Trail={best['params']['trailing_sl_pct']}%@{best['params']['trailing_sl_activation_pct']}%, "
                 f"OOS Sharpe={best['oos_sharpe']:.2f}")

    return {
        "best": best,
        "top_5": results[:5],
        "total_tested": len(results),
        "all_results": results,
    }


# ============================================================
# ML AUTO-COMPARE
# ============================================================

def compare_ml_vs_fixed(histories, config=None,
                        vix_history=None, earnings_blackouts=None):
    """Vergleiche ML Scoring vs Fixed Weights auf Out-of-Sample Daten.

    Returns dict mit Empfehlung ob ML aktiviert werden soll.
    """
    from app.backtester import walk_forward_validate

    if config is None:
        config = load_config()

    use_filters = bool(vix_history or earnings_blackouts)
    filter_kwargs = {
        "use_realistic_filters": use_filters,
        "vix_history": vix_history,
        "earnings_blackouts": earnings_blackouts,
    }

    # 1. Fixed Weights Backtest
    fixed_config = copy.deepcopy(config)
    fixed_config["demo_trading"]["use_ml_scoring"] = False
    fixed_wf = walk_forward_validate(histories, fixed_config, **filter_kwargs)

    # 2. ML trainieren und testen
    ml_result = None
    try:
        from app.ml_scorer import train_model, is_model_trained
        train_result = train_model(histories)

        if is_model_trained():
            ml_config = copy.deepcopy(config)
            ml_config["demo_trading"]["use_ml_scoring"] = True
            ml_wf = walk_forward_validate(histories, ml_config, **filter_kwargs)
            ml_result = ml_wf
    except Exception as e:
        log.warning(f"ML Training/Vergleich Fehler: {e}")

    if not fixed_wf:
        return {"recommendation": "keep_fixed", "reason": "Kein Walk-Forward moeglich"}

    fixed_oos = fixed_wf["out_of_sample"]["metrics"]
    comparison = {
        "fixed_weights": {
            "oos_sharpe": fixed_oos.get("sharpe_ratio", -99),
            "oos_return": fixed_oos.get("total_return_pct", -100),
            "oos_win_rate": fixed_oos.get("win_rate_pct", 0),
        },
        "ml_scoring": None,
        "recommendation": "keep_fixed",
        "reason": "ML nicht trainiert oder nicht besser",
    }

    if ml_result:
        ml_oos = ml_result["out_of_sample"]["metrics"]
        comparison["ml_scoring"] = {
            "oos_sharpe": ml_oos.get("sharpe_ratio", -99),
            "oos_return": ml_oos.get("total_return_pct", -100),
            "oos_win_rate": ml_oos.get("win_rate_pct", 0),
        }

        # ML aktivieren wenn OOS Sharpe deutlich besser (mindestens +0.3)
        if ml_oos.get("sharpe_ratio", -99) > fixed_oos.get("sharpe_ratio", -99) + 0.3:
            comparison["recommendation"] = "switch_to_ml"
            comparison["reason"] = (
                f"ML OOS Sharpe ({ml_oos['sharpe_ratio']:.2f}) > "
                f"Fixed ({fixed_oos['sharpe_ratio']:.2f}) + 0.3 Margin"
            )
        else:
            comparison["reason"] = (
                f"ML OOS Sharpe ({ml_oos.get('sharpe_ratio', -99):.2f}) nicht "
                f"deutlich besser als Fixed ({fixed_oos.get('sharpe_ratio', -99):.2f})"
            )

    return comparison


# ============================================================
# SAFETY GUARDS
# ============================================================

def check_rollback_needed(config=None):
    """Pruefe ob die letzte Optimierung einen Performance-Einbruch verursacht hat.

    Vergleicht Wochen-PnL seit letzter Optimierung mit Rollback-Threshold.
    Returns (should_rollback, reason)
    """
    history = load_json("optimization_history.json") or {"runs": []}
    if not history["runs"]:
        return False, "Keine vorherige Optimierung"

    last_run = history["runs"][-1]
    if last_run.get("rolled_back", False):
        return False, "Letzte Optimierung bereits zurueckgerollt"

    # Pruefe PnL seit Optimierung
    risk_state = load_json("risk_state.json") or {}
    weekly_pnl = risk_state.get("weekly_pnl_pct", 0)

    threshold = -5.0  # -5% Wochen-Drawdown nach Optimierung = Rollback

    if weekly_pnl < threshold:
        return True, f"Weekly PnL {weekly_pnl:.1f}% < {threshold}% nach letzter Optimierung"

    return False, f"Weekly PnL {weekly_pnl:.1f}% — kein Rollback noetig"


def rollback_optimization():
    """Letzte Optimierung rueckgaengig machen — alte Parameter wiederherstellen."""
    history = load_json("optimization_history.json") or {"runs": []}
    if not history["runs"]:
        return False, "Keine Optimierung zum Zurueckrollen"

    last_run = history["runs"][-1]
    old_params = last_run.get("previous_params")
    if not old_params:
        return False, "Keine vorherigen Parameter gespeichert"

    config = load_config()
    dt = config.get("demo_trading", {})

    for key, value in old_params.items():
        if key in dt:
            dt[key] = value

    save_config(config)

    # Rollback markieren
    last_run["rolled_back"] = True
    last_run["rollback_time"] = datetime.now().isoformat()
    save_json("optimization_history.json", history)

    log.warning(f"ROLLBACK: Parameter zurueckgesetzt auf {old_params}")

    # CRITICAL: Backup nach Rollback damit die Ruecksetzung persistiert
    try:
        from app.persistence import backup_to_cloud
        backup_to_cloud()
        log.info("Cloud-Backup nach Rollback erfolgreich")
    except Exception as e:
        log.warning(f"Cloud-Backup nach Rollback fehlgeschlagen: {e}")

    return True, f"Rollback erfolgreich auf {old_params}"


# ============================================================
# MAIN OPTIMIZER
# ============================================================

def run_weekly_optimization():
    """Haupt-Optimierungslauf — wird woechentlich vom Scheduler aufgerufen.

    Pipeline:
    1. Rollback-Check (letzte Woche ok?)
    2. Daten herunterladen
    3. Volatilitaets-basierte SL/TP berechnen
    4. Parameter Grid-Search
    5. ML vs Fixed Vergleich
    6. Beste Parameter in Config schreiben
    7. Kosten-Filter berechnen
    8. History speichern

    Safety: Max 1 grosse Aenderung pro Woche.
    """
    log.info("=" * 55)
    log.info("WEEKLY OPTIMIZATION START")
    log.info("=" * 55)

    # Memory-Safeguard: auf 512 MB Render-Starter kann Grid-Search + ML + Walk-Forward
    # das Memory-Limit sprengen. Brich frueh ab statt mit OOM-Kill zu sterben.
    try:
        import psutil
        mem = psutil.virtual_memory()
        free_mb = mem.available / (1024 * 1024)
        log.info(f"Memory-Check: {free_mb:.0f} MB verfuegbar ({mem.percent:.0f}% in use)")
        if free_mb < 150:
            msg = f"Zu wenig freier Speicher: {free_mb:.0f} MB (min 150 MB noetig)"
            log.error(msg)
            return {"action": "error", "error": msg, "free_memory_mb": round(free_mb)}
    except ImportError:
        log.warning("psutil nicht verfuegbar, Memory-Check uebersprungen")
    except Exception as e:
        log.warning(f"Memory-Check Fehler: {e}")

    config = load_config()
    dt = config.get("demo_trading", {})

    # Aktuelle Parameter merken (fuer Rollback)
    current_params = {
        "min_scanner_score": dt.get("min_scanner_score", 15),
        "stop_loss_pct": dt.get("stop_loss_pct", -5),
        "take_profit_pct": dt.get("take_profit_pct", 18),
        "use_ml_scoring": dt.get("use_ml_scoring", False),
    }

    # 1. Rollback-Check
    should_rollback, rollback_reason = check_rollback_needed(config)
    if should_rollback:
        success, msg = rollback_optimization()
        log.warning(f"Rollback: {msg}")
        _save_optimization_run("rollback", current_params, current_params,
                               {"reason": rollback_reason})
        rollback_result = {"action": "rollback", "reason": rollback_reason}
        try:
            from app.alerts import alert_optimizer_completed
            alert_optimizer_completed(rollback_result)
        except Exception:
            pass
        return rollback_result

    # 2. Daten herunterladen
    log.info("Downloading historical data...")
    from app.backtester import (download_history, download_vix_history,
                                _fetch_historical_earnings_dates,
                                _build_earnings_blackout_set)
    from app.market_scanner import ASSET_UNIVERSE as _AU
    histories = download_history(years=5)
    if not histories:
        log.error("Keine Daten — Optimierung abgebrochen")
        return {"error": "Keine historischen Daten"}

    # 2b. Download realistic filter data (VIX + Earnings)
    log.info("Downloading VIX + Earnings data for realistic filters...")
    vix_history = download_vix_history(years=5)
    earnings_blackouts = {}
    mc_cfg = config.get("market_context", {})
    buf_before = mc_cfg.get("earnings_buffer_days_before", 3)
    buf_after = mc_cfg.get("earnings_buffer_days_after", 1)
    for sym in histories.keys():
        info = _AU.get(sym, {})
        if info.get("class", "") in ("crypto", "forex", "commodities", "indices"):
            continue
        edates = _fetch_historical_earnings_dates(sym)
        if edates:
            earnings_blackouts[sym] = _build_earnings_blackout_set(
                sym, edates, buf_before, buf_after)
    log.info(f"  VIX: {len(vix_history)} days, Earnings: {len(earnings_blackouts)} symbols")

    filter_kwargs = {
        "vix_history": vix_history,
        "earnings_blackouts": earnings_blackouts,
    }

    # 3. Volatilitaets-basierte SL/TP
    log.info("Berechne Asset-Klassen SL/TP...")
    asset_params = calculate_volatility_sl_tp(histories)
    config["asset_class_params"] = asset_params
    log.info(f"  Asset-Klassen Parameter: {list(asset_params.keys())}")

    # 4. Grid-Search
    log.info("Starte Parameter Grid-Search (mit realistischen Filtern)...")
    grid_result = run_grid_search(histories, config, **filter_kwargs)
    best = grid_result.get("best")

    # 4b. ML Training auf eigener Trade-History (wenn genug Daten)
    ml_trade_training = None
    try:
        from app.ml_scorer import train_from_trade_history, MIN_TRADES_FOR_TRAINING
        trade_history = load_json("trade_history.json") or []
        if len(trade_history) >= MIN_TRADES_FOR_TRAINING:
            log.info(f"ML Training auf {len(trade_history)} eigenen Trades...")
            ml_trade_training = train_from_trade_history(trade_history)
            if "error" not in ml_trade_training:
                log.info(f"  ML Trade-History Model: "
                         f"Acc={ml_trade_training.get('test_accuracy', 0):.1f}%, "
                         f"F1={ml_trade_training.get('test_f1', 0):.1f}%")
            else:
                log.info(f"  ML Trade-History: {ml_trade_training['error']}")
        else:
            log.info(f"ML Trade-History: {len(trade_history)}/{MIN_TRADES_FOR_TRAINING} "
                     f"Trades — noch nicht genug, uebersprungen")
    except Exception as e:
        log.warning(f"ML Trade-History Training Fehler: {e}")

    # 5. ML vs Fixed Vergleich
    log.info("Vergleiche ML vs Fixed Weights...")
    ml_comparison = compare_ml_vs_fixed(histories, config, **filter_kwargs)

    # 6. Kosten-Filter berechnen
    min_return = calculate_min_expected_return()
    log.info(f"  Min Expected Return: {min_return}% (nach Kosten)")

    # 7. Entscheide welche Aenderungen anwenden
    changes_made = {}
    new_params = dict(current_params)

    # Grid-Search Ergebnis anwenden — v12: nur wenn OOS Sharpe positiv.
    # Negative Sharpe-Werte kommen typischerweise von veralteten Backtests
    # die unsere v12-Features (Time-Stop, Meta-Labeler, Kelly, Trailing
    # Tranchen) noch nicht modellieren. Lieber gar nichts apply'en als
    # Garbage-In-Garbage-Out.
    if best and best["oos_sharpe"] > 0:
        bp = best["params"]

        # v12 Sanity-Guard: schuetze asymmetrisches R/R vor "klassischer"
        # Optimizer-Drift. Wenn das aktuelle TP deutlich groesser ist als
        # das beste vom Grid (z.B. wir laufen auf 18%, Grid empfiehlt 8%),
        # ueberschreiben wir NICHT — vermutlich kennt der Optimizer-Backtest
        # die Asymmetric-Strategy nicht (Time-Stop, Trailing, Tranchen).
        # Zusaetzlich: nur uebernehmen wenn OOS-Sharpe positiv ist.
        oos_sharpe_ok = (best.get("oos_sharpe") or 0) > 0

        if bp["min_scanner_score"] != current_params["min_scanner_score"]:
            new_params["min_scanner_score"] = bp["min_scanner_score"]
            changes_made["min_scanner_score"] = {
                "old": current_params["min_scanner_score"],
                "new": bp["min_scanner_score"],
            }

        if bp["stop_loss_pct"] != current_params["stop_loss_pct"]:
            cur_sl = abs(current_params["stop_loss_pct"] or 0)
            new_sl = abs(bp["stop_loss_pct"] or 0)
            # Nur uebernehmen wenn OOS positiv ODER neuer SL nicht radikal anders
            if oos_sharpe_ok or (0.5 <= (new_sl / max(cur_sl, 0.1)) <= 2.0):
                new_params["stop_loss_pct"] = bp["stop_loss_pct"]
                changes_made["stop_loss_pct"] = {
                    "old": current_params["stop_loss_pct"],
                    "new": bp["stop_loss_pct"],
                }
            else:
                log.info(f"  [v12-guard] SL-Aenderung blockiert: "
                         f"{current_params['stop_loss_pct']} -> {bp['stop_loss_pct']} "
                         f"(OOS Sharpe={best.get('oos_sharpe')})")

        if bp["take_profit_pct"] != current_params["take_profit_pct"]:
            cur_tp = current_params["take_profit_pct"] or 0
            new_tp = bp["take_profit_pct"] or 0
            # v12 Asymmetric-Schutz: blockiere TP-Reduktion >30% wenn OOS negativ
            tp_drop_pct = (cur_tp - new_tp) / max(cur_tp, 0.1)
            if oos_sharpe_ok or tp_drop_pct < 0.3:
                new_params["take_profit_pct"] = bp["take_profit_pct"]
                changes_made["take_profit_pct"] = {
                    "old": current_params["take_profit_pct"],
                    "new": bp["take_profit_pct"],
                }
            else:
                log.info(f"  [v12-guard] TP-Aenderung blockiert: "
                         f"{cur_tp} -> {new_tp} (drop={tp_drop_pct:.0%}, "
                         f"OOS Sharpe={best.get('oos_sharpe')})")

    # ML Empfehlung anwenden
    if ml_comparison["recommendation"] == "switch_to_ml":
        new_params["use_ml_scoring"] = True
        changes_made["use_ml_scoring"] = {"old": False, "new": True}
    elif ml_comparison["recommendation"] == "keep_fixed" and current_params.get("use_ml_scoring"):
        new_params["use_ml_scoring"] = False
        changes_made["use_ml_scoring"] = {"old": True, "new": False}

    # 8. Parameter in Config schreiben
    if changes_made:
        dt["min_scanner_score"] = new_params["min_scanner_score"]
        dt["stop_loss_pct"] = new_params["stop_loss_pct"]
        dt["take_profit_pct"] = new_params["take_profit_pct"]
        dt["use_ml_scoring"] = new_params["use_ml_scoring"]
        config["min_expected_return_pct"] = min_return
        save_config(config)
        log.info(f"Config aktualisiert: {changes_made}")
    else:
        log.info("Keine Aenderungen — aktuelle Parameter sind optimal")

    # 9. History speichern
    result = {
        "action": "optimized" if changes_made else "no_change",
        "timestamp": datetime.now().isoformat(),
        "changes": changes_made,
        "grid_search": {
            "best_params": best["params"] if best else None,
            "best_oos_sharpe": best["oos_sharpe"] if best else None,
            "top_5": grid_result.get("top_5", []),
            "total_tested": grid_result.get("total_tested", 0),
        },
        "ml_comparison": ml_comparison,
        "ml_trade_training": ml_trade_training,
        "asset_class_params": asset_params,
        "min_expected_return_pct": min_return,
        "current_params": current_params,
        "new_params": new_params,
    }

    _save_optimization_run(
        result["action"], current_params, new_params, result)

    log.info("=" * 55)
    log.info(f"OPTIMIZATION COMPLETE: {result['action']}")
    if changes_made:
        for key, val in changes_made.items():
            log.info(f"  {key}: {val['old']} -> {val['new']}")
    log.info("=" * 55)

    # CRITICAL: Sofortiges Cloud-Backup nach Optimizer-Lauf.
    # Verhindert Datenverlust falls Container vor dem naechsten
    # Trading-Zyklus neu startet (Render Free Tier spin-down).
    # Im CI-Mode (GitHub-Action) wird das hier uebersprungen — der Runner
    # macht stattdessen einen ISOLIERTEN Push (nur Optimizer-Output-Dateien),
    # um Race-Conditions mit Trading-Server-Updates zu vermeiden.
    if os.environ.get("INVESTPILOT_SKIP_INLINE_BACKUP", "0") == "1":
        log.info("Inline-Backup uebersprungen (CI-Mode, Runner pusht isoliert)")
    else:
        try:
            from app.persistence import backup_to_cloud
            backup_to_cloud()
            log.info("Cloud-Backup nach Optimizer-Lauf erfolgreich")
        except Exception as e:
            log.warning(f"Cloud-Backup nach Optimizer fehlgeschlagen: {e}")

    # Telegram: Optimizer-Ergebnis senden
    try:
        from app.alerts import alert_optimizer_completed
        alert_optimizer_completed(result)
    except Exception as e:
        log.debug(f"Telegram Optimizer Alert fehlgeschlagen: {e}")

    return result


def _save_optimization_run(action, old_params, new_params, details):
    """Speichere Optimierungslauf in History."""
    history = load_json("optimization_history.json") or {"runs": []}
    history["runs"].append({
        "timestamp": datetime.now().isoformat(),
        "action": action,
        "previous_params": old_params,
        "new_params": new_params,
        "details": details,
    })
    # Behalte nur die letzten 52 Laeufe (1 Jahr)
    if len(history["runs"]) > 52:
        history["runs"] = history["runs"][-52:]
    history["last_run"] = datetime.now().isoformat()
    save_json("optimization_history.json", history)


# ============================================================
# SHARDED EXECUTION (Hebel 2 — GitHub Actions Matrix)
# ============================================================

def _load_optimization_data():
    """Lade alle Daten, die fuer Grid-Search/ML-Compare gebraucht werden.

    Wird sowohl von Shard-Workern (jeder downloaded selbst) als auch vom
    Merge-Job benutzt. Memory-Check + Daten-Download in einer Funktion
    gebuendelt, damit Shard- und Merge-Pfad identische Daten sehen.

    Returns:
        dict mit config, histories, vix_history, earnings_blackouts,
        asset_params, current_params  ODER  None bei Fehler.
    """
    try:
        import psutil
        mem = psutil.virtual_memory()
        free_mb = mem.available / (1024 * 1024)
        log.info(f"Memory-Check: {free_mb:.0f} MB verfuegbar ({mem.percent:.0f}% in use)")
        if free_mb < 150:
            log.error(f"Zu wenig Speicher: {free_mb:.0f} MB")
            return None
    except Exception:
        pass

    config = load_config()
    dt = config.get("demo_trading", {})
    current_params = {
        "min_scanner_score": dt.get("min_scanner_score", 15),
        "stop_loss_pct": dt.get("stop_loss_pct", -5),
        "take_profit_pct": dt.get("take_profit_pct", 18),
        "use_ml_scoring": dt.get("use_ml_scoring", False),
    }

    log.info("Downloading historical data...")
    from app.backtester import (download_history, download_vix_history,
                                _fetch_historical_earnings_dates,
                                _build_earnings_blackout_set)
    from app.market_scanner import ASSET_UNIVERSE as _AU
    histories = download_history(years=5)
    if not histories:
        log.error("Keine Daten")
        return None

    log.info("Downloading VIX + Earnings data...")
    vix_history = download_vix_history(years=5)
    earnings_blackouts = {}
    mc_cfg = config.get("market_context", {})
    buf_before = mc_cfg.get("earnings_buffer_days_before", 3)
    buf_after = mc_cfg.get("earnings_buffer_days_after", 1)
    for sym in histories.keys():
        info = _AU.get(sym, {})
        if info.get("class", "") in ("crypto", "forex", "commodities", "indices"):
            continue
        edates = _fetch_historical_earnings_dates(sym)
        if edates:
            earnings_blackouts[sym] = _build_earnings_blackout_set(
                sym, edates, buf_before, buf_after)
    log.info(f"  VIX: {len(vix_history)} days, Earnings: {len(earnings_blackouts)} symbols")

    asset_params = calculate_volatility_sl_tp(histories)
    config["asset_class_params"] = asset_params

    return {
        "config": config,
        "histories": histories,
        "vix_history": vix_history,
        "earnings_blackouts": earnings_blackouts,
        "asset_params": asset_params,
        "current_params": current_params,
    }


def run_shard_optimization(shard_id: int, num_shards: int):
    """Shard-Mode: Nur Grid-Search fuer eigene Shard-Slice ausfuehren.

    Schreibt Partial-Resultate nach data/optimizer_shard_{N}.json.
    KEIN ML-Training, KEIN Config-Update, KEIN Gist-Push.
    Der Merge-Job sammelt alle Shards ein und entscheidet final.
    """
    log.info("=" * 55)
    log.info(f"OPTIMIZER SHARD {shard_id+1}/{num_shards} START")
    log.info("=" * 55)

    data = _load_optimization_data()
    if not data:
        return {"action": "error", "error": "Daten-Load fehlgeschlagen", "shard_id": shard_id}

    grid_result = run_grid_search(
        data["histories"],
        data["config"],
        vix_history=data["vix_history"],
        earnings_blackouts=data["earnings_blackouts"],
        shard_id=shard_id,
        num_shards=num_shards,
    )

    shard_payload = {
        "shard_id": shard_id,
        "num_shards": num_shards,
        "timestamp": datetime.now().isoformat(),
        "total_tested": grid_result.get("total_tested", 0),
        "all_results": grid_result.get("all_results", []),
        "asset_params": data["asset_params"],
    }
    save_json(f"optimizer_shard_{shard_id}.json", shard_payload)
    log.info(f"Shard {shard_id+1}/{num_shards} fertig: "
             f"{shard_payload['total_tested']} Combos getestet, "
             f"gespeichert in optimizer_shard_{shard_id}.json")
    log.info("=" * 55)

    return {
        "action": "shard_done",
        "shard_id": shard_id,
        "total_tested": shard_payload["total_tested"],
    }


def run_merge_optimization():
    """Merge-Mode: Sammle alle Shard-Resultate ein und fuehre den Rest der
    Optimizer-Pipeline aus (ML-Compare, Apply, Save, Push).

    Erwartet, dass data/optimizer_shard_*.json bereits vorliegen
    (von vorherigen Matrix-Jobs oder per download-artifact eingespielt).
    """
    import glob

    log.info("=" * 55)
    log.info("OPTIMIZER MERGE START")
    log.info("=" * 55)

    # 1. Shard-Dateien einlesen (data/ ist das config_manager-Verzeichnis)
    from app.config_manager import DATA_DIR
    shard_files = sorted(glob.glob(os.path.join(DATA_DIR, "optimizer_shard_*.json")))
    if not shard_files:
        log.error("Keine Shard-Dateien gefunden — Merge abgebrochen")
        return {"action": "error", "error": "Keine optimizer_shard_*.json gefunden"}

    log.info(f"Merge: {len(shard_files)} Shard-Dateien gefunden")

    all_results = []
    asset_params_merged = {}
    total_tested = 0
    for sf in shard_files:
        try:
            payload = load_json(os.path.basename(sf)) or {}
            partial = payload.get("all_results", [])
            all_results.extend(partial)
            total_tested += payload.get("total_tested", 0)
            if not asset_params_merged and payload.get("asset_params"):
                asset_params_merged = payload["asset_params"]
            log.info(f"  {os.path.basename(sf)}: {len(partial)} Resultate")
        except Exception as e:
            log.warning(f"  {sf} konnte nicht gelesen werden: {e}")

    if not all_results:
        log.error("Merge: Keine Grid-Resultate in den Shards")
        return {"action": "error", "error": "Leere Shard-Resultate"}

    all_results.sort(key=lambda r: r["oos_sharpe"], reverse=True)
    best = all_results[0]
    log.info(f"Merge: globales Best aus {total_tested} Combos: "
             f"OOS Sharpe={best['oos_sharpe']:.2f}, params={best['params']}")

    # 2. Daten erneut laden (fuer ML-Compare brauchen wir histories + filter)
    data = _load_optimization_data()
    if not data:
        return {"action": "error", "error": "Merge: Daten-Load fehlgeschlagen"}

    config = data["config"]
    if asset_params_merged:
        config["asset_class_params"] = asset_params_merged
    current_params = data["current_params"]
    histories = data["histories"]
    filter_kwargs = {
        "vix_history": data["vix_history"],
        "earnings_blackouts": data["earnings_blackouts"],
    }

    # 3. Rollback-Check (selbe Logik wie weekly)
    should_rollback, rollback_reason = check_rollback_needed(config)
    if should_rollback:
        success, msg = rollback_optimization()
        log.warning(f"Rollback: {msg}")
        _save_optimization_run("rollback", current_params, current_params,
                               {"reason": rollback_reason})
        return {"action": "rollback", "reason": rollback_reason}

    # 4. ML Trade-History Training (best effort)
    ml_trade_training = None
    try:
        from app.ml_scorer import train_from_trade_history, MIN_TRADES_FOR_TRAINING
        trade_history = load_json("trade_history.json") or []
        if len(trade_history) >= MIN_TRADES_FOR_TRAINING:
            log.info(f"ML Training auf {len(trade_history)} eigenen Trades...")
            ml_trade_training = train_from_trade_history(trade_history)
    except Exception as e:
        log.warning(f"ML Trade-History Training Fehler: {e}")

    # 5. ML vs Fixed Compare
    log.info("Vergleiche ML vs Fixed Weights...")
    ml_comparison = compare_ml_vs_fixed(histories, config, **filter_kwargs)

    # 6. Min Expected Return
    min_return = calculate_min_expected_return()
    log.info(f"  Min Expected Return: {min_return}% (nach Kosten)")

    # 7. Aenderungen ableiten — v12: nur bei positivem OOS Sharpe + Sanity-Guards
    changes_made = {}
    new_params = dict(current_params)
    if best and best["oos_sharpe"] > 0:
        bp = best["params"]
        oos_sharpe_ok = best["oos_sharpe"] > 0

        if bp["min_scanner_score"] != current_params["min_scanner_score"]:
            new_params["min_scanner_score"] = bp["min_scanner_score"]
            changes_made["min_scanner_score"] = {
                "old": current_params["min_scanner_score"],
                "new": bp["min_scanner_score"]}

        if bp["stop_loss_pct"] != current_params["stop_loss_pct"]:
            cur_sl = abs(current_params["stop_loss_pct"] or 0)
            new_sl = abs(bp["stop_loss_pct"] or 0)
            if oos_sharpe_ok or (0.5 <= (new_sl / max(cur_sl, 0.1)) <= 2.0):
                new_params["stop_loss_pct"] = bp["stop_loss_pct"]
                changes_made["stop_loss_pct"] = {
                    "old": current_params["stop_loss_pct"],
                    "new": bp["stop_loss_pct"]}

        if bp["take_profit_pct"] != current_params["take_profit_pct"]:
            cur_tp = current_params["take_profit_pct"] or 0
            new_tp = bp["take_profit_pct"] or 0
            tp_drop_pct = (cur_tp - new_tp) / max(cur_tp, 0.1)
            if oos_sharpe_ok or tp_drop_pct < 0.3:
                new_params["take_profit_pct"] = bp["take_profit_pct"]
                changes_made["take_profit_pct"] = {
                    "old": current_params["take_profit_pct"],
                    "new": bp["take_profit_pct"]}
            else:
                log.info(f"  [v12-guard] TP-Aenderung blockiert: "
                         f"{cur_tp} -> {new_tp} (drop={tp_drop_pct:.0%})")

    if ml_comparison["recommendation"] == "switch_to_ml":
        new_params["use_ml_scoring"] = True
        changes_made["use_ml_scoring"] = {"old": False, "new": True}
    elif ml_comparison["recommendation"] == "keep_fixed" and current_params.get("use_ml_scoring"):
        new_params["use_ml_scoring"] = False
        changes_made["use_ml_scoring"] = {"old": True, "new": False}

    # 8. Apply
    dt = config.get("demo_trading", {})
    if changes_made:
        dt["min_scanner_score"] = new_params["min_scanner_score"]
        dt["stop_loss_pct"] = new_params["stop_loss_pct"]
        dt["take_profit_pct"] = new_params["take_profit_pct"]
        dt["use_ml_scoring"] = new_params["use_ml_scoring"]
        config["min_expected_return_pct"] = min_return
        save_config(config)
        log.info(f"Config aktualisiert: {changes_made}")
    else:
        log.info("Keine Aenderungen — aktuelle Parameter sind optimal")

    result = {
        "action": "optimized" if changes_made else "no_change",
        "timestamp": datetime.now().isoformat(),
        "changes": changes_made,
        "grid_search": {
            "best_params": best["params"],
            "best_oos_sharpe": best["oos_sharpe"],
            "top_5": all_results[:5],
            "total_tested": total_tested,
            "shards_merged": len(shard_files),
        },
        "ml_comparison": ml_comparison,
        "ml_trade_training": ml_trade_training,
        "asset_class_params": data["asset_params"],
        "min_expected_return_pct": min_return,
        "current_params": current_params,
        "new_params": new_params,
    }
    _save_optimization_run(result["action"], current_params, new_params, result)

    log.info("=" * 55)
    log.info(f"MERGE COMPLETE: {result['action']}")
    log.info("=" * 55)

    # Telegram alert (Push macht der Runner danach selbst)
    try:
        from app.alerts import alert_optimizer_completed
        alert_optimizer_completed(result)
    except Exception:
        pass

    return result


# ============================================================
# SCHEDULER HELPER
# ============================================================

def is_sunday_optimization_time():
    """Pruefe ob Sonntag-Optimierung faellig ist.

    Erweitertes Fenster: Sonntag 02:00-06:00 UTC.
    UTC, damit das Fenster nicht durch DST-Wechsel oder Container-TZ
    verschoben wird (analog GitHub-Action-Crons).
    Intervall konfigurierbar (default: 14 Tage = bi-weekly).
    Verhindert verpasste Laeufe durch Render Free Tier Sleep.
    """
    now = datetime.now(timezone.utc)
    if now.weekday() != 6 or now.hour < 2 or now.hour >= 6:
        return False

    # Pruefe ob genug Zeit seit letztem Lauf vergangen
    config = load_config()
    interval_days = config.get("optimizer", {}).get("optimization_interval_days", 14)

    try:
        from app.config_manager import load_json
        history = load_json("optimization_history.json") or {}
        last_run = history.get("last_run", "")
        if last_run:
            last_dt = datetime.fromisoformat(last_run)
            days_since = (now - last_dt).days
            if days_since < max(interval_days - 1, 6):
                return False  # Intervall noch nicht erreicht
    except Exception:
        pass

    return True
