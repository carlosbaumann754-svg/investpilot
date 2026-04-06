"""
InvestPilot - Risk Manager
Zentrales Risikomanagement: Position Sizing, Drawdown-Stops,
Korrelationschecks, Margin-Ueberwachung, Exposure-Limits.
"""

import logging
import statistics
from datetime import datetime, timedelta

from app.config_manager import load_config, load_json, save_json

log = logging.getLogger("RiskManager")

RISK_STATE_FILE = "risk_state.json"


def _load_risk_state():
    state = load_json(RISK_STATE_FILE)
    if state:
        return state
    return {
        "daily_pnl_usd": 0,
        "daily_pnl_pct": 0,
        "weekly_pnl_usd": 0,
        "weekly_pnl_pct": 0,
        "daily_start_value": 0,
        "weekly_start_value": 0,
        "last_daily_reset": "",
        "last_weekly_reset": "",
        "paused_until": None,
        "pause_reason": "",
        "total_exposure": 0,
        "margin_used_pct": 0,
        "consecutive_losses": 0,
        "daily_trades": 0,
    }


def _save_risk_state(state):
    save_json(RISK_STATE_FILE, state)


# ============================================================
# DRAWDOWN TRACKING & AUTO-PAUSE
# ============================================================

def update_portfolio_tracking(portfolio_value):
    """Aktualisiere taegliche/woechentliche P/L Tracking."""
    state = _load_risk_state()
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    week_start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")

    # Tagesreset
    if state["last_daily_reset"] != today:
        state["daily_start_value"] = portfolio_value
        state["daily_pnl_usd"] = 0
        state["daily_pnl_pct"] = 0
        state["last_daily_reset"] = today
        state["daily_trades"] = 0
        log.info(f"  Risk: Tagesstart-Wert = ${portfolio_value:,.2f}")

    # Wochenreset
    if state["last_weekly_reset"] != week_start:
        state["weekly_start_value"] = portfolio_value
        state["weekly_pnl_usd"] = 0
        state["weekly_pnl_pct"] = 0
        state["last_weekly_reset"] = week_start

    # P/L berechnen
    if state["daily_start_value"] > 0:
        state["daily_pnl_usd"] = round(portfolio_value - state["daily_start_value"], 2)
        state["daily_pnl_pct"] = round(
            state["daily_pnl_usd"] / state["daily_start_value"] * 100, 2)

    if state["weekly_start_value"] > 0:
        state["weekly_pnl_usd"] = round(portfolio_value - state["weekly_start_value"], 2)
        state["weekly_pnl_pct"] = round(
            state["weekly_pnl_usd"] / state["weekly_start_value"] * 100, 2)

    _save_risk_state(state)
    return state


def check_drawdown_limits():
    """Pruefe ob Drawdown-Limits erreicht sind. Gibt (ok, reason) zurueck."""
    config = load_config()
    risk_cfg = config.get("risk_management", {})
    state = _load_risk_state()

    daily_limit = risk_cfg.get("daily_drawdown_stop_pct", -5)
    weekly_limit = risk_cfg.get("weekly_drawdown_stop_pct", -10)

    # Pause noch aktiv?
    if state.get("paused_until"):
        try:
            pause_end = datetime.fromisoformat(state["paused_until"])
            if datetime.now() < pause_end:
                return False, f"Bot pausiert bis {state['paused_until']}: {state.get('pause_reason', '')}"
            else:
                state["paused_until"] = None
                state["pause_reason"] = ""
                _save_risk_state(state)
        except (ValueError, TypeError):
            state["paused_until"] = None
            _save_risk_state(state)

    # Tages-Drawdown
    if state["daily_pnl_pct"] <= daily_limit:
        reason = (f"TAGES-DRAWDOWN-STOP: {state['daily_pnl_pct']:.1f}% "
                  f"(Limit: {daily_limit}%, Verlust: ${state['daily_pnl_usd']:,.2f})")
        log.warning(f"  {reason}")
        # Pause bis naechsten Tag 09:00
        tomorrow = datetime.now().replace(hour=9, minute=0, second=0) + timedelta(days=1)
        state["paused_until"] = tomorrow.isoformat()
        state["pause_reason"] = reason
        _save_risk_state(state)
        return False, reason

    # Wochen-Drawdown
    if state["weekly_pnl_pct"] <= weekly_limit:
        reason = (f"WOCHEN-DRAWDOWN-STOP: {state['weekly_pnl_pct']:.1f}% "
                  f"(Limit: {weekly_limit}%, Verlust: ${state['weekly_pnl_usd']:,.2f})")
        log.warning(f"  {reason}")
        # Pause bis naechsten Montag
        days_to_monday = (7 - datetime.now().weekday()) % 7 or 7
        next_monday = (datetime.now() + timedelta(days=days_to_monday)).replace(hour=9, minute=0, second=0)
        state["paused_until"] = next_monday.isoformat()
        state["pause_reason"] = reason
        _save_risk_state(state)
        return False, reason

    return True, "OK"


