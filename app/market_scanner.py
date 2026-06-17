"""
InvestPilot - Market Scanner
Scannt alle verfuegbaren Asset-Klassen und bewertet Opportunities.
Nutzt yfinance fuer technische Analyse, eToro API fuer Instrument Discovery.
"""

import logging
import time
from datetime import datetime
from typing import Optional

log = logging.getLogger("Scanner")


# v37h+3 (Sprint-Tag-9, 19.05.2026): Audit-Coverage-Marker
AUDIT_METADATA = {
    "purpose": "Market-Scanner: ASSET_UNIVERSE-Iteration, Score-Computation, Signal-Klassifikation, expand_symbol_for_match (Bot<->IBKR)",
    "config_section": "scanner",
    "state_files": ["scanner_state.json"],
    "self_tests": ["tc_yfinance_freshness"],
    "scheduler_hooks": [],
    "health_check": None,
    "added_in": "v1 (Foundation), v37de Symbol-Mapping",
}

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
    # --- US Large Cap Tech ---
    "AAPL":  {"internal_id": 6408,  "yf": "AAPL",  "class": "stocks",  "name": "Apple",     "sector": "tech"},
    "MSFT":  {"internal_id": 1139,  "yf": "MSFT",  "class": "stocks",  "name": "Microsoft", "sector": "tech"},
    "GOOGL": {"internal_id": 1002,  "yf": "GOOGL", "class": "stocks",  "name": "Alphabet",  "sector": "tech"},
    "AMZN":  {"internal_id": 1026,  "yf": "AMZN",  "class": "stocks",  "name": "Amazon",    "sector": "consumer"},
    "TSLA":  {"internal_id": 1397,  "yf": "TSLA",  "class": "stocks",  "name": "Tesla",     "sector": "growth"},
    "NVDA":  {"internal_id": 1518,  "yf": "NVDA",  "class": "stocks",  "name": "NVIDIA",    "sector": "tech"},
    "META":  {"internal_id": 10548, "yf": "META",  "class": "stocks",  "name": "Meta",      "sector": "tech"},
    "NFLX":  {"internal_id": 1049,  "yf": "NFLX",  "class": "stocks",  "name": "Netflix",   "sector": "tech"},
    "AMD":   {"internal_id": 1004,  "yf": "AMD",   "class": "stocks",  "name": "AMD",       "sector": "tech"},
    "INTC":  {"internal_id": 1033,  "yf": "INTC",  "class": "stocks",  "name": "Intel",     "sector": "tech"},
    # --- US Finance & Health ---
    "JPM":   {"internal_id": 1036,  "yf": "JPM",   "class": "stocks",  "name": "JPMorgan",            "sector": "finance"},
    "V":     {"internal_id": 1180,  "yf": "V",     "class": "stocks",  "name": "Visa",                "sector": "finance"},
    "MA":    {"internal_id": 1089,  "yf": "MA",    "class": "stocks",  "name": "Mastercard",           "sector": "finance"},
    "UNH":   {"internal_id": 1166,  "yf": "UNH",   "class": "stocks",  "name": "UnitedHealth",        "sector": "health"},
    "JNJ":   {"internal_id": 1035,  "yf": "JNJ",   "class": "stocks",  "name": "Johnson & Johnson",   "sector": "health"},
    "PFE":   {"internal_id": 1094,  "yf": "PFE",   "class": "stocks",  "name": "Pfizer",              "sector": "health"},
    # --- US Consumer & Industrial ---
    "KO":    {"internal_id": 1038,  "yf": "KO",    "class": "stocks",  "name": "Coca-Cola",           "sector": "consumer"},
    "PG":    {"internal_id": 1096,  "yf": "PG",    "class": "stocks",  "name": "Procter & Gamble",    "sector": "consumer"},
    "DIS":   {"internal_id": 1024,  "yf": "DIS",   "class": "stocks",  "name": "Disney",              "sector": "consumer"},
    "BA":    {"internal_id": 1008,  "yf": "BA",    "class": "stocks",  "name": "Boeing",              "sector": "consumer"},
    "NKE":   {"internal_id": 1053,  "yf": "NKE",   "class": "stocks",  "name": "Nike",                "sector": "consumer"},
    "MCD":   {"internal_id": 1044,  "yf": "MCD",   "class": "stocks",  "name": "McDonald's",          "sector": "consumer"},
    # --- US Growth / Tech ---
    "PYPL":  {"internal_id": 5765,  "yf": "PYPL",  "class": "stocks",  "name": "PayPal",       "sector": "growth"},
    "SQ":    {"internal_id": 7961,  "yf": "XYZ",   "class": "stocks",  "name": "Block (formerly Square)","sector": "growth",
              # R-B6 (02.06.2026): SQ->XYZ Rename (Block, Jan 2024) war nur datenseitig (yf) migriert. Der IBKR-Resolver
              # nutzte den Universe-Key 'SQ' -> Stock('SQ','SMART') -> IBKR Error 200, jeder STRONG_BUY scheiterte.
              # ibkr_override setzt das echte IBKR-Ticker XYZ (gleicher Mechanismus wie GOLD->GLD).
              "ibkr_override": {"symbol": "XYZ", "secType": "STK", "exchange": "SMART"}},
    "SHOP":  {"internal_id": 7905,  "yf": "SHOP",  "class": "stocks",  "name": "Shopify",      "sector": "growth"},
    "UBER":  {"internal_id": 14066, "yf": "UBER",  "class": "stocks",  "name": "Uber",         "sector": "growth"},
    "COIN":  {"internal_id": 18001, "yf": "COIN",  "class": "stocks",  "name": "Coinbase",     "sector": "crypto_major"},
    "PLTR":  {"internal_id": 17181, "yf": "PLTR",  "class": "stocks",  "name": "Palantir",     "sector": "tech"},
    "SNAP":  {"internal_id": 8014,  "yf": "SNAP",  "class": "stocks",  "name": "Snap",         "sector": "tech"},
    "ROKU":  {"internal_id": 8150,  "yf": "ROKU",  "class": "stocks",  "name": "Roku",         "sector": "tech"},
    "CRM":   {"internal_id": 1021,  "yf": "CRM",   "class": "stocks",  "name": "Salesforce",   "sector": "tech"},
    "ADBE":  {"internal_id": 1003,  "yf": "ADBE",  "class": "stocks",  "name": "Adobe",        "sector": "tech"},
    # --- EU Stocks ---
    "SAP":   {"internal_id": 1341,  "yf": "SAP",   "class": "stocks",  "name": "SAP",    "sector": "tech"},
    "ASML":  {"internal_id": 5523,  "yf": "ASML",  "class": "stocks",  "name": "ASML",   "sector": "tech"},
    "LVMH":  {"internal_id": 1350,  "yf": "MC.PA", "class": "stocks",  "name": "LVMH",   "sector": "consumer"},
    # --- ETFs ---
    "SPY":   {"internal_id": 1116,  "yf": "SPY",   "class": "etf",     "name": "S&P 500 ETF",        "sector": "broad_market"},
    "QQQ":   {"internal_id": 1321,  "yf": "QQQ",   "class": "etf",     "name": "NASDAQ 100 ETF",     "sector": "tech"},
    "IWM":   {"internal_id": 1108,  "yf": "IWM",   "class": "etf",     "name": "Russell 2000 ETF",   "sector": "broad_market"},
    "DIA":   {"internal_id": 1101,  "yf": "DIA",   "class": "etf",     "name": "Dow Jones ETF",      "sector": "broad_market"},
    "XLK":   {"internal_id": 1125,  "yf": "XLK",   "class": "etf",     "name": "Technology ETF",     "sector": "tech"},
    "XLF":   {"internal_id": 1123,  "yf": "XLF",   "class": "etf",     "name": "Financial ETF",      "sector": "finance"},
    "XLE":   {"internal_id": 1122,  "yf": "XLE",   "class": "etf",     "name": "Energy ETF",         "sector": "energy"},
    "GLD":   {"internal_id": 1105,  "yf": "GLD",   "class": "etf",     "name": "Gold ETF",           "sector": "commodities"},
    "SLV":   {"internal_id": 1115,  "yf": "SLV",   "class": "etf",     "name": "Silver ETF",         "sector": "commodities"},
    "TLT":   {"internal_id": 1120,  "yf": "TLT",   "class": "etf",     "name": "20Y Treasury ETF",   "sector": "bonds"},
    "EEM":   {"internal_id": 1102,  "yf": "EEM",   "class": "etf",     "name": "Emerging Markets ETF","sector": "broad_market"},
    "VNQ":   {"internal_id": 1127,  "yf": "VNQ",   "class": "etf",     "name": "Real Estate ETF",    "sector": "real_estate"},
    # --- Crypto ---
    "BTC":   {"internal_id": 100000, "yf": "BTC-USD",  "class": "crypto", "name": "Bitcoin",    "sector": "crypto_major"},
    "ETH":   {"internal_id": 100001, "yf": "ETH-USD",  "class": "crypto", "name": "Ethereum",   "sector": "crypto_major"},
    "XRP":   {"internal_id": 100004, "yf": "XRP-USD",  "class": "crypto", "name": "Ripple",     "sector": "crypto_alt"},
    "ADA":   {"internal_id": 100044, "yf": "ADA-USD",  "class": "crypto", "name": "Cardano",    "sector": "crypto_alt"},
    "SOL":   {"internal_id": 100077, "yf": "SOL-USD",  "class": "crypto", "name": "Solana",     "sector": "crypto_alt"},
    "DOGE":  {"internal_id": 100060, "yf": "DOGE-USD", "class": "crypto", "name": "Dogecoin",   "sector": "crypto_alt"},
    "DOT":   {"internal_id": 100063, "yf": "DOT-USD",  "class": "crypto", "name": "Polkadot",   "sector": "crypto_alt"},
    "AVAX":  {"internal_id": 100072, "yf": "AVAX-USD", "class": "crypto", "name": "Avalanche",  "sector": "crypto_alt"},
    "LINK":  {"internal_id": 100010, "yf": "LINK-USD", "class": "crypto", "name": "Chainlink",  "sector": "crypto_alt"},
    # v36i: MATIC entfernt — Polygon Token-Migration Sept 2024 (MATIC -> POL),
    # nach Migration ist das Asset auf Yahoo Finance nicht mehr abrufbar
    # (POL-USD / POL / POLYGON-USD alle EMPTY). Falls Polygon zurueck
    # gewollt: alternative Datenquelle (CoinGecko/CMC API) implementieren
    # oder ETH-Layer-2 Proxy nutzen.
    # --- Commodities (via eToro CFDs auf eToro; via ETF-Proxies auf IBKR) ---
    # v36d (28.04.2026): ibkr_override Feld ergaenzt. eToro tradet direkt CFD,
    # IBKR Paper hat keine CMDTY/NYMEX-Definition fuer diese Symbole — daher
    # routen wir bei IBKR ueber liquide ETFs auf ARCA (gleiche Asset-Exposure,
    # echte Settlement, kein Futures-Roll-Risiko fuer Bot-Position-Sizing).
    # yfinance-Symbol bleibt das Future fuer Technische Analyse.
    "GOLD":  {"internal_id": 5002,  "yf": "GC=F",   "class": "commodities", "name": "Gold",
              "ibkr_override": {"symbol": "GLD",  "secType": "STK", "exchange": "ARCA",
                                "name": "SPDR Gold Trust"}},
    "SILVER":{"internal_id": 5003,  "yf": "SI=F",   "class": "commodities", "name": "Silver",
              "ibkr_override": {"symbol": "SLV",  "secType": "STK", "exchange": "ARCA",
                                "name": "iShares Silver Trust"}},
    "OIL":   {"internal_id": 5001,  "yf": "CL=F",   "class": "commodities", "name": "Crude Oil",
              "ibkr_override": {"symbol": "USO",  "secType": "STK", "exchange": "ARCA",
                                "name": "United States Oil Fund"}},
    "NGAS":  {"internal_id": 5007,  "yf": "NG=F",   "class": "commodities", "name": "Natural Gas",
              "ibkr_override": {"symbol": "UNG",  "secType": "STK", "exchange": "ARCA",
                                "name": "United States Natural Gas Fund"}},
    "COPPER":{"internal_id": 5009,  "yf": "HG=F",   "class": "commodities", "name": "Copper",
              "ibkr_override": {"symbol": "CPER", "secType": "STK", "exchange": "ARCA",
                                "name": "United States Copper Index Fund"}},
    # --- Forex ---
    "EURUSD":{"internal_id": 1,     "yf": "EURUSD=X","class": "forex", "name": "EUR/USD"},
    "GBPUSD":{"internal_id": 2,     "yf": "GBPUSD=X","class": "forex", "name": "GBP/USD"},
    "USDJPY":{"internal_id": 3,     "yf": "JPY=X",   "class": "forex", "name": "USD/JPY"},
    "USDCHF":{"internal_id": 4,     "yf": "CHF=X",   "class": "forex", "name": "USD/CHF"},
    "AUDUSD":{"internal_id": 5,     "yf": "AUDUSD=X","class": "forex", "name": "AUD/USD"},
    # --- Indices (CFDs) ---
    "SPX500": {"internal_id": 10136, "yf": "^GSPC",  "class": "indices", "name": "S&P 500"},
    "NSDQ100":{"internal_id": 10137, "yf": "^IXIC",  "class": "indices", "name": "NASDAQ 100"},
    "DJ30":   {"internal_id": 10138, "yf": "^DJI",   "class": "indices", "name": "Dow Jones 30"},
    "DAX":    {"internal_id": 10141, "yf": "^GDAXI", "class": "indices", "name": "DAX 40"},
}


