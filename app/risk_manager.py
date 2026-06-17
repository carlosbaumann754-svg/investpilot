"""
InvestPilot - Risk Manager
Zentrales Risikomanagement: Position Sizing, Drawdown-Stops,
Korrelationschecks, Margin-Ueberwachung, Exposure-Limits.
"""

import logging
import statistics
from datetime import datetime, timedelta, timezone

from app.config_manager import load_config, load_json, save_json


# v37h+3 (Sprint-Tag-9, 19.05.2026): Audit-Coverage-Marker.
# health_audit.check_module_coverage liest diesen Block via AST und
# generiert automatisch Cross-Validation-Checks.
AUDIT_METADATA = {
    "purpose": "Risk-Management: Position-Sizing, Concentration-Limits (Symbol+Sector), Cash-Reserve, Drawdown-Stops, Kelly-Auto-Apply",
    "config_section": "risk_management",
    "state_files": ["risk_state.json"],
    "self_tests": ["tc_symbol_concentration_sane", "tc_cash_reserve_sane"],
    "scheduler_hooks": [],
    "health_check": "audit_health_check",
    "added_in": "v37 (pre-Sprint-Cycle), R-A10 Symbol-Concentration 19.05.2026",
}


def audit_health_check() -> dict:
    """Operational-Health-Check fuer health_audit.check_module_coverage.

    Returns {'healthy': bool, 'detail': str}. Healthy = Risk-State-File
    existiert + sane + heutige Concentration-Blocks unter 20 (>20 = Scanner-Spam).
    """
    try:
        state = load_json("risk_state.json") or {}
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        blocks_today = sum((state.get("symbol_concentration_blocks", {})
                                 .get(today, {}) or {}).values())
        healthy = blocks_today < 20
        return {
            "healthy": healthy,
            "detail": (f"risk_state OK; {blocks_today} concentration-blocks heute"
                       + ("" if healthy else " (>20 = Scanner-Spam-Pattern)")),
        }
    except Exception as e:
        return {"healthy": False, "detail": f"audit_health_check error: {e}"}

log = logging.getLogger("RiskManager")

RISK_STATE_FILE = "risk_state.json"
CASH_DCA_STATE_FILE = "cash_dca_state.json"


# v37h+2 (R-A1, 15.05.2026): Timezone-aware datetime fuer paused_until.
# VPS loggt UTC, Carlos lebt in CEST. Vorher: datetime.now() ohne TZ
# -> isoformat() schreibt naive ISO -> bei State-Sync zwischen VPS (UTC)
# und Local-Box (CEST) verschob sich paused_until um 2h. Daily-Drawdown-
# Stop konnte 2h zu frueh aufheben.
# Loesung: ZoneInfo('Europe/Zurich') konsistent ueberall. Caller die
# datetime.now() mit paused_until vergleichen wollen muessen _now_local()
# nutzen damit Mixed-Naive-Aware vermieden wird.
try:
    from zoneinfo import ZoneInfo
    _LOCAL_TZ = ZoneInfo("Europe/Zurich")
except ImportError:
    # Fallback fuer aeltere Python-Versionen ohne zoneinfo (sollte nicht
    # vorkommen, Python>=3.9 hat zoneinfo). Defensiv UTC.
    _LOCAL_TZ = timezone.utc


def _now_local():
    """Returns aktuelle Zeit in Europe/Zurich-TZ (CEST/CET).

    Konsistent ueberall fuer paused_until-Berechnungen + Vergleiche.
    isoformat() schreibt mit Offset (z.B. '+02:00'), fromisoformat
    liest das korrekt zurueck.
    """
    return datetime.now(_LOCAL_TZ)


def _parse_paused_until(ts):
    """Parse paused_until-ISO-String tolerant.

    Behandelt sowohl alte (naive) als auch neue (tz-aware) Eintraege.
    Returns tz-aware datetime in Local-TZ oder None bei Parse-Fail.
    """
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        # Alte naive Eintraege als Local-TZ interpretieren (= bisheriges Verhalten)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_LOCAL_TZ)
        return dt.astimezone(_LOCAL_TZ)
    except (ValueError, TypeError):
        return None


# ============================================================
# v15: PORTFOLIO-PROZENT-BASIERTE CAPS (Live-Gang-Prep, Auto-Skalierung)
# ============================================================
# Ersatz fuer feste max_single_trade_usd=5000 (die bei 2000 CHF Startkapital
# gar nicht greift). Siehe CLAUDE.md "Live-Gang Strategie".

def resolve_max_single_trade_usd(portfolio_value, config):
    """Berechnet effektives Single-Trade-USD-Cap.

    Prioritaet:
    1. Prozent-basiert (neu): `demo_trading.max_single_trade_pct_of_portfolio`
       * portfolio_value, mit Floor und optionalem Hard-Cap.
    2. Fix-USD (legacy): `demo_trading.max_single_trade_usd` — wird ignoriert
       wenn prozent-basiert gesetzt ist.

    Floor: `demo_trading.max_single_trade_usd_floor` (default 50)
       -> Min-Order-Puffer fuer eToro.
    Hard-Cap: `demo_trading.max_single_trade_usd_hard_cap` (default None)
       -> absolute Obergrenze, None = kein Cap.
    """
    dt = (config or {}).get("demo_trading", {}) or {}
    pct = dt.get("max_single_trade_pct_of_portfolio")
    if pct is not None and portfolio_value > 0:
        try:
            floor = float(dt.get("max_single_trade_usd_floor", 50))
            cap_raw = dt.get("max_single_trade_usd_hard_cap")
            cap = float(cap_raw) if cap_raw is not None else None
            value = max(float(pct) * float(portfolio_value), floor)
            if cap is not None:
                value = min(value, cap)
            return round(value, 2)
        except (TypeError, ValueError):
            log.warning("resolve_max_single_trade_usd: pct-Config ungueltig, fallback auf fix")

    # Legacy-Fallback
    legacy = dt.get("max_single_trade_usd", 5000)
    try:
        return float(legacy)
    except (TypeError, ValueError):
        return 5000.0


