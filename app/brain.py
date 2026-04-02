"""
InvestPilot - Trade Brain (Selbstlernendes Analyse-Modul)
Sammelt Performance-Daten, erkennt Muster, optimiert Strategie.
Refactored aus trade_brain.py - nutzt ConfigManager.
"""

import logging
import statistics
from datetime import datetime

from app.config_manager import load_config, save_config, load_json, save_json
from app.etoro_client import EtoroClient
from app.persistence import backup_to_cloud

log = logging.getLogger("TradeBrain")

BRAIN_FILE = "brain_state.json"


def load_brain():
    """Lade oder initialisiere den Brain-State."""
    brain = load_json(BRAIN_FILE)
    if brain:
        return brain
    return {
        "version": 2,
        "created": datetime.now().isoformat(),
        "total_runs": 0,
        "performance_snapshots": [],
        "instrument_scores": {},
        "learned_rules": [],
        "optimization_log": [],
        "strategy_adjustments": {},
        "best_performers": [],
        "worst_performers": [],
        "market_regime": "unknown",
        "win_rate": 0,
        "avg_return_pct": 0,
        "sharpe_estimate": 0,
    }


def save_brain(brain):
    save_json(BRAIN_FILE, brain)


# ============================================================
# 1. SNAPSHOT
# ============================================================

def record_snapshot(portfolio):
    """Speichere Portfolio-Snapshot fuer Trendanalyse."""
    brain = load_brain()
    brain["total_runs"] += 1

    credit = portfolio.get("credit", 0)
    positions = portfolio.get("positions", [])
    unrealized = portfolio.get("unrealizedPnL", 0)

    parsed = [EtoroClient.parse_position(pos) for pos in positions]
    total_invested = sum(p["invested"] for p in parsed)

    snapshot = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "time": datetime.now().strftime("%H:%M"),
        "run_number": brain["total_runs"],
        "credit": round(credit, 2),
        "invested": round(total_invested, 2),
        "unrealized_pnl": round(unrealized, 2),
        "total_value": round(credit + total_invested + unrealized, 2),
        "num_positions": len(positions),
        "positions": [{
            "instrument_id": p["instrument_id"],
            "invested": p["invested"],
            "pnl": p["pnl"],
            "pnl_pct": p["pnl_pct"],
            "leverage": p["leverage"],
        } for p in parsed],
    }

    brain["performance_snapshots"].append(snapshot)
    if len(brain["performance_snapshots"]) > 365:
        brain["performance_snapshots"] = brain["performance_snapshots"][-365:]

    save_brain(brain)
    log.info(f"  Snapshot #{brain['total_runs']}: Wert=${snapshot['total_value']:,.2f}, "
             f"P/L=${snapshot['unrealized_pnl']:,.2f}, Positionen={snapshot['num_positions']}")
    return snapshot


# ============================================================
# 2. ANALYSE
# ============================================================

