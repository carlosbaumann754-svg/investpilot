"""
InvestPilot - Leverage Manager
Dynamische Hebel-Selektion, eToro-Limits, Margin-Safety,
Trailing Stop-Loss, Take-Profit Staffelung, Short-Support.
"""

import logging
from datetime import datetime

from app.config_manager import load_config, load_json, save_json

log = logging.getLogger("LeverageManager")

# ============================================================
# eToro MAXIMALHEBEL JE ASSET-KLASSE (Retail)
# ============================================================

ETORO_MAX_LEVERAGE = {
    "forex_major": 30,
    "forex_minor": 20,
    "forex_exotic": 20,
    "indices": 20,
    "commodities": 10,
    "stocks": 5,
    "etf": 5,
    "crypto": 2,
}

FOREX_MAJORS = {"EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD"}

# eToro erlaubte Hebel-Stufen
ALLOWED_LEVERAGES = {
    "stocks": [1, 2, 5],
    "etf": [1, 2, 5],
    "crypto": [1, 2],
    "forex_major": [1, 2, 5, 10, 20, 30],
    "forex_minor": [1, 2, 5, 10, 20],
    "forex_exotic": [1, 2, 5, 10, 20],
    "indices": [1, 2, 5, 10, 20],
    "commodities": [1, 2, 5, 10],
}


def _get_leverage_class(asset_class, symbol=""):
    """Bestimme die eToro Leverage-Klasse."""
    if asset_class == "forex":
        if symbol.upper() in FOREX_MAJORS:
            return "forex_major"
        return "forex_minor"
    return asset_class


def get_max_leverage(asset_class, symbol=""):
    """Hole maximalen erlaubten Hebel fuer Asset-Klasse."""
    lev_class = _get_leverage_class(asset_class, symbol)
    return ETORO_MAX_LEVERAGE.get(lev_class, 1)


def get_allowed_leverages(asset_class, symbol=""):
    """Hole erlaubte Hebel-Stufen fuer Asset-Klasse."""
    lev_class = _get_leverage_class(asset_class, symbol)
    return ALLOWED_LEVERAGES.get(lev_class, [1])


def snap_to_allowed(leverage, asset_class, symbol=""):
    """Runde Hebel auf naechste erlaubte Stufe ab."""
    allowed = get_allowed_leverages(asset_class, symbol)
    # Groesste erlaubte Stufe die <= gewuenschtem Hebel ist
    valid = [l for l in allowed if l <= leverage]
    return max(valid) if valid else 1


# ============================================================
# DYNAMISCHE HEBEL-SELEKTION
# ============================================================

def calculate_optimal_leverage(asset_class, symbol, volatility, signal_confidence,
                               market_regime="unknown", vix_level=None, config=None):
    """Berechne optimalen Hebel basierend auf Marktbedingungen.

    Faktoren:
    - Asset-Klasse (Maximum)
    - Volatilitaet (hoeher = weniger Hebel)
    - Signal-Konfidenz (staerker = mehr Hebel)
    - Marktregime (bear = weniger Hebel)
    - VIX Level (hoch = weniger Hebel)
    """
    if config is None:
        config = load_config()
    lev_cfg = config.get("leverage", {})

    max_lev = get_max_leverage(asset_class, symbol)
    base_leverage = lev_cfg.get("default_leverage", 2)

    # Starte mit Basis-Hebel
    optimal = min(base_leverage, max_lev)

    # Volatilitaets-Anpassung
    vol_thresholds = lev_cfg.get("volatility_thresholds", {
        "low": 2.0,    # <2%: Hebel erhoehen
        "medium": 4.0,  # 2-4%: Normal
        "high": 6.0,    # >4%: Hebel reduzieren
    })
    if volatility < vol_thresholds.get("low", 2.0):
        optimal = min(optimal * 1.5, max_lev)
    elif volatility > vol_thresholds.get("high", 6.0):
        optimal = max(optimal * 0.5, 1)
    elif volatility > vol_thresholds.get("medium", 4.0):
        optimal = max(optimal * 0.75, 1)

    # Signal-Konfidenz (Score -100 bis +100, normalisiert)
    if abs(signal_confidence) > 30:
        optimal = min(optimal * 1.2, max_lev)
    elif abs(signal_confidence) < 10:
        optimal = max(optimal * 0.5, 1)

    # Marktregime
    if market_regime == "bear":
        optimal = max(optimal * 0.5, 1)
    elif market_regime == "sideways":
        optimal = max(optimal * 0.75, 1)

    # VIX Level
    if vix_level is not None:
        if vix_level > 30:
            optimal = max(optimal * 0.5, 1)
        elif vix_level > 20:
            optimal = max(optimal * 0.75, 1)

    # Auf erlaubte Stufe runden
    return snap_to_allowed(int(optimal), asset_class, symbol)


