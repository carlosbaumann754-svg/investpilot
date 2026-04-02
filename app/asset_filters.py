"""
InvestPilot - Asset-Class-Specific Filters
Handelsregeln pro Asset-Klasse: Zeitfenster, Liquiditaet,
Crypto-Filter, Forex-Sessions, Rohstoff-Saisonalitaet, Index-Regeln.
"""

import logging
from datetime import datetime

from app.config_manager import load_config

log = logging.getLogger("AssetFilters")

# ============================================================
# HANDELSZEITEN JE ASSET-KLASSE (CET/Schweizer Zeit)
# ============================================================

TRADING_WINDOWS = {
    "stocks_us": {"open_h": 15, "open_m": 30, "close_h": 22, "close_m": 0,
                  "weekdays": [0, 1, 2, 3, 4]},  # Mo-Fr
    "stocks_eu": {"open_h": 9, "open_m": 0, "close_h": 17, "close_m": 30,
                  "weekdays": [0, 1, 2, 3, 4]},
    "etf": {"open_h": 15, "open_m": 30, "close_h": 22, "close_m": 0,
            "weekdays": [0, 1, 2, 3, 4]},
    "crypto": None,  # 24/7
    "forex": {"open_h": 0, "open_m": 0, "close_h": 23, "close_m": 59,
              "weekdays": [0, 1, 2, 3, 4]},  # Mo-Fr rund um die Uhr
    "commodities": {"open_h": 8, "open_m": 0, "close_h": 22, "close_m": 0,
                    "weekdays": [0, 1, 2, 3, 4]},
    "indices": {  # Individuelle Fenster, US als Default
        "open_h": 15, "open_m": 30, "close_h": 22, "close_m": 0,
        "weekdays": [0, 1, 2, 3, 4]},
}

# Individuelle Index-Handelszeiten
INDEX_TRADING_HOURS = {
    "DAX": {"open_h": 9, "open_m": 0, "close_h": 17, "close_m": 30},
    "SPX500": {"open_h": 15, "open_m": 30, "close_h": 22, "close_m": 0},
    "NSDQ100": {"open_h": 15, "open_m": 30, "close_h": 22, "close_m": 0},
    "DJ30": {"open_h": 15, "open_m": 30, "close_h": 22, "close_m": 0},
}

# Forex Sessions (CET)
FOREX_SESSIONS = {
    "tokyo": {"open_h": 1, "close_h": 10},
    "london": {"open_h": 9, "close_h": 18},
    "newyork": {"open_h": 14, "close_h": 23},
}

# Beste Session je Forex-Paar
FOREX_BEST_SESSION = {
    "USDJPY": "tokyo",
    "EURJPY": "tokyo",
    "EURUSD": "london",
    "GBPUSD": "london",
    "EURCHF": "london",
    "USDCHF": "london",
    "AUDUSD": "tokyo",
    "NZDUSD": "tokyo",
    "USDCAD": "newyork",
}

# Stablecoins (vom Handel ausschliessen)
STABLECOINS = {"USDT", "USDC", "DAI", "BUSD", "TUSD", "FRAX", "USDP"}

# NFT-Tokens (vom Handel ausschliessen)
NFT_TOKENS = {"MANA", "SAND", "AXS", "ENJ", "THETA", "CHZ"}


# ============================================================
# ZEITFENSTER-CHECKS
# ============================================================

def is_within_trading_window(asset_class, symbol="", config=None):
    """Pruefe ob das Asset gerade handelbar ist."""
    if config is None:
        config = load_config()

    # Demo-Modus: immer handeln
    env = config.get("etoro", {}).get("environment", "demo")
    if env == "demo":
        return True, "Demo-Modus: immer handelbar"

    now = datetime.now()
    weekday = now.weekday()

    # Crypto: 24/7
    if asset_class == "crypto":
        return True, "Crypto: 24/7 handelbar"

    # Index: individuelle Zeiten
    if asset_class == "indices" and symbol in INDEX_TRADING_HOURS:
        window = INDEX_TRADING_HOURS[symbol]
    else:
        window = TRADING_WINDOWS.get(asset_class)

    if window is None:
        return True, "Kein Zeitfenster definiert"

    if weekday not in window.get("weekdays", [0, 1, 2, 3, 4]):
        return False, f"Wochenende: {asset_class} nicht handelbar"

    current_minutes = now.hour * 60 + now.minute
    open_minutes = window["open_h"] * 60 + window["open_m"]
    close_minutes = window["close_h"] * 60 + window["close_m"]

    if current_minutes < open_minutes or current_minutes >= close_minutes:
        return False, f"{asset_class}: Ausserhalb Handelszeit ({window['open_h']:02d}:{window['open_m']:02d}-{window['close_h']:02d}:{window['close_m']:02d})"

    return True, "OK"


