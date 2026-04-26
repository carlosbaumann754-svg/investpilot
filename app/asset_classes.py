"""
InvestPilot - Asset-Class Registry (v1)
========================================

Single Source of Truth fuer:
  1. Welche Asset-Klassen existieren
  2. Wann sie tradeable sind (Trading-Hours pro Exchange, DST-aware)
  3. Wie sie zu IBKR-Contracts gemappt werden (SecType, Exchange, Currency)

Design-Ziel: Diese Registry soll von KUENFTIGEN Bot-Klonen (andere
Strategien) ohne Code-Aenderung wiederverwendbar sein. Jeder Klon kann
per `config.ini` die Registry filtern oder erweitern.

Konsumenten:
  - app/scheduler.py        -> is_market_hours() / is_asset_class_tradeable()
  - app/ibkr_contract_resolver.py -> SecType + Exchange-Hints
  - app/trader.py           -> per-Symbol-Skip wenn Klasse zu

DST-Handling: Wir nutzen zoneinfo (stdlib). Das heisst: alle Zeiten in
der Registry sind in LOKALER Exchange-Zeit (z.B. "America/New_York" fuer
NYSE) — der Wechsel CET/CEST und EST/EDT passiert automatisch.

Hinweis Feiertage: Diese Registry kennt keine Boersen-Feiertage
(z.B. 4th of July, Christmas). Implementierung waere via `pandas_market_calendars`
oder `exchange_calendars` moeglich — aktuell akzeptieren wir 1-2x/Jahr
False-Positive (Bot versucht zu traden, IBKR rejected, Cycle ueberlebt).
Wenn ein Klon Feiertage zwingend braucht: optional 'holiday_calendar'
key pro Asset-Class hinzufuegen.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Callable, Iterable, Optional
from zoneinfo import ZoneInfo

# ----------------------------------------------------------------------
# Trading-Session-Definitionen
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class TradingSession:
    """Eine Handels-Session in lokaler Exchange-Zeit.

    weekdays: Mo=0 .. So=6, welche Wochentage die Session AKTIV starten kann.
              Wenn end < start (z.B. Forex), interpretiert end als naechster Tag.
    """
    timezone: str                 # IANA-TZ, z.B. "America/New_York"
    start: time                   # lokale Open-Time
    end: time                     # lokale Close-Time
    weekdays: tuple[int, ...] = (0, 1, 2, 3, 4)  # Default Mo-Fr

    def is_active(self, now_utc: Optional[datetime] = None) -> bool:
        """Block-Semantik: weekdays definiert eine Menge "aktiver Tage". Die Session
        startet zu `start` am ersten Tag des zusammenhaengenden Block-Segments und
        endet zu `end` am letzten Tag — alle Tage dazwischen sind ganztags aktiv.

        Beispiele:
        - RTH (start=09:30, end=16:00, weekdays=Mo-Fr): jeder Tag isoliert; first==last,
          also nur 09:30-16:00 lokal.
        - Forex (start=17:00, end=17:00, weekdays=So-Fr): Block So-Fr; So<17 blockiert,
          Mo-Do ganztags, Fr>=17 blockiert, Sa komplett blockiert.
        - Echte Overnight (start=22:00, end=06:00, weekdays=Mo-Fr): Mo 22-Sa 06.
        """
        tz = ZoneInfo(self.timezone)
        local = (now_utc or datetime.now(tz=ZoneInfo("UTC"))).astimezone(tz)
        wd = local.weekday()
        cur = local.time().replace(microsecond=0)

        # Modus A: start < end -> klassische tagesbeschraenkte Session pro Weekday
        if self.start < self.end:
            if wd not in self.weekdays:
                return False
            return self.start <= cur < self.end

        # Modus B: start >= end -> kontinuierliche Block-Session ueber mehrere
        # Tage (24h-Forex, Overnight-Futures). Block-Semantik: Session laeuft
        # vom ersten Tag des zusammenhaengenden weekdays-Blocks ab `start` bis
        # zum letzten Tag bis `end`.
        if wd not in self.weekdays:
            return False

        prev_wd = (wd - 1) % 7
        next_wd = (wd + 1) % 7
        is_first_day = prev_wd not in self.weekdays
        is_last_day = next_wd not in self.weekdays

        if is_first_day and cur < self.start:
            return False
        if is_last_day and cur >= self.end:
            return False
        return True


@dataclass(frozen=True)
class AssetClassSpec:
    """Beschreibt eine Asset-Klasse vollstaendig.

    aliases: Strings die in market_scanner.ASSET_UNIVERSE als 'class' auftauchen
             koennen (eToro/IBKR/yfinance benutzen z.T. unterschiedliche Namen).
    sessions: Liste von Sessions — Asset gilt als tradeable wenn IRGENDEINE
              aktiv ist. (Mehrere Sessions z.B. fuer US-Stocks RTH+Pre+After.)
    ibkr_sec_type: IBKR-SecType ('STK', 'CRYPTO', 'CASH', 'FUT', 'IND', 'CMDTY', 'BOND')
    ibkr_exchange_default: Default-Exchange fuer qualifyContracts() wenn
                           kein expliziter Exchange im Symbol-Mapping ist.
    ibkr_currency_default: Default-Quote-Currency.
    custom_check: Optionaler Hook (cb(now_utc) -> bool) fuer Spezialfaelle
                  (z.B. IBKR-Crypto-Maintenance-Window).
    """
    name: str
    aliases: tuple[str, ...]
    sessions: tuple[TradingSession, ...]
    ibkr_sec_type: str
    ibkr_exchange_default: str = "SMART"
    ibkr_currency_default: str = "USD"
    custom_check: Optional[Callable[[Optional[datetime]], bool]] = None
    description: str = ""

    def is_tradeable(self, now_utc: Optional[datetime] = None) -> bool:
        if self.custom_check is not None:
            try:
                if not self.custom_check(now_utc):
                    return False
            except Exception:
                pass  # Custom-Check-Fehler -> nicht blockieren
        if not self.sessions:
            return True  # Klasse ohne Sessions = always-on (z.B. Crypto)
        return any(s.is_active(now_utc) for s in self.sessions)


# ----------------------------------------------------------------------
# Custom Hooks
# ----------------------------------------------------------------------


def _ibkr_crypto_maintenance(now_utc: Optional[datetime] = None) -> bool:
    """IBKR Paxos Crypto Wartungsfenster: Sa 19:00 NY -> So 07:00 NY (~12h).
    Real-Account: kontinuierlich (24/7) — diese Begrenzung gilt nur fuer
    IBKR's Crypto-Routing. Wir akzeptieren False-Positives lieber als
    Order-Rejects."""
    tz = ZoneInfo("America/New_York")
    local = (now_utc or datetime.now(tz=ZoneInfo("UTC"))).astimezone(tz)
    wd = local.weekday()  # Sa=5, So=6
    h = local.hour
    if wd == 5 and h >= 19:
        return False  # Sa 19:00 NY -> Wartung beginnt
    if wd == 6 and h < 7:
        return False  # So < 07:00 NY -> Wartung dauert an
    return True


# ----------------------------------------------------------------------
# Registry — die "richtigen" Sessions fuer 2025/26
# ----------------------------------------------------------------------

# US Equities: RTH (NYSE/Nasdaq), Pre-Market 04:00-09:30, After 16:00-20:00 NY
_US_RTH = TradingSession("America/New_York", time(9, 30), time(16, 0))
_US_PRE = TradingSession("America/New_York", time(4, 0), time(9, 30))
_US_AFTER = TradingSession("America/New_York", time(16, 0), time(20, 0))

# EU Equities
_FRANKFURT = TradingSession("Europe/Berlin", time(9, 0), time(17, 30))
_LSE = TradingSession("Europe/London", time(8, 0), time(16, 30))
_PARIS = TradingSession("Europe/Paris", time(9, 0), time(17, 30))
_SIX = TradingSession("Europe/Zurich", time(9, 0), time(17, 30))

# Asia
_TOKYO_AM = TradingSession("Asia/Tokyo", time(9, 0), time(11, 30))
_TOKYO_PM = TradingSession("Asia/Tokyo", time(12, 30), time(15, 0))
_HKEX_AM = TradingSession("Asia/Hong_Kong", time(9, 30), time(12, 0))
_HKEX_PM = TradingSession("Asia/Hong_Kong", time(13, 0), time(16, 0))
_ASX = TradingSession("Australia/Sydney", time(10, 0), time(16, 0))

# Forex: Sonntag 17:00 NY -> Freitag 17:00 NY (durchgehend)
_FOREX = TradingSession(
    "America/New_York", time(17, 0), time(17, 0),
    weekdays=(6, 0, 1, 2, 3, 4),  # Sun-Fri-Open
)

# CME Futures (Equity-Index, Commodities, Bonds): So 18:00 - Fr 17:00 NY
# mit taeglicher 60-Min-Pause 17:00-18:00 (akzeptieren wir als blockiert)
_CME_FUT = TradingSession(
    "America/New_York", time(18, 0), time(17, 0),
    weekdays=(6, 0, 1, 2, 3, 4),
)

# US Bonds (TRACE / cash bond market): Mo-Fr 08:00-17:00 NY
_US_BOND = TradingSession("America/New_York", time(8, 0), time(17, 0))


REGISTRY: dict[str, AssetClassSpec] = {
    # --- Crypto: 24/7 (mit optionalem IBKR-Wartungsfenster) ---
    "crypto": AssetClassSpec(
        name="crypto",
        aliases=("crypto", "cryptocurrency", "coin"),
        sessions=(),  # always-on
        ibkr_sec_type="CRYPTO",
        ibkr_exchange_default="PAXOS",
        ibkr_currency_default="USD",
        custom_check=_ibkr_crypto_maintenance,
        description="Kryptowaehrungen — 24/7, Wartung Sa-So bei IBKR Paper",
    ),

    # --- US Equities (Stocks, ETFs) ---
    "stocks": AssetClassSpec(
        name="stocks",
        aliases=("stocks", "stock", "equity", "us-stock"),
        sessions=(_US_RTH,),  # Default nur RTH; Klone koennen Pre/After zuschalten
        ibkr_sec_type="STK",
        ibkr_exchange_default="SMART",
        ibkr_currency_default="USD",
        description="US-Aktien NYSE/Nasdaq Mo-Fr 09:30-16:00 ET",
    ),
    "etf": AssetClassSpec(
        name="etf",
        aliases=("etf", "etfs", "fund"),
        sessions=(_US_RTH,),
        ibkr_sec_type="STK",  # ETFs sind technisch STK bei IBKR
        ibkr_exchange_default="SMART",
        ibkr_currency_default="USD",
        description="US-ETFs — wie Stocks RTH",
    ),

    # --- Extended Hours (eigene Klasse, opt-in fuer Klone) ---
    "stocks_extended": AssetClassSpec(
        name="stocks_extended",
        aliases=("stocks_extended", "stocks-extended", "us-stock-ext"),
        sessions=(_US_PRE, _US_RTH, _US_AFTER),
        ibkr_sec_type="STK",
        description="US-Stocks inkl. Pre/After-Market 04:00-20:00 ET",
    ),

    # --- EU Equities ---
    "eu_stocks": AssetClassSpec(
        name="eu_stocks",
        aliases=("eu_stocks", "eu-stock", "dax", "frankfurt"),
        sessions=(_FRANKFURT,),
        ibkr_sec_type="STK",
        ibkr_exchange_default="IBIS",  # Xetra
        ibkr_currency_default="EUR",
        description="EU-Aktien (DAX/CAC) Frankfurt Mo-Fr 09:00-17:30 CET",
    ),
    "uk_stocks": AssetClassSpec(
        name="uk_stocks",
        aliases=("uk_stocks", "lse", "london"),
        sessions=(_LSE,),
        ibkr_sec_type="STK",
        ibkr_exchange_default="LSE",
        ibkr_currency_default="GBP",
        description="UK-Aktien LSE Mo-Fr 08:00-16:30 London",
    ),
    "ch_stocks": AssetClassSpec(
        name="ch_stocks",
        aliases=("ch_stocks", "six", "swiss"),
        sessions=(_SIX,),
        ibkr_sec_type="STK",
        ibkr_exchange_default="EBS",
        ibkr_currency_default="CHF",
        description="Schweizer Aktien SIX Mo-Fr 09:00-17:30 Zurich",
    ),

    # --- Asia ---
    "jp_stocks": AssetClassSpec(
        name="jp_stocks",
        aliases=("jp_stocks", "tse", "tokyo"),
        sessions=(_TOKYO_AM, _TOKYO_PM),
        ibkr_sec_type="STK",
        ibkr_exchange_default="TSEJ",
        ibkr_currency_default="JPY",
        description="Tokio-Aktien Mo-Fr (Mittagspause 11:30-12:30)",
    ),
    "hk_stocks": AssetClassSpec(
        name="hk_stocks",
        aliases=("hk_stocks", "hkex", "hongkong"),
        sessions=(_HKEX_AM, _HKEX_PM),
        ibkr_sec_type="STK",
        ibkr_exchange_default="SEHK",
        ibkr_currency_default="HKD",
        description="Hong Kong HKEX (Mittagspause 12:00-13:00)",
    ),
    "au_stocks": AssetClassSpec(
        name="au_stocks",
        aliases=("au_stocks", "asx", "sydney"),
        sessions=(_ASX,),
        ibkr_sec_type="STK",
        ibkr_exchange_default="ASX",
        ibkr_currency_default="AUD",
        description="Australien ASX Mo-Fr 10:00-16:00 Sydney",
    ),

    # --- Forex ---
    "forex": AssetClassSpec(
        name="forex",
        aliases=("forex", "fx", "currency", "currencies"),
        sessions=(_FOREX,),
        ibkr_sec_type="CASH",
        ibkr_exchange_default="IDEALPRO",
        ibkr_currency_default="USD",  # Quote-Currency, base aus Symbol
        description="FX 24/5 (Sun 17:00 - Fri 17:00 NY)",
    ),

    # --- Futures (CME) ---
    "futures": AssetClassSpec(
        name="futures",
        aliases=("futures", "future", "fut"),
        sessions=(_CME_FUT,),
        ibkr_sec_type="FUT",
        ibkr_exchange_default="CME",
        ibkr_currency_default="USD",
        description="CME-Futures Sun 18:00 - Fri 17:00 NY (60-Min Daily Maint)",
    ),

    # --- Indices (Spot — meist nur Quote, getradet via ETF/Futures) ---
    "indices": AssetClassSpec(
        name="indices",
        aliases=("indices", "index", "indexes"),
        sessions=(_US_RTH,),  # Spot folgt RTH; Futures-Variante = "futures"
        ibkr_sec_type="IND",
        ibkr_exchange_default="CBOE",
        ibkr_currency_default="USD",
        description="Index-Spot (zB SPX) — folgt US-RTH",
    ),

    # --- Commodities (Spot, ueblicherweise via Futures gehandelt) ---
    "commodities": AssetClassSpec(
        name="commodities",
        aliases=("commodities", "commodity", "metal", "energy"),
        sessions=(_CME_FUT,),  # echte Commodities = Futures-Sessions
        ibkr_sec_type="CMDTY",
        ibkr_exchange_default="NYMEX",
        ibkr_currency_default="USD",
        description="Commodities — meist Futures-Routing (NYMEX/COMEX)",
    ),

    # --- Bonds (US Treasuries cash market) ---
    "bonds": AssetClassSpec(
        name="bonds",
        aliases=("bonds", "bond", "treasury", "fixed-income"),
        sessions=(_US_BOND,),
        ibkr_sec_type="BOND",
        ibkr_exchange_default="SMART",
        ibkr_currency_default="USD",
        description="US-Bonds Mo-Fr 08:00-17:00 ET (cash market)",
    ),
}


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------


def _alias_index() -> dict[str, AssetClassSpec]:
    """Lazy-build {alias_lower: spec} fuer schnelles Lookup."""
    idx: dict[str, AssetClassSpec] = {}
    for spec in REGISTRY.values():
        for a in spec.aliases:
            idx[a.lower()] = spec
        idx[spec.name.lower()] = spec
    return idx


_ALIAS_IDX: dict[str, AssetClassSpec] = _alias_index()


def resolve_spec(asset_class: str) -> Optional[AssetClassSpec]:
    """Liefert Spec fuer eine class-Bezeichnung (case-insensitive, alias-aware)."""
    if not asset_class:
        return None
    return _ALIAS_IDX.get(asset_class.lower())


def is_asset_class_tradeable(
    asset_class: str,
    now_utc: Optional[datetime] = None,
    *,
    default_if_unknown: bool = True,
) -> bool:
    """Pruefe ob eine spezifische Asset-Klasse JETZT tradeable ist.

    - Nutzt Registry inkl. DST-aware sessions.
    - default_if_unknown: was zurueckgeben wenn die Klasse unbekannt ist?
      True (default) ist konservativ-permissiv: erlaubt weiter zu traden,
      damit ein neuer Asset-Typ im Universum den Bot nicht versehentlich
      stillegt. Ein Klon kann False setzen wenn er strikt sein will.
    """
    spec = resolve_spec(asset_class)
    if spec is None:
        return default_if_unknown
    return spec.is_tradeable(now_utc)


def any_class_tradeable(
    classes: Iterable[str],
    now_utc: Optional[datetime] = None,
) -> bool:
    """True wenn IRGENDEINE der angegebenen Klassen jetzt tradeable ist."""
    return any(is_asset_class_tradeable(c, now_utc) for c in classes)


def get_ibkr_hints(asset_class: str) -> Optional[dict]:
    """IBKR-Contract-Hints fuer den Resolver."""
    spec = resolve_spec(asset_class)
    if spec is None:
        return None
    return {
        "secType": spec.ibkr_sec_type,
        "exchange": spec.ibkr_exchange_default,
        "currency": spec.ibkr_currency_default,
    }


def list_classes() -> list[str]:
    """Alle bekannten Klassennamen (kanonisch, ohne Aliase)."""
    return sorted(REGISTRY.keys())