# ============================================================
# R-B1 (29.05.2026 Soak-Phase): Broker-ID-Abstraktion (Phase 1 Foundation)
# ============================================================
# Migration-Schuld aus eToro-Aera: das ASSET_UNIVERSE traegt eine "etoro_id",
# obwohl der Bot seit v36 ausschliesslich ueber IBKR handelt. Die etoro_id
# wird intern als instrument_id durchgereicht (client.buy, trade_history,
# Cooldown-Keys, Position-Matching).
#
# R-B1 Ziel: Symbol als logischer Primary-Key, Broker-IDs als Adapter-Layer.
# PHASE 1 (hier, ADDITIV, zero behavior change): zentrale Mapping-Helfer
# einfuehren OHNE bestehende Aufrufer zu aendern. Spaetere Phasen migrieren
# Call-Sites schrittweise auf symbol-basierte Lookups.
#
# WICHTIG: Die Werte (Zahlen) bleiben identisch (AAPL bleibt 6408) → KEINE
# Migration persistierter Daten (trade_history instrument_id, Cooldown-Keys
# bleiben gueltig). Nur die *konzeptionelle* Quelle wird ein Adapter-Layer.
#
# 'internal_id' ist die semantisch korrekte Bezeichnung (stabiler interner
# Identifier, historisch == etoro_id). Naming-Cleanup etoro_id->internal_id
# folgt in spaeterer Phase mit Backward-Compat.