def analyze_instrument_performance():
    """Bewerte jedes Instrument basierend auf historischen Daten."""
    brain = load_brain()
    snapshots = brain["performance_snapshots"]

    if len(snapshots) < 2:
        log.info("  Zu wenig Daten fuer Analyse (min. 2 Snapshots)")
        return {}

    instrument_data = {}
    for snap in snapshots:
        for pos in snap.get("positions", []):
            iid = str(pos["instrument_id"])
            if iid not in instrument_data:
                instrument_data[iid] = {
                    "pnl_history": [], "pnl_pct_history": [],
                    "invested_history": [], "days_held": 0,
                }
            instrument_data[iid]["pnl_history"].append(pos["pnl"])
            instrument_data[iid]["pnl_pct_history"].append(pos["pnl_pct"])
            instrument_data[iid]["invested_history"].append(pos["invested"])
            instrument_data[iid]["days_held"] += 1

    scores = {}
    for iid, data in instrument_data.items():
        pnl_vals = data["pnl_pct_history"]
        if not pnl_vals:
            continue

        avg_return = statistics.mean(pnl_vals)
        volatility = statistics.stdev(pnl_vals) if len(pnl_vals) > 1 else 1
        latest_pnl = pnl_vals[-1]
        trend = pnl_vals[-1] - pnl_vals[0] if len(pnl_vals) > 1 else 0
        consistency = sum(1 for p in pnl_vals if p > 0) / len(pnl_vals)
        sharpe = avg_return / volatility if volatility > 0 else 0

        score = (
            avg_return * 0.25 +
            trend * 0.20 +
            consistency * 30 +
            sharpe * 10 +
            (10 if latest_pnl > 0 else -5)
        )

        scores[iid] = {
            "score": round(score, 2),
            "avg_return_pct": round(avg_return, 2),
            "volatility": round(volatility, 2),
            "trend": round(trend, 2),
            "consistency": round(consistency * 100, 1),
            "sharpe": round(sharpe, 3),
            "days_held": data["days_held"],
            "latest_pnl_pct": round(latest_pnl, 2),
            "total_pnl": round(sum(data["pnl_history"]) / max(len(data["pnl_history"]), 1), 2),
        }

    sorted_scores = dict(sorted(scores.items(), key=lambda x: x[1]["score"], reverse=True))

    brain["instrument_scores"] = sorted_scores
    brain["best_performers"] = list(sorted_scores.keys())[:3]
    brain["worst_performers"] = list(sorted_scores.keys())[-3:]
    save_brain(brain)

    log.info("  Instrument-Scores:")
    for iid, s in sorted_scores.items():
        emoji = "+" if s["score"] > 0 else "-"
        log.info(f"    [{emoji}] #{iid}: Score={s['score']}, "
                 f"Avg={s['avg_return_pct']}%, Trend={s['trend']:+.1f}, "
                 f"Win={s['consistency']}%")

    return sorted_scores


# ============================================================
# 3. MARKTREGIME
# ============================================================

def detect_market_regime():
    """Erkenne aktuelles Marktregime aus Portfolio-Entwicklung."""
    brain = load_brain()
    snapshots = brain["performance_snapshots"]

    if len(snapshots) < 3:
        brain["market_regime"] = "unknown"
        save_brain(brain)
        return "unknown"

    recent = snapshots[-min(10, len(snapshots)):]
    values = [s["total_value"] for s in recent]

    if len(values) < 2:
        return "unknown"

    changes = [(values[i] - values[i-1]) / values[i-1] * 100
               for i in range(1, len(values)) if values[i-1] > 0]

    if not changes:
        return "unknown"

    avg_change = statistics.mean(changes)
    positive_ratio = sum(1 for c in changes if c > 0) / len(changes)

    if avg_change > 0.3 and positive_ratio > 0.6:
        regime = "bull"
    elif avg_change < -0.3 and positive_ratio < 0.4:
        regime = "bear"
    else:
        regime = "sideways"

    brain["market_regime"] = regime
    save_brain(brain)
    log.info(f"  Marktregime: {regime.upper()} (Avg Change: {avg_change:+.2f}%, "
             f"Positive: {positive_ratio*100:.0f}%)")
    return regime


# ============================================================
# 4. REGELN LERNEN
# ============================================================