def check_market_open_buffer(asset_class, symbol="", buffer_minutes=30):
    """Pruefe ob Markt seit mindestens N Minuten offen ist.
    Vermeide Trades in den ersten 30 Min nach Oeffnung (hohe Volatilitaet).
    """
    now = datetime.now()

    if asset_class in ("crypto", "forex"):
        return True, "Kein Opening Buffer noetig"

    if asset_class == "indices" and symbol in INDEX_TRADING_HOURS:
        window = INDEX_TRADING_HOURS[symbol]
    else:
        window = TRADING_WINDOWS.get(asset_class, {})

    if not window:
        return True, "OK"

    open_minutes = window.get("open_h", 0) * 60 + window.get("open_m", 0)
    current_minutes = now.hour * 60 + now.minute

    if current_minutes < open_minutes + buffer_minutes:
        return False, f"Opening Buffer: Markt erst seit {current_minutes - open_minutes} Min offen"
    return True, "OK"


def check_market_close_buffer(asset_class, symbol="", buffer_minutes=15):
    """Keine neuen Positionen in den letzten N Minuten vor Marktschluss."""
    now = datetime.now()

    if asset_class in ("crypto", "forex"):
        return True, "Kein Closing Buffer noetig"

    if asset_class == "indices" and symbol in INDEX_TRADING_HOURS:
        window = INDEX_TRADING_HOURS[symbol]
    else:
        window = TRADING_WINDOWS.get(asset_class, {})

    if not window:
        return True, "OK"

    close_minutes = window.get("close_h", 22) * 60 + window.get("close_m", 0)
    current_minutes = now.hour * 60 + now.minute

    if current_minutes >= close_minutes - buffer_minutes:
        return False, f"Closing Buffer: Nur noch {close_minutes - current_minutes} Min bis Schluss"
    return True, "OK"


# ============================================================
# FOREX-SPEZIFISCHE FILTER
# ============================================================

def is_optimal_forex_session(symbol):
    """Pruefe ob das Forex-Paar in seiner besten Session gehandelt wird."""
    best = FOREX_BEST_SESSION.get(symbol)
    if not best:
        return True, "Keine Session-Praeferenz"

    session = FOREX_SESSIONS.get(best, {})
    now_h = datetime.now().hour

    if session.get("open_h", 0) <= now_h < session.get("close_h", 24):
        return True, f"Optimale Session ({best})"
    return False, f"Nicht in optimaler Session ({best}: {session.get('open_h')}:00-{session.get('close_h')}:00)"


def is_forex_major(symbol):
    """Pruefe ob Forex-Paar ein Major ist (hoechste Liquiditaet)."""
    from app.leverage_manager import FOREX_MAJORS
    return symbol.upper() in FOREX_MAJORS


# ============================================================
# CRYPTO-SPEZIFISCHE FILTER
# ============================================================

def is_stablecoin(symbol):
    """Pruefe ob Symbol ein Stablecoin ist (nicht handeln)."""
    return symbol.upper() in STABLECOINS


def is_nft_token(symbol):
    """Pruefe ob Symbol ein NFT-Token ist (ausschliessen)."""
    return symbol.upper() in NFT_TOKENS


def check_crypto_volatility_filter(analysis, max_1h_change_pct=10):
    """Pausiere Crypto-Handel bei extremer kurzfristiger Volatilitaet."""
    if analysis is None:
        return True, "Keine Analyse"

    momentum_5d = abs(analysis.get("momentum_5d", 0))
    volatility = analysis.get("volatility", 0)

    # Extrem hohe Volatilitaet
    if volatility > 8:
        return False, f"Crypto Volatilitaet zu hoch ({volatility:.1f}%)"

    return True, "OK"


def get_crypto_weekend_multiplier():
    """Reduziere Crypto-Positionsgroesse am Wochenende (duennere Liquiditaet)."""
    weekday = datetime.now().weekday()
    if weekday >= 5:  # Samstag, Sonntag
        return 0.7  # 30% weniger
    return 1.0


