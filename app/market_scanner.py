"""
InvestPilot - Market Scanner
Scannt alle verfuegbaren Asset-Klassen und bewertet Opportunities.
Nutzt yfinance fuer technische Analyse, eToro API fuer Instrument Discovery.
"""

import logging
import time
from datetime import datetime

log = logging.getLogger("Scanner")

try:
    import yfinance as yf
    import numpy as np
except ImportError:
    yf = None
    np = None

# ============================================================
# ASSET UNIVERSE - eToro Instrument IDs + yfinance Symbole
# Alle gaengigen Assets die auf eToro handelbar sind
# ============================================================

ASSET_UNIVERSE = {
    # --- US Large Cap Stocks ---
    "AAPL":  {"etoro_id": 6408,  "yf": "AAPL",  "class": "stocks",  "name": "Apple"},
    "MSFT":  {"etoro_id": 1139,  "yf": "MSFT",  "class": "stocks",  "name": "Microsoft"},
    "GOOGL": {"etoro_id": 1002,  "yf": "GOOGL", "class": "stocks",  "name": "Alphabet"},
    "AMZN":  {"etoro_id": 1026,  "yf": "AMZN",  "class": "stocks",  "name": "Amazon"},
    "TSLA":  {"etoro_id": 1397,  "yf": "TSLA",  "class": "stocks",  "name": "Tesla"},
    "NVDA":  {"etoro_id": 1518,  "yf": "NVDA",  "class": "stocks",  "name": "NVIDIA"},
    "META":  {"etoro_id": 10548, "yf": "META",  "class": "stocks",  "name": "Meta"},
    "NFLX":  {"etoro_id": 1049,  "yf": "NFLX",  "class": "stocks",  "name": "Netflix"},
    "AMD":   {"etoro_id": 1004,  "yf": "AMD",   "class": "stocks",  "name": "AMD"},
    "INTC":  {"etoro_id": 1033,  "yf": "INTC",  "class": "stocks",  "name": "Intel"},
    # --- US Finance & Health ---
    "JPM":   {"etoro_id": 1036,  "yf": "JPM",   "class": "stocks",  "name": "JPMorgan"},
    "V":     {"etoro_id": 1180,  "yf": "V",     "class": "stocks",  "name": "Visa"},
    "MA":    {"etoro_id": 1089,  "yf": "MA",    "class": "stocks",  "name": "Mastercard"},
    "UNH":   {"etoro_id": 1166,  "yf": "UNH",   "class": "stocks",  "name": "UnitedHealth"},
    "JNJ":   {"etoro_id": 1035,  "yf": "JNJ",   "class": "stocks",  "name": "Johnson & Johnson"},
    "PFE":   {"etoro_id": 1094,  "yf": "PFE",   "class": "stocks",  "name": "Pfizer"},
    # --- US Consumer & Industrial ---
    "KO":    {"etoro_id": 1038,  "yf": "KO",    "class": "stocks",  "name": "Coca-Cola"},
    "PG":    {"etoro_id": 1096,  "yf": "PG",    "class": "stocks",  "name": "Procter & Gamble"},
    "DIS":   {"etoro_id": 1024,  "yf": "DIS",   "class": "stocks",  "name": "Disney"},
    "BA":    {"etoro_id": 1008,  "yf": "BA",    "class": "stocks",  "name": "Boeing"},
    "NKE":   {"etoro_id": 1053,  "yf": "NKE",   "class": "stocks",  "name": "Nike"},
    "MCD":   {"etoro_id": 1044,  "yf": "MCD",   "class": "stocks",  "name": "McDonald's"},
    # --- US Growth / Tech ---
    "PYPL":  {"etoro_id": 5765,  "yf": "PYPL",  "class": "stocks",  "name": "PayPal"},
    "SQ":    {"etoro_id": 7961,  "yf": "SQ",    "class": "stocks",  "name": "Block (Square)"},
    "SHOP":  {"etoro_id": 7905,  "yf": "SHOP",  "class": "stocks",  "name": "Shopify"},
    "UBER":  {"etoro_id": 14066, "yf": "UBER",  "class": "stocks",  "name": "Uber"},
    "COIN":  {"etoro_id": 18001, "yf": "COIN",  "class": "stocks",  "name": "Coinbase"},
    "PLTR":  {"etoro_id": 17181, "yf": "PLTR",  "class": "stocks",  "name": "Palantir"},
    "SNAP":  {"etoro_id": 8014,  "yf": "SNAP",  "class": "stocks",  "name": "Snap"},
    "ROKU":  {"etoro_id": 8150,  "yf": "ROKU",  "class": "stocks",  "name": "Roku"},
    "CRM":   {"etoro_id": 1021,  "yf": "CRM",   "class": "stocks",  "name": "Salesforce"},
    "ADBE":  {"etoro_id": 1003,  "yf": "ADBE",  "class": "stocks",  "name": "Adobe"},
    # --- EU Stocks ---
    "SAP":   {"etoro_id": 1341,  "yf": "SAP",   "class": "stocks",  "name": "SAP"},
    "ASML":  {"etoro_id": 5523,  "yf": "ASML",  "class": "stocks",  "name": "ASML"},
    "LVMH":  {"etoro_id": 1350,  "yf": "MC.PA", "class": "stocks",  "name": "LVMH"},
    # --- ETFs ---
    "SPY":   {"etoro_id": 1116,  "yf": "SPY",   "class": "etf",     "name": "S&P 500 ETF"},
    "QQQ":   {"etoro_id": 1321,  "yf": "QQQ",   "class": "etf",     "name": "NASDAQ 100 ETF"},
    "IWM":   {"etoro_id": 1108,  "yf": "IWM",   "class": "etf",     "name": "Russell 2000 ETF"},
    "DIA":   {"etoro_id": 1101,  "yf": "DIA",   "class": "etf",     "name": "Dow Jones ETF"},
    "XLK":   {"etoro_id": 1125,  "yf": "XLK",   "class": "etf",     "name": "Technology ETF"},
    "XLF":   {"etoro_id": 1123,  "yf": "XLF",   "class": "etf",     "name": "Financial ETF"},
    "XLE":   {"etoro_id": 1122,  "yf": "XLE",   "class": "etf",     "name": "Energy ETF"},
    "GLD":   {"etoro_id": 1105,  "yf": "GLD",   "class": "etf",     "name": "Gold ETF"},
    "SLV":   {"etoro_id": 1115,  "yf": "SLV",   "class": "etf",     "name": "Silver ETF"},
    "TLT":   {"etoro_id": 1120,  "yf": "TLT",   "class": "etf",     "name": "20Y Treasury ETF"},
    "EEM":   {"etoro_id": 1102,  "yf": "EEM",   "class": "etf",     "name": "Emerging Markets ETF"},
    "VNQ":   {"etoro_id": 1127,  "yf": "VNQ",   "class": "etf",     "name": "Real Estate ETF"},
    # --- Crypto ---
    "BTC":   {"etoro_id": 100000, "yf": "BTC-USD",  "class": "crypto", "name": "Bitcoin"},
    "ETH":   {"etoro_id": 100001, "yf": "ETH-USD",  "class": "crypto", "name": "Ethereum"},
    "XRP":   {"etoro_id": 100004, "yf": "XRP-USD",  "class": "crypto", "name": "Ripple"},
    "ADA":   {"etoro_id": 100044, "yf": "ADA-USD",  "class": "crypto", "name": "Cardano"},
    "SOL":   {"etoro_id": 100077, "yf": "SOL-USD",  "class": "crypto", "name": "Solana"},
    "DOGE":  {"etoro_id": 100060, "yf": "DOGE-USD", "class": "crypto", "name": "Dogecoin"},
    "DOT":   {"etoro_id": 100063, "yf": "DOT-USD",  "class": "crypto", "name": "Polkadot"},
    "AVAX":  {"etoro_id": 100072, "yf": "AVAX-USD", "class": "crypto", "name": "Avalanche"},
    "LINK":  {"etoro_id": 100010, "yf": "LINK-USD", "class": "crypto", "name": "Chainlink"},
    "MATIC": {"etoro_id": 100067, "yf": "MATIC-USD","class": "crypto", "name": "Polygon"},
    # --- Commodities (via eToro CFDs) ---
    "GOLD":  {"etoro_id": 5002,  "yf": "GC=F",   "class": "commodities", "name": "Gold"},
    "SILVER":{"etoro_id": 5003,  "yf": "SI=F",   "class": "commodities", "name": "Silver"},
    "OIL":   {"etoro_id": 5001,  "yf": "CL=F",   "class": "commodities", "name": "Crude Oil"},
    "NGAS":  {"etoro_id": 5007,  "yf": "NG=F",   "class": "commodities", "name": "Natural Gas"},
    "COPPER":{"etoro_id": 5009,  "yf": "HG=F",   "class": "commodities", "name": "Copper"},
    # --- Forex ---
    "EURUSD":{"etoro_id": 1,     "yf": "EURUSD=X","class": "forex", "name": "EUR/USD"},
    "GBPUSD":{"etoro_id": 2,     "yf": "GBPUSD=X","class": "forex", "name": "GBP/USD"},
    "USDJPY":{"etoro_id": 3,     "yf": "JPY=X",   "class": "forex", "name": "USD/JPY"},
    "USDCHF":{"etoro_id": 4,     "yf": "CHF=X",   "class": "forex", "name": "USD/CHF"},
    "AUDUSD":{"etoro_id": 5,     "yf": "AUDUSD=X","class": "forex", "name": "AUD/USD"},
    # --- Indices (CFDs) ---
    "SPX500": {"etoro_id": 10136, "yf": "^GSPC",  "class": "indices", "name": "S&P 500"},
    "NSDQ100":{"etoro_id": 10137, "yf": "^IXIC",  "class": "indices", "name": "NASDAQ 100"},
    "DJ30":   {"etoro_id": 10138, "yf": "^DJI",   "class": "indices", "name": "Dow Jones 30"},
    "DAX":    {"etoro_id": 10141, "yf": "^GDAXI", "class": "indices", "name": "DAX 40"},
}