def learn_rules():
    """Leite Regeln aus der Performance-Historie ab."""
    brain = load_brain()
    scores = brain.get("instrument_scores", {})
    snapshots = brain.get("performance_snapshots", [])
    regime = brain.get("market_regime", "unknown")

    if len(snapshots) < 5:
        log.info("  Zu wenig Daten zum Lernen (min. 5 Laeufe)")
        return []

    new_rules = []
    config = load_config()
    targets = config.get("demo_trading", {}).get("portfolio_targets", {})

    # Regel 1: Top-Performer aufstocken
    for iid in brain.get("best_performers", [])[:2]:
        s = scores.get(iid, {})
        if s.get("score", 0) > 15 and s.get("consistency", 0) > 60:
            sym = None
            for symbol, t in targets.items():
                if str(t["instrument_id"]) == str(iid):
                    sym = symbol
                    break
            if sym:
                new_rules.append({
                    "type": "INCREASE_ALLOCATION",
                    "instrument_id": iid, "symbol": sym,
                    "reason": f"Konsistent positiv: Score={s['score']}, Win={s['consistency']}%",
                    "suggested_change_pct": 2,
                    "confidence": min(s["consistency"] / 100, 0.9),
                    "created": datetime.now().isoformat(),
                })

    # Regel 2: Schlechte Performer reduzieren
    for iid in brain.get("worst_performers", [])[-2:]:
        s = scores.get(iid, {})
        if s.get("score", 0) < -10 and s.get("trend", 0) < -2:
            sym = None
            for symbol, t in targets.items():
                if str(t["instrument_id"]) == str(iid):
                    sym = symbol
                    break
            if sym:
                new_rules.append({
                    "type": "DECREASE_ALLOCATION",
                    "instrument_id": iid, "symbol": sym,
                    "reason": f"Negativer Trend: Score={s['score']}, Trend={s['trend']}",
                    "suggested_change_pct": -2,
                    "confidence": min(abs(s["score"]) / 30, 0.8),
                    "created": datetime.now().isoformat(),
                })

    # Regel 3: Marktregime-basierte Anpassungen
    if regime == "bear":
        new_rules.append({
            "type": "REGIME_ADJUSTMENT",
            "reason": "Baerischer Markt - defensiver positionieren",
            "action": "Erhoehe Cash-Anteil, reduziere spekulative Positionen",
            "confidence": 0.6,
            "created": datetime.now().isoformat(),
        })
    elif regime == "bull":
        new_rules.append({
            "type": "REGIME_ADJUSTMENT",
            "reason": "Bullischer Markt - aggressiver positionieren",
            "action": "Reduziere Cash, erhoehe Wachstumspositionen",
            "confidence": 0.6,
            "created": datetime.now().isoformat(),
        })

    # Regel 4: Stop-Loss optimieren
    if len(snapshots) >= 10:
        all_pnl = []
        for snap in snapshots[-10:]:
            for pos in snap.get("positions", []):
                all_pnl.append(pos["pnl_pct"])
        if all_pnl and min(all_pnl) < -15:
            new_rules.append({
                "type": "TIGHTEN_STOP_LOSS",
                "reason": f"Hoher Max-Verlust: {min(all_pnl):.1f}%",
                "suggested_stop_loss": max(min(all_pnl) * 0.7, -20),
                "confidence": 0.7,
                "created": datetime.now().isoformat(),
            })

    brain["learned_rules"].extend(new_rules)
    if len(brain["learned_rules"]) > 50:
        brain["learned_rules"] = brain["learned_rules"][-50:]

    save_brain(brain)

    for r in new_rules:
        log.info(f"  NEUE REGEL: {r['type']} - {r['reason']} (Conf: {r.get('confidence', 0):.0%})")

    return new_rules


# ============================================================
# 5. OPTIMIERUNG
# ============================================================