# Sentinel fuer Assets ohne bekannte Broker-ID (R-A46: SPOT/MRVL/STX/MU)
NO_INSTRUMENT_ID = -1


# ============================================================
# R-B1 Phase 4 (29.05.2026 Soak): Naming-Cleanup etoro_id -> internal_id
# ============================================================
# Strangler-Transition: Entries tragen BEIDE Keys (internal_id + etoro_id,
# gleicher Wert). Neuer Code liest internal_id (via _meta_internal_id),
# alter Code liest weiter etoro_id. So ist das System an JEDEM Migrations-
# Zwischenstand lauffaehig (kein halb-fertiger-Rename-Bug). etoro_id wird
# in finaler Phase entfernt wenn alle 136 Read-Sites migriert sind.
# 'internal_id' = semantisch korrekt (stabiler interner Identifier, NICHT
# eToro-spezifisch; Bot handelt rein ueber IBKR seit v36).

def _meta_internal_id(meta: dict):
    """R-B1 Phase 4: Single-Source-Accessor fuer die interne Instrument-ID
    aus einem ASSET_UNIVERSE-Meta-Dict. Liest internal_id, faellt auf
    etoro_id zurueck (Transition + persistierte Legacy-Entries). R-A46-
    defensive: None/0/Falsy -> NO_INSTRUMENT_ID."""
    if not meta:
        return NO_INSTRUMENT_ID
    return int(meta.get("internal_id") or meta.get("etoro_id") or NO_INSTRUMENT_ID)


def _ensure_internal_ids(universe: dict = None) -> int:
    """R-B1 Phase 4: BIDIREKTIONALER Dual-Key-Shim. Stellt sicher dass jeder
    Entry BEIDE Keys hat (internal_id + etoro_id, gleicher Wert) — kopiert
    den jeweils fehlenden vom vorhandenen.

    Phase 4c: Die ASSET_UNIVERSE-Literale wurden auf 'internal_id' umbenannt
    (Domain-Modell sauber von eToro-Naming). Dieser Shim re-ergaenzt etoro_id
    zur Laufzeit, damit die ~130 Legacy-Reader (meta.get("etoro_id"),
    candidate["etoro_id"], ...) + persistierte Daten OHNE Migration weiter
    funktionieren. Transparente Backward-Compat-Maschinerie, kein Naming-Leak
    im Quell-Domain-Modell. Idempotent. Returns Anzahl ergaenzter Keys."""
    u = universe if universe is not None else ASSET_UNIVERSE
    added = 0
    for meta in u.values():
        has_int = "internal_id" in meta
        has_eto = "etoro_id" in meta
        if has_int and not has_eto:
            meta["etoro_id"] = meta["internal_id"]  # Compat-Rueck-Ergaenzung
            added += 1
        elif has_eto and not has_int:
            meta["internal_id"] = meta["etoro_id"]
            added += 1
    return added


# Direkt nach dem Literal anwenden (Discovery-Boot-Merge ruft es erneut).
_ensure_internal_ids(ASSET_UNIVERSE)


def instrument_id_for_symbol(symbol: str):
    """R-B1: Liefert die interne instrument_id (internal_id, historisch
    etoro_id) fuer ein Bot-Symbol. Liest live aus ASSET_UNIVERSE (inkl.
    Boot-gemergte Discoveries).

    Returns int instrument_id, oder NO_INSTRUMENT_ID (-1) wenn unbekannt/keine
    Broker-ID (Discovery-Assets ohne eToro-ID). Defensive gegen None (R-A46).
    """
    meta = ASSET_UNIVERSE.get(symbol)
    if not meta:
        # v38: sp600-Signal-Stack-Universum (additiv, dormant wenn use_signal_stack AUS)
        try:
            from app import sp600_universe
            sid = sp600_universe.symbol_to_id().get(symbol)
            if sid:
                return sid
        except Exception:
            pass
        return NO_INSTRUMENT_ID
    return _meta_internal_id(meta)