# ============================================================
# TECHNISCHE ANALYSE
# ============================================================

def calc_rsi(prices, period=14):
    """Relative Strength Index berechnen."""
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]

    if len(gains) < period:
        return 50  # neutral

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calc_macd(prices, fast=12, slow=26, signal=9):
    """MACD berechnen (vereinfacht mit EMA)."""
    if len(prices) < slow + signal:
        return 0, 0, 0

    def ema(data, period):
        k = 2 / (period + 1)
        result = [data[0]]
        for i in range(1, len(data)):
            result.append(data[i] * k + result[-1] * (1 - k))
        return result

    ema_fast = ema(prices, fast)
    ema_slow = ema(prices, slow)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_line = ema(macd_line, signal)

    return macd_line[-1], signal_line[-1], macd_line[-1] - signal_line[-1]


def calc_bollinger_position(prices, period=20):
    """Position relativ zu Bollinger Bands (0=unteres Band, 1=oberes Band)."""
    if len(prices) < period:
        return 0.5

    recent = prices[-period:]
    mean = sum(recent) / len(recent)
    std = (sum((p - mean) ** 2 for p in recent) / len(recent)) ** 0.5

    if std == 0:
        return 0.5

    upper = mean + 2 * std
    lower = mean - 2 * std
    current = prices[-1]

    if upper == lower:
        return 0.5
    return max(0, min(1, (current - lower) / (upper - lower)))