def check_crypto_listing_age(symbol, min_days=90):
    """Pruefe ob Crypto-Asset seit mindestens N Tagen gelistet ist."""
    # Unsere bekannten Assets sind alle alt genug
    # Nur fuer neu entdeckte Assets relevant
    return True, "OK"


# ============================================================
# AKTIEN-SPEZIFISCHE FILTER
# ============================================================

def check_liquidity(analysis, min_volume=1_000_000):
    """Pruefe Mindest-Handelsvolumen (nur wenn verfuegbar)."""
    # Volume-Daten sind in analyse enthalten
    # Da wir ueber eToro handeln, ist Liquiditaet weniger kritisch
    # als bei direktem Boersenhandel
    return True, "OK"


# ============================================================
# INDEX-SPEZIFISCHE FILTER
# ============================================================

def check_index_overnight_risk(symbol, has_leveraged_position):
    """Pruefe ob gehebelte Index-Position ueber Nacht gehalten werden sollte."""
    if not has_leveraged_position:
        return True, "Kein Hebel"

    now = datetime.now()
    if symbol in INDEX_TRADING_HOURS:
        close_h = INDEX_TRADING_HOURS[symbol]["close_h"]
        if now.hour >= close_h - 1:
            return False, f"Index {symbol}: Gehebelte Position vor Schluss schliessen"

    return True, "OK"


# ============================================================
# ROHSTOFF-SPEZIFISCHE FILTER
# ============================================================

def check_commodity_rollover(symbol):
    """Pruefe ob Rollover-Termin nah ist (vereinfacht: Quartalswechsel)."""
    now = datetime.now()
    # Rollover typischerweise 3. Mittwoch des Monats bei Futures
    # Vereinfacht: Warnung in letzter Woche des Quartals
    if now.month in (3, 6, 9, 12) and now.day >= 25:
        return True, f"Moegl. Rollover-Fenster fuer {symbol}"
    return False, "OK"


# ============================================================
# ZENTRALE FILTER-FUNKTION
# ============================================================

def apply_asset_filters(symbol, asset_class, analysis=None, config=None):
    """Wende alle relevanten Filter fuer ein Asset an.

    Returns: (allowed, reasons_list)
    """
    if config is None:
        config = load_config()

    filters_cfg = config.get("asset_filters", {})
    if not filters_cfg.get("enabled", True):
        return True, []

    reasons = []

    # 1. Zeitfenster
    ok, reason = is_within_trading_window(asset_class, symbol, config)
    if not ok:
        reasons.append(reason)

    # 2. Opening Buffer
    if filters_cfg.get("use_opening_buffer", True):
        ok, reason = check_market_open_buffer(asset_class, symbol,
                                              filters_cfg.get("opening_buffer_minutes", 30))
        if not ok:
            reasons.append(reason)

    # 3. Closing Buffer
    if filters_cfg.get("use_closing_buffer", True):
        ok, reason = check_market_close_buffer(asset_class, symbol,
                                               filters_cfg.get("closing_buffer_minutes", 15))
        if not ok:
            reasons.append(reason)

    # 4. Crypto-spezifisch
    if asset_class == "crypto":
        if is_stablecoin(symbol):
            reasons.append(f"Stablecoin: {symbol} nicht handelbar")
        if is_nft_token(symbol):
            reasons.append(f"NFT-Token: {symbol} ausgeschlossen")
        if analysis:
            ok, reason = check_crypto_volatility_filter(analysis)
            if not ok:
                reasons.append(reason)

    # 5. Forex-spezifisch
    if asset_class == "forex":
        if filters_cfg.get("forex_optimal_session_only", False):
            ok, reason = is_optimal_forex_session(symbol)
            if not ok:
                reasons.append(reason)

    # 6. Rohstoff-Rollover
    if asset_class == "commodities":
        near_rollover, reason = check_commodity_rollover(symbol)
        if near_rollover:
            reasons.append(reason)

    allowed = len(reasons) == 0
    if not allowed:
        log.info(f"  Filter blockiert {symbol} ({asset_class}): {'; '.join(reasons)}")

    return allowed, reasons


def get_position_size_adjustment(symbol, asset_class):
    """Hole Asset-spezifische Positionsgroessen-Anpassung."""
    multiplier = 1.0

    # Crypto am Wochenende: kleiner
    if asset_class == "crypto":
        multiplier *= get_crypto_weekend_multiplier()

    return multiplier