def symbol_for_instrument_id(instrument_id):
    """R-B1: Reverse-Lookup interne instrument_id -> Bot-Symbol.

    Ermoeglicht symbol-basierte Logik OHNE persistierte instrument_ids zu
    migrieren (alte trade_history/Cooldown-Eintraege mit etoro_id-Werten
    bleiben aufloesbar). Returns symbol-str oder None wenn kein Match.

    Sentinel-IDs (-1) werden NICHT gematcht (mehrere Discovery-Assets teilen
    -1 → kein eindeutiges Symbol).
    """
    try:
        iid = int(instrument_id)
    except (TypeError, ValueError):
        return None
    if iid == NO_INSTRUMENT_ID:
        return None
    # v38: sp600 synthetische IDs (>900000) -> direkter Reverse-Lookup
    try:
        from app import sp600_universe
        if sp600_universe.is_sp600_id(iid):
            return sp600_universe.id_to_symbol().get(iid)
    except Exception:
        pass
    for sym, meta in ASSET_UNIVERSE.items():
        if _meta_internal_id(meta) == iid:
            return sym
    return None


def build_broker_id_map() -> dict:
    """R-B1: Adapter-Map Symbol -> interne instrument_id, live aus
    ASSET_UNIVERSE abgeleitet (inkl. Discoveries). Single-Source-of-Truth
    bleibt ASSET_UNIVERSE; diese Map ist die broker-agnostische Sicht darauf.

    Zukunft (spaetere Phase): zusaetzliche Broker (ibkr conId) als weitere
    Map-Ebene, dann ist der Bot vollstaendig broker-agnostisch.
    """
    return {
        sym: _meta_internal_id(meta)
        for sym, meta in ASSET_UNIVERSE.items()
    }


# ============================================================
# SYMBOL-MAPPING — Bot-internal <-> IBKR-Ticker (v37de)
# ============================================================
# Bot's Universum nutzt eigene Symbol-Names (z.B. SILVER, GOLD, OIL).
# IBKR-API nutzt ETF-Tickers (SLV, GLD, USO) via `ibkr_override`-Feld
# in ASSET_UNIVERSE. Reconcile-Code, Position-Tracking und Trade-History
# muessen zwischen beiden Welten transparent uebersetzen.
#
# Hintergrund: 06.05.2026 Pushover-Spam SLV PHANTOM_POSITION/MISSED_FILL
# weil Bot-Log "SILVER" nicht mit IBKR-Position "SLV" matched. CRIT-
# Cutover-Blocker behoben mit dieser zentralen Translation-Layer.

def bot_symbol_to_ibkr_ticker(bot_symbol: str) -> str:
    """Bot-internal Symbol -> IBKR-Ticker (z.B. SILVER -> SLV).

    Wenn kein ibkr_override existiert: returnt Bot-Symbol unveraendert
    (z.B. AAPL, TSLA — gleiche Sprache fuer Stocks/ETFs).
    """
    if not bot_symbol:
        return bot_symbol
    info = ASSET_UNIVERSE.get(bot_symbol)
    if info:
        override = info.get("ibkr_override") or {}
        if override.get("symbol"):
            return override["symbol"]
    return bot_symbol


def ibkr_ticker_to_bot_symbol(ibkr_ticker: str) -> str:
    """IBKR-Ticker -> Bot-internal Symbol (z.B. SLV -> SILVER).

    Reverse-Lookup ueber ibkr_override-Map. Wenn IBKR-Ticker = Bot-Symbol
    (z.B. AAPL): returnt unveraendert.

    Bei mehreren Bot-Symbols die auf gleichen IBKR-Ticker zeigen (sollte
    nicht vorkommen): nimmt ersten Match.
    """
    if not ibkr_ticker:
        return ibkr_ticker
    # Erst direkter Match (Bot-Symbol = IBKR-Ticker)
    if ibkr_ticker in ASSET_UNIVERSE:
        return ibkr_ticker
    # Reverse-Lookup ueber ibkr_override
    for bot_sym, info in ASSET_UNIVERSE.items():
        override = info.get("ibkr_override") or {}
        if override.get("symbol") == ibkr_ticker:
            return bot_sym
    return ibkr_ticker  # Fallback: unmodifiziert


def get_asset_class_for_instrument_id(instrument_id) -> Optional[str]:
    """v37h Task 1 (09.05.2026): Asset-Class-Lookup ueber etoro_id.

    Returnt z.B. "stocks", "etf", "commodities", "crypto", "forex" — oder
    None wenn instrument_id nicht im Universe ist.

    Wird in IbkrBroker._place_market_order benutzt um per-Asset-Class
    Slippage + Trading-Hours zu konfigurieren (asset_class_settings in
    config.json).

    Hintergrund: Stress-Test 08.05. zeigte Cluster bei Commodity-ETFs
    (CPER, SLV) die in Pre-Market-Hours mehrfach failed/cancelled wurden.
    Liquiditaet + Spread sind Asset-Class-Eigenschaften, nicht symbol-
    spezifisch. Asset-Class-Settings = saubere Cross-Cutting-Concern.
    """
    try:
        iid = int(instrument_id)
    except (ValueError, TypeError):
        return None
    for info in ASSET_UNIVERSE.values():
        if _meta_internal_id(info) == iid:  # R-B1 Phase 4c
            return info.get("class")
    return None


def get_asset_class_for_symbol(bot_symbol: str) -> Optional[str]:
    """Asset-Class-Lookup via Bot-Symbol (Pendant zu instrument_id-Variante).

    Nuetzlich fuer Pfade die Symbol-basiert arbeiten (z.B. Strategy-Code).

    v37h Fix (10.05.2026): Reverse-Lookup ueber ibkr_override.symbol falls
    direkter Key nicht gefunden. Beispiel: 'CPER' ist kein ASSET_UNIVERSE-
    Key, sondern ibkr_override.symbol von 'COPPER'. Vorher returnte das
    None -> Earnings-Filter griff nicht -> yfinance 404 ERROR-Spam fuer
    Commodity-ETF-Tickers. Jetzt: 'CPER' -> COPPER's class = 'commodities'.
    """
    if not bot_symbol:
        return None
    # Direkter Lookup
    info = ASSET_UNIVERSE.get(bot_symbol)
    if info:
        return info.get("class")
    # Reverse-Lookup: ist symbol ein ibkr_override.symbol?
    for meta in ASSET_UNIVERSE.values():
        override = meta.get("ibkr_override") or {}
        if override.get("symbol") == bot_symbol:
            return meta.get("class")
    return None


