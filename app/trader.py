"""
InvestPilot - Trading Engine (v2)
Automatisches Portfolio-Management: Aufbau, Rebalancing, SL/TP.
Integriert: Risk Manager, Leverage Manager, Asset Filters,
Market Context, Execution Tracking, Alerts.
"""

import logging
import time
from datetime import datetime

from app.config_manager import load_config, save_json, load_json
from app.etoro_client import EtoroClient  # noqa: F401  — static parse_position() bleibt genutzt
from app.broker_base import get_broker

log = logging.getLogger("Trader")


def save_trade(trade_entry):
    """Trade-Historie persistent speichern."""
    history = load_json("trade_history.json") or []
    history.append(trade_entry)
    save_json("trade_history.json", history)


def _attach_fill_prices(trade_entry: dict, broker_result: dict | None) -> dict:
    """v37j: Reichert ein trade_entry um avg_fill_price + intended_price an.

    Diese Felder werden vom Cost-Model-Calibrator (E2) konsumiert, um
    die realisierte IBKR-Slippage pro Asset-Klasse zu schaetzen und
    nach 20+ Fills die hardcodierten Defaults aus app/cost_model.py
    zu ueberschreiben.

    Args:
        trade_entry: bestehender dict (wird in-place erweitert + zurueckgegeben).
        broker_result: das von client.buy/sell/close_position zurueckgegebene
                       dict. Sucht orderForOpen.avgFillPrice/intendedPrice.

    Werte=0 oder None werden NICHT geschrieben (Calibrator filtert sowieso).
    """
    if not isinstance(broker_result, dict):
        return trade_entry
    order = broker_result.get("orderForOpen") or {}
    if not isinstance(order, dict):
        return trade_entry
    avg = order.get("avgFillPrice")
    intended = order.get("intendedPrice")
    ref_quote = order.get("refQuote")
    try:
        if avg and float(avg) > 0:
            trade_entry["avg_fill_price"] = float(avg)
        if intended and float(intended) > 0:
            trade_entry["intended_price"] = float(intended)
        if ref_quote and float(ref_quote) > 0:
            trade_entry["ref_quote"] = float(ref_quote)
    except (TypeError, ValueError):
        pass
    return trade_entry


def _log_close_failure(action_name: str, p: dict, alerts_mod=None, extra: dict | None = None):
    """Protokolliere + persistiere eine fehlgeschlagene close_position()-Antwort.

    Zuvor gab es 4 Stellen in der SL/TP-Logik wo `client.close_position()` einen
    falsy-Wert zurueckgeben konnte und wir nur in den Sonnenschein-Fall (`if result:`)
    eingetreten sind. Folge: Position blieb am eToro offen, aber lokal dachten wir
    'geschlossen' (oder noch schlimmer: kein Log). Das kostet echtes Geld
    (Overnight-Fees, verpasster Exit). Dieser Helper wird jetzt im else-Fall
    aufgerufen und:
      1) loggt als ERROR (sichtbar im Render-Log)
      2) persistiert einen `<action>_FAILED`-Eintrag in trade_history.json
         (damit das Dashboard den Fehlversuch sehen kann)
      3) schickt Telegram-Alert falls das alerts-Modul verfuegbar ist
    """
    pnl_pct = p.get("pnl_pct", 0)
    pos_id = p.get("position_id")
    instr_id = p.get("instrument_id")
    log.error(
        f"  CLOSE FAILED: {action_name} fuer Position {pos_id} (Instrument {instr_id}) — "
        f"eToro hat keinen Erfolg gemeldet. PnL war {pnl_pct:+.2f}%. "
        f"Position bleibt OFFEN — naechster Zyklus wird erneut versuchen."
    )
    try:
        entry = {
            "timestamp": datetime.now().isoformat(),
            "action": f"{action_name}_FAILED",
            "instrument_id": instr_id,
            "position_id": pos_id,
            "pnl_pct": pnl_pct,
            "status": "close_failed",
        }
        if extra:
            entry.update(extra)
        save_trade(entry)
    except Exception as e:
        log.warning(f"  Konnte {action_name}_FAILED nicht in trade_history persistieren: {e}")
    if alerts_mod:
        try:
            alerts_mod.alert_trade_executed({
                "action": f"{action_name}_FAILED",
                "position_id": pos_id,
                "instrument_id": instr_id,
                "pnl_pct": pnl_pct,
                "status": "close_failed",
            })
        except Exception as e:
            log.debug(f"Alert-Dispatch fehlgeschlagen: {e}")


