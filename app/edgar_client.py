"""EDGAR-Client (E5b) — SEC XBRL company-facts Fundamental-Daten.

Fetcht + cached point-in-time Fundamentaldaten aus der SEC-EDGAR-XBRL-API
(gratis, kein API-Key, 15+ Jahre Historie mit echtem ``filed``-Datum = kein
Look-Ahead-Bias). Datenbasis fuer den Fundamental-Signal-Stack
(app/fundamental_signals.py), der den edgelosen TA-Score (market_scanner.
score_asset) in der Asset-Selektion ersetzt.

Validierungs-Hintergrund (07.-08.06.2026, siehe docs/UNIVERSE_CHANGE_PLAN.md):
Der edgelose TA-Composite (IC ~0, kombiniert sogar kontraproduktiv: -0.88 Korr.
zu Reversal) wurde durch einen 5-Signal-Stack (Value+Quality+Reversal+Lev+
EarnGrowth) ersetzt, der den Equal-Weight-Benchmark risiko-adjustiert schlug
(Sharpe 1.02 vs 0.77 ueber 15J, positiv in beiden Halbperioden). Die
Fundamental-Signale brauchen Eigenkapital/GrossProfit/Earnings — die liefert
dieses Modul gratis aus EDGAR.

TRADING-NEUTRAL: dieses Modul fetcht + cached nur Daten, beruehrt keine
Handelslogik. Sicher waehrend der Soak.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request
from datetime import datetime, timedelta
from typing import Optional

AUDIT_METADATA = {
    "purpose": "EDGAR-Client (E5b): SEC XBRL company-facts Fundamental-Fetch+Cache + Point-in-Time-Accessoren fuer den Fundamental-Signal-Stack (ersetzt edgelosen TA-Score)",
    "config_section": None,  # v37dr: keine 'edgar'-Config-Section (config-los)
    "state_files": ["edgar_facts.json", "edgar_cik_map.json"],
    "self_tests": ["tc_edgar_cache_fresh"],
    "scheduler_hooks": ["refresh_edgar_facts (Host-Crontab woechentlich, 0 5 * * 0)"],
    "health_check": "edgar_cache_status",
    "added_in": "v38 (Fundamental-Signal-Stack — ersetzt edgelosen TA-Score)",
}

log = logging.getLogger(__name__)

# SEC verlangt einen User-Agent mit Kontakt; Rate-Limit ~10 req/s.
_USER_AGENT = "InvestPilot Research investpilot@mattka.ch"
_CIK_URL = "https://www.sec.gov/files/company_tickers.json"
_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"

_CIK_CACHE_FILE = "edgar_cik_map.json"
_FACTS_CACHE_FILE = "edgar_facts.json"

# us-gaap-Konzepte (USD-Einheit) fuer die Fundamental-Signale.
USD_CONCEPTS = [
    "NetIncomeLoss",
    "StockholdersEquity",
    "Assets",
    "GrossProfit",
    "NetCashProvidedByUsedInOperatingActivities",
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
]
# Aktien-Konzepte (Einheit "shares") fuer Market-Cap (Value/Yield-Signale).
SHARE_CONCEPTS = [
    "CommonStockSharesOutstanding",
    "WeightedAverageNumberOfSharesOutstandingBasic",
]

# Maximales Cache-Alter (Tage) bevor der Health-Check warnt. Fundamentaldaten
# aendern sich quartalsweise -> woechentlicher Refresh, 35d-Toleranz.
_MAX_CACHE_AGE_DAYS = 35


# ===========================================================================
# HTTP (stdlib urllib — keine externe Dependency)
# ===========================================================================
def _http_get_json(url: str, timeout: int = 30) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ===========================================================================
# CIK-Mapping (Ticker -> SEC CIK)
# ===========================================================================
def get_cik_map(force_refresh: bool = False) -> dict:
    """Ticker(UPPER) -> CIK(int). Gecacht in edgar_cik_map.json."""
    from app.config_manager import load_json, save_json

    if not force_refresh:
        cached = load_json(_CIK_CACHE_FILE)
        if isinstance(cached, dict) and cached:
            return cached
    data = _http_get_json(_CIK_URL)
    cmap = {v["ticker"].upper(): int(v["cik_str"]) for v in data.values()}
    try:
        save_json(_CIK_CACHE_FILE, cmap)
    except Exception as e:  # pragma: no cover - IO best effort
        log.warning("CIK-Cache schreiben fehlgeschlagen: %s", e)
    return cmap


# ===========================================================================
# Company-Facts Fetch + Cache
# ===========================================================================
def _compact(units: list) -> list:
    """Reduziere XBRL-Datenpunkte auf {s,e,f,v} (start, end, filed, val).

    Behalte nur Punkte mit ``end``, ``filed`` UND ``val`` — alles andere ist
    fuer point-in-time-Accessoren unbrauchbar.
    """
    return [
        {"s": p.get("start"), "e": p.get("end"), "f": p.get("filed"), "v": p.get("val")}
        for p in units
        if p.get("end") and p.get("filed") and p.get("val") is not None
    ]


def fetch_symbol_facts(symbol: str, cik_map: Optional[dict] = None) -> Optional[dict]:
    """Hole + reduziere die benoetigten Konzepte fuer EIN Symbol.

    Returns dict {concept: [compact-points]} oder None (kein CIK / Fetch-Fehler).
    """
    cmap = cik_map if cik_map is not None else get_cik_map()
    cik = cmap.get(symbol.upper())
    if not cik:
        return None
    try:
        facts = _http_get_json(_FACTS_URL.format(cik=int(cik))).get("facts", {})
    except Exception as e:
        log.warning("EDGAR-Fetch %s (CIK %s) fehlgeschlagen: %s", symbol, cik, e)
        return None
    ug = facts.get("us-gaap", {})
    dei = facts.get("dei", {})
    rec: dict = {}
    for c in USD_CONCEPTS:
        rec[c] = _compact(ug.get(c, {}).get("units", {}).get("USD", []))
    for c in SHARE_CONCEPTS:
        rec[c] = _compact(ug.get(c, {}).get("units", {}).get("shares", []))
    rec["SharesDEI"] = _compact(
        dei.get("EntityCommonStockSharesOutstanding", {}).get("units", {}).get("shares", [])
    )
    return rec


def refresh_edgar_facts(symbols: list, sleep: float = 0.13) -> dict:
    """Fetch + cache Fundamentaldaten fuer alle ``symbols`` -> edgar_facts.json.

    TRADING-NEUTRAL. Scheduler-Hook: woechentlich. ``sleep`` haelt das
    SEC-Rate-Limit (~10 req/s) ein. Returns {cached, missing, generated_at}.
    """
    from app.config_manager import save_json

    cmap = get_cik_map()
    out: dict = {}
    missing = 0
    for sym in symbols:
        rec = fetch_symbol_facts(sym, cmap)
        if rec is None:
            missing += 1
            continue
        out[sym.upper()] = rec
        time.sleep(sleep)
    payload = {"generated_at": datetime.utcnow().isoformat(), "symbols": out}
    try:
        save_json(_FACTS_CACHE_FILE, payload)
    except Exception as e:  # pragma: no cover
        log.error("edgar_facts.json schreiben fehlgeschlagen: %s", e)
    log.info("EDGAR-Refresh: %d gecacht, %d ohne CIK", len(out), missing)
    return {"cached": len(out), "missing": missing, "generated_at": payload["generated_at"]}


def load_facts() -> dict:
    """Lade den gecachten Fundamental-Datensatz {symbol: {concept: [...]}}."""
    from app.config_manager import load_json

    payload = load_json(_FACTS_CACHE_FILE)
    if isinstance(payload, dict):
        return payload.get("symbols", {})
    return {}


# ===========================================================================
# Point-in-Time-Accessoren — KEIN Look-Ahead: nur Werte mit filed <= asof.
# (Reine Funktionen, voll unit-testbar ohne Netzwerk.)
# ===========================================================================
def _parse(d: str) -> Optional[datetime]:
    try:
        return datetime.strptime(d[:10], "%Y-%m-%d")
    except Exception:
        return None


def annual_latest(points: list, asof: str) -> Optional[float]:
    """Letzter JAHRES-Flusswert (Periodendauer ~365T) mit filed <= asof."""
    best = None
    for p in points:
        s, e, f, v = p.get("s"), p.get("e"), p.get("f"), p.get("v")
        if not s or not e or not f or v is None or f > asof:
            continue
        d0, d1 = _parse(s), _parse(e)
        if not d0 or not d1:
            continue
        # v37dt Audit#5: bei gleichem End-Datum (Restatement) den ZULETZT
        # eingereichten Wert nehmen (filed-Tiebreak), nicht den erstgesehenen.
        if 350 <= (d1 - d0).days <= 380 and (best is None or (e, f) > (best[0], best[1])):
            best = (e, f, v)
    return best[2] if best else None


def latest_stock(points: list, asof: str) -> Optional[float]:
    """Letzter BESTANDS-Wert (Bilanz, instantan) mit filed <= asof."""
    series = [
        (p["e"], p["f"], p["v"])
        for p in points
        if p.get("e") and p.get("f") and p.get("v") is not None and p["f"] <= asof
    ]
    # v37dt Audit#5: Tiebreak nach (end, filed) statt (end, value) — vorher
    # waehlte series.sort() bei gleichem End-Datum den GROESSEREN Wert statt den
    # zuletzt eingereichten (Restatements wurden falschrum aufgeloest).
    series.sort(key=lambda t: (t[0], t[1]))
    return series[-1][2] if series else None


def annual_two(points: list, asof: str) -> tuple:
    """(letzter, vorjahres-) JAHRES-Wert fuer YoY-Aenderungen (EarnGrowth).

    Vorjahreswert nur wenn ein Jahres-Enddatum ~365T (+-70T) vor dem letzten
    existiert; sonst None.
    """
    annuals = []
    for p in points:
        s, e, f, v = p.get("s"), p.get("e"), p.get("f"), p.get("v")
        if not s or not e or not f or v is None or f > asof:
            continue
        d0, d1 = _parse(s), _parse(e)
        if d0 and d1 and 350 <= (d1 - d0).days <= 380:
            annuals.append((e, f, v))
    # v37dt Audit#5: filed-Tiebreak (s.o.) — bei gleichem End-Datum zuletzt
    # eingereichten Wert nehmen, nicht den groesseren.
    annuals.sort(key=lambda t: (t[0], t[1]))
    if not annuals:
        return None, None
    if len(annuals) < 2:
        return annuals[-1][2], None
    e_now, _f_now, v_now = annuals[-1]
    target = _parse(e_now) - timedelta(days=365)
    best = None
    for e, _f, v in annuals[:-1]:
        diff = abs((_parse(e) - target).days)
        if best is None or diff < best[0]:
            best = (diff, v)
    return v_now, (best[1] if best and best[0] <= 70 else None)


def shares_outstanding(rec: dict, asof: str) -> Optional[float]:
    """Beste verfuegbare Aktienzahl (point-in-time) fuer Market-Cap."""
    for k in ("CommonStockSharesOutstanding", "SharesDEI",
              "WeightedAverageNumberOfSharesOutstandingBasic"):
        v = latest_stock(rec.get(k, []), asof)
        if v and v > 0:
            return v
    return None


# ===========================================================================
# Health-Check
# ===========================================================================
def edgar_cache_status() -> dict:
    """Health: Alter + Umfang des Fundamental-Caches."""
    from app.config_manager import load_json

    payload = load_json(_FACTS_CACHE_FILE)
    if not isinstance(payload, dict) or not payload.get("symbols"):
        return {"ok": False, "reason": "edgar_facts.json fehlt/leer", "symbols": 0}
    n = len(payload.get("symbols", {}))
    gen = payload.get("generated_at", "")
    g = _parse(gen)
    age_days = (datetime.utcnow() - g).days if g else None
    ok = n > 0 and (age_days is None or age_days <= _MAX_CACHE_AGE_DAYS)
    return {"ok": ok, "symbols": n, "age_days": age_days, "generated_at": gen}