def expand_symbol_for_match(symbol: str) -> set[str]:
    """Liefert ALLE Symbol-Variants fuer Match-Sets (Bot + IBKR).

    Beispiele:
      'SILVER' -> {'SILVER', 'SLV'}     (SILVER hat ibkr_override.symbol=SLV)
      'SLV'    -> {'SILVER', 'SLV'}     (Reverse: jemand zeigt mit Override hin)
      'AAPL'   -> {'AAPL'}              (kein Override, kein Reverse)

    Wird im Reconcile-Code genutzt um beide Symbol-Welten gleichzeitig
    zu matchen (statt zu raten welche Seite die "richtige" ist).
    """
    if not symbol:
        return set()
    s = {symbol}
    # Vorwaerts: wenn `symbol` ein Bot-Universum-Name mit ibkr_override ist
    s.add(bot_symbol_to_ibkr_ticker(symbol))
    # Rueckwaerts: alle Bot-Symbols deren ibkr_override auf `symbol` zeigt
    for bot_sym, info in ASSET_UNIVERSE.items():
        override = info.get("ibkr_override") or {}
        if override.get("symbol") == symbol:
            s.add(bot_sym)
    return s


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


# --- R-B3 (01.06.2026): Fonds-/Nicht-Handels-Filter -----------------------
# yfinance liefert pro Symbol einen instrumentType. Boersengehandelte Klassen
# (EQUITY/ETF/CRYPTOCURRENCY/CURRENCY/FUTURE) sind handelbar; MUTUALFUND/INDEX/
# MONEYMARKET nicht (nur Tages-NAV bzw. kein Intraday) -> gehoeren nie ins
# Trading-Universum. VITAX (Vanguard Mutual Fund) loeste 15x/48h "possibly
# delisted"-Fehler aus, weil die Discovery ihn einsammelte. analyze_single_asset
# ist der gemeinsame Chokepoint von Discovery UND Live-Scanner -> ein Filter
# hier wirkt an beiden Stellen.
_NON_TRADEABLE_INSTRUMENT_TYPES = {"MUTUALFUND", "INDEX", "MONEYMARKET"}


def _instrument_type(ticker):
    """yfinance-instrumentType aus den History-Metadaten (gratis — von
    ticker.history() gefuellt, kein Extra-Call). Fail-open: bei Unsicherheit
    None, damit handelbare Assets nie faelschlich gefiltert werden."""
    try:
        meta = getattr(ticker, "history_metadata", None)
        if isinstance(meta, dict):
            t = meta.get("instrumentType")
            return t.upper() if isinstance(t, str) else None
    except Exception:
        pass
    return None


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

        # R-B3: Nicht-handelbare Instrumente (Fonds/Index) ausfiltern.
        # Mutual Funds bestehen den Tages-History-Check oben (NAV-Daten),
        # scheitern aber im Live-Intraday-Scan -> hier sauber abfangen.
        itype = _instrument_type(ticker)
        if itype in _NON_TRADEABLE_INSTRUMENT_TYPES:
            log.info(
                f"  {symbol} ({yf_symbol}): Instrument-Typ '{itype}' nicht "
                f"boersenhandelbar -> uebersprungen (R-B3 Fonds-Filter)"
            )
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

        # v5 Features: ATR, ADX, OBV, VWAP
        highs = hist["High"].tolist() if "High" in hist.columns else closes
        lows = hist["Low"].tolist() if "Low" in hist.columns else closes
        from app.ml_scorer import _calc_atr, _calc_adx, _calc_obv_slope
        atr_pct = _calc_atr(highs, lows, closes)
        adx = _calc_adx(highs, lows, closes)
        obv_slope = _calc_obv_slope(closes, volumes)

        # VWAP deviation
        vwap_deviation_pct = 0
        if len(highs) >= 20 and sum(volumes[-20:]) > 0:
            typical = [(h + l + c) / 3 for h, l, c in
                       zip(highs[-20:], lows[-20:], closes[-20:])]
            vols_20 = volumes[-20:]
            vwap = sum(t * v for t, v in zip(typical, vols_20)) / sum(vols_20)
            if vwap > 0:
                vwap_deviation_pct = (current_price - vwap) / vwap * 100

        return {
            "symbol": symbol,
            "name": asset_info["name"],
            "class": asset_info["class"],
            # R-B1 Phase 4c: internal_id (kanonisch) + etoro_id (Dual-Key,
            # entfernt in 4d). Beide via Accessor -> robust gegen Literal-Rename.
            "internal_id": _meta_internal_id(asset_info),
            "etoro_id": _meta_internal_id(asset_info),
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
            "atr_pct": round(atr_pct, 2),
            "adx": round(adx, 1),
            "obv_slope": round(obv_slope, 3),
            "vwap_deviation_pct": round(vwap_deviation_pct, 2),
        }

    except Exception as e:
        log.debug(f"  Fehler bei {symbol}: {e}")
        return None


# ============================================================
# SCORING
# ============================================================

def score_asset(analysis, use_ml=False):
    """Berechne einen Gesamtscore (-100 bis +100) fuer ein Asset.

    Args:
        analysis: dict von analyze_single_asset()
        use_ml: wenn True, ML-Modell statt fixe Gewichte verwenden
    """
    # ML Scoring Path
    if use_ml:
        try:
            from app.ml_scorer import score_asset_ml, is_model_trained
            if is_model_trained():
                ml_score = score_asset_ml(analysis)
                if ml_score is not None:
                    # ML gibt 0-100 (Wahrscheinlichkeit), umrechnen auf -100 bis +100
                    return round((ml_score - 50) * 2, 1)
        except ImportError:
            pass

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

    # Regime Filter: Score-Penalties basierend auf VIX und Marktregime
    # === SCHICHT B von 3 (Regime-Architektur) ===
    # Weiche Punkt-Abzuege je Kandidat (blockt NICHT, macht nur
    # unattraktiver). Liest die *_score_penalty-Keys aus dem geteilten
    # Config-Block `regime_filter`. Die harte GO/NO-GO-Sperre ist
    # Schicht A in app/market_context.py::check_regime_filter() und liest
    # die *_threshold-Keys (disjunkt!). Details: dortige Docstring.
    try:
        from app.config_manager import load_config, load_json
        cfg = load_config()
        rf = cfg.get("regime_filter", {})
        if rf.get("enabled", False):
            from app.market_context import get_current_context
            ctx = get_current_context()
            brain = load_json("brain_state.json") or {}

            vix_regime = ctx.get("vix_regime", "normal")
            market_regime = brain.get("market_regime", "unknown")

            if vix_regime == "high_fear":
                score += rf.get("high_fear_score_penalty", -15)
            elif vix_regime == "elevated":
                score += rf.get("elevated_score_penalty", -5)

            if market_regime == "bear":
                score += rf.get("bear_score_penalty", -10)
            elif market_regime == "sideways":
                score += rf.get("sideways_score_penalty", -3)
    except Exception:
        pass

    return round(score, 1)


