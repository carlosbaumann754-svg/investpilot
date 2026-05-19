"""Tests fuer R-A29 Ebene 3 - api_broker_status Stale-Cache-Fallback.

Anlass: Bei Container-Restart pollt Dashboard /api/broker_status. Live-Fetch
failt waehrend IBKR-Disconnect-Phase → Frontend bekommt broker_unavailable.
Stattdessen: letzten gueltigen Cache mit _stale-Marker zurueckgeben.

Source-basierte Tests (Module-Import würde pyotp brauchen, lokal nicht da).
Live-Verifikation erfolgt per Container-Health-Check + Dashboard-Polling.
"""

import re


def test_api_broker_status_has_stale_cache_fallback():
    """R-A29: api_broker_status hat Stale-Cache-Fallback bei Fetch-Error."""
    src = open("web/app.py", encoding="utf-8").read()
    start = src.find("async def api_broker_status")
    assert start != -1, "api_broker_status endpoint not found"
    body = src[start:start + 5000]

    # Muss _stale-Marker + Stale-Cache-Logik haben
    assert "_stale" in body, "R-A29 _stale-Marker fehlt"
    assert "_BROKER_STATUS_STALE_MAX_SECONDS" in body, "Stale-Max-Constant fehlt"


def test_stale_cache_constant_is_reasonable():
    """Stale-Cache-Max sollte zwischen 5min und 30min liegen (Pre-Cutover-Sweet-Spot)."""
    src = open("web/app.py", encoding="utf-8").read()
    m = re.search(r"_BROKER_STATUS_STALE_MAX_SECONDS\s*=\s*(\d+)", src)
    assert m is not None, "Constant nicht gefunden"
    val = int(m.group(1))
    assert 300 <= val <= 1800, f"Stale-Max {val}s ausserhalb sane range 300-1800"


def test_fetch_error_returns_stale_cache_marker():
    """Code-Pfad: except Exception → stale_cache mit _stale=True Marker setzen."""
    src = open("web/app.py", encoding="utf-8").read()
    start = src.find("async def api_broker_status")
    body = src[start:start + 5000]
    # Pattern: except Exception ... cached is not None ... _stale: True
    assert "except Exception" in body
    assert '"_stale": True' in body or "'_stale': True" in body
    assert "_stale_reason" in body  # Error-Reason mitschreiben


def test_no_cache_falls_through_to_unavailable():
    """Wenn cached is None bei Fetch-Error → echter Error mit _unavailable_since."""
    src = open("web/app.py", encoding="utf-8").read()
    start = src.find("async def api_broker_status")
    body = src[start:start + 5000]
    assert "_unavailable_since" in body, "Fallback-Error-Marker fehlt"
    assert '"broker": "?"' in body or "'broker': '?'" in body


def test_log_info_instead_of_log_error_on_fetch_fail():
    """Sentry-Bubble unterdruecken: log.info statt log.error/exception bei
    Container-Restart-Cascade (erwartet, kein echter Bug)."""
    src = open("web/app.py", encoding="utf-8").read()
    start = src.find("async def api_broker_status")
    body = src[start:start + 5000]
    # Im except-Block: log.info (nicht log.error) damit Sentry-Logging-
    # Integration es nicht als ERROR-Level erfasst
    except_block_match = re.search(
        r"except Exception as e:(.*?)(?=\n[a-zA-Z_]|\nasync def|\ndef )",
        body, re.DOTALL)
    if except_block_match:
        except_block = except_block_match.group(1)
        # Sollte log.info enthalten, NICHT log.error/exception
        assert "log.info(" in except_block, "R-A29 sollte log.info nutzen"
        assert "log.error(" not in except_block, "log.error → Sentry-Spam"
        assert "log.exception(" not in except_block, "log.exception → Sentry-Spam"
