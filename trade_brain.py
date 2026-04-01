"""
InvestPilot v2 - Trade Brain (Selbstlernendes Analyse-Modul)

Sammelt Performance-Daten, erkennt Muster, optimiert Strategie.
Wird nach jedem Trading-Lauf aufgerufen und verbessert sich kontinuierlich.

Datenstruktur (brain_state.json):
  - trade_history: Alle Trades mit Outcome
  - performance_snapshots: Taegliche Portfolio-Snapshots
  - strategy_scores: Bewertung jeder Position/Strategie
  - learned_rules: Automatisch abgeleitete Regeln
  - optimization_log: Aenderungen an der Strategie
"""

import json
import logging
import statistics
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
BRAIN_PATH = SCRIPT_DIR / "brain_state.json"
CONFIG_PATH = SCRIPT_DIR / "config.json"

log = logging.getLogger("TradeBrain")


def load_brain():
    """Lade oder initialisiere den Brain-State."""
    if BRAIN_PATH.exists():
        with open(BRAIN_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
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
    with open(BRAIN_PATH, "w", encoding="utf-8") as f:
        json.dump(brain, f, indent=2, ensure_ascii=False, default=str)


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(config):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


# ============================================================
# 1. SNAPSHOT: Taeglichen Portfolio-Status speichern
# ============================================================

def record_snapshot(portfolio, indices=None):
    """Speichere taeglichen Portfolio-Snapshot fuer Trendanalyse."""
    brain = load_brain()
    brain["total_runs"] += 1

    credit = portfolio.get("credit", 0)
    positions = portfolio.get("positions", [])
    unrealized = portfolio.get("unrealizedPnL", 0)
    total_invested = sum((p.get("amount") or p.get("investedAmount") or 0) for p in positions)

    snapshot = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "time": datetime.now().strftime("%H:%M"),
        "run_number": brain["total_runs"],
        "credit": round(credit, 2),
        "invested": round(total_invested, 2),
        "unrealized_pnl": round(unrealized, 2),
        "total_value": round(credit + total_invested + unrealized, 2),
        "num_positions": len(positions),
        "positions": [],
    }

    for pos in positions:
        iid = pos.get("instrumentID") or pos.get("instrumentId") or pos.get("InstrumentID") or "?"
        invested = pos.get("amount") or pos.get("investedAmount") or 0
        pnl = pos.get("unrealizedPnL", {})
        pnl_val = pnl.get("pnL", 0) if isinstance(pnl, dict) else 0
        pnl_pct = (pnl_val / invested * 100) if invested > 0 else 0

        snapshot["positions"].append({
            "instrument_id": iid,
            "invested": round(invested, 2),
            "pnl": round(pnl_val, 2),
            "pnl_pct": round(pnl_pct, 2),
            "leverage": pos.get("leverage", 1),
        })

    # Max 365 Snapshots behalten
    brain["performance_snapshots"].append(snapshot)
    if len(brain["performance_snapshots"]) > 365:
        brain["performance_snapshots"] = brain["performance_snapshots"][-365:]

    save_brain(brain)
    log.info(f"  Snapshot #{brain['total_runs']}: Wert=${snapshot['total_value']:,.2f}, "
             f"P/L=${snapshot['unrealized_pnl']:,.2f}, Positionen={snapshot['num_positions']}")
    return snapshot


# ============================================================
# 2. ANALYSE: Instrument-Performance bewerten
# ============================================================