# ============================================================
# REGIME-SPEZIFISCHE STRATEGIE-PROFILE (Phase 4.2)
# ============================================================

# Defensive Sektoren fuer Bear-Market (niedrigere Beta, Cash-Flow stabil)
_DEFENSIVE_SECTORS = {"health", "consumer", "bonds", "commodities", "real_estate"}


def apply_regime_strategy_modifier(score, analysis, sector, config=None):
    """
    Passt den Basis-Score an das aktuelle Marktregime an.

    Philosophie:
      - Bull:     Momentum-Trades verstaerken, Counter-Trend dampen
      - Sideways: Mean-Reversion verstaerken, ueberdehnte Momentum-Trades dampen
      - Bear:     Nur defensive Sektoren + starke Mean-Reversion

    Aufruf NACH score_asset() in scan_all_assets() unter Feature-Flag
    `regime_strategies.enabled` (Default: false).

    Args:
        score: Basis-Score aus score_asset() (-100..+100)
        analysis: dict aus analyze_single_asset()
        sector: Sektor-Tag des Assets (tech/consumer/finance/...)
        config: optional bereits geladene Config

    Returns:
        (modified_score, reason_str) — reason fuer Logging/Debugging
    """
    try:
        if config is None:
            from app.config_manager import load_config
            config = load_config()

        rs_cfg = config.get("regime_strategies", {})
        if not rs_cfg.get("enabled", False):
            return score, None

        from app.config_manager import load_json
        brain = load_json("brain_state.json") or {}
        regime = brain.get("market_regime", "unknown")

        # Signal-Staerken aus Analysis extrahieren
        rsi = analysis.get("rsi", 50)
        mom5 = analysis.get("momentum_5d", 0)
        mom20 = analysis.get("momentum_20d", 0)
        boll = analysis.get("bollinger_pos", 0.5)
        above_sma20 = analysis.get("above_sma20", False)
        above_sma50 = analysis.get("above_sma50", False)

        # Momentum-Staerke: positiv = Aufwaertstrend
        mom_strength = (mom5 + mom20 * 0.5)
        # Mean-Reversion-Staerke: positiv = Oversold-Setup
        mr_strength = 0
        if rsi < 35:
            mr_strength += (35 - rsi) * 0.5  # 0..17.5
        if boll < 0.25:
            mr_strength += (0.25 - boll) * 20  # 0..5

        reason = None
        delta = 0

        if regime == "bull":
            boost = rs_cfg.get("bull_momentum_boost", 0.5)
            if mom_strength > 0 and above_sma50:
                delta += mom_strength * boost
                reason = f"bull_momentum_boost +{delta:.1f}"
            # Counter-Trend gegen Bull dampen (RSI<35 aber Preis unter SMA20)
            if mr_strength > 0 and not above_sma20:
                penalty = -mr_strength * 0.5
                delta += penalty
                reason = f"bull_counter_trend_penalty {penalty:.1f}"

        elif regime == "sideways":
            boost = rs_cfg.get("sideways_mr_boost", 0.6)
            if mr_strength > 0:
                delta += mr_strength * boost
                reason = f"sideways_mr_boost +{delta:.1f}"
            # Ueberdehnter Momentum-Trade in Seitwaertsmarkt = Risiko
            if mom_strength > 8 and boll > 0.8:
                penalty = -(mom_strength - 8) * 0.4
                delta += penalty
                reason = f"sideways_overextended {penalty:.1f}"

        elif regime == "bear":
            non_def_penalty = rs_cfg.get("bear_non_defensive_penalty", -10)
            if sector not in _DEFENSIVE_SECTORS:
                delta += non_def_penalty
                reason = f"bear_non_defensive {non_def_penalty}"
            # In Bear nur starke MR-Setups durchlassen (RSI<30 + Boll<0.2)
            if mr_strength > 10:
                delta += 3
                reason = (reason or "") + " +bear_strong_mr +3"

        if delta == 0:
            return score, None

        return round(score + delta, 1), reason
    except Exception as e:
        log.debug(f"apply_regime_strategy_modifier error: {e}")
        return score, None


# ============================================================
# SCANNER HAUPTFUNKTION
# ============================================================

