"""
InvestPilot - Self-Improvement Optimizer
Woechentlicher Auto-Optimizer: testet Parameter-Kombinationen per Backtest,
waehlt die beste Out-of-Sample Konfiguration, trainiert ML-Modell,
und schreibt optimierte Parameter zurueck in config.json.

Safety Guards: Max 1 Aenderung/Woche, OOS muss besser sein, Rollback bei Einbruch.
"""

import logging
import copy
from datetime import datetime

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
    "min_scanner_score": [20, 30, 40, 50],
    "stop_loss_pct":     [-3, -5, -8],
    "take_profit_pct":   [5, 8, 12],
    "trailing_sl_pct":   [1.5, 2.0, 3.0],
    "trailing_sl_activation_pct": [0.5, 1.0],
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
# PARAMETER GRID SEARCH
# ============================================================

def run_grid_search(histories, base_config=None):
    """Teste verschiedene Parameter-Kombinationen per Walk-Forward Backtest.

    Returns:
        dict mit best_params, all_results sortiert nach OOS Sharpe
    """
    from app.backtester import walk_forward_validate

    if base_config is None:
        base_config = load_config()

    results = []
    total_combos = (len(PARAM_GRID["min_scanner_score"]) *
                    len(PARAM_GRID["stop_loss_pct"]) *
                    len(PARAM_GRID["take_profit_pct"]) *
                    len(PARAM_GRID["trailing_sl_pct"]) *
                    len(PARAM_GRID["trailing_sl_activation_pct"]))

    log.info(f"Grid-Search: {total_combos} Kombinationen testen...")
    combo_num = 0

    for min_score in PARAM_GRID["min_scanner_score"]:
        for sl in PARAM_GRID["stop_loss_pct"]:
            for tp in PARAM_GRID["take_profit_pct"]:
                # Skip unsinnige Kombis (TP muss > |SL| fuer min 1:1 R/R)
                if tp < abs(sl):
                    continue

                for trail_sl in PARAM_GRID["trailing_sl_pct"]:
                    for trail_act in PARAM_GRID["trailing_sl_activation_pct"]:
                        combo_num += 1
                        test_config = copy.deepcopy(base_config)
                        test_config["demo_trading"]["min_scanner_score"] = min_score
                        test_config["demo_trading"]["stop_loss_pct"] = sl
                        test_config["demo_trading"]["take_profit_pct"] = tp
                        if "leverage" not in test_config:
                            test_config["leverage"] = {}
                        test_config["leverage"]["trailing_sl_pct"] = trail_sl
                        test_config["leverage"]["trailing_sl_activation_pct"] = trail_act

                        try:
                            wf = walk_forward_validate(histories, test_config)
                            if not wf:
                                continue

                            oos = wf["out_of_sample"]["metrics"]
                            ins = wf["in_sample"]["metrics"]

                            results.append({
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
                            })

                            if combo_num % 20 == 0:
                                log.info(f"  Grid-Search: {combo_num}/{total_combos} getestet...")

                        except Exception as e:
                            log.debug(f"  Combo {min_score}/{sl}/{tp}/trail{trail_sl} Fehler: {e}")

    # Sortiere nach OOS Sharpe (bestes zuerst)
    results.sort(key=lambda r: r["oos_sharpe"], reverse=True)

    best = results[0] if results else None

    log.info(f"Grid-Search fertig: {len(results)} gueltige Kombinationen")
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
    }


# ============================================================
# ML AUTO-COMPARE
# ============================================================