def analyze_single_asset(symbol, asset_info):
    """Technische Analyse fuer ein einzelnes Asset."""
    if yf is None:
        return None

    yf_symbol = asset_info.get("yf", symbol)

    try:
        ticker = yf.Ticker(yf_symbol)
        hist = ticker.history(period="3mo", interval="1d")

        if hist.empty or len(hist) < 20:
            return None

        closes = hist["Close"].tolist()
        volumes = hist["Volume"].tolist()

        # Technische Indikatoren
        rsi = calc_rsi(closes)
        macd_val, signal_val, macd_hist = calc_macd(closes)
        boll_pos = calc_bollinger_position(closes)

        # Preis-Momentum
        if len(closes) >= 5:
            momentum_5d = (closes[-1] - closes[-5]) / closes[-5] * 100
        else:
            momentum_5d = 0

        if len(closes) >= 20:
            momentum_20d = (closes[-1] - closes[-20]) / closes[-20] * 100
            sma_20 = sum(closes[-20:]) / 20
        else:
            momentum_20d = 0
            sma_20 = closes[-1]

        if len(closes) >= 50:
            sma_50 = sum(closes[-50:]) / 50
        else:
            sma_50 = sma_20

        # Volumen-Trend
        if len(volumes) >= 10 and sum(volumes[-10:-5]) > 0:
            vol_trend = sum(volumes[-5:]) / sum(volumes[-10:-5])
        else:
            vol_trend = 1.0

        # Volatilitaet (20-Tage)
        if len(closes) >= 20:
            returns = [(closes[i] - closes[i-1]) / closes[i-1]
                       for i in range(max(1, len(closes)-20), len(closes))]
            volatility = (sum(r**2 for r in returns) / len(returns)) ** 0.5 * 100
        else:
            volatility = 5.0

        current_price = closes[-1]
        above_sma20 = current_price > sma_20
        above_sma50 = current_price > sma_50
        golden_cross = sma_20 > sma_50

        return {
            "symbol": symbol,
            "name": asset_info["name"],
            "class": asset_info["class"],
            "etoro_id": asset_info["etoro_id"],
            "price": round(current_price, 4),
            "rsi": round(rsi, 1),
            "macd": round(macd_val, 4),
            "macd_signal": round(signal_val, 4),
            "macd_histogram": round(macd_hist, 4),
            "bollinger_pos": round(boll_pos, 3),
            "momentum_5d": round(momentum_5d, 2),
            "momentum_20d": round(momentum_20d, 2),
            "volatility": round(volatility, 2),
            "volume_trend": round(vol_trend, 2),
            "above_sma20": above_sma20,
            "above_sma50": above_sma50,
            "golden_cross": golden_cross,
        }

    except Exception as e:
        log.debug(f"  Fehler bei {symbol}: {e}")
        return None


