"""
Cash-Reserve (Hybrid-Floor + Prozent) — v37h+1 (14.05.2026)
============================================================

Ersetzt den W7-MVP-Entnahme-Planer durch einen einfachen, dauerhaft aktiven
Cash-Buffer. Der Bot kauft nur, wenn nach Abzug der Reserve noch Cash
verfuegbar ist. Bei Unterschreitung pausieren Buys — es wird NICHT in
Panik verkauft (Strategy-Disziplin bleibt intakt). Auto-Refill durch
normale Sells / TP / Dividenden.

Carlos's Use-Case (14.05.2026 Abend): Steuern, Versicherungen, planbare
Auslagen. Bisheriger Withdrawal-Planner war fuer 90%% der Faelle
overengineered (W8 Auto-Liquidation TODO) — Cash-Buffer ist die
einfachere und sofort wirksame Loesung.

Berechnung
----------
required_reserve_chf = max(
    risk.min_cash_reserve_chf,                       # absoluter Floor
    risk.min_cash_reserve_pct * equity_chf,          # prozentual mitwachsend
)

Beispiel-Evolution bei Floor=500, Pct=0.10:
  Equity   2'000 CHF  ->  Reserve  500 CHF (Floor greift)
  Equity   7'000 CHF  ->  Reserve  700 CHF (Prozent uebernimmt)
  Equity  50'000 CHF  ->  Reserve 5'000 CHF (Prozent skaliert)

Design-Entscheidungen
---------------------
- Reserve in BASE-Currency (CHF) — analog zu IBKR's `credit` + NetLiquidation
- Floor + Prozent als max() — kein OR, sondern beide gleichzeitig
- Pct = 0 oder Floor = 0 erlaubt -> "nur eine Dimension aktiv"
- Beide auf 0 = Feature deaktiviert (Backward-Compat)
- Kein WFO-Lock — Carlos darf die Werte jederzeit ueber Dashboard anpassen
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)

# Defaults wenn Config-Keys fehlen — siehe Sprint-Plan 14.05.2026
DEFAULT_FLOOR_CHF = 500.0
DEFAULT_PCT = 0.10  # 10% des Equity

# Sicherheits-Grenzen fuer User-Input (verhindert Tippfehler-Katastrophen)
MIN_PCT = 0.0
MAX_PCT = 0.50   # 50% — alles darueber ist faktisch "Bot abschalten"
MIN_FLOOR_CHF = 0.0
MAX_FLOOR_CHF = 100_000.0  # generoes; schuetzt vor Zahlendreher 5000 -> 500000


def _load_config() -> dict:
    """Late-Import damit Test-Mocks greifen koennen."""
    from app.config_manager import load_config
    return load_config() or {}


def _get_reserve_params(config: Optional[dict] = None) -> tuple[float, float]:
    """Liest (floor_chf, pct) aus Config mit Default-Fallback.

    Returns:
        Tupel (min_cash_reserve_chf, min_cash_reserve_pct)
    """
    cfg = config if config is not None else _load_config()
    risk = cfg.get("risk_management", {}) if isinstance(cfg.get("risk_management"), dict) else {}

    try:
        floor = float(risk.get("min_cash_reserve_chf", DEFAULT_FLOOR_CHF))
    except (TypeError, ValueError):
        floor = DEFAULT_FLOOR_CHF

    try:
        pct = float(risk.get("min_cash_reserve_pct", DEFAULT_PCT))
    except (TypeError, ValueError):
        pct = DEFAULT_PCT

    # Defensive Clamping (verhindert konfig-induzierten Bot-Kollaps)
    floor = max(MIN_FLOOR_CHF, min(MAX_FLOOR_CHF, floor))
    pct = max(MIN_PCT, min(MAX_PCT, pct))
    return floor, pct


def get_required_reserve_chf(equity_chf: float, config: Optional[dict] = None) -> float:
    """Berechnet aktuell geforderte Cash-Reserve in CHF.

    Args:
        equity_chf: Total-Equity in BASE-Currency (CHF). Aus IBKR's
            NetLiquidation oder portfolio['_equity'] / total_value.
        config: optional zum Override (z.B. fuer Tests / API-Preview)

    Returns:
        Reserve-Betrag in CHF. Floor=0 + Pct=0 -> returns 0.0 (Feature off).
    """
    if not isinstance(equity_chf, (int, float)) or equity_chf < 0:
        return 0.0

    floor, pct = _get_reserve_params(config)
    pct_component = pct * float(equity_chf)
    return float(max(floor, pct_component))


def compute_deployable_cash_chf(
    available_cash_chf: float,
    equity_chf: float,
    config: Optional[dict] = None,
) -> float:
    """Verfuegbarer Buy-Budget nach Abzug der Reserve.

    Returns 0 (nicht negativ) wenn Cash < Reserve — Bot pausiert Buys.
    """
    if not isinstance(available_cash_chf, (int, float)) or available_cash_chf <= 0:
        return 0.0
    required = get_required_reserve_chf(equity_chf, config)
    return float(max(0.0, available_cash_chf - required))


def get_status(
    available_cash_chf: Optional[float] = None,
    equity_chf: Optional[float] = None,
) -> dict:
    """Dashboard-Snapshot. Wenn cash/equity None -> nur Config-Soll-Werte."""
    floor, pct = _get_reserve_params()
    result = {
        "min_cash_reserve_chf": floor,
        "min_cash_reserve_pct": pct,
        "defaults": {
            "floor_chf": DEFAULT_FLOOR_CHF,
            "pct": DEFAULT_PCT,
        },
        "limits": {
            "pct_min": MIN_PCT,
            "pct_max": MAX_PCT,
            "floor_min_chf": MIN_FLOOR_CHF,
            "floor_max_chf": MAX_FLOOR_CHF,
        },
    }
    if equity_chf is not None and isinstance(equity_chf, (int, float)):
        required = get_required_reserve_chf(equity_chf)
        result["equity_chf"] = float(equity_chf)
        result["required_reserve_chf"] = required
        # welche Komponente greift gerade?
        result["binding"] = "floor" if floor >= pct * float(equity_chf) else "pct"

        if available_cash_chf is not None and isinstance(available_cash_chf, (int, float)):
            result["available_cash_chf"] = float(available_cash_chf)
            result["deployable_cash_chf"] = compute_deployable_cash_chf(
                available_cash_chf, equity_chf
            )
            result["reserve_filled"] = float(available_cash_chf) >= required
            # Fuellgrad: 0..1 zeigt wie nah Reserve dran ist
            result["fill_pct"] = (
                min(1.0, float(available_cash_chf) / required) if required > 0 else 1.0
            )
    return result


def update_reserve_settings(
    floor_chf: Optional[float] = None,
    pct: Optional[float] = None,
) -> dict:
    """Persistiert neue Reserve-Settings. Validiert + clamped.

    Beide None = no-op. Returns finalen Status-Snapshot.
    """
    from app.config_manager import load_config, save_config

    config = load_config() or {}
    risk = config.setdefault("risk_management", {})

    changes = {}
    if floor_chf is not None:
        try:
            new_floor = float(floor_chf)
        except (TypeError, ValueError):
            raise ValueError(f"floor_chf '{floor_chf}' ist keine Zahl")
        if new_floor < MIN_FLOOR_CHF or new_floor > MAX_FLOOR_CHF:
            raise ValueError(
                f"floor_chf {new_floor} ausserhalb [{MIN_FLOOR_CHF}, {MAX_FLOOR_CHF}]"
            )
        old = risk.get("min_cash_reserve_chf")
        risk["min_cash_reserve_chf"] = new_floor
        changes["floor_chf"] = {"old": old, "new": new_floor}

    if pct is not None:
        try:
            new_pct = float(pct)
        except (TypeError, ValueError):
            raise ValueError(f"pct '{pct}' ist keine Zahl")
        # Akzeptiere 0..0.5 (Dezimal) oder 0..50 (Prozent) — auto-detect
        if new_pct > 1.0 and new_pct <= 100.0:
            new_pct = new_pct / 100.0
        if new_pct < MIN_PCT or new_pct > MAX_PCT:
            raise ValueError(
                f"pct {new_pct} ausserhalb [{MIN_PCT}, {MAX_PCT}] (max 50%%)"
            )
        old = risk.get("min_cash_reserve_pct")
        risk["min_cash_reserve_pct"] = new_pct
        changes["pct"] = {"old": old, "new": new_pct}

    if changes:
        save_config(config)
        log.info(
            "Cash-Reserve aktualisiert: %s",
            ", ".join(f"{k}: {v['old']!r} -> {v['new']!r}" for k, v in changes.items()),
        )

    return get_status()
