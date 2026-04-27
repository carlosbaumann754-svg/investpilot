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

# ib_insync.util.patchAsyncio() patcht asyncio.get_event_loop() global so dass
# nested calls (FastAPI -> ib_insync) funktionieren. Wird beim ersten Import
# einmalig aufgerufen damit ALLE folgenden ib_insync-Calls Loop-safe sind.
# Loest die "attached to a different loop" Errors die wir im Singleton-Pool
# trotz Auto-Retry noch im Log hatten.
_PATCH_DONE = False


def _patch_asyncio_once() -> None:
    global _PATCH_DONE
    if _PATCH_DONE:
        return
    try:
        from ib_insync import util as _ib_util
        _ib_util.patchAsyncio()
        _PATCH_DONE = True
        log.info("ib_insync.util.patchAsyncio() applied — Loop-Conflicts geloest")
    except Exception as e:
        log.warning("patchAsyncio failed: %s", e)


# Patch beim Module-Import (nicht beim ersten connect — sonst zu spaet bei
# parallel laufenden FastAPI-Endpoints und Bot-Threads)
_patch_asyncio_once()


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
## IbkrBroker — BrokerBase-Implementierung mit Singleton-Connection-Pool
## --------------------------------------------------------------------

import threading
from app.broker_base import BrokerBase

# Module-level Singleton-Pool: cached IB-Instanzen pro (host, port, client_id).
# Loest mehrere chronische Bugs:
#   1. ClientID-Geist: kein wiederholter connect/disconnect derselben ID
#   2. Loop-Conflict: jeder IB-Singleton bleibt in seinem eigenen Loop, nicht
#      zwischen FastAPI/Bot/Reconciliation hin und her geschoben
#   3. IBG-Pool-Exhaustion: bestehende IB-Instanz wird wiederverwendet bis sie
#      explizit invalidiert wird (z.B. Connection-Loss-Detection)
#
# Threadsafe via _IB_LOCK fuer get/set/invalidate.
_IB_INSTANCES: dict[tuple, "object"] = {}  # key=(host,port,client_id) -> IB
_IB_LOCK = threading.RLock()


def _pool_get(key: tuple):
    with _IB_LOCK:
        return _IB_INSTANCES.get(key)


def _pool_set(key: tuple, ib_instance) -> None:
    with _IB_LOCK:
        _IB_INSTANCES[key] = ib_instance


def _pool_invalidate(key: tuple) -> None:
    """Entfernt instance aus Pool. Naechster get_ib() macht frischen Connect."""
    with _IB_LOCK:
        old = _IB_INSTANCES.pop(key, None)
        if old is not None:
            try:
                if hasattr(old, "isConnected") and old.isConnected():
                    old.disconnect()
            except Exception:
                pass


