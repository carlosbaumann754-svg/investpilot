"""
Cost-Model Calibrator (E2 / Option B3)
=======================================

Liest realisierte IBKR-Fills aus trade_history.json und vergleicht
sie mit den intendierten Preisen, um die EMPIRISCHE Slippage je
Asset-Klasse zu schaetzen. Schreibt das Ergebnis nach
``data/cost_model_calibration.json``.

Diese Datei wird von ``app.cost_model.load_empirical_overrides()``
gelesen und ueberschreibt -- sofern genuegend Datenpunkte vorhanden
sind -- die hardcodierten Defaults aus ``cost_model.py``.

Datenfluss
----------
1. trade_history.json:  BUY/SELL-Eintraege mit ``avg_fill_price``
   (sobald IBKR-Fills mitgeschrieben werden) und optional
   ``intended_price`` / ``limit_price``.
2. asset_universe / instrument_cache:  Symbol -> Asset-Klasse.
3. Pro Asset-Klasse: Median(|avg_fill - intended| / intended)
   = realisierte Half-Spread + Slippage.

Mindest-Stichprobe:  >= 20 Trades pro Klasse, sonst FALLBACK auf
hardcodierte Defaults (kein Override).

Manuell ausfuehrbar::

    python -m app.cost_model_calibrator

Geplant fuer wochentlichen VPS-Cron nach Phase 4.
"""

from __future__ import annotations

import json
import logging
import math
import statistics
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ============================================================
# KONFIGURATION
# ============================================================

#: Mindestanzahl Trades pro Asset-Klasse fuer ein verlaessliches Override.
MIN_SAMPLES_PER_CLASS = 20

#: Maximalalter (Tage) der einbezogenen Trades. Aelter = veraltet.
MAX_AGE_DAYS = 90

#: Asset-Klassen, fuer die wir kalibrieren (muessen mit cost_model.py matchen).
TARGET_CLASSES = ("stocks", "etf", "crypto", "forex", "commodities", "indices")

#: Output-Datei (relativ zu data/).
OUTPUT_FILENAME = "cost_model_calibration.json"


# ============================================================
# DATENMODELL
# ============================================================

@dataclass
class TradeFill:
    """Ein einzelner IBKR-Fill mit allem was wir fuer die Kalibrierung brauchen."""
    symbol: str
    asset_class: str
    intended_price: float   # was wir wollten (limit / mid bei market)
    fill_price: float       # was IBKR uns gab (avgFillPrice)
    side: str               # "BUY" / "SELL"
    timestamp: str

    @property
    def slippage_pct(self) -> float:
        """Realisierte Slippage in Prozent (positiv = ungunst)."""
        if self.intended_price <= 0:
            return 0.0
        diff = self.fill_price - self.intended_price
        # Bei BUY: hoeher als intended = schlechter. Bei SELL: niedriger = schlechter.
        if self.side.upper() == "SELL":
            diff = -diff
        return (diff / self.intended_price) * 100.0


@dataclass
class ClassCalibration:
    """Empirische Kennzahlen fuer eine Asset-Klasse."""
    asset_class: str
    sample_count: int
    median_slippage_pct: float
    mean_slippage_pct: float
    p75_slippage_pct: float
    p95_slippage_pct: float
    stdev_slippage_pct: float
    is_reliable: bool       # True wenn sample_count >= MIN_SAMPLES_PER_CLASS

    @classmethod
    def from_fills(cls, asset_class: str, fills: List[TradeFill]) -> "ClassCalibration":
        slippages = [abs(f.slippage_pct) for f in fills]
        if not slippages:
            return cls(asset_class, 0, 0.0, 0.0, 0.0, 0.0, 0.0, False)

        slippages_sorted = sorted(slippages)
        n = len(slippages_sorted)

        def _percentile(p: float) -> float:
            if n == 1:
                return slippages_sorted[0]
            k = (n - 1) * p
            f, c = math.floor(k), math.ceil(k)
            if f == c:
                return slippages_sorted[int(k)]
            return slippages_sorted[f] * (c - k) + slippages_sorted[c] * (k - f)

        return cls(
            asset_class=asset_class,
            sample_count=n,
            median_slippage_pct=round(statistics.median(slippages), 4),
            mean_slippage_pct=round(statistics.fmean(slippages), 4),
            p75_slippage_pct=round(_percentile(0.75), 4),
            p95_slippage_pct=round(_percentile(0.95), 4),
            stdev_slippage_pct=round(statistics.pstdev(slippages), 4) if n > 1 else 0.0,
            is_reliable=(n >= MIN_SAMPLES_PER_CLASS),
        )


