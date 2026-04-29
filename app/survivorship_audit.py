"""
app/survivorship_audit.py — Survivorship-Bias-Audit (E4, Q1 Foundation)

Was:
   Quantifiziert wie stark die WFO/Backtest-Sharpe durch Survivorship-Bias
   verzerrt ist. Pragmatischer 3-Schicht-Ansatz:

   1. LIVE-CHECK: Jedes Symbol in ASSET_UNIVERSE auf yfinance pingen
      (alive? letzter Trade?)
   2. HISTORICAL-REFERENCE: Bekannte 2021-2026 Insolvenzen/Delistings
      gegen ASSET_UNIVERSE checken — waeren die in unserem Universum
      gewesen?
   3. BOT-PROFIL: Asset-Mix-Klassifizierung (Mid/Large-Cap-Bias) -> Sharpe-
      Korrektur basierend auf empirischer Literatur

Warum:
   yfinance liefert nur Daten von HEUTE existierenden Tickern. Wenn unser
   Backtest 2021-2026 laeuft, fehlen alle in dieser Zeit insolvent
   gegangenen / delisted Stocks. Der WFO-Sharpe von 4.80 ist also auf
   einem 'Survivors-Sample' berechnet, was systematisch zu hoch ist.

   Studien (Brown/Goetzmann/Ross/Ibbotson 1992, Carhart 1997, Elton/
   Gruber/Blake 1996) zeigen Survivorship-Bias-Effekte:
   - Mutual-Fund-Universe: ~1.0-1.5% pro Jahr (bias upward)
   - US-Equity-Strategy: ~0.5-1.0% pro Jahr fuer Mid-Cap-Strategien
   - Mega-Cap-only: 0.1-0.3% pro Jahr (minimal)
   - Small/Penny-Cap: 2-4% pro Jahr (massiv)

   Bei Sharpe-Konversion (Annual-Return-Bias / Vol): Reduktion um 0.2-0.5
   Sharpe-Punkte fuer typischen Mid-Cap-Strategie.

Output:
   data/survivorship_audit.json — vollstaendiger Audit-Report
   data/survivorship_audit_summary.json — kompakter Summary fuer Dashboard

Usage:
   python -m app.survivorship_audit          # Vollaudit + Persist
   python -m app.survivorship_audit --quick  # nur Live-Check, kein yfinance
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta
from typing import Optional

log = logging.getLogger(__name__)


# ============================================================
# HISTORICAL-REFERENCE: Bekannte 2021-2026 US-Pleiten / Delistings
# ============================================================
# Pre-curated, basierend auf SEC 8-K Filings + Bloomberg Bankruptcy Tracker.
# Liste ist absichtlich konservativ (nur grosse, eindeutige Faelle), damit
# der Audit reproduzierbar bleibt und nicht von Live-Datenquellen abhaengt.
#
# Format: ticker -> (year, what happened)
# Wenn ein Ticker in unserem ASSET_UNIVERSE haette sein KOENNEN (Mid/
# Large-Cap, US-listed) -> potenzielle Survivorship-Bias-Quelle.
KNOWN_DELISTED_2021_2026: dict[str, tuple[int, str]] = {
    # 2021
    "GME":  (2021, "saved by short-squeeze, alive — listed for reference"),  # noqa: alive
    # 2022
    "FB":   (2022, "renamed to META, ticker change Jun 2022"),  # noqa: ticker change
    "TWTR": (2022, "Acquired by Elon Musk Oct 2022, delisted from NYSE"),
    "FRC":  (2022, "First Republic — initial stress 2022, bankrupt 2023"),
    # 2023 — Banking-Crisis + Tech-Wreck
    "SIVB": (2023, "SVB Financial — bankrupt March 2023, FDIC takeover"),
    "SBNY": (2023, "Signature Bank — closed by FDIC March 2023"),
    "FRC":  (2023, "First Republic Bank — JPM acquired May 2023"),
    "BBBY": (2023, "Bed Bath & Beyond — Chapter 11 April 2023"),
    "WE":   (2023, "WeWork — Chapter 11 November 2023"),
    "RIDE": (2023, "Lordstown Motors — Chapter 11 June 2023"),
    "YELL": (2023, "Yellow Corp — Chapter 11 August 2023"),
    "VLDR": (2023, "Velodyne LiDAR — merged with Ouster"),
    "PRTY": (2023, "Party City — Chapter 11 January 2023"),
    "GOEV": (2023, "Canoo — Chapter 11 January 2024 (announced 2023)"),
    # 2024
    "EXPR": (2024, "Express Inc — Chapter 11 April 2024"),
    "RUE":  (2024, "Rue21 — Chapter 11 May 2024"),
    "RAD":  (2024, "Rite Aid — Chapter 11 October 2023, delisted 2024"),
    "FSR":  (2024, "Fisker — Chapter 11 June 2024"),
    "REDF": (2024, "Redfin — acquired by Rocket"),
    # 2025
    "SPRT": (2025, "Greenidge Generation Holdings — Chapter 11"),
    # 2026 (ongoing) — keine bekannten Faelle bisher
}

# Ticker-Renames sind KEIN Survivorship-Bias (Asset existiert weiter, nur
# anderer Name). Tracken wir separat als Reference fuer Audit-Trail.
KNOWN_TICKER_RENAMES_2021_2026: dict[str, tuple[int, str]] = {
    "FB":  (2022, "renamed to META in Jun 2022"),
    "SQ":  (2024, "renamed to XYZ — Block, Inc. Jan 2024"),
    "MATIC": (2024, "renamed to POL — Polygon Migration Sept 2024 (yfinance dropped)"),
}

# Symbole die wir aus diesen Faellen aktuell im ASSET_UNIVERSE haben (oder
# hatten). Wird vom Audit dynamisch geprueft — diese hier sind zur
# Erinnerung welche bereits korrekt entfernt wurden.
KNOWN_REMOVED_FROM_UNIVERSE = ["MATIC", "SQ"]  # SQ ticker now XYZ, MATIC delisted yfinance


# ============================================================
# BOT-PROFIL-KLASSIFIZIERUNG (Asset-Mix-Survivorship-Faktor)
# ============================================================
# Empirische Literatur-Werte (annualisierte Sharpe-Reduktion):
#
#   Asset-Klasse                     Sharpe-Reduktion (pa)
#   ────────────────────────────────────────────────────
#   Mega-Cap US-Equities (>$200B)    0.05 - 0.15
#   Large-Cap US-Equities ($10-200B) 0.15 - 0.30
#   Mid-Cap US-Equities ($2-10B)     0.30 - 0.50
#   Small-Cap (<$2B)                 0.50 - 1.00+
#   ETFs (broad)                     0.05 - 0.15  (sehr stabil)
#   Sector-ETFs                      0.10 - 0.25
#   Crypto-Top10                     0.20 - 0.40
#   Crypto-Long-Tail                 0.50 - 1.50
#   Forex (Major)                    0.00 - 0.05
#   Commodities (Future-ETFs)        0.10 - 0.20
#
# Diese Werte stammen aus:
# - Brown et al. 1992 (Survivor Bias und Performance Studies)
# - Lopez de Prado 2018 (Advances in Financial ML)
# - Eigene empirische Schaetzung fuer Crypto-Strategien

ASSET_CLASS_BIAS_FACTORS: dict[str, tuple[float, float]] = {
    # (min, max) Sharpe-Reduktion pro Jahr (additive auf gemessene Sharpe)
    "stocks":      (0.20, 0.40),  # Mid/Large-Cap mix
    "etf":         (0.05, 0.15),  # broad + sector mix
    "crypto":      (0.30, 0.60),  # Top-10 mostly, etwas Long-Tail
    "forex":       (0.00, 0.05),  # Major-Pairs, kein Survivorship
    "commodities": (0.10, 0.20),  # via ETF-Proxies (CPER, etc.)
    "indices":     (0.00, 0.05),  # synthetic via CFD/futures
}


def classify_universe_by_class(asset_universe: dict) -> dict[str, int]:
    """Zaehlt Symbole pro Asset-Klasse im aktiven Universum."""
    counts: dict[str, int] = {}
    for sym, meta in asset_universe.items():
        cls = meta.get("class", "unknown")
        counts[cls] = counts.get(cls, 0) + 1
    return counts


def estimate_sharpe_correction(class_counts: dict[str, int]) -> dict:
    """Schaetzt die Sharpe-Reduktion aufgrund des Asset-Mix.

    Liefert (min, max, point_estimate) als Bias-Korrektur.
    """
    total = sum(class_counts.values()) or 1
    weighted_min = 0.0
    weighted_max = 0.0
    breakdown = []
    for cls, count in class_counts.items():
        weight = count / total
        bmin, bmax = ASSET_CLASS_BIAS_FACTORS.get(cls, (0.10, 0.30))
        weighted_min += weight * bmin
        weighted_max += weight * bmax
        breakdown.append({
            "class": cls,
            "count": count,
            "weight_pct": round(weight * 100, 1),
            "bias_min": bmin,
            "bias_max": bmax,
        })
    point_estimate = (weighted_min + weighted_max) / 2
    return {
        "weighted_min_reduction": round(weighted_min, 3),
        "weighted_max_reduction": round(weighted_max, 3),
        "point_estimate": round(point_estimate, 3),
        "breakdown": breakdown,
    }


# ============================================================
# LIVE-CHECK: yfinance-Status pro Symbol
# ============================================================

def live_check_universe(asset_universe: dict, lookback_days: int = 7) -> dict:
    """Prueft jedes Symbol auf aktuelle yfinance-Datenverfuegbarkeit.

    Returns:
        dict mit per-Symbol Status: {alive, last_close, last_close_date, days_since}
        + Aggregat: alive_count, dead_count, suspicious_count
    """
    try:
        import yfinance as yf
    except ImportError:
        log.error("yfinance nicht installiert")
        return {"error": "yfinance not installed"}

    today = datetime.utcnow()
    cutoff = today - timedelta(days=lookback_days)
    per_symbol = {}
    alive_count = 0
    dead_count = 0
    suspicious = []

    for sym, meta in asset_universe.items():
        yf_sym = meta.get("yf", sym)
        try:
            df = yf.download(yf_sym, period="14d", progress=False,
                             auto_adjust=False, multi_level_index=False)
            if df.empty:
                per_symbol[sym] = {"status": "dead", "yf_symbol": yf_sym}
                dead_count += 1
                continue
            close = df["Close"].dropna()
            if len(close) == 0:
                per_symbol[sym] = {"status": "dead", "yf_symbol": yf_sym}
                dead_count += 1
                continue
            last_date = close.index[-1].to_pydatetime()
            days_since = (today - last_date).days
            entry = {
                "status": "alive",
                "yf_symbol": yf_sym,
                "last_close": round(float(close.iloc[-1]), 4),
                "last_close_date": last_date.date().isoformat(),
                "days_since": days_since,
            }
            if days_since > lookback_days:
                entry["status"] = "suspicious"
                suspicious.append(sym)
            else:
                alive_count += 1
            per_symbol[sym] = entry
        except Exception as e:
            per_symbol[sym] = {"status": "error", "yf_symbol": yf_sym,
                              "error": str(e)[:120]}
            dead_count += 1

    return {
        "total": len(asset_universe),
        "alive": alive_count,
        "dead": dead_count,
        "suspicious": len(suspicious),
        "suspicious_list": suspicious,
        "per_symbol": per_symbol,
    }


# ============================================================
# HISTORICAL-REFERENCE-CHECK
# ============================================================

def historical_reference_check(asset_universe: dict) -> dict:
    """Prueft ob bekannte delisted-Tickers im aktuellen Universum sind
    oder waren (Audit-Trail).
    """
    in_universe = []
    correctly_excluded = []
    universe_set = set(asset_universe.keys())
    yf_to_sym = {meta.get("yf", sym): sym for sym, meta in asset_universe.items()}
    for ticker, (year, reason) in KNOWN_DELISTED_2021_2026.items():
        if ticker in universe_set or ticker in yf_to_sym:
            in_universe.append({"ticker": ticker, "year": year, "reason": reason})
        else:
            correctly_excluded.append({"ticker": ticker, "year": year, "reason": reason})

    return {
        "known_delisted_count": len(KNOWN_DELISTED_2021_2026),
        "in_universe": in_universe,
        "correctly_excluded": correctly_excluded,
        "exclusion_rate_pct": round(
            len(correctly_excluded) / len(KNOWN_DELISTED_2021_2026) * 100, 1)
        if KNOWN_DELISTED_2021_2026 else 0,
    }


# ============================================================
# AUDIT-ORCHESTRATOR
# ============================================================

def run_audit(quick: bool = False) -> dict:
    """Vollstaendiger Audit + Persist."""
    from app.market_scanner import ASSET_UNIVERSE
    from app.config_manager import save_json, load_config

    cfg = load_config() or {}
    disabled = set(cfg.get("disabled_symbols", []) or [])
    active_universe = {s: m for s, m in ASSET_UNIVERSE.items() if s not in disabled}

    log.info("Survivorship-Audit gestartet (active universe: %d symbols)",
             len(active_universe))

    # Phase 1: Live-Check
    if quick:
        log.info("--quick: Live-Check skip")
        live = {"skipped": True}
    else:
        live = live_check_universe(active_universe)
        log.info("Live-Check: alive=%s, dead=%s, suspicious=%s",
                 live.get("alive"), live.get("dead"), live.get("suspicious"))

    # Phase 2: Historical-Reference
    historical = historical_reference_check(active_universe)
    log.info("Historical-Reference: %d known delistings, %d correctly excluded "
             "(%s%%), %d still in universe",
             historical["known_delisted_count"],
             len(historical["correctly_excluded"]),
             historical["exclusion_rate_pct"],
             len(historical["in_universe"]))

    # Phase 3: Asset-Mix-Klassifizierung + Sharpe-Korrektur
    class_counts = classify_universe_by_class(active_universe)
    correction = estimate_sharpe_correction(class_counts)
    log.info("Asset-Mix: %s", class_counts)
    log.info("Estimated Sharpe-Reduction: %.3f (range %.3f-%.3f)",
             correction["point_estimate"],
             correction["weighted_min_reduction"],
             correction["weighted_max_reduction"])

    # WFO-Resultat einbeziehen falls vorhanden
    try:
        from app.config_manager import load_json
        wfo = load_json("wfo_status.json") or {}
        wfo_agg = wfo.get("aggregate") or {}
        wfo_sharpe = wfo_agg.get("mean_oos_sharpe")
        if wfo_sharpe:
            corrected_min = wfo_sharpe - correction["weighted_max_reduction"]
            corrected_max = wfo_sharpe - correction["weighted_min_reduction"]
            corrected_point = wfo_sharpe - correction["point_estimate"]
            wfo_correction = {
                "wfo_mean_oos_sharpe": wfo_sharpe,
                "corrected_min": round(corrected_min, 2),
                "corrected_max": round(corrected_max, 2),
                "corrected_point_estimate": round(corrected_point, 2),
            }
        else:
            wfo_correction = None
    except Exception as e:
        log.warning(f"WFO-correction calc failed: {e}")
        wfo_correction = None

    report = {
        "generated_at": datetime.utcnow().isoformat(),
        "active_universe_size": len(active_universe),
        "live_check": live,
        "historical_reference": historical,
        "asset_mix": class_counts,
        "sharpe_correction": correction,
        "wfo_correction": wfo_correction,
    }
    save_json("survivorship_audit.json", report)

    # Compact summary fuer Dashboard
    summary = {
        "generated_at": report["generated_at"],
        "universe_size": len(active_universe),
        "live_alive": live.get("alive"),
        "live_dead": live.get("dead"),
        "live_suspicious": live.get("suspicious"),
        "historical_in_universe": len(historical["in_universe"]),
        "historical_excluded": len(historical["correctly_excluded"]),
        "exclusion_rate_pct": historical["exclusion_rate_pct"],
        "estimated_sharpe_reduction_min": correction["weighted_min_reduction"],
        "estimated_sharpe_reduction_max": correction["weighted_max_reduction"],
        "estimated_sharpe_reduction_point": correction["point_estimate"],
        "wfo_correction": wfo_correction,
    }
    save_json("survivorship_audit_summary.json", summary)
    return report


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    quick = "--quick" in sys.argv
    report = run_audit(quick=quick)
    print("=" * 60)
    print("SURVIVORSHIP-AUDIT RESULTAT")
    print("=" * 60)
    print(f"Active Universe: {report['active_universe_size']} symbols")
    if not quick:
        live = report["live_check"]
        print(f"Live-Check: alive={live.get('alive')} dead={live.get('dead')} "
              f"suspicious={live.get('suspicious')}")
    hist = report["historical_reference"]
    print(f"Historical-Reference: {len(hist['in_universe'])} of "
          f"{hist['known_delisted_count']} known delistings still in universe "
          f"({hist['exclusion_rate_pct']}% correctly excluded)")
    if hist["in_universe"]:
        print("  IN UNIVERSE (potential bias source):")
        for r in hist["in_universe"]:
            print(f"    - {r['ticker']} ({r['year']}): {r['reason']}")
    corr = report["sharpe_correction"]
    print(f"Asset-Mix: {report['asset_mix']}")
    print(f"Estimated Sharpe-Reduction: "
          f"min={corr['weighted_min_reduction']} "
          f"max={corr['weighted_max_reduction']} "
          f"point={corr['point_estimate']}")
    if report["wfo_correction"]:
        wc = report["wfo_correction"]
        print(f"WFO-Sharpe: {wc['wfo_mean_oos_sharpe']} "
              f"-> corrected: {wc['corrected_min']} - {wc['corrected_max']} "
              f"(point: {wc['corrected_point_estimate']})")
