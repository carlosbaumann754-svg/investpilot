"""
Entnahme-Planer (Withdrawal Scheduler)
=======================================

Schrittweise Liquidation eines Teilportfolios fuer groessere Ausgaben
(Auto, Haus, etc.) mit Zielbetrag + Zeithorizont. Reduziert Risiko von
"alles am Tief verkauft" durch zeitliche Verteilung.

Status (W7): MVP — Plan-Persistenz + API + Dashboard. Trader-Integration
ist als TODO markiert; bei aktivem Plan reduziert der Bot heute noch
KEINE Buys / triggert keine zusaetzlichen Sells. Das wird in W8+ erweitert.

Plan-Schema (data/withdrawal_plan.json):
{
    "active": true,
    "target_amount_usd": 5000,
    "deadline": "2026-06-30",
    "created_at": "2026-04-25T15:00:00",
    "strategy": "fifo",  # "fifo" | "lifo" | "tax_optimal" — heute nur fifo
    "withdrawn_so_far_usd": 0,
    "notes": "Auto-Anzahlung",
    "history": []  # Liste der bereits liquidierten Tranchen
}

CLI:
    python -m app.withdrawal_planner status
    python -m app.withdrawal_planner plan --amount 5000 --deadline 2026-06-30
    python -m app.withdrawal_planner cancel
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, date
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

PLAN_FILE = "withdrawal_plan.json"
VALID_STRATEGIES = {"fifo", "lifo", "tax_optimal"}


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load_plan() -> Optional[dict]:
    """Liest aktuellen Plan oder None wenn keiner aktiv."""
    from app.config_manager import load_json
    plan = load_json(PLAN_FILE)
    if not plan or not plan.get("active"):
        return None
    return plan


def save_plan(plan: Optional[dict]) -> None:
    """Persistiert Plan (oder None = keinen aktiven Plan)."""
    from app.config_manager import save_json
    save_json(PLAN_FILE, plan or {})


def create_plan(
    target_amount_usd: float,
    deadline: str,
    strategy: str = "fifo",
    notes: str = "",
) -> dict:
    """Erstellt einen neuen aktiven Plan. Ueberschreibt vorhandenen Plan
    (mit Backup in history)."""
    if target_amount_usd <= 0:
        raise ValueError(f"target_amount_usd muss > 0 sein, war {target_amount_usd}")
    try:
        deadline_dt = date.fromisoformat(deadline)
    except ValueError as e:
        raise ValueError(f"deadline '{deadline}' muss ISO-Datum (YYYY-MM-DD) sein: {e}")
    if deadline_dt <= date.today():
        raise ValueError(f"deadline {deadline} muss in der Zukunft liegen")
    if strategy not in VALID_STRATEGIES:
        raise ValueError(f"strategy '{strategy}' ungueltig. Erlaubt: {sorted(VALID_STRATEGIES)}")

    existing = load_plan()
    history = existing.get("history", []) if existing else []
    if existing:
        history.append({
            "replaced_at": _now_iso(),
            "previous": {k: v for k, v in existing.items() if k != "history"},
        })

    plan = {
        "active": True,
        "target_amount_usd": float(target_amount_usd),
        "deadline": deadline,
        "created_at": _now_iso(),
        "strategy": strategy,
        "withdrawn_so_far_usd": 0.0,
        "notes": notes,
        "history": history,
    }
    save_plan(plan)
    log.info(
        "Withdrawal plan created: $%.2f bis %s (strategy=%s, notes='%s')",
        target_amount_usd, deadline, strategy, notes,
    )
    return plan


def cancel_plan() -> Optional[dict]:
    """Storniert aktiven Plan. Returns gespeicherten Plan oder None."""
    plan = load_plan()
    if not plan:
        return None
    plan["active"] = False
    plan["cancelled_at"] = _now_iso()
    save_plan(plan)
    log.info("Withdrawal plan cancelled at %s", plan["cancelled_at"])
    return plan


def get_status() -> dict:
    """Status-Snapshot fuer Dashboard."""
    plan = load_plan()
    if not plan:
        return {"active": False}

    today = date.today()
    deadline_dt = date.fromisoformat(plan["deadline"])
    days_left = (deadline_dt - today).days
    target = float(plan["target_amount_usd"])
    withdrawn = float(plan.get("withdrawn_so_far_usd", 0))
    remaining = max(0.0, target - withdrawn)
    progress_pct = (withdrawn / target * 100) if target > 0 else 0.0

    # Empfohlene Tagesrate (gleichmaessig verteilt) — heuristisch
    daily_rate = (remaining / max(1, days_left)) if days_left > 0 else remaining

    return {
        "active": True,
        "target_amount_usd": target,
        "withdrawn_so_far_usd": withdrawn,
        "remaining_usd": remaining,
        "progress_pct": round(progress_pct, 2),
        "deadline": plan["deadline"],
        "days_left": days_left,
        "strategy": plan.get("strategy", "fifo"),
        "notes": plan.get("notes", ""),
        "recommended_daily_liquidation_usd": round(daily_rate, 2),
        "created_at": plan.get("created_at"),
    }


# ----------------------------------------------------------------------
# Trader-Integration Hooks (TODO W8 — heute Stubs)
# ----------------------------------------------------------------------

def adjust_buy_amount(planned_amount_usd: float) -> float:
    """Wenn Plan aktiv: reduziert geplante Buys (lass Cash fuer Liquidation).

    TODO W8: Aktuell pass-through. Implementierung:
        - days_left berechnen
        - remaining_to_withdraw / days_left = pro-Tag-Bedarf
        - Buy-Reduzierung proportional
    """
    return planned_amount_usd


def should_force_sell() -> Optional[dict]:
    """Wenn Plan aktiv: returnt Sell-Anweisung fuer naechsten Cycle.

    TODO W8: Aktuell None (kein force-sell). Implementierung:
        - daily_rate vs withdrawn_today vergleichen
        - Wenn unter Plan: Position nach FIFO/LIFO/tax_optimal waehlen
        - Return: {"position_id": ..., "amount_usd": ...}
    """
    return None


def record_withdrawal(amount_usd: float, source: str = "manual") -> None:
    """Logge eine durchgefuehrte Liquidation. Aktualisiert withdrawn_so_far_usd."""
    plan = load_plan()
    if not plan:
        log.warning("record_withdrawal called without active plan — ignoriert")
        return
    plan["withdrawn_so_far_usd"] = float(plan.get("withdrawn_so_far_usd", 0)) + float(amount_usd)
    plan.setdefault("liquidations", []).append({
        "ts": _now_iso(),
        "amount_usd": float(amount_usd),
        "source": source,
    })
    if plan["withdrawn_so_far_usd"] >= plan["target_amount_usd"]:
        plan["completed_at"] = _now_iso()
        plan["active"] = False
        log.info("Withdrawal plan COMPLETED: $%.2f reached", plan["withdrawn_so_far_usd"])
    save_plan(plan)


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="InvestPilot Entnahme-Planer")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("status", help="Aktuellen Plan-Status zeigen")

    plan_parser = sub.add_parser("plan", help="Neuen Plan erstellen")
    plan_parser.add_argument("--amount", type=float, required=True, help="Zielbetrag USD")
    plan_parser.add_argument("--deadline", required=True, help="ISO-Datum YYYY-MM-DD")
    plan_parser.add_argument("--strategy", default="fifo", choices=sorted(VALID_STRATEGIES))
    plan_parser.add_argument("--notes", default="")

    sub.add_parser("cancel", help="Aktiven Plan stornieren")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.cmd == "status":
        print(json.dumps(get_status(), indent=2, default=str))
    elif args.cmd == "plan":
        plan = create_plan(args.amount, args.deadline, args.strategy, args.notes)
        print(json.dumps(get_status(), indent=2, default=str))
    elif args.cmd == "cancel":
        plan = cancel_plan()
        if plan is None:
            print("Kein aktiver Plan zum stornieren.")
        else:
            print(json.dumps(plan, indent=2, default=str))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
