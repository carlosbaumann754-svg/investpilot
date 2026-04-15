"""
InvestPilot - Makro-Fruehwarnsignale (Phase 2/5, Pre-Live 27.04.2026)

Drei Leading Indicators die Rezessionen/Krisen 6-18 Monate FRUEHER
anzeigen als VIX/Fear&Greed (die reaktiv sind):

1. **Yield Curve Inversion (2Y vs 10Y)** — 2Y > 10Y signalisiert historisch
   eine Rezession in 6-18 Monaten. Datenquelle: FRED API (kostenlos, kein
   API-Key noetig fuer einzelne Series) oder yfinance ^TNX/^IRX als Proxy.

2. **Credit Spread (HYG vs IEF Ratio)** — High-Yield Corporate Bonds vs
   Treasuries. Wenn Investoren Risiko scheuen (HYG faellt relativ zu IEF),
   deutet das auf Kreditstress / Rezessions-Angst hin. Datenquelle: yfinance.

3. **Marktbreite (% SP500-Aktien ueber SMA200)** — Wenn der Index steigt
   aber nur wenige Aktien die Rallye tragen, ist sie fragil. Proxy: Vergleich
   SPY zu RSP (equal-weight). Bei <50% Aktien ueber SMA200 -> Warnung.

Integration: Liefert `score_delta` den der `check_regime_filter` in
market_context.py zum combined_score addieren kann. Defaults sind
konservativ gesetzt — der Filter BLOCKIERT keine Trades solange nicht
alle drei Signale gleichzeitig rot sind.

Siehe CLAUDE.md "Live-Gang Strategie" fuer Go-Live-Kriterien.
"""

import logging
from datetime import datetime, timedelta

try:
    import yfinance as yf
except ImportError:
    yf = None

try:
    import requests
except ImportError:
    requests = None

from app.config_manager import load_json, save_json

log = logging.getLogger("MacroSignals")

MACRO_CACHE_FILE = "macro_signals.json"
CACHE_TTL_MINUTES = 60  # Makro-Signale aendern sich langsam, stuendlich reicht


# ============================================================
# 1) YIELD CURVE (2Y vs 10Y)
# ============================================================

def fetch_yield_curve():
    """Holt 10Y - 2Y Treasury Yield Spread.

    Datenquelle: yfinance ^TNX (10Y) und ^FVX (5Y als Proxy — 2Y gibt's nicht
    direkt auf yfinance, aber 5Y ist nah genug und liquider als der 2Y-Proxy).

    Fallback: FRED API T10Y2Y Series (direkter Spread, kostenlos ohne API-Key
    via fred.stlouisfed.org/graph/fredgraph.csv?id=T10Y2Y).

    Returns:
        dict mit keys `spread_pct` (float, 10Y-2Y in Prozentpunkten),
        `inverted` (bool, True wenn negativ), `source` (str).
        None bei Fehler.
    """
    # Primaer: FRED API direkter Spread
    if requests is not None:
        try:
            url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=T10Y2Y"
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200 and resp.text:
                lines = [l for l in resp.text.strip().split("\n") if l]
                # FRED CSV: "observation_date,T10Y2Y" — letzte nicht-leere Zeile
                for line in reversed(lines[1:]):  # skip header
                    parts = line.split(",")
                    if len(parts) == 2 and parts[1] not in (".", ""):
                        try:
                            spread = float(parts[1])
                            return {
                                "spread_pct": round(spread, 3),
                                "inverted": spread < 0,
                                "source": "FRED:T10Y2Y",
                                "observation_date": parts[0],
                            }
                        except ValueError:
                            continue
        except Exception as e:
            log.warning(f"FRED Yield-Curve Fetch fehlgeschlagen: {e}", exc_info=True)

    # Fallback: yfinance 10Y via ^TNX
    if yf is not None:
        try:
            tnx = yf.Ticker("^TNX").history(period="5d")
            fvx = yf.Ticker("^FVX").history(period="5d")
            if not tnx.empty and not fvx.empty:
                ten_y = float(tnx["Close"].iloc[-1])
                five_y = float(fvx["Close"].iloc[-1])
                # 10Y - 5Y als schlechterer Proxy (5Y hat weniger Inversions-Signal)
                # Historisch: 10Y-2Y inverts bei ~-0.5%, 10Y-5Y bei ~-0.3%
                spread = ten_y - five_y
                return {
                    "spread_pct": round(spread, 3),
                    "inverted": spread < 0,
                    "source": "yfinance:^TNX-^FVX (5Y Proxy)",
                    "observation_date": datetime.now().strftime("%Y-%m-%d"),
                }
        except Exception as e:
            log.warning(f"yfinance Yield-Curve Fallback fehlgeschlagen: {e}", exc_info=True)

    log.error("Yield-Curve: keine Datenquelle erfolgreich (FRED + yfinance beide down)")
    return None