def optimize_strategy():
    """Passe Strategie basierend auf gelernten Regeln an."""
    brain = load_brain()
    rules = brain.get("learned_rules", [])

    if not rules:
        log.info("  Keine Regeln zum Optimieren vorhanden")
        return False

    config = load_config()
    dt = config.get("demo_trading", {})
    targets = dt.get("portfolio_targets", {})
    changed = False

    high_conf_rules = [r for r in rules if r.get("confidence", 0) >= 0.7]

    for rule in high_conf_rules[-5:]:
        rtype = rule.get("type")

        if rtype == "INCREASE_ALLOCATION":
            sym = rule.get("symbol")
            if sym in targets:
                old = targets[sym]["allocation_pct"]
                max_cap = dt.get("max_allocation_per_instrument_pct", 25)
                new = min(old + rule["suggested_change_pct"], max_cap)
                if new != old:
                    targets[sym]["allocation_pct"] = new
                    changed = True
                    log.info(f"  OPTIMIERUNG: {sym} {old}% -> {new}%")
                    brain["optimization_log"].append({
                        "date": datetime.now().isoformat(),
                        "action": f"{sym} {old}% -> {new}%",
                        "rule": rule["reason"],
                    })

        elif rtype == "DECREASE_ALLOCATION":
            sym = rule.get("symbol")
            if sym in targets:
                old = targets[sym]["allocation_pct"]
                new = max(old + rule["suggested_change_pct"], 2)
                if new != old:
                    targets[sym]["allocation_pct"] = new
                    changed = True
                    log.info(f"  OPTIMIERUNG: {sym} {old}% -> {new}%")
                    brain["optimization_log"].append({
                        "date": datetime.now().isoformat(),
                        "action": f"{sym} {old}% -> {new}%",
                        "rule": rule["reason"],
                    })

        elif rtype == "TIGHTEN_STOP_LOSS":
            new_sl = rule.get("suggested_stop_loss", -10)
            old_sl = dt.get("stop_loss_pct", -10)
            if new_sl != old_sl:
                dt["stop_loss_pct"] = round(new_sl, 1)
                changed = True
                log.info(f"  OPTIMIERUNG: Stop-Loss {old_sl}% -> {new_sl:.1f}%")

    if changed:
        total_alloc = sum(t["allocation_pct"] for t in targets.values())
        if total_alloc != 100:
            factor = 100 / total_alloc
            for sym in targets:
                targets[sym]["allocation_pct"] = round(targets[sym]["allocation_pct"] * factor, 1)
            log.info(f"  Allokation normalisiert ({total_alloc}% -> 100%)")

        config["demo_trading"]["portfolio_targets"] = targets
        save_config(config)

    if len(brain["optimization_log"]) > 100:
        brain["optimization_log"] = brain["optimization_log"][-100:]

    save_brain(brain)
    return changed


# ============================================================
# 6. REPORT
# ============================================================

def generate_performance_report():
    """Erstelle Performance-Report."""
    brain = load_brain()
    snapshots = brain.get("performance_snapshots", [])

    report = {
        "generated": datetime.now().isoformat(),
        "total_runs": brain.get("total_runs", 0),
        "market_regime": brain.get("market_regime", "unknown"),
        "days_tracked": len(snapshots),
    }

    if not snapshots:
        return report

    first = snapshots[0]
    latest = snapshots[-1]
    start_value = first["total_value"]
    current_value = latest["total_value"]
    total_return = ((current_value - start_value) / start_value * 100) if start_value > 0 else 0

    report["start_value"] = start_value
    report["current_value"] = current_value
    report["total_return_pct"] = round(total_return, 2)
    report["total_return_usd"] = round(current_value - start_value, 2)

    daily_returns = []
    for i in range(1, len(snapshots)):
        prev = snapshots[i-1]["total_value"]
        curr = snapshots[i]["total_value"]
        if prev > 0:
            daily_returns.append((curr - prev) / prev * 100)

    if daily_returns:
        report["avg_daily_return"] = round(statistics.mean(daily_returns), 3)
        report["max_daily_gain"] = round(max(daily_returns), 2)
        report["max_daily_loss"] = round(min(daily_returns), 2)
        report["win_days"] = sum(1 for r in daily_returns if r > 0)
        report["lose_days"] = sum(1 for r in daily_returns if r < 0)
        report["win_rate"] = round(report["win_days"] / len(daily_returns) * 100, 1)

        if len(daily_returns) > 1:
            vol = statistics.stdev(daily_returns)
            report["daily_volatility"] = round(vol, 3)
            report["sharpe_estimate"] = round(
                (statistics.mean(daily_returns) / vol * (252 ** 0.5)) if vol > 0 else 0, 2)

        brain["win_rate"] = report.get("win_rate", 0)
        brain["avg_return_pct"] = report.get("avg_daily_return", 0)
        brain["sharpe_estimate"] = report.get("sharpe_estimate", 0)

    report["instrument_scores"] = brain.get("instrument_scores", {})
    report["optimization_count"] = len(brain.get("optimization_log", []))
    report["active_rules"] = len(brain.get("learned_rules", []))

    save_brain(brain)

    log.info("=" * 55)
    log.info("PERFORMANCE REPORT")
    log.info("=" * 55)
    log.info(f"  Lauf #{brain['total_runs']} | {report['days_tracked']} Tage getrackt")
    log.info(f"  Start:     ${report.get('start_value', 0):>12,.2f}")
    log.info(f"  Aktuell:   ${report.get('current_value', 0):>12,.2f}")
    log.info(f"  Rendite:   {report.get('total_return_pct', 0):>+10.2f}% "
             f"(${report.get('total_return_usd', 0):+,.2f})")
    log.info(f"  Win-Rate:  {report.get('win_rate', 0):>10.1f}%")
    log.info(f"  Sharpe:    {report.get('sharpe_estimate', 0):>10.2f}")
    log.info(f"  Regime:    {report.get('market_regime', '?')}")
    log.info(f"  Regeln:    {report.get('active_rules', 0)} aktiv")
    log.info(f"  Optimiert: {report.get('optimization_count', 0)}x")

    return report