# ============================================================
# SCORING
# ============================================================

def score_asset(analysis):
    """Berechne einen Gesamtscore (-100 bis +100) fuer ein Asset."""
    score = 0

    # RSI Signal (-20 bis +20)
    rsi = analysis["rsi"]
    if rsi < 30:
        score += 20  # Ueberverkauft = Kaufsignal
    elif rsi < 40:
        score += 10
    elif rsi > 70:
        score -= 20  # Ueberkauft = Verkaufssignal
    elif rsi > 60:
        score -= 5

    # MACD Signal (-15 bis +15)
    if analysis["macd_histogram"] > 0:
        score += 10
        if analysis["macd"] > analysis["macd_signal"]:
            score += 5  # Bullish crossover
    else:
        score -= 10
        if analysis["macd"] < analysis["macd_signal"]:
            score -= 5  # Bearish crossover

    # Momentum (-20 bis +20)
    m5 = analysis["momentum_5d"]
    m20 = analysis["momentum_20d"]
    score += max(-10, min(10, m5 * 2))   # kurzfristiges Momentum
    score += max(-10, min(10, m20 * 0.5)) # laengerfristiges Momentum

    # Trend (SMA) (-15 bis +15)
    if analysis["golden_cross"]:
        score += 10
    if analysis["above_sma20"]:
        score += 5
    elif not analysis["above_sma20"]:
        score -= 5
    if analysis["above_sma50"]:
        score += 5
    elif not analysis["above_sma50"]:
        score -= 5

    # Bollinger (-10 bis +10)
    boll = analysis["bollinger_pos"]
    if boll < 0.2:
        score += 10  # Nahe unterem Band = Kaufsignal
    elif boll > 0.8:
        score -= 10  # Nahe oberem Band = Verkaufssignal

    # Volumen-Bestaetigung (-5 bis +5)
    if analysis["volume_trend"] > 1.2 and score > 0:
        score += 5  # Steigendes Volumen bestaetigt Aufwaertstrend
    elif analysis["volume_trend"] > 1.2 and score < 0:
        score -= 5  # Steigendes Volumen bestaetigt Abwaertstrend

    # Volatilitaets-Malus
    if analysis["volatility"] > 5:
        score *= 0.9  # Hohe Volatilitaet = etwas weniger Vertrauen

    return round(score, 1)