def score_yield_curve(yc_data, config):
    """Bewertet Yield Curve Signal.

    Score-Logik:
    - Spread > 0.5%: normal/positiv (0)
    - 0 < Spread < 0.5%: flat (-0.5) -> Warnung, noch kein Block
    - -0.3% < Spread < 0: leicht invertiert (-1)
    - Spread < -0.5%: tief invertiert (-2) -> historisch starker Rezessions-Signal

    Returns: (score: float, detail: str)
    """
    if not yc_data or yc_data.get("spread_pct") is None:
        return 0, None

    cfg = (config or {}).get("macro_signals", {}).get("yield_curve", {})
    flat_threshold = cfg.get("flat_threshold_pct", 0.5)
    deep_inversion_threshold = cfg.get("deep_inversion_threshold_pct", -0.5)

    spread = yc_data["spread_pct"]

    if spread < deep_inversion_threshold:
        return -2, f"Yield-Curve TIEF INVERTIERT ({spread:+.2f}pp)"
    elif spread < 0:
        return -1, f"Yield-Curve invertiert ({spread:+.2f}pp)"
    elif spread < flat_threshold:
        return -0.5, f"Yield-Curve flach ({spread:+.2f}pp)"
    return 0, None


# ============================================================
# 2) CREDIT SPREAD (HYG vs IEF)
# ============================================================

def fetch_credit_spread():
    """Ratio HYG (High-Yield Corporate Bonds) / IEF (7-10Y Treasuries).

    Wenn Investoren Risiko scheuen, faellt HYG relativ zu IEF -> Ratio sinkt.
    Wir vergleichen den aktuellen Ratio mit dem 90-Tage-Mittel. Abweichung
    > 1.5 Std.abw. unten = Stress.

    Returns: dict mit `ratio`, `ratio_zscore_90d`, `stress` (bool).
    None bei Fehler.
    """
    if yf is None:
        log.warning("Credit-Spread: yfinance nicht verfuegbar")
        return None

    try:
        # Hole 90 Tage History fuer Baseline
        hyg = yf.Ticker("HYG").history(period="90d")["Close"]
        ief = yf.Ticker("IEF").history(period="90d")["Close"]
        if hyg.empty or ief.empty or len(hyg) < 30:
            log.warning("Credit-Spread: unzureichende History")
            return None

        # Ratio-Serie
        ratio = (hyg / ief).dropna()
        if len(ratio) < 30:
            return None

        current = float(ratio.iloc[-1])
        mean = float(ratio.mean())
        std = float(ratio.std())

        if std == 0:
            zscore = 0.0
        else:
            zscore = (current - mean) / std

        return {
            "ratio": round(current, 4),
            "ratio_mean_90d": round(mean, 4),
            "ratio_zscore_90d": round(zscore, 2),
            "stress": zscore < -1.5,
            "source": "yfinance:HYG/IEF",
            "observation_date": datetime.now().strftime("%Y-%m-%d"),
        }
    except Exception as e:
        log.error(f"Credit-Spread Fetch fehlgeschlagen: {e}", exc_info=True)
        return None


def score_credit_spread(cs_data, config):
    """Bewertet Credit-Spread-Signal via Z-Score gegen 90d-Mittel.

    Score-Logik:
    - z > -1: normal (0)
    - -2 < z < -1: leichter Stress (-0.5)
    - -3 < z < -2: moderater Stress (-1)
    - z < -3: schwerer Kreditstress (-2)

    Returns: (score: float, detail: str)
    """
    if not cs_data or cs_data.get("ratio_zscore_90d") is None:
        return 0, None

    cfg = (config or {}).get("macro_signals", {}).get("credit_spread", {})
    mild_z = cfg.get("mild_stress_zscore", -1.0)
    moderate_z = cfg.get("moderate_stress_zscore", -2.0)
    severe_z = cfg.get("severe_stress_zscore", -3.0)

    z = cs_data["ratio_zscore_90d"]

    if z < severe_z:
        return -2, f"Credit-Spread SCHWERER STRESS (z={z:.2f})"
    elif z < moderate_z:
        return -1, f"Credit-Spread moderater Stress (z={z:.2f})"
    elif z < mild_z:
        return -0.5, f"Credit-Spread leichter Stress (z={z:.2f})"
    return 0, None


# ============================================================
# 3) MARKTBREITE (SPY vs RSP Divergenz)
# ============================================================

