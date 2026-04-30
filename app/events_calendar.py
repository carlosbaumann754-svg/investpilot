"""
Events Calendar — Earnings/Events Blackout Filter for InvestPilot.

Prevents trading around earnings announcements by checking yfinance
calendar data with configurable buffer days and 24h caching.
"""

import logging
import time
from datetime import datetime

log = logging.getLogger("EventsCalendar")

try:
    import yfinance as yf
except ImportError:
    yf = None
    log.warning("yfinance nicht verfuegbar — Earnings-Filter deaktiviert")

# Module-level cache: symbol -> {"earnings_date": datetime|None, "fetched_at": float}
_earnings_cache = {}
_CACHE_TTL_SECONDS = 24 * 60 * 60  # 24 hours


def _fetch_earnings_date(symbol):
    """Fetch next earnings date from yfinance for a single symbol.

    Returns datetime or None. Results are cached for 24h.
    """
    now = time.time()

    # Check cache
    if symbol in _earnings_cache:
        entry = _earnings_cache[symbol]
        if now - entry["fetched_at"] < _CACHE_TTL_SECONDS:
            return entry["earnings_date"]

    earnings_dt = None

    if yf is None:
        _earnings_cache[symbol] = {"earnings_date": None, "fetched_at": now}
        return None

    try:
        ticker = yf.Ticker(symbol)
        calendar = ticker.calendar

        if calendar is None:
            _earnings_cache[symbol] = {"earnings_date": None, "fetched_at": now}
            return None

        # Handle different yfinance return formats
        earnings_date_raw = None

        if isinstance(calendar, dict):
            # Newer yfinance versions return a dict
            earnings_date_raw = calendar.get("Earnings Date")
            if earnings_date_raw is None:
                earnings_date_raw = calendar.get("Earnings Average Date")
        elif hasattr(calendar, 'empty'):
            # DataFrame format
            if calendar.empty:
                _earnings_cache[symbol] = {"earnings_date": None, "fetched_at": now}
                return None
            if hasattr(calendar, 'get'):
                earnings_date_raw = calendar.get("Earnings Date")
            elif hasattr(calendar, 'iloc') and len(calendar) > 0:
                earnings_date_raw = calendar.iloc[0]

        if earnings_date_raw is None:
            _earnings_cache[symbol] = {"earnings_date": None, "fetched_at": now}
            return None

        # Unwrap list/series
        if hasattr(earnings_date_raw, '__iter__') and not isinstance(earnings_date_raw, str):
            items = list(earnings_date_raw)
            if not items:
                _earnings_cache[symbol] = {"earnings_date": None, "fetched_at": now}
                return None
            earnings_date_raw = items[0]

        # Convert to datetime
        if hasattr(earnings_date_raw, 'to_pydatetime'):
            earnings_dt = earnings_date_raw.to_pydatetime().replace(tzinfo=None)
        elif isinstance(earnings_date_raw, datetime):
            earnings_dt = earnings_date_raw.replace(tzinfo=None)
        elif isinstance(earnings_date_raw, str):
            try:
                earnings_dt = datetime.fromisoformat(earnings_date_raw)
            except (ValueError, TypeError):
                pass

    except Exception as e:
        log.warning(f"Earnings-Datum Abruf fehlgeschlagen fuer {symbol}: {e}")

    # v37x: Finnhub-Fallback wenn yfinance kein Earnings-Datum liefert.
    # yfinance hat seit 2024 sporadische Calendar-API-Probleme (~10-15% der
    # US-Stocks fehlend). Finnhub /calendar/earnings ist robuster.
    if earnings_dt is None:
        try:
            from app import finnhub_client
            if finnhub_client.is_available():
                earnings_dt = finnhub_client.fetch_earnings_calendar(symbol)
                if earnings_dt is not None:
                    log.info(f"Earnings-Datum fuer {symbol} via Finnhub-Fallback: {earnings_dt}")
        except Exception as e:
            log.debug(f"Finnhub-Earnings-Fallback fuer {symbol} fehlgeschlagen: {e}")

    _earnings_cache[symbol] = {"earnings_date": earnings_dt, "fetched_at": now}
    return earnings_dt


def is_earnings_blackout(symbol, config=None):
    """Check if a symbol is within the earnings blackout window.

    Args:
        symbol: Ticker symbol (e.g. 'AAPL')
        config: Full config dict (reads market_context.earnings_buffer_days_*)

    Returns:
        (is_blackout: bool, reason: str or None)
        On failure, returns (False, None) to allow trading.
    """
    mc_config = {}
    if config:
        mc_config = config.get("market_context", {})

    # Check if filter is enabled
    if not mc_config.get("use_earnings_filter", True):
        return False, None

    buffer_before = mc_config.get("earnings_buffer_days_before", 3)
    buffer_after = mc_config.get("earnings_buffer_days_after", 1)

    try:
        earnings_dt = _fetch_earnings_date(symbol)
        if earnings_dt is None:
            return False, None

        now = datetime.now()
        days_until = (earnings_dt - now).days

        # Blackout window: -buffer_after <= days_until <= buffer_before
        if -buffer_after <= days_until <= buffer_before:
            if days_until >= 0:
                reason = f"Earnings in {days_until} Tagen ({earnings_dt.strftime('%Y-%m-%d')})"
            else:
                reason = f"Earnings vor {abs(days_until)} Tag(en) ({earnings_dt.strftime('%Y-%m-%d')})"
            log.info(f"EARNINGS BLACKOUT: {symbol} — {reason}")
            return True, reason

        return False, None

    except Exception as e:
        log.warning(f"Earnings-Blackout Check fehlgeschlagen fuer {symbol}: {e}")
        return False, None


