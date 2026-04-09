"""
bootstrap_v12.py — One-Shot-Migration fuer v12-Game-Changer-Features.

Problem: Render's Persistent Disk shadowed die gebaute data/config.json,
und Git-Commits erreichen den Disk nicht. Der Gist-Snapshot (Backup-Source)
enthaelt keine v12-Sections, weshalb jeder Optimizer-Push von Render's
naechstem backup_to_cloud() wieder ueberschrieben wird.

Dieser Script wird bei jedem Container-Start (entrypoint.sh) aufgerufen.
Er merged ausschliesslich die v12-Sections (Feature-Flags + disabled_symbols)
in die lokale config.json, OHNE Optimizer-tunbare Werte (demo_trading.sl_pct,
tp_pct, min_score) anzufassen.

Design-Regeln:
  1. disabled_symbols: IMMER aus Git ueberschreiben (Git = Source of Truth)
  2. Feature-Flag-Sections (regime_strategies, time_stop, kelly_sizing,
     meta_labeling, hedging, vix_term_structure): nur INJIZIEREN wenn Section
     fehlt. Falls existiert, unveraendert lassen (Optimizer darf tunen).
  3. demo_trading.stop_loss_pct / take_profit_pct / min_scanner_score:
     NIE anfassen — der Optimizer besitzt diese Werte.
  4. Idempotent: Mehrfach-Aufruf veraendert nichts, wenn bereits migriert.
  5. Atomic Write via save_json (thread-safe Lock).

Aufruf:
    python -m app.bootstrap_v12          # Apply
    python -m app.bootstrap_v12 --check  # Dry-Run (zeigt Diff, schreibt nicht)
"""
from __future__ import annotations

import logging
import sys
from typing import Any

from app.config_manager import load_json, save_json

log = logging.getLogger("bootstrap_v12")
if not log.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s [BOOTSTRAP-V12] [%(levelname)s] %(message)s"))
    log.addHandler(h)
log.setLevel(logging.INFO)


# ============================================================
# V12 BASELINE — Source of Truth fuer Feature-Flag-Sections.
# Synchron halten mit data/config.json im Repo.
# ============================================================

V12_DISABLED_SYMBOLS = [
    "DIS", "ROKU", "VNQ", "UNH", "GOOGL", "MA", "ADBE", "SNAP", "PFE",
    "PYPL", "SHOP", "MCD", "CRM", "PLTR", "NKE", "V", "DIA", "TLT",
    "XLK", "PG", "QQQ",
]

V12_SECTIONS: dict[str, dict[str, Any]] = {
    "time_stop": {
        "enabled": True,
        "max_days_stale": 10,
        "stale_pnl_threshold_pct": 0.5,
        "min_days_open": 2,
    },
    "meta_labeling": {
        "enabled": True,
        "shadow_mode": True,
        "min_trades_to_activate": 50,
        "min_precision_to_activate": 0.65,
        "decision_threshold": 0.55,
        "retrain_every_n_trades": 20,
        "backtest_min_score": 50,
        "backtest_max_volatility": 4.5,
    },
    "kelly_sizing": {
        "enabled": True,
        "half_kelly": True,
        "max_fraction": 0.01,
        "min_trades": 20,
        "min_position_usd": 50,
    },
    "vix_term_structure": {
        "enabled": True,
        "panic_dip_override_enabled": True,
        "panic_dip_position_multiplier": 0.6,
        "spike_warning_ratio": 1.15,
        "panic_dip_ratio": 1.20,
    },
    "hedging": {
        "enabled": True,
        "bear_position_multiplier": 0.5,
        "defensive_sectors": ["health", "consumer", "bonds", "commodities"],
    },
    "regime_strategies": {
        "enabled": True,  # Aktiviert 2026-04-09 nach Backtest-Validation (+0.38 Sharpe)
        "bull_momentum_boost": 0.5,
        "sideways_mr_boost": 0.6,
        "bear_non_defensive_penalty": -10,
    },
}