def resolve_max_positions(portfolio_value, config):
    """Gibt die maximal erlaubte Anzahl paralleler Positionen zurueck,
    basierend auf Portfolio-Wert (Tier-Map).

    Config:
      portfolio_sizing.max_positions_by_capital = {"3000":6, "10000":10, ...}
      Die Keys sind AUFSTEIGEND sortierte Portfolio-Schwellen (USD).
      Returnt den Value des ERSTEN Keys >= portfolio_value.

    Fallback: `demo_trading.max_positions` (legacy) -> 10
    """
    ps = (config or {}).get("portfolio_sizing", {}) or {}
    tiers = ps.get("max_positions_by_capital")
    if tiers:
        try:
            # Sort keys numerisch
            sorted_tiers = sorted(((float(k), int(v)) for k, v in tiers.items()),
                                  key=lambda x: x[0])
            for threshold, max_pos in sorted_tiers:
                if portfolio_value <= threshold:
                    return max_pos
            # Ueber allen Schwellen -> letztes Limit
            return sorted_tiers[-1][1]
        except (TypeError, ValueError) as e:
            log.warning(f"resolve_max_positions: Tier-Map ungueltig ({e}), fallback legacy")

    # Legacy-Fallback
    dt = (config or {}).get("demo_trading", {}) or {}
    return int(dt.get("max_positions", 10))


# ============================================================
# v15: CASH-DEPOSIT-DCA (Staffel bei neuen Einzahlungen)
# ============================================================

def detect_cash_deposit(current_cash, config, currency=None):
    """Detect monatliche Einzahlung und initiiere DCA-Staffel.

    Liest den letzten bekannten Cash-Stand aus cash_dca_state.json. Wenn
    der neue Stand > (alter Stand + min_new_cash_trigger_usd), wird ein
    DCA-Plan aktiviert: der neue Cash wird ueber N Scheduler-Zyklen
    verteilt deployed.

    v37cd: Trade-Settlement-aware. Cash-Increase nach SELL/CLOSE ist KEINE
    Einzahlung. Subtrahiere Sell-Erloese der letzten N Stunden vom Delta.
    Loest Audit-Finding F2: nach F1-Fix (credit=TotalCashValue) sprang
    Cash nach jedem SELL um Verkaufserlös und triggerte falschen DCA-Plan.

    Returns: dict mit
      - `dca_active` (bool)
      - `remaining_budget_usd` (float) — verfuegbar im aktuellen Zyklus
      - `remaining_cycles` (int)
      - `plan_created_at` (iso)
    """
    dh = (config or {}).get("deposit_handling", {}) or {}
    if not dh.get("dca_on_new_cash", False):
        return {"dca_active": False, "remaining_budget_usd": current_cash,
                "remaining_cycles": 0}

    min_trigger = float(dh.get("min_new_cash_trigger_usd", 500))
    spread_cycles = int(dh.get("dca_spread_cycles", 5))

    state = load_json(CASH_DCA_STATE_FILE) or {}
    prev_cash = float(state.get("last_seen_cash_usd", current_cash))
    active_plan = state.get("active_plan")

    # v37h+2 (R-A2, 15.05.2026): Currency-Mismatch-Detection.
    # current_cash kann je nach IBKR-Reporting-Modus mal USD-Wert, mal
    # BASE-Wert (CHF) sein (siehe Q3-13 _get_account_value-Praeferenzen).
    # Wenn die Currency zwischen zwei Cycles wechselt, ist `delta = current_cash
    # - prev_cash` keine echte Einzahlung sondern ein FX-Reval — Phantom-DCA-
    # Plan. Vorbeugung: pruefe currency-Hint aus Caller (Default 'USD' fuer
    # Backward-Compat). Bei Mismatch: State-Reset, kein DCA-Plan.
    saved_currency = state.get("last_seen_currency")
    if currency and saved_currency and saved_currency != currency:
        log.warning(
            "Cash-DCA Currency-Mismatch erkannt: gespeichert=%s aktuell=%s. "
            "State-Reset (kein Phantom-DCA aus FX-Reval). Naechster Cycle "
            "startet mit aktuellem Cash als Baseline.",
            saved_currency, currency,
        )
        state = {
            "last_seen_cash_usd": current_cash,
            "last_seen_currency": currency,
            "last_seen_at": _now_local().isoformat(),
            "active_plan": None,
        }
        try:
            save_json(CASH_DCA_STATE_FILE, state)
        except Exception as e:
            log.warning(f"Cash-DCA state save fehlgeschlagen: {e}", exc_info=True)
        return {"dca_active": False, "remaining_budget_usd": current_cash,
                "remaining_cycles": 0}

    # v37cd: Trade-Settlement-Adjustment.
    # Schaue letzten Cycle (~6h Worst-Case Cron-Drift) und summiere SELL/
    # CLOSE-Notional. Diese Cash-Increases sind keine Einzahlungen.
    sell_proceeds_recent = 0.0
    try:
        trade_hist = load_json("trade_history.json") or []
        last_seen_at = state.get("last_seen_at")
        from datetime import datetime as _dt, timedelta as _td
        cutoff = _dt.now() - _td(hours=6)
        if last_seen_at:
            try:
                cutoff = _dt.fromisoformat(last_seen_at)
            except Exception:
                pass
        for t in trade_hist[-50:]:  # letzte 50 reicht
            try:
                ts = _dt.fromisoformat(t.get("timestamp", ""))
            except Exception:
                continue
            if ts < cutoff:
                continue
            action = t.get("action", "")
            if action == "BUY" or action == "SCANNER_BUY":
                continue  # BUYs reduzieren Cash, nicht relevant
            # CLOSE/SELL/STOP_LOSS_CLOSE/TAKE_PROFIT_CLOSE/EARNINGS_BLACKOUT_CLOSE/
            # MANUAL_SELL/TIME_STOP_CLOSE etc. erhoehen Cash
            if "CLOSE" in action or action == "SELL" or action == "MANUAL_SELL":
                # Erloes ~ avg_fill_price * qty. Fallback invested+pnl wenn fill fehlt.
                fill = float(t.get("avg_fill_price", 0) or 0)
                qty = float(t.get("qty", 0) or 0)
                proceeds = fill * qty if (fill and qty) else 0
                if proceeds <= 0:
                    invested = float(t.get("invested", 0) or 0)
                    pnl = float(t.get("pnl_usd", 0) or 0)
                    proceeds = invested + pnl
                sell_proceeds_recent += max(proceeds, 0)
    except Exception as e:
        log.debug(f"detect_cash_deposit: Trade-Settlement-Adjustment fehlgeschlagen: {e}")

    # Delta = Cash-Aenderung MINUS bekannte Sell-Erloese
    raw_delta = current_cash - prev_cash
    delta = raw_delta - sell_proceeds_recent
    if sell_proceeds_recent > 0:
        log.debug(f"  DCA-Delta-Adjust: raw=${raw_delta:,.2f} - "
                  f"sells=${sell_proceeds_recent:,.2f} = ${delta:,.2f}")

    # Neuer Cash-Anstieg erkannt?
    if delta >= min_trigger and (not active_plan or active_plan.get("consumed_usd", 0) >= active_plan.get("total_deposit_usd", 0)):
        # Frischer DCA-Plan
        active_plan = {
            "total_deposit_usd": delta,
            "consumed_usd": 0.0,
            "remaining_cycles": spread_cycles,
            "per_cycle_usd": delta / spread_cycles,
            "created_at": datetime.now().isoformat(),
        }
        log.info(
            f"  Cash-DCA: Einzahlung +${delta:,.2f} detected -> Staffel ueber "
            f"{spread_cycles} Zyklen (${active_plan['per_cycle_usd']:,.2f}/Zyklus)"
        )

    state["last_seen_cash_usd"] = current_cash
    state["last_seen_at"] = datetime.now().isoformat()
    state["active_plan"] = active_plan
    # v37h+2 (R-A2): Currency-Hint persistieren fuer Mismatch-Detection
    if currency:
        state["last_seen_currency"] = currency
    try:
        save_json(CASH_DCA_STATE_FILE, state)
    except Exception as e:
        log.warning(f"Cash-DCA state save fehlgeschlagen: {e}", exc_info=True)

    if not active_plan or active_plan.get("remaining_cycles", 0) <= 0:
        return {"dca_active": False, "remaining_budget_usd": current_cash,
                "remaining_cycles": 0}

    # Budget im aktuellen Zyklus: per_cycle_usd + bereits bestehender Cash-Pool
    # minus bereits konsumierten Betrag. Konservativ: nur per_cycle_usd freigeben.
    per_cycle = float(active_plan.get("per_cycle_usd", 0))
    # Aber mindestens soviel wie "alter Cash-Stand vor Einzahlung" (der war
    # schon vollstaendig deployable, den darf die DCA-Logik nicht blockieren).
    pre_deposit_cash = current_cash - (active_plan["total_deposit_usd"] - active_plan.get("consumed_usd", 0))
    pre_deposit_cash = max(pre_deposit_cash, 0)
    available = pre_deposit_cash + per_cycle

    return {
        "dca_active": True,
        "remaining_budget_usd": round(min(available, current_cash), 2),
        "remaining_cycles": int(active_plan.get("remaining_cycles", 0)),
        "plan_created_at": active_plan.get("created_at"),
        "per_cycle_usd": round(per_cycle, 2),
    }


