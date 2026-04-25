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


def _normalize_class(asset_class: str) -> str:
    """Map ASSET_UNIVERSE 'class' -> IBKR secType."""
    c = (asset_class or "").lower().strip()
    if c in ("stocks", "stock", "etf", "etfs", "equity", "equities"):
        return "STK"
    if c in ("crypto", "cryptocurrency"):
        return "CRYPTO"
    if c in ("forex", "fx", "currency"):
        return "CASH"
    if c in ("commodity", "commodities"):
        return "CMDTY"  # selten direkt handelbar, meist via ETF-Proxy
    if c in ("index", "indices"):
        return "IND"
    return "STK"  # Default fuer S&P-Style Symbole


def _exchange_for_sec_type(sec_type: str) -> str:
    """Default-Exchange pro IBKR secType."""
    return {
        "STK": "SMART",
        "CRYPTO": "PAXOS",
        "CASH": "IDEALPRO",
        "CMDTY": "SMART",
        "IND": "CBOE",
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

    sec_type = _normalize_class(meta["class"])
    exchange = _exchange_for_sec_type(sec_type)
    symbol = meta["symbol"]

    # 3. Vorlaeufiger Contract bauen
    if sec_type == "STK":
        c = Stock(symbol, exchange, currency)
    elif sec_type == "CRYPTO":
        c = Crypto(symbol, exchange, currency)
    elif sec_type == "CASH":
        # Forex: symbol = base currency (z.B. EUR), counter = currency (z.B. USD)
        c = Forex(f"{symbol}{currency}")
    else:
        raise NotImplementedError(
            f"Asset-Class '{meta['class']}' (-> {sec_type}) noch nicht unterstuetzt. "
            f"Aktuell: STK (Stocks/ETFs), CRYPTO, CASH (Forex). "
            f"Futures/Indizes/Commodities folgen in W4."
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
