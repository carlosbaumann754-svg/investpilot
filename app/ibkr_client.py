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


# v37h+3 (Sprint-Tag-9, 19.05.2026): Audit-Coverage-Marker
AUDIT_METADATA = {
    "purpose": "IBKR-Broker-Adapter: Buy/Sell/Close-Orders, Position-Sync, Stale-Portfolio-Filter, Boot-Race-Detection, Account-Summary",
    "config_section": None,
    "state_files": [],
    "self_tests": ["tc_ibkr_connect", "tc_ibkr_session_watchdog"],
    "scheduler_hooks": [],
    "health_check": None,
    "added_in": "v37c (eToro->IBKR Migration)",
}

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

# v37h Tab-Audit-Day-2 (12.05.2026): Connection-Healthcheck-Cache.
# isConnected() reicht NICHT als Liveness-Check — ib_insync's Socket kann
# weiterhin "open" sein nachdem ib-gateway interne Session verloren hat
# (z.B. nach Daily 03:00 UTC Restart). Resultat: accountValues() liefert
# stale Cached-Werte ohne Fehler. Cash-Drift-Alert vom 12.05.06:43 UTC
# war exakt dieser Bug — Bot's Snapshots zeigten 2+ Stunden identische
# Werte ($421'397.13) während IBKR-Live $416'822 hatte.
#
# Fix: aktiver reqCurrentTime()-Healthcheck mit Throttling. Pro Pool-Key
# wird der letzte erfolgreiche Healthcheck-Zeitstempel gespeichert; falls
# > HEALTHCHECK_INTERVAL_SEC her, wird ein neuer reqCurrentTime() mit
# kurzem Timeout gemacht. Falls der failed: Connection als stale gemeldet.
import time as _time
_HEALTHCHECK_TS: dict[tuple, float] = {}
_HEALTHCHECK_INTERVAL_SEC = 60  # 1x pro Minute reicht (Bot-Cycle = ~5 Min)
_HEALTHCHECK_TIMEOUT_SEC = 2.0   # reqCurrentTime sollte <1s antworten


def _connection_is_fresh(ib, key, *, force=False) -> bool:
    """v37h: Aktiver Liveness-Check via reqCurrentTime().

    Cache-Pattern: gecached pro `_HEALTHCHECK_INTERVAL_SEC` (Default 60s).
    Wenn letzter erfolgreicher Check juenger als das, return True ohne
    erneuten Call. Sonst: reqCurrentTime() ausfuehren, bei Erfolg cache
    updaten, bei Fail return False (Caller invalidiert Pool).

    Returns:
        True wenn Connection live ist (oder check skipped wegen Cache)
        False wenn reqCurrentTime() failed -> stale connection
    """
    now = _time.time()
    if not force:
        last = _HEALTHCHECK_TS.get(key, 0)
        if now - last < _HEALTHCHECK_INTERVAL_SEC:
            return True
    try:
        # reqCurrentTime() ist async aber ib_insync exponiert synchronen
        # Wrapper. Bei stale connection blockiert er bis Timeout (default
        # 2s). Wir trustern den default-timeout.
        srv_time = ib.reqCurrentTime()
        if srv_time is None:
            return False
        _HEALTHCHECK_TS[key] = now
        return True
    except Exception as e:
        log.warning("IBKR-Healthcheck reqCurrentTime() failed (%s) — "
                    "Connection wird als stale invalidiert", type(e).__name__)
        return False


# Pushover-Throttle fuer Stale-Detection (vermeidet Spam wenn IBG-Outage
# laenger anhaelt — 1x pro Stunde reicht als Signal an Carlos)
_STALE_ALERT_LAST: dict[tuple, float] = {}
_STALE_ALERT_INTERVAL_SEC = 3600


def _maybe_alert_stale(key: tuple) -> None:
    now = _time.time()
    last = _STALE_ALERT_LAST.get(key, 0)
    if now - last < _STALE_ALERT_INTERVAL_SEC:
        return
    _STALE_ALERT_LAST[key] = now
    try:
        from app.alerts import push_alert
        push_alert(
            "IBKR-Connection stale",
            f"reqCurrentTime() failed fuer Pool-Key={key}. Bot hat "
            f"Connection invalidiert und reconnected. Falls dies oft "
            f"vorkommt: ib-gateway Container pruefen.",
        )
    except Exception as e:
        log.debug("Pushover-Alert (stale) failed: %s", e)


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


