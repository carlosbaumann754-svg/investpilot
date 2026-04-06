"""
Sentiment Analysis for InvestPilot.

Uses yfinance news feed for basic keyword-based sentiment scoring.
No external API keys required.
"""

import logging
import time

log = logging.getLogger("Sentiment")

try:
    import yfinance as yf
except ImportError:
    yf = None
    log.warning("yfinance nicht verfuegbar — Sentiment-Analyse deaktiviert")

# Module-level cache: symbol -> {"result": dict, "fetched_at": float}
_sentiment_cache = {}
_CACHE_TTL_SECONDS = 4 * 60 * 60  # 4 hours

# Keyword lists for simple sentiment scoring
_POSITIVE_WORDS = {"beat", "growth", "upgrade", "bullish", "profit", "strong",
                   "surpass", "outperform", "rally", "gain", "record", "boost",
                   "optimistic", "buy", "positive", "exceed", "revenue"}
_NEGATIVE_WORDS = {"miss", "downgrade", "bearish", "loss", "weak", "lawsuit",
                   "recall", "decline", "crash", "sell", "negative", "warning",
                   "cut", "risk", "debt", "fraud", "investigation", "layoff"}


def _score_text(text):
    """Score a text string based on positive/negative keyword presence.

    Returns a float between -1 and 1.
    """
    if not text:
        return 0.0

    words = set(text.lower().split())
    pos_count = len(words & _POSITIVE_WORDS)
    neg_count = len(words & _NEGATIVE_WORDS)

    total = pos_count + neg_count
    if total == 0:
        return 0.0

    return (pos_count - neg_count) / total


def get_sentiment(symbol):
    """Get sentiment score for a symbol based on yfinance news.

    Args:
        symbol: Ticker symbol (e.g. 'AAPL')

    Returns:
        dict with keys:
            score: float from -1 (very negative) to 1 (very positive)
            articles: int number of articles analyzed
            summary: str human-readable summary
    """
    now = time.time()

    # Check cache
    if symbol in _sentiment_cache:
        entry = _sentiment_cache[symbol]
        if now - entry["fetched_at"] < _CACHE_TTL_SECONDS:
            return entry["result"]

    neutral_result = {"score": 0.0, "articles": 0, "summary": "Keine Daten"}

    if yf is None:
        _sentiment_cache[symbol] = {"result": neutral_result, "fetched_at": now}
        return neutral_result

    try:
        ticker = yf.Ticker(symbol)
        news = ticker.news

        if not news:
            _sentiment_cache[symbol] = {"result": neutral_result, "fetched_at": now}
            return neutral_result

        scores = []
        for article in news:
            title = article.get("title", "")
            # Some yfinance versions have 'summary' or 'description'
            body = article.get("summary", "") or article.get("description", "")
            combined = f"{title} {body}"
            score = _score_text(combined)
            scores.append(score)

        if not scores:
            _sentiment_cache[symbol] = {"result": neutral_result, "fetched_at": now}
            return neutral_result

        avg_score = sum(scores) / len(scores)
        avg_score = max(-1.0, min(1.0, avg_score))

        if avg_score > 0.3:
            mood = "positiv"
        elif avg_score < -0.3:
            mood = "negativ"
        else:
            mood = "neutral"

        result = {
            "score": round(avg_score, 3),
            "articles": len(scores),
            "summary": f"{mood} ({len(scores)} Artikel, Score={avg_score:+.2f})",
        }

        _sentiment_cache[symbol] = {"result": result, "fetched_at": now}
        return result

    except Exception as e:
        log.warning(f"Sentiment-Analyse fehlgeschlagen fuer {symbol}: {e}")
        _sentiment_cache[symbol] = {"result": neutral_result, "fetched_at": now}
        return neutral_result


def get_market_sentiment():
    """Get overall market sentiment based on SPY and major holdings.

    Returns:
        dict with keys:
            score: float from -1 to 1
            components: dict of symbol -> score
            summary: str
    """
    market_symbols = ["SPY", "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA"]
    components = {}
    scores = []

    for symbol in market_symbols:
        try:
            result = get_sentiment(symbol)
            components[symbol] = result["score"]
            scores.append(result["score"])
        except Exception as e:
            log.debug(f"Market-Sentiment fuer {symbol} fehlgeschlagen: {e}")

    if not scores:
        return {
            "score": 0.0,
            "components": components,
            "summary": "Keine Markt-Sentiment-Daten verfuegbar",
        }

    avg = sum(scores) / len(scores)
    avg = max(-1.0, min(1.0, avg))

    if avg > 0.3:
        mood = "Markt positiv"
    elif avg < -0.3:
        mood = "Markt negativ"
    else:
        mood = "Markt neutral"

    return {
        "score": round(avg, 3),
        "components": components,
        "summary": f"{mood} (Score={avg:+.2f})",
    }
