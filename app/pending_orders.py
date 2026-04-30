"""
Pending-Orders-Visibility (v37bb, Lite-D).

Read-only Helper fuer pending IBKR-Orders. Liefert die aktuelle Liste
von pending Orders direkt von ib_insync.openTrades(). Diagnostik fuer
Dashboard + Reconcile-Korrelation.

WARUM nur Read-only (statt persistente State):
- Echte Pending-Persistenz erfordert State-Sync zwischen IBKR-Session-Cache
  und Bot-internem Tracking — komplex, anfaellig fuer Race-Conditions.
- v37aa Reconcile-Pending-Fix nutzt schon ib.openTrades(), was reicht fuer
  die Drift-Pruefung.
- Vor Cutover lieber visibility-only als komplexer State-Code mit
  potentiellen Bugs.

Wenn nach Cutover echte Persistenz noetig: separater W6+ Engineering-Item.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def get_live_pending_orders(timeout: int = 10) -> list[dict]:
    """Liefert Liste der aktuell pending IBKR-Orders.

    Liest ueber separate IBKR-Connection (clientId=98 readonly) damit
    Bot-Hauptprozess nicht gestoert wird.

    Returns:
        Liste von Dicts mit {order_id, symbol, action, qty, price, status,
        time_submitted, time_acted}.
        Leer wenn keine pending oder Connection fehlt.
    """
    try:
        from app.ibkr_client import IbkrBroker
        broker = IbkrBroker({"ibkr": {"client_id": 98, "readonly": True}})
    except Exception as e:
        logger.debug(f"IbkrBroker Setup fehlgeschlagen: {e}")
        return []

    try:
        ib = broker._get_ib()
        try:
            ib.reqAllOpenOrders()
            ib.sleep(1.0)
        except Exception:
            pass

        result = []
        try:
            for t in (ib.openTrades() or []):
                if not (t.contract and t.order and t.orderStatus):
                    continue
                status = t.orderStatus.status or ""
                # Nur tatsaechlich pending Status
                if status not in ("Submitted", "PreSubmitted", "PendingSubmit",
                                  "PendingCancel"):
                    continue
                result.append({
                    "order_id": int(t.order.orderId) if t.order.orderId else None,
                    "perm_id": int(t.order.permId) if t.order.permId else None,
                    "symbol": t.contract.symbol,
                    "conId": int(t.contract.conId) if t.contract.conId else None,
                    "action": t.order.action,
                    "qty": float(t.order.totalQuantity or 0),
                    "filled_qty": float(t.orderStatus.filled or 0),
                    "remaining_qty": float(t.orderStatus.remaining or 0),
                    "limit_price": float(t.order.lmtPrice or 0),
                    "status": status,
                    "order_type": t.order.orderType,
                    "tif": t.order.tif,
                })
        except Exception as e:
            logger.debug(f"openTrades-Parse fehlgeschlagen: {e}")

        return result

    except Exception as e:
        logger.warning(f"Pending-Orders-Fetch fehlgeschlagen: {e}")
        return []
    finally:
        try:
            broker.disconnect()
        except Exception:
            pass


def summary() -> dict:
    """Aggregierte Stats fuer Dashboard-Card."""
    pending = get_live_pending_orders()
    by_status: dict[str, int] = {}
    by_action: dict[str, int] = {}
    for p in pending:
        by_status[p["status"]] = by_status.get(p["status"], 0) + 1
        by_action[p["action"]] = by_action.get(p["action"], 0) + 1
    return {
        "total_pending": len(pending),
        "by_status": by_status,
        "by_action": by_action,
        "orders": pending,
    }