# Sub-Keys in bestehenden Sections, die fehlen koennten und injiziert werden
# (nur wenn Parent-Section existiert, aber Sub-Key fehlt)
V12_SUBKEY_INJECT: dict[str, dict[str, Any]] = {
    "leverage": {
        "trailing_sl_enabled": True,
        "trailing_sl_activation_pct": 0.8,
        "trailing_sl_pct": 1.8,
        "tp_tranches": [
            {"pct_of_position": 30, "profit_target_pct": 4},
            {"pct_of_position": 30, "profit_target_pct": 8},
            {"pct_of_position": 40, "profit_target_pct": 15},
        ],
    },
}


def _section_is_empty(section: Any) -> bool:
    """Eine Section gilt als 'fehlend', wenn sie None, {} oder kein dict ist."""
    if section is None:
        return True
    if not isinstance(section, dict):
        return True
    if len(section) == 0:
        return True
    return False


def migrate(config: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """
    Merged v12-Baseline in config. Liefert (new_config, list_of_changes).
    Mutiert config NICHT (arbeitet auf Deep-Copy).
    """
    import copy
    new_cfg = copy.deepcopy(config) if config else {}
    changes: list[str] = []

    # 1. disabled_symbols: IMMER Git-Version
    current_disabled = new_cfg.get("disabled_symbols") or []
    if set(current_disabled) != set(V12_DISABLED_SYMBOLS):
        new_cfg["disabled_symbols"] = list(V12_DISABLED_SYMBOLS)
        changes.append(
            f"disabled_symbols: {len(current_disabled)} -> {len(V12_DISABLED_SYMBOLS)} Symbols"
        )

    # 2. Feature-Flag-Sections: nur injizieren wenn fehlend
    for section_name, baseline in V12_SECTIONS.items():
        if _section_is_empty(new_cfg.get(section_name)):
            new_cfg[section_name] = copy.deepcopy(baseline)
            changes.append(f"{section_name}: injiziert ({len(baseline)} Keys)")

    # 3. Sub-Key-Injection (z.B. leverage.trailing_sl_pct)
    for parent_name, subkeys in V12_SUBKEY_INJECT.items():
        parent = new_cfg.get(parent_name)
        if not isinstance(parent, dict):
            continue
        for k, v in subkeys.items():
            if k not in parent:
                parent[k] = copy.deepcopy(v)
                changes.append(f"{parent_name}.{k}: injiziert")

    return new_cfg, changes


def run(check_only: bool = False) -> int:
    """
    Fuehrt die Migration aus. Gibt Exit-Code zurueck:
      0 = Erfolg (egal ob Changes oder nicht)
      1 = Fehler beim Laden/Speichern
    """
    log.info("=" * 55)
    log.info("v12 Bootstrap-Migration " + ("(DRY-RUN)" if check_only else "(APPLY)"))
    log.info("=" * 55)

    try:
        config = load_json("config.json") or {}
    except Exception as e:
        log.error(f"Config laden fehlgeschlagen: {e}")
        return 1

    log.info(f"Bestehende config.json: {len(config)} Top-Level-Keys")

    new_cfg, changes = migrate(config)

    if not changes:
        log.info("Keine Aenderungen noetig — config.json ist bereits v12-konform")
        return 0

    log.info(f"Geplante Aenderungen ({len(changes)}):")
    for c in changes:
        log.info(f"  • {c}")

    if check_only:
        log.info("DRY-RUN: keine Datei geschrieben")
        return 0

    try:
        save_json("config.json", new_cfg)
        log.info("config.json aktualisiert (atomic write)")
    except Exception as e:
        log.error(f"Config schreiben fehlgeschlagen: {e}")
        return 1

    log.info("Bootstrap abgeschlossen")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    check_only = "--check" in args or "--dry-run" in args
    return run(check_only=check_only)


if __name__ == "__main__":
    sys.exit(main())