def fetch_market_breadth():
    """Misst Marktbreite via SPY (cap-weighted) vs RSP (equal-weight).

    Wenn SPY stark steigt aber RSP zurueckbleibt -> wenige Mega-Caps tragen
    die Rallye, Breite ist schwach, Krisenrisiko hoeher.

    Metrik: 20-Tage-Return-Differenz SPY - RSP. Wenn SPY +5% aber RSP nur +1%
    ist die Rallye schmal -> -4pp Divergenz.

    Returns: dict mit `spy_return_20d`, `rsp_return_20d`, `divergence_pp`
    (positive Zahl = schmaler Markt).
    None bei Fehler.
    """
    if yf is None:
        return None

    try:
        spy = yf.Ticker("SPY").history(period="30d")["Close"]
        rsp = yf.Ticker("RSP").history(period="30d")["Close"]
        if len(spy) < 21 or len(rsp) < 21:
            log.warning("Marktbreite: unzureichende History")
            return None

        # 20-Tage-Return
        spy_ret = (float(spy.iloc[-1]) / float(spy.iloc[-21]) - 1) * 100
        rsp_ret = (float(rsp.iloc[-1]) / float(rsp.iloc[-21]) - 1) * 100
        divergence = spy_ret - rsp_ret  # positiv = SPY outperformt RSP = schmaler Markt

        return {
            "spy_return_20d_pct": round(spy_ret, 2),
            "rsp_return_20d_pct": round(rsp_ret, 2),
            "divergence_pp": round(divergence, 2),
            "source": "yfinance:SPY/RSP",
            "observation_date": datetime.now().strftime("%Y-%m-%d"),
        }
    except Exception as e:
        log.error(f"Marktbreite Fetch fehlgeschlagen: {e}", exc_info=True)
        return None


def score_market_breadth(mb_data, config):
    """Bewertet Marktbreite via SPY-RSP-Divergenz.

    Score-Logik:
    - Divergenz < 2pp: OK (0)
    - 2-4pp: schmale Rallye (-0.5)
    - 4-6pp: fragile Rallye (-1)
    - >6pp: nur Mega-Caps tragen (-1.5)

    Returns: (score: float, detail: str)
    """
    if not mb_data or mb_data.get("divergence_pp") is None:
        return 0, None

    cfg = (config or {}).get("macro_signals", {}).get("market_breadth", {})
    narrow_pp = cfg.get("narrow_divergence_pp", 2.0)
    fragile_pp = cfg.get("fragile_divergence_pp", 4.0)
    severe_pp = cfg.get("severe_divergence_pp", 6.0)

    d = mb_data["divergence_pp"]

    if d > severe_pp:
        return -1.5, f"Marktbreite SCHWACH (SPY-RSP +{d:.1f}pp)"
    elif d > fragile_pp:
        return -1, f"Marktbreite fragil (SPY-RSP +{d:.1f}pp)"
    elif d > narrow_pp:
        return -0.5, f"Marktbreite schmal (SPY-RSP +{d:.1f}pp)"
    return 0, None


# ============================================================
# COMPOSITE + CACHE
# ============================================================

def _load_cache():
    return load_json(MACRO_CACHE_FILE) or {}


def _cache_is_fresh(cache):
    """True wenn Cache juenger als CACHE_TTL_MINUTES."""
    ts = cache.get("updated_at")
    if not ts:
        return False
    try:
        updated = datetime.fromisoformat(ts)
        return (datetime.now() - updated).total_seconds() < CACHE_TTL_MINUTES * 60
    except Exception:
        return False


def update_macro_signals(config=None, force_refresh=False):
    """Holt alle drei Signale und schreibt sie in macro_signals.json.

    Wird vom Scheduler stuendlich aufgerufen (zusammen mit update_full_context).
    Cached fuer CACHE_TTL_MINUTES — `force_refresh=True` umgeht Cache.

    Returns: Der komplette Cache-Dict inkl. Composite-Score.
    """
    cache = _load_cache()
    if not force_refresh and _cache_is_fresh(cache):
        log.debug("Macro-Signale: Cache noch frisch, Skip-Fetch")
        return cache

    yc = fetch_yield_curve()
    cs = fetch_credit_spread()
    mb = fetch_market_breadth()

    yc_score, yc_detail = score_yield_curve(yc, config or {})
    cs_score, cs_detail = score_credit_spread(cs, config or {})
    mb_score, mb_detail = score_market_breadth(mb, config or {})

    composite = yc_score + cs_score + mb_score
    details = [d for d in (yc_detail, cs_detail, mb_detail) if d]

    result = {
        "yield_curve": yc,
        "credit_spread": cs,
        "market_breadth": mb,
        "scores": {
            "yield_curve": yc_score,
            "credit_spread": cs_score,
            "market_breadth": mb_score,
            "composite": round(composite, 2),
        },
        "details": details,
        "updated_at": datetime.now().isoformat(),
    }

    try:
        save_json(MACRO_CACHE_FILE, result)
    except Exception as e:
        log.warning(f"Macro-Signale Cache-Write fehlgeschlagen: {e}", exc_info=True)

    log.info(
        f"Macro-Signale: YC={yc_score:+g} CS={cs_score:+g} MB={mb_score:+g} "
        f"Composite={composite:+g} | {'; '.join(details) if details else 'alle gruen'}"
    )
    return result


def get_macro_score(config=None):
    """Fast-Path fuer check_regime_filter — nutzt Cache, returnt nur Score.

    Returns: (score: float, details: list[str])
    """
    cache = _load_cache()
    if not _cache_is_fresh(cache):
        # Trigger Update wenn stale — Scheduler sollte das eh regelmaessig tun,
        # aber wenn check_regime_filter vor dem ersten Update laeuft, refresh.
        cache = update_macro_signals(config=config)

    scores = cache.get("scores") or {}
    composite = scores.get("composite", 0) or 0
    details = cache.get("details") or []
    return float(composite), details