# ============================================================
# MAIN: Kompletter Brain-Zyklus
# ============================================================

def walk_forward_validate(proposed_changes, brain):
    """Walk-Forward-Validierung: Teste Regelaenderungen auf Out-of-Sample Daten.

    Teile Snapshots in Training (80%) und Test (20%).
    Aenderung wird nur akzeptiert wenn sie auch auf Test-Daten positiv ist.
    """
    snapshots = brain.get("performance_snapshots", [])
    if len(snapshots) < 20:
        return True, "Zu wenig Daten fuer Walk-Forward (akzeptiert)"

    split = int(len(snapshots) * 0.8)
    train = snapshots[:split]
    test = snapshots[split:]

    # Berechne Performance auf Test-Set
    test_values = [s["total_value"] for s in test]
    if len(test_values) < 2 or test_values[0] <= 0:
        return True, "Test-Set zu klein"

    test_return = (test_values[-1] - test_values[0]) / test_values[0] * 100

    # Wenn Test-Performance negativ und wir wollen aggressiver werden: ablehnen
    for change in proposed_changes:
        if change.get("type") == "INCREASE_ALLOCATION" and test_return < -2:
            log.info(f"  Walk-Forward REJECT: {change.get('symbol')} Erhoehung "
                     f"(Test-Return: {test_return:+.1f}%)")
            return False, f"Test-Set negativ ({test_return:+.1f}%)"

    return True, f"Walk-Forward OK (Test-Return: {test_return:+.1f}%)"


def log_trade_decision_context(action, symbol, brain):
    """Logge vollstaendigen Kontext fuer Trade-Entscheid."""
    context = {
        "timestamp": datetime.now().isoformat(),
        "action": action,
        "symbol": symbol,
        "market_regime": brain.get("market_regime", "unknown"),
        "total_runs": brain.get("total_runs", 0),
        "win_rate": brain.get("win_rate", 0),
        "sharpe": brain.get("sharpe_estimate", 0),
        "active_rules": len(brain.get("learned_rules", [])),
    }

    # Instrument Score
    scores = brain.get("instrument_scores", {})
    for iid, score_data in scores.items():
        if symbol and symbol.upper() in str(score_data):
            context["instrument_score"] = score_data
            break

    # Market Context (wenn verfuegbar)
    try:
        from app.market_context import get_current_context
        ctx = get_current_context()
        context["vix"] = ctx.get("vix_level")
        context["fear_greed"] = ctx.get("fear_greed_index")
        context["macro_events"] = len(ctx.get("macro_events_today", []))
    except ImportError:
        pass

    decision_log = load_json("decision_log.json") or []
    decision_log.append(context)
    if len(decision_log) > 500:
        decision_log = decision_log[-500:]
    save_json("decision_log.json", decision_log)

    return context


