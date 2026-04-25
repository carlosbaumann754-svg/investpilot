"""
Broker-Abstraktion (W2 Migration eToro -> IBKR)
================================================

Definiert das gemeinsame Interface aller Broker-Implementierungen
(EtoroClient, IbkrBroker). Erlaubt config-driven Broker-Switching ohne
Code-Aenderung in den Konsumenten (trader.py, brain.py, etc.).

**Backwards-compatible Strategy:**
- `EtoroClient` bleibt unveraendert in `etoro_client.py` und behaelt seinen
  Namen — alle bestehenden Imports funktionieren weiter
- `EtoroClient` erbt nur formal von `BrokerBase` (Interface-Compliance-Check)
- `IbkrBroker` in `ibkr_client.py` implementiert dasselbe Interface
- Konsumenten koennen nach und nach von `EtoroClient(config)` auf
  `get_broker(config)` migriert werden

**Datenformat-Konvention:**
- Methoden geben Dict/List-Strukturen zurueck die der eToro-Response-Form
  folgen (incumbent broker dictiert das Format). IbkrBroker mappt seine
  IBKR-Daten in dieses Format.
- `instrument_id` ist immer eine Integer (eToro-native; IBKR mapped Symbol -> int).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional


class BrokerBase(ABC):
    """Gemeinsames Interface fuer alle Broker-Implementierungen."""

    # ------------------------------------------------------------------
    # Account / Portfolio (Read)
    # ------------------------------------------------------------------

    @abstractmethod
    def get_portfolio(self) -> Optional[dict]:
        """
        Portfolio mit Positionen + P/L.

        Returns:
            Dict im eToro-Format mit 'positions', 'equity', 'pnl', etc.
            None bei Fehler.
        """
        ...

    @abstractmethod
    def get_equity(self) -> Optional[float]:
        """Aktueller Equity-Wert (Portfolio + Cash)."""
        ...

    @abstractmethod
    def get_available_cash(self) -> Optional[float]:
        """Verfuegbares Cash fuer neue Trades."""
        ...

    @abstractmethod
    def get_total_invested(self) -> Optional[float]:
        """Total gebundenes Kapital in offenen Positionen."""
        ...

    @abstractmethod
    def get_pnl(self) -> Optional[dict]:
        """Roher P/L-Response (eToro: clientPortfolio mit positions+aggregatedPositions)."""
        ...

    # ------------------------------------------------------------------
    # Trading (Write)
    # ------------------------------------------------------------------

    @abstractmethod
    def buy(
        self,
        instrument_id: int,
        amount_usd: float,
        leverage: int = 1,
        stop_loss: float = 0,
        take_profit: float = 0,
    ) -> Optional[dict]:
        """
        Kauf-Order (Market, by Amount USD).

        Returns:
            Dict mit 'orderForOpen.orderID' und 'statusID' (eToro-Format).
            None bei Fehler.
        """
        ...

    @abstractmethod
    def sell(
        self,
        instrument_id: int,
        amount_usd: float,
        leverage: int = 1,
    ) -> Optional[dict]:
        """Sell/Short-Order (Market, by Amount USD)."""
        ...

    @abstractmethod
    def close_position(
        self,
        position_id: str,
        instrument_id: Optional[int] = None,
    ) -> Optional[dict]:
        """Offene Position schliessen."""
        ...

    # ------------------------------------------------------------------
    # Instruments / Market-Data
    # ------------------------------------------------------------------

    @abstractmethod
    def search_instrument(self, query: str) -> list[dict]:
        """
        Instrument-Suche per Name/Symbol.

        Returns:
            Liste von Dicts mit Keys: id, name, symbol, exchange, asset_class.
        """
        ...

    @abstractmethod
    def get_instruments(self, instrument_ids: Optional[list[int]] = None) -> Any:
        """Instrument-Metadaten (alle oder gefiltert)."""
        ...

    # ------------------------------------------------------------------
    # Identitaet (informativ)
    # ------------------------------------------------------------------

    @property
    @abstractmethod
    def broker_name(self) -> str:
        """Lesbarer Broker-Name fuer Logging/Dashboard ('etoro' oder 'ibkr')."""
        ...


# ----------------------------------------------------------------------
# Factory
# ----------------------------------------------------------------------

def get_broker(config: Optional[dict] = None, readonly: bool = False) -> BrokerBase:
    """
    Liefert die Broker-Implementierung gemaess config.

    Config-Schluessel `broker` steuert die Auswahl:
        "etoro"  (default) -> EtoroClient
        "ibkr"            -> IbkrBroker

    Falls `broker` fehlt oder leer: faellt auf "etoro" zurueck (backwards-compat).

    Args:
        config: Geladene Config-Struktur (dict). Wenn None, wird sie via
                config_manager.load_config() geholt.
        readonly: True fuer Dashboard-Endpoints / Reconciliation / Ad-hoc-Reads.
                  Bei IBKR fuehrt das zu random clientId (vermeidet Conflict mit
                  Bot-Hauptinstanz auf clientId=1). Hat keinen Effekt bei eToro.

    Returns:
        BrokerBase-Instanz, ready-to-use.

    Raises:
        ValueError wenn `broker` einen unbekannten Wert hat.
    """
    if config is None:
        from app.config_manager import load_config
        config = load_config()

    broker_name = (config.get("broker") or "etoro").lower().strip()

    if broker_name == "etoro":
        # Lazy-import to avoid circular dependencies
        from app.etoro_client import EtoroClient
        # readonly Param wird ignoriert — eToro hat keine clientId-Konflikte
        return EtoroClient(config)

    if broker_name == "ibkr":
        from app.ibkr_client import IbkrBroker
        return IbkrBroker(config, readonly=readonly)

    raise ValueError(
        f"Unbekannter Broker '{broker_name}'. Erwartet: 'etoro' oder 'ibkr'."
    )
