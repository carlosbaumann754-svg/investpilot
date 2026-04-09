"""
Sentiment Analysis for InvestPilot — v12.1 Multi-Source Edition.

Liefert Sentiment-Scores pro Symbol. Funktioniert vollstaendig ohne
Anthropic-Tokens dank 3-stufiger Scorer-Cascade + multiplen News-Quellen.

News-Quellen (Prioritaet):
  1. Finnhub /news-sentiment      — direkt gescored (wenn FINNHUB_API_KEY)
  2. Finnhub /company-news        — Headlines -> lokaler Scorer
  3. yfinance ticker.news         — Headlines -> lokaler Scorer

Scorer-Cascade (Prioritaet):
  1. Claude Haiku LLM    — wenn ANTHROPIC_API_KEY valide & Guthaben
  2. VADER               — lexikon-basiert, offline, gratis, gut
  3. Keyword-Fallback    — primitive Wortliste, letzter Rettungsanker

Public API (stabil):
  - get_sentiment(symbol) -> {score, articles, summary, source, ...}
  - get_market_sentiment() -> {score, components, summary}

Cache: 4h pro Symbol.
"""

import json
import logging
import os
import time

log = logging.getLogger("Sentiment")

# ------------------------------------------------------------
# Optional Dependencies
# ------------------------------------------------------------
try:
    import yfinance as yf
except ImportError:
    yf = None
    log.warning("yfinance nicht verfuegbar — yfinance-News-Fallback deaktiviert")

try:
    import anthropic
except ImportError:
    anthropic = None

try:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
    _vader_analyzer = SentimentIntensityAnalyzer()
except Exception as e:
    _vader_analyzer = None
    log.info(f"VADER nicht verfuegbar: {e}")

try:
    from app import finnhub_client
except ImportError:
    finnhub_client = None

# ------------------------------------------------------------
# Cache & Constants
# ------------------------------------------------------------
_sentiment_cache: dict = {}
_CACHE_TTL_SECONDS = 4 * 60 * 60  # 4h

# Fallback keyword lists (letzter Rettungsanker)
_POSITIVE_WORDS = {"beat", "growth", "upgrade", "bullish", "profit", "strong",
                   "surpass", "outperform", "rally", "gain", "record", "boost",
                   "optimistic", "buy", "positive", "exceed", "revenue"}
_NEGATIVE_WORDS = {"miss", "downgrade", "bearish", "loss", "weak", "lawsuit",
                   "recall", "decline", "crash", "sell", "negative", "warning",
                   "cut", "risk", "debt", "fraud", "investigation", "layoff"}

_LLM_MODEL = "claude-haiku-4-5-20251001"
_MAX_HEADLINES = 8
_MAX_INPUT_CHARS = 2000


# ============================================================
# Scorers (Cascade)
# ============================================================

def _score_text_keyword(text: str) -> float:
    """Stufe 3 (Fallback): primitive Keyword-Methode. Returns [-1, 1]."""
    if not text:
        return 0.0
    words = set(text.lower().split())
    pos = len(words & _POSITIVE_WORDS)
    neg = len(words & _NEGATIVE_WORDS)
    if pos + neg == 0:
        return 0.0
    return (pos - neg) / (pos + neg)


def _score_headlines_vader(headlines: list) -> dict | None:
    """Stufe 2: VADER lexikon-basiert. Schnell, offline, gratis.

    Returns dict {score, label, confidence, rationale} oder None.
    """
    if _vader_analyzer is None or not headlines:
        return None
    try:
        compound_scores = []
        for h in headlines[:_MAX_HEADLINES]:
            vs = _vader_analyzer.polarity_scores(h[:300])
            compound_scores.append(vs["compound"])  # bereits -1..1
        if not compound_scores:
            return None
        avg = sum(compound_scores) / len(compound_scores)
        avg = max(-1.0, min(1.0, avg))
        # Confidence = Magnitude (|avg|) * Headline-Count-Faktor
        conf = min(1.0, abs(avg) * (len(compound_scores) / _MAX_HEADLINES + 0.5))
        if avg > 0.2:
            label = "bullish"
        elif avg < -0.2:
            label = "bearish"
        else:
            label = "neutral"
        return {
            "score": round(avg, 3),
            "label": label,
            "confidence": round(conf, 2),
            "rationale": f"VADER avg over {len(compound_scores)} headlines",
        }
    except Exception as e:
        log.debug(f"VADER-Scoring fehlgeschlagen: {e}")
        return None


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


def _score_with_llm(symbol: str, headlines: list) -> dict | None:
    """Stufe 1: Claude Haiku. Beste Qualitaet, benoetigt Guthaben.

    Bei 401/insufficient_credits returniert None -> Cascade faellt durch auf VADER.
    """
    client = _get_anthropic_client()
    if client is None or not headlines:
        return None

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


# ============================================================
# Headline-Quellen (Finnhub -> yfinance)
# ============================================================

