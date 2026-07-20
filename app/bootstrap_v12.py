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
  6. v37du Boot-Invarianten (Seed-Drift-Schutz, siehe migrate() Schritt 4):
     - use_signal_stack=True wird IMMER erzwungen (Motor-Identitaet; ohne den
       Flag faellt der Scanner auf die alte TA-Strategie zurueck).
     - optimizer.enabled=False + risk_management.{catastrophic_stop, caps}
       werden nur INJIZIERT wenn fehlend (kein Override bewusster Live-Werte).
     Diese Baseline ist die de-facto DR-Quelle der config, weil der Cloud-
     Restore config.json nur bei lokal-leer zurueckspielt — und bootstrap_v12
     davor immer eine nicht-leere config sicherstellt.

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
    # R-A34 (21.05.2026, Pre-Cutover-Sprint-Tag-11): Reports-KI-Vorschlag
    # umgesetzt nach Live-Bestaetigung. Begruendung:
    # - SILVER: Live-Score -133.94 (Brain-Tab, 21.05.) + Single-Trade-Loss
    #   $-13'209 am 15.5. (STOP_LOSS_CLOSE) + "dauerhaft schwach" laut
    #   Weekly-Report Score -26.8. PF deutlich unter 1.2 in 2026 YTD.
    # - IWM: Reports Score -10.2, "dauerhaft schwach". 2x STALE_NO_IBKR_HISTORY
    #   im Failed-Orders. Russell-2000 Smallcap-Universum performt 2026
    #   strukturell schlechter als unsere Konzentration auf Big-Tech + Energy.
    # Beide bleiben im Universe-Filter sichtbar fuer Audit/Diagnose, werden
    # aber nicht mehr fuer neue Trades genutzt. Auto-Curate-Mechanismus
    # haette beide vermutlich beim naechsten WFO-Run gleichermassen
    # disabled — wir nehmen das hier vorweg.
    "SILVER", "IWM",
    # R-B3 (01.06.2026): VITAX (Vanguard Information Technology Index Fund) ist
    # ein MUTUAL FUND — nur Tages-NAV, kein Boersen-/Intraday-Handel. Wurde von
    # der Wochen-Discovery faelschlich eingesammelt -> 15x/48h "possibly
    # delisted; no price data found" im Scan. Der R-B3-instrumentType-Filter
    # (market_scanner.analyze_single_asset) verhindert KUENFTIGE Fonds; VITAX ist
    # aber bereits persistiert + inzwischen voellig dataless (leere History
    # greift vor dem Filter) -> hier gezielt deaktivieren, damit der Scanner es
    # gar nicht erst zu fetchen versucht. Bleibt im Universe-Filter sichtbar.
    "VITAX",
    # v37e+ (16.07.2026): Universe-Cleanup — die 25 to_disable-Vorschlaege des
    # bot-eigenen Universe-Health-Watchers umgesetzt (>=7 Fehl-Checks in Folge bzw.
    # no_price). Delistete/fusionierte/umbenannte sp600-Namen, die auf veralteten
    # EDGAR-Fakten noch hoch scoren -> verschwendete Buy-Slots + Rand-Sentry-Fehler
    # (KW=PYTHON-FASTAPI-17, AMWD/FDP=no_price seit Tagen; BBT=ex-BB&T/Truist etc.).
    # SUNMED BEWUSST NICHT dabei (Health-Watcher not_ok=0 -> hat sich erholt).
    # Bleiben im Universe-Filter sichtbar (nur kein Trade); Health-Watcher hebt sie
    # bei Erholung selbst wieder auf OK -> dann re-enable-Kandidat.
    "AMWD", "KW", "FDP",                                    # no_price (klar delisted)
    "ADAM", "AGNT", "AMTM", "BBT", "BTSG", "CALY", "CENTA", "COCO", "CON",
    "CTKB", "CURB", "CVSA", "DCH", "ECG", "EFOR", "ESI", "GTM", "HTO",
    "INVX", "JBTM", "LIF", "OPLN",                          # 7 Fehl-Checks in Folge
]

