"""
IBKR Contract Resolver (W3)
============================

Loest eToro-instrument_id (Integer) zu IBKR-Contract-Objekten auf.
Per-Resolution-Cache auf Disk in `data/ibkr_contract_cache.json`.

**Workflow pro Resolution:**
1. Cache-Lookup via etoro_id
2. Fallback: Reverse-Lookup in `ASSET_UNIVERSE` (etoro_id -> symbol + class)
3. Vorlaeufiger `Contract` aus Symbol + Class -> `ib.qualifyContracts()` ergaenzt conId/primaryExchange
4. Cache-Eintrag schreiben + Contract zurueckgeben

**Asset-Class-Mapping (ASSET_UNIVERSE.class -> IBKR secType + exchange):**
- "stocks" / "ETF" -> STK auf SMART (USD)
- "Crypto"        -> CRYPTO auf PAXOS (USD)
- "Forex"         -> CASH auf IDEALPRO
- Andere         -> NotImplementedError (W4 - Futures, Indizes, Commodities)

Deisgn-Entscheidung: Cache ist append-only und nie selbst-invalidiert.
Bei Listing-Aenderungen (extrem selten fuer S&P-500-Niveau Symbole) muss
die Cache-File manuell geloescht werden.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Cache-File-Pfad — relativ zum Projekt-Root (`app/` liegt darunter)
CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "ibkr_contract_cache.json"


def _registry_hints(asset_class: str) -> dict:
    """Hole IBKR-Hints aus app.asset_classes.Registry. Fallback auf STK/SMART/USD."""
    try:
        from app.asset_classes import get_ibkr_hints
        h = get_ibkr_hints(asset_class)
        if h:
            return h
    except Exception as e:
        log.debug("Registry-Lookup fehlgeschlagen (%s) — Fallback", e)
    return {"secType": "STK", "exchange": "SMART", "currency": "USD"}


def _normalize_class(asset_class: str) -> str:
    """Kompat-Wrapper: Map ASSET_UNIVERSE 'class' -> IBKR secType (via Registry)."""
    return _registry_hints(asset_class)["secType"]


def _exchange_for_sec_type(sec_type: str) -> str:
    """Default-Exchange pro IBKR secType (Legacy-Fallback)."""
    return {
        "STK": "SMART",
        "CRYPTO": "PAXOS",
        "CASH": "IDEALPRO",
        "CMDTY": "NYMEX",
        "IND": "CBOE",
        "FUT": "CME",
        "BOND": "SMART",
    }.get(sec_type, "SMART")


def _load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("Cache-Load failed (%s) — starte leer", e)
        return {}


def _save_cache(cache: dict) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write via tempfile
        tmp = CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(CACHE_PATH)
    except Exception as e:
        log.error("Cache-Save failed: %s", e)


def _lookup_etoro_id_in_universe(etoro_id: int) -> Optional[dict]:
    """
    Reverse-Lookup: etoro_id -> {symbol, class, name, sector}

    Lazy import um Circular-Dependencies zu vermeiden.
    """
    try:
        from app.market_scanner import ASSET_UNIVERSE
    except Exception as e:
        log.error("ASSET_UNIVERSE Import failed: %s", e)
        return None

    for symbol, meta in ASSET_UNIVERSE.items():
        if int(meta.get("etoro_id", -1)) == int(etoro_id):
            return {
                "symbol": symbol,
                "class": meta.get("class", "stocks"),
                "name": meta.get("name", symbol),
                "sector": meta.get("sector"),
            }
    return None


def resolve_contract(ib, etoro_id: int, currency: str = "USD"):
    """
    Loest eToro-instrument_id zu IBKR Contract auf (qualified, mit conId).

    Args:
        ib: ib_insync.IB Instanz, connected.
        etoro_id: eToro instrument ID (z.B. 6408 fuer AAPL)
        currency: Default USD (overridebar fuer Forex/EU-Stocks)

    Returns:
        ib_insync.Contract Instanz, qualifiziert (conId gesetzt).

    Raises:
        ValueError wenn etoro_id nicht in ASSET_UNIVERSE.
        NotImplementedError fuer noch nicht unterstuetzte Asset-Klassen.
        RuntimeError wenn IBKR die Qualifikation ablehnt (Symbol unbekannt).
    """
    from ib_insync import Stock, Contract, Crypto, Forex
    # Optional: erweiterte Asset-Klassen (Index/Future) — falls in dieser
    # ib_insync-Version vorhanden (>=0.9.86 hat sie alle).
    try:
        from ib_insync import Index, Future
    except ImportError:
        Index = Future = None

    cache = _load_cache()
    cache_key = str(int(etoro_id))

    # 1. Cache-Hit -> direkt aus Cache rekonstruieren
    if cache_key in cache:
        entry = cache[cache_key]
        log.debug("Cache-Hit fuer etoro_id=%d -> %s", etoro_id, entry["symbol"])
        c = Contract(
            secType=entry["secType"],
            conId=entry["conId"],
            symbol=entry["symbol"],
            exchange=entry["exchange"],
            currency=entry.get("currency", currency),
        )
        # Eine bereits qualifizierte Contract braucht kein erneutes qualifyContracts
        return c

    # 2. Reverse-Lookup in ASSET_UNIVERSE
    meta = _lookup_etoro_id_in_universe(etoro_id)
    if meta is None:
        raise ValueError(
            f"etoro_id={etoro_id} nicht in ASSET_UNIVERSE — kein Symbol-Mapping. "
            f"Asset zuerst via market_scanner.ASSET_UNIVERSE oder asset_discovery aufnehmen."
        )

    # Hints aus Registry holen — Single Source of Truth fuer SecType/Exchange/Currency
    hints = _registry_hints(meta["class"])
    sec_type = hints["secType"]
    # Symbol-Meta darf Registry-Defaults overrulen (per-Symbol Overrides moeglich)
    exchange = meta.get("ibkr_exchange") or hints["exchange"]
    # Currency: explicit param > Symbol-Meta-Override > Registry-Default
    if currency == "USD" and (meta.get("ibkr_currency") or hints["currency"]) != "USD":
        # User hat default USD nicht explizit overruled -> Registry-Currency nehmen
        currency = meta.get("ibkr_currency") or hints["currency"]
    symbol = meta["symbol"]

    # 3. Vorlaeufiger Contract bauen
    if sec_type == "STK":
        # Funktioniert fuer ALLE Equities-Boersen (NYSE/Nasdaq/IBIS/LSE/SEHK/TSEJ/ASX/EBS).
        # IBKR's qualifyContracts() resolved primaryExchange automatisch.
        c = Stock(symbol, exchange, currency)
    elif sec_type == "CRYPTO":
        c = Crypto(symbol, exchange, currency)
    elif sec_type == "CASH":
        # Forex: symbol = base currency (z.B. EUR), counter = currency (z.B. USD)
        c = Forex(f"{symbol}{currency}")
    elif sec_type == "IND":
        # Indizes (z.B. SPX, NDX) — nur Quote/Read-Only sinnvoll, nicht direkt handelbar
        # Nutzer muss das via Future oder ETF-Proxy traden (z.B. ES Future fuer SPX,
        # SPY ETF als Equity-Proxy)
        if Index is None:
            raise NotImplementedError("ib_insync.Index nicht importiert — Version pruefen")
        c = Index(symbol, exchange, currency)
        log.warning(
            "Index-Contract %s erstellt — Trades NICHT empfohlen, nur Quotes. "
            "Fuer Trading nutze ETF-Proxy (z.B. SPY statt SPX) oder Future (ES).",
            symbol,
        )
    elif sec_type == "FUT":
        # Futures: braucht expiry. Klone-Bot muss in ASSET_UNIVERSE folgendes setzen:
        #   ibkr_expiry: 'YYYYMM' (z.B. '202506')  ODER  'YYYYMMDD'
        #   ibkr_multiplier: optional, IBKR liefert Default
        # Quantity-Calc beruecksichtigt Multiplier nicht automatisch — caller muss
        # amount_to_quantity() mit price * multiplier aufrufen.
        if Future is None:
            raise NotImplementedError("ib_insync.Future nicht importiert — Version pruefen")
        expiry = meta.get("ibkr_expiry")
        if not expiry:
            raise NotImplementedError(
                f"Futures-Asset {symbol} braucht 'ibkr_expiry' (YYYYMM) in ASSET_UNIVERSE. "
                f"Beispiel: meta['ibkr_expiry'] = '202506' fuer June-2025-Contract."
            )
        c = Future(symbol, expiry, exchange, currency=currency)
    elif sec_type == "CMDTY":
        # IBKR CMDTY-Pfad existiert (Spot Gold via XAUUSD), aber selten genutzt.
        # Empfehlung bleibt: ETF-Proxy ('class: etf' in Universe). Wenn Klon-Bot
        # echte CMDTY will, muss er Symbol z.B. "XAUUSD" + exchange "SMART" setzen.
        c = Contract(secType="CMDTY", symbol=symbol, exchange=exchange, currency=currency)
    elif sec_type == "BOND":
        # US-Treasuries: braucht conId oder ISIN. Aktuell nur Stub —
        # echter Klon-Bot muss meta['ibkr_conId'] setzen.
        cid = meta.get("ibkr_conId")
        if not cid:
            raise NotImplementedError(
                f"Bonds-Asset {symbol} braucht 'ibkr_conId' in ASSET_UNIVERSE. "
                f"Bonds haben keine eindeutigen Ticker — IBKR conId aus Search nutzen."
            )
        c = Contract(secType="BOND", conId=int(cid), exchange=exchange, currency=currency)
    else:
        raise NotImplementedError(
            f"Asset-Class '{meta['class']}' (-> {sec_type}) noch nicht unterstuetzt. "
            f"Aktuell: STK (Stocks/ETFs), CRYPTO, CASH (Forex), IND (Index, Read-Only)."
        )

    # 4. Qualifizieren (IBKR ergaenzt conId, primaryExchange, etc.)
    try:
        qualified = ib.qualifyContracts(c)
    except Exception as e:
        raise RuntimeError(
            f"IBKR qualifyContracts failed fuer {symbol} ({sec_type}): {e}"
        )

    if not qualified or not qualified[0].conId:
        raise RuntimeError(
            f"IBKR konnte {symbol} ({sec_type} auf {exchange}) nicht aufloesen "
            f"— evtl. Symbol falsch oder kein Marktzugang."
        )

    qc = qualified[0]
    log.info("Qualified: etoro_id=%d %s -> conId=%d on %s",
             etoro_id, symbol, qc.conId, qc.exchange)

    # 5. Cache-Eintrag schreiben
    cache[cache_key] = {
        "conId": qc.conId,
        "symbol": qc.symbol,
        "secType": qc.secType,
        "exchange": qc.exchange,
        "currency": qc.currency,
        "primaryExchange": getattr(qc, "primaryExchange", ""),
        "name": meta.get("name"),
        "qualified_at": int(time.time()),
    }
    _save_cache(cache)

    return qc


def _safe_num(v) -> Optional[float]:
    """Wandle ib_insync-Ticker-Werte (kann float, NaN, callable, None sein) in Optional[float] um."""
    import math
    try:
        if v is None:
            return None
        # Callable abfangen (ticker.marketPrice() ist eine method in ib_insync)
        if callable(v):
            try:
                v = v()
            except Exception:
                return None
        f = float(v)
        if math.isnan(f) or f <= 0:
            return None
        return f
    except (TypeError, ValueError):
        return None


def get_quote(ib, contract, timeout: float = 5.0, allow_delayed: bool = True) -> Optional[float]:
    """
    Schneller Price-Snapshot fuer Quantity-Berechnung.

    Versucht in folgender Reihenfolge:
    1. Last-Price (wenn frisch)
    2. Mid (Bid+Ask)/2
    3. marketPrice() (ib_insync's smart fallback)
    4. Close (Vortagesschluss)

    Bei Paper-Accounts ohne Market-Data-Abo: schaltet auf Delayed-Data
    via reqMarketDataType(3) wenn allow_delayed=True (default).

    Returns:
        Price als float, oder None wenn kein Quote verfuegbar.
    """
    try:
        if allow_delayed:
            try:
                # 1=Live, 2=Frozen, 3=Delayed, 4=Delayed-Frozen
                ib.reqMarketDataType(3)
            except Exception:
                pass

        ticker = ib.reqMktData(contract, snapshot=True, regulatorySnapshot=False)
        deadline = time.time() + timeout
        while time.time() < deadline:
            ib.sleep(0.1)
            last = _safe_num(getattr(ticker, "last", None))
            if last:
                return last
            bid = _safe_num(getattr(ticker, "bid", None))
            ask = _safe_num(getattr(ticker, "ask", None))
            if bid and ask:
                return (bid + ask) / 2.0
            mp = _safe_num(getattr(ticker, "marketPrice", None))
            if mp:
                return mp
            close = _safe_num(getattr(ticker, "close", None))
            if close:
                return close
        # Final Fallback nach Timeout
        for attr in ("last", "marketPrice", "close", "bid", "ask"):
            v = _safe_num(getattr(ticker, attr, None))
            if v:
                return v
        return None
    except Exception as e:
        log.error("get_quote failed: %s", e)
        return None
    finally:
        try:
            ib.cancelMktData(contract)
        except Exception:
            pass


def amount_to_quantity(amount_usd: float, price: float, min_qty: int = 1) -> int:
    """
    eToro-Style 'amount in USD' -> IBKR-Style 'integer quantity'.

    Stocks koennen bei IBKR nur in ganzen Aktien gehandelt werden (Fractional
    Shares sind moeglich aber nicht in allen Konten — wir bleiben konservativ
    bei Integer).

    Returns:
        Floor(amount_usd / price), mindestens min_qty wenn budget erlaubt,
        sonst 0 (= Trade ueberspringen).
    """
    if price <= 0 or amount_usd <= 0:
        return 0
    qty = int(amount_usd // price)
    if qty < min_qty:
        return 0
    return qty