# ============================================================
# TRAILING STOP-LOSS
# ============================================================

TRAILING_SL_FILE = "trailing_sl_state.json"


def _load_trailing_state():
    return load_json(TRAILING_SL_FILE) or {}


def _save_trailing_state(state):
    save_json(TRAILING_SL_FILE, state)


def update_trailing_stop_loss(position_id, current_price, entry_price, leverage=1, config=None):
    """Aktualisiere Trailing Stop-Loss fuer eine Position.

    Der SL bewegt sich nur nach oben (bei Longs) / nach unten (bei Shorts).
    """
    if config is None:
        config = load_config()
    lev_cfg = config.get("leverage", {})
    trail_pct = lev_cfg.get("trailing_sl_pct", 2.0)
    activation_pct = lev_cfg.get("trailing_sl_activation_pct", 1.0)

    state = _load_trailing_state()
    pid = str(position_id)

    # Berechne aktuelle Rendite
    pnl_pct = (current_price - entry_price) / entry_price * 100

    # Trailing SL erst aktivieren wenn Position im Gewinn
    if pnl_pct < activation_pct:
        return None

    # Trailing SL Level berechnen
    trail_level = current_price * (1 - trail_pct / 100)

    # Nur erhoehen, nie senken
    if pid in state:
        old_level = state[pid].get("sl_level", 0)
        if trail_level > old_level:
            state[pid]["sl_level"] = round(trail_level, 4)
            state[pid]["updated"] = datetime.now().isoformat()
            log.info(f"  Trailing SL aktualisiert: Pos {pid} -> ${trail_level:.4f} "
                     f"(+{pnl_pct:.1f}%)")
        else:
            trail_level = old_level
    else:
        state[pid] = {
            "sl_level": round(trail_level, 4),
            "entry_price": entry_price,
            "activated": datetime.now().isoformat(),
            "updated": datetime.now().isoformat(),
        }
        log.info(f"  Trailing SL aktiviert: Pos {pid} -> ${trail_level:.4f}")

    _save_trailing_state(state)
    return trail_level


def check_trailing_stop_losses(positions):
    """Pruefe ob Trailing SL ausgeloest wurde. Gibt Liste von position_ids zurueck."""
    state = _load_trailing_state()
    triggered = []

    for pos in positions:
        pid = str(pos.get("position_id", ""))
        if pid not in state:
            continue

        current_price = pos.get("current_price", 0)
        sl_level = state[pid].get("sl_level", 0)

        if current_price > 0 and current_price <= sl_level:
            triggered.append({
                "position_id": pos.get("position_id"),
                "instrument_id": pos.get("instrument_id"),
                "sl_level": sl_level,
                "current_price": current_price,
            })
            log.warning(f"  Trailing SL TRIGGERED: Pos {pid}, "
                        f"Price ${current_price:.4f} <= SL ${sl_level:.4f}")

    return triggered


def cleanup_trailing_state(open_position_ids):
    """Entferne Trailing-SL-Eintraege fuer geschlossene Positionen."""
    state = _load_trailing_state()
    open_set = {str(pid) for pid in open_position_ids}
    cleaned = {pid: data for pid, data in state.items() if pid in open_set}
    if len(cleaned) != len(state):
        _save_trailing_state(cleaned)


# ============================================================
# TAKE-PROFIT STAFFELUNG
# ============================================================