V12_SECTIONS: dict[str, dict[str, Any]] = {
    "time_stop": {
        "enabled": True,
        "max_days_stale": 30,  # v37du: an Live (v37do interim-entschaerft) angeglichen
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
        # R-B12 (20.07.2026) ZURUECKGEDREHT — WICHTIG, nicht erneut "optimieren"
        # ohne den folgenden Befund zu beruecksichtigen:
        # Der Exit-Sweep empfahl Trail 10/12 + TP aus + Tranchen aus (PF 1.66 vs
        # 1.33). Die Auszaehlung der Exit-Gruende zeigte danach aber: bei dieser
        # Variante stammen 62 %% der Ausstiege und ~86 %% des Gewinns aus dem
        # monatlichen REBALANCING des Backtesters — einem Mechanismus, den der
        # LIVE-Bot NICHT hat (kein Verkauf, wenn ein Titel aus den Top-15 faellt;
        # rebalance_portfolio gleicht nur Ziel-Gewichte ab). Die alte Config
        # dagegen erntet ueber TP + Tranchen (+3'282 bzw. +616 Pp) — Mechanismen,
        # die live existieren. Ohne sie realisiert der Bot fast nur noch Verluste
        # (Verlierer via SL raus, Gewinner bleiben ewig liegen).
        # Zusatzfalle bei 10/12: scharf ab +10 %, dann -12 %% vom Hoch = Ausstieg
        # bei -3.2 %% -> der Trail kann einen Gewinner in einen Verlierer drehen.
        # Erst wenn der Backtester Mehr-Monats-Halten abbildet (oder der Bot echte
        # Rotation bekommt), ist die Exit-Frage sinnvoll beantwortbar.
        "trailing_sl_enabled": True,
        "trailing_sl_activation_pct": 6.0,
        "trailing_sl_pct": 4.0,
        "tp_tranches": [
            {"pct_of_position": 30, "profit_target_pct": 8},
            {"pct_of_position": 30, "profit_target_pct": 16},
            {"pct_of_position": 40, "profit_target_pct": 30},
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

    # 4. v37du Boot-Invarianten (Seed-Drift-Schutz). Hintergrund: der
    #    Cloud-Restore (persistence.restore_from_cloud) spielt config.json NUR
    #    bei lokal-leer zurueck, und bootstrap_v12 stellt davor immer eine
    #    nicht-leere config sicher -> der Gist-Stand der config wird NIE
    #    auto-restored. Damit ist DIESE Baseline (nicht der Gist) die de-facto
    #    DR-Quelle. Ohne die folgenden Invarianten wuerde ein Volume-Wipe /
    #    Fresh-Clone den alten TA-Motor wiederbeleben (use_signal_stack default
    #    False -> market_scanner TA-Fallback) + den Optimizer reaktivieren.

    # 4a. use_signal_stack: HARTER Invariant (immer erzwingen, nicht nur
    #     inject-if-missing). Der Bot IST der Fundamental-Signal-Stack-Motor;
    #     fehlt/false der Flag, scannt er die alte edgelose TA-Strategie.
    if new_cfg.get("use_signal_stack") is not True:
        old = new_cfg.get("use_signal_stack")
        new_cfg["use_signal_stack"] = True
        changes.append(f"use_signal_stack: {old} -> True (erzwungen)")

    # 4b. optimizer.enabled=False (Soak-Politik, REVERSIBEL): nur setzen wenn
    #     fehlend — ein bewusstes Re-Enable (post-Soak) wird NICHT ueberschrieben.
    opt = new_cfg.get("optimizer")
    if not isinstance(opt, dict):
        new_cfg["optimizer"] = {"enabled": False}
        changes.append("optimizer: injiziert (enabled=False)")
    elif "enabled" not in opt:
        opt["enabled"] = False
        changes.append("optimizer.enabled: injiziert (False)")

    # 4c. risk_management Sicherheits-/Cap-Invarianten (inject-if-missing).
    #     KEINE Optimizer-Werte (SL/TP/min_score bleiben in demo_trading
    #     unangetastet). catastrophic_stop = E6 Broker-seitiger Hard-Stop.
    rm = new_cfg.get("risk_management")
    if not isinstance(rm, dict):
        rm = {}
        new_cfg["risk_management"] = rm
    rm_invariants = {
        "catastrophic_stop": {"enabled": True, "pct": 20},
        "max_positions_per_class": 20,
        "max_class_allocation_pct": 1000,
    }
    for k, v in rm_invariants.items():
        if k not in rm:
            rm[k] = copy.deepcopy(v)
            changes.append(f"risk_management.{k}: injiziert")

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
        log.error(f"Config laden fehlgeschlagen: {e}", exc_info=True)
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
        log.error(f"Config schreiben fehlgeschlagen: {e}", exc_info=True)
        return 1

    log.info("Bootstrap abgeschlossen")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    check_only = "--check" in args or "--dry-run" in args
    return run(check_only=check_only)


if __name__ == "__main__":
    sys.exit(main())