def _pool_invalidate_all() -> None:
    """Pool komplett leeren — z.B. fuer Test-Isolation oder Container-Reset."""
    with _IB_LOCK:
        for k in list(_IB_INSTANCES.keys()):
            _pool_invalidate(k)


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

    def __init__(self, config: Optional[dict] = None, readonly: Optional[bool] = None):
        """
        Args:
            config: Geladene config.json. ibkr.client_id wird IGNORIERT wenn
                    readonly=True (random clientId 100-999 stattdessen).
            readonly: Override fuer ibkr.readonly. Bei True wird zwingend
                     eine eigene clientId genutzt (vermeidet Conflict mit Bot).

        Order-relevante Config-Keys (alle optional, mit Defaults):
            ibkr.fill_timeout_s           # Default 30s, wie lange auf Fill warten
            ibkr.cancel_on_timeout        # Default True, Auto-Cancel bei timeout
            ibkr.limit_slippage_pct       # Default 0.5%, Buffer fuer LimitOrder-Preis
            ibkr.default_order_type       # Default 'LIMIT' ('LIMIT' oder 'MARKET')
        """
        ibkr_cfg = (config or {}).get("ibkr", {}) if config else {}
        self.host = ibkr_cfg.get("host") or IBG_HOST
        self.port = int(ibkr_cfg.get("port") or IBG_PORT)
        self.readonly = readonly if readonly is not None else bool(ibkr_cfg.get("readonly", False))
        # Order-Verhalten konfigurierbar (Class-Defaults als Fallback)
        self.fill_timeout_s = float(ibkr_cfg.get("fill_timeout_s", 30.0))
        self.cancel_on_timeout = bool(ibkr_cfg.get("cancel_on_timeout", True))
        self.limit_slippage_pct = float(ibkr_cfg.get("limit_slippage_pct", 0.5))
        self.default_order_type = str(ibkr_cfg.get("default_order_type", "LIMIT")).upper()
        # ClientID-Strategie:
        #   - readonly=True ODER kein explicit id: random clientId (100-999)
        #     -> Dashboard-Endpoints, Reconciliation-Cron, Ad-hoc-Calls
        #     -> Vermeidet 'Error 326: client id already in use' bei
        #        parallelen Connects mit der Bot-Hauptinstanz.
        #   - readonly=False UND explicit id: nutze diese ID
        #     -> Bot-Trader-Hauptinstanz: clientId=1 aus config.json
        explicit_id = ibkr_cfg.get("client_id")
        if self.readonly or explicit_id is None:
            import random
            self.client_id = random.randint(100, 999)
        else:
            self.client_id = int(explicit_id)
        self.timeout = int(ibkr_cfg.get("timeout") or IBG_TIMEOUT)
        self._ib = None  # ib_insync.IB instance, lazy
        self.configured = True  # IBKR braucht keine API-Keys, nur Container-Reachability

    @property
    def broker_name(self) -> str:
        return "ibkr"

    # --- Connection-Lifecycle ---

    def _ensure_event_loop(self):
        """Loop-Setup fuer ib_insync je nach Calling-Context:

        - Aus FastAPI async-Handler (running loop): nest_asyncio.apply() patcht
          den Loop sodass ib_insync's eigene asyncio.run() Calls darin laufen
          koennen — ohne Loop-Conflict.
        - Aus asyncio.to_thread / threading.Thread (kein Loop): neuen erstellen
          und setzen.
        - Aus normalem sync-Code (Bot-Trader): bestehender Loop wird genutzt.

        Damit funktionieren ALLE Aufruf-Wege ohne 'no current event loop'-Fehler
        oder 'attached to a different loop'-Crash.
        """
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Loop laeuft bereits (FastAPI) -> nest_asyncio patchen
                try:
                    import nest_asyncio
                    nest_asyncio.apply(loop)
                except ImportError:
                    log.warning(
                        "nest_asyncio fehlt — Calls aus running event loop "
                        "(z.B. FastAPI) koennten haengen. pip install nest_asyncio."
                    )
        except RuntimeError:
            # Kein loop im aktuellen Thread -> neuen erstellen
            asyncio.set_event_loop(asyncio.new_event_loop())

    @property
    def _pool_key(self) -> tuple:
        """Eindeutiger Key fuer den Connection-Pool — pro (host, port, client_id)."""
        return (self.host, self.port, self.client_id)

    def _get_ib(self):
        """Liefert IB-Instanz aus Singleton-Pool — wird ueber Cycles hinweg
        wiederverwendet. Verhindert clientId-Conflicts und Loop-Issues.

        Workflow:
        0. Test-Hook: wenn self._ib direkt gesetzt (mock-injection), nutze das
        1. Pool-Lookup via (host, port, client_id) Key
        2. Wenn Instance da UND noch connected -> wiederverwenden
        3. Wenn Instance da aber tot -> invalidate + neu erstellen
        4. Wenn keine Instance -> erstellen mit Auto-Retry-Logic
        """
        # 0. Direkt-Injection (Tests, oder vorheriger Set)
        if self._ib is not None:
            try:
                if self._ib.isConnected():
                    return self._ib
            except Exception:
                pass

        key = self._pool_key

        # 1. Pool-Hit
        cached = _pool_get(key)
        if cached is not None:
            try:
                if cached.isConnected():
                    self._ib = cached  # backwards-compat
                    return cached
            except Exception:
                pass
            # Tote Instance -> invalidieren
            _pool_invalidate(key)

        # 2. Frische Connection mit Auto-Retry
        self._ensure_event_loop()
        try:
            ib = connect(
                host=self.host,
                port=self.port,
                client_id=self.client_id,
                timeout=self.timeout,
                readonly=self.readonly,
            )
        except Exception as primary_err:
            err_msg = str(primary_err).lower()
            # Error 326 / Timeout / Peer-closed / loop-issue -> Retry mit fresh clientId
            if any(k in err_msg for k in (
                "already in use", "timeout", "peer closed", "326",
                "different loop", "different event loop"
            )):
                import random
                fresh_id = random.randint(100, 999)
                log.warning(
                    "IBG-Connect mit clientId=%d failed (%s) — Retry mit fresh clientId=%d",
                    self.client_id, type(primary_err).__name__, fresh_id,
                )
                # Auch alten Pool-Eintrag invalidieren falls da
                _pool_invalidate(key)
                ib = connect(
                    host=self.host,
                    port=self.port,
                    client_id=fresh_id,
                    timeout=self.timeout,
                    readonly=self.readonly,
                )
                # Effective clientId fuer diese Instanz updaten -> neuer Pool-Key
                self.client_id = fresh_id
                key = self._pool_key
            else:
                raise

        _pool_set(key, ib)
        self._ib = ib  # backwards-compat fuer test-mocks die ._ib direkt setzen
        return ib

    def disconnect(self) -> None:
        """Im Singleton-Pattern KEIN echter disconnect.

        Connection bleibt im Pool aktiv fuer naechsten Caller (Bot oder
        Dashboard). Echter disconnect nur via _pool_invalidate(key) oder
        _pool_invalidate_all() fuer expliziten Reset.

        Das verhindert das clientId-Geist-Problem komplett: ohne disconnect
        hat IBG keinen Grund eine Session zu 'rotten lassen' — sie bleibt
        einfach aktiv und idle.
        """
        # Bewusst NO-OP: Connection bleibt im Pool fuer Reuse.
        # Wenn jemand wirklich disconnecten will: _pool_invalidate(self._pool_key)
        return

    def force_disconnect(self) -> None:
        """Echter disconnect + Pool-Invalidierung. Fuer Tests / Reset-Szenarien."""
        _pool_invalidate(self._pool_key)
        self._ib = None

    # --- Read-Operations (LIVE) ---

    def _get_account_value(self, tag: str) -> Optional[float]:
        """
        Holt einen Account-Value (NetLiquidation, AvailableFunds etc.) per Tag.

        Bevorzugt Currency=USD, dann BASE, sonst irgendeinen Match (IBKR
        liefert manchmal '' als currency fuer aggregierte Werte).
        """
        try:
            ib = self._get_ib()
            matches = [av for av in ib.accountValues() if av.tag == tag]
            if not matches:
                return None
            # Praeferenz USD > BASE > leer > rest
            for pref in ("USD", "BASE", ""):
                for av in matches:
                    if av.currency == pref:
                        try:
                            return float(av.value)
                        except (TypeError, ValueError):
                            continue
            # Fallback: erster nutzbarer
            for av in matches:
                try:
                    return float(av.value)
                except (TypeError, ValueError):
                    continue
            return None
        except Exception as e:
            log.error("_get_account_value(%s) failed: %s", tag, e)
            return None

    def get_portfolio(self) -> Optional[dict]:
        """
        Portfolio-Snapshot im eToro-kompatiblen Format.

        Returns:
            {"positions": [...], "aggregatedPositions": [...], "creditByRealizedEquity": ...}
        """
        try:
            ib = self._get_ib()
            # v36h: ib.portfolio() statt ib.positions() — liefert PortfolioItem
            # mit unrealizedPNL/marketPrice/marketValue pro Position. Vorher
            # zeigte Dashboard P/L $0.00 fuer alle Positionen weil
            # ib.positions() (Position) keine PnL-Felder hat.
            try:
                items = ib.portfolio()  # liste von PortfolioItem
            except Exception:
                items = []
            if not items:
                # Fallback: positions() hat zumindest Contract+Qty+AvgCost
                items = ib.positions()

            mapped_positions = []
            for p in items:
                contract = getattr(p, "contract", None)
                qty = float(getattr(p, "position", 0))
                avg_cost = float(getattr(p, "averageCost", 0) or getattr(p, "avgCost", 0) or 0)
                # PortfolioItem hat marketPrice, marketValue, unrealizedPNL
                mkt_price = getattr(p, "marketPrice", None)
                unreal = getattr(p, "unrealizedPNL", None)
                cost_basis = qty * avg_cost
                pnl_pct = (unreal / cost_basis * 100) if (unreal is not None and cost_basis) else 0
                mapped_positions.append({
                    "instrumentID": getattr(contract, "conId", None),
                    "symbol": getattr(contract, "symbol", None),
                    "amount": cost_basis,
                    "positionID": str(getattr(contract, "conId", "")),
                    "leverage": 1,
                    "openRate": avg_cost,
                    "currentRate": float(mkt_price) if mkt_price is not None else None,
                    "pnl": float(unreal) if unreal is not None else 0,
                    "pnl_pct": round(pnl_pct, 2),
                    "isBuy": qty > 0,
                    # eToro-kompatible PnL-Struct fuer parse_position
                    "unrealizedPnL": {"pnL": float(unreal) if unreal is not None else 0},
                })

            equity = self._get_account_value("NetLiquidation") or 0.0
            cash = self._get_account_value("AvailableFunds") or 0.0
            unrealized = self._get_account_value("UnrealizedPnL") or 0.0
            realized = self._get_account_value("RealizedPnL") or 0.0
            gross_pos_value = self._get_account_value("GrossPositionValue") or 0.0

            # eToro-kompatible Top-Level-Keys (Bot-Konsumenten lesen diese!):
            #   credit         = Cash-Balance (eToro Standard)
            #   unrealizedPnL  = offene P/L
            #   positions      = Liste offener Positionen
            # Plus IBKR-spezifische Erweiterungen mit '_'-Prefix
            return {
                "credit": cash,                   # ETORO STANDARD — kritisch fuer trader.py!
                "unrealizedPnL": unrealized,      # ETORO STANDARD
                "positions": mapped_positions,    # ETORO STANDARD
                "aggregatedPositions": [],        # eToro-Kompatibilitaet
                "creditByRealizedEquity": equity, # Legacy-Alias
                "availableCash": cash,            # Legacy-Alias
                "_broker": "ibkr",
                "_equity": equity,
                "_realized_pnl": realized,
                "_gross_position_value": gross_pos_value,
            }
        except Exception as e:
            log.error("get_portfolio failed: %s", e)
            return None

    def get_equity(self) -> Optional[float]:
        return self._get_account_value("NetLiquidation")

    def get_available_cash(self) -> Optional[float]:
        return self._get_account_value("AvailableFunds")

    def get_total_invested(self) -> Optional[float]:
        return self._get_account_value("GrossPositionValue")

    def get_pnl(self) -> Optional[dict]:
        """Roher P/L. Bei IBKR mappen wir das auf get_portfolio() (kompatibel)."""
        return self.get_portfolio()

    # --- Write-Operations (W3 LIVE — gegen Paper-Account verifizieren!) ---

    # Slippage-Buffer fuer LimitOrders: BUY akzeptiert +0.5% ueber Quote, SELL -0.5% drunter
    LIMIT_SLIPPAGE_PCT = 0.5

    # Default-Verhalten fuer noch nicht gefuellte Orders nach fill_timeout:
    #   True  = sicherer Default (cancel automatisch, kein Hanging-Order-Risk)
    #   False = Order bleibt im IBKR-Order-Book (z.B. Limit fuer After-Hours)
    CANCEL_ON_TIMEOUT = True

    def _place_market_order(
        self,
        instrument_id: int,
        amount_usd: float,
        action: str,  # "BUY" oder "SELL"
        stop_loss_pct: float = 0,
        take_profit_pct: float = 0,
        fill_timeout: Optional[float] = None,  # None -> self.fill_timeout_s
        order_type: Optional[str] = None,      # None -> self.default_order_type
        cancel_on_timeout: Optional[bool] = None,  # None -> self.cancel_on_timeout
        limit_slippage_pct: Optional[float] = None,  # None -> self.limit_slippage_pct
    ) -> Optional[dict]:
        """
        Gemeinsame Order-Submission. Returns eToro-kompatibles Response-Dict.

        Workflow:
        1. Contract aufloesen via ibkr_contract_resolver
        2. Live-Quote (oder Delayed-Quote) fetchen
        3. amount_usd -> quantity umrechnen (qty = floor(amount/price))
        4. Order einreichen via ib.placeOrder
           - LIMIT (default): limitPrice = quote * (1 + slippage_buffer * sign)
           - MARKET: nur wenn order_type='MARKET' explizit, braucht RT-Marktdaten
        5. Bis Fill warten (oder timeout)
        6. (optional) Bracket: SL/TP als Child-Orders nachschicken
        7. Response im eToro-Format zurueckgeben

        Warum LIMIT default:
        - Paper-Accounts ohne Market-Data-Abo lehnen MarketOrders ab
          ('No market data on major exchange for market order')
        - In Production verhindert Limit den Worst-Case-Slippage
        - 0.5% Buffer ist liquide genug fuer Fills auf Major-Stocks
        """
        from app.ibkr_contract_resolver import resolve_contract, get_quote, amount_to_quantity
        from ib_insync import MarketOrder, StopOrder, LimitOrder

        # Resolve effective config (per-call override > instance config > class default)
        eff_timeout = fill_timeout if fill_timeout is not None else self.fill_timeout_s
        eff_order_type = (order_type if order_type is not None else self.default_order_type).upper()
        eff_slippage = limit_slippage_pct if limit_slippage_pct is not None else self.limit_slippage_pct

        try:
            ib = self._get_ib()
            contract = resolve_contract(ib, instrument_id)
            price = get_quote(ib, contract)
            if price is None or price <= 0:
                log.error("Kein Quote fuer instrument_id=%d (%s) — Order abgebrochen",
                          instrument_id, contract.symbol)
                return None

            qty = amount_to_quantity(amount_usd, price)
            if qty <= 0:
                log.warning("amount=$%.2f bei price=$%.2f -> qty=0 — uebersprungen",
                            amount_usd, price)
                return None

            # 1. Main-Order: LIMIT mit Slippage-Buffer (default) oder MARKET
            if eff_order_type == "MARKET":
                order = MarketOrder(action, qty)
                limit_price_log = "MKT"
            else:
                slippage_sign = 1 if action == "BUY" else -1
                limit_price = round(price * (1 + slippage_sign * eff_slippage / 100.0), 2)
                order = LimitOrder(action, qty, limit_price)
                limit_price_log = f"limit ${limit_price:.2f} (slip {eff_slippage}%)"

            log.info("ORDER %s %d %s @ %s (target $%.2f, quote $%.2f)",
                     action, qty, contract.symbol, limit_price_log, amount_usd, price)

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
            deadline = time.time() + eff_timeout
            while time.time() < deadline:
                ib.sleep(0.2)
                if trade.isDone():
                    break

            status = trade.orderStatus.status  # "Filled", "Submitted", "Cancelled", ...
            fill_qty = trade.orderStatus.filled
            avg_fill_price = trade.orderStatus.avgFillPrice

            # 3b. Auto-Cancel wenn nach Timeout noch nicht filled (sicherer Default)
            #     Verhindert haengende Limit-Orders die ueberraschend Tage spaeter fuellen
            should_cancel = self.cancel_on_timeout if cancel_on_timeout is None else cancel_on_timeout
            if not trade.isDone() and should_cancel and status not in ("Filled", "Cancelled"):
                log.warning("Order %s nach %.0fs noch %s — Auto-Cancel (cancel_on_timeout=True)",
                            trade.order.orderId, eff_timeout, status)
                try:
                    ib.cancelOrder(trade.order)
                    # Bis zu 5s warten dass Cancel durchkommt
                    cancel_deadline = time.time() + 5.0
                    while time.time() < cancel_deadline and not trade.isDone():
                        ib.sleep(0.2)
                    status = trade.orderStatus.status
                    fill_qty = trade.orderStatus.filled
                    avg_fill_price = trade.orderStatus.avgFillPrice
                except Exception as e:
                    log.error("Auto-Cancel von Order %s failed: %s", trade.order.orderId, e)

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

            # LimitOrder (statt MarketOrder) — siehe _place_market_order Doku
            from app.ibkr_contract_resolver import get_quote
            from ib_insync import LimitOrder
            quote = get_quote(ib, pos.contract)
            if quote is None or quote <= 0:
                log.error("Kein Quote fuer Close von %s — Order abgebrochen", pos.contract.symbol)
                return None
            slippage_sign = 1 if action == "BUY" else -1
            limit_price = round(quote * (1 + slippage_sign * self.limit_slippage_pct / 100.0), 2)

            log.info("CLOSE Position %s qty=%d %s @ limit $%.2f (quote $%.2f)",
                     pos.contract.symbol, qty, action, limit_price, quote)
            order = LimitOrder(action, qty, limit_price)
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
