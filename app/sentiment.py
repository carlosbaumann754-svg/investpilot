"""
Sentiment Analysis for InvestPilot — v12 LLM Edition.

Fetches news via yfinance (free) and classifies them via Claude Haiku
(ANTHROPIC_API_KEY).  Falls back to keyword scoring if the SDK or the
key are missing, so the bot keeps running in degraded mode.

Public API (unchanged):
  - get_sentiment(symbol) -> {score, articles, summary}
  - get_market_sentiment() -> {score, components, summary}

Cache: 4h per symbol to keep Claude API costs under ~$5/month.
"""

import json
import logging
import os
import time

log = logging.getLogger("Sentiment")

try:
    import yfinance as yf
except ImportError:
    yf = None
    log.warning("yfinance nicht verfuegbar — Sentiment-Analyse deaktiviert")

try:
    import anthropic
except ImportError:
    anthropic = None

_sentiment_cache = {}
_CACHE_TTL_SECONDS = 4 * 60 * 60  # 4h

# Fallback keyword lists (only used if Claude API unavailable)
_POSITIVE_WORDS = {"beat", "growth", "upgrade", "bullish", "profit", "strong",
                   "surpass", "outperform", "rally", "gain", "record", "boost",
                   "optimistic", "buy", "positive", "exceed", "revenue"}
_NEGATIVE_WORDS = {"miss", "downgrade", "bearish", "loss", "weak", "lawsuit",
                   "recall", "decline", "crash", "sell", "negative", "warning",
                   "cut", "risk", "debt", "fraud", "investigation", "layoff"}

_LLM_MODEL = "claude-haiku-4-5-20251001"
_MAX_HEADLINES = 8
_MAX_INPUT_CHARS = 2000


def _score_text_keyword(text):
    """Fallback keyword scorer: returns float in [-1, 1]."""
    if not text:
        return 0.0
    words = set(text.lower().split())
    pos = len(words & _POSITIVE_WORDS)
    neg = len(words & _NEGATIVE_WORDS)
    if pos + neg == 0:
        return 0.0
    return (pos - neg) / (pos + neg)


def _get_anthropic_client():
    """Return an anthropic client or None if unavailable."""
    if anthropic is None:
        return None
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None
    try:
        return anthropic.Anthropic(api_key=api_key)
    except Exception as e:
        log.warning(f"Anthropic-Client-Init fehlgeschlagen: {e}")
        return None


def _score_with_llm(symbol, headlines):
    """Use Claude Haiku to classify a batch of headlines for one symbol.

    Returns dict {score: float in [-1,1], label: str, confidence: float,
    rationale: str} or None on failure.
    """
    client = _get_anthropic_client()
    if client is None or not headlines:
        return None

    # Trim headlines so we stay well inside Haiku's budget
    joined = "\n".join(f"- {h[:200]}" for h in headlines[:_MAX_HEADLINES])
    joined = joined[:_MAX_INPUT_CHARS]

    prompt = (
        f"You are a financial news sentiment classifier for the trading bot.\n"
        f"Asset: {symbol}\n"
        f"Recent headlines:\n{joined}\n\n"
        f"Classify the OVERALL sentiment for holding this asset over the next 5 trading days.\n"
        f"Ignore generic market noise and click-bait.\n"
        f"Return ONLY valid JSON with this exact schema (no prose, no markdown fences):\n"
        f'{{"score": -1.0 to 1.0, "label": "bullish"|"bearish"|"neutral"|"noise", '
        f'"confidence": 0.0 to 1.0, "rationale": "<=15 words"}}'
    )

    try:
        response = client.messages.create(
            model=_LLM_MODEL,
            max_tokens=200,
            system="You are a concise, disciplined financial sentiment classifier. Output valid JSON only.",
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        # Strip markdown fences if Claude adds them despite instructions
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        data = json.loads(text)
        score = float(data.get("score", 0.0))
        score = max(-1.0, min(1.0, score))
        return {
            "score": score,
            "label": data.get("label", "neutral"),
            "confidence": float(data.get("confidence", 0.0)),
            "rationale": data.get("rationale", ""),
        }
    except Exception as e:
        log.warning(f"LLM-Sentiment fuer {symbol} fehlgeschlagen: {e}")
        return None


def _fetch_headlines(symbol):
    """Pull a list of headline strings via yfinance.  Empty list on error."""
    if yf is None:
        return []
    try:
        ticker = yf.Ticker(symbol)
        news = ticker.news or []
        headlines = []
        for article in news:
            title = article.get("title") or ""
            summary = article.get("summary") or article.get("description") or ""
            combined = f"{title}. {summary}".strip(". ")
            if combined:
                headlines.append(combined)
        return headlines
    except Exception as e:
        log.debug(f"Headline-Fetch fuer {symbol} fehlgeschlagen: {e}")
        return []


def get_sentiment(symbol):
    """Return sentiment dict for a symbol.

    Shape:
        {score: float -1..1, articles: int, summary: str,
         label?: str, confidence?: float, source: "llm"|"keyword"|"none"}
    """
    now = time.time()

    cached = _sentiment_cache.get(symbol)
    if cached and now - cached["fetched_at"] < _CACHE_TTL_SECONDS:
        return cached["result"]

    neutral = {"score": 0.0, "articles": 0, "summary": "Keine Daten", "source": "none"}

    headlines = _fetch_headlines(symbol)
    if not headlines:
        _sentiment_cache[symbol] = {"result": neutral, "fetched_at": now}
        return neutral

    # Try LLM first
    llm_result = _score_with_llm(symbol, headlines)
    if llm_result is not None:
        score = llm_result["score"]
        label = llm_result["label"]
        mood_de = {"bullish": "bullisch", "bearish": "baerisch",
                   "neutral": "neutral", "noise": "rauschen"}.get(label, label)
        result = {
            "score": round(score, 3),
            "articles": len(headlines),
            "label": label,
            "confidence": round(llm_result["confidence"], 2),
            "rationale": llm_result["rationale"],
            "summary": f"{mood_de} ({len(headlines)} Artikel, LLM-Score={score:+.2f}, "
                       f"Conf={llm_result['confidence']:.2f})",
            "source": "llm",
        }
        _sentiment_cache[symbol] = {"result": result, "fetched_at": now}
        return result

    # Fallback: keyword scoring (same logic as pre-v12)
    scores = [_score_text_keyword(h) for h in headlines]
    if not scores:
        _sentiment_cache[symbol] = {"result": neutral, "fetched_at": now}
        return neutral
    avg = max(-1.0, min(1.0, sum(scores) / len(scores)))
    mood = "positiv" if avg > 0.3 else "negativ" if avg < -0.3 else "neutral"
    result = {
        "score": round(avg, 3),
        "articles": len(scores),
        "summary": f"{mood} ({len(scores)} Artikel, Keyword-Score={avg:+.2f})",
        "source": "keyword",
    }
    _sentiment_cache[symbol] = {"result": result, "fetched_at": now}
    return result


def get_market_sentiment():
    """Aggregated market sentiment across major names."""
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
        return {"score": 0.0, "components": components,
                "summary": "Keine Markt-Sentiment-Daten verfuegbar"}

    avg = max(-1.0, min(1.0, sum(scores) / len(scores)))
    mood = "Markt positiv" if avg > 0.3 else "Markt negativ" if avg < -0.3 else "Markt neutral"
    return {
        "score": round(avg, 3),
        "components": components,
        "summary": f"{mood} (Score={avg:+.2f})",
    }