# ============================================================
# SCANNER HAUPTFUNKTION
# ============================================================

def scan_all_assets(enabled_classes=None, max_per_class=None):
    """
    Scannt alle Assets im Universum und gibt sortierte Opportunities zurueck.

    Args:
        enabled_classes: Liste von Asset-Klassen (None = alle)
        max_per_class: Max Anzahl Resultate pro Klasse (None = unbegrenzt)

    Returns:
        Liste von {symbol, name, class, etoro_id, score, analysis, signal}
    """
    if yf is None:
        log.error("yfinance nicht installiert - Scanner deaktiviert")
        return []

    if enabled_classes is None:
        enabled_classes = ["stocks", "etf", "crypto", "commodities", "forex", "indices"]

    log.info("=" * 55)
    log.info("MARKET SCANNER - Alle Asset-Klassen")
    log.info(f"  Klassen: {', '.join(enabled_classes)}")
    log.info(f"  Universum: {len(ASSET_UNIVERSE)} Assets")
    log.info("=" * 55)

    # Filtere nach aktivierten Klassen
    to_scan = {s: info for s, info in ASSET_UNIVERSE.items()
               if info["class"] in enabled_classes}

    log.info(f"  Scanne {len(to_scan)} Assets...")

    results = []
    errors = 0

    for symbol, info in to_scan.items():
        analysis = analyze_single_asset(symbol, info)
        if analysis is None:
            errors += 1
            continue

        score = score_asset(analysis)

        # Signal bestimmen
        if score >= 25:
            signal = "STRONG_BUY"
        elif score >= 10:
            signal = "BUY"
        elif score <= -25:
            signal = "STRONG_SELL"
        elif score <= -10:
            signal = "SELL"
        else:
            signal = "HOLD"

        results.append({
            "symbol": symbol,
            "name": info["name"],
            "class": info["class"],
            "etoro_id": info["etoro_id"],
            "score": score,
            "signal": signal,
            "analysis": analysis,
        })

        # Rate limiting - yfinance mag keine zu schnellen Requests
        time.sleep(0.3)

    # Sortiere nach Score (beste zuerst)
    results.sort(key=lambda x: x["score"], reverse=True)

    # Optional: Limitiere pro Klasse
    if max_per_class:
        class_counts = {}
        filtered = []
        for r in results:
            c = r["class"]
            class_counts[c] = class_counts.get(c, 0) + 1
            if class_counts[c] <= max_per_class:
                filtered.append(r)
        results = filtered

    # Logging
    buy_signals = [r for r in results if r["signal"] in ("BUY", "STRONG_BUY")]
    sell_signals = [r for r in results if r["signal"] in ("SELL", "STRONG_SELL")]

    log.info(f"\n  Scan komplett: {len(results)} analysiert, {errors} Fehler")
    log.info(f"  Kaufsignale: {len(buy_signals)}")
    log.info(f"  Verkaufssignale: {len(sell_signals)}")

    log.info("\n  TOP 10 Kaufsignale:")
    for r in buy_signals[:10]:
        log.info(f"    [{r['signal']}] {r['symbol']:8s} ({r['class']:12s}) "
                 f"Score={r['score']:+6.1f}  RSI={r['analysis']['rsi']:.0f}  "
                 f"Mom5d={r['analysis']['momentum_5d']:+.1f}%")

    if sell_signals:
        log.info("\n  TOP 5 Verkaufssignale:")
        for r in sell_signals[:5]:
            log.info(f"    [{r['signal']}] {r['symbol']:8s} ({r['class']:12s}) "
                     f"Score={r['score']:+6.1f}  RSI={r['analysis']['rsi']:.0f}")

    return results


def get_top_opportunities(results, top_n=10):
    """Extrahiere die besten Kauf-Opportunities."""
    buys = [r for r in results if r["signal"] in ("BUY", "STRONG_BUY")]
    return buys[:top_n]


def get_sell_candidates(results):
    """Extrahiere Verkaufs-Kandidaten."""
    return [r for r in results if r["signal"] in ("SELL", "STRONG_SELL")]