def analyze_parameter_performance():
    """Analysiere welche Parameter-Kombinationen in welchen Marktphasen am besten performten."""
    brain = load_brain()
    opt_log = brain.get("optimization_log", [])
    snapshots = brain.get("performance_snapshots", [])

    if len(opt_log) < 3 or len(snapshots) < 10:
        return {}

    # Gruppiere Performance nach Regime
    regime_perf = {}
    for snap in snapshots:
        date = snap.get("date", "")
        regime = brain.get("market_regime", "unknown")
        if regime not in regime_perf:
            regime_perf[regime] = []

        if len(regime_perf[regime]) > 0:
            prev = regime_perf[regime][-1]
            if prev > 0:
                change = (snap["total_value"] - prev) / prev * 100
                regime_perf[regime].append(change)
            else:
                regime_perf[regime].append(0)
        else:
            regime_perf[regime].append(snap["total_value"])

    analysis = {}
    for regime, values in regime_perf.items():
        if len(values) > 1:
            returns = values[1:]  # Skip first (absolute value)
            if returns:
                analysis[regime] = {
                    "avg_return": round(statistics.mean(returns), 3) if returns else 0,
                    "win_rate": round(sum(1 for r in returns if r > 0) / len(returns) * 100, 1),
                    "count": len(returns),
                }

    return analysis


def run_brain_cycle(portfolio):
    """Fuehre kompletten Brain-Zyklus aus (v2 mit Walk-Forward)."""
    log.info("")
    log.info("=" * 55)
    log.info("TRADE BRAIN - Analyse & Optimierung")
    log.info("=" * 55)

    log.info("\n[1/7] Snapshot aufzeichnen...")
    record_snapshot(portfolio)

    log.info("\n[2/7] Performance analysieren...")
    analyze_instrument_performance()

    log.info("\n[3/7] Marktregime erkennen...")
    detect_market_regime()

    log.info("\n[4/7] Regeln ableiten...")
    new_rules = learn_rules()

    log.info("\n[5/7] Walk-Forward Validierung...")
    brain = load_brain()
    wf_ok, wf_reason = walk_forward_validate(new_rules, brain)
    log.info(f"  {wf_reason}")

    log.info("\n[6/7] Strategie optimieren...")
    if wf_ok:
        changed = optimize_strategy()
        if changed:
            log.info("  -> Strategie wurde angepasst!")
        else:
            log.info("  -> Keine Anpassung noetig")
    else:
        log.info("  -> Walk-Forward hat Aenderungen abgelehnt")

    log.info("\n[7/7] Parameter-Analyse...")
    param_analysis = analyze_parameter_performance()
    if param_analysis:
        for regime, perf in param_analysis.items():
            log.info(f"    {regime}: Avg={perf['avg_return']:+.3f}%, "
                     f"Win={perf['win_rate']}%, N={perf['count']}")

    report = generate_performance_report()

    # Sortino Ratio ergaenzen
    try:
        from app.execution import calculate_sortino_ratio
        snapshots = brain.get("performance_snapshots", [])
        if len(snapshots) > 2:
            daily_returns = []
            for i in range(1, len(snapshots)):
                prev = snapshots[i-1]["total_value"]
                curr = snapshots[i]["total_value"]
                if prev > 0:
                    daily_returns.append((curr - prev) / prev * 100)
            report["sortino_ratio"] = calculate_sortino_ratio(daily_returns)
    except ImportError:
        pass

    report["parameter_analysis"] = param_analysis

    # Cloud-Backup nach jedem Brain-Zyklus
    log.info("\n[+] Cloud-Backup der Learnings...")
    backup_to_cloud()

    log.info("\nBrain-Zyklus abgeschlossen.")
    return report