def get_upcoming_earnings(symbols):
    """Get upcoming earnings dates for a list of symbols.

    Args:
        symbols: List of ticker symbols

    Returns:
        dict of symbol -> next earnings date (datetime or None)
    """
    result = {}
    for symbol in symbols:
        try:
            result[symbol] = _fetch_earnings_date(symbol)
        except Exception as e:
            log.debug(f"Earnings-Datum fuer {symbol} nicht abrufbar: {e}")
            result[symbol] = None
    return result


# Module-level cache for earnings surprise data
_surprise_cache = {}
_SURPRISE_CACHE_TTL = 24 * 60 * 60  # 24 hours


def get_earnings_surprise(symbol):
    """Check last earnings: did company beat or miss expectations?

    Uses yfinance earnings_history or quarterly_earnings to compare
    expected vs actual EPS.

    Args:
        symbol: Ticker symbol (e.g. 'AAPL')

    Returns:
        dict with keys:
            surprise_pct: float (positive = beat, negative = miss)
            beat: bool or None
            actual_eps: float or None
            expected_eps: float or None
            available: bool
    """
    now = time.time()

    # Check cache
    if symbol in _surprise_cache:
        entry = _surprise_cache[symbol]
        if now - entry["fetched_at"] < _SURPRISE_CACHE_TTL:
            return entry["result"]

    no_data = {
        "surprise_pct": 0.0,
        "beat": None,
        "actual_eps": None,
        "expected_eps": None,
        "available": False,
    }

    if yf is None:
        _surprise_cache[symbol] = {"result": no_data, "fetched_at": now}
        return no_data

    try:
        ticker = yf.Ticker(symbol)

        # Try earnings_history first (has expected vs actual)
        surprise_pct = None
        actual_eps = None
        expected_eps = None

        # Method 1: earnings_history (newer yfinance)
        try:
            eh = getattr(ticker, "earnings_history", None)
            if eh is not None and hasattr(eh, 'empty') and not eh.empty:
                last_row = eh.iloc[-1]
                actual_eps = float(last_row.get("epsActual", 0) or 0)
                expected_eps = float(last_row.get("epsEstimate", 0) or 0)
                if expected_eps != 0:
                    surprise_pct = ((actual_eps - expected_eps) / abs(expected_eps)) * 100
        except Exception:
            pass

        # Method 2: quarterly_earnings fallback
        if surprise_pct is None:
            try:
                qe = getattr(ticker, "quarterly_earnings", None)
                if qe is not None and hasattr(qe, 'empty') and not qe.empty:
                    last_row = qe.iloc[-1]
                    revenue = float(last_row.get("Revenue", 0) or 0)
                    earnings = float(last_row.get("Earnings", 0) or 0)
                    if revenue > 0:
                        # Approximate: use earnings margin as proxy
                        actual_eps = earnings
                        # No expected data in this format, mark as limited
            except Exception:
                pass

        if surprise_pct is not None:
            result = {
                "surprise_pct": round(surprise_pct, 2),
                "beat": surprise_pct > 0,
                "actual_eps": actual_eps,
                "expected_eps": expected_eps,
                "available": True,
            }
        else:
            result = no_data

        _surprise_cache[symbol] = {"result": result, "fetched_at": now}
        return result

    except Exception as e:
        log.warning(f"Earnings-Surprise Abruf fehlgeschlagen fuer {symbol}: {e}")
        _surprise_cache[symbol] = {"result": no_data, "fetched_at": now}
        return no_data


def adjust_score_for_earnings(symbol, base_score):
    """Adjust a scanner score based on last earnings surprise.

    Big beat (>10% above expected): +5 bonus
    Big miss (>10% below expected): -5 penalty

    Args:
        symbol: Ticker symbol
        base_score: float original scanner score

    Returns:
        float adjusted score
    """
    try:
        surprise = get_earnings_surprise(symbol)
        if not surprise.get("available", False):
            return base_score

        surprise_pct = surprise.get("surprise_pct", 0)

        if surprise_pct > 10:
            adjustment = 5
            log.info(f"  EARNINGS BEAT: {symbol} +{surprise_pct:.1f}% -> Score +{adjustment}")
        elif surprise_pct < -10:
            adjustment = -5
            log.info(f"  EARNINGS MISS: {symbol} {surprise_pct:.1f}% -> Score {adjustment}")
        elif surprise_pct > 5:
            adjustment = 2
        elif surprise_pct < -5:
            adjustment = -2
        else:
            adjustment = 0

        return base_score + adjustment

    except Exception as e:
        log.debug(f"Earnings-Score Anpassung fehlgeschlagen fuer {symbol}: {e}")
        return base_score
