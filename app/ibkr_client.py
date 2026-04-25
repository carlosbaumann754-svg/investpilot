"""
IBKR Client (Stub) — InvestPilot W2 Migration eToro -> Interactive Brokers
=========================================================================

Minimaler Stub fuer die Verbindung zum IB Gateway via ib_insync.

WICHTIG — Port-Architektur (gnzsnz/ib-gateway Image):
    IB Gateway lauscht intern nur auf 127.0.0.1:4002 (strict localhost).
    Ein socat-Daemon im Container exposed Port 0.0.0.0:4004 und forwarded
    zu 127.0.0.1:4002. Damit sieht IBG die Connection als lokal und
    akzeptiert sie.

    -> ib_insync MUSS zu Port 4004 connecten, NICHT 4002.

    Port 4002 ist nur von 127.0.0.1 (Host des Containers) per
    docker-compose Mapping erreichbar (fuer SSH-Tunnel-Use-Case).

Verifiziert: 2026-04-25, Paper-Account DUP108015, server version 176,
alle 3 Data-Farms (usfarm, ushmds, secdefil) grün.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

log = logging.getLogger(__name__)

# Connection-Konstanten (siehe Modul-Docstring fuer Begruendung)
IBG_HOST = os.environ.get("IBG_HOST", "ib-gateway")
IBG_PORT = int(os.environ.get("IBG_PORT", "4004"))  # socat-bridge port, NICHT 4002
IBG_CLIENT_ID = int(os.environ.get("IBG_CLIENT_ID", "1"))
IBG_TIMEOUT = int(os.environ.get("IBG_TIMEOUT", "15"))


def connect(
    host: str = IBG_HOST,
    port: int = IBG_PORT,
    client_id: int = IBG_CLIENT_ID,
    timeout: int = IBG_TIMEOUT,
    readonly: bool = False,
):
    """
    Verbindet sich zum IB Gateway und gibt eine ib_insync.IB Instanz zurueck.

    Standard-Parameter sind so gesetzt dass die Verbindung im Docker-Setup
    out-of-the-box funktioniert (investpilot Container -> ib-gateway Container).

    Args:
        host: Docker-DNS-Name oder IP des IB Gateway (default: 'ib-gateway')
        port: Socat-Bridge-Port (default: 4004 — KEIN 4002!)
        client_id: IB API Client ID (default: 1, jeder Client braucht eindeutige ID)
        timeout: Connection-Timeout in Sekunden (default: 15)
        readonly: True = nur Read-Operations erlaubt (kein Order-Submit)

    Returns:
        ib_insync.IB Instanz, connected.

    Raises:
        TimeoutError wenn Connection nicht innerhalb timeout Sekunden zustande kommt.
        ConnectionRefusedError wenn Port nicht erreichbar.
    """
    from ib_insync import IB

    ib = IB()
    log.info(
        "Connecting to IB Gateway at %s:%d (clientId=%d, readonly=%s)",
        host, port, client_id, readonly,
    )
    ib.connect(host, port, clientId=client_id, timeout=timeout, readonly=readonly)
    log.info(
        "Connected: server v%d, accounts=%s",
        ib.client.serverVersion(), ib.managedAccounts(),
    )
    return ib


def healthcheck() -> dict:
    """
    Schneller Connectivity-Check ohne Side-Effects. Connected -> Disconnected.

    Returns:
        {"ok": bool, "server_version": Optional[int], "accounts": list[str],
         "server_time": Optional[str], "error": Optional[str]}
    """
    try:
        ib = connect(readonly=True)
        try:
            return {
                "ok": True,
                "server_version": ib.client.serverVersion(),
                "accounts": ib.managedAccounts(),
                "server_time": str(ib.reqCurrentTime()),
                "error": None,
            }
        finally:
            ib.disconnect()
    except Exception as e:
        return {
            "ok": False,
            "server_version": None,
            "accounts": [],
            "server_time": None,
            "error": f"{type(e).__name__}: {e}",
        }


## --------------------------------------------------------------------
## IbkrBroker — BrokerBase-Implementierung
## --------------------------------------------------------------------

from app.broker_base import BrokerBase


class IbkrBroker(BrokerBase):
    """
    IBKR-Implementierung des BrokerBase-Interfaces.

    Status (W2):
    - Read-Operations (Portfolio, Equity, Cash, P/L) sind LIVE und gegen das
      Paper-Account DUP108015 verifiziert
    - Write-Operations (buy/sell/close_position) sind TODO-Stubs — werfen
      `NotImplementedError` mit klarer Begruendung
    - Reason: eToro `buy(amount_usd)` vs IBKR `placeOrder(Contract, qty)` ist
      asymmetrisch — Conversion `qty = amount_usd / price` braucht Live-Quote
      und Contract-Resolution. Das wird in W3 ausgebaut.

    Strategy: Lazy-Connection — `IB`-Instanz wird erst beim ersten Read
    erstellt und bleibt offen, bis `disconnect()` explizit aufgerufen wird.
    Connection-Pool kuendigt sich von selbst nach 60s Idle (IBG-Default).
    """

    def __init__(self, config: Optional[dict] = None):
        ibkr_cfg = (config or {}).get("ibkr", {}) if config else {}
        self.host = ibkr_cfg.get("host") or IBG_HOST
        self.port = int(ibkr_cfg.get("port") or IBG_PORT)
        self.client_id = int(ibkr_cfg.get("client_id") or IBG_CLIENT_ID)
        self.timeout = int(ibkr_cfg.get("timeout") or IBG_TIMEOUT)
        self.readonly = bool(ibkr_cfg.get("readonly", False))
        self._ib = None  # ib_insync.IB instance, lazy
        self.configured = True  # IBKR braucht keine API-Keys, nur Container-Reachability

    @property
    def broker_name(self) -> str:
        return "ibkr"

    # --- Connection-Lifecycle ---

    def _get_ib(self):
        """Lazy-init der IB-Instanz."""
        if self._ib is None or not self._ib.isConnected():
            self._ib = connect(
                host=self.host,
                port=self.port,
                client_id=self.client_id,
                timeout=self.timeout,
                readonly=self.readonly,
            )
        return self._ib

    def disconnect(self) -> None:
        """Verbindung schliessen (manuell aufrufen wenn fertig)."""
        if self._ib is not None and self._ib.isConnected():
            self._ib.disconnect()
        self._ib = None

    # --- Read-Operations (LIVE) ---

    def get_portfolio(self) -> Optional[dict]:
        """
        Portfolio-Snapshot im eToro-kompatiblen Format.

        Returns:
            {"positions": [...], "aggregatedPositions": [...], "creditByRealizedEquity": ...}
        """
        try:
            ib = self._get_ib()
            positions = ib.positions()
            account_values = {av.tag: av for av in ib.accountValues() if av.currency in ("USD", "BASE")}

            mapped_positions = []
            for p in positions:
                mapped_positions.append({
                    "instrumentID": getattr(p.contract, "conId", None),
                    "symbol": getattr(p.contract, "symbol", None),
                    "amount": float(p.position) * float(p.avgCost),
                    "positionID": str(getattr(p.contract, "conId", "")),
                    "leverage": 1,
                    "openRate": float(p.avgCost),
                    "isBuy": p.position > 0,
                })

            equity = float(account_values["NetLiquidation"].value) if "NetLiquidation" in account_values else 0.0
            cash = float(account_values["AvailableFunds"].value) if "AvailableFunds" in account_values else 0.0

            return {
                "positions": mapped_positions,
                "aggregatedPositions": [],
                "creditByRealizedEquity": equity,
                "availableCash": cash,
                "_broker": "ibkr",
            }
        except Exception as e:
            log.error("get_portfolio failed: %s", e)
            return None

    def get_equity(self) -> Optional[float]:
        try:
            ib = self._get_ib()
            for av in ib.accountValues():
                if av.tag == "NetLiquidation" and av.currency in ("USD", "BASE"):
                    return float(av.value)
            return None
        except Exception as e:
            log.error("get_equity failed: %s", e)
            return None

    def get_available_cash(self) -> Optional[float]:
        try:
            ib = self._get_ib()
            for av in ib.accountValues():
                if av.tag == "AvailableFunds" and av.currency in ("USD", "BASE"):
                    return float(av.value)
            return None
        except Exception as e:
            log.error("get_available_cash failed: %s", e)
            return None

    def get_total_invested(self) -> Optional[float]:
        try:
            ib = self._get_ib()
            for av in ib.accountValues():
                if av.tag == "GrossPositionValue" and av.currency in ("USD", "BASE"):
                    return float(av.value)
            return None
        except Exception as e:
            log.error("get_total_invested failed: %s", e)
            return None

    def get_pnl(self) -> Optional[dict]:
        """Roher P/L. Bei IBKR mappen wir das auf get_portfolio() (kompatibel)."""
        return self.get_portfolio()

    # --- Write-Operations (W3 LIVE — gegen Paper-Account verifizieren!) ---

    def _place_market_order(
        self,
        instrument_id: int,
        amount_usd: float,
        action: str,  # "BUY" oder "SELL"
        stop_loss_pct: float = 0,
        take_profit_pct: float = 0,
        fill_timeout: float = 30.0,
    ) -> Optional[dict]:
        """
        Gemeinsame Order-Submission. Returns eToro-kompatibles Response-Dict.

        Workflow:
        1. Contract aufloesen via ibkr_contract_resolver
        2. Live-Quote fetchen
        3. amount_usd -> quantity umrechnen (qty = floor(amount/price))
        4. MarketOrder einreichen via ib.placeOrder
        5. Bis Fill warten (oder timeout)
        6. (optional) Bracket: SL/TP als Child-Orders nachschicken
        7. Response im eToro-Format zurueckgeben

        Bei IBKR werden SL/TP als separate Stop/Limit-Orders eingereicht
        (Bracket-Order). eToro nimmt sie als Order-Parameter — wir
        emulieren das durch sequentielle Submission.
        """
        from app.ibkr_contract_resolver import resolve_contract, get_quote, amount_to_quantity
        from ib_insync import MarketOrder, StopOrder, LimitOrder

        try:
            ib = self._get_ib()
            contract = resolve_contract(ib, instrument_id)
            price = get_quote(ib, contract)
            if price is None or price <= 0:
                log.error("Kein Live-Quote fuer instrument_id=%d (%s) — Order abgebrochen",
                          instrument_id, contract.symbol)
                return None

            qty = amount_to_quantity(amount_usd, price)
            if qty <= 0:
                log.warning("amount=$%.2f bei price=$%.2f -> qty=0 — uebersprungen",
                            amount_usd, price)
                return None

            log.info("ORDER %s %d %s @ ~$%.2f (target $%.2f)",
                     action, qty, contract.symbol, price, amount_usd)

            # 1. Main-Order
            order = MarketOrder(action, qty)
            order.transmit = (stop_loss_pct == 0 and take_profit_pct == 0)
            trade = ib.placeOrder(contract, order)

            # 2. Bracket-Orders (SL/TP) wenn gewuenscht
            child_trades = []
            if stop_loss_pct > 0 or take_profit_pct > 0:
                # SL = price * (1 - stop_loss_pct/100) bei BUY, umgekehrt bei SELL
                opposite = "SELL" if action == "BUY" else "BUY"
                sign = -1 if action == "BUY" else 1

                if stop_loss_pct > 0:
                    sl_price = round(price * (1 + sign * stop_loss_pct / 100.0), 2)
                    sl_order = StopOrder(opposite, qty, sl_price)
                    sl_order.parentId = trade.order.orderId
                    sl_order.transmit = (take_profit_pct == 0)
                    child_trades.append(ib.placeOrder(contract, sl_order))

                if take_profit_pct > 0:
                    tp_price = round(price * (1 - sign * take_profit_pct / 100.0), 2)
                    tp_order = LimitOrder(opposite, qty, tp_price)
                    tp_order.parentId = trade.order.orderId
                    tp_order.transmit = True
                    child_trades.append(ib.placeOrder(contract, tp_order))

            # 3. Auf Fill warten
            deadline = time.time() + fill_timeout
            while time.time() < deadline:
                ib.sleep(0.2)
                if trade.isDone():
                    break

            status = trade.orderStatus.status  # "Filled", "Submitted", "Cancelled", ...
            fill_qty = trade.orderStatus.filled
            avg_fill_price = trade.orderStatus.avgFillPrice

            log.info("Order %s status=%s filled=%d avgPrice=%.4f",
                     trade.order.orderId, status, fill_qty, avg_fill_price)

            # 4. eToro-kompatibles Response
            return {
                "orderForOpen": {
                    "orderID": str(trade.order.orderId),
                    "statusID": status,
                    "filledQuantity": int(fill_qty),
                    "avgFillPrice": float(avg_fill_price or 0),
                },
                "_broker": "ibkr",
                "_contract": {
                    "symbol": contract.symbol,
                    "conId": contract.conId,
                    "secType": contract.secType,
                },
                "_amount_usd_target": amount_usd,
                "_amount_usd_actual": float((fill_qty or qty) * (avg_fill_price or price)),
                "_child_orders": [
                    {"orderId": ct.order.orderId, "status": ct.orderStatus.status}
                    for ct in child_trades
                ],
            }
        except (ValueError, NotImplementedError) as e:
            # Resolver-Fehler (etoro_id unknown, asset class unsupported)
            log.error("Order-Resolve failed: %s", e)
            return None
        except Exception as e:
            log.exception("Order failed: %s", e)
            return None

    def buy(self, instrument_id, amount_usd, leverage=1, stop_loss=0, take_profit=0):
        """
        Market-BUY by Amount USD.

        Args:
            instrument_id: eToro instrument_id (wird via ASSET_UNIVERSE auf IBKR Contract gemappt)
            amount_usd: Ziel-Volumen in USD (qty = floor(amount/price))
            leverage: IBKR Stock-Trades sind 1x — Margin macht IBKR automatisch.
                      Parameter wird IGNORIERT (nur fuer eToro-API-Kompatibilitaet).
            stop_loss: % unter Entry-Price fuer Stop-Loss (0 = kein SL)
            take_profit: % ueber Entry-Price fuer Take-Profit (0 = kein TP)
        """
        if leverage != 1:
            log.warning("IbkrBroker.buy: leverage=%d ignoriert (Stock-Margin via IBKR-Account-Setup)",
                        leverage)
        return self._place_market_order(
            instrument_id, amount_usd, "BUY",
            stop_loss_pct=stop_loss, take_profit_pct=take_profit,
        )

    def sell(self, instrument_id, amount_usd, leverage=1):
        """Market-SELL by Amount USD (Short-Open)."""
        if leverage != 1:
            log.warning("IbkrBroker.sell: leverage=%d ignoriert", leverage)
        return self._place_market_order(instrument_id, amount_usd, "SELL")

    def close_position(self, position_id, instrument_id=None):
        """
        Position schliessen via opposite-side Market-Order.

        IBKR braucht Contract+qty; position_id allein reicht nicht.
        Wir suchen die Position via ib.positions() und feuern eine
        Closing-Order in entgegengesetzter Richtung.

        Args:
            position_id: bei eToro UUID, bei IBKR string-form von conId.
                         Wir nutzen instrument_id wenn gegeben, sonst position_id als conId.
        """
        from ib_insync import MarketOrder

        try:
            ib = self._get_ib()
            target_con_id = None
            if instrument_id is not None:
                from app.ibkr_contract_resolver import resolve_contract
                target_con_id = resolve_contract(ib, instrument_id).conId
            else:
                try:
                    target_con_id = int(position_id)
                except (ValueError, TypeError):
                    log.error("close_position: position_id '%s' ist keine conId und instrument_id fehlt",
                              position_id)
                    return None

            positions = [p for p in ib.positions() if getattr(p.contract, "conId", None) == target_con_id]
            if not positions:
                log.error("Keine offene Position fuer conId=%s", target_con_id)
                return None

            pos = positions[0]
            qty = abs(int(pos.position))
            action = "SELL" if pos.position > 0 else "BUY"

            log.info("CLOSE Position %s qty=%d (%s)", pos.contract.symbol, qty, action)
            order = MarketOrder(action, qty)
            trade = ib.placeOrder(pos.contract, order)

            # Wait for fill
            deadline = time.time() + 30.0
            while time.time() < deadline:
                ib.sleep(0.2)
                if trade.isDone():
                    break

            return {
                "orderForOpen": {
                    "orderID": str(trade.order.orderId),
                    "statusID": trade.orderStatus.status,
                    "filledQuantity": int(trade.orderStatus.filled),
                    "avgFillPrice": float(trade.orderStatus.avgFillPrice or 0),
                },
                "_broker": "ibkr",
                "_action": "close",
                "_closed_position_id": str(target_con_id),
            }
        except Exception as e:
            log.exception("close_position failed: %s", e)
            return None

    # --- Instruments ---

    def search_instrument(self, query: str) -> list[dict]:
        """
        Symbol-Search via IBKR reqMatchingSymbols.

        Returns Liste im eToro-kompatiblen Format.
        """
        try:
            ib = self._get_ib()
            matches = ib.reqMatchingSymbols(query)
            results = []
            for m in matches:
                c = m.contract
                results.append({
                    "id": getattr(c, "conId", None),
                    "name": getattr(m, "longName", None) or getattr(c, "localSymbol", None) or c.symbol,
                    "symbol": c.symbol,
                    "exchange": getattr(c, "primaryExchange", None) or c.exchange,
                    "asset_class": c.secType,
                })
            return results
        except Exception as e:
            log.error("search_instrument failed: %s", e)
            return []

    def get_instruments(self, instrument_ids=None):
        """
        IBKR hat keine 'all instruments' API — daher nur Lookup wenn IDs gegeben.
        instrument_ids hier sind IBKR conIds.
        """
        if not instrument_ids:
            log.warning("IbkrBroker.get_instruments ohne IDs nicht unterstuetzt — IBKR hat kein Master-Universum")
            return []
        try:
            from ib_insync import Contract
            ib = self._get_ib()
            results = []
            for con_id in instrument_ids:
                c = Contract(conId=int(con_id))
                details = ib.reqContractDetails(c)
                if details:
                    d = details[0]
                    results.append({
                        "id": d.contract.conId,
                        "symbol": d.contract.symbol,
                        "name": d.longName,
                        "exchange": d.contract.primaryExchange or d.contract.exchange,
                        "asset_class": d.contract.secType,
                    })
            return results
        except Exception as e:
            log.error("get_instruments failed: %s", e)
            return []


if __name__ == "__main__":
    # CLI-Healthcheck: python -m app.ibkr_client
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    import json
    result = healthcheck()
    print(json.dumps(result, indent=2, default=str))
    raise SystemExit(0 if result["ok"] else 1)