@dataclass
class CalibrationReport:
    """Vollstaendiger Report mit Overrides + Diagnose."""
    generated_at: str
    total_fills_analyzed: int
    age_window_days: int
    per_class: Dict[str, ClassCalibration] = field(default_factory=dict)
    slippage_buffer_pct_overrides: Dict[str, float] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "total_fills_analyzed": self.total_fills_analyzed,
            "age_window_days": self.age_window_days,
            "per_class": {k: asdict(v) for k, v in self.per_class.items()},
            "slippage_buffer_pct_overrides": self.slippage_buffer_pct_overrides,
            "notes": self.notes,
        }


# ============================================================
# HELPER: Asset-Klasse aufloesen
# ============================================================

def _build_symbol_to_class_map() -> Dict[str, str]:
    """Mappt Symbol -> Asset-Klasse via asset_universe / instrument_cache."""
    mapping: Dict[str, str] = {}
    try:
        from app.config_manager import load_json
        for source in ("asset_universe.json", "instrument_cache.json", "asset_universe_meta.json"):
            data = load_json(source) or {}
            if isinstance(data, dict):
                for sym, meta in data.items():
                    if isinstance(meta, dict):
                        cls = meta.get("class") or meta.get("asset_class") or meta.get("type")
                        if cls and sym:
                            mapping[sym.upper()] = str(cls).lower()
    except Exception as e:
        logger.warning("Konnte Symbol->Klasse-Mapping nicht laden: %s", e)
    return mapping


def _guess_asset_class(symbol: str, fallback: str = "stocks") -> str:
    """Heuristik wenn kein Mapping vorhanden (Crypto-Pattern -USD/-USDT etc.)."""
    sym = symbol.upper()
    if any(s in sym for s in ("-USD", "USDT", "BTC", "ETH", "SOL")):
        return "crypto"
    if "/" in sym or sym.endswith("=X"):
        return "forex"
    if sym.endswith("=F"):
        return "commodities"
    if sym.startswith("^"):
        return "indices"
    return fallback


# ============================================================
# CORE: Trades laden und filtern
# ============================================================

def _load_trade_fills(max_age_days: int) -> List[TradeFill]:
    """Liest trade_history.json und extrahiert nur ECHTE Fills mit Preisen."""
    try:
        from app.config_manager import load_json
        history = load_json("trade_history.json") or []
    except Exception as e:
        logger.error("trade_history.json nicht ladbar: %s", e)
        return []

    if not isinstance(history, list):
        return []

    cutoff_ts = datetime.now(timezone.utc).timestamp() - max_age_days * 86400
    sym_class = _build_symbol_to_class_map()
    fills: List[TradeFill] = []

    for entry in history:
        if not isinstance(entry, dict):
            continue
        if entry.get("status") != "executed":
            continue

        # ECHTE Preise sind Pflicht. Solange das Bot diese nicht
        # mitschreibt, hat der Calibrator nix zu tun -> graceful skip.
        fill_price = entry.get("avg_fill_price") or entry.get("avgFillPrice") \
            or entry.get("fill_price") or entry.get("executed_price")
        intended = entry.get("intended_price") or entry.get("limit_price") \
            or entry.get("mid_price") or entry.get("ref_price")
        if fill_price is None or intended is None:
            continue
        try:
            fill_price = float(fill_price)
            intended = float(intended)
        except (TypeError, ValueError):
            continue
        if fill_price <= 0 or intended <= 0:
            continue

        # Alter pruefen
        ts_str = entry.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts.timestamp() < cutoff_ts:
                continue
        except Exception:
            ts_str = ""

        symbol = (entry.get("symbol") or "").upper()
        if not symbol:
            continue
        asset_class = sym_class.get(symbol) or _guess_asset_class(symbol)

        fills.append(TradeFill(
            symbol=symbol,
            asset_class=asset_class,
            intended_price=intended,
            fill_price=fill_price,
            side=str(entry.get("action", "BUY")).upper(),
            timestamp=ts_str,
        ))

    return fills


