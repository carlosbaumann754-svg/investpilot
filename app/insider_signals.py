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

# Score-Caps (v33 erweitert auf +5 fuer Pattern-Detection)
MAX_POSITIVE_SCORE = 5
MAX_NEGATIVE_SCORE = -2

# v32 — Transaction-Code Quality-Filter
# Form 4 hat ~25 Transaction-Codes; nur diese sind echte Conviction-Signale:
# https://www.sec.gov/about/forms/form4data.pdf
TX_CODES_REAL_PURCHASE = {"P"}      # Open-Market Purchase = volles Signal
TX_CODES_REAL_SALE = {"S"}          # Open-Market Sale (regulaer)
TX_CODES_NOISE = {                  # Compensation/Awards/Exercises = NICHT als Signal werten
    "A",   # Award/Grant
    "M",   # Options Exercise
    "G",   # Gift
    "F",   # Tax Withholding
    "I",   # Discretionary
    "L",   # Small Acquisition
    "W",   # Will/Inheritance
}

# v33 — Pattern-Detection Schwellwerte
NOVELTY_LOOKBACK_DAYS = 730   # 2 Jahre — "erstmaliger Buy seit X" Erkennung
NOVELTY_BONUS = 2             # Score-Bonus wenn Insider zum ersten Mal seit 2J kauft
CONTRARIAN_DROP_PCT = 15.0    # Aktie >= 15% Drawdown in 30 Tagen
CONTRARIAN_BONUS = 2          # Score-Bonus fuer Insider-Buy nach Crash


def _is_signal_transaction(tx: dict, *, quality_filter: bool) -> bool:
    """v32: Pruefe ob Transaction echtes Conviction-Signal ist.

    Wenn quality_filter=True: nur Open-Market Purchases (P) und Sales (S) zaehlen.
    Awards, Options-Exercises, Gifts, Tax-Withholdings werden ausgefiltert.
    """
    if not quality_filter:
        return True
    code = (tx.get("transactionCode") or "").strip().upper()
    if not code:
        return True  # Code fehlt -> nicht ausfiltern (data quality issue)
    if code in TX_CODES_NOISE:
        return False
    return code in TX_CODES_REAL_PURCHASE or code in TX_CODES_REAL_SALE


def _aggregate_by_insider(
    transactions: list[dict],
    cutoff_date: datetime,
    *,
    quality_filter: bool = False,
) -> dict[str, dict]:
    """Gruppiere Transaktionen pro Insider innerhalb des lookback-Fensters.

    Args:
        quality_filter: Wenn True (v32), nur Open-Market-Trades zaehlen.

    Returns: {insider_name: {"net_shares": int, "net_usd": float, "n_tx": int}}
    """
    by_insider: dict[str, dict] = defaultdict(
        lambda: {"net_shares": 0, "net_usd": 0.0, "n_tx": 0}
    )

    for tx in transactions:
        if not _is_signal_transaction(tx, quality_filter=quality_filter):
            continue
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


def _detect_novelty_buyers(
    transactions: list[dict],
    cutoff_date: datetime,
    *,
    novelty_lookback_days: int = NOVELTY_LOOKBACK_DAYS,
) -> list[str]:
    """v33: Insider die im aktuellen 30-Tage-Fenster gekauft haben UND
    davor mindestens 2 Jahre lang KEINEN Open-Market-Buy hatten.

    Liefert Namen von Insidern, deren aktueller Buy ein "First-Buy-in-2J" ist.
    Diese sind historisch deutlich aussagekraeftiger als Routine-Kaeufer.
    """
    novelty_cutoff = cutoff_date - timedelta(days=novelty_lookback_days)

    # Wer hat im aktuellen Fenster (>= cutoff_date) ein P-Trade?
    current_buyers: dict[str, datetime] = {}
    historical_buyers: set[str] = set()

    for tx in transactions:
        code = (tx.get("transactionCode") or "").strip().upper()
        if code != "P":
            continue
        change = tx.get("change", 0) or 0
        try:
            if int(change) <= 0:
                continue
        except (ValueError, TypeError):
            continue
        date_str = tx.get("transactionDate") or tx.get("filingDate") or ""
        if not date_str:
            continue
        try:
            tx_date = datetime.fromisoformat(date_str[:10])
        except (ValueError, TypeError):
            continue
        name = (tx.get("name") or "UNKNOWN").strip().upper()

        if tx_date >= cutoff_date:
            # Im aktuellen 30-Tage-Fenster
            if name not in current_buyers or tx_date > current_buyers[name]:
                current_buyers[name] = tx_date
        elif tx_date >= novelty_cutoff:
            # In den 2 Jahren VOR dem aktuellen Fenster
            historical_buyers.add(name)

    # Novelty = im aktuellen Fenster gekauft UND nicht in den 2J davor
    return [name for name in current_buyers if name not in historical_buyers]


def _detect_contrarian_setup(
    symbol: str,
    *,
    drop_pct_threshold: float = CONTRARIAN_DROP_PCT,
) -> bool:
    """v33: Pruefe ob die Aktie >= drop_pct_threshold% in den letzten 30 Tagen
    verloren hat. Liefert True bei Crash-Setup -> Insider-Buy waere contrarian.

    Nutzt yfinance (bereits Bot-Dependency). Bei Fehler: False (kein Bonus).
    """
    try:
        import yfinance as yf
        df = yf.download(symbol, period="2mo", interval="1d",
                         progress=False, auto_adjust=True, threads=False)
        if df is None or df.empty or len(df) < 20:
            return False
        # Hoechster Close in letzten 30 Trading-Days vs. aktuell
        recent = df["Close"].tail(30)
        if hasattr(recent, "values"):
            vals = [float(v) for v in recent.values.flatten() if v == v]  # NaN-filter
        else:
            vals = list(recent)
        if not vals:
            return False
        peak = max(vals)
        current = vals[-1]
        if peak <= 0:
            return False
        drawdown_pct = (peak - current) / peak * 100.0
        return drawdown_pct >= drop_pct_threshold
    except Exception as e:
        log.debug(f"Contrarian-Check fuer {symbol} fehlgeschlagen: {e}")
        return False


def compute_insider_score(
    symbol: str,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    *,
    transactions: Optional[list[dict]] = None,
    quality_filter: bool = False,         # v32 Schalter
    detect_novelty: bool = False,          # v33 Schalter A
    detect_contrarian: bool = False,       # v33 Schalter B (langsam — yfinance)
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
    by_insider = _aggregate_by_insider(transactions, cutoff, quality_filter=quality_filter)

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

    # --- v33 Pattern-Bonusse (additiv on top, nur bei Buy-Setup) ---
    if score > 0:
        if detect_novelty:
            novelty_buyers = _detect_novelty_buyers(transactions, cutoff)
            if novelty_buyers:
                score += NOVELTY_BONUS  # +2

        if detect_contrarian and _detect_contrarian_setup(symbol):
            score += CONTRARIAN_BONUS  # +2

    # --- NEGATIVE-Pfad: starke Sell-Cluster (asymmetrisch hoehere Schwellen) ---
    if score <= 0:
        is_sell_cluster = len(net_sellers) >= MIN_UNIQUE_SELLERS_FOR_PENALTY
        is_big_sell = total_net_sell_usd >= MIN_NET_SELL_USD_FOR_PENALTY

        if is_sell_cluster and is_big_sell:
            score = -2
        elif is_big_sell and total_net_sell_usd >= 5 * MIN_NET_SELL_USD_FOR_PENALTY:
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
