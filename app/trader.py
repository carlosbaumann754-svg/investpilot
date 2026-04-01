"""
InvestPilot - Trading Engine
Automatisches Portfolio-Management: Aufbau, Rebalancing, SL/TP.
Refactored aus demo_trader.py - nutzt unified EtoroClient + ConfigManager.
"""

import logging
from datetime import datetime

from app.config_manager import load_config, save_json, load_json
from app.etoro_client import EtoroClient

log = logging.getLogger("Trader")


def save_trade(trade_entry):
    """Trade-Historie persistent speichern."""
    history = load_json("trade_history.json") or []
    history.append(trade_entry)
    save_json("trade_history.json", history)


def show_portfolio_status(client):
    """Aktuellen Portfolio-Status anzeigen und zurueckgeben."""
    log.info("=" * 55)
    log.info("DEMO PORTFOLIO STATUS")
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

    total_value = total_invested + unrealized_pnl + credit

    log.info(f"  Credit (Cash):     ${credit:>12,.2f}")
    log.info(f"  Investiert:        ${total_invested:>12,.2f}")
    log.info(f"  Unrealized P/L:    ${unrealized_pnl:>12,.2f}")
    log.info(f"  Gesamtwert:        ${total_value:>12,.2f}")
    log.info(f"  Positionen:        {len(positions)}")

    for p in parsed_positions:
        log.info(f"    #{p['instrument_id']}: ${p['invested']:,.0f} -> "
                 f"P/L: ${p['pnl']:+,.2f} ({p['pnl_pct']:+.1f}%) {p['leverage']}x")

    return portfolio


def build_initial_portfolio(client, config):
    """Portfolio nach Ziel-Allokation aufbauen."""
    log.info("=" * 55)
    log.info("PORTFOLIO AUFBAU")
    log.info("=" * 55)

    dt_config = config.get("demo_trading", {})
    targets = dt_config.get("portfolio_targets", {})
    max_trade = dt_config.get("max_single_trade_usd", 5000)
    default_leverage = dt_config.get("default_leverage", 1)

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
                log.info(f"    -> Skip (zu klein: ${amount:.0f})")
                continue

            leverage = target.get("leverage", default_leverage)
            result = client.buy(iid, round(amount, 2), leverage=leverage)
            if result:
                order = result.get("orderForOpen", {})
                trade_entry = {
                    "timestamp": datetime.now().isoformat(),
                    "action": "BUY",
                    "symbol": symbol,
                    "name": target["name"],
                    "instrument_id": iid,
                    "amount_usd": round(amount, 2),
                    "leverage": leverage,
                    "order_id": order.get("orderID"),
                    "status": "executed",
                }
                save_trade(trade_entry)
                trades_executed.append(trade_entry)
                credit -= amount
                log.info(f"    -> GEKAUFT: ${amount:,.2f} (Order: {order.get('orderID')})")
            else:
                log.error(f"    -> FEHLER bei {symbol}")

    log.info(f"\n  {len(trades_executed)} Trades ausgefuehrt")
    return trades_executed


def check_stop_loss_take_profit(client, config):
    """Stop-Loss und Take-Profit pruefen."""
    log.info("=" * 55)
    log.info("STOP-LOSS / TAKE-PROFIT CHECK")
    log.info("=" * 55)

    dt_config = config.get("demo_trading", {})
    sl_pct = dt_config.get("stop_loss_pct", -10)
    tp_pct = dt_config.get("take_profit_pct", 25)

    portfolio = client.get_portfolio()
    if not portfolio:
        return []

    actions = []
    for pos in portfolio.get("positions", []):
        p = EtoroClient.parse_position(pos)
        if p["invested"] <= 0:
            continue

        if p["pnl_pct"] <= sl_pct:
            log.warning(f"  STOP-LOSS: Position {p['position_id']} "
                        f"(Instrument {p['instrument_id']}) bei {p['pnl_pct']:+.1f}%")
            result = client.close_position(p["position_id"])
            if result:
                save_trade({
                    "timestamp": datetime.now().isoformat(),
                    "action": "STOP_LOSS_CLOSE",
                    "instrument_id": p["instrument_id"],
                    "position_id": p["position_id"],
                    "pnl_pct": p["pnl_pct"],
                    "pnl_usd": p["pnl"],
                    "status": "executed",
                })
                actions.append("STOP_LOSS_CLOSE")

        elif p["pnl_pct"] >= tp_pct:
            log.info(f"  TAKE-PROFIT: Position {p['position_id']} "
                     f"(Instrument {p['instrument_id']}) bei {p['pnl_pct']:+.1f}%")
            result = client.close_position(p["position_id"])
            if result:
                save_trade({
                    "timestamp": datetime.now().isoformat(),
                    "action": "TAKE_PROFIT_CLOSE",
                    "instrument_id": p["instrument_id"],
                    "position_id": p["position_id"],
                    "pnl_pct": p["pnl_pct"],
                    "pnl_usd": p["pnl"],
                    "status": "executed",
                })
                actions.append("TAKE_PROFIT_CLOSE")

    log.info(f"  {len(actions)} SL/TP Aktionen")
    return actions


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


def run_trading_cycle():
    """Kompletter Trading-Zyklus (ein Lauf)."""
    log.info("=" * 55)
    log.info("InvestPilot Trading-Zyklus startet...")
    log.info(f"Zeit: {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    log.info("=" * 55)

    config = load_config()
    client = EtoroClient(config)

    if not client.configured:
        log.error("eToro Client nicht konfiguriert!")
        return

    # Brain importieren
    try:
        from app.brain import run_brain_cycle
        brain_available = True
    except ImportError:
        log.warning("Brain nicht verfuegbar")
        brain_available = False

    # 1. Status anzeigen
    show_portfolio_status(client)

    # 2. Config neu laden (Brain koennte sie optimiert haben)
    config = load_config()

    # 3. Portfolio aufbauen/rebalancieren
    portfolio = client.get_portfolio()
    if portfolio:
        positions = portfolio.get("positions", [])
        if len(positions) == 0:
            log.info("\nPortfolio ist leer - baue initiales Portfolio auf...")
            build_initial_portfolio(client, config)
        else:
            rebalance_portfolio(client, config)

    # 4. Stop-Loss / Take-Profit
    check_stop_loss_take_profit(client, config)

    # 5. Finaler Status
    log.info("")
    final_portfolio = show_portfolio_status(client)

    # 6. Brain: Lernen & Optimieren
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

    log.info("")
    log.info("Trading-Zyklus beendet.")
    log.info("=" * 55)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    run_trading_cycle()