# ============================================================
# PUBLIC API
# ============================================================

def calibrate(max_age_days: int = MAX_AGE_DAYS,
              persist: bool = True) -> CalibrationReport:
    """Fuehrt die Kalibrierung durch und schreibt optional die Override-Datei.

    Args:
        max_age_days: Trades aelter als das werden ignoriert.
        persist: Wenn True, wird ``data/cost_model_calibration.json`` geschrieben.

    Returns:
        Vollstaendiger CalibrationReport (auch ohne genuegend Daten).
    """
    fills = _load_trade_fills(max_age_days)
    report = CalibrationReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        total_fills_analyzed=len(fills),
        age_window_days=max_age_days,
    )

    if not fills:
        report.notes.append(
            "Keine Fills mit avg_fill_price + intended_price gefunden. "
            "Bot muss diese Felder erst in trade_history.json schreiben "
            "(siehe ibkr_executor.py). Calibrator faellt auf Defaults zurueck."
        )
        if persist:
            _save(report)
        return report

    # Gruppieren nach Asset-Klasse
    by_class: Dict[str, List[TradeFill]] = {}
    for f in fills:
        by_class.setdefault(f.asset_class, []).append(f)

    overrides: Dict[str, float] = {}
    for cls in TARGET_CLASSES:
        cls_fills = by_class.get(cls, [])
        cal = ClassCalibration.from_fills(cls, cls_fills)
        report.per_class[cls] = cal
        if cal.is_reliable:
            # Override = empirischer Median (Half-Spread + Slippage one-side).
            # cost_model.py multipliziert intern x2 fuer Round-Trip, daher one-side speichern.
            overrides[cls] = cal.median_slippage_pct
        else:
            report.notes.append(
                f"{cls}: nur {cal.sample_count} Fills (<{MIN_SAMPLES_PER_CLASS}) - "
                f"kein Override, Defaults bleiben aktiv."
            )

    report.slippage_buffer_pct_overrides = overrides

    if persist:
        _save(report)

    return report


def _save(report: CalibrationReport) -> None:
    """Schreibt den Report nach data/cost_model_calibration.json."""
    try:
        from app.config_manager import save_json
        save_json(OUTPUT_FILENAME, report.to_dict())
        logger.info("cost_model_calibration.json geschrieben (%d Fills, %d Overrides)",
                    report.total_fills_analyzed,
                    len(report.slippage_buffer_pct_overrides))
    except Exception as e:
        logger.error("Konnte %s nicht schreiben: %s", OUTPUT_FILENAME, e)


# ============================================================
# CLI
# ============================================================

def _print_human_summary(report: CalibrationReport) -> None:
    print(f"\n=== Cost-Model Calibration ({report.generated_at}) ===")
    print(f"Fills analysiert: {report.total_fills_analyzed} "
          f"(Window: {report.age_window_days} Tage)")
    print(f"Mindest-Stichprobe pro Klasse: {MIN_SAMPLES_PER_CLASS}\n")

    if not report.per_class:
        print("(keine verwertbaren Trades)")
    else:
        print(f"{'Klasse':<13} {'n':>5} {'Median%':>9} {'P75%':>8} {'P95%':>8} "
              f"{'Stdev':>8} {'Override':>10}")
        for cls, c in report.per_class.items():
            override = report.slippage_buffer_pct_overrides.get(cls)
            ov_str = f"{override:.4f}" if override is not None else "-"
            print(f"{cls:<13} {c.sample_count:>5} {c.median_slippage_pct:>9.4f} "
                  f"{c.p75_slippage_pct:>8.4f} {c.p95_slippage_pct:>8.4f} "
                  f"{c.stdev_slippage_pct:>8.4f} {ov_str:>10}")

    if report.notes:
        print("\nHinweise:")
        for n in report.notes:
            print(f"  - {n}")
    print()


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    report = calibrate()
    _print_human_summary(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
