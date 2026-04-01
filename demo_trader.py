"""
InvestPilot v2 - Demo Trading Modul (Selbstlernend)
Handelt automatisch auf dem eToro Demo-Konto ($100'000 virtuell).
Lernt aus vergangenen Trades und optimiert Strategie kontinuierlich.

Features:
  1. Portfolio nach Ziel-Allokation aufbauen
  2. Rebalancing wenn Abweichung > Schwellenwert
  3. Stop-Loss / Take-Profit Management
  4. Vollstaendiges Logging aller Trades
  5. [NEU] Trade Brain: Selbstlernendes Analyse-Modul
     - Performance-Tracking & Snapshots
     - Instrument-Scoring & Ranking
     - Marktregime-Erkennung (Bull/Bear/Sideways)
     - Automatische Regel-Ableitung
     - Strategie-Optimierung nach jedem Lauf

eToro Demo API: https://public-api.etoro.com/api/v1
"""

import json
import sys
import logging
import uuid
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    print("pip install requests")
    sys.exit(1)

SCRIPT_DIR = Path(__file__).parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
LOG_PATH = SCRIPT_DIR / "demo_trader.log"
TRADE_LOG = SCRIPT_DIR / "trade_history.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("DemoTrader")


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_trade(trade_entry):
    """Trade-Historie persistent speichern."""
    history = []
    if TRADE_LOG.exists():
        with open(TRADE_LOG, "r", encoding="utf-8") as f:
            history = json.load(f)
    history.append(trade_entry)
    with open(TRADE_LOG, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)


# ============================================================
# eTORO DEMO API CLIENT
# ============================================================

class EtoroDemoClient:
    """Client fuer eToro Demo Trading API."""

    def __init__(self, config):
        etoro = config.get("etoro", {})
        self.base_url = etoro.get("base_url", "https://public-api.etoro.com/api/v1")
        self.public_key = etoro.get("public_key", "")
        self.private_key = etoro.get("demo_private_key", "")

        if not self.public_key or not self.private_key:
            log.error("Demo API Keys fehlen in config.json!")
            self.configured = False
        else:
            self.configured = True

    def _headers(self):
        return {
            "x-api-key": self.public_key,
            "x-user-key": self.private_key,
            "x-request-id": str(uuid.uuid4()),
            "Content-Type": "application/json",
        }

    def _get(self, endpoint):
        url = f"{self.base_url}{endpoint}"
        try:
            resp = requests.get(url, headers=self._headers(), timeout=30)
            if resp.status_code == 200:
                return resp.json()
            log.error(f"GET {endpoint}: {resp.status_code} - {resp.text[:200]}")
            return None
        except Exception as e:
            log.error(f"GET {endpoint}: {e}")
            return None

    def _post(self, endpoint, payload):
        url = f"{self.base_url}{endpoint}"
        try:
            resp = requests.post(url, headers=self._headers(), json=payload, timeout=30)
            if resp.status_code == 200:
                return resp.json()
            log.error(f"POST {endpoint}: {resp.status_code} - {resp.text[:300]}")
            return None
        except Exception as e:
            log.error(f"POST {endpoint}: {e}")
            return None

    # --- Portfolio ---
    def get_portfolio(self):
        """Demo-Portfolio laden."""
        data = self._get("/trading/info/demo/pnl")
        if not data:
            return None
        return data.get("clientPortfolio", data)

    # --- Trading ---
    def buy(self, instrument_id, amount_usd, leverage=1, stop_loss=0, take_profit=0):
        """Kauf-Order (Market, by Amount)."""
        payload = {
            "InstrumentID": instrument_id,
            "Amount": amount_usd,
            "IsBuy": True,
            "Leverage": leverage,
            "StopLossRate": stop_loss,
            "TakeProfitRate": take_profit,
            "IsTslEnabled": False,
        }
        log.info(f"  BUY: InstrumentID={instrument_id}, Amount=${amount_usd}, Leverage={leverage}x")
        result = self._post("/trading/execution/demo/market-open-orders/by-amount", payload)
        if result:
            order = result.get("orderForOpen", {})
            log.info(f"  -> Order OK: ID={order.get('orderID')}, Status={order.get('statusID')}")
        return result

    def sell(self, instrument_id, amount_usd, leverage=1):
        """Sell/Short-Order (Market, by Amount)."""
        payload = {
            "InstrumentID": instrument_id,
            "Amount": amount_usd,
            "IsBuy": False,
            "Leverage": leverage,
            "IsTslEnabled": False,
        }
        log.info(f"  SELL: InstrumentID={instrument_id}, Amount=${amount_usd}")
        return self._post("/trading/execution/demo/market-open-orders/by-amount", payload)

    def close_position(self, position_id):
        """Position schliessen."""
        log.info(f"  CLOSE: PositionID={position_id}")
        return self._post(f"/trading/execution/market-close-orders/positions/{position_id}", {})

    def search_instrument(self, query):
        """Instrument suchen."""
        data = self._get(f"/market-data/search?query={query}")
        if not data:
            return []
        results = []
        for item in data.get("items", []):
            if item.get("isHiddenFromClient"):
                continue
            results.append({
                "id": item.get("internalInstrumentId"),
                "name": item.get("internalInstrumentDisplayName"),
                "symbol": item.get("internalSymbolFull"),
                "exchange": item.get("internalExchangeName"),
                "asset_class": item.get("internalAssetClassName"),
            })
        return results