# ============================================================
# POSITION SIZING (1-2% Risiko pro Trade)
# ============================================================

def calculate_position_size(portfolio_value, stop_loss_pct, config=None):
    """Berechne maximale Positionsgroesse basierend auf Risiko pro Trade.

    Formel: Position = (Portfolio * Risiko%) / |Stop-Loss%|
    Beispiel: $100k * 2% / 3% = $666 max Verlust -> Position = $2,222
    """
    if config is None:
        config = load_config()
    risk_cfg = config.get("risk_management", {})

    risk_per_trade_pct = risk_cfg.get("risk_per_trade_pct", 2.0)
    max_single_trade = config.get("demo_trading", {}).get("max_single_trade_usd", 5000)

    if stop_loss_pct == 0:
        stop_loss_pct = -3  # Fallback

    max_risk_usd = portfolio_value * (risk_per_trade_pct / 100)
    position_size = max_risk_usd / (abs(stop_loss_pct) / 100)

    # Nie mehr als konfiguriertes Maximum
    position_size = min(position_size, max_single_trade)

    # Nie mehr als 10% des Portfolios in einer Position
    max_position_pct = risk_cfg.get("max_single_position_pct", 10)
    position_size = min(position_size, portfolio_value * max_position_pct / 100)

    return round(max(position_size, 0), 2)


def calculate_dynamic_position_size(portfolio_value, stop_loss_pct, signal_score, config=None):
    """Dynamische Positionsgroesse basierend auf Signal-Score.

    Hoeherer Score = groessere Position (max 150%), niedriger Score = kleiner (min 50%).
    """
    if config is None:
        config = load_config()
    base_size = calculate_position_size(portfolio_value, stop_loss_pct, config)
    reference_score = config.get("risk_management", {}).get("dynamic_sizing_reference_score", 30)
    if reference_score <= 0:
        reference_score = 30
    scale = max(0.5, min(1.5, signal_score / reference_score))
    return round(base_size * scale, 2)


def calculate_leveraged_position_size(portfolio_value, stop_loss_pct, leverage, config=None):
    """Position Sizing mit Hebel: Effektives Risiko = Verlust * Hebel."""
    if config is None:
        config = load_config()

    effective_sl = stop_loss_pct * leverage
    base_size = calculate_position_size(portfolio_value, effective_sl, config)

    # Bei Hebel kleiner handeln (Risiko steigt mit Hebel)
    return round(base_size / leverage, 2)


# ============================================================
# KORRELATIONSCHECK
# ============================================================

def check_correlation(new_instrument_class, existing_positions, config=None):
    """Pruefe ob neue Position zu stark mit bestehenden korreliert.

    Einfache Heuristik: Nicht mehr als N Positionen pro Asset-Klasse,
    und nicht mehr als M% des Portfolios in einer Klasse.
    """
    if config is None:
        config = load_config()
    risk_cfg = config.get("risk_management", {})

    max_per_class = risk_cfg.get("max_positions_per_class", 5)
    max_class_pct = risk_cfg.get("max_class_allocation_pct", 40)

    # Zaehle Positionen pro Klasse
    class_count = {}
    class_value = {}
    total_value = 0
    for pos in existing_positions:
        cls = pos.get("asset_class", "stocks")
        class_count[cls] = class_count.get(cls, 0) + 1
        val = pos.get("invested", 0)
        class_value[cls] = class_value.get(cls, 0) + val
        total_value += val

    # Pruefe Anzahl
    current_count = class_count.get(new_instrument_class, 0)
    if current_count >= max_per_class:
        return False, f"Max {max_per_class} Positionen fuer {new_instrument_class} erreicht ({current_count})"

    # Pruefe Allokation
    if total_value > 0:
        current_pct = class_value.get(new_instrument_class, 0) / total_value * 100
        if current_pct >= max_class_pct:
            return False, f"Max {max_class_pct}% Allokation fuer {new_instrument_class} erreicht ({current_pct:.1f}%)"

    return True, "OK"


