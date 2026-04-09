"""
InvestPilot - Market Context
Makro-Ereignisse, Sentiment-Layer, erweiterte Regime-Erkennung,
VIX-Monitoring, Wirtschaftskalender.
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

from app.config_manager import load_config, load_json, save_json

log = logging.getLogger("MarketContext")

MARKET_CONTEXT_FILE = "market_context.json"


def _load_context():
    return load_json(MARKET_CONTEXT_FILE) or {
        "vix_level": None,
        "fear_greed_index": None,
        "market_regime": "unknown",
        "macro_events_today": [],
        "last_update": None,
    }


def _save_context(ctx):
    save_json(MARKET_CONTEXT_FILE, ctx)


# ============================================================
# VIX (Volatility Index)
# ============================================================

def fetch_vix():
    """Hole aktuellen VIX-Wert via yfinance."""
    if yf is None:
        return None
    try:
        vix = yf.Ticker("^VIX")
        hist = vix.history(period="5d")
        if not hist.empty:
            val = round(hist["Close"].iloc[-1], 2)
            log.info(f"  VIX: {val}")
            return val
    except Exception as e:
        log.debug(f"VIX Fehler: {e}")
    return None


def get_vix_regime(vix_value):
    """Klassifiziere VIX-Level.

    <15: Low Volatility (Complacency)
    15-20: Normal
    20-30: Elevated (Caution)
    30+: High Fear (Reduce Positions)
    """
    if vix_value is None:
        return "unknown"
    if vix_value < 15:
        return "low_vol"
    elif vix_value < 20:
        return "normal"
    elif vix_value < 30:
        return "elevated"
    else:
        return "high_fear"


def fetch_vix_term_structure():
    """v12: VIX Term Structure ^VIX9D (9-Tage) vs ^VIX (30d) vs ^VIX3M (90d).

    Backwardation (VIX9D > VIX > VIX3M): akute kurzfristige Panik,
        historisch oft Mean-Reversion-Signal (buy-the-dip).
    Contango (VIX9D < VIX < VIX3M): normaler Zustand, institutionelles
        Hedging im Hintergrund, keine akute Panik.
    Steep Contango (VIX3M deutlich ueber VIX9D): komfortable Marktstruktur.

    Returns dict oder None:
        {vix9d, vix, vix3m, ratio_9d_vs_30d, shape, is_backwardation,
         spike_warning, panic_dip_buy_signal}
    """
    if yf is None:
        return None
    try:
        tickers = {"vix9d": "^VIX9D", "vix": "^VIX", "vix3m": "^VIX3M"}
        out = {}
        for key, sym in tickers.items():
            try:
                hist = yf.Ticker(sym).history(period="5d")
                if not hist.empty:
                    out[key] = round(float(hist["Close"].iloc[-1]), 2)
            except Exception as e:
                log.debug(f"VIX-Term fetch {sym}: {e}")
        if not out.get("vix") or not out.get("vix9d"):
            return None

        vix9d = out["vix9d"]
        vix = out["vix"]
        vix3m = out.get("vix3m")
        ratio = vix9d / vix if vix > 0 else 1.0

        # Shape
        if vix3m and vix9d < vix < vix3m:
            shape = "contango"
        elif vix3m and vix9d > vix > vix3m:
            shape = "backwardation"
        elif vix9d > vix:
            shape = "short_term_stress"
        else:
            shape = "flat"

        is_backwardation = shape == "backwardation" or shape == "short_term_stress"
        # Spike warning: 9D liegt deutlich ueber 30D (> 1.15)
        spike_warning = ratio > 1.15
        # Panic dip buy signal: starke Backwardation + VIX auf Stressniveau
        panic_dip_buy_signal = is_backwardation and vix >= 22 and ratio > 1.20

        result = {
            "vix9d": vix9d,
            "vix": vix,
            "vix3m": vix3m,
            "ratio_9d_vs_30d": round(ratio, 3),
            "shape": shape,
            "is_backwardation": is_backwardation,
            "spike_warning": spike_warning,
            "panic_dip_buy_signal": panic_dip_buy_signal,
        }
        log.info(f"  VIX Term: 9D={vix9d} 30D={vix} 3M={vix3m} "
                 f"ratio={ratio:.2f} shape={shape}"
                 + (" [PANIC-DIP-BUY]" if panic_dip_buy_signal else ""))
        return result
    except Exception as e:
        log.debug(f"VIX Term Structure Fehler: {e}")
        return None


# ============================================================
# FEAR & GREED INDEX
# ============================================================

def fetch_fear_greed():
    """Hole CNN Fear & Greed Index (via Alternative API)."""
    if not requests:
        return None
    try:
        resp = requests.get(
            "https://api.alternative.me/fng/?limit=1",
            timeout=10
        )
        if resp.status_code == 200:
            data = resp.json().get("data", [{}])[0]
            value = int(data.get("value", 50))
            classification = data.get("value_classification", "Neutral")
            log.info(f"  Fear & Greed: {value} ({classification})")
            return {"value": value, "classification": classification}
    except Exception as e:
        log.debug(f"Fear & Greed Fehler: {e}")
    return None


def get_sentiment_adjustment(fear_greed_value):
    """Berechne Sentiment-basierte Anpassung (-1 bis +1).

    Extreme Fear (<25): Kontra-Indikator -> leicht bullish (+0.3)
    Fear (25-45): Neutral leicht bearish (-0.1)
    Neutral (45-55): Keine Anpassung
    Greed (55-75): Neutral leicht bearish (-0.1)
    Extreme Greed (>75): Kontra-Indikator -> bearish (-0.3)
    """
    if fear_greed_value is None:
        return 0

    if fear_greed_value < 25:
        return 0.3   # Extreme Fear = Kaufgelegenheit
    elif fear_greed_value < 45:
        return -0.1
    elif fear_greed_value <= 55:
        return 0
    elif fear_greed_value <= 75:
        return -0.1
    else:
        return -0.3  # Extreme Greed = Vorsicht


# ============================================================
# MAKRO-EREIGNIS-KALENDER
# ============================================================

# Statischer Kalender fuer wichtige regelmaessige Ereignisse
# Format: (weekday, day_range, description)
# Dynamische Daten koennen spaeter via API ergaenzt werden
RECURRING_MACRO_EVENTS = {
    "FOMC": {
        "description": "Fed Interest Rate Decision",
        "impact": "high",
        "affected_classes": ["stocks", "etf", "forex", "indices", "commodities"],
    },
    "NFP": {
        "description": "Non-Farm Payrolls (1. Freitag im Monat)",
        "impact": "high",
        "affected_classes": ["stocks", "etf", "forex", "indices"],
    },
    "CPI": {
        "description": "Consumer Price Index",
        "impact": "high",
        "affected_classes": ["stocks", "etf", "forex", "indices", "commodities"],
    },
    "ECB": {
        "description": "EZB Zinsentscheid",
        "impact": "high",
        "affected_classes": ["forex", "indices"],
    },
    "SNB": {
        "description": "SNB Zinsentscheid",
        "impact": "medium",
        "affected_classes": ["forex"],
    },
    "BOJ": {
        "description": "Bank of Japan Entscheid",
        "impact": "medium",
        "affected_classes": ["forex"],
    },
}


def fetch_economic_calendar(config=None):
    """Lade Wirtschaftskalender.

    Nutzt eine kostenlose API oder manuell gepflegte Daten.
    Speichert Ergebnisse in market_context.json.
    """
    if config is None:
        config = load_config()

    ctx = _load_context()
    now = datetime.now()

    # Nur 1x pro Stunde aktualisieren
    if ctx.get("last_calendar_fetch"):
        try:
            last = datetime.fromisoformat(ctx["last_calendar_fetch"])
            if (now - last).total_seconds() < 3600:
                return ctx.get("macro_events_today", [])
        except (ValueError, TypeError):
            pass

    events = []

    # 1. Statische Heuristik: NFP = 1. Freitag im Monat
    if now.weekday() == 4 and now.day <= 7:
        events.append({
            "name": "NFP",
            "description": "Non-Farm Payrolls Release",
            "impact": "high",
            "time": "14:30 CET",
        })

    # 2. Finnhub Economic Calendar (gratis, hohe Qualitaet)
    try:
        from app import finnhub_client
        if finnhub_client.is_available():
            fh_events = finnhub_client.fetch_economic_calendar(days_ahead=1)
            # Filter: nur heute (auch todayaelter als jetzt duerfen raus)
            today_str = now.strftime("%Y-%m-%d")
            for ev in fh_events:
                t = ev.get("time") or ""
                # Finnhub Time-Format: "YYYY-MM-DD HH:MM:SS" (UTC)
                if today_str in t:
                    # Duplikat-Check vs static NFP
                    if not any(e.get("name") == ev.get("name") for e in events):
                        events.append(ev)
    except Exception as e:
        log.debug(f"Finnhub-Calendar Fehler: {e}")

    # 3. Fallback: Manuelle Events aus Config
    manual_events = config.get("market_context", {}).get("manual_events", [])
    for event in manual_events:
        event_date = event.get("date", "")
        if event_date == now.strftime("%Y-%m-%d"):
            events.append(event)

    ctx["macro_events_today"] = events
    ctx["last_calendar_fetch"] = now.isoformat()
    _save_context(ctx)

    return events


def is_high_impact_event_window(events=None):
    """Pruefe ob wir in einem Hochrisiko-Zeitfenster sind.

    Reduziere Positionsgroessen 2h vor und 1h nach High-Impact Events.
    """
    if events is None:
        ctx = _load_context()
        events = ctx.get("macro_events_today", [])

    high_impact = [e for e in events if e.get("impact") == "high"]
    return len(high_impact) > 0


def get_position_size_multiplier(events=None, vix_level=None):
    """Berechne Positions-Multiplikator basierend auf Marktkontext.

    0.0 = Nicht handeln
    0.5 = Halbe Groesse
    1.0 = Normal
    """
    multiplier = 1.0

    # Makro-Events
    if is_high_impact_event_window(events):
        multiplier *= 0.5
        log.info("  Marktkontext: Reduziere um 50% (Makro-Event)")

    # VIX
    if vix_level is not None:
        if vix_level > 30:
            multiplier *= 0.5
            log.info(f"  Marktkontext: Reduziere um 50% (VIX={vix_level})")
        elif vix_level > 25:
            multiplier *= 0.75
            log.info(f"  Marktkontext: Reduziere um 25% (VIX={vix_level})")

    return round(multiplier, 2)


# ============================================================
# BTC DOMINANZ (Crypto-Regime)
# ============================================================

def fetch_btc_dominance():
    """Hole BTC Dominance als Proxy fuer Crypto-Regime."""
    if yf is None:
        return None
    try:
        btc = yf.Ticker("BTC-USD")
        btc_hist = btc.history(period="1mo")
        if btc_hist.empty:
            return None

        # BTC Dominance ist nicht direkt via yfinance verfuegbar,
        # daher verwenden wir BTC vs ETH Performance als Proxy
        eth = yf.Ticker("ETH-USD")
        eth_hist = eth.history(period="1mo")
        if eth_hist.empty:
            return None

        btc_change = (btc_hist["Close"].iloc[-1] - btc_hist["Close"].iloc[0]) / btc_hist["Close"].iloc[0] * 100
        eth_change = (eth_hist["Close"].iloc[-1] - eth_hist["Close"].iloc[0]) / eth_hist["Close"].iloc[0] * 100

        # Wenn BTC staerker als ETH -> Hohe Dominanz -> Altcoins meiden
        dominance_proxy = btc_change - eth_change
        log.info(f"  BTC Dominance Proxy: {dominance_proxy:+.1f}% (BTC: {btc_change:+.1f}%, ETH: {eth_change:+.1f}%)")
        return round(dominance_proxy, 2)
    except Exception as e:
        log.debug(f"BTC Dominance Fehler: {e}")
    return None


def should_avoid_altcoins(btc_dominance_proxy):
    """Pruefe ob Altcoins gemieden werden sollten (hohe BTC Dominanz)."""
    if btc_dominance_proxy is None:
        return False
    # Wenn BTC 10%+ staerker als ETH -> Altcoins meiden
    return btc_dominance_proxy > 10


# ============================================================
# EARNINGS KALENDER (Aktien)
# ============================================================

def check_earnings_window(symbol):
    """Pruefe ob ein Aktien-Symbol in einem Earnings-Fenster liegt.

    3 Tage vor und 1 Tag nach Earnings: nicht handeln.
    """
    if yf is None:
        return False, None

    try:
        ticker = yf.Ticker(symbol)
        calendar = ticker.calendar
        if calendar is None or calendar.empty:
            return False, None

        # yfinance gibt Earnings-Datum zurueck
        if hasattr(calendar, 'get'):
            earnings_date = calendar.get("Earnings Date")
        elif hasattr(calendar, 'iloc'):
            earnings_date = calendar.iloc[0] if len(calendar) > 0 else None
        else:
            return False, None

        if earnings_date is None:
            return False, None

        now = datetime.now()
        if hasattr(earnings_date, '__iter__') and not isinstance(earnings_date, str):
            earnings_date = list(earnings_date)[0]

        if hasattr(earnings_date, 'to_pydatetime'):
            earnings_dt = earnings_date.to_pydatetime().replace(tzinfo=None)
        else:
            return False, None

        days_until = (earnings_dt - now).days

        # 3 Tage vor bis 1 Tag nach
        if -1 <= days_until <= 3:
            log.info(f"  Earnings-Fenster: {symbol} in {days_until} Tagen")
            return True, earnings_dt.strftime("%Y-%m-%d")

        return False, earnings_dt.strftime("%Y-%m-%d")

    except Exception as e:
        log.debug(f"Earnings Check Fehler fuer {symbol}: {e}")
        return False, None


# ============================================================
# FULL CONTEXT UPDATE
# ============================================================

def update_full_context(config=None):
    """Aktualisiere den gesamten Marktkontext. Wird 1x pro Stunde aufgerufen."""
    if config is None:
        config = load_config()

    ctx = _load_context()
    now = datetime.now()

    # VIX
    vix = fetch_vix()
    if vix is not None:
        ctx["vix_level"] = vix
        ctx["vix_regime"] = get_vix_regime(vix)

    # v12: VIX Term Structure (leading regime indicator)
    vts = fetch_vix_term_structure()
    if vts is not None:
        ctx["vix_term_structure"] = vts

    # Fear & Greed
    fg = fetch_fear_greed()
    if fg:
        ctx["fear_greed_index"] = fg["value"]
        ctx["fear_greed_class"] = fg["classification"]
        ctx["sentiment_adjustment"] = get_sentiment_adjustment(fg["value"])

    # Makro-Kalender
    events = fetch_economic_calendar(config)
    ctx["macro_events_today"] = events
    ctx["high_impact_window"] = is_high_impact_event_window(events)

    # BTC Dominance
    btc_dom = fetch_btc_dominance()
    if btc_dom is not None:
        ctx["btc_dominance_proxy"] = btc_dom
        ctx["avoid_altcoins"] = should_avoid_altcoins(btc_dom)

    ctx["last_update"] = now.isoformat()
    ctx["position_size_multiplier"] = get_position_size_multiplier(events, vix)

    _save_context(ctx)
    log.info(f"  Marktkontext aktualisiert: VIX={ctx.get('vix_level')}, "
             f"F&G={ctx.get('fear_greed_index')}, "
             f"Events={len(events)}, Multiplier={ctx.get('position_size_multiplier')}")

    return ctx


def get_current_context():
    """Hole aktuellen Marktkontext (aus Cache)."""
    return _load_context()


# ============================================================
# SAISONALITAET (Rohstoffe)
# ============================================================

def get_seasonal_adjustment(asset_class, symbol):
    """Einfaches Saisonalitaets-Signal fuer Rohstoffe.

    Gold: Q4 und Q1 historisch stark
    Oil: Sommer (Driving Season) und Winter (Heating)
    """
    if asset_class != "commodities":
        return 0

    month = datetime.now().month
    symbol_upper = symbol.upper()

    if "GOLD" in symbol_upper:
        if month in (10, 11, 12, 1, 2):
            return 0.2  # Leicht bullish in Q4/Q1
        elif month in (6, 7, 8):
            return -0.1  # Leicht bearish im Sommer
    elif "OIL" in symbol_upper:
        if month in (5, 6, 7):
            return 0.15  # Driving Season
        elif month in (11, 12, 1):
            return 0.1  # Heating Season
    elif "NGAS" in symbol_upper:
        if month in (10, 11, 12, 1):
            return 0.2  # Winter-Nachfrage
        elif month in (4, 5, 6):
            return -0.15  # Uebergangsperiode

    return 0


# ============================================================
# KOMBINIERTER REGIME-FILTER
# ============================================================

def check_regime_filter(config=None):
    """Kombinierter Regime-Filter: VIX + Fear&Greed + Brain-Regime.

    Bewertet die aktuelle Marktlage anhand mehrerer Indikatoren
    und blockiert neue BUY-Trades bei unguenstigen Bedingungen.

    Returns:
        tuple: (buy_allowed: bool, reason: str, regime_data: dict)
    """
    if config is None:
        config = load_config()

    rf = config.get("regime_filter", {})

    # Feature-Toggle: Wenn deaktiviert, immer erlauben
    if not rf.get("enabled", True):
        return True, "Regime-Filter deaktiviert", {}

    # Schwellenwerte aus Config
    vix_crisis = rf.get("vix_crisis_threshold", 35)
    vix_caution = rf.get("vix_caution_threshold", 25)
    fg_crisis = rf.get("fear_greed_crisis_threshold", 15)
    fg_fear = rf.get("fear_greed_fear_threshold", 25)
    score_threshold = rf.get("combined_score_threshold", -2)

    # Aktuellen Context laden
    ctx = get_current_context()
    combined_score = 0
    details = []

    regime_data = {
        "vix_level": ctx.get("vix_level"),
        "fear_greed_index": ctx.get("fear_greed_index"),
        "brain_regime": "unknown",
        "combined_score": 0,
        "score_threshold": score_threshold,
    }

    # --- VIX-Bewertung ---
    vix_level = ctx.get("vix_level")
    if vix_level is not None:
        if vix_level > vix_crisis:
            combined_score -= 2
            details.append(f"VIX={vix_level:.1f} CRISIS (>{vix_crisis})")
            log.warning(f"  Regime-Filter: VIX {vix_level:.1f} = CRISIS (-2)")
        elif vix_level > vix_caution:
            combined_score -= 1
            details.append(f"VIX={vix_level:.1f} CAUTION (>{vix_caution})")
            log.info(f"  Regime-Filter: VIX {vix_level:.1f} = CAUTION (-1)")
        else:
            log.info(f"  Regime-Filter: VIX {vix_level:.1f} = OK")
    else:
        log.debug("  Regime-Filter: VIX nicht verfuegbar")

    # --- Fear & Greed Bewertung ---
    fg_value = ctx.get("fear_greed_index")
    if fg_value is not None:
        if fg_value < fg_crisis:
            combined_score -= 2
            details.append(f"F&G={fg_value} EXTREME FEAR (<{fg_crisis})")
            log.warning(f"  Regime-Filter: Fear&Greed {fg_value} = EXTREME FEAR (-2)")
        elif fg_value < fg_fear:
            combined_score -= 1
            details.append(f"F&G={fg_value} FEAR (<{fg_fear})")
            log.info(f"  Regime-Filter: Fear&Greed {fg_value} = FEAR (-1)")
        else:
            log.info(f"  Regime-Filter: Fear&Greed {fg_value} = OK")
    else:
        log.debug("  Regime-Filter: Fear&Greed nicht verfuegbar")

    # --- Brain Marktregime ---
    brain_state = load_json("brain_state.json") or {}
    brain_regime = brain_state.get("market_regime", "unknown")
    regime_data["brain_regime"] = brain_regime

    if brain_regime == "bear":
        combined_score -= 1
        details.append(f"Brain-Regime={brain_regime} (-1)")
        log.info(f"  Regime-Filter: Brain-Regime = bear (-1)")
    else:
        log.info(f"  Regime-Filter: Brain-Regime = {brain_regime}")

    # --- Ergebnis ---
    regime_data["combined_score"] = combined_score
    regime_data["details"] = details

    buy_allowed = combined_score > score_threshold
    if buy_allowed:
        reason = f"Regime OK (Score={combined_score}, Threshold={score_threshold})"
        log.info(f"  Regime-Filter: {reason}")
    else:
        reason = (f"Score {combined_score} <= {score_threshold}: "
                  f"{'; '.join(details)}")
        log.warning(f"  Regime-Filter BLOCK: {reason}")

    return buy_allowed, reason, regime_data
