"""Tests fuer v37dg Position-ID-Match-Fix.

Bug 06./07.05.: Bot kauft wiederholt SLV trotz existierender 597-SLV-
Position (3 Buy-Attempts in 24h). Root-Cause: Bot's existing_ids-Set
enthielt IBKR-conIds, Scanner-Filter prueft aber etoro_id (5003 fuer
SILVER) — Mismatch -> Filter laesst Buy durch.

Fix v37dg: zusaetzliches existing_symbols-Set mit Symbol-Translation
(v37de expand_symbol_for_match). Buy-Filter prueft beide.
"""

from unittest.mock import patch


def test_existing_symbols_includes_translation_variants():
    """Bei IBKR-Position 'SLV' soll existing_symbols beide enthalten (SLV + SILVER)."""
    from app.market_scanner import expand_symbol_for_match

    # IBKR liefert Position mit Symbol "SLV" (ETF-Ticker)
    # parsed_position.symbol = "SLV"
    # existing_symbols sollte sowohl "SLV" als auch "SILVER" enthalten
    variants = expand_symbol_for_match("SLV")
    assert "SLV" in variants
    assert "SILVER" in variants


def test_existing_symbols_passthrough_for_stocks():
    """Stocks ohne Override: nur ein Eintrag (AAPL bleibt AAPL)."""
    from app.market_scanner import expand_symbol_for_match

    variants = expand_symbol_for_match("AAPL")
    assert variants == {"AAPL"}


def test_buy_filter_blocks_silver_when_slv_position_exists():
    """Bug-Reproduktion: Bot hat SLV-Position, Scanner liefert SILVER-Buy-Signal.

    Mit Fix v37dg sollte der Buy NICHT durchgelassen werden.
    """
    from app.market_scanner import expand_symbol_for_match

    # Simuliere parsed_positions wie sie aus IBKR.get_portfolio() kommen
    parsed_positions = [
        {"instrument_id": 1316487, "symbol": "SLV", "invested": 41890.0},  # IBKR conId, ETF-Ticker
    ]
    # Aufgebaute Sets (wie in trader.py:1234ff nach v37dg)
    existing_ids = {p["instrument_id"] for p in parsed_positions}
    existing_symbols = set()
    for p in parsed_positions:
        existing_symbols.update(expand_symbol_for_match(p["symbol"]))

    # Scanner-Resultat: STRONG_BUY fuer SILVER (Bot-Universum-Name)
    scanner_result = {"symbol": "SILVER", "etoro_id": 5003, "signal": "STRONG_BUY", "score": 50}

    # Filter-Logik aus trader.py:1354ff (v37dg)
    is_buy_candidate = (
        scanner_result["signal"] in ("BUY", "STRONG_BUY")
        and scanner_result["score"] >= 40
        and scanner_result["etoro_id"] not in existing_ids
        and scanner_result.get("symbol") not in existing_symbols
    )
    assert is_buy_candidate is False, \
        "Bot sollte SILVER-Buy NICHT triggern wenn SLV-Position existiert (v37dg Fix)"


def test_buy_filter_allows_new_symbol():
    """Regression: Wenn keine matching Position, soll Buy durchgehen."""
    from app.market_scanner import expand_symbol_for_match

    # Bot hat AAPL Position (kein Override-Konflikt)
    parsed_positions = [{"instrument_id": 265598, "symbol": "AAPL", "invested": 50000}]
    existing_ids = {p["instrument_id"] for p in parsed_positions}
    existing_symbols = set()
    for p in parsed_positions:
        existing_symbols.update(expand_symbol_for_match(p["symbol"]))

    # Scanner: NVDA Strong-Buy (hat Bot nicht)
    scanner_result = {"symbol": "NVDA", "etoro_id": 9999, "signal": "STRONG_BUY", "score": 50}

    is_buy_candidate = (
        scanner_result["signal"] in ("BUY", "STRONG_BUY")
        and scanner_result["score"] >= 40
        and scanner_result["etoro_id"] not in existing_ids
        and scanner_result.get("symbol") not in existing_symbols
    )
    assert is_buy_candidate is True, "Neue Symbole sollen weiterhin Buy ausloesen"


def test_sell_filter_finds_position_via_symbol_match():
    """SELL-Filter: wenn Bot SLV-Position hat + Scanner SILVER-SELL signal,
    soll SELL ausgeloest werden (via symbol-match)."""
    from app.market_scanner import expand_symbol_for_match

    parsed_positions = [{"instrument_id": 1316487, "symbol": "SLV", "invested": 41890}]
    existing_ids = {p["instrument_id"] for p in parsed_positions}
    existing_symbols = set()
    for p in parsed_positions:
        existing_symbols.update(expand_symbol_for_match(p["symbol"]))

    scanner_result = {"symbol": "SILVER", "etoro_id": 5003, "signal": "STRONG_SELL", "score": 20}

    is_sell_candidate = (
        scanner_result["signal"] in ("SELL", "STRONG_SELL")
        and (scanner_result["etoro_id"] in existing_ids
             or scanner_result.get("symbol") in existing_symbols)
    )
    assert is_sell_candidate is True, "SELL muss SLV-Position via Symbol-Match finden"