def consume_dca_budget(spent_usd):
    """Nach einem Zyklus: Markiert den ausgegebenen Betrag als konsumiert
    und dekrementiert remaining_cycles."""
    state = load_json(CASH_DCA_STATE_FILE) or {}
    plan = state.get("active_plan")
    if not plan:
        return
    plan["consumed_usd"] = round(float(plan.get("consumed_usd", 0)) + float(spent_usd), 2)
    plan["remaining_cycles"] = max(0, int(plan.get("remaining_cycles", 0)) - 1)
    plan["last_consumed_at"] = datetime.now().isoformat()
    state["active_plan"] = plan
    try:
        save_json(CASH_DCA_STATE_FILE, state)
    except Exception as e:
        log.warning(f"Cash-DCA consume save fehlgeschlagen: {e}", exc_info=True)


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

def update_portfolio_tracking(portfolio_value, account_key=None):
    """Aktualisiere taegliche/woechentliche P/L Tracking.

    R-B11/C4 (07.06.2026): account_key (z.B. Base-Currency oder Account-Nr).
    Wechselt er (Cutover Paper->Live, USD->CHF-Base), wird das Drawdown-Tracking
    RE-BASELINED statt einen kuenstlichen FX-Sprung (~-12%) als Drawdown zu
    registrieren — der haette am Cutover-Tag sofort einen Drawdown-Stop ausgeloest.
    """
    state = _load_risk_state()
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    week_start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")

    # C4: Account-/Currency-Wechsel erkennen -> Re-Baseline (Diskontinuitaet,
    # kein PnL ueber den Sprung hinweg).
    rebaseline = False
    if account_key is not None and state.get("account_key") not in (None, account_key):
        rebaseline = True
        log.warning(
            f"  Risk: Account/Currency-Wechsel ({state.get('account_key')} -> "
            f"{account_key}) -> Drawdown-Tracking re-baselined (C4)")
    if account_key is not None:
        state["account_key"] = account_key

    # Tagesreset (auch bei Re-Baseline)
    if state["last_daily_reset"] != today or rebaseline:
        state["daily_start_value"] = portfolio_value
        state["daily_pnl_usd"] = 0
        state["daily_pnl_pct"] = 0
        state["last_daily_reset"] = today
        state["daily_trades"] = 0
        log.info(f"  Risk: Tagesstart-Wert = ${portfolio_value:,.2f}")

    # Wochenreset (auch bei Re-Baseline)
    if state["last_weekly_reset"] != week_start or rebaseline:
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
    # v37h+2 (R-A1): tz-aware Vergleich via _now_local() + _parse_paused_until.
    # Verhindert CEST/UTC-Drift wenn State zwischen VPS und Local synct.
    if state.get("paused_until"):
        pause_end = _parse_paused_until(state["paused_until"])
        if pause_end is None:
            # Unparseable -> Pause zuruecksetzen (defensiv, bisheriges Verhalten)
            state["paused_until"] = None
            _save_risk_state(state)
        elif _now_local() < pause_end:
            return False, f"Bot pausiert bis {state['paused_until']}: {state.get('pause_reason', '')}"
        else:
            state["paused_until"] = None
            state["pause_reason"] = ""
            _save_risk_state(state)

    # v36f: None-Safe — wenn Baseline frisch geresetted wurde (z.B. nach
    # Broker-Migration, IBKR-Cutover) sind die *_pnl_pct Felder None bis
    # zum naechsten Cycle-Update. Treat None als 0% (kein Drawdown).
    daily_pct = state.get("daily_pnl_pct")
    daily_usd = state.get("daily_pnl_usd") or 0
    if daily_pct is not None and daily_pct <= daily_limit:
        reason = (f"TAGES-DRAWDOWN-STOP: {daily_pct:.1f}% "
                  f"(Limit: {daily_limit}%, Verlust: ${daily_usd:,.2f})")
        log.warning(f"  {reason}")
        # Pause bis naechsten Tag 09:00 (Local-Time CEST/CET)
        # v37h+2 (R-A1): _now_local() statt datetime.now() -> isoformat
        # schreibt mit TZ-Offset -> kein UTC/CEST-Drift bei State-Sync.
        tomorrow = _now_local().replace(
            hour=9, minute=0, second=0, microsecond=0) + timedelta(days=1)
        state["paused_until"] = tomorrow.isoformat()
        state["pause_reason"] = reason
        _save_risk_state(state)
        return False, reason

    # Wochen-Drawdown (None-safe wie Daily)
    weekly_pct = state.get("weekly_pnl_pct")
    weekly_usd = state.get("weekly_pnl_usd") or 0
    if weekly_pct is not None and weekly_pct <= weekly_limit:
        reason = (f"WOCHEN-DRAWDOWN-STOP: {weekly_pct:.1f}% "
                  f"(Limit: {weekly_limit}%, Verlust: ${weekly_usd:,.2f})")
        log.warning(f"  {reason}")
        # Pause bis naechsten Montag (Local-Time)
        # v37h+2 (R-A1): _now_local() konsistent
        now = _now_local()
        days_to_monday = (7 - now.weekday()) % 7 or 7
        next_monday = (now + timedelta(days=days_to_monday)).replace(
            hour=9, minute=0, second=0, microsecond=0)
        state["paused_until"] = next_monday.isoformat()
        state["pause_reason"] = reason
        _save_risk_state(state)
        return False, reason

    return True, "OK"


