"""
InvestPilot - Execution Quality
Slippage-Tracking, Latenz-Monitoring, Order-Typ-Optimierung.
"""

import logging
import time
from datetime import datetime

from app.config_manager import load_json, save_json

log = logging.getLogger("Execution")

EXECUTION_LOG_FILE = "execution_log.json"


def _load_execution_log():
    return load_json(EXECUTION_LOG_FILE) or []


def _save_execution_log(entries):
    # Max 1000 Eintraege behalten
    if len(entries) > 1000:
        entries = entries[-1000:]
    save_json(EXECUTION_LOG_FILE, entries)


# ============================================================
# SLIPPAGE TRACKING
# ============================================================

def track_execution(expected_price, actual_result, instrument_id, action,
                    amount_usd, asset_class, start_time):
    """Zeichne Execution-Qualitaet auf: Slippage + Latenz."""
    end_time = time.time()
    latency_ms = round((end_time - start_time) * 1000)

    # Aus dem eToro Ergebnis den tatsaechlichen Preis extrahieren
    actual_price = None
    if actual_result and isinstance(actual_result, dict):
        order = actual_result.get("orderForOpen", {})
        actual_price = order.get("openRate") or order.get("rate")

    # Slippage berechnen
    slippage_pct = 0
    slippage_usd = 0
    if expected_price and actual_price and expected_price > 0:
        slippage_pct = round((actual_price - expected_price) / expected_price * 100, 4)
        slippage_usd = round(amount_usd * slippage_pct / 100, 2)

    entry = {
        "timestamp": datetime.now().isoformat(),
        "instrument_id": instrument_id,
        "action": action,
        "amount_usd": amount_usd,
        "asset_class": asset_class,
        "expected_price": expected_price,
        "actual_price": actual_price,
        "slippage_pct": slippage_pct,
        "slippage_usd": slippage_usd,
        "latency_ms": latency_ms,
        "success": actual_result is not None,
    }

    entries = _load_execution_log()
    entries.append(entry)
    _save_execution_log(entries)

    if abs(slippage_pct) > 0.1:
        log.warning(f"  Slippage: {slippage_pct:+.4f}% (${slippage_usd:+.2f}) "
                    f"fuer Instrument {instrument_id}")
    if latency_ms > 2000:
        log.warning(f"  Hohe Latenz: {latency_ms}ms fuer Instrument {instrument_id}")

    return entry


# ============================================================
# EXECUTION ANALYTICS
# ============================================================

def get_execution_stats(days=7):
    """Berechne Execution-Statistiken der letzten N Tage."""
    entries = _load_execution_log()
    if not entries:
        return {}

    cutoff = datetime.now().replace(hour=0, minute=0, second=0)
    from datetime import timedelta
    cutoff -= timedelta(days=days)
    cutoff_str = cutoff.isoformat()

    recent = [e for e in entries if e.get("timestamp", "") >= cutoff_str]
    if not recent:
        return {"trades": 0, "period_days": days}

    slippages = [e["slippage_pct"] for e in recent if e.get("slippage_pct") is not None]
    latencies = [e["latency_ms"] for e in recent if e.get("latency_ms") is not None]
    slippage_costs = [e["slippage_usd"] for e in recent if e.get("slippage_usd") is not None]

    stats = {
        "period_days": days,
        "trades": len(recent),
        "success_rate": round(sum(1 for e in recent if e["success"]) / len(recent) * 100, 1),
    }

    if slippages:
        stats["avg_slippage_pct"] = round(sum(slippages) / len(slippages), 4)
        stats["max_slippage_pct"] = round(max(slippages), 4)
        stats["total_slippage_cost"] = round(sum(slippage_costs), 2)

    if latencies:
        stats["avg_latency_ms"] = round(sum(latencies) / len(latencies))
        stats["max_latency_ms"] = max(latencies)
        stats["p95_latency_ms"] = round(sorted(latencies)[int(len(latencies) * 0.95)])

    # Breakdown by asset class
    by_class = {}
    for e in recent:
        cls = e.get("asset_class", "unknown")
        if cls not in by_class:
            by_class[cls] = {"count": 0, "slippages": [], "latencies": []}
        by_class[cls]["count"] += 1
        if e.get("slippage_pct") is not None:
            by_class[cls]["slippages"].append(e["slippage_pct"])
        if e.get("latency_ms") is not None:
            by_class[cls]["latencies"].append(e["latency_ms"])

    stats["by_class"] = {}
    for cls, data in by_class.items():
        stats["by_class"][cls] = {
            "count": data["count"],
            "avg_slippage": round(sum(data["slippages"]) / len(data["slippages"]), 4) if data["slippages"] else 0,
            "avg_latency_ms": round(sum(data["latencies"]) / len(data["latencies"])) if data["latencies"] else 0,
        }

    return stats