def analyze_instrument_performance():
    """Bewerte jedes Instrument basierend auf historischen Daten."""
    brain = load_brain()
    snapshots = brain["performance_snapshots"]

    if len(snapshots) < 2:
        log.info("  Zu wenig Daten fuer Analyse (min. 2 Snapshots)")
        return {}

    # Sammle P/L-Daten pro Instrument ueber Zeit
    instrument_data = {}
    for snap in snapshots:
        for pos in snap.get("positions", []):
            iid = str(pos["instrument_id"])
            if iid not in instrument_data:
                instrument_data[iid] = {
                    "pnl_history": [],
                    "pnl_pct_history": [],
                    "invested_history": [],
                    "days_held": 0,
                }
            instrument_data[iid]["pnl_history"].append(pos["pnl"])
            instrument_data[iid]["pnl_pct_history"].append(pos["pnl_pct"])
            instrument_data[iid]["invested_history"].append(pos["invested"])
            instrument_data[iid]["days_held"] += 1

    # Score berechnen (0-100)
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

        # Composite Score: Gewichtete Kombination
        score = (
            avg_return * 0.25 +          # Durchschnittliche Rendite
            trend * 0.20 +                # Trend (steigend = gut)
            consistency * 30 +            # Win-Rate (0-1 -> 0-30)
            sharpe * 10 +                 # Risiko-adjustierte Rendite
            (10 if latest_pnl > 0 else -5)  # Aktueller Status Bonus/Malus
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

    # Sortieren
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
# 3. MARKTREGIME: Erkennen ob Bull/Bear/Seitwaerts
# ============================================================

def detect_market_regime():
    """Erkenne aktuelles Marktregime aus Portfolio-Entwicklung."""
    brain = load_brain()
    snapshots = brain["performance_snapshots"]

    if len(snapshots) < 3:
        brain["market_regime"] = "unknown"
        save_brain(brain)
        return "unknown"

    # Letzte 5-10 Snapshots analysieren
    recent = snapshots[-min(10, len(snapshots)):]
    values = [s["total_value"] for s in recent]

    if len(values) < 2:
        return "unknown"

    # Trend berechnen
    changes = [(values[i] - values[i-1]) / values[i-1] * 100
               for i in range(1, len(values)) if values[i-1] > 0]

    if not changes:
        return "unknown"

    avg_change = statistics.mean(changes)
    positive_days = sum(1 for c in changes if c > 0)
    positive_ratio = positive_days / len(changes)

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
# 4. REGELN LERNEN: Automatische Strategie-Ableitung
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
            # Finde Symbol
            sym = None
            for symbol, t in targets.items():
                if str(t["instrument_id"]) == str(iid):
                    sym = symbol
                    break
            if sym:
                rule = {
                    "type": "INCREASE_ALLOCATION",
                    "instrument_id": iid,
                    "symbol": sym,
                    "reason": f"Konsistent positiv: Score={s['score']}, Win={s['consistency']}%",
                    "suggested_change_pct": 2,
                    "confidence": min(s["consistency"] / 100, 0.9),
                    "created": datetime.now().isoformat(),
                }
                new_rules.append(rule)

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
                rule = {
                    "type": "DECREASE_ALLOCATION",
                    "instrument_id": iid,
                    "symbol": sym,
                    "reason": f"Negativer Trend: Score={s['score']}, Trend={s['trend']}",
                    "suggested_change_pct": -2,
                    "confidence": min(abs(s["score"]) / 30, 0.8),
                    "created": datetime.now().isoformat(),
                }
                new_rules.append(rule)

    # Regel 3: Marktregime-basierte Anpassungen
    if regime == "bear":
        new_rules.append({
            "type": "REGIME_ADJUSTMENT",
            "reason": "Baerischer Markt erkannt - defensiver positionieren",
            "action": "Erhoehe Cash-Anteil, reduziere spekulative Positionen",
            "confidence": 0.6,
            "created": datetime.now().isoformat(),
        })
    elif regime == "bull":
        new_rules.append({
            "type": "REGIME_ADJUSTMENT",
            "reason": "Bullischer Markt erkannt - aggressiver positionieren",
            "action": "Reduziere Cash, erhoehe Wachstumspositionen",
            "confidence": 0.6,
            "created": datetime.now().isoformat(),
        })

    # Regel 4: Stop-Loss/Take-Profit optimieren
    if len(snapshots) >= 10:
        all_pnl = []
        for snap in snapshots[-10:]:
            for pos in snap.get("positions", []):
                all_pnl.append(pos["pnl_pct"])
        if all_pnl:
            max_loss = min(all_pnl)
            max_gain = max(all_pnl)
            if max_loss < -15:
                new_rules.append({
                    "type": "TIGHTEN_STOP_LOSS",
                    "reason": f"Hoher Max-Verlust erkannt: {max_loss:.1f}%",
                    "suggested_stop_loss": max(max_loss * 0.7, -20),
                    "confidence": 0.7,
                    "created": datetime.now().isoformat(),
                })

    # Regeln speichern (max 50 behalten)
    brain["learned_rules"].extend(new_rules)
    if len(brain["learned_rules"]) > 50:
        brain["learned_rules"] = brain["learned_rules"][-50:]

    save_brain(brain)

    for r in new_rules:
        log.info(f"  NEUE REGEL: {r['type']} - {r['reason']} (Conf: {r.get('confidence', 0):.0%})")

    return new_rules


# ============================================================
# 5. OPTIMIERUNG: Strategie automatisch anpassen
# ============================================================

def optimize_strategy(dry_run=False):
    """Passe Strategie basierend auf gelernten Regeln an."""
    brain = load_brain()
    rules = brain.get("learned_rules", [])
    scores = brain.get("instrument_scores", {})

    if not rules:
        log.info("  Keine Regeln zum Optimieren vorhanden")
        return False

    config = load_config()
    dt = config.get("demo_trading", {})
    targets = dt.get("portfolio_targets", {})
    changed = False

    # Nur Regeln mit hoher Konfidenz anwenden (> 0.7)
    high_conf_rules = [r for r in rules if r.get("confidence", 0) >= 0.7]

    for rule in high_conf_rules[-5:]:  # Maximal 5 Regeln pro Lauf
        rtype = rule.get("type")

        if rtype == "INCREASE_ALLOCATION":
            sym = rule.get("symbol")
            if sym in targets:
                old_alloc = targets[sym]["allocation_pct"]
                new_alloc = min(old_alloc + rule["suggested_change_pct"], 25)  # Max 25%
                if new_alloc != old_alloc and not dry_run:
                    targets[sym]["allocation_pct"] = new_alloc
                    changed = True
                    log.info(f"  OPTIMIERUNG: {sym} Allokation {old_alloc}% -> {new_alloc}%")
                    brain["optimization_log"].append({
                        "date": datetime.now().isoformat(),
                        "action": f"{sym} {old_alloc}% -> {new_alloc}%",
                        "rule": rule["reason"],
                    })

        elif rtype == "DECREASE_ALLOCATION":
            sym = rule.get("symbol")
            if sym in targets:
                old_alloc = targets[sym]["allocation_pct"]
                new_alloc = max(old_alloc + rule["suggested_change_pct"], 2)  # Min 2%
                if new_alloc != old_alloc and not dry_run:
                    targets[sym]["allocation_pct"] = new_alloc
                    changed = True
                    log.info(f"  OPTIMIERUNG: {sym} Allokation {old_alloc}% -> {new_alloc}%")
                    brain["optimization_log"].append({
                        "date": datetime.now().isoformat(),
                        "action": f"{sym} {old_alloc}% -> {new_alloc}%",
                        "rule": rule["reason"],
                    })

        elif rtype == "TIGHTEN_STOP_LOSS":
            new_sl = rule.get("suggested_stop_loss", -10)
            old_sl = dt.get("stop_loss_pct", -10)
            if new_sl != old_sl and not dry_run:
                dt["stop_loss_pct"] = round(new_sl, 1)
                changed = True
                log.info(f"  OPTIMIERUNG: Stop-Loss {old_sl}% -> {new_sl:.1f}%")

    # Allokation normalisieren (Summe = 100%)
    if changed:
        total_alloc = sum(t["allocation_pct"] for t in targets.values())
        if total_alloc != 100:
            factor = 100 / total_alloc
            for sym in targets:
                targets[sym]["allocation_pct"] = round(targets[sym]["allocation_pct"] * factor, 1)
            log.info(f"  Allokation normalisiert (Summe war {total_alloc}%, jetzt 100%)")

        config["demo_trading"]["portfolio_targets"] = targets
        save_config(config)

    # Optimization Log kuerzen
    if len(brain["optimization_log"]) > 100:
        brain["optimization_log"] = brain["optimization_log"][-100:]

    save_brain(brain)
    return changed


# ============================================================
# 6. REPORT: Gesamtbewertung des Systems
# ============================================================

def generate_performance_report():
    """Erstelle Performance-Report fuer den aktuellen Stand."""
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

    # Portfolio-Entwicklung
    first = snapshots[0]
    latest = snapshots[-1]
    start_value = first["total_value"]
    current_value = latest["total_value"]
    total_return = ((current_value - start_value) / start_value * 100) if start_value > 0 else 0

    report["start_value"] = start_value
    report["current_value"] = current_value
    report["total_return_pct"] = round(total_return, 2)
    report["total_return_usd"] = round(current_value - start_value, 2)

    # Taegliche Returns
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

        # Update brain metrics
        brain["win_rate"] = report.get("win_rate", 0)
        brain["avg_return_pct"] = report.get("avg_daily_return", 0)
        brain["sharpe_estimate"] = report.get("sharpe_estimate", 0)

    # Top/Bottom Instruments
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
    log.info(f"  Rendite:   {report.get('total_return_pct', 0):>+10.2f}% (${report.get('total_return_usd', 0):+,.2f})")
    log.info(f"  Win-Rate:  {report.get('win_rate', 0):>10.1f}%")
    log.info(f"  Sharpe:    {report.get('sharpe_estimate', 0):>10.2f}")
    log.info(f"  Regime:    {report.get('market_regime', '?')}")
    log.info(f"  Regeln:    {report.get('active_rules', 0)} aktiv")
    log.info(f"  Optimiert: {report.get('optimization_count', 0)}x")

    return report


# ============================================================
# MAIN: Kompletter Analyse-Zyklus
# ============================================================

def run_brain_cycle(portfolio, indices=None):
    """Fuehre kompletten Brain-Zyklus aus (nach jedem Trade-Lauf)."""
    log.info("")
    log.info("=" * 55)
    log.info("TRADE BRAIN - Analyse & Optimierung")
    log.info("=" * 55)

    # 1. Snapshot speichern
    log.info("\n[1/5] Snapshot aufzeichnen...")
    record_snapshot(portfolio, indices)

    # 2. Instrument-Performance analysieren
    log.info("\n[2/5] Performance analysieren...")
    analyze_instrument_performance()

    # 3. Marktregime erkennen
    log.info("\n[3/5] Marktregime erkennen...")
    detect_market_regime()

    # 4. Regeln lernen
    log.info("\n[4/5] Regeln ableiten...")
    learn_rules()

    # 5. Strategie optimieren
    log.info("\n[5/5] Strategie optimieren...")
    changed = optimize_strategy()
    if changed:
        log.info("  -> Strategie wurde angepasst!")
    else:
        log.info("  -> Keine Anpassung noetig")

    # Report
    report = generate_performance_report()

    log.info("\nBrain-Zyklus abgeschlossen.")
    return report