def check_sector_concentration(new_sector, existing_positions, config=None):
    """Pruefe ob neue Position zu hohe Sektor-Konzentration verursacht.

    Verhindert Klumpenrisiko innerhalb einer Asset-Klasse, z.B. 8x Tech-Aktien.
    """
    if config is None:
        config = load_config()
    risk_cfg = config.get("risk_management", {})

    max_per_sector = risk_cfg.get("max_positions_per_sector", 4)
    max_sector_pct = risk_cfg.get("max_sector_allocation_pct", 35)

    if not new_sector:
        return True, "OK"

    # Zaehle Positionen und Werte pro Sektor
    sector_count = {}
    sector_value = {}
    total_value = 0
    for pos in existing_positions:
        sec = pos.get("sector", "")
        if not sec:
            continue
        sector_count[sec] = sector_count.get(sec, 0) + 1
        val = pos.get("invested", 0)
        sector_value[sec] = sector_value.get(sec, 0) + val
        total_value += val

    # Pruefe Anzahl
    current_count = sector_count.get(new_sector, 0)
    if current_count >= max_per_sector:
        return False, (f"Max {max_per_sector} Positionen im Sektor '{new_sector}' "
                       f"erreicht ({current_count})")

    # Pruefe Allokation
    if total_value > 0:
        current_pct = sector_value.get(new_sector, 0) / total_value * 100
        if current_pct >= max_sector_pct:
            return False, (f"Max {max_sector_pct}% Allokation im Sektor '{new_sector}' "
                           f"erreicht ({current_pct:.1f}%)")

    return True, "OK"


# ============================================================
# MAX OFFENE POSITIONEN
# ============================================================

def check_max_positions(current_count, config=None):
    """Pruefe ob maximale Anzahl offener Positionen erreicht."""
    if config is None:
        config = load_config()
    max_pos = config.get("risk_management", {}).get("max_open_positions", 20)
    if current_count >= max_pos:
        return False, f"Max {max_pos} offene Positionen erreicht ({current_count})"
    return True, "OK"


# ============================================================
# EXPOSURE & MARGIN MONITORING
# ============================================================

def calculate_exposure(positions):
    """Berechne effektive Marktexposure (Invested * Leverage) pro Klasse."""
    exposure = {
        "total_invested": 0,
        "total_effective": 0,
        "by_class": {},
        "by_instrument": {},
        "worst_case_loss": 0,
    }

    for pos in positions:
        invested = pos.get("invested", 0)
        leverage = pos.get("leverage", 1)
        effective = invested * leverage
        iid = str(pos.get("instrument_id", "?"))
        cls = pos.get("asset_class", "unknown")

        exposure["total_invested"] += invested
        exposure["total_effective"] += effective

        if cls not in exposure["by_class"]:
            exposure["by_class"][cls] = {"invested": 0, "effective": 0, "count": 0}
        exposure["by_class"][cls]["invested"] += invested
        exposure["by_class"][cls]["effective"] += effective
        exposure["by_class"][cls]["count"] += 1

        exposure["by_instrument"][iid] = {
            "invested": invested,
            "effective": effective,
            "leverage": leverage,
        }

        # Worst Case: 100% Verlust der investierten Summe (bei CFDs moeglich)
        exposure["worst_case_loss"] += invested

    for key in ["total_invested", "total_effective", "worst_case_loss"]:
        exposure[key] = round(exposure[key], 2)

    return exposure