def _find_position_open_time(position_id, api_open_time=None):
    """Ermittle wie lange eine Position offen ist (in Tagen).

    Priority:
    1) api_open_time aus eToro API (wenn Feld vorhanden)
    2) trade_history.json Lookup nach position_id (erster BUY-Entry)

    Returns (open_datetime, age_days) oder (None, None) wenn unbekannt.
    """
    from datetime import datetime, timezone

    def _parse(ts):
        """Parse ISO-Timestamp -> timezone-aware UTC datetime.

        Wichtig: eToro liefert 'Z'-Suffix (UTC), trade_history.json schreibt
        lokale naive Timestamps (datetime.now().isoformat()). Fruehere Version
        hat tzinfo einfach gestripped und mit datetime.now() verglichen -
        das gab bei UTC-Inputs +1/+2h falsche Alter. Jetzt: alles nach UTC
        normalisieren, naive Inputs als lokale Zeit interpretieren.
        """
        if not ts:
            return None
        try:
            s = str(ts).replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                # Naive = lokale Zeit (wie trade_history.json via datetime.now())
                # -> Python 3.6+ astimezone() interpretiert naive als local
                dt = dt.astimezone(timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
            return dt
        except Exception:
            return None

    # 1) eToro API
    dt = _parse(api_open_time)

    # 2) Fallback trade_history lookup
    if dt is None and position_id is not None:
        history = load_json("trade_history.json") or []
        for entry in history:
            if (str(entry.get("position_id")) == str(position_id)
                    and entry.get("action") in ("BUY", "OPEN", "buy", "open")):
                dt = _parse(entry.get("timestamp"))
                if dt is not None:
                    break

    if dt is None:
        return None, None

    age = (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0
    return dt, age


# ============================================================
# SAFE MODULE IMPORTS (Graceful Degradation)
# ============================================================

def _import_risk_manager():
    try:
        from app import risk_manager
        return risk_manager
    except ImportError:
        log.warning("Risk Manager nicht verfuegbar")
        return None

def _import_leverage_manager():
    try:
        from app import leverage_manager
        return leverage_manager
    except ImportError:
        log.warning("Leverage Manager nicht verfuegbar")
        return None

def _import_asset_filters():
    try:
        from app import asset_filters
        return asset_filters
    except ImportError:
        log.warning("Asset Filters nicht verfuegbar")
        return None

def _import_market_context():
    try:
        from app import market_context
        return market_context
    except ImportError:
        log.warning("Market Context nicht verfuegbar")
        return None

def _import_execution():
    try:
        from app import execution
        return execution
    except ImportError:
        log.warning("Execution Tracker nicht verfuegbar")
        return None

def _import_alerts():
    try:
        from app import alerts
        return alerts
    except ImportError:
        log.debug("Alerts nicht verfuegbar")
        return None

def _import_events_calendar():
    try:
        from app import events_calendar
        return events_calendar
    except ImportError:
        log.debug("Events Calendar nicht verfuegbar")
        return None

def _import_sentiment():
    try:
        from app import sentiment
        return sentiment
    except ImportError:
        log.debug("Sentiment-Analyse nicht verfuegbar")
        return None

def _import_hedging():
    try:
        from app import hedging
        return hedging
    except ImportError:
        log.debug("Hedging nicht verfuegbar")
        return None


# ============================================================
# PORTFOLIO STATUS
# ============================================================

def show_portfolio_status(client):
    """Aktuellen Portfolio-Status anzeigen und zurueckgeben."""
    log.info("=" * 55)
    log.info("PORTFOLIO STATUS")
    log.info("=" * 55)

    portfolio = client.get_portfolio()
    if not portfolio:
        log.error("Portfolio nicht verfuegbar")
        return None

    credit = portfolio.get("credit", 0)
    positions = portfolio.get("positions", [])
    unrealized_pnl = portfolio.get("unrealizedPnL", 0)

    total_invested = 0
    parsed_positions = []
    for pos in positions:
        p = EtoroClient.parse_position(pos)
        parsed_positions.append(p)
        total_invested += p["invested"]

    # v36g — gleicher Fix wie in brain.record_snapshot:
    # IBKR liefert _equity (NetLiquidation) — das ist die korrekte Equity.
    # credit + invested + pnl ueberzaehlt bei IBKR, weil credit dort
    # AvailableFunds (Cash MINUS Margin-Reserve) ist.
    ibkr_equity = portfolio.get("_equity")
    if ibkr_equity is not None and ibkr_equity > 0:
        total_value = float(ibkr_equity)
    else:
        total_value = total_invested + unrealized_pnl + credit

    log.info(f"  Credit (Cash):     ${credit:>12,.2f}")
    log.info(f"  Investiert:        ${total_invested:>12,.2f}")
    log.info(f"  Unrealized P/L:    ${unrealized_pnl:>12,.2f}")
    log.info(f"  Gesamtwert:        ${total_value:>12,.2f}")
    log.info(f"  Positionen:        {len(positions)}")

    for p in parsed_positions:
        log.info(f"    #{p['instrument_id']}: ${p['invested']:,.0f} -> "
                 f"P/L: ${p['pnl']:+,.2f} ({p['pnl_pct']:+.1f}%) {p['leverage']}x")

    # Risk Manager: Drawdown-Tracking
    rm = _import_risk_manager()
    if rm:
        state = rm.update_portfolio_tracking(total_value)
        log.info(f"  Tages-P/L:         {state['daily_pnl_pct']:+.2f}% (${state['daily_pnl_usd']:+,.2f})")
        log.info(f"  Wochen-P/L:        {state['weekly_pnl_pct']:+.2f}% (${state['weekly_pnl_usd']:+,.2f})")

    return portfolio


# ============================================================
# PORTFOLIO AUFBAU
# ============================================================

def build_initial_portfolio(client, config):
    """Portfolio nach Ziel-Allokation aufbauen."""
    log.info("=" * 55)
    log.info("PORTFOLIO AUFBAU")
    log.info("=" * 55)

    dt_config = config.get("demo_trading", {})
    targets = dt_config.get("portfolio_targets", {})
    max_trade = dt_config.get("max_single_trade_usd", 5000)
    default_leverage = dt_config.get("default_leverage", 1)

    rm = _import_risk_manager()
    lm = _import_leverage_manager()
    af = _import_asset_filters()
    ex = _import_execution()

    portfolio = client.get_portfolio()
    if not portfolio:
        log.error("Konnte Portfolio nicht laden")
        return []

    credit = portfolio.get("credit", 0)
    positions = portfolio.get("positions", [])
    log.info(f"  Verfuegbar: ${credit:,.2f}")
    log.info(f"  Positionen: {len(positions)}")

    if credit < 100:
        log.warning("  Zu wenig Credit fuer neue Trades")
        return []

    # Bestehende Positionen nach InstrumentID mappen
    existing = {}
    for pos in positions:
        p = EtoroClient.parse_position(pos)
        iid = p["instrument_id"]
        existing[iid] = existing.get(iid, 0) + p["invested"]

    total_portfolio = credit + sum(existing.values())
    log.info(f"  Portfolio-Gesamtwert: ${total_portfolio:,.2f}")

    trades_executed = []
    for symbol, target in targets.items():
        iid = target["instrument_id"]
        target_pct = target["allocation_pct"]
        target_value = total_portfolio * target_pct / 100
        current_value = existing.get(iid, 0)
        diff = target_value - current_value

        log.info(f"  {symbol}: Soll=${target_value:,.0f} Ist=${current_value:,.0f} Diff=${diff:,.0f}")

        if diff > 50:
            amount = min(diff, max_trade, credit * 0.9)
            if amount < 50:
                continue

            leverage = target.get("leverage", default_leverage)

            # Leverage Manager: Auf erlaubte Stufe pruefen
            asset_class = target.get("class", "stocks")
            if lm:
                leverage = lm.snap_to_allowed(leverage, asset_class, symbol)

            # Risk Manager: Position Sizing
            if rm:
                stop_loss_pct = dt_config.get("stop_loss_pct", -3)
                max_risk_size = rm.calculate_leveraged_position_size(
                    total_portfolio, stop_loss_pct, leverage, config)
                amount = min(amount, max_risk_size)

            if amount < 50:
                log.info(f"    -> Skip (Risk-Sizing reduziert auf ${amount:.0f})")
                continue

            # Execution Tracking
            start_time = time.time()
            result = client.buy(iid, round(amount, 2), leverage=leverage)

            if ex and result:
                ex.track_execution(None, result, iid, "BUY", amount, asset_class, start_time)

            if result:
                order = result.get("orderForOpen", {})
                # v37q: position_id mitschreiben fuer Meta-Labeler-Training-Match.
                # IBKR: conId aus result._contract. eToro: order_id ist gleichzeitig
                # die position_id (UUID).
                contract_info = result.get("_contract") or {}
                pid = contract_info.get("conId") or order.get("orderID")
                trade_entry = {
                    "timestamp": datetime.now().isoformat(),
                    "action": "BUY",
                    "symbol": symbol,
                    "name": target["name"],
                    "instrument_id": iid,
                    "position_id": str(pid) if pid is not None else None,
                    "asset_class": asset_class,
                    "amount_usd": round(amount, 2),
                    "leverage": leverage,
                    "order_id": order.get("orderID"),
                    "status": "executed",
                }

                # Leverage logging
                if lm:
                    trade_entry = lm.log_leverage_trade(trade_entry, total_portfolio)

                save_trade(_attach_fill_prices(trade_entry, result))
                trades_executed.append(trade_entry)
                credit -= amount
                log.info(f"    -> GEKAUFT: ${amount:,.2f} {leverage}x (Order: {order.get('orderID')})")

                # Alert
                al = _import_alerts()
                if al:
                    al.alert_trade_executed(trade_entry)
            else:
                log.error(f"    -> FEHLER bei {symbol}")

    log.info(f"\n  {len(trades_executed)} Trades ausgefuehrt")
    return trades_executed


# ============================================================
# STOP-LOSS / TAKE-PROFIT
# ============================================================

def check_stop_loss_take_profit(client, config):
    """Stop-Loss und Take-Profit pruefen (inkl. Trailing SL)."""
    log.info("=" * 55)
    log.info("STOP-LOSS / TAKE-PROFIT CHECK")
    log.info("=" * 55)

    dt_config = config.get("demo_trading", {})
    sl_pct = dt_config.get("stop_loss_pct", -10)
    tp_pct = dt_config.get("take_profit_pct", 25)

    # v12: Time-Stop Exit Config
    ts_cfg = config.get("time_stop", {}) or {}
    ts_enabled = ts_cfg.get("enabled", False)
    ts_max_days = ts_cfg.get("max_days_stale", 10)
    ts_stale_thr = ts_cfg.get("stale_pnl_threshold_pct", 0.5)
    ts_min_days = ts_cfg.get("min_days_open", 2)

    lm = _import_leverage_manager()
    al = _import_alerts()

    portfolio = client.get_portfolio()
    if not portfolio:
        return []

    # v37i: Market-Hours-Guard pro Position
    # Verhindert STOP_LOSS_CLOSE_FAILED-Spam-Loops wenn der Bot waehrend
    # geschlossener Boersen versucht eine Position zu schliessen (z.B. ROKU
    # in US-Pre-Market 03-09 EST). IBKR liefert dann keinen Live-Quote ->
    # close_position() returned None -> trader logged FAILED -> trade_history
    # bekommt im 5-Min-Takt einen Fehler-Eintrag, bis der Markt aufmacht.
    # Loesung: vor jedem Close-Versuch pruefen ob Asset-Klasse jetzt
    # tradeable ist. Wenn nicht: einmal INFO-loggen und ueberspringen.
    try:
        from app.asset_classes import is_asset_class_tradeable
    except ImportError:
        is_asset_class_tradeable = None
    skipped_off_hours: set[str] = set()  # nur einmal pro Klasse loggen

    actions = []
    for pos in portfolio.get("positions", []):
        p = EtoroClient.parse_position(pos)
        if p["invested"] <= 0:
            continue

        # Market-Hours Guard
        if is_asset_class_tradeable is not None:
            asset_class = _lookup_asset_class(p.get("instrument_id"))
            if not is_asset_class_tradeable(asset_class):
                if asset_class not in skipped_off_hours:
                    log.info(
                        f"  Markt geschlossen fuer Klasse '{asset_class}' — "
                        f"SL/TP-Checks fuer betroffene Positionen werden uebersprungen "
                        f"(Position bleibt offen, retry zum naechsten RTH-Open)."
                    )
                    skipped_off_hours.add(asset_class)
                continue

        # Trailing SL: Fallback current_price aus PnL + entry_price berechnen
        if not p.get("current_price") and p.get("entry_price") and p["invested"] > 0:
            # current_price = entry_price * (1 + pnl_pct/100)
            p["current_price"] = round(p["entry_price"] * (1 + p["pnl_pct"] / 100), 6)
        if not p.get("entry_price") and p.get("current_price") and p["invested"] > 0 and p["pnl_pct"] != 0:
            # entry_price = current_price / (1 + pnl_pct/100)
            p["entry_price"] = round(p["current_price"] / (1 + p["pnl_pct"] / 100), 6)

        # Trailing SL Update + Trigger Check
        if lm and p.get("current_price"):
            lm.update_trailing_stop_loss(
                p["position_id"], p["current_price"], p.get("entry_price", p["current_price"]),
                p["leverage"], config)

            # Pruefe ob Trailing SL ausgeloest wurde
            triggered = lm.check_trailing_stop_losses([{
                "position_id": p["position_id"],
                "instrument_id": p["instrument_id"],
                "current_price": p["current_price"],
            }])
            if triggered:
                result = client.close_position(p["position_id"], p["instrument_id"])
                if result:
                    trade_entry = {
                        "timestamp": datetime.now().isoformat(),
                        "action": "TRAILING_SL_CLOSE",
                        "instrument_id": p["instrument_id"],
                        "position_id": p["position_id"],
                        "pnl_pct": p["pnl_pct"],
                        "pnl_usd": p["pnl"],
                        "leverage": p["leverage"],
                        "trailing_sl_level": triggered[0]["sl_level"],
                        "status": "executed",
                    }
                    save_trade(_attach_fill_prices(trade_entry, result))
                    actions.append("TRAILING_SL_CLOSE")
                    if al:
                        al.alert_trade_executed(trade_entry)
                else:
                    _log_close_failure("TRAILING_SL_CLOSE", p, al,
                                       extra={"trailing_sl_level": triggered[0]["sl_level"]})
                continue  # Trailing SL hat Prioritaet, Skip fixed SL/TP

        # --- v12: Time-Stop / Staleness Exit ---
        # Schliesst Positionen die zu lange "stuck" sind und kaum P/L generieren.
        # Opportunitaetskosten-Schutz: gebundenes Kapital waere woanders besser.
        if ts_enabled:
            _, age_days = _find_position_open_time(p["position_id"], p.get("open_time"))
            if age_days is not None and age_days >= ts_max_days \
                    and age_days >= ts_min_days \
                    and abs(p["pnl_pct"]) < ts_stale_thr:
                log.info(f"  TIME_STOP: Position {p['position_id']} "
                         f"(Instrument {p['instrument_id']}) — "
                         f"{age_days:.1f}d offen, PnL {p['pnl_pct']:+.2f}% < {ts_stale_thr}%")
                result = client.close_position(p["position_id"], p["instrument_id"])
                if result:
                    trade_entry = {
                        "timestamp": datetime.now().isoformat(),
                        "action": "TIME_STOP_CLOSE",
                        "instrument_id": p["instrument_id"],
                        "position_id": p["position_id"],
                        "pnl_pct": p["pnl_pct"],
                        "pnl_usd": p["pnl"],
                        "leverage": p["leverage"],
                        "age_days": round(age_days, 2),
                        "status": "executed",
                    }
                    save_trade(_attach_fill_prices(trade_entry, result))
                    actions.append("TIME_STOP_CLOSE")
                    if al:
                        al.alert_trade_executed(trade_entry)
                    continue  # Position ist zu, Rest skippen
                else:
                    _log_close_failure("TIME_STOP_CLOSE", p, al,
                                       extra={"age_days": round(age_days, 2)})
                    # WICHTIG: KEIN continue — naechster Zyklus versucht erneut,
                    # aber innerhalb dieses Zyklus nicht noch SL/TP drueberlegen.

        # --- Profit-Locking: Partial Close (TP-Tranchen) ---
        lev_cfg = config.get("leverage", {})
        tp_tranches = lev_cfg.get("tp_tranches", [])
        if tp_tranches and p["pnl_pct"] > 0:
            partial_state = load_json("partial_close_state.json") or {}
            pid_key = str(p["position_id"])
            triggered_tranches = partial_state.get(pid_key, {}).get("triggered", [])

            for tranche_idx, tranche in enumerate(tp_tranches):
                target_pct = tranche.get("profit_target_pct", 0)
                close_pct = tranche.get("pct_of_position", 0)

                if tranche_idx in triggered_tranches:
                    continue  # Diese Tranche wurde bereits ausgeloest

                if p["pnl_pct"] >= target_pct:
                    close_amount = round(p["invested"] * close_pct / 100, 2)
                    if close_amount >= 1:
                        log.info(f"  PARTIAL_CLOSE: Position {p['position_id']} "
                                 f"(Instrument {p['instrument_id']}) — "
                                 f"Tranche {tranche_idx+1}: {close_pct}% bei +{target_pct}% "
                                 f"(PnL: {p['pnl_pct']:+.1f}%, Betrag: ${close_amount:,.2f})")

                        # Berechne neue kumulierte Summe (noch nicht persistiert!)
                        prev_total = partial_state.get(pid_key, {}).get("total_closed_pct", 0)
                        new_total = prev_total + close_pct

                        # eToro API unterstuetzt kein partielles Schliessen.
                        # Wenn kumulierte Tranchen >= 100%: GANZE Position schliessen.
                        if new_total >= 100:
                            log.info(f"  PROFIT_LOCK_CLOSE: Alle Tranchen erreicht "
                                     f"({new_total}%) — schliesse Position komplett")
                            result = client.close_position(p["position_id"], p["instrument_id"])
                            trade_status = "executed" if result else "failed"
                            if not result:
                                log.warning(f"  PROFIT_LOCK_CLOSE FEHLGESCHLAGEN — "
                                            f"Tranche wird NICHT als erledigt markiert")
                        else:
                            log.info(f"  PARTIAL_SIGNAL: Tranche {tranche_idx+1} erreicht "
                                     f"(kumuliert {new_total}% — eToro erlaubt nur Full Close)")
                            trade_status = "signal_logged"
                            result = True  # Signals werden immer als "erfolgreich" gewertet

                        # Tranche NUR bei Erfolg als ausgeloest markieren
                        # (bei API-Fehler bleibt Tranche offen fuer naechsten Zyklus)
                        if result:
                            if pid_key not in partial_state:
                                partial_state[pid_key] = {"triggered": [], "total_closed_pct": 0}
                            partial_state[pid_key]["triggered"].append(tranche_idx)
                            partial_state[pid_key]["total_closed_pct"] = new_total
                            save_json("partial_close_state.json", partial_state)

                        trade_entry = {
                            "timestamp": datetime.now().isoformat(),
                            "action": "PROFIT_LOCK_CLOSE" if new_total >= 100 else "PARTIAL_SIGNAL",
                            "instrument_id": p["instrument_id"],
                            "position_id": p["position_id"],
                            "pnl_pct": p["pnl_pct"],
                            "pnl_usd": round(p["pnl"] * close_pct / 100, 2),
                            "leverage": p["leverage"],
                            "tranche_index": tranche_idx,
                            "tranche_close_pct": close_pct,
                            "tranche_target_pct": target_pct,
                            "close_amount_usd": close_amount,
                            "total_closed_pct": new_total,
                            "status": trade_status,
                        }
                        save_trade(_attach_fill_prices(trade_entry, result))
                        actions.append(trade_entry["action"])

                        # Bei voller Schliessung: Rest der Tranchen-Pruefung ueberspringen
                        if new_total >= 100 and result:
                            break

                        if al:
                            al.alert_trade_executed(trade_entry)

        # Stop-Loss Check
        if p["pnl_pct"] <= sl_pct:
            log.warning(f"  STOP-LOSS: Position {p['position_id']} "
                        f"(Instrument {p['instrument_id']}) bei {p['pnl_pct']:+.1f}%")
            result = client.close_position(p["position_id"], p["instrument_id"])
            if result:
                trade_entry = {
                    "timestamp": datetime.now().isoformat(),
                    "action": "STOP_LOSS_CLOSE",
                    "instrument_id": p["instrument_id"],
                    "position_id": p["position_id"],
                    "pnl_pct": p["pnl_pct"],
                    "pnl_usd": p["pnl"],
                    "leverage": p["leverage"],
                    "status": "executed",
                }
                save_trade(_attach_fill_prices(trade_entry, result))
                actions.append("STOP_LOSS_CLOSE")
                if al:
                    al.alert_trade_executed(trade_entry)
            else:
                _log_close_failure("STOP_LOSS_CLOSE", p, al)

        # Take-Profit Check (nur fuer verbleibende Position nach Partial Closes)
        elif p["pnl_pct"] >= tp_pct:
            partial_state_tp = load_json("partial_close_state.json") or {}
            pid_tp_key = str(p["position_id"])
            closed_pct_total = partial_state_tp.get(pid_tp_key, {}).get("total_closed_pct", 0)
            remaining_label = f" (Rest nach {closed_pct_total}% Partial Close)" if closed_pct_total > 0 else ""
            log.info(f"  TAKE-PROFIT: Position {p['position_id']} "
                     f"(Instrument {p['instrument_id']}) bei {p['pnl_pct']:+.1f}%{remaining_label}")
            result = client.close_position(p["position_id"], p["instrument_id"])
            if result:
                trade_entry = {
                    "timestamp": datetime.now().isoformat(),
                    "action": "TAKE_PROFIT_CLOSE",
                    "instrument_id": p["instrument_id"],
                    "position_id": p["position_id"],
                    "pnl_pct": p["pnl_pct"],
                    "pnl_usd": p["pnl"],
                    "leverage": p["leverage"],
                    "status": "executed",
                }
                save_trade(_attach_fill_prices(trade_entry, result))
                actions.append("TAKE_PROFIT_CLOSE")
                if al:
                    al.alert_trade_executed(trade_entry)
            else:
                _log_close_failure("TAKE_PROFIT_CLOSE", p, al)

    # Partial-Close State bereinigen fuer geschlossene Positionen
    _cleanup_partial_close_state(portfolio)

    log.info(f"  {len(actions)} SL/TP Aktionen")
    return actions


def _cleanup_partial_close_state(portfolio):
    """Entferne Partial-Close-State fuer Positionen die nicht mehr offen sind."""
    partial_state = load_json("partial_close_state.json") or {}
    if not partial_state:
        return
    open_ids = set()
    if portfolio:
        for pos in portfolio.get("positions", []):
            p = EtoroClient.parse_position(pos)
            open_ids.add(str(p["position_id"]))
    cleaned = {pid: data for pid, data in partial_state.items() if pid in open_ids}
    if len(cleaned) != len(partial_state):
        save_json("partial_close_state.json", cleaned)


# ============================================================
# REBALANCING
# ============================================================

def rebalance_portfolio(client, config):
    """Portfolio rebalancieren wenn Abweichung zu gross."""
    log.info("=" * 55)
    log.info("REBALANCING CHECK")
    log.info("=" * 55)

    dt_config = config.get("demo_trading", {})
    targets = dt_config.get("portfolio_targets", {})
    threshold = dt_config.get("rebalance_threshold_pct", 5)

    portfolio = client.get_portfolio()
    if not portfolio:
        return

    credit = portfolio.get("credit", 0)
    positions = portfolio.get("positions", [])

    pos_by_instrument = {}
    total_invested = 0
    for pos in positions:
        p = EtoroClient.parse_position(pos)
        iid = p["instrument_id"]
        current_val = p["invested"] + p["pnl"]
        pos_by_instrument[iid] = pos_by_instrument.get(iid, 0) + current_val
        total_invested += current_val

    total = total_invested + credit
    if total <= 0:
        log.info("  Portfolio leer - kein Rebalancing noetig")
        return

    needs_rebalance = False
    for symbol, target in targets.items():
        iid = target["instrument_id"]
        target_pct = target["allocation_pct"]
        current_val = pos_by_instrument.get(iid, 0)
        current_pct = (current_val / total * 100) if total > 0 else 0
        deviation = current_pct - target_pct

        status = "OK" if abs(deviation) <= threshold else "REBALANCE"
        if status == "REBALANCE":
            needs_rebalance = True

        log.info(f"  {symbol}: Soll={target_pct}% Ist={current_pct:.1f}% "
                 f"Abw={deviation:+.1f}% [{status}]")

    if needs_rebalance and credit > 100:
        log.info("  -> Rebalancing wird ausgefuehrt...")
        build_initial_portfolio(client, config)
    else:
        log.info("  -> Kein Rebalancing noetig")


# ============================================================
# SCANNER-BASIERTES TRADING (v2 mit allen Filtern)
# ============================================================

def execute_scanner_trades(client, config, scan_results):
    """Trades basierend auf Scanner-Ergebnissen mit vollen Safety-Checks."""
    log.info("=" * 55)
    log.info("DYNAMISCHES TRADING (Scanner-basiert)")
    log.info("=" * 55)

    dt_config = config.get("demo_trading", {})
    # v15: max_trade wird pro Trade aus dem Portfolio-Wert resolved (siehe unten),
    # max_positions skaliert mit Portfolio via Tier-Map.
    min_score = dt_config.get("min_scanner_score", 15)
    stop_loss_pct = dt_config.get("stop_loss_pct", -3)

    rm = _import_risk_manager()
    lm = _import_leverage_manager()
    af = _import_asset_filters()
    mc = _import_market_context()
    ex = _import_execution()
    al = _import_alerts()
    ec = _import_events_calendar()
    sent = _import_sentiment()
    hdg = _import_hedging()

    portfolio = client.get_portfolio()
    if not portfolio:
        log.error("Portfolio nicht verfuegbar")
        return []

    credit = portfolio.get("credit", 0)
    positions = portfolio.get("positions", [])
    parsed_positions = [EtoroClient.parse_position(pos) for pos in positions]
    # v36g — IBKR-Korrektur: credit ist AvailableFunds, nicht reines Cash.
    # NetLiquidation aus _equity nutzen wenn verfuegbar (siehe brain.py).
    ibkr_equity_pre = portfolio.get("_equity")
    if ibkr_equity_pre is not None and ibkr_equity_pre > 0:
        total_value = float(ibkr_equity_pre)
    else:
        total_value = credit + sum(p["invested"] for p in parsed_positions)

    # v15: Portfolio-Sizing skaliert mit Portfolio-Wert (Tier-Map + Prozent-basiert).
    if rm:
        max_positions = rm.resolve_max_positions(total_value, config)
        max_trade = rm.resolve_max_single_trade_usd(total_value, config)
    else:
        max_positions = config.get("risk_management", {}).get(
            "max_open_positions", dt_config.get("max_positions", 10))
        max_trade = dt_config.get("max_single_trade_usd", 3000)

    # v15: Cash-Deposit-DCA — staffel neue Einzahlungen ueber N Zyklen,
    # damit Market-Timing-Risiko reduziert wird. `effective_cash` ist der
    # fuer diesen Zyklus deploy-bare Betrag; `credit` bleibt unveraendert
    # fuer Logging und echte Safety-Checks.
    dca_info = None
    effective_cash = credit
    if rm:
        try:
            dca_info = rm.detect_cash_deposit(credit, config)
            effective_cash = dca_info.get("remaining_budget_usd", credit)
            if dca_info.get("dca_active"):
                log.info(
                    f"  Cash-DCA aktiv: ${effective_cash:,.2f}/{credit:,.2f} "
                    f"verfuegbar ({dca_info.get('remaining_cycles')} Zyklen verbleibend)"
                )
        except Exception as e:
            log.warning(f"Cash-DCA Detection fehlgeschlagen (non-fatal): {e}", exc_info=True)

    # Risk Manager: Drawdown-Check
    if rm:
        dd_ok, dd_reason = rm.check_drawdown_limits()
        if not dd_ok:
            log.warning(f"  TRADING PAUSIERT: {dd_reason}")
            if al:
                al.alert_drawdown(
                    rm.get_risk_summary().get("daily_pnl_pct", 0),
                    rm.get_risk_summary().get("weekly_pnl_pct", 0),
                    dd_reason)
            return []

    # Market Context: Positionsgroessen-Multiplikator + Regime Gate
    ctx_multiplier = 1.0
    regime_halt = False
    regime_data = {}
    if mc:
        ctx = mc.get_current_context()
        ctx_multiplier = ctx.get("position_size_multiplier", 1.0)
        if ctx_multiplier < 1.0:
            log.info(f"  Marktkontext: Positionsgroessen x{ctx_multiplier}")

        # Kombinierter Regime-Filter: VIX + Fear&Greed + Brain-Regime
        from app.market_context import check_regime_filter
        buy_allowed, regime_reason, regime_data = check_regime_filter(config)
        if not buy_allowed:
            log.warning(f"  REGIME FILTER: {regime_reason}")
            regime_halt = True
            # v12: Panic-Dip-Buy Override — wenn VIX Term Structure auf akuter
            # Backwardation ist (VIX9D deutlich > VIX > VIX3M) ist das historisch
            # ein Capitulation-Signal. Wir heben den Halt auf und fahren mit
            # reduzierter Position-Multiplier.
            vts = ctx.get("vix_term_structure") or {}
            vts_cfg = config.get("vix_term_structure", {}) or {}
            if vts_cfg.get("panic_dip_override_enabled", True) and vts.get("panic_dip_buy_signal"):
                log.warning(f"  PANIC-DIP-BUY OVERRIDE: VIX Term "
                            f"ratio={vts.get('ratio_9d_vs_30d')} "
                            f"shape={vts.get('shape')} — Regime Halt aufgehoben")
                regime_halt = False
                ctx_multiplier *= vts_cfg.get("panic_dip_position_multiplier", 0.6)
            elif al:
                # Telegram: Regime Halt Notification
                al.alert_regime_halt(regime_reason, regime_data)

    # Hedging: Bear-Regime Schutz
    hedge_result = {"hedge_needed": False}
    if hdg:
        hedge_result = hdg.check_hedge_needed(regime_data, parsed_positions, config)
        if hedge_result.get("hedge_needed"):
            hedge_mult = hedge_result.get("bear_position_multiplier", 0.5)
            ctx_multiplier *= hedge_mult
            log.info(f"  HEDGING AKTIV: Positionsgroessen x{hedge_mult} "
                     f"(effektiv x{ctx_multiplier:.2f})")

    # Risk Manager: Margin Safety Check
    if rm:
        margin_ok, margin_reason, exposure = rm.check_margin_safety(total_value, parsed_positions, config)
        if not margin_ok:
            log.warning(f"  MARGIN WARNING: {margin_reason}")
            # Auto-Deleverage bei kritischem Margin
            rm.auto_deleverage(client, parsed_positions, total_value, config)

    existing_ids = {p["instrument_id"] for p in parsed_positions}

    log.info(
        f"  Cash: ${credit:,.2f} (deploy-bar ${effective_cash:,.2f}) | "
        f"Positionen: {len(positions)}/{max_positions} | "
        f"max_trade=${max_trade:,.0f}"
    )

    # --- VERKAUFEN: Positionen mit SELL-Signal ---
    sell_candidates = [r for r in scan_results
                       if r["signal"] in ("SELL", "STRONG_SELL")
                       and r["etoro_id"] in existing_ids]

    trades_executed = []
    for candidate in sell_candidates:
        for pos in positions:
            p = EtoroClient.parse_position(pos)
            if p["instrument_id"] == candidate["etoro_id"] and p["invested"] > 0:
                log.info(f"  SCANNER SELL: {candidate['symbol']} "
                         f"(Score={candidate['score']:+.1f}, {candidate['signal']})")

                start_time = time.time()
                result = client.close_position(p["position_id"], p["instrument_id"])

                if ex and result:
                    ex.track_execution(None, result, candidate["etoro_id"],
                                       "SCANNER_SELL", p["invested"],
                                       candidate["class"], start_time)

                if result:
                    trade_entry = {
                        "timestamp": datetime.now().isoformat(),
                        "action": "SCANNER_SELL",
                        "symbol": candidate["symbol"],
                        "name": candidate["name"],
                        "instrument_id": candidate["etoro_id"],
                        "asset_class": candidate["class"],
                        "position_id": p["position_id"],
                        "pnl_pct": p["pnl_pct"],
                        "scanner_score": candidate["score"],
                        "signal": candidate["signal"],
                        "status": "executed",
                    }
                    save_trade(_attach_fill_prices(trade_entry, result))
                    trades_executed.append(trade_entry)
                    if al:
                        al.alert_trade_executed(trade_entry)
                else:
                    _log_close_failure("SCANNER_SELL", p, al, extra={
                        "symbol": candidate["symbol"],
                        "scanner_score": candidate["score"],
                        "signal": candidate["signal"],
                    })

    # --- KAUFEN: Top Opportunities mit vollen Safety-Checks ---
    if regime_halt:
        log.info("  BUY-Phase uebersprungen (Regime Halt aktiv)")
        return trades_executed

    # Intraday Timing Filter: Keine Kaeufe in volatilen Marktphasen (Open/Close)
    timing_cfg = config.get("intraday_timing", {})
    if timing_cfg.get("enabled", False):
        now = datetime.now()
        avoid_first = timing_cfg.get("avoid_first_minutes", 30)
        avoid_last = timing_cfg.get("avoid_last_minutes", 30)
        # US Markt: 15:30-22:00 CET
        market_open_h, market_open_m = 15, 30
        market_close_h, market_close_m = 22, 0

        minutes_since_open = (now.hour - market_open_h) * 60 + (now.minute - market_open_m)
        minutes_until_close = (market_close_h - now.hour) * 60 + (market_close_m - now.minute)

        if 0 <= minutes_since_open < avoid_first:
            log.info(f"  INTRADAY TIMING: Erste {avoid_first} Minuten nach Open — "
                     f"keine Kaeufe ({minutes_since_open} Min seit Open)")
            return trades_executed

        if 0 <= minutes_until_close < avoid_last:
            log.info(f"  INTRADAY TIMING: Letzte {avoid_last} Minuten vor Close — "
                     f"keine Kaeufe ({minutes_until_close} Min bis Close)")
            return trades_executed

    # Recovery Mode: Einschraenkungen bei moderatem Drawdown
    recovery_active = False
    recovery_restrictions = {}
    if rm:
        recovery_active, recovery_restrictions = rm.check_recovery_mode(config)
        if recovery_active:
            log.warning(f"  {recovery_restrictions.get('reason', 'RECOVERY MODE')}")
            min_score = max(min_score, recovery_restrictions.get("min_score", 30))

    # v36 — Loop-Cooldown: blockiert Re-Buy desselben Symbols innerhalb
    # cooldown_cycles falls vorheriger Versuch nicht in eine echte Position
    # mündete (broker bestätigt nicht). Fix für ROKU-Fixierungs-Loop am
    # 27.04. (133+ identische Buys).
    cooldown_cycles = dt_config.get("buy_cooldown_cycles", 12)  # 12 × 5min = 1h
    cooldown_state = load_json("buy_cooldown.json") or {}
    now_iso = datetime.now().isoformat()
    # Stale-Eintraege bereinigen (>24h)
    cooldown_state = {
        k: v for k, v in cooldown_state.items()
        if (datetime.now() - datetime.fromisoformat(v["last_attempt"])).total_seconds() < 86400
    }

    def _in_cooldown(sym_id: int) -> tuple[bool, str]:
        rec = cooldown_state.get(str(sym_id))
        if not rec:
            return False, ""
        elapsed_cycles = (datetime.now() - datetime.fromisoformat(rec["last_attempt"])).total_seconds() / 300
        if elapsed_cycles < cooldown_cycles:
            return True, f"cooldown {elapsed_cycles:.1f}/{cooldown_cycles} cycles, {rec.get('attempts',1)} prev attempts"
        return False, ""

    buy_candidates = [r for r in scan_results
                      if r["signal"] in ("BUY", "STRONG_BUY")
                      and r["score"] >= min_score
                      and r["etoro_id"] not in existing_ids]

    # Cooldown-Filter
    blocked_by_cooldown = []
    filtered = []
    for c in buy_candidates:
        in_cd, reason = _in_cooldown(c["etoro_id"])
        if in_cd:
            blocked_by_cooldown.append((c["symbol"], reason))
        else:
            filtered.append(c)
    if blocked_by_cooldown:
        for sym, r in blocked_by_cooldown:
            log.info(f"  COOLDOWN-SKIP {sym}: {r}")
    buy_candidates = filtered

    # v36 — Insider-Signal-Filter (CEOWatcher-Equivalent via Finnhub)
    # Blockt Buys wenn Insider verkaufen (-2 Score). Filter ist via
    # config.scanner.insider_signal_enabled aktiviert. Fix fuer ROKU
    # (Insider-Score -2) das am 27.04. 67x gekauft werden sollte.
    #
    # v37m — Shadow-Mode (Forward-A/B): wenn Filter DEAKTIVIERT ist, wird
    # der Insider-Score trotzdem fuer jedes Candidate berechnet und in
    # data/insider_shadow_log.jsonl persistiert. Ueber 2-4 Wochen Paper-
    # Trading laesst sich so vergleichen ob die "geblockten" Candidates
    # schlechter performt haetten als die durchgelassenen — echtes A/B
    # mit Live-Daten ohne Backtest-Datenbedarf (siehe E5b in Roadmap).
    try:
        from app import insider_signals
        scanner_cfg = config.get("scanner", {}) or {}
        insider_min_score = scanner_cfg.get("insider_min_score", -1)
        quality_filter = scanner_cfg.get("insider_quality_filter", True)
        detect_novelty = scanner_cfg.get("insider_detect_novelty", True)
        shadow_enabled = scanner_cfg.get("insider_shadow_tracking", True)

        if insider_signals.is_enabled(config):
            # AKTIV: filtern + Score-Bonus
            insider_filtered = []
            blocked_by_insider = []
            for c in buy_candidates:
                try:
                    iscore = insider_signals.compute_insider_score(
                        c["symbol"],
                        quality_filter=quality_filter,
                        detect_novelty=detect_novelty,
                    )
                    if iscore < insider_min_score:
                        blocked_by_insider.append((c["symbol"], iscore))
                        continue
                    # Score als Bonus auf Scanner-Score (-2..+5 → bis +10 Bonus)
                    if iscore != 0:
                        original = c["score"]
                        bonus = iscore * 2.0  # Skaliert: -2 Insider → -4 Score
                        c["score"] = round(original + bonus, 1)
                        log.info(f"  INSIDER {c['symbol']}: insider={iscore:+d} -> "
                                 f"score {original:.1f} -> {c['score']:.1f}")
                    insider_filtered.append(c)
                except Exception as ie:
                    log.debug(f"Insider-Score {c['symbol']} fehlgeschlagen: {ie}")
                    insider_filtered.append(c)  # bei Fehler durchlassen

            for sym, sc in blocked_by_insider:
                log.info(f"  INSIDER-BLOCK {sym}: insider_score={sc:+d} "
                         f"(min={insider_min_score})")
            buy_candidates = [c for c in insider_filtered if c["score"] >= min_score]
            buy_candidates.sort(key=lambda x: x["score"], reverse=True)

        elif shadow_enabled and buy_candidates:
            # SHADOW: passiv tracken was geblockt WUERDE — kein Eingriff
            from app.insider_shadow import log_shadow_decision
            shadow_count = 0
            for c in buy_candidates:
                try:
                    iscore = insider_signals.compute_insider_score(
                        c["symbol"],
                        quality_filter=quality_filter,
                        detect_novelty=detect_novelty,
                    )
                    would_block = iscore < insider_min_score
                    log_shadow_decision(
                        symbol=c["symbol"],
                        scanner_score=c.get("score", 0),
                        insider_score=iscore,
                        would_block=would_block,
                        insider_min_score=insider_min_score,
                    )
                    shadow_count += 1
                except Exception as ie:
                    log.debug(f"Shadow-Insider {c['symbol']} fehlgeschlagen: {ie}")
            if shadow_count > 0:
                log.info(f"  INSIDER-SHADOW: {shadow_count} Candidates getrackt "
                         f"(Filter inaktiv — Forward-A/B)")
    except ImportError:
        pass
    except Exception as e:
        log.warning(f"Insider-Filter/Shadow Fehler (non-fatal): {e}", exc_info=True)

    # ML Confidence: multiply scanner score by ML probability if enabled
    use_ml = dt_config.get("use_ml_scoring", False)
    if use_ml:
        try:
            from app.ml_scorer import (
                is_model_trained, predict_score, load_persisted_model,
                get_tuned_threshold,
            )
            if not is_model_trained():
                load_persisted_model()
            if is_model_trained():
                # Option B: Bonus/Malus um den F1-getunten Threshold zentrieren
                # (statt um fixe 0.5), damit der sichere Default des Trainings
                # auch die Buy-Entscheidung kalibriert.
                ml_threshold = get_tuned_threshold()
                for cand in buy_candidates:
                    analysis = cand.get("analysis", {})
                    ml_prob = predict_score({
                        "scanner_score": cand["score"],
                        "rsi": analysis.get("rsi", 50),
                        "macd_hist": analysis.get("macd_histogram", 0),
                        "volume_trend": analysis.get("volume_trend", 1.0),
                        "volatility": analysis.get("volatility", 5.0),
                        "momentum_5d": analysis.get("momentum_5d", 0),
                        "momentum_20d": analysis.get("momentum_20d", 0),
                        "bollinger_pos": analysis.get("bollinger_pos", 0.5),
                    })
                    if ml_prob is not None:
                        original = cand["score"]
                        # Additive Anpassung um den tuned Threshold:
                        # prob == threshold -> 0 Bonus (neutral)
                        # prob == threshold+0.3 -> +15 Bonus
                        # prob == threshold-0.3 -> -15 Malus
                        ml_bonus = round((ml_prob - ml_threshold) * 50, 1)
                        cand["score"] = round(original + ml_bonus, 1)
                        log.info(f"  ML Score: {cand['symbol']} "
                                 f"{original:.1f} + {ml_bonus:+.1f} = {cand['score']:.1f} "
                                 f"(prob={ml_prob:.2f}, t={ml_threshold:.2f})")
                # Re-sort and re-filter after ML adjustment
                buy_candidates = [c for c in buy_candidates if c["score"] >= min_score]
                buy_candidates.sort(key=lambda x: x["score"], reverse=True)
            else:
                log.debug("  ML Scoring: Model nicht trainiert, verwende fixe Scores")
        except Exception as e:
            log.debug(f"  ML Scoring nicht verfuegbar: {e}")

    available_slots = max_positions - len(positions) + len(
        [t for t in trades_executed if t["action"] == "SCANNER_SELL"])
    if available_slots <= 0 or effective_cash < 100:
        log.info(f"  Keine Slots oder Cash fuer neue Trades")
    else:
        top_buys = buy_candidates[:min(available_slots, 5)]
        if top_buys:
            total_score = sum(max(b["score"], 1) for b in top_buys)
            # v15: Budget nutzt effective_cash (bei aktivem DCA gestaffelt).
            budget = min(effective_cash * 0.7, max_trade * len(top_buys))

            for candidate in top_buys:
                if effective_cash < 100:
                    break

                symbol = candidate["symbol"]
                asset_class = candidate["class"]
                analysis = candidate.get("analysis", {})

                # Asset Filter Check
                if af:
                    allowed, filter_reasons = af.apply_asset_filters(
                        symbol, asset_class, analysis, config)
                    if not allowed:
                        log.info(f"  SKIP {symbol}: {'; '.join(filter_reasons)}")
                        continue

                # Earnings Blackout Check (Aktien)
                if asset_class == "stocks" and ec:
                    blackout, blackout_reason = ec.is_earnings_blackout(symbol, config)
                    if blackout:
                        log.info(f"  EARNINGS BLACKOUT: {symbol} — {blackout_reason}")
                        continue

                # Sentiment Filter
                mc_config = config.get("market_context", {})
                if sent and mc_config.get("use_sentiment_filter", False):
                    sent_result = sent.get_sentiment(symbol)
                    sent_threshold = mc_config.get("sentiment_block_threshold", -0.5)
                    if sent_result["score"] < sent_threshold:
                        log.info(f"  NEGATIVE SENTIMENT: {symbol} "
                                 f"(Score={sent_result['score']:+.2f}, "
                                 f"Threshold={sent_threshold})")
                        continue

                # Earnings Surprise Score Adjustment
                if asset_class == "stocks" and ec:
                    from app.events_calendar import adjust_score_for_earnings
                    adjusted_score = adjust_score_for_earnings(symbol, candidate["score"])
                    if adjusted_score != candidate["score"]:
                        log.info(f"  Earnings-Anpassung {symbol}: "
                                 f"{candidate['score']:+.1f} -> {adjusted_score:+.1f}")
                        candidate["score"] = adjusted_score
                        if candidate["score"] < min_score:
                            log.info(f"  SKIP {symbol}: Score nach Earnings-Anpassung "
                                     f"unter Minimum ({candidate['score']:.1f} < {min_score})")
                            continue

                # Hedging: Defensive Sektoren bevorzugen
                if hdg and hedge_result.get("hedge_needed"):
                    candidate_sector = analysis.get("sector", "")
                    if candidate_sector and not hdg.is_defensive_sector(candidate_sector, config):
                        # Non-defensive sectors get extra reduction in bear regime
                        log.info(f"  Hedging: {symbol} nicht-defensiver Sektor '{candidate_sector}'")

                # Betrag nach Score-Gewichtung
                weight = max(candidate["score"], 1) / total_score
                # v15: Cap pro Trade via effective_cash (DCA) und max_trade (Prozent-Sizing).
                amount = round(min(budget * weight, max_trade, effective_cash * 0.3), 2)

                # Market Context Multiplikator
                amount = round(amount * ctx_multiplier, 2)

                # Konzentrations-Penalty: Reduziere Positionsgroesse bei hoher Konzentration
                risk_cfg_conc = config.get("risk_management", {})
                if risk_cfg_conc.get("concentration_penalty_enabled", False) and rm:
                    conc_threshold = risk_cfg_conc.get("concentration_threshold", 70)
                    conc_reduction = risk_cfg_conc.get("concentration_size_reduction", 0.7)
                    conc_score = rm.get_portfolio_concentration_score(parsed_positions, config)
                    if conc_score > conc_threshold:
                        amount = round(amount * conc_reduction, 2)
                        log.info(f"  Konzentrations-Penalty: Score {conc_score:.0f} > {conc_threshold} "
                                 f"-> Positionsgroesse x{conc_reduction}")

                # Asset-spezifische Groessen-Anpassung
                if af:
                    amount = round(amount * af.get_position_size_adjustment(symbol, asset_class), 2)

                # Leverage berechnen
                volatility = analysis.get("volatility", 3)
                brain_state = load_json("brain_state.json") or {}
                market_regime = brain_state.get("market_regime", "unknown")

                if lm:
                    leverage = lm.calculate_optimal_leverage(
                        asset_class, symbol, volatility, candidate["score"],
                        market_regime,
                        mc.get_current_context().get("vix_level") if mc else None,
                        config)
                else:
                    leverage = 1
                    if asset_class == "forex":
                        leverage = 2
                    elif asset_class == "indices":
                        leverage = 2

                # Risk Manager: Position Sizing
                if rm:
                    max_size = rm.calculate_leveraged_position_size(
                        total_value, stop_loss_pct, leverage, config)
                    amount = min(amount, max_size)

                # v12: Kelly Sizing (bevorzugt) oder Dynamic Sizing
                if rm and config.get("kelly_sizing", {}).get("enabled", False):
                    kelly_size = rm.calculate_kelly_position_size(
                        total_value, stop_loss_pct, candidate["score"], config)
                    amount = min(amount, kelly_size)
                elif rm and config.get("risk_management", {}).get("dynamic_sizing_enabled", False):
                    dynamic_size = rm.calculate_dynamic_position_size(
                        total_value, stop_loss_pct, candidate["score"], config)
                    amount = min(amount, dynamic_size)

                # Recovery Mode Restrictions
                if recovery_active:
                    amount = round(amount * recovery_restrictions.get("position_size_multiplier", 0.5), 2)
                    max_lev = recovery_restrictions.get("max_leverage", 1)
                    if leverage > max_lev:
                        leverage = max_lev

                # Risk Manager: Pre-Trade Validation
                if rm:
                    # Enrich positions with asset_class + sector for correlation check
                    enriched = []
                    for p in parsed_positions:
                        ep = dict(p)
                        ep["asset_class"] = _lookup_asset_class(p["instrument_id"])
                        ep["sector"] = _lookup_sector(p["instrument_id"])
                        enriched.append(ep)

                    # Sektor des neuen Kandidaten
                    candidate_sector = analysis.get("sector", "") or _lookup_sector(candidate["etoro_id"])

                    allowed, reasons = rm.validate_trade(
                        total_value, amount, leverage, asset_class,
                        enriched, stop_loss_pct, config, sector=candidate_sector)
                    if not allowed:
                        log.info(f"  RISK BLOCK {symbol}: {'; '.join(reasons)}")
                        continue

                if amount < 50:
                    continue

                # Kosten-Filter: Trade muss nach Kosten profitabel sein
                try:
                    from app.optimizer import calculate_min_expected_return, get_asset_class_params
                    min_return = config.get("min_expected_return_pct", 0)
                    if min_return <= 0:
                        min_return = calculate_min_expected_return()
                    tp_check = dt_config.get("take_profit_pct", 5)
                    # Asset-Klassen-spezifische SL/TP
                    ac_params = get_asset_class_params(config)
                    if asset_class in ac_params:
                        ac = ac_params[asset_class]
                        stop_loss_pct = ac.get("sl_pct", stop_loss_pct)
                        tp_check = ac.get("tp_pct", tp_check)
                    # Erwarteter Return = WinRate * TP - (1-WinRate) * |SL|
                    # Vereinfacht: TP muss > min_return sein
                    if tp_check < min_return:
                        log.info(f"  SKIP {symbol}: TP {tp_check}% < min {min_return}% (Kosten-Filter)")
                        continue
                except ImportError:
                    pass

                # Risk/Reward Check
                if lm:
                    entry_price = analysis.get("price", 0)
                    if entry_price > 0:
                        sl_price = entry_price * (1 + stop_loss_pct / 100)
                        tp_price = entry_price * (1 + dt_config.get("take_profit_pct", 5) / 100)
                        rr_ok, rr_ratio, rr_reason = lm.check_risk_reward(
                            entry_price, sl_price, tp_price,
                            config.get("leverage", {}).get("min_risk_reward_ratio", 2.0))
                        if not rr_ok:
                            log.info(f"  SKIP {symbol}: {rr_reason}")
                            continue

                # v12: Meta-Labeling Gate (Shadow- oder Live-Mode)
                ml_decision = None
                try:
                    from app import meta_labeler
                    signal_ctx = {
                        "scanner_score": candidate["score"],
                        "rsi": analysis.get("rsi"),
                        "macd_hist": analysis.get("macd_histogram"),
                        "momentum_5d": analysis.get("momentum_5d"),
                        "momentum_20d": analysis.get("momentum_20d"),
                        "volatility": volatility,
                        "volume_trend": analysis.get("volume_trend"),
                        "market_regime": market_regime,
                        "vix_level": mc.get_current_context().get("vix_level") if mc else None,
                        "fear_greed": mc.get_current_context().get("fear_greed_index") if mc else None,
                        "sector": analysis.get("sector") or _lookup_sector(candidate["etoro_id"]),
                        "asset_class": asset_class,
                    }
                    ml_decision = meta_labeler.meta_predict(signal_ctx, config)
                    if ml_decision["decision"] == "skip":
                        log.info(f"  META-SKIP {symbol}: {ml_decision['reason']}")
                        continue
                    log.info(f"  META: {ml_decision['reason']} -> "
                             f"{ml_decision['decision']}")
                except Exception as e:
                    log.debug(f"Meta-Labeler exception (non-fatal): {e}")

                log.info(f"  SCANNER BUY: {symbol} ({asset_class}) "
                         f"${amount:,.2f} {leverage}x "
                         f"(Score={candidate['score']:+.1f}, {candidate['signal']})")

                start_time = time.time()
                result = client.buy(candidate["etoro_id"], amount, leverage=leverage)

                if ex and result:
                    ex.track_execution(
                        analysis.get("price"), result, candidate["etoro_id"],
                        "SCANNER_BUY", amount, asset_class, start_time)

                if result:
                    order = result.get("orderForOpen", {})
                    # v37q: position_id fuer Meta-Labeler-Match (IBKR conId / eToro orderID)
                    contract_info = result.get("_contract") or {}
                    pid = contract_info.get("conId") or order.get("orderID")
                    trade_entry = {
                        "timestamp": datetime.now().isoformat(),
                        "action": "SCANNER_BUY",
                        "symbol": symbol,
                        "name": candidate["name"],
                        "instrument_id": candidate["etoro_id"],
                        "position_id": str(pid) if pid is not None else None,
                        "asset_class": asset_class,
                        "amount_usd": amount,
                        "leverage": leverage,
                        "order_id": order.get("orderID"),
                        "scanner_score": candidate["score"],
                        "signal": candidate["signal"],
                        "rsi": analysis.get("rsi"),
                        "momentum_5d": analysis.get("momentum_5d"),
                        "volatility": volatility,
                        "market_regime": market_regime,
                        "vix_level": mc.get_current_context().get("vix_level") if mc else None,
                        "ctx_multiplier": ctx_multiplier,
                        "status": "executed",
                    }

                    if lm:
                        trade_entry = lm.log_leverage_trade(trade_entry, total_value)

                    save_trade(_attach_fill_prices(trade_entry, result))
                    trades_executed.append(trade_entry)
                    credit -= amount
                    effective_cash -= amount

                    # v36 — Cooldown-State updaten: jeder Buy wird notiert,
                    # damit das Symbol fuer cooldown_cycles nicht erneut
                    # gekauft wird (auch wenn Broker keine Position bestaetigt).
                    sym_key = str(candidate["etoro_id"])
                    prev = cooldown_state.get(sym_key, {})
                    cooldown_state[sym_key] = {
                        "symbol": symbol,
                        "last_attempt": datetime.now().isoformat(),
                        "attempts": prev.get("attempts", 0) + 1,
                        "last_amount": amount,
                    }

                    # v12: Meta-Labeler Shadow-Log (matched later via position_id)
                    if ml_decision is not None and ml_decision.get("p_win") is not None:
                        try:
                            from app import meta_labeler
                            meta_labeler.log_shadow_decision({
                                "timestamp": datetime.now().isoformat(),
                                "position_id": order.get("positionID") or order.get("positionId"),
                                "order_id": order.get("orderID"),
                                "instrument_id": candidate["etoro_id"],
                                "symbol": symbol,
                                "p_win": ml_decision["p_win"],
                                "threshold": ml_decision.get("threshold"),
                                "decision": ml_decision["decision"],
                                "shadow_mode": ml_decision.get("shadow_mode", True),
                                "scanner_score": candidate["score"],
                                "market_regime": market_regime,
                            })
                        except Exception as e:
                            log.debug(f"Shadow-Log fehlgeschlagen: {e}")

                    if al:
                        al.alert_trade_executed(trade_entry)

    # v15: DCA-Budget konsumieren — einmal pro Scheduler-Zyklus dekrementieren.
    # Der Spent-Betrag summiert alle SCANNER_BUY dieses Zyklus (SELL-Trades
    # geben Cash frei und zaehlen nicht gegen die Staffel).
    if rm and dca_info and dca_info.get("dca_active"):
        spent_this_cycle = sum(
            t.get("amount_usd", 0) for t in trades_executed
            if t.get("action") == "SCANNER_BUY"
        )
        try:
            rm.consume_dca_budget(spent_this_cycle)
        except Exception as e:
            log.warning(f"DCA-Budget Update fehlgeschlagen: {e}", exc_info=True)

    # v36 — Cooldown-State persistieren
    try:
        from app.config_manager import save_json as _save_json
        _save_json("buy_cooldown.json", cooldown_state)
    except Exception as e:
        log.warning(f"Cooldown-State save fehlgeschlagen: {e}")

    log.info(f"\n  Scanner-Trades: {len(trades_executed)} ausgefuehrt")
    return trades_executed


def _resolve_meta_for_id(instrument_id):
    """v36f: Universal-Lookup eToro-ID ODER IBKR-conId -> ASSET_UNIVERSE meta.

    Risk/Sector-Lookup kannte vorher nur eToro-IDs. Mit IBKR-Migration
    haben Positionen jetzt conIds (z.B. 290651477 fuer ROKU). Wir
    konsultieren ibkr_contract_cache.json fuer Reverse-Lookup conId
    -> etoro_id, dann ASSET_UNIVERSE.
    """
    try:
        from app.market_scanner import ASSET_UNIVERSE
    except ImportError:
        return None
    iid = int(instrument_id) if instrument_id is not None else None
    if iid is None:
        return None
    # Direkt: etoro_id matcht
    for symbol, info in ASSET_UNIVERSE.items():
        if int(info.get("etoro_id", -1)) == iid:
            return info
    # IBKR-conId Reverse-Lookup
    try:
        from app.config_manager import load_json
        cache = load_json("ibkr_contract_cache.json") or {}
        for etoro_id_str, entry in cache.items():
            if isinstance(entry, dict) and int(entry.get("conId", -1)) == iid:
                etoro_id = int(etoro_id_str)
                for symbol, info in ASSET_UNIVERSE.items():
                    if int(info.get("etoro_id", -1)) == etoro_id:
                        return info
    except Exception:
        pass
    return None


def _lookup_asset_class(instrument_id):
    """Finde Asset-Klasse fuer eine Instrument-ID (eToro-ID oder IBKR-conId)."""
    meta = _resolve_meta_for_id(instrument_id)
    return meta.get("class", "stocks") if meta else "stocks"


def _lookup_sector(instrument_id):
    """Finde Sektor fuer eine Instrument-ID (eToro-ID oder IBKR-conId)."""
    meta = _resolve_meta_for_id(instrument_id)
    return meta.get("sector", "") if meta else ""


# ============================================================
# OVERNIGHT / WEEKEND CHECKS
# ============================================================

def check_overnight_positions(client, config):
    """Pruefe und schliesse ggf. Overnight-Risiko-Positionen."""
    rm = _import_risk_manager()
    if not rm:
        return []

    portfolio = client.get_portfolio()
    if not portfolio:
        return []

    positions = portfolio.get("positions", [])
    parsed = []
    for pos in positions:
        p = EtoroClient.parse_position(pos)
        p["asset_class"] = _lookup_asset_class(p["instrument_id"])
        parsed.append(p)

    # Overnight Risk
    to_close = rm.check_overnight_risk(parsed, config)

    # Freitag: Weekend-Gebuehren Check
    if datetime.now().weekday() == 4:
        weekend_close = rm.check_weekend_fee_impact(parsed, config)
        to_close.extend(weekend_close)

    closed = []
    for pos in to_close:
        result = client.close_position(pos["position_id"], pos.get("instrument_id"))
        if result:
            trade_entry = {
                "timestamp": datetime.now().isoformat(),
                "action": "OVERNIGHT_CLOSE",
                "instrument_id": pos["instrument_id"],
                "position_id": pos["position_id"],
                "pnl_pct": pos.get("pnl_pct", 0),
                "reason": pos.get("reason", "Overnight-Risiko"),
                "status": "executed",
            }
            save_trade(_attach_fill_prices(trade_entry, result))
            closed.append(trade_entry)
            log.info(f"  Overnight Close: #{pos['instrument_id']} ({pos.get('reason', '')})")
        else:
            _log_close_failure("OVERNIGHT_CLOSE", pos, None, extra={
                "reason": pos.get("reason", "Overnight-Risiko"),
            })

    return closed


# ============================================================
# HAUPTFUNKTION: TRADING-ZYKLUS (v2)
# ============================================================

def run_trading_cycle():
    """Kompletter Trading-Zyklus mit allen Safety-Checks."""
    log.info("=" * 55)
    log.info("InvestPilot Trading-Zyklus startet...")
    log.info(f"Zeit: {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    log.info("=" * 55)

    config = load_config()
    client = get_broker(config)  # broker-agnostic: 'etoro' (default) oder 'ibkr' aus config.json

    if not client.configured:
        log.error("Broker '%s' nicht konfiguriert!", client.broker_name)
        return
    log.info("Trading-Cycle mit Broker '%s'", client.broker_name)

    # Broker-Health-Check mit Telegram-Alert bei Connection-Lost (state-deduped)
    try:
        from app.alerts import check_broker_health
        if not check_broker_health(client, config):
            log.error("Broker-Healthcheck fehlgeschlagen — Cycle wird uebersprungen")
            return
    except Exception as e:
        log.warning("Broker-Healthcheck nicht verfuegbar: %s", e)

    # Module laden
    rm = _import_risk_manager()
    mc = _import_market_context()
    al = _import_alerts()

    # Heartbeat fuer Watchdog
    if al:
        al.update_heartbeat()

    # Telegram Commands pruefen (Kill Switch etc.)
    if al:
        commands = al.check_telegram_commands(config)
        if commands:
            for cmd in commands:
                if cmd["command"] == "killswitch":
                    log.warning("TELEGRAM KILL SWITCH empfangen!")
                    if rm:
                        result = rm.emergency_close_all(client, "Telegram Kill Switch")
                        al.alert_emergency("Telegram Kill Switch", result.get("closed", 0))
                    return
                elif cmd["command"] == "status":
                    portfolio = client.get_portfolio()
                    if portfolio:
                        credit = portfolio.get("credit", 0)
                        pnl = portfolio.get("unrealizedPnL", 0)
                        al.send_alert(f"Portfolio: ${credit + pnl:,.2f}\nP/L: ${pnl:+,.2f}", "INFO")

    # Brain importieren
    try:
        from app.brain import run_brain_cycle
        brain_available = True
    except ImportError:
        brain_available = False

    # Scanner importieren
    try:
        from app.market_scanner import scan_all_assets
        scanner_available = True
    except ImportError:
        scanner_available = False

    # 0. Risk Manager: Drawdown-Check
    if rm:
        dd_ok, dd_reason = rm.check_drawdown_limits()
        if not dd_ok:
            log.warning(f"TRADING PAUSIERT: {dd_reason}")
            if al:
                risk_state = rm.get_risk_summary()
                al.alert_drawdown(
                    risk_state.get("daily_pnl_pct", 0),
                    risk_state.get("weekly_pnl_pct", 0), dd_reason)
            return

    # 1. Status anzeigen
    show_portfolio_status(client)

    # 2. Market Context aktualisieren (1x pro Stunde)
    if mc:
        ctx = mc.get_current_context()
        last_update = ctx.get("last_update", "")
        try:
            last_dt = datetime.fromisoformat(last_update)
            if (datetime.now() - last_dt).total_seconds() > 3600:
                mc.update_full_context(config)
        except (ValueError, TypeError):
            mc.update_full_context(config)

    # 3. Config neu laden
    config = load_config()

    # 4. Market Scan (alle 6 Zyklen)
    scan_results = None
    dt_config = config.get("demo_trading", {})
    scan_interval = dt_config.get("scan_interval_cycles", 6)

    if scanner_available:
        from app.config_manager import load_json as _lj, save_json as _sj
        scan_state = _lj("scanner_state.json") or {"cycle_count": 0, "last_results": []}
        scan_state["cycle_count"] = scan_state.get("cycle_count", 0) + 1

        # v36 — Stale-Cache-Gate: cached scan-results NUR nutzen wenn jung
        # genug. Fix fuer ROKU-Bug am 27.04. (identische RSI/Momentum ueber
        # hunderte Cycles → veraltete Marktdaten generierten dauerhaft den
        # gleichen Buy-Score 58.8 fuer ROKU).
        max_cache_age_min = dt_config.get("scan_max_cache_age_minutes", 15)
        last_scan_iso = scan_state.get("last_scan")
        cache_too_old = True
        if last_scan_iso:
            try:
                age_min = (datetime.now() - datetime.fromisoformat(last_scan_iso)).total_seconds() / 60
                cache_too_old = age_min >= max_cache_age_min
            except Exception:
                cache_too_old = True

        if (scan_state["cycle_count"] >= scan_interval
                or not scan_state.get("last_results")
                or cache_too_old):
            log.info("\n--- Market Scan wird ausgefuehrt ---")
            enabled_classes = dt_config.get("enabled_asset_classes",
                                            ["stocks", "etf", "crypto", "commodities", "forex", "indices"])
            scan_results = scan_all_assets(enabled_classes=enabled_classes)
            scan_state["cycle_count"] = 0
            scan_state["last_results"] = scan_results
            scan_state["last_scan"] = datetime.now().isoformat()
            _sj("scanner_state.json", scan_state)
        else:
            log.info(f"\n  Scanner: Gespeicherte Ergebnisse "
                     f"(naechster Scan in {scan_interval - scan_state['cycle_count']} Zyklen, "
                     f"cache {age_min:.0f}min alt)")
            scan_results = scan_state.get("last_results", [])

    # 5. Portfolio aufbauen oder Scanner-Trades
    portfolio = client.get_portfolio()
    if portfolio:
        positions = portfolio.get("positions", [])
        if len(positions) == 0 and not scan_results:
            log.info("\nPortfolio leer - baue initiales Portfolio auf...")
            build_initial_portfolio(client, config)
        elif scan_results:
            execute_scanner_trades(client, config, scan_results)
        else:
            rebalance_portfolio(client, config)

    # 6. Stop-Loss / Take-Profit
    check_stop_loss_take_profit(client, config)

    # 6b. Trailing SL State bereinigen (geschlossene Positionen entfernen)
    lm = _import_leverage_manager()
    if lm:
        portfolio_for_cleanup = client.get_portfolio()
        if portfolio_for_cleanup:
            open_ids = [
                EtoroClient.parse_position(pos)["position_id"]
                for pos in portfolio_for_cleanup.get("positions", [])
            ]
            lm.cleanup_trailing_state(open_ids)

    # 7. Overnight Check (abends)
    now = datetime.now()
    if now.hour >= 21:
        check_overnight_positions(client, config)

    # 8. Margin Safety / Auto-Deleverage
    if rm:
        portfolio = client.get_portfolio()
        if portfolio:
            parsed = [EtoroClient.parse_position(pos) for pos in portfolio.get("positions", [])]
            total = portfolio.get("credit", 0) + sum(p["invested"] for p in parsed)
            margin_ok, margin_reason, exposure = rm.check_margin_safety(total, parsed, config)
            if not margin_ok:
                log.warning(f"  MARGIN ALERT: {margin_reason}")
                rm.auto_deleverage(client, parsed, total, config)

    # 9. Finaler Status
    log.info("")
    final_portfolio = show_portfolio_status(client)

    # 10. Brain: Lernen & Optimieren
    if brain_available and final_portfolio:
        report = run_brain_cycle(final_portfolio)
        log.info("")
        log.info("=" * 55)
        log.info("BRAIN ZUSAMMENFASSUNG")
        log.info(f"  Lauf:      #{report.get('total_runs', '?')}")
        log.info(f"  Rendite:   {report.get('total_return_pct', 0):+.2f}%")
        log.info(f"  Win-Rate:  {report.get('win_rate', 0):.1f}%")
        log.info(f"  Sharpe:    {report.get('sharpe_estimate', 0):.2f}")
        log.info(f"  Regime:    {report.get('market_regime', '?')}")
        log.info(f"  Regeln:    {report.get('active_rules', 0)} aktiv")
        log.info("=" * 55)

    # 11. Daily Summary (21:00)
    if al and al.should_send_daily_summary():
        if final_portfolio and rm:
            risk = rm.get_risk_summary()
            total = final_portfolio.get("credit", 0)
            for pos in final_portfolio.get("positions", []):
                total += EtoroClient.parse_position(pos)["invested"]
            brain_state = load_json("brain_state.json") or {}
            trades_today = risk.get("daily_trades", 0)
            al.send_daily_summary(
                total, risk["daily_pnl_pct"], risk["daily_pnl_usd"],
                trades_today, brain_state.get("market_regime", "?"))

    # Cloud-Backup nach jedem Zyklus
    try:
        from app.persistence import backup_to_cloud
        backup_to_cloud()
    except Exception as e:
        log.warning(f"Cloud-Backup fehlgeschlagen: {e}")

    log.info("")
    log.info("Trading-Zyklus beendet.")
    log.info("=" * 55)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    run_trading_cycle()