# ============================================================
# PERFORMANCE BREAKDOWN (Zeit/Tag/Asset/Strategie)
# ============================================================

def get_performance_breakdown(trade_history, days=30):
    """Erstelle Performance-Breakdown nach verschiedenen Dimensionen."""
    if not trade_history:
        return {}

    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    recent = [t for t in trade_history if t.get("timestamp", "") >= cutoff]

    if not recent:
        return {"period_days": days, "total_trades": 0}

    breakdown = {
        "period_days": days,
        "total_trades": len(recent),
        "by_hour": {},
        "by_weekday": {},
        "by_asset_class": {},
        "by_symbol": {},
        "by_action": {},
    }

    weekday_names = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]

    for trade in recent:
        ts = trade.get("timestamp", "")
        pnl_pct = trade.get("pnl_pct", 0)
        symbol = trade.get("symbol", "?")
        action = trade.get("action", "?")
        asset_class = trade.get("asset_class", "unknown")

        try:
            dt = datetime.fromisoformat(ts)
            hour = dt.hour
            weekday = weekday_names[dt.weekday()]
        except (ValueError, TypeError):
            hour = 0
            weekday = "?"

        # By hour
        h_key = f"{hour:02d}:00"
        if h_key not in breakdown["by_hour"]:
            breakdown["by_hour"][h_key] = {"trades": 0, "wins": 0, "total_pnl": 0}
        breakdown["by_hour"][h_key]["trades"] += 1
        if pnl_pct > 0:
            breakdown["by_hour"][h_key]["wins"] += 1
        breakdown["by_hour"][h_key]["total_pnl"] += pnl_pct

        # By weekday
        if weekday not in breakdown["by_weekday"]:
            breakdown["by_weekday"][weekday] = {"trades": 0, "wins": 0, "total_pnl": 0}
        breakdown["by_weekday"][weekday]["trades"] += 1
        if pnl_pct > 0:
            breakdown["by_weekday"][weekday]["wins"] += 1
        breakdown["by_weekday"][weekday]["total_pnl"] += pnl_pct

        # By asset class
        if asset_class not in breakdown["by_asset_class"]:
            breakdown["by_asset_class"][asset_class] = {"trades": 0, "wins": 0, "total_pnl": 0}
        breakdown["by_asset_class"][asset_class]["trades"] += 1
        if pnl_pct > 0:
            breakdown["by_asset_class"][asset_class]["wins"] += 1
        breakdown["by_asset_class"][asset_class]["total_pnl"] += pnl_pct

        # By symbol (top 20)
        if symbol not in breakdown["by_symbol"]:
            breakdown["by_symbol"][symbol] = {"trades": 0, "wins": 0, "total_pnl": 0}
        breakdown["by_symbol"][symbol]["trades"] += 1
        if pnl_pct > 0:
            breakdown["by_symbol"][symbol]["wins"] += 1
        breakdown["by_symbol"][symbol]["total_pnl"] += pnl_pct

        # By action
        if action not in breakdown["by_action"]:
            breakdown["by_action"][action] = {"count": 0, "total_pnl": 0}
        breakdown["by_action"][action]["count"] += 1
        breakdown["by_action"][action]["total_pnl"] += pnl_pct

    # Win rates berechnen
    for dim in ["by_hour", "by_weekday", "by_asset_class", "by_symbol"]:
        for key, data in breakdown[dim].items():
            if data["trades"] > 0:
                data["win_rate"] = round(data["wins"] / data["trades"] * 100, 1)
                data["avg_pnl"] = round(data["total_pnl"] / data["trades"], 2)
            data["total_pnl"] = round(data["total_pnl"], 2)

    # Top 20 Symbole behalten
    top_symbols = sorted(breakdown["by_symbol"].items(),
                         key=lambda x: x[1]["trades"], reverse=True)[:20]
    breakdown["by_symbol"] = dict(top_symbols)

    return breakdown


# ============================================================
# SORTINO RATIO
# ============================================================

def calculate_sortino_ratio(returns, risk_free_rate=0):
    """Berechne Sortino Ratio (wie Sharpe, aber nur Downside-Volatilitaet)."""
    if not returns or len(returns) < 2:
        return 0

    import statistics
    avg_return = statistics.mean(returns)
    downside = [r for r in returns if r < risk_free_rate]

    if not downside:
        return 0 if avg_return <= risk_free_rate else float('inf')

    downside_dev = (sum((r - risk_free_rate) ** 2 for r in downside) / len(downside)) ** 0.5

    if downside_dev == 0:
        return 0

    return round((avg_return - risk_free_rate) / downside_dev * (252 ** 0.5), 2)
