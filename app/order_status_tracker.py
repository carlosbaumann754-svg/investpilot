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
    "Stale",  # v37e Tag 3: Custom-Status fuer no-match-after-bot-offline
}

# R-A49 (29.05.2026 Sprint-Tag-18): Pure-Function Helper fuer Triple-Source-
# Recovery. Wird in recover_from_ibkr() genutzt um pending Orders gegen Bot's
# eigene trade_history.json zu cross-referenzieren — Defense-in-Depth-Layer
# falls IBKR-Server-Side-Quellen (openTrades + completedOrders) die Order
# bereits archiviert haben oder ein orderStatusEvent zwischen Fill und Bot-
# Reconnect verloren ging.
# R-A49: Trade-Status-Strings die signalisieren "Order ist noch NICHT final
# executed" — diese Trades werden bei der Cross-Ref aus pending_orders ausge-
# schlossen. Beispiel: ein Trade-Entry mit status="submitted" wurde geloggt
# (Submit erfolgreich) aber IBKR hat noch keinen Fill bestaetigt — wenn die
# Order in pending_orders.json hängt, ist sie genau in dem Zustand wo sie der
# Cross-Ref-Layer NICHT als "schon executed" markieren darf.
# Trades OHNE status-Feld (Legacy + manche SCANNER_BUY-Logs) werden als
# executed gewertet — Bot's eigener Logging-Layer schreibt sie nur nach
# tatsaechlicher Execution.
_R_A49_NON_EXECUTED_STATUSES = {
    "submitted", "presubmitted", "pendingsubmit", "pendingcancel",
    "apipending", "stale", "rejected", "cancelled", "apicancelled",
    "inactive", "failed",
}