def _scan_via_signal_stack(max_per_class=None):
    """v38: Asset-Selektion via Fundamental-Signal-Stack (flag-gated).

    Liest die taeglich vom Cron berechneten + self-test-validierten Shadow-Scores
    (signal_stack_shadow.json) und ersetzt damit den edgelosen TA-score_asset.
    Mapping Stack-Score 0-100 -> Bot-Score -100..+100 via (s-50)*2 (reusing der
    bestehenden Signal-Schwellen).

    FAIL-SAFE: bei fehlenden/stale/fehlerhaften Scores -> [] (KEIN Buy). Der Bot
    handelt NIE auf kaputten Scores. Bestehende Positionen managt die Exit-Logik
    unabhaengig weiter. exc_info -> Sentry erfasst Tracebacks.
    """
    try:
        from datetime import datetime, timezone
        from app import sp600_universe
        from app.config_manager import load_json
        shadow = load_json("signal_stack_shadow.json")
        if not isinstance(shadow, dict) or not shadow.get("scores"):
            log.error("Signal-Stack: keine Shadow-Scores -> FAIL-SAFE (kein Buy)")
            return []
        gen = shadow.get("generated_at", "")
        try:
            g = datetime.fromisoformat(str(gen).replace("Z", ""))
            if g.tzinfo is None:
                g = g.replace(tzinfo=timezone.utc)
            age_h = (datetime.now(timezone.utc) - g).total_seconds() / 3600
            if age_h > 96:
                log.error("Signal-Stack: Shadow-Scores %.0fh alt (>96h) -> FAIL-SAFE (kein Buy)", age_h)
                return []
        except Exception as _ts_e:
            # v37dt Audit#6: unparsebares/fehlendes generated_at -> Alter UNBEKANNT.
            # Vorher `pass` (fail-OPEN) -> Bot handelte auf Scores unbekannten Alters
            # trotz dokumentiertem FAIL-SAFE-Vertrag. Jetzt fail-safe: kein Buy.
            log.error("Signal-Stack: generated_at unlesbar (%r): %s -> FAIL-SAFE (kein Buy)",
                      gen, _ts_e)
            return []
        scores = shadow["scores"]
        entries = sp600_universe.universe_entries()
        ranked = sorted(scores.items(), key=lambda kv: -(kv[1].get("score") or 0))
        results = []
        analyzed_buys = 0
        max_analyze = max_per_class if (max_per_class and max_per_class > 0) else 40
        for sym, d in ranked:
            stack_score = d.get("score")
            info = entries.get(sym)
            if stack_score is None or not info:
                continue
            # v37dt Audit#4: zu wenig Roh-Signal-Coverage -> nicht kaufbar (sonst
            # BUY auf 1 Signal + 4x neutral-0.5). Altdaten ohne Feld: wie bisher.
            if not d.get("eligible", True):
                continue
            bot_score = round((float(stack_score) - 50.0) * 2.0, 1)  # 0-100 -> -100..+100
            if bot_score >= 25:
                signal = "STRONG_BUY"
            elif bot_score >= 10:
                signal = "BUY"
            elif bot_score <= -25:
                signal = "STRONG_SELL"
            elif bot_score <= -10:
                signal = "SELL"
            else:
                signal = "HOLD"
            analysis = None
            if signal in ("BUY", "STRONG_BUY"):
                if analyzed_buys >= max_analyze:
                    signal = "HOLD"  # ueber Analyse-Cap -> nicht als Buy ausgeben
                else:
                    analysis = analyze_single_asset(sym, info)
                    if analysis is None:
                        continue  # kein Preis/Analysis -> kein Buy ohne Sizing-Daten
                    analyzed_buys += 1
                    time.sleep(0.3)
            results.append({
                "symbol": sym,
                "name": info["name"],
                "class": info["class"],
                "internal_id": info["internal_id"],
                "etoro_id": info["internal_id"],
                "score": bot_score,
                "signal": signal,
                "analysis": analysis,
            })
        n_buy = sum(1 for r in results if r["signal"] in ("BUY", "STRONG_BUY"))
        log.info("Signal-Stack-Scan: %d gescort, %d Buy-Kandidaten (analysiert)",
                 len(results), n_buy)
        return results
    except Exception as e:
        log.error("Signal-Stack-Scan EXCEPTION -> FAIL-SAFE (kein Buy): %s", e, exc_info=True)
        return []


def scan_all_assets(enabled_classes=None, max_per_class=None, use_ml=None):
    """
    Scannt alle Assets im Universum und gibt sortierte Opportunities zurueck.

    Args:
        enabled_classes: Liste von Asset-Klassen (None = alle)
        max_per_class: Max Anzahl Resultate pro Klasse (None = unbegrenzt)
        use_ml: ML-Scoring verwenden? None = aus config lesen

    Returns:
        Liste von {symbol, name, class, etoro_id, score, analysis, signal}
    """
    if yf is None:
        log.error("yfinance nicht installiert - Scanner deaktiviert")
        return []

    # v38: Signal-Stack-Selektion (flag-gated, Default AUS). Bei Flag-Check-Fehler
    # -> sicherer Fallback auf den bestehenden TA-Scan (alte Behaviour).
    try:
        from app.config_manager import load_config as _lc_ss
        if bool(_lc_ss().get("use_signal_stack", False)):
            log.info("MARKET SCANNER — Signal-Stack-Modus (use_signal_stack=ON)")
            return _scan_via_signal_stack(max_per_class=max_per_class)
    except Exception as _e:
        log.error("use_signal_stack-Flag-Check fehlgeschlagen -> TA-Scan-Fallback: %s", _e)

    if enabled_classes is None:
        # v37cv (04.05.2026): Default auf IBKR-handelbar reduziert.
        # Crypto (Spot via Kraken in Q3), Forex (IDEALPRO-Notation noetig),
        # Indices (nicht direkt handelbar, nur Futures/ETFs) sind explizit
        # ausgeschlossen — sonst Symbol-Resolution-Errors bei Order-Submit.
        # Commodities sind safe weil ibkr_override sie auf liquide ETFs
        # routed (GLD/SLV/USO/UNG/CPER auf ARCA).
        enabled_classes = ["stocks", "etf", "commodities"]

    # ML-Flag aus Config lesen wenn nicht explizit gesetzt
    if use_ml is None:
        try:
            from app.config_manager import load_config
            cfg = load_config()
            use_ml = cfg.get("demo_trading", {}).get("use_ml_scoring", False)
        except Exception:
            use_ml = False

    log.info("=" * 55)
    log.info("MARKET SCANNER - Alle Asset-Klassen")
    log.info(f"  Klassen: {', '.join(enabled_classes)}")
    log.info(f"  Universum: {len(ASSET_UNIVERSE)} Assets")
    log.info("=" * 55)

    # Filtere nach aktivierten Klassen
    try:
        from app.config_manager import load_config as _lc
        _cfg_ds = _lc()
        disabled_symbols = set(_cfg_ds.get("disabled_symbols", []) or [])
    except Exception:
        disabled_symbols = set()
    to_scan = {s: info for s, info in ASSET_UNIVERSE.items()
               if info["class"] in enabled_classes and s not in disabled_symbols}

    if disabled_symbols:
        log.info(f"  Universe-Filter: {len(disabled_symbols)} disabled_symbols ausgefiltert")
    log.info(f"  Scanne {len(to_scan)} Assets...")

    results = []
    errors = 0

    for symbol, info in to_scan.items():
        analysis = analyze_single_asset(symbol, info)
        if analysis is None:
            errors += 1
            continue

        score = score_asset(analysis, use_ml=use_ml)

        # Phase 4.2: Regime-spezifische Strategie-Profile
        try:
            from app.config_manager import load_config as _lc
            _cfg = _lc()
            if _cfg.get("regime_strategies", {}).get("enabled", False):
                sector = info.get("sector", "unknown")
                new_score, rs_reason = apply_regime_strategy_modifier(
                    score, analysis, sector, config=_cfg
                )
                if rs_reason:
                    log.debug(f"  [regime-strat] {symbol}: {score} -> {new_score} ({rs_reason})")
                score = new_score
        except Exception as _e:
            log.debug(f"regime_strategies hook failed: {_e}")

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
            # R-B1 Phase 4c: internal_id (kanonisch) + etoro_id (Dual-Key)
            "internal_id": _meta_internal_id(info),
            "etoro_id": _meta_internal_id(info),
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

    # Sector Rotation: Boost/Penalty basierend auf Sektorstaerke
    results = apply_sector_rotation(results)

    # Multi-Timeframe Confluence (enriches top results)
    results = enrich_with_mtf(results)

    return results