def calculate_tp_tranches(entry_price, total_amount, config=None):
    """Berechne Take-Profit Tranchen (gestaffelter Ausstieg).

    Default: 50% bei TP1, 30% bei TP2, 20% laufen lassen.
    """
    if config is None:
        config = load_config()
    lev_cfg = config.get("leverage", {})

    tranches = lev_cfg.get("tp_tranches", [
        {"pct_of_position": 50, "profit_target_pct": 3},
        {"pct_of_position": 30, "profit_target_pct": 6},
        {"pct_of_position": 20, "profit_target_pct": 10},
    ])

    result = []
    for tranche in tranches:
        tp_price = entry_price * (1 + tranche["profit_target_pct"] / 100)
        amount = total_amount * tranche["pct_of_position"] / 100
        result.append({
            "target_price": round(tp_price, 4),
            "target_pct": tranche["profit_target_pct"],
            "amount_usd": round(amount, 2),
            "pct_of_position": tranche["pct_of_position"],
        })

    return result


# ============================================================
# RISK/REWARD CHECK
# ============================================================

def check_risk_reward(entry_price, stop_loss_price, take_profit_price,
                      min_ratio=2.0):
    """Pruefe ob Risk/Reward-Verhaeltnis ausreichend ist.

    Kein Trade wird eroeffnet wenn R/R unter min_ratio liegt.
    """
    risk = abs(entry_price - stop_loss_price)
    reward = abs(take_profit_price - entry_price)

    if risk == 0:
        return False, 0, "Risk ist 0"

    ratio = reward / risk

    if ratio < min_ratio:
        return False, round(ratio, 2), f"R/R {ratio:.1f} < Min {min_ratio}"
    return True, round(ratio, 2), "OK"


# ============================================================
# SHORT POSITION SUPPORT
# ============================================================

def validate_short_entry(asset_class, market_regime, signal_score, config=None):
    """Validiere ob Short-Position eroeffnet werden darf.

    - In Aufwaertstrends: keine Shorts (ausser STRONG_SELL)
    - Zusaetzliche SL-Pflicht fuer Shorts
    - Nur in erlaubten Asset-Klassen
    """
    if config is None:
        config = load_config()
    lev_cfg = config.get("leverage", {})
    short_enabled = lev_cfg.get("short_enabled", True)
    short_classes = lev_cfg.get("short_allowed_classes",
                                ["stocks", "etf", "forex", "indices", "commodities"])

    if not short_enabled:
        return False, "Short-Trading deaktiviert"

    if asset_class not in short_classes:
        return False, f"Shorts nicht erlaubt fuer {asset_class}"

    # In Bull-Maerkten nur bei starkem Sell-Signal
    if market_regime == "bull" and signal_score > -25:
        return False, f"Bull-Markt: Short nur bei STRONG_SELL (Score {signal_score} > -25)"

    return True, "OK"


# ============================================================
# HEBEL-LOGGING & REPORTING
# ============================================================

def log_leverage_trade(trade_entry, portfolio_value):
    """Ergaenze Trade-Log mit Hebel-spezifischen Daten."""
    invested = trade_entry.get("amount_usd", 0)
    leverage = trade_entry.get("leverage", 1)
    effective = invested * leverage

    trade_entry["effective_exposure"] = round(effective, 2)
    trade_entry["max_loss_potential"] = round(invested, 2)  # Bei CFDs: investierter Betrag
    trade_entry["portfolio_exposure_pct"] = round(
        effective / portfolio_value * 100, 2) if portfolio_value > 0 else 0

    return trade_entry


def get_leverage_summary(positions):
    """Erstelle Hebel-Zusammenfassung fuer Dashboard."""
    summary = {
        "total_leveraged_positions": 0,
        "total_effective_exposure": 0,
        "avg_leverage": 0,
        "max_leverage_used": 0,
        "by_class": {},
    }

    leveraged = [p for p in positions if p.get("leverage", 1) > 1]
    summary["total_leveraged_positions"] = len(leveraged)

    if not leveraged:
        return summary

    leverages = []
    for pos in leveraged:
        lev = pos.get("leverage", 1)
        invested = pos.get("invested", 0)
        effective = invested * lev
        cls = pos.get("asset_class", "unknown")

        leverages.append(lev)
        summary["total_effective_exposure"] += effective

        if cls not in summary["by_class"]:
            summary["by_class"][cls] = {"count": 0, "invested": 0, "effective": 0}
        summary["by_class"][cls]["count"] += 1
        summary["by_class"][cls]["invested"] += invested
        summary["by_class"][cls]["effective"] += effective

    summary["avg_leverage"] = round(sum(leverages) / len(leverages), 1)
    summary["max_leverage_used"] = max(leverages)
    summary["total_effective_exposure"] = round(summary["total_effective_exposure"], 2)

    return summary
