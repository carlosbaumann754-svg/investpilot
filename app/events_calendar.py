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