# ============================================================
# TRADING STRATEGIEN
# ============================================================

def build_initial_portfolio(client, config):
    """Portfolio nach Ziel-Allokation aufbauen."""
    log.info("=" * 55)
    log.info("PORTFOLIO AUFBAU (Demo)")
    log.info("=" * 55)

    dt_config = config.get("demo_trading", {})
    targets = dt_config.get("portfolio_targets", {})
    max_trade = dt_config.get("max_single_trade_usd", 5000)
    default_leverage = dt_config.get("default_leverage", 1)

    # Portfolio laden
    portfolio = client.get_portfolio()
    if not portfolio:
        log.error("Konnte Portfolio nicht laden")
        return

    credit = portfolio.get("credit", 0)
    positions = portfolio.get("positions", [])
    log.info(f"  Verfuegbar: ${credit:,.2f}")
    log.info(f"  Positionen: {len(positions)}")

    if credit < 100:
        log.warning("  Zu wenig Credit fuer neue Trades")
        return

    # Bestehende Positionen nach InstrumentID mappen
    existing = {}
    for pos in positions:
        iid = pos.get("instrumentID") or pos.get("instrumentId") or pos.get("InstrumentID")
        invested = pos.get("amount") or pos.get("investedAmount") or pos.get("Amount") or 0
        existing[iid] = existing.get(iid, 0) + invested

    total_portfolio = credit + sum(existing.values())
    log.info(f"  Portfolio-Gesamtwert: ${total_portfolio:,.2f}")

    # Fuer jedes Ziel pruefen ob Kauf noetig
    trades_executed = []
    for symbol, target in targets.items():
        iid = target["instrument_id"]
        target_pct = target["allocation_pct"]
        target_value = total_portfolio * target_pct / 100
        current_value = existing.get(iid, 0)
        diff = target_value - current_value

        log.info(f"  {symbol}: Soll=${target_value:,.0f} Ist=${current_value:,.0f} Diff=${diff:,.0f}")

        if diff > 50:  # Mindestens $50 Differenz
            amount = min(diff, max_trade, credit * 0.9)  # Nie mehr als 90% des Credits
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
        return

    actions = []
    for pos in portfolio.get("positions", []):
        pnl = pos.get("unrealizedPnL", {})
        pnl_val = pnl.get("pnL", 0) if isinstance(pnl, dict) else 0
        invested = pos.get("amount") or pos.get("investedAmount") or 0
        if invested <= 0:
            continue

        pnl_pct = (pnl_val / invested) * 100
        pid = pos.get("positionID") or pos.get("positionId") or pos.get("PositionID")
        iid = pos.get("instrumentID") or pos.get("instrumentId") or pos.get("InstrumentID")

        if pnl_pct <= sl_pct:
            log.warning(f"  STOP-LOSS: Position {pid} (Instrument {iid}) bei {pnl_pct:+.1f}%")
            result = client.close_position(pid)
            action = "STOP_LOSS_CLOSE"
            if result:
                save_trade({
                    "timestamp": datetime.now().isoformat(),
                    "action": action, "instrument_id": iid,
                    "position_id": pid, "pnl_pct": round(pnl_pct, 2),
                    "pnl_usd": round(pnl_val, 2), "status": "executed",
                })
                actions.append(action)

        elif pnl_pct >= tp_pct:
            log.info(f"  TAKE-PROFIT: Position {pid} (Instrument {iid}) bei {pnl_pct:+.1f}%")
            result = client.close_position(pid)
            action = "TAKE_PROFIT_CLOSE"
            if result:
                save_trade({
                    "timestamp": datetime.now().isoformat(),
                    "action": action, "instrument_id": iid,
                    "position_id": pid, "pnl_pct": round(pnl_pct, 2),
                    "pnl_usd": round(pnl_val, 2), "status": "executed",
                })
                actions.append(action)

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

    # Aktuelle Allokation berechnen
    pos_by_instrument = {}
    total_invested = 0
    for pos in positions:
        iid = pos.get("instrumentID") or pos.get("instrumentId") or pos.get("InstrumentID")
        invested = pos.get("amount") or pos.get("investedAmount") or pos.get("Amount") or 0
        pnl = pos.get("unrealizedPnL", {})
        pnl_val = pnl.get("pnL", 0) if isinstance(pnl, dict) else 0
        current_val = invested + pnl_val
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

        log.info(f"  {symbol}: Soll={target_pct}% Ist={current_pct:.1f}% Abw={deviation:+.1f}% [{status}]")

    if needs_rebalance and credit > 100:
        log.info("  -> Rebalancing wird ausgefuehrt...")
        build_initial_portfolio(client, config)
    else:
        log.info("  -> Kein Rebalancing noetig")