def check_margin_safety(portfolio_value, positions, config=None):
    """Pruefe ob genuegend Margin-Puffer vorhanden."""
    if config is None:
        config = load_config()
    risk_cfg = config.get("risk_management", {})

    min_margin_buffer_pct = risk_cfg.get("min_margin_buffer_pct", 20)
    max_total_exposure_pct = risk_cfg.get("max_total_exposure_pct", 300)

    exposure = calculate_exposure(positions)
    effective_exposure = exposure["total_effective"]

    if portfolio_value <= 0:
        return False, "Portfolio-Wert ist 0", exposure

    exposure_pct = effective_exposure / portfolio_value * 100
    margin_used = exposure["total_invested"]
    margin_available = portfolio_value - margin_used
    margin_buffer_pct = (margin_available / portfolio_value * 100) if portfolio_value > 0 else 0

    warnings = []

    if exposure_pct > max_total_exposure_pct:
        warnings.append(f"Exposure {exposure_pct:.0f}% > Max {max_total_exposure_pct}%")

    if margin_buffer_pct < min_margin_buffer_pct:
        warnings.append(f"Margin-Puffer {margin_buffer_pct:.1f}% < Min {min_margin_buffer_pct}%")

    exposure["exposure_pct"] = round(exposure_pct, 1)
    exposure["margin_buffer_pct"] = round(margin_buffer_pct, 1)

    if warnings:
        return False, " | ".join(warnings), exposure
    return True, "OK", exposure


# ============================================================
# SLIPPAGE & TRANSAKTIONSKOSTEN
# ============================================================

# eToro-spezifische Spreads (in Prozent des Preises, Durchschnitt)
ETORO_SPREADS = {
    "stocks": 0.09,       # ~0.09% fuer US Aktien
    "etf": 0.09,
    "crypto": 1.0,        # ~1% fuer Crypto
    "forex": 0.01,        # ~1 Pip
    "commodities": 0.05,
    "indices": 0.04,
}


def estimate_transaction_costs(amount_usd, asset_class, is_leveraged=False):
    """Schaetze Transaktionskosten (Spread + Overnight fuer CFDs)."""
    spread_pct = ETORO_SPREADS.get(asset_class, 0.1)
    spread_cost = amount_usd * spread_pct / 100

    # eToro Overnight-Gebuehren fuer CFDs (ca. 0.01-0.05% pro Nacht)
    overnight_daily = 0
    if is_leveraged:
        overnight_daily = amount_usd * 0.02 / 100  # ~0.02% pro Nacht

    return {
        "spread_cost": round(spread_cost, 2),
        "overnight_daily": round(overnight_daily, 2),
        "overnight_weekly": round(overnight_daily * 5, 2),  # Mo-Fr
        "overnight_weekend_3x": round(overnight_daily * 3, 2),  # Freitag = 3x
        "total_entry_cost": round(spread_cost, 2),
    }


def adjust_profit_target_for_costs(take_profit_pct, asset_class, leverage=1):
    """Passe Take-Profit-Ziel an um Transaktionskosten zu decken."""
    spread_pct = ETORO_SPREADS.get(asset_class, 0.1)
    # Roundtrip = 2x Spread (rein + raus)
    roundtrip_cost = spread_pct * 2
    # Bei Hebel: Kosten steigen relativ zum investierten Betrag
    effective_cost = roundtrip_cost * leverage
    min_tp = effective_cost * 2  # Mindestens doppelte Kosten als Gewinn
    return max(take_profit_pct, min_tp)


# ============================================================
# OVERNIGHT / WEEKEND RISIKO
# ============================================================

def check_overnight_risk(positions, config=None):
    """Identifiziere Positionen die vor Marktschluss geschlossen werden sollten."""
    if config is None:
        config = load_config()
    risk_cfg = config.get("risk_management", {})

    close_stocks_overnight = risk_cfg.get("close_stocks_overnight", False)
    close_leveraged_overnight = risk_cfg.get("close_leveraged_overnight", True)
    exempt_etfs = risk_cfg.get("exempt_etfs_from_overnight_close", True)

    to_close = []
    for pos in positions:
        cls = pos.get("asset_class", "stocks")
        leverage = pos.get("leverage", 1)

        if cls == "crypto":
            continue  # Crypto handelt 24/7

        if close_leveraged_overnight and leverage > 1:
            to_close.append(pos)
        elif close_stocks_overnight and cls == "stocks":
            to_close.append(pos)
        elif close_stocks_overnight and cls == "etf" and not exempt_etfs:
            to_close.append(pos)

    return to_close