# ============================================================
# POSITION SIZING (1-2% Risiko pro Trade)
# ============================================================

def _get_kelly_recommendation(config):
    """v37h+2 (9B, 17.05.2026): Liest Kelly-Sweep-Empfehlung aus
    kelly_sweep_results.json wenn aktiv + verlaesslich.

    Returns:
        (kelly_fraction, kelly_max_position_pct) oder (None, None) wenn
        Empfehlung nicht verfuegbar / nicht trustworthy / Feature aus.

    Auto-Apply ist standardmaessig AUS (`risk_management.kelly_auto_apply: false`)
    bis Carlos das explizit aktiviert. Empfehlung wird nur dann konsumiert.
    """
    risk_cfg = (config or {}).get("risk_management", {}) if config else {}
    if not risk_cfg.get("kelly_auto_apply", False):
        return None, None

    try:
        ks = load_json("kelly_sweep_results.json") or {}
    except Exception:
        return None, None

    rec = ks.get("recommended") or ks.get("recommendation") or {}
    if not isinstance(rec, dict):
        return None, None

    # Trust-Check: braucht min_trades (Default 30) damit Statistik valide
    n_trades = rec.get("n_trades") or ks.get("n_trades") or 0
    min_trades_trust = risk_cfg.get("kelly_min_trades_trust", 30)
    if n_trades < min_trades_trust:
        log.info(
            f"Kelly-Auto-Apply: Empfehlung n_trades={n_trades} < "
            f"min_trust {min_trades_trust} — ignoriert (statistik nicht valide)"
        )
        return None, None

    # Kelly-Fraction safety-clamping (Half-Kelly als Default-Konservativismus)
    raw_fraction = rec.get("kelly_fraction") or rec.get("fraction") or 0
    try:
        raw_fraction = float(raw_fraction)
    except (TypeError, ValueError):
        return None, None
    # Hard-Cap: nicht mehr als 25% Half-Kelly (= ueber 50% Full-Kelly extrem
    # aggressiv, wird nie zugelassen)
    kelly_fraction = max(0.0, min(0.25, raw_fraction * 0.5))  # Half-Kelly

    # Optional: Kelly-spezifischer max-position-pct-override
    raw_max_pos = rec.get("max_position_pct")
    try:
        kelly_max_pos_pct = float(raw_max_pos) if raw_max_pos is not None else None
    except (TypeError, ValueError):
        kelly_max_pos_pct = None
    if kelly_max_pos_pct is not None:
        kelly_max_pos_pct = max(1.0, min(15.0, kelly_max_pos_pct))  # Safety-Clamp 1-15%

    return kelly_fraction, kelly_max_pos_pct


