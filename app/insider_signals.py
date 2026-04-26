"""
Insider-Trading-Signal (CEOWatcher-Aequivalent via Finnhub Free-Tier)
======================================================================

Wissenschaftlich validierter Alpha-Faktor: Wenn mehrere Insider (CEO/CFO/
Direktoren) gleichzeitig (in einem Cluster) Aktien des eigenen Unternehmens
KAUFEN, ist das ein bullishes Signal mit historisch ~5-10% p.a. Edge ueber
3-12 Monate (Lakonishok/Lee/Zhu Studien).

DESIGN-PRINZIPIEN:
1. **OFF by default**: insider_signal_enabled in config.json muss aktiv geschaltet
   werden. Default false. Gruund: jede neue Alpha-Quelle aendert die Strategie-
   Mischung -> bestehender Backtest (Sharpe 3.5) waere nicht mehr valide.
   Erst nach eigenem Backtest-Run einschalten.
2. **Additiv, nicht ersetzend**: Score-Booster (-2..+3) on top of bestehender
   Scanner-Score. Existing Logik bleibt unangetastet.
3. **Conservative**: Nur cluster-buys (>= 3 unique Insider) zaehlen positiv.
   Einzel-Insider-Buys koennten Routine-Aktienprogramme oder steueroptimale
   Aktionen sein und sind als Signal zu schwach.
4. **Verkaufs-Asymmetrie**: Sells sind weniger aussagekraeftig als Buys
   (Diversifikation, Steuern, Optionsausuebung). Negativ-Score nur bei
   sehr starken Sell-Clustern.

WARUM SELBST BAUEN STATT CEOWatcher?
- Daten kommen via Finnhub /stock/insider-transactions (Free-Tier, 60 req/min)
- CEOWatcher.com hat kein Public API, waere Scrape-Fragil
- Eigene Logik ist tunable und backtestbar
- Kein neuer Vendor-Lock-in

USAGE:
  from app.insider_signals import compute_insider_score
  score = compute_insider_score("NVDA")  # int -2..+3
  # in scanner.py: total_score += score (wenn config-flag an)
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

log = logging.getLogger("InsiderSignals")

# Schwellwerte — bewusst konservativ. Nach Backtest tunable.
DEFAULT_LOOKBACK_DAYS = 30
MIN_UNIQUE_INSIDERS_FOR_CLUSTER = 3
MIN_NET_BUY_USD_FOR_VOLUME_BONUS = 500_000  # $500k netto-Kauf-Volumen
MIN_NET_SELL_USD_FOR_PENALTY = 2_000_000    # $2M netto-Verkauf bevor wir negativ scoren
MIN_UNIQUE_SELLERS_FOR_PENALTY = 5          # Mehr Schwelle als Buys (siehe Asymmetrie)

# Score-Caps
MAX_POSITIVE_SCORE = 3
MAX_NEGATIVE_SCORE = -2


def _aggregate_by_insider(
    transactions: list[dict],
    cutoff_date: datetime,
) -> dict[str, dict]:
    """Gruppiere Transaktionen pro Insider innerhalb des lookback-Fensters.

    Returns: {insider_name: {"net_shares": int, "net_usd": float, "n_tx": int}}
    """
    by_insider: dict[str, dict] = defaultdict(
        lambda: {"net_shares": 0, "net_usd": 0.0, "n_tx": 0}
    )

    for tx in transactions:
        date_str = tx.get("transactionDate") or tx.get("filingDate") or ""
        if not date_str:
            continue
        try:
            tx_date = datetime.fromisoformat(date_str[:10])
        except (ValueError, TypeError):
            continue
        if tx_date < cutoff_date:
            continue

        change = tx.get("change", 0) or 0
        price = tx.get("transactionPrice", 0.0) or 0.0
        name = (tx.get("name") or "UNKNOWN").strip().upper()

        try:
            change_int = int(change)
            price_float = float(price)
        except (ValueError, TypeError):
            continue

        by_insider[name]["net_shares"] += change_int
        by_insider[name]["net_usd"] += change_int * price_float
        by_insider[name]["n_tx"] += 1

    return dict(by_insider)


def compute_insider_score(
    symbol: str,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    *,
    transactions: Optional[list[dict]] = None,
) -> int:
    """Berechne Insider-Score fuer ein Symbol.

    Returns: int in [MAX_NEGATIVE_SCORE, MAX_POSITIVE_SCORE]
        +3: Stark bullish (Cluster-Buy + grosses Volumen)
        +2: Bullish (Cluster-Buy)
        +1: Schwach bullish (Volumen-Bonus ohne Cluster, oder grenzwertiger Cluster)
         0: Neutral / keine Daten / ausgewogene Trades
        -1: Schwach bearish (sehr grosses Sell-Volumen)
        -2: Bearish (Sell-Cluster + grosses Volumen)

    Args:
        symbol: Ticker (z.B. "NVDA")
        lookback_days: Zeitfenster fuer Cluster-Erkennung
        transactions: Optional vorgefetchte Daten — sonst wird Finnhub angefragt.
                      Erlaubt Tests ohne Network und reuse zwischen mehreren Aufrufen.
    """
    if transactions is None:
        try:
            from app import finnhub_client
            if not finnhub_client.is_available():
                return 0
            transactions = finnhub_client.fetch_insider_transactions(symbol)
        except Exception as e:
            log.debug(f"Insider-Fetch fuer {symbol} fehlgeschlagen: {e}")
            return 0

    if not transactions:
        return 0

    cutoff = datetime.utcnow() - timedelta(days=lookback_days)
    by_insider = _aggregate_by_insider(transactions, cutoff)

    if not by_insider:
        return 0

    net_buyers = [n for n, d in by_insider.items() if d["net_shares"] > 0]
    net_sellers = [n for n, d in by_insider.items() if d["net_shares"] < 0]
    total_net_buy_usd = sum(d["net_usd"] for d in by_insider.values() if d["net_usd"] > 0)
    total_net_sell_usd = -sum(d["net_usd"] for d in by_insider.values() if d["net_usd"] < 0)

    score = 0

    # --- POSITIVE-Pfad: Cluster-Buys ---
    is_cluster_buy = len(net_buyers) >= MIN_UNIQUE_INSIDERS_FOR_CLUSTER
    has_volume_bonus = total_net_buy_usd >= MIN_NET_BUY_USD_FOR_VOLUME_BONUS

    if is_cluster_buy and has_volume_bonus:
        score = 3
    elif is_cluster_buy:
        score = 2
    elif has_volume_bonus:
        score = 1

    # --- NEGATIVE-Pfad: starke Sell-Cluster (asymmetrisch hoehere Schwellen) ---
    if score <= 0:  # nur wenn nicht schon klar bullish
        is_sell_cluster = len(net_sellers) >= MIN_UNIQUE_SELLERS_FOR_PENALTY
        is_big_sell = total_net_sell_usd >= MIN_NET_SELL_USD_FOR_PENALTY

        if is_sell_cluster and is_big_sell:
            score = -2
        elif is_big_sell and total_net_sell_usd >= 5 * MIN_NET_SELL_USD_FOR_PENALTY:
            # Extreme Sell-Volumes auch ohne Cluster (z.B. Founder-Exit)
            score = -1

    return max(MAX_NEGATIVE_SCORE, min(MAX_POSITIVE_SCORE, score))


def is_enabled(config: dict) -> bool:
    """Helper: Pruefe ob Insider-Signal in der Config aktiviert ist.

    DEFAULT FALSE — das ist Absicht. Erst nach eigenem Backtest aktivieren.
    Mapping: config['scanner']['insider_signal_enabled'] = true
    """
    if not isinstance(config, dict):
        return False
    scanner_cfg = config.get("scanner") or {}
    return bool(scanner_cfg.get("insider_signal_enabled", False))