def _position_pnl_pct(qty, avg_cost, unrealized_pnl):
    """R-B11/E1 — korrekt vorzeichenbehaftete PnL% (Short-sicher).

    cost_basis = abs(qty) * avg_cost (Magnitude); das Vorzeichen kommt aus dem
    unrealized_pnl, das IBKR fuer Long UND Short korrekt liefert. Frueher
    qty*avg_cost -> bei einem Short (qty<0) wurde cost_basis negativ -> pnl_pct
    invertiert -> ein VERLIERENDER Short sah profitabel aus -> der Stop-Loss
    (check_stop_loss_take_profit: pnl_pct <= sl_pct) feuerte NIE. Genau das
    Szenario der ungewollten BA/CPER-Shorts (04.05.).
    """
    try:
        cost_basis = abs(float(qty)) * float(avg_cost)
    except (TypeError, ValueError):
        return 0.0
    if unrealized_pnl is None or not cost_basis:
        return 0.0
    return float(unrealized_pnl) / cost_basis * 100


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

        # E27 (07.05.2026): OrderStatusTracker fuer Reality-Aware-Trade-Logging.
        # Feature-Flag-geschuetzt — instantiiert immer (kein Risiko) aber
        # subscription erst aktiv wenn config.realtime_status_tracker.enabled=true.
        # Vorteile: Branch deploybar mit Flag OFF, jederzeit aktivierbar via API.
        # v37h Task 2c (09.05.2026): None-safe — IbkrBroker() ohne config-Arg
        # (z.B. von cancel_all_pending_orders) crashte vorher mit
        # AttributeError: 'NoneType' has no attribute 'get'.
        self._e27_enabled = bool(
            (config or {}).get("realtime_status_tracker", {}).get("enabled", False)
        )
        # v37h Task 1 (09.05.2026): Asset-Class-Order-Settings.
        # Stress-Test-Befund 08.05.: Commodity-ETFs (CPER, SLV) failen in
        # Pre-Market wegen duenner Liquiditaet. Per-Asset-Class Slippage +
        # Trading-Hours sind sauberer Architektur-Default.
        # Format: {"stocks": {"slippage_pct": 0.5, "trading_hours": "extended"},
        #          "commodities": {"slippage_pct": 1.5, "trading_hours": "rth_only"}, ...}
        self._asset_class_order_settings = (config or {}).get(
            "asset_class_order_settings", {}
        )
        try:
            # R-B7 (03.06.2026): Prozess-Singleton statt pro-Broker-Instanz.
            # Vorher bekam jeder per-Cycle-Broker einen eigenen Tracker; die
            # orderStatusEvent-Subscription haftete am Boot-Tracker, dessen
            # in-memory _pending spaeter registrierte Orders nie sah -> Phantom-
            # PendingSubmit. Singleton = geteilte _pending-Map -> kohaerent.
            from app.order_status_tracker import get_shared_tracker
            self._tracker = get_shared_tracker()
            log.info("E27 OrderStatusTracker (Singleton R-B7) bereit (enabled=%s, pending=%d)",
                     self._e27_enabled, self._tracker.get_pending_count())
        except Exception as e:
            log.warning("E27 Tracker-Init failed (non-fatal): %s", e)
            self._tracker = None
        self._e27_subscribed = False
        # R-A40 (22.05.2026 Sprint-Tag-12): Track WELCHE ib-Instanz subscribed
        # ist (per id()) damit nach Pool-Invalidate / Reconnect / Daily-Restart
        # automatisch neu subscribed wird. Sonst verloren wir bei jedem
        # Reconnect die orderStatusEvent-Subscription → Tracker-State bleibt
        # auf "PendingSubmit" haengen obwohl Order in Realitaet executed war
        # (Symptom: 2 stale Pending-Orders nach 19.6h obwohl Trade-History
        # sagt executed; IBKR ib.openTrades() sagt 0 pending).
        self._e27_subscribed_ib_id = None

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
                    # v37h Healthcheck: pruefe ob ib_insync wirklich live ist
                    # (isConnected reicht nicht — silent stale moeglich)
                    if _connection_is_fresh(self._ib, self._pool_key):
                        return self._ib
                    log.warning("Direkt-injected IB-Instanz ist stale — "
                                "Invalidate Pool und neu connecten")
                    _maybe_alert_stale(self._pool_key)
                    _pool_invalidate(self._pool_key)
                    self._ib = None
            except Exception:
                pass

        key = self._pool_key

        # 1. Pool-Hit
        cached = _pool_get(key)
        if cached is not None:
            try:
                if cached.isConnected():
                    # v37h Healthcheck: vor Reuse pruefen ob Connection live
                    if _connection_is_fresh(cached, key):
                        self._ib = cached  # backwards-compat
                        return cached
                    log.warning("Pool-Instance %s ist stale (silent-stale "
                                "nach ib-gateway Restart?) — invalidiere "
                                "Pool fuer fresh reconnect", key)
                    _maybe_alert_stale(key)
                    # fall-through zur Invalidation + Neu-Connect
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

        # E27: Subscribe orderStatusEvent fuer Real-Time Status-Updates
        # Idempotent (subscribe nur einmal pro Connection). Feature-Flag
        # entscheidet ob Tracker aktiv reagiert oder no-op macht.
        self._maybe_subscribe_e27_events(ib)

        return ib

    def _maybe_subscribe_e27_events(self, ib):
        """E27: subscribe orderStatusEvent wenn Feature-Flag + Tracker da.

        R-A40 (22.05.2026): Idempotent PRO ib-Instanz statt global.
        Vorher: _e27_subscribed=True wurde einmal gesetzt und nie zurueck —
        nach Pool-Invalidate (Stale-Connection, Daily-Restart) bekam die
        NEUE ib-Instanz keine Subscription, alte Subscription war tot.
        Resultat: orderStatusEvents kamen nicht mehr beim Tracker an,
        pending_orders.json-Status blieb auf "PendingSubmit" haengen obwohl
        Order in Wirklichkeit executed. Symptom heute: 2 stale Pending
        nach 19.6h (KO SELL + ASML BUY beide in Trade-History als executed).

        Fix: id(ib) tracking. Bei neuer ib-Instanz wird neu subscribed.
        Alte Subscription ist eh tot weil ib-Object disconnected/garbage-
        collected wird.
        """
        if not self._e27_enabled or self._tracker is None:
            return
        current_ib_id = id(ib)
        if self._e27_subscribed_ib_id == current_ib_id:
            return  # bereits auf DIESER ib-Instanz subscribed
        try:
            ib.orderStatusEvent += self._tracker.handle_status_event
            self._e27_subscribed = True  # Backwards-compat fuer Tests
            self._e27_subscribed_ib_id = current_ib_id
            log.info("E27 orderStatusEvent subscribed (Real-Time Status-Tracking aktiv, ib_id=%s)", current_ib_id)
            # Recovery: pending Orders gegen IBKR-Reality synchronisieren.
            # v37e Tag 3: returnt dict mit resolved/staled/still_pending.
            try:
                stats = self._tracker.recover_from_ibkr(ib)
                if isinstance(stats, dict):
                    if stats.get("resolved", 0) or stats.get("staled", 0):
                        log.info(
                            "E27 Recovery: %d resolved, %d staled (>48h offline), %d still pending",
                            stats.get("resolved", 0),
                            stats.get("staled", 0),
                            stats.get("still_pending", 0),
                        )
                elif stats:  # Backward-Compat falls Tracker int returnt
                    log.info("E27 Recovery: %d pending Orders synchronisiert", stats)
            except Exception as e:
                log.warning("E27 Recovery failed (non-fatal): %s", e)
        except Exception as e:
            log.warning("E27 Subscription failed (non-fatal): %s", e)

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

    def _get_account_value_base(self, tag: str) -> Optional[float]:
        """v37h Tab-Audit-Day-2 (11.05.2026): explizit BASE-Currency-Variante.

        Bei Multi-Currency-Setup (z.B. Carlos: CHF=BASE, USD/GBP-Loans)
        liefert IBKR fuer einige Tags (UnrealizedPnL, RealizedPnL) MEHRERE
        Eintraege je Currency. Fuer Display in der Account-Basis-Waehrung
        brauchen wir explizit den BASE-Eintrag. Tags die nur BASE haben
        (NetLiquidation, TotalCashValue, GrossPositionValue) liefern hier
        denselben Wert wie _get_account_value (Fallback-Logik).
        """
        try:
            ib = self._get_ib()
            matches = [av for av in ib.accountValues() if av.tag == tag]
            if not matches:
                return None
            # Praeferenz BASE > leer > USD > rest
            for pref in ("BASE", "", "USD"):
                for av in matches:
                    if av.currency == pref:
                        try:
                            return float(av.value)
                        except (TypeError, ValueError):
                            continue
            for av in matches:
                try:
                    return float(av.value)
                except (TypeError, ValueError):
                    continue
            return None
        except Exception as e:
            log.error("_get_account_value_base(%s) failed: %s", tag, e)
            return None

    def _get_base_currency(self) -> str:
        """v37h: Identifiziert die Account-Basis-Waehrung aus IBKR.

        AccountValue mit tag='AccountType' oder via NetLiquidation's
        currency-Feld. Fallback 'USD' falls nicht ermittelbar.
        """
        try:
            ib = self._get_ib()
            # NetLiquidation hat genau einen Eintrag in BASE-Currency
            for av in ib.accountValues():
                if av.tag == "NetLiquidation" and av.currency and av.currency != "BASE":
                    return str(av.currency).upper()
        except Exception:
            pass
        return "USD"

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

            # v37ce: Stale-Portfolio-Filter (META-Loop Hotfix 30.04. 16:25 CEST).
            # ib_insync ib.portfolio() kann nach Close-Fills veraltete Items mit
            # qty>0 zurueckliefern, weil IBKR nicht immer einen updatePortfolio-
            # Event nach SELL-Fill sendet — nur ein position()-Event. Daher:
            # (1) qty=0 explizit rausfiltern, (2) Cross-Check gegen ib.positions()
            # (das schneller updated wird) — nur Items die in BEIDEN Datenquellen
            # sind ueberleben. Verhindert META-style Loop-Bugs (11x SL_FAILED
            # Spam in 50min nach erfolgreichem 15:31 Close).
            # v37cf: Boot-Race-Schutz — wenn ib.positions() leer ODER deutlich
            # weniger Items hat als ib.portfolio(), ist der ib_insync-Cache
            # waehrend Boot noch nicht synct. Filter dann deaktivieren statt
            # echte Positionen wegzuwerfen (NVDA-Boot-Race vom 30.04. 16:35).
            try:
                live_pos = list(ib.positions() or [])
                live_con_ids = {getattr(p.contract, "conId", None)
                                for p in live_pos
                                if abs(float(getattr(p, "position", 0) or 0)) > 0}
                # Boot-Race-Detection: ib.positions() liefert <50%% der Items
                # die ib.portfolio() hat -> wahrscheinlich noch nicht synct.
                portfolio_qty_count = sum(
                    1 for p in items
                    if abs(float(getattr(p, "position", 0) or 0)) > 0
                )
                if portfolio_qty_count > 0 and len(live_con_ids) < portfolio_qty_count * 0.5:
                    log.warning(
                        "Boot-Race-Detection: ib.positions()=%d, ib.portfolio() "
                        "hat %d aktive — Stale-Filter pausiert (Sync laeuft).",
                        len(live_con_ids), portfolio_qty_count,
                    )
                    live_con_ids = None  # Kein Filter
            except Exception:
                live_con_ids = None  # Fallback: kein Filter wenn positions() fehlt

            mapped_positions = []
            for p in items:
                contract = getattr(p, "contract", None)
                qty = float(getattr(p, "position", 0))
                # v37ce-Filter
                if qty == 0:
                    continue
                con_id = getattr(contract, "conId", None)
                if live_con_ids is not None and con_id not in live_con_ids:
                    # R-A20 (19.05.2026): WARNING -> INFO. Filter wirkt
                    # erwuenscht (v37ce Anti-META-Loop). Kein Bot-Problem,
                    # erzeugt nur Log-Noise + Sentry-Spam wenn als WARN.
                    log.info(
                        "Stale ib.portfolio()-Eintrag uebersprungen: %s conId=%s "
                        "qty=%s (nicht in ib.positions(), wahrscheinlich nach "
                        "Close-Fill ohne updatePortfolio-Event — Filter wirkt erwuenscht)",
                        getattr(contract, "symbol", "?"), con_id, qty,
                    )
                    continue
                avg_cost = float(getattr(p, "averageCost", 0) or getattr(p, "avgCost", 0) or 0)
                # PortfolioItem hat marketPrice, marketValue, unrealizedPNL
                mkt_price = getattr(p, "marketPrice", None)
                unreal = getattr(p, "unrealizedPNL", None)
                # E1: abs(qty) -> Short-sicheres Vorzeichen (Helper). amount =
                # Magnitude des Kapitaleinsatzes; Richtung steckt in isBuy.
                cost_basis = abs(qty) * avg_cost
                pnl_pct = _position_pnl_pct(qty, avg_cost, unreal)
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
            # v37cd FIX: TotalCashValue = echter Settled-Cash (Cash-Balance).
            # AvailableFunds ist NetLiq-InitialMargin (Buying-Power-Reserve)
            # und war fuer Cash-Konten gleich, fuer Margin-Konten (DUP108015)
            # ~30-50%% groesser als echter Cash. Folge auf eToro-Standard
            # `credit`: DCA-Detection, Margin-Safety, Daily-Summary, Display
            # rechneten alle mit Phantom-Cash. Audit Findings F1-F4.
            cash = self._get_account_value("TotalCashValue") or 0.0
            buying_power = self._get_account_value("AvailableFunds") or 0.0
            unrealized = self._get_account_value("UnrealizedPnL") or 0.0
            realized = self._get_account_value("RealizedPnL") or 0.0
            gross_pos_value = self._get_account_value("GrossPositionValue") or 0.0

            # v37h Tab-Audit-Day-2 (11.05.2026): Multi-Currency-Display.
            # Carlos's Konto: CHF=BASE, USD/GBP Cash-Loans + USD Aktien-Positionen.
            # NetLiquidation kommt nur in BASE (CHF). UnrealizedPnL kommt sowohl
            # USD ($21'951) als auch BASE (CHF 17'082) — Bot-Trading nutzt USD,
            # Dashboard zeigt BASE (= IBKR-Mobile-Sicht).
            base_currency = self._get_base_currency()
            base_unrealized = self._get_account_value_base("UnrealizedPnL") or 0.0
            base_realized = self._get_account_value_base("RealizedPnL") or 0.0
            # v37h+2 (Q3-13, 15.05.2026): explizit BASE-Werte fuer Cash + GrossPosVal
            # statt _get_account_value (das USD bevorzugt). Bug-Risk: bei Multi-
            # Currency-Konten wo IBKR sowohl USD- als auch BASE-Eintraege liefert,
            # haette der Bot bislang USD-Cash bevorzugt -> Position-Sizing falsch
            # kalibriert + Cash-Reserve gegen falsche Currency abgezogen.
            base_total_cash = self._get_account_value_base("TotalCashValue") or cash
            base_gross_position_value = (
                self._get_account_value_base("GrossPositionValue") or gross_pos_value
            )
            base_net_liquidation = equity  # NetLiquidation kommt nur in BASE
            # Implizite Cost-Basis in BASE = GrossPositionValue - UnrealizedPnL
            base_cost_basis = base_gross_position_value - base_unrealized

            # v37h+2 (Q3-13, 15.05.2026): Defensive-Logging bei Currency-Drift.
            # Wenn cash (_get_account_value USD-pref) != base_total_cash, ist
            # IBKR Multi-Currency-Reporting im Spiel. Bot's Top-Level credit
            # behaelt USD-pref-Wert fuer Backward-Compat (positions sind in
            # USD), aber Cash-Reserve + Sizing-Stellen muessen _base_total_cash
            # nutzen. Drift > 1%% = Warning damit sichtbar.
            if base_total_cash > 0 and cash > 0:
                drift_pct = abs(cash - base_total_cash) / base_total_cash * 100
                if drift_pct > 1.0:
                    log.warning(
                        "Currency-Drift TotalCashValue: USD-pref=%.2f vs BASE=%.2f "
                        "(%.1f%% Drift). Bot's `credit` zeigt USD-pref; "
                        "Cash-Reserve/Sizing nutzen _base_total_cash.",
                        cash, base_total_cash, drift_pct,
                    )

            # eToro-kompatible Top-Level-Keys (Bot-Konsumenten lesen diese!):
            #   credit         = Cash-Balance (eToro Standard, jetzt TotalCashValue)
            #   unrealizedPnL  = offene P/L (USD-Trading-Sicht)
            #   positions      = Liste offener Positionen (alle USD-Werte)
            # Plus IBKR-spezifische Erweiterungen mit '_'-Prefix.
            # Plus _base_* fuer Multi-Currency-Display (Dashboard).
            return {
                "credit": cash,                   # ETORO STANDARD — kritisch fuer trader.py!
                "unrealizedPnL": unrealized,      # ETORO STANDARD (USD-Wert fuer Trading-Code)
                "positions": mapped_positions,    # ETORO STANDARD
                "aggregatedPositions": [],        # eToro-Kompatibilitaet
                "creditByRealizedEquity": equity, # Legacy-Alias
                "availableCash": cash,            # Legacy-Alias (TotalCashValue)
                "_broker": "ibkr",
                "_equity": equity,
                "_buying_power": buying_power,    # v37cd: AvailableFunds separat
                "_realized_pnl": realized,
                "_gross_position_value": gross_pos_value,
                # v37h Multi-Currency-Layer (Display in Account-Basis-Waehrung):
                "_base_currency": base_currency,
                "_base_net_liquidation": base_net_liquidation,
                "_base_total_cash": base_total_cash,
                "_base_gross_position_value": base_gross_position_value,
                "_base_unrealized_pnl": base_unrealized,
                "_base_realized_pnl": base_realized,
                "_base_cost_basis": base_cost_basis,
            }
        except Exception as e:
            log.error("get_portfolio failed: %s", e)
            return None

    def get_equity(self) -> Optional[float]:
        return self._get_account_value("NetLiquidation")

    def get_available_cash(self) -> Optional[float]:
        """v37cd: TotalCashValue (echter Settled-Cash) statt AvailableFunds.
        AvailableFunds = Buying-Power-Reserve via get_buying_power().
        """
        return self._get_account_value("TotalCashValue")

    def get_buying_power(self) -> Optional[float]:
        """v37cd: NEU — AvailableFunds = Buying-Power (Cash + Margin-Capacity).
        Nutzt fuer Order-Sizing-Pruefungen, NICHT fuer Cash-Anzeige."""
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

    def _resolve_order_settings(self, instrument_id, limit_slippage_pct=None):
        """v37h Task 1 (09.05.2026): Asset-Class-aware Order-Settings.

        Resolution-Reihenfolge fuer slippage_pct:
          1. Per-call limit_slippage_pct (wenn explizit gesetzt)
          2. asset_class_order_settings[asset_class].slippage_pct
          3. asset_class_order_settings._default.slippage_pct
          4. self.limit_slippage_pct (instance default)

        Resolution fuer outsideRth:
          - asset_class_order_settings[asset_class].trading_hours
          - "rth_only" -> outsideRth=False (NUR NYSE-RTH 09:30-16:00 EST)
          - "extended" -> outsideRth=True (Pre-Market + After-Hours OK)

        Returns: dict mit slippage_pct, outside_rth, asset_class

        Diese Helper ist isoliert von ib_insync damit Unit-Tests ohne
        broker-connection moeglich sind.
        """
        from app.market_scanner import get_asset_class_for_instrument_id
        asset_class = get_asset_class_for_instrument_id(instrument_id)
        ac_settings = self._asset_class_order_settings.get(asset_class) if asset_class else None
        # Review-Fix #3 (09.05.2026): `not ac_settings` faengt None UND {} —
        # der empty-dict-Fall passiert wenn Config explizit `"bonds": {}` setzt
        # (Typo) oder eine neue Asset-Class in ASSET_UNIVERSE ohne Settings-
        # Eintrag bleibt. Dann _default als Safety-Net.
        if not ac_settings:
            ac_settings = self._asset_class_order_settings.get("_default", {})
        # slippage
        if limit_slippage_pct is not None:
            slippage = limit_slippage_pct
        elif ac_settings.get("slippage_pct") is not None:
            slippage = float(ac_settings["slippage_pct"])
        else:
            slippage = self.limit_slippage_pct
        # outsideRth
        trading_hours_mode = ac_settings.get("trading_hours", "extended")
        outside_rth = (trading_hours_mode != "rth_only")
        return {
            "slippage_pct": slippage,
            "outside_rth": outside_rth,
            "asset_class": asset_class,
            "trading_hours_mode": trading_hours_mode,
        }

    # Status-Werte die einen Brain-Snapshot ausloesen (Cash/Positions haben
    # sich tatsaechlich veraendert). Zentrale Liste damit Tests + Implementierung
    # konsistent bleiben.
    _SNAPSHOTTABLE_STATUSES = frozenset({"Filled", "PartiallyFilled"})

    def _ensure_connected(
        self,
        max_retries: int = 3,
        backoff_base_s: float = 5.0,
    ) -> bool:
        """v37h Task 2d (10.05.2026): IB-Gateway-Resilience mit Auto-Reconnect.

        HINTERGRUND
        Daily-Restart-Cron (03:00 UTC) handhabt geplante Restarts. Mid-day
        Socket-Drops (Network-Blip, IBG-GC-Pause, IBKR-Server-Restarts in
        Quiet-Hours) sind aber nicht abgefangen — Trader-Cycle crasht oder
        haengt. _get_ib() macht bereits einen einzigen Retry bei clientId-
        Conflict, aber kein Backoff bei Connection-Errors.

        Diese Methode wraps _get_ib() mit exponential backoff:
            Versuch 0: sofort
            Versuch 1: nach backoff_base * 3^0 Sekunden
            Versuch 2: nach backoff_base * 3^1 Sekunden
            Versuch 3: nach backoff_base * 3^2 Sekunden
        Default-Werte (5s base, 3 retries): 5s + 15s + 45s = max 65s Wait.

        Args:
            max_retries: Anzahl Wiederholversuche NACH dem ersten (default 3).
            backoff_base_s: Basis fuer exponential backoff (default 5.0).

        Returns:
            True  = Connection lebt (initial OK ODER nach Retry wiederhergestellt).
            False = Final-Failure nach allen Versuchen — log.error -> Sentry.
                    Caller sollte Safe-Mode (no new orders, cancel pending).

        DEFENSIVE
        Diese Methode ist OPT-IN. Public Methods koennen sie vorab aufrufen
        wenn sie Resilience brauchen. Bestehende Methoden bleiben unveraendert
        (backwards-compat).

        SYNC-ONLY (Review-Fix #2, 10.05.2026)
        Diese Methode nutzt time.sleep — blockiert den callenden Thread bis
        65s. NICHT aus async/FastAPI-Handlern aufrufen — sonst Event-Loop-
        Starvation. Andere Stellen in diesem Modul nutzen ib.sleep (asyncio-
        aware via ib_insync). Wenn async-wiring noetig wird, sleep_fn als
        Argument injizieren.

        client_id-MUTATION (Review-Fix #3, 10.05.2026)
        _get_ib() macht intern bereits einen Single-Retry bei clientId-
        Conflict und mutiert dabei self.client_id. Diese Wrapper-Schicht
        retryt OBENDREIN. D.h. ein Final-Fail kann bedeuten:
        max_retries * 2 echte Connect-Versuche und ein zufaellig gewaehlter
        Pool-Key. Fuer Cutover-Phase OK weil Final-Fail = Sentry + Safe-Mode,
        aber post-cutover ggf. die zwei Retry-Layer entkoppeln.
        """
        last_error: Optional[Exception] = None
        total_attempts = max_retries + 1
        for attempt in range(total_attempts):
            try:
                ib = self._get_ib()
                if ib is not None and ib.isConnected():
                    if attempt > 0:
                        log.info(
                            "IB-Reconnect erfolgreich nach %d Versuch(en)",
                            attempt + 1,
                        )
                    return True
                log.warning(
                    "IB-Connect Versuch %d/%d: _get_ib lieferte disconnected ib",
                    attempt + 1, total_attempts,
                )
            except Exception as e:
                last_error = e
                log.warning(
                    "IB-Connect Versuch %d/%d failed: %s: %s",
                    attempt + 1, total_attempts, type(e).__name__, e,
                )

            # Backoff zwischen Versuchen — nicht nach letztem Versuch
            if attempt < max_retries:
                backoff = backoff_base_s * (3 ** attempt)
                time.sleep(backoff)

        log.error(
            "IB-Connect FINAL-FAILED nach %d Versuchen — Safe-Mode aktiv. "
            "Letzter Fehler: %s",
            total_attempts, last_error,
        )
        return False

    def _snapshot_after_fill_safely(
        self,
        status: str,
        symbol: str,
        async_persist: bool = True,
    ) -> bool:
        """v37h Task 2 (10.05.2026): Eager Brain-Snapshot direkt nach Fill.

        HINTERGRUND
        Vor dieser Aenderung wurde Brain-Snapshot nur im Trader-Cycle (alle 5-15
        Min) geschrieben. Reconcile-Cron laeuft alle 30 Min. Wenn ein Fill
        zwischen Cycle-Tick und naechstem Reconcile-Run passiert, sieht
        Reconcile veraltete Cash-Werte -> Phantom-Drift-Alert (siehe
        08.05.2026-Vorfall: 12 Pushover-Alerts ueber Nacht mit 'CASH_DRIFT:').

        v37f hat das Schema-Mismatch (cash vs credit) gefixt aber NICHT den
        Timing-Gap. Diese Methode schliesst den Gap.

        Args:
            status: Order-Status nach _place_market_order Wartephase.
                Snapshot wird nur bei Filled/PartiallyFilled getriggert.
            symbol: Fuer Audit-Log (Postmortem-Debugging).
            async_persist: True (default) -> record_snapshot laeuft in
                daemon-thread, _place_market_order kehrt sofort zurueck.
                False -> synchron persistieren (nur fuer Tests, damit
                MagicMock-Assertions stabil sind).

        Returns:
            True  = Snapshot getriggert (im async-Modus: Thread gestartet,
                    Persistenz noch nicht garantiert; im sync-Modus: persisted).
            False = Snapshot uebersprungen (status nicht-fuellend) ODER
                    Fehler aufgetreten (defensiv gefangen, Order-Flow geht
                    weiter).

        DEFENSIVE
        Diese Methode darf NIE eine Exception hochwerfen — der Call-Site
        in _place_market_order rechnet damit dass eine fehlgeschlagene
        Snapshot-Persistierung den Order-Result nicht beeinflusst.

        REVIEW-FIX #1 (Code-Review 10.05.2026): record_snapshot ruft
        save_json(BRAIN_FILE) unter threading.Lock auf. Synchroner Aufruf
        wuerde Order-Confirmation-Return durch Lock-Wait + 365-Snapshot-
        Serialization verzoegern. Daher async_persist=True default —
        save_json laeuft im daemon-thread, der Order-Path returnt sofort.
        Defensiveness bleibt: Errors in Thread werden geloggt, nicht
        propagiert.
        """
        if status not in self._SNAPSHOTTABLE_STATUSES:
            return False
        try:
            portfolio = self.get_portfolio()
        except Exception as e:
            log.warning(
                "Snapshot-After-Fill: get_portfolio raised for %s — non-fatal: %s",
                symbol, e,
            )
            return False
        if portfolio is None:
            log.warning(
                "Snapshot-After-Fill skipped for %s: get_portfolio returned None",
                symbol,
            )
            return False

        def _do_persist():
            """Roher Persist-Call ohne Error-Handling — Caller faengt."""
            from app.brain import record_snapshot  # lazy: Circular avoidance
            record_snapshot(portfolio)

        if async_persist:
            # Async: Thread schluckt + loggt Fehler intern (Caller bekommt
            # immer True = "Thread gestartet"). So bleibt Order-Path schnell.
            def _persist_swallow():
                try:
                    _do_persist()
                    log.info("Snapshot-After-Fill OK: %s persisted (async)", symbol)
                except Exception as e:
                    log.warning(
                        "Snapshot-After-Fill: record_snapshot raised for %s — "
                        "non-fatal: %s", symbol, e,
                    )
            t = threading.Thread(
                target=_persist_swallow,
                name=f"snapshot-after-fill-{symbol}",
                daemon=True,
            )
            t.start()
            return True

        # Sync: Caller will klare True/False-Antwort fuer Persist-Erfolg.
        try:
            _do_persist()
            log.info("Snapshot-After-Fill OK: %s persisted (sync)", symbol)
            return True
        except Exception as e:
            log.warning(
                "Snapshot-After-Fill: record_snapshot raised for %s — "
                "non-fatal: %s", symbol, e,
            )
            return False

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

        # v37h Task 1 (09.05.2026): Asset-Class-aware Slippage + Trading-Hours.
        # Resolution via _resolve_order_settings helper (isoliert testbar).
        order_settings = self._resolve_order_settings(instrument_id, limit_slippage_pct)
        eff_slippage = order_settings["slippage_pct"]
        outside_rth = order_settings["outside_rth"]
        asset_class = order_settings["asset_class"]
        log.debug("Order-Settings: asset=%s, slippage=%s%%, outsideRth=%s",
                  asset_class or "?", eff_slippage, outside_rth)

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
                # Bei MARKET-Orders ist intended_price der zur Order-Zeit gesehene Quote
                intended_price = float(price) if price else 0.0
            else:
                slippage_sign = 1 if action == "BUY" else -1
                limit_price = round(price * (1 + slippage_sign * eff_slippage / 100.0), 2)
                order = LimitOrder(action, qty, limit_price)
                limit_price_log = f"limit ${limit_price:.2f} (slip {eff_slippage}%)"
                intended_price = float(limit_price)

            # v37h Task 1 (09.05.2026): outsideRth aus Asset-Class-Settings.
            # rth_only -> Order fuellt nur in NYSE-RTH 09:30-16:00 EST.
            # Pre-Market/After-Hours wird die Order zwar submitted aber NICHT
            # gefuellt = saubere "warte-auf-RTH"-Semantik.
            order.outsideRth = outside_rth

            log.info("ORDER %s %d %s @ %s (target $%.2f, quote $%.2f, asset=%s, mode=%s, outsideRth=%s)",
                     action, qty, contract.symbol, limit_price_log, amount_usd, price,
                     asset_class or "?", order_settings.get("trading_hours_mode", "?"),
                     outside_rth)

            order.transmit = (stop_loss_pct == 0 and take_profit_pct == 0)
            trade = ib.placeOrder(contract, order)

            # E27: Tracker registriert die Order fuer Real-Time Status-Updates.
            # Feature-Flag-geschuetzt (no-op wenn disabled). Single-Point-of-
            # Integration: alle 8 Trader-Pfade (Scanner-Buy/SL/TP/...) gehen
            # durch _place_market_order, daher ein Hook genuegt.
            if self._e27_enabled and self._tracker is not None:
                try:
                    self._tracker.register(
                        order_id=trade.order.orderId,
                        trade_entry={
                            "symbol": contract.symbol,
                            "action": action,
                            "amount_usd": amount_usd,
                            "order_id": str(trade.order.orderId),
                            "instrument_id": instrument_id,
                            "status": "submitted",
                            "ibkr_status_raw": trade.orderStatus.status if trade.orderStatus else "",
                        },
                    )
                except Exception as e:
                    log.warning("E27 register failed (non-fatal): %s", e)

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
                    # v37h Task 1 Review-Fix #1 (09.05.2026): SL muss in selber
                    # Trading-Hours-Window aktiv sein wie Parent. Sonst: bei
                    # extended-Stocks fuellt Entry pre-market, aber StopOrder
                    # bleibt RTH-only und triggert nicht bei Pre-Market-Crash.
                    sl_order.outsideRth = outside_rth
                    child_trades.append(ib.placeOrder(contract, sl_order))

                if take_profit_pct > 0:
                    tp_price = round(price * (1 - sign * take_profit_pct / 100.0), 2)
                    tp_order = LimitOrder(opposite, qty, tp_price)
                    tp_order.parentId = trade.order.orderId
                    tp_order.transmit = True
                    # v37h Task 1 Review-Fix #1 (09.05.2026): TP folgt selber
                    # Hours-Logik wie Parent (Konsistenz; weniger relevant aber
                    # vermeidet missed-fill bei Pre-Market-Spike).
                    tp_order.outsideRth = outside_rth
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

            # v37h Task 2 (10.05.2026): Eager Brain-Snapshot direkt nach Fill —
            # schliesst Timing-Gap zwischen Fill und naechstem Trader-Cycle.
            # Reconcile-Cron sieht damit immer aktuelles Cash + Positions.
            # Defensive: Helper faengt alle Errors (Order-Result unbeeinflusst).
            self._snapshot_after_fill_safely(status, contract.symbol)

            # 4. eToro-kompatibles Response
            return {
                "orderForOpen": {
                    "orderID": str(trade.order.orderId),
                    "statusID": status,
                    "filledQuantity": int(fill_qty),
                    "avgFillPrice": float(avg_fill_price or 0),
                    # E2-Calibrator: was wollten wir vs. was kriegten wir
                    "intendedPrice": float(intended_price or 0),
                    "refQuote": float(price or 0),
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
        # v37dc (06.05.2026): Visibility-Fix. Bisher: warning + ignoriert silent
        # weiter. Carlos hat 06.05. Inkonsistenz Trades-Tab(2x) vs Positionen-
        # Tab(1x) entdeckt — Bot loggte intended-Leverage, IBKR-Realitaet war
        # 1x. Result: Bot's Position-Sizing rechnete mit 2x Cash-Faktor, real
        # 1x → Position 2x zu gross relativ zu Bot's Risk-Annahme.
        # Fix: leverage explizit clampen + clearer warning. Plus Result-Dict
        # bekommt 'leverage_actual: 1' damit trade_history.json korrekt loggt
        # was der Bot WIRKLICH ausgefuehrt hat (Single-Source-of-Truth Reality).
        if leverage != 1:
            log.warning(
                "IbkrBroker.buy: intended leverage=%d wird auf 1x geclampt — "
                "IBKR Stock-Trades nutzen Account-Margin, nicht Order-Param. "
                "Position wird mit voller Cash-Belastung ausgefuehrt.",
                leverage,
            )
        result = self._place_market_order(
            instrument_id, amount_usd, "BUY",
            stop_loss_pct=stop_loss, take_profit_pct=take_profit,
        )
        if result is not None and isinstance(result, dict):
            result["leverage_actual"] = 1  # v37dc: Reality-Marker
        return result

    def sell(self, instrument_id, amount_usd, leverage=1):
        """Market-SELL by Amount USD (Short-Open)."""
        if leverage != 1:
            log.warning(
                "IbkrBroker.sell: intended leverage=%d wird auf 1x geclampt",
                leverage,
            )
        result = self._place_market_order(instrument_id, amount_usd, "SELL")
        if result is not None and isinstance(result, dict):
            result["leverage_actual"] = 1
        return result

    def _place_close_order_adaptive(
        self, contract, action: str, qty: int,
        fill_timeout: float = 30.0,
        purpose: str = "close",
        instrument_id=None,
    ) -> Optional[dict]:
        """v37h+2 (15.05.2026, Q3-10): Adaptive LIMIT->MARKET Close-Order.

        Carlos's SLV-Bug 14.05.2026: close_position platzierte LimitOrder mit
        0.5%% Slippage-Buffer. In schnell fallenden Maerkten verfehlt der
        Limit-Preis den Markt sofort -> Order haengt 'Submitted' ohne Fill.
        SLV-Position blieb ~21h ungefuellt waehrend Preis weiter fiel von
        $77.72 auf $70.16 (-9.7%%). Bei Cutover mit echtem Geld waere das
        ein verlorener SL = unbeschraenkter Drawdown.

        Loesung: 2-Phasen-Adaptive:
          Phase 1: LIMIT mit Slippage-Buffer (besserer Preis bei normalen
                   Maerkten, paper-account-kompatibel)
          Phase 2: Bei Timeout/0-Fill -> Cancel LIMIT + MARKET-Fallback
                   (garantierter Fill, Slippage akzeptiert)

        Bei partial-fill aus LIMIT wird der Rest via MARKET nachgefuellt.
        Aggregiert weighted-avg-Preis ueber beide Fills.

        Args:
            contract: ib_insync.Contract
            action: 'SELL' oder 'BUY' (opposite-side fuer Close)
            qty: Anzahl Aktien/Kontrakte zu schliessen
            fill_timeout: Sekunden bis MARKET-Fallback (Default 30)
            purpose: Logging-Label ('close', 'partial_close', 'sl_close', etc.)

        Returns:
            Dict mit orderForOpen + Aggregat-Metadaten + _used_market_fallback,
            None bei Setup-Fehler (kein Quote, kein contract).
        """
        from ib_insync import LimitOrder, MarketOrder
        from app.ibkr_contract_resolver import get_quote

        ib = self._get_ib()

        quote = get_quote(ib, contract)
        if quote is None or quote <= 0:
            log.error("%s: kein Quote fuer %s — abgebrochen", purpose, contract.symbol)
            return None

        # v37h+2 (Q3-12, 15.05.2026): outsideRth analog zu _place_market_order.
        # Vorher fehlte das im Q3-10-Helper -> Close-Versuche waehrend Pre-/Post-
        # Market wurden silent nicht gefuellt, MARKET-Fallback auch nicht ->
        # SL effektiv tot in off-hours. Bug der Q3-10's Daseinszweck untergrub.
        # Asset-Class-aware: bei US-Stocks rth_only, bei Forex/Crypto outsideRth=True.
        # instrument_id ist optional — wenn nicht gegeben: Default-Behavior (False).
        outside_rth = False
        if instrument_id is not None:
            try:
                settings = self._resolve_order_settings(instrument_id, None)
                outside_rth = settings.get("outside_rth", False)
            except Exception as e:
                log.debug("%s: _resolve_order_settings fehlgeschlagen "
                          "(non-fatal, default outsideRth=False): %s", purpose, e)

        slippage_sign = 1 if action == "BUY" else -1
        limit_price = round(quote * (1 + slippage_sign * self.limit_slippage_pct / 100.0), 2)

        # Phase 1: LIMIT mit Slippage-Buffer
        log.info("%s LIMIT %s %d %s @ $%.2f (quote $%.2f, slip %s%%, outsideRth=%s)",
                 purpose.upper(), action, qty, contract.symbol, limit_price,
                 quote, self.limit_slippage_pct, outside_rth)
        limit_order = LimitOrder(action, qty, limit_price)
        limit_order.outsideRth = outside_rth
        limit_trade = ib.placeOrder(contract, limit_order)

        # v37h+2 (Q3-14, 15.05.2026): E27-Tracker-Register fuer Close-Orders.
        # Vorher fehlte das im Adaptive Helper -> Close-Orders verloren Real-
        # Time-Status-Updates, trade_history inkonsistent Buy-vs-Close-Pfade.
        # Defensive: nur registrieren wenn Tracker aktiv.
        if self._e27_enabled and self._tracker is not None:
            try:
                self._tracker.register(
                    order_id=limit_trade.order.orderId,
                    trade_entry={
                        "symbol": contract.symbol,
                        "action": action,
                        "order_id": str(limit_trade.order.orderId),
                        "instrument_id": instrument_id,
                        "status": "submitted",
                        "purpose": purpose,  # 'close' / 'partial_close'
                        "ibkr_status_raw": (
                            limit_trade.orderStatus.status
                            if limit_trade.orderStatus else ""
                        ),
                    },
                )
            except Exception as e:
                log.warning("E27 register fuer close-LIMIT failed (non-fatal): %s", e)

        deadline = time.time() + fill_timeout
        while time.time() < deadline:
            ib.sleep(0.2)
            if limit_trade.isDone():
                break

        limit_fill_qty = int(limit_trade.orderStatus.filled or 0)
        limit_avg_price = float(limit_trade.orderStatus.avgFillPrice or 0.0)
        used_market_fallback = False
        market_fill_qty = 0
        market_avg_price = 0.0
        market_order_id = None

        # Phase 2: MARKET-Fallback wenn nicht vollstaendig gefuellt
        if limit_fill_qty < qty:
            remaining_qty = qty - limit_fill_qty
            # Erst LIMIT canceln falls noch aktiv
            if not limit_trade.isDone():
                log.warning(
                    "%s LIMIT-Order nach %.0fs noch %s (fill %d/%d) — Cancel + MARKET-Fallback",
                    purpose, fill_timeout, limit_trade.orderStatus.status,
                    limit_fill_qty, qty,
                )
                try:
                    ib.cancelOrder(limit_trade.order)
                    cancel_deadline = time.time() + 5.0
                    while time.time() < cancel_deadline and not limit_trade.isDone():
                        ib.sleep(0.2)
                    # Final-State nach Cancel lesen
                    limit_fill_qty = int(limit_trade.orderStatus.filled or 0)
                    limit_avg_price = float(limit_trade.orderStatus.avgFillPrice or 0.0)
                    remaining_qty = qty - limit_fill_qty
                except Exception as e:
                    log.error("%s Cancel-LIMIT failed: %s", purpose, e)

            # MARKET fuer den Rest (falls > 0)
            if remaining_qty > 0:
                log.warning(
                    "%s MARKET-Fallback %s %d %s (LIMIT-fill %d/%d, Q3-10 Safety-Net, outsideRth=%s)",
                    purpose.upper(), action, remaining_qty, contract.symbol,
                    limit_fill_qty, qty, outside_rth,
                )
                market_order = MarketOrder(action, remaining_qty)
                market_order.outsideRth = outside_rth  # Q3-12: gleicher Hours-Mode wie LIMIT
                market_trade = ib.placeOrder(contract, market_order)
                used_market_fallback = True

                # Q3-14: auch MARKET-Fallback registrieren
                if self._e27_enabled and self._tracker is not None:
                    try:
                        self._tracker.register(
                            order_id=market_trade.order.orderId,
                            trade_entry={
                                "symbol": contract.symbol,
                                "action": action,
                                "order_id": str(market_trade.order.orderId),
                                "instrument_id": instrument_id,
                                "status": "submitted",
                                "purpose": f"{purpose}_market_fallback",
                                "ibkr_status_raw": (
                                    market_trade.orderStatus.status
                                    if market_trade.orderStatus else ""
                                ),
                            },
                        )
                    except Exception as e:
                        log.warning(
                            "E27 register fuer close-MARKET failed (non-fatal): %s", e)

                deadline = time.time() + 30.0
                while time.time() < deadline:
                    ib.sleep(0.2)
                    if market_trade.isDone():
                        break

                market_fill_qty = int(market_trade.orderStatus.filled or 0)
                market_avg_price = float(market_trade.orderStatus.avgFillPrice or 0.0)
                market_order_id = str(market_trade.order.orderId)

        # Aggregiere Fills aus beiden Orders (weighted-avg-Preis)
        total_fill_qty = limit_fill_qty + market_fill_qty
        if total_fill_qty > 0:
            agg_avg_price = (
                (limit_fill_qty * limit_avg_price + market_fill_qty * market_avg_price)
                / total_fill_qty
            )
        else:
            agg_avg_price = 0.0

        # Status: Filled wenn alles weg, sonst der finale LIMIT-Status
        if total_fill_qty >= qty:
            agg_status = "Filled"
        elif total_fill_qty > 0:
            agg_status = "PartiallyFilled"
        else:
            agg_status = limit_trade.orderStatus.status or "Submitted"

        return {
            "orderForOpen": {
                "orderID": str(limit_trade.order.orderId),
                "statusID": agg_status,
                "filledQuantity": total_fill_qty,
                "avgFillPrice": agg_avg_price,
                "intendedPrice": float(limit_price),
                "refQuote": float(quote),
            },
            "_broker": "ibkr",
            "_action": purpose,
            "_used_market_fallback": used_market_fallback,
            "_limit_fill_qty": limit_fill_qty,
            "_market_fill_qty": market_fill_qty,
            "_market_order_id": market_order_id,
        }

    def close_position(self, position_id, instrument_id=None):
        """
        Position schliessen via opposite-side Market-Order.

        IBKR braucht Contract+qty; position_id allein reicht nicht.
        Wir suchen die Position via ib.positions() und feuern eine
        Closing-Order in entgegengesetzter Richtung.

        v37h+2 (Q3-10, 15.05.2026): Nutzt _place_close_order_adaptive
        statt direkter LimitOrder. LIMIT-First mit MARKET-Fallback bei
        Timeout. Verhindert SLV-style stuck-Submitted bei fallenden
        Maerkten.

        Args:
            position_id: bei eToro UUID, bei IBKR string-form von conId.
                         Wir nutzen instrument_id wenn gegeben, sonst position_id als conId.
        """
        from ib_insync import MarketOrder

        try:
            ib = self._get_ib()
            target_con_id = None

            # v37s-Fix: instrument_id kann SOWOHL eToro-ID (klein, 1-5-stellig)
            # ALS AUCH IBKR-conId (gross, 7-10-stellig) sein. IbkrBroker.get_portfolio
            # mapped beide auf die conId (siehe positionID/instrumentID-Felder).
            # Heuristik: wenn die ID schon zu einer offenen Position als conId
            # passt -> direkt nutzen, sonst via resolve_contract als eToro-ID auflösen.
            if instrument_id is not None:
                try:
                    iid = int(instrument_id)
                except (ValueError, TypeError):
                    iid = None

                # v37ce: Drei-Stufen-Match (war zwei in v37s):
                # 1. Direct-Match: iid passt zu offener Position (qty>0)
                # 2. Already-Closed-Detection: iid sieht aus wie conId (>=8 Stellen),
                #    ist aber NICHT in ib.positions() -> Position bereits geschlossen,
                #    kein FAIL sondern "already_closed"-Sentinel zurueckgeben.
                # 3. Fallback: als kleine eToro-ID via resolve_contract aufloesen.
                # Verhindert META-style Loop-Bugs (geschlossene Position in stale
                # ib.portfolio() -> close_position wird erneut getriggert).
                if iid is not None and any(
                    getattr(p.contract, "conId", None) == iid for p in ib.positions()
                ):
                    target_con_id = iid
                elif iid is not None and iid >= 10000000:
                    # >=8 Stellen sieht nach IBKR-conId aus, aber nicht offen
                    # -> bereits geschlossen
                    log.warning(
                        "close_position: conId=%s nicht (mehr) in ib.positions() "
                        "-> Position bereits geschlossen. Skip ohne Error.",
                        iid,
                    )
                    return {"_already_closed": True, "_conId": iid}
                else:
                    # Fallback: als eToro-ID via ASSET_UNIVERSE aufloesen
                    try:
                        from app.ibkr_contract_resolver import resolve_contract
                        target_con_id = resolve_contract(ib, instrument_id).conId
                    except Exception as e:
                        log.error("close_position resolve_contract failed fuer "
                                  "instrument_id=%s (weder offene conId noch eToro-ID): %s",
                                  instrument_id, e)
                        return None
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

            # v37h+2 (Q3-10): Adaptive LIMIT->MARKET via Helper.
            # Verhindert haengende Submitted-Orders in fallenden Maerkten.
            # Q3-12: instrument_id propagiert fuer outsideRth-Bestimmung.
            result = self._place_close_order_adaptive(
                contract=pos.contract,
                action=action,
                qty=qty,
                fill_timeout=self.fill_timeout_s,
                purpose="close",
                instrument_id=target_con_id,
            )
            if result is None:
                return None
            result["_closed_position_id"] = str(target_con_id)
            return result
        except Exception as e:
            log.exception("close_position failed: %s", e)
            return None

    def partial_close(self, position_id, pct_of_position: float, instrument_id=None):
        """
        v37h Tab-Audit-Day-2 (12.05.2026): Teil-Schliessung einer Position.

        Carlos's Beobachtung: PARTIAL_SIGNAL-Code in trader.py loggte nur
        TP-Tranchen-Erreichungen (eToro hatte kein partial-close). Bei
        IBKR-Migration wurde der Code-Pfad nicht aktualisiert -> 60%% der
        Strategie (TP-1 +4%%, TP-2 +8%%) waren effektiv tot.

        Fix: echte partial-close via opposite-side LimitOrder mit qty_close
        statt full-qty. Volle Position-Resolution-Logic dupliziert (mit
        close_position konsistent halten, kein gemeinsamer Helper bisher).

        Args:
            position_id: eToro UUID oder IBKR conId.
            pct_of_position: 0.0-100.0 — wieviel Prozent der offenen Qty
                             geschlossen werden sollen.
            instrument_id: optional, wenn position_id nicht conId ist.

        Returns:
            dict mit orderForOpen + _close_qty / _remaining_qty,
            oder None bei Fehler,
            oder {"_already_closed": True, ...} wenn Position weg ist.
        """
        from ib_insync import LimitOrder

        if not (0 < pct_of_position <= 100):
            log.error("partial_close: pct_of_position muss 0-100 sein, war %s",
                      pct_of_position)
            return None

        try:
            ib = self._get_ib()
            target_con_id = None

            # Position-Resolution (analog close_position)
            if instrument_id is not None:
                try:
                    iid = int(instrument_id)
                except (ValueError, TypeError):
                    iid = None

                if iid is not None and any(
                    getattr(p.contract, "conId", None) == iid for p in ib.positions()
                ):
                    target_con_id = iid
                elif iid is not None and iid >= 10000000:
                    log.warning(
                        "partial_close: conId=%s nicht (mehr) in ib.positions() "
                        "-> Position bereits geschlossen. Skip ohne Error.",
                        iid,
                    )
                    return {"_already_closed": True, "_conId": iid}
                else:
                    try:
                        from app.ibkr_contract_resolver import resolve_contract
                        target_con_id = resolve_contract(ib, instrument_id).conId
                    except Exception as e:
                        log.error("partial_close resolve_contract failed: %s", e)
                        return None
            else:
                try:
                    target_con_id = int(position_id)
                except (ValueError, TypeError):
                    log.error("partial_close: position_id '%s' ist keine conId",
                              position_id)
                    return None

            positions = [p for p in ib.positions() if getattr(p.contract, "conId", None) == target_con_id]
            if not positions:
                log.error("partial_close: keine offene Position fuer conId=%s", target_con_id)
                return None

            pos = positions[0]
            full_qty = abs(int(pos.position))
            # Quantity-Berechnung mit round-off + Min-Order-Schutz
            qty_close = int(round(full_qty * pct_of_position / 100.0))
            # Sicherheits-Caps:
            # 1. Min 1 share (sonst sinnloser Trade)
            # 2. Max full_qty - 1 (wir wollen nicht versehentlich komplett schliessen)
            # Bei pct=100 koennte der Caller stattdessen close_position() nutzen.
            if qty_close < 1:
                log.warning(
                    "partial_close: %.1f%% von %d shares = %d (zu klein) — skip",
                    pct_of_position, full_qty, qty_close,
                )
                return {"_skipped_too_small": True, "_full_qty": full_qty,
                        "_pct": pct_of_position}
            if qty_close >= full_qty:
                log.warning(
                    "partial_close: %.1f%% von %d = %d (>=full) — Caller sollte "
                    "close_position() nutzen. Begrenze auf full_qty-1=%d.",
                    pct_of_position, full_qty, qty_close, full_qty - 1,
                )
                qty_close = full_qty - 1
                if qty_close < 1:
                    return {"_skipped_too_small": True, "_full_qty": full_qty}

            remaining_qty = full_qty - qty_close
            action = "SELL" if pos.position > 0 else "BUY"

            log.info("PARTIAL_CLOSE %s qty=%d (%.1f%% von %d, %d bleibt) %s",
                     pos.contract.symbol, qty_close, pct_of_position, full_qty,
                     remaining_qty, action)

            # v37h+2 (Q3-10): Adaptive LIMIT->MARKET via Helper (gleich wie close_position).
            # Q3-12: instrument_id propagiert fuer outsideRth-Bestimmung.
            result = self._place_close_order_adaptive(
                contract=pos.contract,
                action=action,
                qty=qty_close,
                fill_timeout=self.fill_timeout_s,
                purpose="partial_close",
                instrument_id=target_con_id,
            )
            if result is None:
                return None
            # partial_close-spezifische Metadaten ergaenzen
            result["_close_qty"] = qty_close
            result["_remaining_qty"] = remaining_qty
            result["_full_qty"] = full_qty
            result["_pct_closed"] = pct_of_position
            result["_closed_position_id"] = str(target_con_id)
            return result
        except Exception as e:
            log.exception("partial_close failed: %s", e)
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
