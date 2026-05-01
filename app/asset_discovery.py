"""
InvestPilot - Asset Discovery
Durchsucht woechentlich eToro nach neuen handelbaren Assets.
Findet neue Aktien, ETFs, Crypto, Rohstoffe etc. und fuegt
vielversprechende automatisch zum Scanner-Universum hinzu.
"""

import logging
import time
from datetime import datetime, timezone

from app.config_manager import load_config, load_json, save_json
from app.etoro_client import EtoroClient
from app.broker_base import get_broker

log = logging.getLogger("AssetDiscovery")

# Breite Suchbegriffe um moeglichst viele Assets zu finden
DISCOVERY_QUERIES = [
    # Sektoren
    "technology", "healthcare", "finance", "energy", "consumer",
    "industrial", "materials", "utilities", "real estate",
    # Trends
    "AI", "artificial intelligence", "cloud", "cybersecurity",
    "electric vehicle", "clean energy", "semiconductor", "fintech",
    "biotech", "quantum", "robotics", "space", "blockchain",
    # Regionen
    "china", "japan", "europe", "emerging", "india", "brazil",
    # Asset-Typen
    "ETF", "index", "bond", "commodity", "gold", "silver", "oil",
    # Crypto
    "bitcoin", "ethereum", "crypto", "defi",
    # Populaere Einzeltitel (fuer Neulistungen)
    "IPO", "SPAC",
    # Forex
    "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD",
]

# yfinance Symbol-Mapping fuer gaengige eToro Asset-Klassen
YF_SUFFIX_MAP = {
    "Crypto": "-USD",
    "Currencies": "=X",
}


def _get_known_etoro_ids():
    """Hole alle bereits bekannten eToro Instrument IDs."""
    from app.market_scanner import ASSET_UNIVERSE
    known = set()
    for asset in ASSET_UNIVERSE.values():
        known.add(asset["etoro_id"])

    # Auch entdeckte Assets aus vorherigen Scans
    discovered = load_json("discovered_assets.json") or []
    for asset in discovered:
        known.add(asset.get("etoro_id", 0))

    return known


def _guess_yfinance_symbol(symbol, asset_class, exchange):
    """Versuche ein yfinance-kompatibles Symbol abzuleiten."""
    if not symbol:
        return None

    # Crypto
    if asset_class and "crypto" in asset_class.lower():
        base = symbol.replace("/", "").replace("USD", "").strip()
        return f"{base}-USD"

    # Forex
    if asset_class and "currenc" in asset_class.lower():
        clean = symbol.replace("/", "")
        return f"{clean}=X"

    # Stocks auf nicht-US Boersen
    exchange_suffixes = {
        "London": ".L",
        "Frankfurt": ".F",
        "Paris": ".PA",
        "Amsterdam": ".AS",
        "Tokyo": ".T",
        "Hong Kong": ".HK",
        "Toronto": ".TO",
        "Sydney": ".AX",
    }
    if exchange:
        for ex_name, suffix in exchange_suffixes.items():
            if ex_name.lower() in exchange.lower():
                return f"{symbol}{suffix}"

    # US Stocks - Symbol direkt verwenden
    return symbol


def _classify_asset(asset_class_name):
    """eToro Asset-Klasse in unsere Kategorien uebersetzen."""
    if not asset_class_name:
        return "stocks"
    name = asset_class_name.lower()
    if "crypto" in name:
        return "crypto"
    if "currenc" in name or "forex" in name:
        return "forex"
    if "commodit" in name:
        return "commodities"
    if "etf" in name:
        return "etf"
    if "indic" in name or "index" in name:
        return "indices"
    return "stocks"


def discover_new_assets():
    """Durchsuche eToro nach neuen handelbaren Assets."""
    config = load_config()
    if not config:
        log.warning("Config nicht geladen - Discovery abgebrochen")
        return []

    client = get_broker(config, readonly=True)
    if not client.configured:
        log.warning("eToro Client nicht konfiguriert - Discovery abgebrochen")
        return []

    known_ids = _get_known_etoro_ids()
    log.info(f"Bekannte Assets: {len(known_ids)}")
    log.info(f"Starte Discovery mit {len(DISCOVERY_QUERIES)} Suchbegriffen...")

    new_assets = {}  # etoro_id -> asset dict (dedupliziert)

    for i, query in enumerate(DISCOVERY_QUERIES):
        try:
            results = client.search_instrument(query)
            for item in results:
                etoro_id = item.get("id")
                if not etoro_id or etoro_id in known_ids or etoro_id in new_assets:
                    continue

                asset_class = _classify_asset(item.get("asset_class", ""))
                yf_symbol = _guess_yfinance_symbol(
                    item.get("symbol", ""),
                    item.get("asset_class", ""),
                    item.get("exchange", ""),
                )

                new_assets[etoro_id] = {
                    "etoro_id": etoro_id,
                    "name": item.get("name", "Unknown"),
                    "symbol": item.get("symbol", ""),
                    "yf_symbol": yf_symbol,
                    "asset_class": asset_class,
                    "exchange": item.get("exchange", ""),
                    "discovered": datetime.now().isoformat(),
                    "query": query,
                    "added_to_scanner": False,
                }

            if (i + 1) % 5 == 0:
                log.info(f"  Fortschritt: {i+1}/{len(DISCOVERY_QUERIES)} Queries, {len(new_assets)} neue gefunden")

            time.sleep(0.5)  # Rate Limiting

        except Exception as e:
            log.warning(f"  Suche '{query}' fehlgeschlagen: {e}")
            continue

    log.info(f"Discovery abgeschlossen: {len(new_assets)} neue Assets gefunden")
    return list(new_assets.values())