def _executed_order_ids_from_trade_history(trade_history) -> set[str]:
    """R-A49: extrahiere Order-IDs aus trade_history.json (as string).

    Pure function — testbar ohne IBKR-Mock.

    Filter: Trades deren `status` (case-insensitive) in
    `_R_A49_NON_EXECUTED_STATUSES` ist, werden ausgeschlossen — sie sind
    eingeloggt aber noch nicht final filled, also kein gueltiges Recovery-
    Signal.

    Args:
        trade_history: list[dict] (oder dict mit "trades"-Key). Jeder Trade
            kann order_id-Feld haben (int oder str).

    Returns:
        set[str] der order_ids die in der Trade-History als EXECUTED stehen.
        Alle als str normalisiert fuer konsistenten Lookup gegen
        pending_orders.json keys (die auch str sind).
    """
    if isinstance(trade_history, dict):
        trades = trade_history.get("trades", [])
    elif isinstance(trade_history, list):
        trades = trade_history
    else:
        return set()
    out: set[str] = set()
    for t in trades:
        if not isinstance(t, dict):
            continue
        # R-A49 Filter: non-final status -> nicht als executed werten
        status_val = (t.get("status") or "").lower()
        if status_val in _R_A49_NON_EXECUTED_STATUSES:
            continue
        oid = t.get("order_id") or t.get("id")
        if oid is not None and str(oid).strip() and str(oid) != "-":
            out.add(str(oid))
    return out


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

    def recover_from_ibkr(self, ib, stale_after_hours: int = 48) -> dict:
        """Nach Bot-Restart: pending Orders gegen IBKR sync.

        Findet alle pending_orders.json-Eintraege ohne Final-Status.
        Fragt IBKR's aktuelle openTrades + completedOrders ab.
        Updatet Status entsprechend.

        v37e Tag 3 (Strategie B — stale-Marker):
        Wenn pending Order aelter als stale_after_hours UND NICHT in IBKR's
        History/openTrades: status='stale' setzen + resolved_at + Audit-Trail.
        Cleanup-Cron raeumt das nach max_age_hours auf.

        Hintergrund: Bot >24h offline -> IBKR Session-History gewiped.
        Wir wissen nicht ob Order filled, cancelled oder noch active war.
        'stale' macht den Unknown-Status explizit (Visibility > Optimization).

        Args:
            ib: ib_insync.IB-Instanz (oder None fuer Test-Skip)
            stale_after_hours: Schwelle ab wann no-match-pending als 'stale'
                              markiert wird. Default 48h (IBKR-Session-Cache
                              ~24h plus Buffer).

        Returns:
            {'resolved': int, 'staled': int, 'still_pending': int}
        """
        if not ib:
            return {"resolved": 0, "staled": 0, "still_pending": 0}

        resolved_count = 0
        staled_count = 0

        # R-A49 (29.05.2026 Sprint-Tag-18): Triple-Source Recovery.
        # Vor R-A49 checkte nur openTrades + trades. Wenn IBKR die Order
        # nach Fill bereits in completedOrders verschoben hatte (eine Stunde
        # oder mehr nach Fill ueblich), war sie weder in openTrades noch
        # trades → no-match → Order blieb stale in pending_orders.json bis
        # >48h alt. Symptom: 9-12 Pending-Orders die laengst executed waren,
        # Dashboard zeigte sie als "PendingSubmit" tagelang.
        # Fix: erweitere current_trades um completedOrders() + fallback auf
        # Bot's trade_history.json wenn IBKR alle Quellen leer (z.B. nach
        # mehrtaegigem Bot-Offline und IBKR-Session-Wipe).
        completed_count_log = 0
        try:
            ib.reqAllOpenOrders()
            ib.sleep(1.0)
            current_trades = list(ib.openTrades() or [])
            current_trades += list(ib.trades() or [])
            # R-A49 Stufe 2: completedOrders (Filled-Orders die IBKR aus
            # active-list bereits archiviert hat — typisch >5min nach Fill).
            try:
                ib.reqCompletedOrders(apiOnly=False)
                ib.sleep(1.0)
                completed = list(ib.completedOrders() or [])
                completed_count_log = len(completed)
                # completedOrders liefert OrderStatus-Objekte, aber wir
                # brauchen Trade-aehnliche-Schnittstelle. completedOrders
                # gibt eigentlich Trade-Objekte mit orderStatus zurueck.
                current_trades += completed
            except Exception as e:
                log.warning("E27 recover R-A49: completedOrders() fetch failed: %s", e)
        except Exception as e:
            log.warning("E27 recover: IBKR fetch failed: %s", e)
            return {"resolved": 0, "staled": 0, "still_pending": 0}

        # Build IBKR-order-id-Set fuer schnellen Lookup
        ibkr_order_ids: set[str] = set()
        ibkr_trade_by_id: dict[str, Any] = {}
        for trade in current_trades:
            try:
                oid = str(trade.order.orderId)
                ibkr_order_ids.add(oid)
                ibkr_trade_by_id[oid] = trade
            except Exception:
                continue

        # R-A49 Stufe 3: lade Bot's eigene Trade-History als zweite
        # Wahrheitsquelle. Wenn Order-ID dort gefunden -> als Filled
        # markieren (Trade-History wird nur bei tatsaechlicher Execution
        # geschrieben). Defensiver Fallback gegen IBKR-Session-Wipe.
        try:
            from app.config_manager import load_json
            trade_history_data = load_json("trade_history.json") or []
            executed_in_history = _executed_order_ids_from_trade_history(trade_history_data)
        except Exception as e:
            log.warning("E27 recover R-A49: trade_history.json load failed: %s", e)
            executed_in_history = set()

        log.info(
            "E27 recover R-A49: IBKR open=%d completed=%d, trade_history executed=%d",
            len(current_trades) - completed_count_log, completed_count_log,
            len(executed_in_history),
        )

        history_matched_count = 0
        now = datetime.now(timezone.utc)
        stale_threshold = now - timedelta(hours=stale_after_hours)

        with self._lock:
            for key, entry in list(self._pending.items()):
                if entry.get("current_status") in IBKR_FINAL_STATUSES:
                    continue

                # 1. IBKR-Match (openTrades + trades + completedOrders) -> normal resolve
                if key in ibkr_order_ids:
                    matching_trade = ibkr_trade_by_id[key]
                    try:
                        new_status = matching_trade.orderStatus.status
                        if new_status != entry.get("current_status"):
                            self.handle_status_event(matching_trade)
                            resolved_count += 1
                    except Exception:
                        continue
                    continue

                # 2. R-A49: trade_history-Match -> als Filled markieren.
                # Bot's eigene History sagt: diese Order ist executed.
                # Wir markieren als Filled (Final-Status) damit Stale-Marker
                # NICHT mehr triggert und Dashboard sauber wird.
                if key in executed_in_history:
                    entry["current_status"] = "Filled"
                    entry["resolved_at"] = now.isoformat()
                    entry["resolved_by"] = "R-A49-trade-history-cross-ref"
                    history_matched_count += 1
                    resolved_count += 1
                    continue

                # 3. Kein Match nirgendwo -> Stale-Check
                registered_at_str = entry.get("registered_at")
                if not registered_at_str:
                    continue
                try:
                    registered_at = datetime.fromisoformat(
                        registered_at_str.replace("Z", "+00:00")
                    )
                except Exception:
                    continue

                if registered_at < stale_threshold:
                    # Pending Order ist alt + nicht in IBKR + nicht in History -> stale-Marker
                    self._mark_stale(key, entry)
                    staled_count += 1

        if history_matched_count:
            log.info(
                "E27 recover R-A49: %d Orders via trade_history-Cross-Ref aufgeloest",
                history_matched_count,
            )
            # Persist nach trade_history-resolves (sonst geht der status="Filled"
            # bei Container-Restart verloren).
            try:
                self._save_state()
            except Exception as e:
                log.warning("E27 recover R-A49: save_state failed: %s", e)

        still_pending = self.get_pending_count()
        log.info(
            "E27 recovery: %d resolved, %d staled (>%dh ohne IBKR-Match), %d still pending",
            resolved_count, staled_count, stale_after_hours, still_pending,
        )
        return {
            "resolved": resolved_count,
            "staled": staled_count,
            "still_pending": still_pending,
        }

    def _mark_stale(self, key: str, entry: dict) -> None:
        """Markiere pending Order als 'stale' wenn nach Bot->24h-Offline kein IBKR-Match.

        Caller hat bereits self._lock. Updatet pending-Eintrag UND
        trade_history.json UND persistiert pending_orders.json.

        BUGFIX 10.05.2026 (Carlos's Pushover-Spam-Vorfall): vorher fehlte
        self._save_state() am Ende. Folge: Status="Stale" lebte nur in-
        memory. Beim naechsten Container-Restart oder bei einer neuen
        IbkrBroker-Instanz (Reconcile-Cron, API-Endpoints, Bot-Cycle)
        wurde pending_orders.json frisch geladen mit alter Status =
        PendingSubmit -> Stale-Check feuerte erneut -> Pushover-Spam
        alle 1-3 Min. 'Stale' war zwar in IBKR_FINAL_STATUSES (also
        Idempotenz-Check Zeile 227 wuerde greifen), aber der Marker
        wurde nie persistiert.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        entry["current_status"] = "Stale"  # Custom-Status (nicht IBKR-Status)
        entry["last_event_at"] = now_iso
        entry["resolved_at"] = now_iso
        entry["stale_reason"] = (
            "Bot war wahrscheinlich >24h offline. IBKR-Session-History "
            "enthaelt diese Order nicht mehr — final-Status nicht verifizierbar."
        )

        # trade_history.json updaten
        try:
            from app.config_manager import load_json, save_json
            history = load_json("trade_history.json") or []
            order_id_str = key
            for t in reversed(history):
                if str(t.get("order_id")) == order_id_str:
                    t["status"] = "stale"
                    t["ibkr_status_raw"] = "STALE_NO_IBKR_HISTORY"
                    t["_e27_stale_marker"] = now_iso
                    save_json("trade_history.json", history)
                    break
        except Exception as e:
            log.warning("E27 _mark_stale: trade_history update failed: %s", e)

        log.info("E27 STALE: order_id=%s symbol=%s — Bot war wahrscheinlich >24h offline",
                 key, entry.get("symbol"))

        # v37e Tag 6: Pushover-Alert bei Stale-Marker
        # Visibility-Loch geschlossen — Stale ist eine echte Anomalie
        # die manuelles IBKR-Web-Login erfordert. Priority=1 (HIGH).
        try:
            from app.alerts import send_pushover
            symbol = entry.get("symbol", "?")
            order_id = key
            registered_at = entry.get("registered_at", "?")
            msg = (
                f"STALE_ORDER: {symbol} (order_id={order_id}) "
                f">48h pending, kein IBKR-Match. "
                f"Registered: {registered_at}. "
                f"Aktion: IBKR-Web-Login (cbaumann_view) pruefen ob Position offen."
            )
            send_pushover(
                msg,
                title="InvestPilot STALE_ORDER",
                priority=1,  # HIGH — überbrückt Quiet-Hours
                sound="siren",
            )
        except Exception as exc:  # pragma: no cover — Pushover-Failure soll Stale-Marker nicht brechen
            log.warning("E27 STALE: Pushover-Alert failed: %s", exc)

        # BUGFIX 10.05.2026: pending_orders.json persistieren damit der
        # "Stale"-Marker den Cross-Instance-Idempotenz-Check ueberlebt.
        # Caller (recover_from_ibkr) haelt self._lock — _save_state ist
        # konsistent mit dem Pattern in handle_status_event/cleanup_resolved.
        self._save_state()

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


# ============================================================
# Public Helper: daily maintenance fuer scheduler.py
# ============================================================

def run_periodic_maintenance(broker, max_age_hours: int = 24,
                              stale_after_hours: int = 48) -> dict:
    """Daily Maintenance — cleanup + recovery in einem.

    Wird einmal taeglich vom Scheduler aufgerufen (nach 04:30 UTC, kurz nach
    Backup-Cron, vor Markt-Open).

    Args:
        broker: IbkrBroker-Instanz (Tracker via broker._tracker)
        max_age_hours: cleanup-Threshold fuer resolved Eintraege
        stale_after_hours: Threshold ab wann pending Order als 'stale' markiert
                          wird (siehe v37e Tag 3 Strategie B)

    Returns:
        {'enabled': bool, 'recovery': dict | None, 'cleanup_deleted': int | None}
        Bei feature-flag OFF: enabled=False, andere Keys None.
    """
    result = {"enabled": False, "recovery": None, "cleanup_deleted": None}

    if not getattr(broker, "_e27_enabled", False):
        return result

    tracker = getattr(broker, "_tracker", None)
    if tracker is None:
        log.debug("E27 maintenance skipped: tracker is None")
        return result

    result["enabled"] = True

    # 1. Recovery (synchronisiert pending vs IBKR + setzt stale-Marker)
    try:
        ib = broker._get_ib() if hasattr(broker, "_get_ib") else None
        if ib is not None:
            stats = tracker.recover_from_ibkr(ib, stale_after_hours=stale_after_hours)
            result["recovery"] = stats
            log.info("E27 daily maintenance — recovery: %s", stats)
    except Exception as e:
        log.warning("E27 daily maintenance — recovery failed (non-fatal): %s", e)

    # 2. Cleanup (entfernt resolved + stale Eintraege > max_age_hours)
    try:
        deleted = tracker.cleanup_resolved(max_age_hours=max_age_hours)
        result["cleanup_deleted"] = deleted
        if deleted:
            log.info("E27 daily maintenance — cleanup: %d resolved entries removed", deleted)
    except Exception as e:
        log.warning("E27 daily maintenance — cleanup failed (non-fatal): %s", e)

    return result