# ============================================================
# MULTI-TIMEFRAME ANALYSE
# ============================================================

def analyze_multi_timeframe(symbol, asset_info):
    """Multi-Timeframe-Analyse: 1h Trend + 15min Entry + 5min SL.

    1-Stunden-Chart: Uebergeordnete Trendrichtung
    15-Minuten-Chart: Praeziser Einstiegspunkt
    5-Minuten-Chart: Stop-Loss-Feinabstimmung

    Hinweis: yfinance hat begrenzte Intraday-Daten. Wir verwenden:
    - 1mo mit 1h Interval fuer Trendrichtung
    - 5d mit 15m Interval fuer Entry
    - 1d mit 5m Interval fuer SL
    """
    if yf is None:
        return None

    yf_symbol = asset_info.get("yf", symbol)

    try:
        ticker = yf.Ticker(yf_symbol)

        # 1H Chart: Trend-Richtung (letzte 2 Wochen)
        h1 = ticker.history(period="1mo", interval="1h")
        trend_direction = "neutral"
        if not h1.empty and len(h1) >= 20:
            closes_1h = h1["Close"].tolist()
            sma_short = sum(closes_1h[-10:]) / 10
            sma_long = sum(closes_1h[-20:]) / 20
            if sma_short > sma_long * 1.005:
                trend_direction = "up"
            elif sma_short < sma_long * 0.995:
                trend_direction = "down"

            # 1H RSI
            rsi_1h = calc_rsi(closes_1h)
        else:
            rsi_1h = 50

        # 15M Chart: Entry Signal (letzte 5 Tage)
        m15 = ticker.history(period="5d", interval="15m")
        entry_signal = "neutral"
        rsi_15m = 50
        if not m15.empty and len(m15) >= 20:
            closes_15m = m15["Close"].tolist()
            rsi_15m = calc_rsi(closes_15m)
            macd_val, signal_val, hist = calc_macd(closes_15m)

            if rsi_15m < 35 and hist > 0:
                entry_signal = "buy"
            elif rsi_15m > 65 and hist < 0:
                entry_signal = "sell"

        # 5M Chart: SL Bestimmung (letzter Tag)
        m5 = ticker.history(period="1d", interval="5m")
        suggested_sl = None
        if not m5.empty and len(m5) >= 10:
            closes_5m = m5["Close"].tolist()
            lows = m5["Low"].tolist()
            current = closes_5m[-1]
            recent_low = min(lows[-12:])  # Letztes Stunden-Tief
            if current > 0:
                suggested_sl = round((recent_low - current) / current * 100, 2)

        return {
            "trend_1h": trend_direction,
            "rsi_1h": round(rsi_1h, 1),
            "entry_15m": entry_signal,
            "rsi_15m": round(rsi_15m, 1),
            "suggested_sl_pct": suggested_sl,
            "mtf_aligned": (trend_direction == "up" and entry_signal == "buy") or
                           (trend_direction == "down" and entry_signal == "sell"),
        }

    except Exception as e:
        log.debug(f"  MTF Fehler bei {symbol}: {e}")
        return None


def enrich_with_mtf(scan_results, top_n=20):
    """Ergaenze die Top-N Scanner-Ergebnisse mit Multi-Timeframe-Daten."""
    enriched = 0
    for result in scan_results[:top_n]:
        if result["signal"] not in ("BUY", "STRONG_BUY", "SELL", "STRONG_SELL"):
            continue

        asset_info = ASSET_UNIVERSE.get(result["symbol"])
        if not asset_info:
            continue

        mtf = analyze_multi_timeframe(result["symbol"], asset_info)
        if mtf:
            result["mtf"] = mtf
            # Score-Bonus wenn alle Timeframes aligned sind
            if mtf["mtf_aligned"]:
                result["score"] = round(result["score"] * 1.15, 1)  # 15% Bonus
                log.info(f"    MTF ALIGNED: {result['symbol']} -> Score {result['score']:+.1f}")
            enriched += 1

        time.sleep(0.5)  # Rate limiting

    log.info(f"  MTF: {enriched} Assets mit Multi-Timeframe angereichert")
    return scan_results