def _fetch_headlines_yfinance(symbol: str) -> list:
    """Fallback-News-Quelle: yfinance ticker.news."""
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
        log.debug(f"yfinance-Headline-Fetch fuer {symbol} fehlgeschlagen: {e}")
        return []


def _fetch_headlines(symbol: str) -> tuple[list, str]:
    """Versuche Finnhub zuerst, fallback auf yfinance.
    Returns (headlines, source_name).
    """
    if finnhub_client is not None and finnhub_client.is_available():
        headlines = finnhub_client.fetch_company_news(symbol, days=7)
        if headlines:
            return headlines, "finnhub"
    return _fetch_headlines_yfinance(symbol), "yfinance"


# ============================================================
# Public API
# ============================================================

def get_sentiment(symbol: str) -> dict:
    """Return sentiment dict for a symbol.

    Shape:
        {score, articles, summary, source,
         label?, confidence?, rationale?}

    source ∈ {"llm", "vader", "keyword", "finnhub_api", "none"}
    """
    now = time.time()

    cached = _sentiment_cache.get(symbol)
    if cached and now - cached["fetched_at"] < _CACHE_TTL_SECONDS:
        return cached["result"]

    neutral = {"score": 0.0, "articles": 0, "summary": "Keine Daten", "source": "none"}

    # ---- Fast-Path: Finnhub /news-sentiment (fertig skaliert) ----
    if finnhub_client is not None and finnhub_client.is_available():
        try:
            fh_sent = finnhub_client.fetch_news_sentiment(symbol)
            if fh_sent is not None:
                score = fh_sent["score"]
                label = ("bullish" if score > 0.2
                         else "bearish" if score < -0.2 else "neutral")
                mood_de = {"bullish": "bullisch", "bearish": "baerisch",
                           "neutral": "neutral"}.get(label, label)
                result = {
                    "score": score,
                    "articles": fh_sent.get("buzz_weekly", 0),
                    "label": label,
                    "confidence": min(1.0, abs(score) + 0.3),
                    "rationale": "Finnhub companyNewsScore",
                    "summary": f"{mood_de} (Finnhub API-Score={score:+.2f}, "
                               f"{fh_sent.get('buzz_weekly', 0)} Artikel/Woche)",
                    "source": "finnhub_api",
                }
                _sentiment_cache[symbol] = {"result": result, "fetched_at": now}
                return result
        except Exception as e:
            log.debug(f"Finnhub-Sentiment fuer {symbol} fehlgeschlagen: {e}")

    # ---- Headlines holen (Finnhub news -> yfinance) ----
    headlines, news_source = _fetch_headlines(symbol)
    if not headlines:
        _sentiment_cache[symbol] = {"result": neutral, "fetched_at": now}
        return neutral

    # ---- Scorer-Cascade: Haiku -> VADER -> Keyword ----
    scored = None
    scorer = None

    llm_result = _score_with_llm(symbol, headlines)
    if llm_result is not None:
        scored = llm_result
        scorer = "llm"
    else:
        vader_result = _score_headlines_vader(headlines)
        if vader_result is not None:
            scored = vader_result
            scorer = "vader"

    if scored is not None:
        score = scored["score"]
        label = scored.get("label", "neutral")
        mood_de = {"bullish": "bullisch", "bearish": "baerisch",
                   "neutral": "neutral", "noise": "rauschen"}.get(label, label)
        result = {
            "score": round(score, 3),
            "articles": len(headlines),
            "label": label,
            "confidence": round(scored.get("confidence", 0.0), 2),
            "rationale": scored.get("rationale", ""),
            "summary": f"{mood_de} ({len(headlines)} Artikel via {news_source}, "
                       f"{scorer.upper()}-Score={score:+.2f})",
            "source": scorer,
        }
        _sentiment_cache[symbol] = {"result": result, "fetched_at": now}
        return result

    # ---- Letzter Rettungsanker: primitiver Keyword-Scorer ----
    scores = [_score_text_keyword(h) for h in headlines]
    if not scores:
        _sentiment_cache[symbol] = {"result": neutral, "fetched_at": now}
        return neutral
    avg = max(-1.0, min(1.0, sum(scores) / len(scores)))
    mood = "positiv" if avg > 0.3 else "negativ" if avg < -0.3 else "neutral"
    result = {
        "score": round(avg, 3),
        "articles": len(scores),
        "summary": f"{mood} ({len(scores)} Artikel via {news_source}, "
                   f"Keyword-Score={avg:+.2f})",
        "source": "keyword",
    }
    _sentiment_cache[symbol] = {"result": result, "fetched_at": now}
    return result


def get_market_sentiment() -> dict:
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


def get_sources_status() -> dict:
    """Diagnose-Endpoint: welche Sentiment-Quellen sind live?"""
    return {
        "finnhub": bool(finnhub_client and finnhub_client.is_available()),
        "anthropic_haiku": bool(_get_anthropic_client()),
        "vader": _vader_analyzer is not None,
        "yfinance": yf is not None,
    }
