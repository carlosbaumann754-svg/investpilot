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

    # --- Write-Operations (TODO W3) ---

    def buy(self, instrument_id, amount_usd, leverage=1, stop_loss=0, take_profit=0):
        raise NotImplementedError(
            "IbkrBroker.buy() ist W3 — eToro 'amount_usd' muss zu IBKR 'quantity' "
            "uebersetzt werden via Live-Quote (qty = floor(amount_usd / price)). "
            "Erfordert Contract-Resolution (instrument_id -> IBKR conId/symbol). "
            "Bis dahin: BROKER=etoro in config.json belassen."
        )

    def sell(self, instrument_id, amount_usd, leverage=1):
        raise NotImplementedError("IbkrBroker.sell() ist W3 (siehe buy())")

    def close_position(self, position_id, instrument_id=None):
        raise NotImplementedError(
            "IbkrBroker.close_position() ist W3 — IBKR braucht Closing-Order "
            "(opposite side, gleiche qty, gleicher Contract)."
        )

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