def evaluate_new_assets(new_assets, max_evaluate=30):
    """Bewerte neue Assets mit technischer Analyse und fuege die besten zum Scanner hinzu."""
    if not new_assets:
        return []

    try:
        from app.market_scanner import analyze_single_asset, score_asset
    except ImportError:
        log.warning("Market Scanner nicht verfuegbar - Bewertung uebersprungen")
        return []

    log.info(f"Bewerte {min(len(new_assets), max_evaluate)} neue Assets...")
    evaluated = []

    for asset in new_assets[:max_evaluate]:
        if not asset.get("yf_symbol"):
            continue

        try:
            asset_info = {
                "etoro_id": asset["etoro_id"],
                "yf": asset["yf_symbol"],
                "class": asset["asset_class"],
                "name": asset["name"],
            }
            analysis = analyze_single_asset(asset["symbol"], asset_info)
            if analysis:
                score = score_asset(analysis)
                asset["score"] = score
                asset["analysis"] = {
                    "rsi": analysis.get("rsi"),
                    "momentum_5d": analysis.get("momentum_5d"),
                    "momentum_20d": analysis.get("momentum_20d"),
                    "price": analysis.get("price"),
                }
                evaluated.append(asset)

                if score >= 15:
                    log.info(f"  INTERESSANT: {asset['symbol']} ({asset['name']}) "
                             f"Score={score:.1f}, Klasse={asset['asset_class']}")

            time.sleep(0.3)

        except Exception as e:
            log.debug(f"  Bewertung fehlgeschlagen fuer {asset.get('symbol')}: {e}")
            continue

    # Sortiere nach Score
    evaluated.sort(key=lambda x: x.get("score", 0), reverse=True)
    log.info(f"Bewertung abgeschlossen: {len(evaluated)} Assets bewertet")
    return evaluated


def add_to_scanner_universe(evaluated_assets, min_score=15, max_add=10):
    """Fuege die besten neuen Assets zum Scanner-Universum hinzu."""
    from app.market_scanner import ASSET_UNIVERSE

    candidates = [a for a in evaluated_assets if a.get("score", 0) >= min_score]
    added = []

    for asset in candidates[:max_add]:
        symbol = asset.get("symbol", "").upper().replace(" ", "_")
        if not symbol or symbol in ASSET_UNIVERSE:
            continue

        # Zum ASSET_UNIVERSE hinzufuegen (Runtime)
        ASSET_UNIVERSE[symbol] = {
            "etoro_id": asset["etoro_id"],
            "yf": asset["yf_symbol"],
            "class": asset["asset_class"],
            "name": asset["name"],
        }
        asset["added_to_scanner"] = True
        added.append(asset)
        log.info(f"  Zum Scanner hinzugefuegt: {symbol} ({asset['name']}) Score={asset['score']:.1f}")

    return added


def run_weekly_discovery():
    """Fuehre den kompletten woechentlichen Discovery-Zyklus aus."""
    log.info("")
    log.info("=" * 55)
    log.info("ASSET DISCOVERY - Woechentliche eToro-Durchsuchung")
    log.info("=" * 55)

    # 1. Neue Assets entdecken
    log.info("\n[1/4] eToro durchsuchen...")
    new_assets = discover_new_assets()

    if not new_assets:
        log.info("Keine neuen Assets gefunden - fertig")
        return {"new_found": 0, "evaluated": 0, "added": 0}

    # 2. Technisch bewerten
    log.info("\n[2/4] Neue Assets bewerten...")
    evaluated = evaluate_new_assets(new_assets)

    # 3. Beste zum Scanner hinzufuegen
    log.info("\n[3/4] Beste Assets zum Scanner hinzufuegen...")
    added = add_to_scanner_universe(evaluated)

    # 4. Ergebnisse speichern
    log.info("\n[4/4] Ergebnisse speichern...")
    existing = load_json("discovered_assets.json") or []
    existing.extend(new_assets)
    # Max 500 Eintraege behalten (aelteste loeschen)
    if len(existing) > 500:
        existing = existing[-500:]
    save_json("discovered_assets.json", existing)

    result = {
        "timestamp": datetime.now().isoformat(),
        "new_found": len(new_assets),
        "evaluated": len(evaluated),
        "added": len(added),
        "added_assets": [
            {"symbol": a["symbol"], "name": a["name"], "score": a.get("score", 0), "class": a["asset_class"]}
            for a in added
        ],
        "top_10": [
            {"symbol": a["symbol"], "name": a["name"], "score": a.get("score", 0), "class": a["asset_class"]}
            for a in evaluated[:10]
        ],
    }
    save_json("discovery_result.json", result)

    log.info(f"\nDiscovery abgeschlossen:")
    log.info(f"  Neue Assets gefunden: {len(new_assets)}")
    log.info(f"  Davon bewertet: {len(evaluated)}")
    log.info(f"  Zum Scanner hinzugefuegt: {len(added)}")
    log.info("=" * 55)

    return result


def is_friday_discovery_time():
    """Pruefe ob es Freitag zwischen 17:00-17:05 UTC ist (vor dem Report).

    UTC, damit das Trigger-Fenster deckungsgleich mit den GitHub-Action-Crons
    bleibt und nicht durch DST-Wechsel oder Container-TZ verschoben wird.
    """
    now = datetime.now(timezone.utc)
    return now.weekday() == 4 and now.hour == 17 and now.minute < 5