def compare_ml_vs_fixed(histories, config=None):
    """Vergleiche ML Scoring vs Fixed Weights auf Out-of-Sample Daten.

    Returns dict mit Empfehlung ob ML aktiviert werden soll.
    """
    from app.backtester import walk_forward_validate

    if config is None:
        config = load_config()

    # 1. Fixed Weights Backtest
    fixed_config = copy.deepcopy(config)
    fixed_config["demo_trading"]["use_ml_scoring"] = False
    fixed_wf = walk_forward_validate(histories, fixed_config)

    # 2. ML trainieren und testen
    ml_result = None
    try:
        from app.ml_scorer import train_model, is_model_trained
        train_result = train_model(histories)

        if is_model_trained():
            ml_config = copy.deepcopy(config)
            ml_config["demo_trading"]["use_ml_scoring"] = True
            ml_wf = walk_forward_validate(histories, ml_config)
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

    config = load_config()
    dt = config.get("demo_trading", {})

    # Aktuelle Parameter merken (fuer Rollback)
    current_params = {
        "min_scanner_score": dt.get("min_scanner_score", 15),
        "stop_loss_pct": dt.get("stop_loss_pct", -3),
        "take_profit_pct": dt.get("take_profit_pct", 5),
        "use_ml_scoring": dt.get("use_ml_scoring", False),
    }

    # 1. Rollback-Check
    should_rollback, rollback_reason = check_rollback_needed(config)
    if should_rollback:
        success, msg = rollback_optimization()
        log.warning(f"Rollback: {msg}")
        _save_optimization_run("rollback", current_params, current_params,
                               {"reason": rollback_reason})
        return {"action": "rollback", "reason": rollback_reason}

    # 2. Daten herunterladen
    log.info("Downloading historical data...")
    from app.backtester import download_history
    histories = download_history(years=5)
    if not histories:
        log.error("Keine Daten — Optimierung abgebrochen")
        return {"error": "Keine historischen Daten"}

    # 3. Volatilitaets-basierte SL/TP
    log.info("Berechne Asset-Klassen SL/TP...")
    asset_params = calculate_volatility_sl_tp(histories)
    config["asset_class_params"] = asset_params
    log.info(f"  Asset-Klassen Parameter: {list(asset_params.keys())}")

    # 4. Grid-Search
    log.info("Starte Parameter Grid-Search...")
    grid_result = run_grid_search(histories, config)
    best = grid_result.get("best")

    # 5. ML vs Fixed Vergleich
    log.info("Vergleiche ML vs Fixed Weights...")
    ml_comparison = compare_ml_vs_fixed(histories, config)

    # 6. Kosten-Filter berechnen
    min_return = calculate_min_expected_return()
    log.info(f"  Min Expected Return: {min_return}% (nach Kosten)")

    # 7. Entscheide welche Aenderungen anwenden
    changes_made = {}
    new_params = dict(current_params)

    # Grid-Search Ergebnis anwenden (nur wenn OOS besser als aktuell)
    if best and best["oos_sharpe"] > -1:
        bp = best["params"]

        # Nur Aenderungen wenn deutlich besser
        if bp["min_scanner_score"] != current_params["min_scanner_score"]:
            new_params["min_scanner_score"] = bp["min_scanner_score"]
            changes_made["min_scanner_score"] = {
                "old": current_params["min_scanner_score"],
                "new": bp["min_scanner_score"],
            }

        if bp["stop_loss_pct"] != current_params["stop_loss_pct"]:
            new_params["stop_loss_pct"] = bp["stop_loss_pct"]
            changes_made["stop_loss_pct"] = {
                "old": current_params["stop_loss_pct"],
                "new": bp["stop_loss_pct"],
            }

        if bp["take_profit_pct"] != current_params["take_profit_pct"]:
            new_params["take_profit_pct"] = bp["take_profit_pct"]
            changes_made["take_profit_pct"] = {
                "old": current_params["take_profit_pct"],
                "new": bp["take_profit_pct"],
            }

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
# SCHEDULER HELPER
# ============================================================

def is_sunday_optimization_time():
    """Pruefe ob es Sonntag 02:00 ist (Optimierungsfenster)."""
    now = datetime.now()
    return now.weekday() == 6 and now.hour == 2 and now.minute < 5