# ============================================================
# FREITAG WEEKEND-GEBUEHREN CHECK
# ============================================================

def check_weekend_fee_impact(positions, config=None):
    """Pruefe ob Weekend-Gebuehren (3x Overnight) die erwartete Rendite uebersteigen."""
    if config is None:
        config = load_config()

    to_close = []
    for pos in positions:
        invested = pos.get("invested", 0)
        pnl_pct = pos.get("pnl_pct", 0)
        leverage = pos.get("leverage", 1)
        cls = pos.get("asset_class", "stocks")

        if leverage <= 1:
            continue  # Keine Overnight-Gebuehren ohne Hebel

        costs = estimate_transaction_costs(invested, cls, is_leveraged=True)
        weekend_cost_pct = costs["overnight_weekend_3x"] / invested * 100 if invested > 0 else 0

        # Schliessen wenn Gebuehren > verbleibender Gewinn oder Position im Minus
        if pnl_pct < weekend_cost_pct or pnl_pct < 0:
            to_close.append({
                **pos,
                "reason": f"Weekend-Gebuehren ({weekend_cost_pct:.2f}%) > Rendite ({pnl_pct:.1f}%)"
            })

    return to_close


# ============================================================
# EMERGENCY: ALLE POSITIONEN SCHLIESSEN
# ============================================================

def emergency_close_all(client, reason="Emergency Kill Switch"):
    """Schliesse ALLE offenen Positionen sofort."""
    from app.etoro_client import EtoroClient

    log.warning(f"!!! EMERGENCY CLOSE ALL: {reason} !!!")

    portfolio = client.get_portfolio()
    if not portfolio:
        log.error("Portfolio nicht verfuegbar fuer Emergency Close")
        return {"closed": 0, "failed": 0, "error": "Portfolio nicht verfuegbar"}

    positions = portfolio.get("positions", [])
    closed = 0
    failed = 0

    for pos in positions:
        p = EtoroClient.parse_position(pos)
        if p["invested"] > 0:
            result = client.close_position(p["position_id"])
            if result:
                closed += 1
                log.info(f"  Geschlossen: Position {p['position_id']} "
                         f"(Instrument {p['instrument_id']}, P/L: {p['pnl_pct']:+.1f}%)")
            else:
                failed += 1
                log.error(f"  FEHLER: Position {p['position_id']} konnte nicht geschlossen werden")

    # Trading deaktivieren
    from app.config_manager import get_data_path
    flag_path = get_data_path("trading_enabled.flag")
    flag_path.write_text("false")

    # Pause setzen
    state = _load_risk_state()
    state["paused_until"] = (datetime.now() + timedelta(hours=24)).isoformat()
    state["pause_reason"] = reason
    _save_risk_state(state)

    log.warning(f"  Emergency Close: {closed} geschlossen, {failed} fehlgeschlagen, Trading deaktiviert")
    return {"closed": closed, "failed": failed, "reason": reason}


# ============================================================
# DELEVERAGING (bei Margin-Engpass)
# ============================================================

def auto_deleverage(client, positions, portfolio_value, config=None):
    """Schliesse die groessten gehebelten Positionen bei Margin-Engpass."""
    if config is None:
        config = load_config()
    risk_cfg = config.get("risk_management", {})
    emergency_margin_pct = risk_cfg.get("emergency_margin_threshold_pct", 30)

    from app.etoro_client import EtoroClient

    total_invested = sum(p.get("invested", 0) for p in positions)
    if portfolio_value <= 0:
        return []

    margin_pct = (portfolio_value - total_invested) / portfolio_value * 100
    if margin_pct >= emergency_margin_pct:
        return []

    log.warning(f"  AUTO-DELEVERAGE: Margin {margin_pct:.1f}% < {emergency_margin_pct}%")

    # Sortiere nach effektiver Exposure (invested * leverage), groesste zuerst
    leveraged = [p for p in positions if p.get("leverage", 1) > 1]
    leveraged.sort(key=lambda p: p.get("invested", 0) * p.get("leverage", 1), reverse=True)

    closed = []
    for pos in leveraged:
        result = client.close_position(pos.get("position_id"))
        if result:
            closed.append(pos)
            log.info(f"  Deleverage: Geschlossen {pos.get('instrument_id')} "
                     f"(${pos.get('invested', 0):,.0f} x{pos.get('leverage', 1)})")

        # Recalculate margin
        total_invested -= pos.get("invested", 0)
        margin_pct = (portfolio_value - total_invested) / portfolio_value * 100
        if margin_pct >= emergency_margin_pct:
            break

    return closed