def calculate_position_size(portfolio_value, stop_loss_pct, config=None):
    """Berechne maximale Positionsgroesse basierend auf Risiko pro Trade.

    Formel: Position = (Portfolio * Risiko%) / |Stop-Loss%|
    Beispiel: $100k * 2% / 3% = $666 max Verlust -> Position = $2,222

    v37h+2 (9B, 17.05.2026): Optional Kelly-Auto-Apply. Wenn config.
    risk_management.kelly_auto_apply=True UND kelly_sweep_results.json
    enthaelt eine trustworthy Empfehlung, wird die Standard-Risk-Per-Trade-Berechnung
    durch Half-Kelly-Fraction ueberschrieben (mit Hard-Cap 25%% Half-Kelly).
    Default: AUS (Backward-Compat, Kelly nur Analytics).
    """
    if config is None:
        config = load_config()
    risk_cfg = config.get("risk_management", {})

    risk_per_trade_pct = risk_cfg.get("risk_per_trade_pct", 2.0)
    # v15: Prozent-basierter Cap statt fix-USD. Skaliert mit Portfolio-Wert.
    max_single_trade = resolve_max_single_trade_usd(portfolio_value, config)

    if stop_loss_pct == 0:
        stop_loss_pct = -3  # Fallback

    # v37h+2 (9B): Kelly-Auto-Apply (wenn enabled + trustworthy)
    kelly_fraction, kelly_max_pos_pct = _get_kelly_recommendation(config)
    if kelly_fraction is not None and kelly_fraction > 0:
        # Half-Kelly als Risiko-Per-Trade nutzen — ersetzt risk_per_trade_pct
        kelly_risk_pct = kelly_fraction * 100
        log.info(
            f"Kelly-Auto-Apply: risk_per_trade {risk_per_trade_pct}%% -> "
            f"Half-Kelly {kelly_risk_pct:.2f}%%"
        )
        risk_per_trade_pct = kelly_risk_pct

    max_risk_usd = portfolio_value * (risk_per_trade_pct / 100)
    position_size = max_risk_usd / (abs(stop_loss_pct) / 100)

    # Nie mehr als konfiguriertes Maximum
    position_size = min(position_size, max_single_trade)

    # Nie mehr als 10% des Portfolios in einer Position.
    # R-B11/R5 (07.06.2026): Kelly darf den harten Single-Position-Cap nur SENKEN,
    # NICHT ueberschreiben. Frueher ersetzte kelly_max_pos_pct den 10%-Cap (bis
    # 15%) -> Optimizer-Output hebelte den Konzentrations-Floor aus (verletzt die
    # Validation-Hierarchie: Optimizer ist Vorschlag, nicht Wahrheit).
    max_position_pct = risk_cfg.get("max_single_position_pct", 10)
    if kelly_max_pos_pct is not None:
        max_position_pct = min(max_position_pct, kelly_max_pos_pct)
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


def _kelly_stats_from_history(trade_history=None, min_trades=20):
    """Empirische Win-Rate + Avg-Win/Avg-Loss aus trade_history.json.

    Returns (winrate, avg_win_pct, avg_loss_pct, n_trades) oder None wenn
    nicht genug Daten.
    """
    if trade_history is None:
        trade_history = load_json("trade_history.json") or []

    pnls = []
    for t in trade_history:
        action = (t.get("action") or "").upper()
        if "CLOSE" not in action and "STOP_LOSS" not in action and "TAKE_PROFIT" not in action \
                and "TIME_STOP" not in action and "TRAILING_SL" not in action:
            continue
        pnl = t.get("pnl_net_pct", t.get("pnl_pct", None))
        if pnl is None:
            continue
        try:
            pnls.append(float(pnl))
        except (TypeError, ValueError):
            continue

    if len(pnls) < min_trades:
        return None

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    if not wins or not losses:
        return None
    winrate = len(wins) / len(pnls)
    avg_win = sum(wins) / len(wins)
    avg_loss = abs(sum(losses) / len(losses))
    return winrate, avg_win, avg_loss, len(pnls)


def calculate_kelly_position_size(portfolio_value, stop_loss_pct, signal_score,
                                   config=None, trade_history=None):
    """v12: Half-Kelly Position Sizing mit hartem Cap.

    Kelly: f* = (p*b - q) / b
        p = winrate, q = 1-p, b = avg_win/avg_loss
    Half-Kelly: f = 0.5 * f*
    Hard-Cap: min(f, kelly.max_fraction) — schuetzt vor Fat-Tail-Suizid.

    Fallback bei < N Trades oder Kelly <= 0: dynamic_position_size.
    """
    if config is None:
        config = load_config()

    k_cfg = config.get("kelly_sizing", {}) or {}
    if not k_cfg.get("enabled", False):
        return calculate_dynamic_position_size(
            portfolio_value, stop_loss_pct, signal_score, config)

    min_trades = int(k_cfg.get("min_trades", 20))
    max_fraction = float(k_cfg.get("max_fraction", 0.01))
    half_kelly = bool(k_cfg.get("half_kelly", True))
    min_single_usd = float(k_cfg.get("min_position_usd", 50))

    stats = _kelly_stats_from_history(trade_history, min_trades)
    if stats is None:
        log.info(f"  KELLY: zu wenig Trade-Daten, fallback auf dynamic sizing")
        return calculate_dynamic_position_size(
            portfolio_value, stop_loss_pct, signal_score, config)

    winrate, avg_win, avg_loss, n = stats
    b = avg_win / avg_loss if avg_loss > 0 else 1.0
    f_star = (winrate * b - (1 - winrate)) / b if b > 0 else 0.0
    if f_star <= 0:
        log.info(f"  KELLY: negatives Edge (f*={f_star:.3f}, "
                 f"winrate={winrate:.1%}, b={b:.2f}), fallback")
        return calculate_dynamic_position_size(
            portfolio_value, stop_loss_pct, signal_score, config)

    fraction = 0.5 * f_star if half_kelly else f_star

    # Signal-Score Modulation: leichte Skalierung basierend auf Confidence
    ref_score = config.get("risk_management", {}).get("dynamic_sizing_reference_score", 30)
    if ref_score > 0:
        score_scale = max(0.5, min(1.25, signal_score / ref_score))
        fraction *= score_scale

    # v37dt Audit#13: Hard-Cap NACH der Score-Modulation anwenden — vorher stand
    # der min(.., max_fraction) VOR der *score_scale (bis 1.25x), wodurch die
    # effektive Kelly-Fraktion den max_fraction-Cap um bis zu 25% ueberschreiten
    # konnte (Cap war kein echter Hard-Cap).
    fraction = min(fraction, max_fraction)

    position_size = portfolio_value * fraction
    # v15: Prozent-basierter Cap (skaliert automatisch mit Portfolio-Wert)
    max_single = resolve_max_single_trade_usd(portfolio_value, config)
    position_size = min(position_size, max_single)

    max_pos_pct = config.get("risk_management", {}).get("max_single_position_pct", 10)
    position_size = min(position_size, portfolio_value * max_pos_pct / 100)

    position_size = max(position_size, 0)
    log.info(f"  KELLY: wr={winrate:.0%}, b={b:.2f}, f*={f_star:.3f}, "
             f"f={fraction:.3f}, size=${position_size:,.0f} "
             f"(n={n} Trades, cap={max_fraction:.1%})")
    return round(position_size, 2)


