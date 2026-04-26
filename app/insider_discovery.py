"""
Insider-Universe-Discovery (v35)
=================================

Scannt aktiv NEUE Aktien außerhalb unseres Universums auf außergewöhnliche
Insider-Cluster-Buys. Findet Symbole, die wir noch nicht beobachten.

Workflow:
1. Liest die Symbol-Watchlist aus app/market_scanner.ASSET_UNIVERSE
2. Scannt eine "Discovery-Watchlist" (S&P 500 minus bereits-im-Universum)
3. Berechnet insider_score fuer jedes (mit allen v32+v33-Filtern aktiv)
4. Top-N mit Score >= 3 -> data/insider_discovery.json + Dashboard-Karte

LIMITATIONEN:
- Finnhub Free-Tier: 60 req/min, mit 6h-Cache reicht das fuer ~500 Symbole/Tag
  bei 1x-taeglichem Run
- Wir scannen nur S&P 500 + Russell-1000 Top-200 (insgesamt ~600 Symbole)
- Bei Start mit leerem Cache braucht der erste Run ~20 Minuten

NUTZUNG:
- run_discovery() taeglich um 04:00 UTC (Pre-Premarket)
- Output via /api/insider-discovery Endpoint
- User entscheidet manuell ob ein Discovery-Symbol ins ASSET_UNIVERSE wandert
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("InsiderDiscovery")

DISCOVERY_PATH = Path(__file__).resolve().parent.parent / "data" / "insider_discovery.json"

# Liquide US-Mid/Large-Caps. Bewusst klein gehalten fuer Free-Tier.
# Sortiert nach erwarteter Insider-Aktivitaet (Healthcare/Tech haben mehr).
DISCOVERY_WATCHLIST = [
    # Healthcare/Biotech (oft Insider-Aktivitaet)
    "MRNA", "BNTX", "VRTX", "REGN", "GILD", "BIIB", "AMGN", "BMY", "CVS", "UNH",
    "ABBV", "LLY", "PFE", "JNJ", "MRK", "ELV", "HUM", "CI", "ZTS", "DHR",
    # Mid-Cap Tech
    "DDOG", "NET", "OKTA", "CRWD", "ZS", "PANW", "FTNT", "ANET", "MDB", "SNOW",
    "TEAM", "WDAY", "NOW", "ADSK", "INTU", "ADBE", "CTSH", "INFY", "ACN",
    # Financials
    "JPM", "BAC", "WFC", "GS", "MS", "C", "BLK", "SCHW", "AXP", "V", "MA",
    "PYPL", "SQ", "COIN", "HOOD", "SOFI", "AFRM", "UPST",
    # Energy/Utilities
    "XOM", "CVX", "COP", "SLB", "EOG", "OXY", "PXD", "MPC", "VLO", "PSX",
    "NEE", "DUK", "SO", "AEP", "EXC",
    # Consumer
    "WMT", "HD", "LOW", "TGT", "COST", "DG", "DLTR", "SBUX", "MCD", "CMG",
    "NKE", "LULU", "DECK", "RH", "WSM",
    # Industrials
    "BA", "CAT", "DE", "HON", "GE", "RTX", "LMT", "NOC", "GD", "MMM",
    "UPS", "FDX", "UNP", "CSX", "NSC",
    # Real Estate / Retail-favorites
    "RBLX", "DKNG", "DIS", "WBD", "PARA", "T", "VZ", "CMCSA",
]


def _load_discovery() -> dict:
    if not DISCOVERY_PATH.exists():
        return {"updated_at": None, "candidates": []}
    try:
        return json.loads(DISCOVERY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"updated_at": None, "candidates": []}


def _save_discovery(data: dict) -> None:
    try:
        DISCOVERY_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = DISCOVERY_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(DISCOVERY_PATH)
    except Exception as e:
        log.error(f"Discovery-Save fehlgeschlagen: {e}")


def run_discovery(min_score: int = 3, max_per_run: int = 80) -> dict:
    """Scanne Discovery-Watchlist nach High-Conviction Insider-Setups.

    Args:
        min_score: Schwelle fuer 'spannenden' Kandidaten (3 = Cluster + Volumen)
        max_per_run: Hard-Cap auf Anzahl Symbole, schuetzt Finnhub-Quota.

    Returns: {"updated_at": iso, "candidates": [{symbol, score, ...}]}
    """
    from app import finnhub_client
    from app.insider_signals import compute_insider_score

    if not finnhub_client.is_available():
        log.warning("Finnhub nicht verfuegbar — Discovery uebersprungen")
        return _load_discovery()

    # Symbole rausfiltern die schon im Bot-Universum sind (kein Discovery noetig)
    try:
        from app.market_scanner import ASSET_UNIVERSE
        in_universe = set(ASSET_UNIVERSE.keys())
    except Exception:
        in_universe = set()

    to_scan = [s for s in DISCOVERY_WATCHLIST if s not in in_universe][:max_per_run]
    candidates = []

    for sym in to_scan:
        try:
            txs = finnhub_client.fetch_insider_transactions(sym)
            score = compute_insider_score(
                sym, transactions=txs,
                quality_filter=True,
                detect_novelty=True,
                detect_contrarian=False,  # zu langsam fuer Bulk-Scan (yfinance)
            )
            if score >= min_score:
                # Cluster-Stats fuer Karte berechnen
                from datetime import timedelta
                from app.insider_signals import _aggregate_by_insider, DEFAULT_LOOKBACK_DAYS
                cutoff = datetime.utcnow() - timedelta(days=DEFAULT_LOOKBACK_DAYS)
                agg = _aggregate_by_insider(txs, cutoff, quality_filter=True)
                buyers = [n for n, d in agg.items() if d["net_shares"] > 0]
                volume = sum(d["net_usd"] for d in agg.values() if d["net_usd"] > 0)
                candidates.append({
                    "symbol": sym,
                    "score": score,
                    "n_unique_buyers": len(buyers),
                    "net_buy_volume_usd": round(volume, 0),
                    "top_buyers": buyers[:5],
                })
        except Exception as e:
            log.debug(f"Discovery-Scan {sym} fehler: {e}")
            continue
        time.sleep(0.05)  # Mini-Pause fuer Rate-Limit-Polster

    # Nach Score sortieren
    candidates.sort(key=lambda c: (c["score"], c["net_buy_volume_usd"]), reverse=True)

    result = {
        "updated_at": datetime.utcnow().isoformat(),
        "scanned": len(to_scan),
        "candidates": candidates,
    }
    _save_discovery(result)
    log.info(f"Insider-Discovery: {len(candidates)} Kandidaten >= score {min_score} "
             f"aus {len(to_scan)} gescannt")
    return result


def get_latest_discovery() -> dict:
    """Cached read fuer API-Endpoint."""
    return _load_discovery()