# ============================================================
# PRE-TRADE VALIDATION (alles zusammen)
# ============================================================

def validate_trade(portfolio_value, amount_usd, leverage, asset_class,
                   existing_positions, stop_loss_pct, config=None, sector=None):
    """Zentrale Pre-Trade-Validierung. Gibt (allowed, reasons) zurueck."""
    if config is None:
        config = load_config()

    reasons = []

    # 1. Drawdown-Check
    dd_ok, dd_reason = check_drawdown_limits()
    if not dd_ok:
        reasons.append(dd_reason)

    # 2. Position Sizing
    max_size = calculate_leveraged_position_size(
        portfolio_value, stop_loss_pct, leverage, config)
    if amount_usd > max_size:
        reasons.append(f"Position ${amount_usd:,.0f} > Max ${max_size:,.0f} (Risk-Sizing)")

    # 3. Max Positionen
    pos_ok, pos_reason = check_max_positions(len(existing_positions), config)
    if not pos_ok:
        reasons.append(pos_reason)

    # 4. Korrelation (Asset-Klasse)
    corr_ok, corr_reason = check_correlation(asset_class, existing_positions, config)
    if not corr_ok:
        reasons.append(corr_reason)

    # 5. Sektor-Konzentration
    if sector:
        sec_ok, sec_reason = check_sector_concentration(sector, existing_positions, config)
        if not sec_ok:
            reasons.append(sec_reason)

    # 6. Margin Safety
    margin_ok, margin_reason, _ = check_margin_safety(
        portfolio_value, existing_positions, config)
    if not margin_ok:
        reasons.append(margin_reason)

    allowed = len(reasons) == 0
    if not allowed:
        log.warning(f"  Trade ABGELEHNT: {' | '.join(reasons)}")

    return allowed, reasons


def check_recovery_mode(config=None):
    """Pruefe ob Recovery Mode aktiv ist (Weekly Drawdown zwischen Threshold und Kill-Switch).

    Im Recovery Mode:
    - Positionsgroessen halbiert
    - Min Score erhoeht
    - Kein Leverage

    Returns:
        (active: bool, restrictions: dict)
    """
    if config is None:
        config = load_config()
    risk_cfg = config.get("risk_management", {})
    state = _load_risk_state()

    recovery_threshold = risk_cfg.get("recovery_mode_threshold_pct", -3)
    weekly_limit = risk_cfg.get("weekly_drawdown_stop_pct", -10)
    weekly_pnl = state.get("weekly_pnl_pct", 0)

    if recovery_threshold < weekly_pnl or weekly_pnl <= weekly_limit:
        return False, {}

    # Recovery Mode aktiv
    restrictions = {
        "position_size_multiplier": 0.5,
        "min_score": risk_cfg.get("recovery_mode_min_score", 30),
        "max_leverage": risk_cfg.get("recovery_mode_max_leverage", 1),
        "weekly_pnl_pct": weekly_pnl,
        "reason": f"RECOVERY MODE: Weekly P&L {weekly_pnl:.1f}% (Threshold: {recovery_threshold}%)",
    }
    log.warning(f"  {restrictions['reason']}")
    return True, restrictions


def get_risk_summary():
    """Hole aktuelle Risk-State Zusammenfassung fuer Dashboard/API."""
    state = _load_risk_state()
    return {
        "daily_pnl_pct": state.get("daily_pnl_pct", 0),
        "daily_pnl_usd": state.get("daily_pnl_usd", 0),
        "weekly_pnl_pct": state.get("weekly_pnl_pct", 0),
        "weekly_pnl_usd": state.get("weekly_pnl_usd", 0),
        "paused": state.get("paused_until") is not None,
        "paused_until": state.get("paused_until"),
        "pause_reason": state.get("pause_reason", ""),
        "consecutive_losses": state.get("consecutive_losses", 0),
    }