def calculate_leveraged_position_size(portfolio_value, stop_loss_pct, leverage, config=None):
    """Position Sizing mit Hebel: Effektives Risiko = Verlust * Hebel."""
    if config is None:
        config = load_config()

    # effective_sl multipliziert den Stop-Loss bereits mit dem Hebel -> der
    # daraus berechnete base_size ist schon hebel-risiko-adjustiert (kleiner bei
    # mehr Hebel). v37dt Audit#14: die zusaetzliche Division durch leverage war
    # ein Doppelzaehler (Hebel zweimal abgezogen -> Position um Faktor `leverage`
    # zu klein). LATENT: aktuell kein Hebel aktiv (leverage=1, ÷1 wirkungslos),
    # aber falsch sobald Hebel je zugeschaltet wird.
    effective_sl = stop_loss_pct * leverage
    base_size = calculate_position_size(portfolio_value, effective_sl, config)
    return round(base_size, 2)


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


def get_portfolio_concentration_score(existing_positions, config=None):
    """Berechne Portfolio-Konzentrations-Score (0-100).

    0 = perfekt diversifiziert, 100 = alles in einem Sektor/Klasse.
    Basiert auf Herfindahl-Index der Sektor-Allokation.
    """
    if config is None:
        config = load_config()

    if not existing_positions:
        return 0

    sector_value = {}
    total_value = 0
    for pos in existing_positions:
        sec = pos.get("sector", "") or pos.get("asset_class", "unknown")
        val = pos.get("invested", 0)
        if val <= 0:
            continue
        sector_value[sec] = sector_value.get(sec, 0) + val
        total_value += val

    if total_value <= 0 or len(sector_value) == 0:
        return 0

    # Herfindahl-Index: Summe der quadrierten Anteile
    hhi = sum((v / total_value) ** 2 for v in sector_value.values())

    # Normalisieren: 1/N (perfekt verteilt) bis 1.0 (alles in einem)
    n = len(sector_value)
    if n <= 1:
        return 100

    min_hhi = 1.0 / n  # Perfekt diversifiziert
    # Score: 0 bei min_hhi, 100 bei 1.0
    score = (hhi - min_hhi) / (1.0 - min_hhi) * 100
    score = max(0, min(100, score))

    return round(score, 1)


# ============================================================
# SYMBOL-KONZENTRATION (R-A10, Sprint-Tag-9 19.05.2026)
# ============================================================
# Hard-Cap pro Symbol als Defense-in-Depth gegen Klumpenrisiko.
# Anlass: 18.05.2026 5x SCANNER_BUY auf OIL/USO (~$267k Brutto-
# Exposure) trotz vorhandenem existing_symbols-Filter in trader.py.
# Wurzel war Cache-Timing zwischen Scanner-Cycles + Filter wird nur
# einmal pre-Loop gebaut. Symbol-Konzentrations-Check ist
# unabhaengige zweite Layer die auch bei stale Cache greift.
#
# Block-Strategie (Option D): Hart blocken + Pushover NUR ab
# pushover_threshold_per_day Blocks (Default 3). Counter im
# risk_state.json mit Datum-Key (implicit daily reset).

def check_symbol_concentration(new_symbol, candidate_amount_usd,
                                existing_positions, total_portfolio_value,
                                config=None):
    """Pruefe Hard-Cap pro Symbol vor neuem Buy.

    Bidirektional Symbol-Match via expand_symbol_for_match — d.h.
    OIL-Candidate matcht IBKR-Position 'USO' und umgekehrt.

    Args:
        new_symbol: Bot-Symbol oder IBKR-Ticker des Candidates
        candidate_amount_usd: USD-Volumen der geplanten neuen Position
        existing_positions: Liste der aktuellen Positions-dicts
                            (jedes mit 'symbol' + 'invested')
        total_portfolio_value: Equity + alle invested (fuer Pct-Berechnung)
        config: optional, sonst load_config()

    Returns:
        (allowed: bool, reason: str)
    """
    if config is None:
        config = load_config()
    sc_cfg = config.get("risk_management", {}).get("symbol_concentration", {})

    if not sc_cfg.get("enabled", True):
        return True, "OK (disabled)"

    if not new_symbol:
        return True, "OK (no symbol)"

    max_positions = sc_cfg.get("max_positions_per_symbol", 1)
    max_exposure_pct = sc_cfg.get("max_exposure_per_symbol_pct", 15)

    # Bidirektional Symbol-Match
    try:
        from app.market_scanner import expand_symbol_for_match
        candidate_variants = expand_symbol_for_match(new_symbol)
    except Exception as e:
        log.debug(f"expand_symbol_for_match failed: {e}")
        candidate_variants = {new_symbol}

    # Zaehle existing Positionen mit Symbol-Match
    same_symbol_count = 0
    same_symbol_invested = 0.0
    for pos in existing_positions or []:
        pos_sym = pos.get("symbol")
        if not pos_sym:
            continue
        # Direkter Match ODER Pos-Symbol expandiert matcht
        try:
            pos_variants = expand_symbol_for_match(pos_sym)
        except Exception:
            pos_variants = {pos_sym}
        if candidate_variants & pos_variants:
            same_symbol_count += 1
            same_symbol_invested += float(pos.get("invested", 0) or 0)

    # Check 1: max_positions_per_symbol (Hard Count-Limit)
    if same_symbol_count >= max_positions:
        return False, (f"max_positions_per_symbol={max_positions} erreicht "
                       f"({same_symbol_count} bestehende Pos auf {new_symbol})")

    # Check 2: max_exposure_per_symbol_pct (Hard Pct-Limit)
    if total_portfolio_value > 0 and max_exposure_pct > 0:
        future_exposure_pct = ((same_symbol_invested + candidate_amount_usd)
                               / total_portfolio_value * 100)
        if future_exposure_pct > max_exposure_pct:
            return False, (f"max_exposure_per_symbol_pct={max_exposure_pct}% "
                           f"wuerde ueberschritten ({future_exposure_pct:.1f}% nach Buy)")

    return True, "OK"