def calculate_sector_strength(results):
    """Berechne durchschnittliche Staerke pro Sektor.

    Returns:
        dict {sector: avg_score}
    """
    sector_scores = {}
    for r in results:
        sector = ASSET_UNIVERSE.get(r["symbol"], {}).get("sector")
        if not sector:
            continue
        if sector not in sector_scores:
            sector_scores[sector] = []
        sector_scores[sector].append(r["score"])

    return {s: sum(scores) / len(scores) for s, scores in sector_scores.items() if scores}


def apply_sector_rotation(results):
    """Boost/Penalty basierend auf Sektorstaerke.

    Starker Sektor (ueber Durchschnitt): +15% Boost
    Schwacher Sektor (5+ unter Durchschnitt): -15% Penalty
    """
    sector_strength = calculate_sector_strength(results)
    if not sector_strength:
        return results

    avg_strength = sum(sector_strength.values()) / len(sector_strength)

    for r in results:
        sector = ASSET_UNIVERSE.get(r["symbol"], {}).get("sector")
        if not sector or sector not in sector_strength:
            continue

        strength = sector_strength[sector]
        r["sector_strength"] = round(strength, 1)

        if strength > avg_strength:
            r["score"] = round(r["score"] * 1.15, 1)
        elif strength < avg_strength - 5:
            r["score"] = round(r["score"] * 0.85, 1)

    results.sort(key=lambda x: x["score"], reverse=True)
    log.info(f"  Sector Strength: {', '.join(f'{s}={v:.1f}' for s, v in sorted(sector_strength.items(), key=lambda x: x[1], reverse=True))}")
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


def calculate_confluence_score(mtf):
    """Berechne gewichteten Confluence-Score aus MTF-Daten.

    1H Trend (50%) + 15M Entry (30%) + 5M Alignment (20%) = -100 bis +100.
    """
    score = 0

    # 1H Trend: 50% Gewicht
    trend = mtf.get("trend_1h", "neutral")
    if trend == "up":
        score += 50
    elif trend == "down":
        score -= 50

    # 15M Entry: 30% Gewicht
    entry = mtf.get("entry_15m", "neutral")
    if entry == "buy":
        score += 30
    elif entry == "sell":
        score -= 30

    # 5M RSI alignment: 20% Gewicht (via 15m RSI as proxy)
    rsi_15m = mtf.get("rsi_15m", 50)
    if trend == "up" and rsi_15m < 50:
        score += 20  # Dip in Uptrend = guter Entry
    elif trend == "down" and rsi_15m > 50:
        score -= 20  # Bounce in Downtrend = guter Short

    return score


def enrich_with_mtf(scan_results, top_n=20, min_confluence=None):
    """Ergaenze die Top-N Scanner-Ergebnisse mit Multi-Timeframe-Daten.

    Confluence-gewichtete Anpassung:
    - Confirming TFs: +20% Score-Boost
    - Conflicting TFs: -30% Penalty
    """
    try:
        from app.config_manager import load_config
        cfg = load_config()
        mtf_cfg = cfg.get("multi_timeframe", {})
        if not mtf_cfg.get("enabled", False):
            return scan_results
        if top_n is None:
            top_n = mtf_cfg.get("top_n", 20)
        if min_confluence is None:
            min_confluence = mtf_cfg.get("min_confluence_score", 0)
    except Exception:
        pass

    enriched = 0
    for result in scan_results[:top_n]:
        if result["signal"] not in ("BUY", "STRONG_BUY", "SELL", "STRONG_SELL"):
            continue

        asset_info = ASSET_UNIVERSE.get(result["symbol"])
        if not asset_info:
            continue

        mtf = analyze_multi_timeframe(result["symbol"], asset_info)
        if mtf:
            confluence = calculate_confluence_score(mtf)
            mtf["confluence_score"] = confluence
            result["mtf"] = mtf
            result["confluence_score"] = confluence

            is_buy = result["signal"] in ("BUY", "STRONG_BUY")

            if (is_buy and confluence >= 40) or (not is_buy and confluence <= -40):
                # Confirming: +20% boost
                result["score"] = round(result["score"] * 1.20, 1)
                log.info(f"    MTF CONFIRMING: {result['symbol']} "
                         f"Confluence={confluence} -> Score {result['score']:+.1f}")
            elif (is_buy and confluence < 0) or (not is_buy and confluence > 0):
                # Conflicting: -30% penalty
                result["score"] = round(result["score"] * 0.70, 1)
                log.info(f"    MTF CONFLICTING: {result['symbol']} "
                         f"Confluence={confluence} -> Score {result['score']:+.1f}")

            enriched += 1

        time.sleep(0.5)  # Rate limiting

    # Re-sort after score adjustments
    scan_results.sort(key=lambda x: x["score"], reverse=True)
    log.info(f"  MTF: {enriched} Assets mit Multi-Timeframe angereichert")
    return scan_results


# ============================================================
# R-A45 (24.05.2026 Sprint-Tag-13): Boot-Time-Reload persisted Discoveries
# ============================================================
# Beim Module-Import (= Bot-Boot) werden persistierte Discovery-Adds aus
# data/discovered_universe_persist.json zurueck ins ASSET_UNIVERSE gemerged.
# Verhindert Universe-Verlust bei Container-Restart.
# Idempotent: schon vorhandene Symbole werden nicht ueberschrieben.
try:
    from app.discovery_persist import merge_into_asset_universe
    _merged = merge_into_asset_universe(ASSET_UNIVERSE)
    if _merged:
        log.info(f"R-A45 Boot: {_merged} persisted Discoveries ins ASSET_UNIVERSE reloaded")
    # R-B1 Phase 4: Boot-gemergte Discovery-Entries auch mit internal_id
    # versehen (Dual-Key-Konsistenz).
    _ensure_internal_ids(ASSET_UNIVERSE)
except Exception as _e:
    # Non-fatal: Bot soll auch ohne discovery-persist hochfahren
    log.warning(f"R-A45 Boot-Merge failed (non-fatal): {_e}")
