"""
app/cost_model.py — Realistic Slippage + Volume-Impact Model (E2, Q1 Foundation)

Was:
   Akademisch korrektes Cost-Model fuer Backtests. Ersetzt das bisherige
   one-size-fits-all `_calc_costs()` mit:

   1. Corwin-Schultz historic-spread-estimator (JF 2012) — leitet Bid-Ask-
      Spread aus OHLC-Daten ab, ohne live API
   2. Almgren-Chriss square-root market-impact (Almgren-Chriss 2001) —
      Volume-Impact-Modell
   3. Per-Asset-Class Slippage-Buffer (Hardcoded MVP, in 2 Wochen via
      Calibrator empirisch ueberschreibbar)

Warum:
   Bisherige _calc_costs in backtester.py rechnete pauschal ~0.40% pro
   Round-Trip-Trade. Realitaet: AAPL ~0.02% Spread, ROKU ~0.10%, Crypto
   ~0.20%, Forex ~0.01%. Plus Volume-Impact: $200k-Position in einem
   $50M Daily-Volume Symbol kostet extra. Pauschal-Modell verzerrt
   Sharpe systematisch positiv (zu wenig Cost) oder negativ (zu viel).

   Ehrliche Sharpe-Schaetzung VOR Real-Money braucht ehrlichen Cost-Modell.

Output (Funktion total_cost_pct):
   total_cost_pct = spread_pct + volume_impact_pct + slippage_buffer_pct
                   (Round-Trip = Entry+Exit zusammen)

References:
   - Corwin & Schultz (2012): "A Simple Way to Estimate Bid-Ask Spreads
     from Daily High and Low Prices." Journal of Finance 67(2): 719-760
   - Almgren & Chriss (2001): "Optimal Execution of Portfolio Transactions"
     Journal of Risk 3: 5-39
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)


# ============================================================
# CONSTANTS — empirisch begruendete Parameter
# ============================================================

# Almgren-Chriss Volatility-Faktor gamma. Empirisch in [0.5, 1.5].
# Wir nehmen 1.0 als robusten Mittelwert. Spaeter via Calibrator.
ALMGREN_CHRISS_GAMMA = 1.0

# Slippage-Buffer pro Side (entry/exit) — was zwischen Order-Submit und
# Fill verloren geht durch Markt-Bewegung. Konservativ.
PER_CLASS_SLIPPAGE_BUFFER_PCT = {
    # Asset-Class -> Slippage-Buffer pro Side in % (wird verdoppelt fuer Round-Trip)
    "stocks":      0.030,   # 3 bps - Mid/Large-Cap-US-Stocks
    "etf":         0.015,   # 1.5 bps - Broad-ETFs sehr eng
    "crypto":      0.080,   # 8 bps - 24/7-Markt aber volatil
    "forex":       0.010,   # 1 bp - Major-Pairs interbank-eng
    "commodities": 0.025,   # 2.5 bps - via ETF-Proxies (CPER etc.)
    "indices":     0.015,   # 1.5 bps - synthetic/CFD, sehr eng
}

# Fallback wenn Asset-Class unbekannt
DEFAULT_SLIPPAGE_BUFFER_PCT = 0.040

# Minimum-Spread floor — Corwin-Schultz kann negativ werden bei Daten-Noise.
# Wir clampen auf realistic-tight (1 bp).
MIN_SPREAD_PCT = 0.0001  # 0.01%

# Maximum-Spread ceiling — sehr hohe Werte deuten auf Daten-Probleme hin.
MAX_SPREAD_PCT = 0.020   # 2%


# ============================================================
# CORWIN-SCHULTZ HISTORICAL SPREAD ESTIMATOR
# ============================================================

@dataclass
class OHLCDay:
    """Minimaler OHLC-Container fuer Corwin-Schultz."""
    high: float
    low: float
    close: float


def _beta(ohlc_today: OHLCDay, ohlc_yesterday: OHLCDay) -> float:
    """Beta-Komponente: log-square-sum der einzelnen Tages-Ranges.

    Per Corwin-Schultz: beta = sum_i (ln(H_i/L_i))^2  fuer i in {today, yesterday}
    """
    def _sq_log(h, l):
        if h <= 0 or l <= 0:
            return 0.0
        return math.log(h / l) ** 2

    return _sq_log(ohlc_today.high, ohlc_today.low) + \
           _sq_log(ohlc_yesterday.high, ohlc_yesterday.low)


def _gamma(ohlc_today: OHLCDay, ohlc_yesterday: OHLCDay) -> float:
    """Gamma-Komponente: log-square der 2-Tages-Range.

    Per Corwin-Schultz: gamma = (ln(H_2day/L_2day))^2
    Wo H_2day = max der beiden Tages-Highs, L_2day = min der beiden Tages-Lows.
    """
    h2 = max(ohlc_today.high, ohlc_yesterday.high)
    l2 = min(ohlc_today.low, ohlc_yesterday.low)
    if h2 <= 0 or l2 <= 0:
        return 0.0
    return math.log(h2 / l2) ** 2


def estimate_corwin_schultz_spread(
    ohlc_today: OHLCDay,
    ohlc_yesterday: OHLCDay,
) -> float:
    """Schaetzt den effektiven Bid-Ask-Spread aus 2 OHLC-Tagen.

    Corwin-Schultz Formel:
        alpha = (sqrt(2*beta) - sqrt(beta)) / (3 - 2*sqrt(2)) - sqrt(gamma / (3 - 2*sqrt(2)))
        S = 2 * (e^alpha - 1) / (1 + e^alpha)

    Returns:
        Spread als Fraktion des Mid-Price (z.B. 0.001 = 10 bps).
        Geclampt auf [MIN_SPREAD_PCT, MAX_SPREAD_PCT].

    Wenn negative oder ungueltige Werte rauskommen (z.B. bei
    Niedrig-Volatil-Tagen ohne Range): fallback auf MIN_SPREAD_PCT.
    """
    beta = _beta(ohlc_today, ohlc_yesterday)
    gamma = _gamma(ohlc_today, ohlc_yesterday)

    if beta <= 0 or gamma <= 0:
        return MIN_SPREAD_PCT

    denominator = 3 - 2 * math.sqrt(2)
    try:
        alpha = (math.sqrt(2 * beta) - math.sqrt(beta)) / denominator \
                - math.sqrt(gamma / denominator)
        spread = 2 * (math.exp(alpha) - 1) / (1 + math.exp(alpha))
    except (ValueError, ZeroDivisionError, OverflowError):
        return MIN_SPREAD_PCT

    if not math.isfinite(spread):
        return MIN_SPREAD_PCT

    # Clamp + abs (negative Spreads physikalisch unmöglich, deuten auf
    # Daten-Noise hin — Corwin-Schultz erwaehnt das explizit)
    spread = abs(spread)
    return max(MIN_SPREAD_PCT, min(spread, MAX_SPREAD_PCT))


def estimate_avg_spread_from_history(
    ohlc_history: list[dict],
    window_days: int = 60,
) -> float:
    """Mittelt Corwin-Schultz Spread ueber window_days.

    ohlc_history: Liste von dicts mit High/Low/Close (yfinance-Format).
    Wir nehmen die letzten window_days Eintraege.

    Returns:
        Durchschnittlicher Spread als Fraktion des Mid-Price.
        Wenn nicht genug Daten: fallback auf MIN_SPREAD_PCT.
    """
    if not ohlc_history or len(ohlc_history) < 2:
        return MIN_SPREAD_PCT

    recent = ohlc_history[-window_days:] if len(ohlc_history) > window_days else ohlc_history
    spreads = []
    for i in range(1, len(recent)):
        try:
            today = OHLCDay(
                high=float(recent[i].get("High") or recent[i].get("high", 0)),
                low=float(recent[i].get("Low") or recent[i].get("low", 0)),
                close=float(recent[i].get("Close") or recent[i].get("close", 0)),
            )
            yesterday = OHLCDay(
                high=float(recent[i-1].get("High") or recent[i-1].get("high", 0)),
                low=float(recent[i-1].get("Low") or recent[i-1].get("low", 0)),
                close=float(recent[i-1].get("Close") or recent[i-1].get("close", 0)),
            )
            spreads.append(estimate_corwin_schultz_spread(today, yesterday))
        except (ValueError, TypeError):
            continue

    if not spreads:
        return MIN_SPREAD_PCT
    return sum(spreads) / len(spreads)


# ============================================================
# ALMGREN-CHRISS VOLUME-IMPACT
# ============================================================

def almgren_chriss_impact(
    amount_usd: float,
    daily_volume_usd: float,
    daily_volatility_pct: float,
    gamma: float = ALMGREN_CHRISS_GAMMA,
) -> float:
    """Schaetzt Market-Impact via Almgren-Chriss Square-Root-Modell.

    Formel: impact_pct = gamma * sigma * sqrt(Q/V)
    Wo:
        Q = trade size (USD)
        V = daily volume (USD)
        sigma = daily return volatility (als Fraktion)
        gamma = empirischer Konstante (~0.5-1.5, default 1.0)

    Args:
        amount_usd: Trade-Groesse in USD
        daily_volume_usd: Average Daily Volume in USD ($-Volumen, nicht Shares)
        daily_volatility_pct: Tagesvolatilitaet in Prozent (z.B. 1.5 = 1.5%/Tag)
        gamma: Almgren-Chriss-Konstante

    Returns:
        Market-Impact als Fraktion (z.B. 0.0005 = 5 bps).
    """
    if amount_usd <= 0 or daily_volume_usd <= 0 or daily_volatility_pct <= 0:
        return 0.0

    Q_over_V = amount_usd / daily_volume_usd
    sigma = daily_volatility_pct / 100.0  # convert % to fraction

    impact = gamma * sigma * math.sqrt(Q_over_V)
    # Clamp auf vernuenftigen Bereich [0, 5%]
    return max(0.0, min(impact, 0.05))


# ============================================================
# MAIN ENTRY POINT — total cost per round-trip trade
# ============================================================

@dataclass
class CostBreakdown:
    """Detail-Auflistung der Cost-Komponenten fuer Audit/Debug."""
    spread_pct: float           # round-trip
    volume_impact_pct: float    # round-trip (entry + exit kombiniert)
    slippage_buffer_pct: float  # round-trip
    overnight_fee_pct: float    # haengt von days_held ab
    total_pct: float            # summe der oben

    def __str__(self):
        return (
            f"CostBreakdown(spread={self.spread_pct:.4f}, "
            f"impact={self.volume_impact_pct:.4f}, "
            f"slip={self.slippage_buffer_pct:.4f}, "
            f"overnight={self.overnight_fee_pct:.4f}, "
            f"total={self.total_pct:.4f})"
        )


def total_cost_pct(
    asset_class: str,
    amount_usd: float,
    days_held: int,
    ohlc_history: Optional[list[dict]] = None,
    daily_volume_usd: Optional[float] = None,
    daily_volatility_pct: Optional[float] = None,
    overnight_fee_pct_per_day: float = 0.0001,
) -> CostBreakdown:
    """Realistisches Cost-Modell fuer einen Round-Trip-Trade.

    Args:
        asset_class: 'stocks', 'etf', 'crypto', 'forex', 'commodities', 'indices'
        amount_usd: Trade-Groesse
        days_held: Halte-Dauer in Tagen
        ohlc_history: optional, fuer Corwin-Schultz Spread (sonst Class-Default)
        daily_volume_usd: optional, fuer Volume-Impact
        daily_volatility_pct: optional, fuer Volume-Impact (% pro Tag)
        overnight_fee_pct_per_day: pro Tag, default 1 bp

    Returns:
        CostBreakdown mit allen Komponenten + Summe.
    """
    # 1. Spread (round-trip = entry + exit)
    if ohlc_history and len(ohlc_history) >= 5:
        spread_one_side = estimate_avg_spread_from_history(ohlc_history)
    else:
        # Fallback: empirische Class-Defaults aus Industry-Studies
        # (Hasbrouck 2009, Stoll 2000)
        class_spread_defaults = {
            "stocks": 0.0005,        # 5 bps
            "etf": 0.0002,           # 2 bps
            "crypto": 0.0010,        # 10 bps
            "forex": 0.0001,         # 1 bp
            "commodities": 0.0005,   # 5 bps (via ETF-Proxies)
            "indices": 0.0002,       # 2 bps
        }
        spread_one_side = class_spread_defaults.get(asset_class, 0.0010)
    spread_round_trip = spread_one_side * 2  # entry + exit

    # 2. Volume-Impact (Almgren-Chriss, falls Daten verfuegbar)
    if daily_volume_usd and daily_volatility_pct:
        impact_one_side = almgren_chriss_impact(
            amount_usd, daily_volume_usd, daily_volatility_pct,
        )
        # Round-trip: entry und exit beide haben Impact, aber meist asymmetrisch.
        # Konservativ: 2x one-side
        impact_round_trip = impact_one_side * 2
    else:
        impact_round_trip = 0.0

    # 3. Slippage-Buffer (per Asset-Class, hardcoded MVP)
    slip_one_side = PER_CLASS_SLIPPAGE_BUFFER_PCT.get(
        asset_class, DEFAULT_SLIPPAGE_BUFFER_PCT,
    ) / 100.0  # convert % to fraction
    slip_round_trip = slip_one_side * 2

    # 4. Overnight-Fee
    overnight = overnight_fee_pct_per_day * max(days_held, 0)

    total = spread_round_trip + impact_round_trip + slip_round_trip + overnight

    return CostBreakdown(
        spread_pct=spread_round_trip,
        volume_impact_pct=impact_round_trip,
        slippage_buffer_pct=slip_round_trip,
        overnight_fee_pct=overnight,
        total_pct=total,
    )


# ============================================================
# CALIBRATION-OVERRIDES (in 2 Wochen via cost_model_calibrator.py)
# ============================================================

def load_empirical_overrides() -> dict:
    """Laedt empirisch kalibrierte Overrides aus
    data/cost_model_calibration.json (geschrieben vom Calibrator).

    Falls nicht vorhanden: leeres Dict (Defaults greifen).
    """
    try:
        from app.config_manager import load_json
        return load_json("cost_model_calibration.json") or {}
    except Exception:
        return {}