def record_concentration_block(symbol, reason, config=None):
    """Logge einen Concentration-Block und triggere Pushover bei Threshold.

    Counter-State in risk_state.json:
      {"symbol_concentration_blocks": {"YYYY-MM-DD": {"OIL": 3, "MSFT": 1}}}

    Daily-Reset implizit durch Datum-Key. Alte Daten (>7d) werden bei
    jedem Aufruf bereinigt damit State nicht waechst.

    Pushover feuert genau EINMAL pro Symbol/Tag — bei dem Block der
    den Threshold erreicht. Folgende Blocks am selben Tag sind still.

    Args:
        symbol: Bot-Symbol oder IBKR-Ticker
        reason: Begruendung (fuer Log + Pushover-Message)
        config: optional, sonst load_config()

    Returns:
        (count_today: int, triggered_pushover: bool)
    """
    if config is None:
        config = load_config()
    sc_cfg = config.get("risk_management", {}).get("symbol_concentration", {})
    threshold = sc_cfg.get("pushover_threshold_per_day", 3)

    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    state = load_json(RISK_STATE_FILE) or {}
    all_blocks = state.get("symbol_concentration_blocks", {}) or {}

    # Cleanup alte Tage (>7d zurueck) damit State nicht waechst
    try:
        cutoff_ts = datetime.now(timezone.utc).timestamp() - 7 * 86400
        all_blocks = {
            k: v for k, v in all_blocks.items()
            if datetime.strptime(k, "%Y-%m-%d")
                       .replace(tzinfo=timezone.utc).timestamp() >= cutoff_ts
        }
    except Exception as e:
        log.debug(f"symbol_concentration_blocks cleanup skipped: {e}")

    today_blocks = all_blocks.setdefault(today_utc, {})
    today_blocks[symbol] = today_blocks.get(symbol, 0) + 1
    count = today_blocks[symbol]

    state["symbol_concentration_blocks"] = all_blocks
    save_json(RISK_STATE_FILE, state)

    # Pushover NUR exact bei Threshold (nicht jedes Mal)
    triggered_pushover = False
    if count == threshold:
        triggered_pushover = True
        try:
            from app import alerts
            msg = (f"{symbol} hat heute {count}x den Symbol-Konzentrations-"
                   f"Hard-Cap getroffen. Letzter Block: {reason}. "
                   f"Weitere Blocks heute werden still geloggt. "
                   f"Check trade-history + config wenn das nicht erwartet ist.")
            alerts.send_pushover(
                msg,
                config=config,
                title=f"CONCENTRATION-BLOCK {symbol}",
                priority=0,  # warning, kein CRITICAL
            )
        except Exception as e:
            log.warning(f"Pushover-Alert fuer CONCENTRATION-BLOCK fehlgeschlagen: {e}")

    return count, triggered_pushover


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
    """Schliesse ALLE offenen Positionen sofort + deaktiviere Trading.

    v37l (Drill-Bug-Fix): Robuste Implementierung in 3 Phasen mit jeweils
    eigener Exception-Behandlung. Die kritische Eigenschaft eines Kill-
    Switches ist DASS ER HAELT. Auch wenn die Position-Schliessung
    fehlschlaegt (Broker-Disconnect, Quote-Errors, etc.) MUSS das
    Trading-Flag auf false gehen, damit der Bot keine NEUEN Trades mehr
    aufmacht.

    Vorher: bei get_portfolio() == None lief die Funktion mit early-return
    raus, das Flag wurde nie gesetzt — Bot tradete weiter. Drill am
    29.04.2026 hat das aufgedeckt (IBKR-readonly-Client lieferte leeres
    Portfolio).

    Phasen:
      1. Trading-Flag IMMER auf false (vor allem anderen).
      2. Risk-Pause 24h (Best-Effort).
      3. Positionen schliessen (Best-Effort, broker-agnostisch via
         EtoroClient.parse_position das auch IBKR-Format versteht).
    """
    log.warning(f"!!! EMERGENCY CLOSE ALL: {reason} !!!")

    # ---- PHASE 1: Trading-Flag IMMER deaktivieren ----
    flag_set = False
    try:
        from app.config_manager import get_data_path
        flag_path = get_data_path("trading_enabled.flag")
        flag_path.write_text("false")
        flag_set = True
        log.warning("  [1/3] Trading-Flag gesetzt -> false (Bot tradet ab naechstem Cycle nicht mehr)")
    except Exception as e:
        log.error(f"  [1/3] KRITISCH: Trading-Flag konnte nicht gesetzt werden: {e}", exc_info=True)

    # ---- PHASE 2: Risk-Pause 24h ----
    pause_set = False
    try:
        state = _load_risk_state()
        # v37h+2 (R-A1): tz-aware Pause-Setzen
        state["paused_until"] = (_now_local() + timedelta(hours=24)).isoformat()
        state["pause_reason"] = reason
        _save_risk_state(state)
        pause_set = True
        log.warning("  [2/3] Risk-Pause 24h gesetzt")
    except Exception as e:
        log.error(f"  [2/3] Risk-Pause konnte nicht gesetzt werden: {e}", exc_info=True)

    # ---- PHASE 3: Offene Positionen schliessen (Best-Effort) ----
    # v37o: Robustes Position-Fetching mit 3-Stage-Fallback:
    #   1. client.get_portfolio() — Standard-Pfad (eToro + IBKR)
    #   2. IBKR-Direct: client._get_ib().positions() falls portfolio leer
    #      (ib_insync Cache war noch nicht populated)
    #   3. Force-Sync via reqPositions + retry — letzte Hoffnung
    # Drill am 29.04. zeigte: bei frisch instanziiertem IBKR-Broker ist der
    # Portfolio-Cache 2-3 Sek leer. Statt Phase 3 komplett zu skippen,
    # versuchen wir aktiv zu syncen.
    closed = 0
    failed = 0
    portfolio_error = None
    positions_to_close: list = []

    try:
        # Versuch 1: Standard get_portfolio()
        portfolio = client.get_portfolio()
        if portfolio and portfolio.get("positions"):
            positions_to_close = portfolio.get("positions") or []
            log.warning(f"  [3/3] Standard-Fetch: {len(positions_to_close)} Positionen")
        else:
            # Versuch 2: IBKR-direkt via ib.positions()
            try:
                if hasattr(client, "_get_ib"):
                    log.warning("  [3/3] Standard-Fetch leer, versuche IBKR-direkt...")
                    ib = client._get_ib()
                    raw_positions = list(ib.positions() or [])
                    if raw_positions:
                        # IBKR-Native-Format -> eToro-kompatibles dict
                        for p in raw_positions:
                            contract = getattr(p, "contract", None)
                            qty = float(getattr(p, "position", 0))
                            avg_cost = float(getattr(p, "avgCost", 0) or 0)
                            cost_basis = qty * avg_cost
                            if cost_basis > 0:
                                positions_to_close.append({
                                    "instrumentID": getattr(contract, "conId", None),
                                    "symbol": getattr(contract, "symbol", None),
                                    "amount": cost_basis,
                                    "positionID": str(getattr(contract, "conId", "")),
                                    "leverage": 1,
                                    "openRate": avg_cost,
                                    "currentRate": avg_cost,  # ohne marketPrice unbekannt
                                    "isBuy": qty > 0,
                                    "unrealizedPnL": {"pnL": 0},
                                })
                        log.warning(f"  [3/3] IBKR-direkt: {len(positions_to_close)} Positionen")
                    else:
                        # Versuch 3: Force-Sync + retry
                        log.warning("  [3/3] IBKR.positions() leer, force-sync + retry...")
                        try:
                            ib.reqPositions()
                            ib.sleep(2.0)
                            raw_positions = list(ib.positions() or [])
                            for p in raw_positions:
                                contract = getattr(p, "contract", None)
                                qty = float(getattr(p, "position", 0))
                                avg_cost = float(getattr(p, "avgCost", 0) or 0)
                                cost_basis = qty * avg_cost
                                if cost_basis > 0:
                                    positions_to_close.append({
                                        "instrumentID": getattr(contract, "conId", None),
                                        "symbol": getattr(contract, "symbol", None),
                                        "amount": cost_basis,
                                        "positionID": str(getattr(contract, "conId", "")),
                                        "leverage": 1,
                                        "openRate": avg_cost,
                                        "currentRate": avg_cost,
                                        "isBuy": qty > 0,
                                        "unrealizedPnL": {"pnL": 0},
                                    })
                            log.warning(f"  [3/3] Force-Sync: {len(positions_to_close)} Positionen")
                        except Exception as e:
                            log.error(f"  [3/3] Force-Sync fehlgeschlagen: {e}")
            except Exception as e:
                log.error(f"  [3/3] IBKR-direkt-Fallback fehlgeschlagen: {e}")

            if not positions_to_close:
                portfolio_error = "Alle 3 Fetch-Versuche lieferten keine Positionen"
                log.error(f"  [3/3] {portfolio_error}")

        # Tatsaechliches Schliessen
        if positions_to_close:
            from app.etoro_client import EtoroClient
            for pos in positions_to_close:
                try:
                    p = EtoroClient.parse_position(pos)
                    if p.get("invested", 0) > 0:
                        result = client.close_position(
                            p["position_id"], p.get("instrument_id")
                        )
                        if result:
                            closed += 1
                            log.info(f"    Geschlossen: {p['position_id']} "
                                     f"(Instrument {p.get('instrument_id')}, "
                                     f"P/L: {p.get('pnl_pct', 0):+.1f}%)")
                        else:
                            failed += 1
                            log.error(f"    FEHLER: Position {p['position_id']} nicht geschlossen")
                except Exception as e:
                    failed += 1
                    log.error(f"    Position-Close Exception: {e}", exc_info=True)
    except Exception as e:
        portfolio_error = str(e)
        log.error(f"  [3/3] Portfolio-Fetch Exception: {e}", exc_info=True)

    log.warning(
        f"  Emergency-Close-Resultat: {closed} geschlossen, {failed} fehlgeschlagen, "
        f"trading_flag_set={flag_set}, pause_set={pause_set}"
        + (f", portfolio_error='{portfolio_error}'" if portfolio_error else "")
    )

    return {
        "closed": closed,
        "failed": failed,
        "trading_flag_set": flag_set,
        "pause_set": pause_set,
        "portfolio_error": portfolio_error,
        "reason": reason,
    }


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
    # R-B11/R2 (07.06.2026): Candidate EINRECHNEN -> Post-Trade-Zustand pruefen,
    # nicht nur den Ist-Zustand. Frueher floss der zu oeffnende amount_usd*leverage
    # nie in die Exposure-/Margin-Puffer-Rechnung ein -> ein Trade, der
    # max_total_exposure_pct oder den Margin-Puffer ueberschreitet, wurde im
    # selben Cycle nicht geblockt.
    candidate_pos = {"invested": amount_usd, "leverage": leverage,
                     "instrument_id": "__candidate__"}
    margin_ok, margin_reason, _ = check_margin_safety(
        portfolio_value, list(existing_positions) + [candidate_pos], config)
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
    # v37dt Audit#8: .get(.., 0) liefert None wenn der Key MIT Wert None existiert
    # -> Vergleich `recovery_threshold < None` crasht (TypeError) und der f-String
    # `{weekly_pnl:.1f}` ebenso. `or 0` faengt None UND fehlenden Key ab; echte
    # negative Werte (truthy) bleiben erhalten.
    weekly_pnl = state.get("weekly_pnl_pct") or 0

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
