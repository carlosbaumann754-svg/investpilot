"""
E27 Order-Status-Tracker — Reality-Aware Trade-Logging (v37e)
=============================================================

Persistent State-Manager fuer Order-Lifecycle. Subscribes auf ib_insync
orderStatusEvent. Bei jedem Status-Change: trade_history-Eintrag wird live
aktualisiert (statt 30-Min-Lag wie v37dh + Reconcile-Cron).

Behebt das letzte Stueck des "intent vs reality"-Pattern: Post-Submit-
Status-Aenderungen (Order pending → spaeter cancelled von IBKR) werden
sofort im Bot-Log korrekt reflektiert.

Foundation-Stueck fuer kuenftige Bot-Klone (E6/E7/E8): ein Tracker,
mehrere Bots.

Architektur-Entscheidungen:
- Thread-safe via RLock (Reconcile-Cron + Event-Handler schreiben parallel)
- Persistent State in `data/pending_orders.json` (Recovery nach Bot-Restart)
- atomic save_json (existing config_manager.save_json macht das schon)
- Feature-Flag-Schutz: Tracker ist nur aktiv wenn config sagt
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("OrderStatusTracker")

# IBKR-Status-Strings die als "final" gelten (nichts mehr zu erwarten).
# Plus PartiallyFilled = Spezialfall, kann weiter Filled werden.
IBKR_FINAL_STATUSES = {
    "Filled",
    "Cancelled",
    "ApiCancelled",
    "Inactive",
    "Rejected",
}

# Fuer Tracking aktive Statuses (= weiter beobachten)
IBKR_PENDING_STATUSES = {
    "Submitted",
    "PreSubmitted",
    "PendingSubmit",
    "PendingCancel",
    "ApiPending",
    "PartiallyFilled",
}


class OrderStatusTracker:
    """Persistent State-Manager fuer Order-Lifecycle.

    Usage:
        tracker = OrderStatusTracker(data_dir=Path("data"))

        # Bei Order-Submit:
        tracker.register(order_id=12345, trade_entry={...})

        # IBKR-Event (subscribed via ib.orderStatusEvent):
        tracker.handle_status_event(trade)

        # Bei Bot-Restart:
        n = tracker.recover_from_ibkr(ib)

        # Periodisch (z.B. taeglich):
        tracker.cleanup_resolved(max_age_hours=24)
    """

    PENDING_FILE = "pending_orders.json"

    def __init__(self, data_dir: Optional[Path] = None,
                 status_mapper: Optional[Any] = None):
        """
        Args:
            data_dir: Verzeichnis fuer pending_orders.json (None = config_manager-default)
            status_mapper: Funktion (ibkr_status: str) -> bot_status: str.
                          Default = _map_ibkr_status_to_bot_status aus app.trader
                          (lazy-imported zur Vermeidung von Circular-Imports).
        """
        self._lock = threading.RLock()
        self._pending: dict[str, dict] = {}
        self._data_dir = data_dir
        self._status_mapper = status_mapper
        self._load_state()

    # ============================================================
    # PUBLIC API
    # ============================================================

    def register(self, order_id: int, trade_entry: dict,
                 trade_history_index: Optional[int] = None) -> None:
        """Registriere neue Order beim Submit.

        Args:
            order_id: IBKR-Order-ID (aus trade.order.orderId)
            trade_entry: Bot's Trade-History-Eintrag (snapshot)
            trade_history_index: Position im trade_history.json (fuer In-Place-Update)
        """
        if order_id is None:
            log.debug("register: order_id=None, skipped")
            return

        key = str(order_id)
        with self._lock:
            self._pending[key] = {
                "registered_at": datetime.now(timezone.utc).isoformat(),
                "symbol": trade_entry.get("symbol"),
                "action": trade_entry.get("action"),
                "amount_usd": trade_entry.get("amount_usd"),
                "trade_history_index": trade_history_index,
                "current_status": trade_entry.get("ibkr_status_raw") or trade_entry.get("status"),
                "last_event_at": datetime.now(timezone.utc).isoformat(),
                "trade_entry_snapshot": dict(trade_entry),  # Defensiv-Copy
            }
            self._save_state()
            log.info("E27 register: order_id=%s symbol=%s status=%s",
                     key, trade_entry.get("symbol"), self._pending[key]["current_status"])

    def handle_status_event(self, trade) -> None:
        """ib_insync orderStatusEvent-Handler.

        Wird automatisch aufgerufen wenn IBKR einen Status-Update sendet.
        Updatet (a) internal pending-Map, (b) trade_history.json-Eintrag.

        Args:
            trade: ib_insync Trade-Objekt (mit .order, .orderStatus)
        """
        try:
            order_id = trade.order.orderId if trade.order else None
            new_status = trade.orderStatus.status if trade.orderStatus else None
            filled = float(trade.orderStatus.filled) if trade.orderStatus else 0
            avg_fill = float(trade.orderStatus.avgFillPrice or 0) if trade.orderStatus else 0
        except Exception as e:
            log.warning("E27 handle_status_event: failed to extract trade fields: %s", e)
            return

        if order_id is None or new_status is None:
            return

        key = str(order_id)
        with self._lock:
            entry = self._pending.get(key)
            if entry is None:
                # Order nicht von uns registriert (z.B. manueller Trade) — skip
                log.debug("E27 status-event fuer unbekannte Order %s — skipped", key)
                return

            old_status = entry.get("current_status")
            entry["current_status"] = new_status
            entry["last_event_at"] = datetime.now(timezone.utc).isoformat()
            entry["filled_qty"] = filled
            entry["avg_fill_price"] = avg_fill

            log.info("E27 status-event: order_id=%s %s -> %s (filled=%s @ %s)",
                     key, old_status, new_status, filled, avg_fill)

            # Update trade_history.json
            self._update_trade_history(entry, new_status, filled, avg_fill)

            # Bei Final-Status: aus pending entfernen (cleanup_resolved-faehig)
            if new_status in IBKR_FINAL_STATUSES:
                entry["resolved_at"] = datetime.now(timezone.utc).isoformat()

            self._save_state()

    def recover_from_ibkr(self, ib) -> int:
        """Nach Bot-Restart: pending Orders gegen IBKR sync.

        Findet alle pending_orders.json-Eintraege ohne Final-Status.
        Fragt IBKR's aktuelle openTrades + completedOrders ab.
        Updatet Status entsprechend.

        Returns:
            Anzahl resolved entries.
        """
        if not ib:
            return 0

        resolved_count = 0
        try:
            # Aktive Orders aus IBKR fetchen
            ib.reqAllOpenOrders()
            ib.sleep(1.0)
            current_trades = list(ib.openTrades() or [])
            current_trades += list(ib.trades() or [])  # Session-trades inkl. cancelled
        except Exception as e:
            log.warning("E27 recover: IBKR fetch failed: %s", e)
            return 0

        with self._lock:
            for key, entry in list(self._pending.items()):
                if entry.get("current_status") in IBKR_FINAL_STATUSES:
                    continue
                # Such matching IBKR-Trade
                for trade in current_trades:
                    try:
                        if str(trade.order.orderId) == key:
                            new_status = trade.orderStatus.status
                            if new_status != entry.get("current_status"):
                                self.handle_status_event(trade)
                                resolved_count += 1
                            break
                    except Exception:
                        continue

        log.info("E27 recovery: %d pending orders synchronized", resolved_count)
        return resolved_count

    def cleanup_resolved(self, max_age_hours: int = 24) -> int:
        """Loesche Final-Status-Eintraege aelter als max_age_hours.

        Hilft pending_orders.json klein zu halten. Wird typischerweise
        einmal taeglich aufgerufen.

        Returns:
            Anzahl geloeschter Eintraege.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        deleted = 0
        with self._lock:
            for key in list(self._pending.keys()):
                entry = self._pending[key]
                if entry.get("current_status") not in IBKR_FINAL_STATUSES:
                    continue
                resolved_at = entry.get("resolved_at")
                if not resolved_at:
                    continue
                try:
                    ts = datetime.fromisoformat(resolved_at.replace("Z", "+00:00"))
                    if ts < cutoff:
                        del self._pending[key]
                        deleted += 1
                except Exception:
                    continue
            if deleted:
                self._save_state()
                log.info("E27 cleanup: %d resolved entries deleted (older than %dh)",
                         deleted, max_age_hours)
        return deleted

    def get_pending_count(self) -> int:
        """Anzahl aktuell pending Orders (nicht-final)."""
        with self._lock:
            return sum(1 for e in self._pending.values()
                       if e.get("current_status") not in IBKR_FINAL_STATUSES)

    # ============================================================
    # INTERNAL
    # ============================================================

    def _map_status(self, ibkr_status: str) -> str:
        """IBKR-Status -> Bot-Status. Lazy-Import um Circular-Import zu vermeiden."""
        if self._status_mapper is None:
            try:
                from app.trader import _map_ibkr_status_to_bot_status
                self._status_mapper = _map_ibkr_status_to_bot_status
            except Exception:
                # Fallback: identity
                self._status_mapper = lambda s: s
        return self._status_mapper(ibkr_status)

    def _update_trade_history(self, entry: dict, new_ibkr_status: str,
                              filled_qty: float, avg_fill_price: float) -> None:
        """Update den entsprechenden trade_history.json-Eintrag."""
        try:
            from app.config_manager import load_json, save_json
        except Exception:
            log.warning("E27 _update_trade_history: config_manager nicht verfuegbar")
            return

        history = load_json("trade_history.json") or []
        if not history:
            return

        # Find entry: erst via index, dann via order_id
        idx = entry.get("trade_history_index")
        target = None
        if idx is not None and 0 <= idx < len(history):
            cand = history[idx]
            if str(cand.get("order_id")) == str(entry.get("order_id", "")) or \
               cand.get("symbol") == entry.get("symbol"):
                target = cand

        if target is None:
            # Fallback: search by order_id
            order_id_str = None
            snap = entry.get("trade_entry_snapshot", {})
            order_id_str = snap.get("order_id")
            if order_id_str:
                for t in reversed(history):  # neuestes zuerst
                    if str(t.get("order_id")) == str(order_id_str):
                        target = t
                        break

        if target is None:
            log.debug("E27 _update_trade_history: kein matching trade_history-Eintrag gefunden")
            return

        # Update Fields
        bot_status = self._map_status(new_ibkr_status)
        target["status"] = bot_status
        target["ibkr_status_raw"] = new_ibkr_status
        target["_e27_last_update"] = datetime.now(timezone.utc).isoformat()
        if filled_qty > 0:
            target["filled_qty"] = filled_qty
        if avg_fill_price > 0:
            target["avg_fill_price"] = avg_fill_price

        save_json("trade_history.json", history)
        log.info("E27 trade_history updated: symbol=%s status=%s", target.get("symbol"), bot_status)

    def _load_state(self) -> None:
        """Lade pending_orders.json beim Init."""
        try:
            from app.config_manager import load_json
        except Exception:
            return

        data = load_json(self.PENDING_FILE)
        if data and isinstance(data, dict):
            self._pending = data.get("pending", {}) or {}
            log.info("E27 state loaded: %d pending orders", len(self._pending))

    def _save_state(self) -> None:
        """Persistiere pending_orders.json (caller hat lock)."""
        try:
            from app.config_manager import save_json
            save_json(self.PENDING_FILE, {
                "version": 1,
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "pending": self._pending,
            })
        except Exception as e:
            log.warning("E27 _save_state failed: %s", e)