def show_portfolio_status(client):
    """Aktuellen Portfolio-Status anzeigen."""
    log.info("=" * 55)
    log.info("DEMO PORTFOLIO STATUS")
    log.info("=" * 55)

    portfolio = client.get_portfolio()
    if not portfolio:
        log.error("Portfolio nicht verfuegbar")
        return

    credit = portfolio.get("credit", 0)
    positions = portfolio.get("positions", [])
    unrealized_pnl = portfolio.get("unrealizedPnL", 0)

    total_invested = sum((p.get("amount") or p.get("investedAmount") or 0) for p in positions)
    total_value = total_invested + unrealized_pnl + credit

    log.info(f"  Credit (Cash):     ${credit:>12,.2f}")
    log.info(f"  Investiert:        ${total_invested:>12,.2f}")
    log.info(f"  Unrealized P/L:    ${unrealized_pnl:>12,.2f}")
    log.info(f"  Gesamtwert:        ${total_value:>12,.2f}")
    log.info(f"  Positionen:        {len(positions)}")

    # Debug: rohe Felder der ersten Position loggen um API-Struktur zu verstehen
    if positions:
        raw_keys = list(positions[0].keys())
        log.info(f"  [DEBUG] API Position-Felder: {raw_keys}")

    for pos in positions:
        iid = (pos.get("instrumentId") or pos.get("InstrumentID") or
               pos.get("instrument_id") or pos.get("instrumentID") or "?")
        invested = (pos.get("investedAmount") or pos.get("Amount") or
                    pos.get("amount") or pos.get("netAmount") or 0)
        pnl = pos.get("unrealizedPnL", {})
        pnl_val = pnl.get("pnL", 0) if isinstance(pnl, dict) else 0
        pnl_pct = (pnl_val / invested * 100) if invested > 0 else 0
        leverage = pos.get("leverage", 1)
        log.info(f"    #{iid}: ${invested:,.0f} -> P/L: ${pnl_val:+,.2f} ({pnl_pct:+.1f}%) {leverage}x")

    return portfolio


# ============================================================
# MAIN
# ============================================================

def main():
    log.info("=" * 55)
    log.info("InvestPilot Demo Trader (Selbstlernend) startet...")
    log.info(f"Zeit: {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    log.info("=" * 55)

    config = load_config()
    client = EtoroDemoClient(config)

    if not client.configured:
        log.error("Demo-Client nicht konfiguriert!")
        sys.exit(1)

    # Trade Brain importieren
    try:
        from trade_brain import run_brain_cycle
        brain_available = True
    except ImportError:
        log.warning("trade_brain.py nicht gefunden - ohne Lernfunktion")
        brain_available = False

    # 1. Status anzeigen
    show_portfolio_status(client)

    # 2. Config neu laden (Brain koennte sie beim letzten Lauf optimiert haben)
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

    # 6. TRADE BRAIN: Lernen & Optimieren
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
    log.info("Demo Trader beendet.")
    log.info("=" * 55)


if __name__ == "__main__":
    main()
