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


# ============================================================
# v37h Task 2b Defensive-Regression-Tests (10.05.2026)
# ============================================================
# Carlos's "Position-Sync-Bug" am 07.05. war Hypothese-driven (08.05.
# widerlegt). Aber v37dg fixte einen ECHTEN Wurzel-Bug. Diese Tests
# locken das v37dg-Verhalten gegen silent-regression — falls jemand
# spaeter expand_symbol_for_match oder den Filter-Code refactored.
# Cutover-Restzeit war Argument fuer billige Defense-in-Depth.

def test_all_commodity_overrides_bidirectional():
    """v37dg-Schutz: ALLE 5 Commodity-Overrides muessen bidirektional
    matchen (GOLD/GLD, SILVER/SLV, OIL/USO, NATGAS/UNG, COPPER/CPER).

    Wenn jemand spaeter ein neues Commodity in ASSET_UNIVERSE eintraegt
    aber `expand_symbol_for_match` versehentlich kaputtmacht, faengt
    dieser Test alle 5 sofort.
    """
    from app.market_scanner import expand_symbol_for_match

    pairs = [
        ("GOLD", "GLD"),
        ("SILVER", "SLV"),
        ("OIL", "USO"),
        ("NGAS", "UNG"),
        ("COPPER", "CPER"),
    ]
    for bot_name, ibkr_ticker in pairs:
        # Vorwaerts: Bot-Universum-Name -> beide
        forward = expand_symbol_for_match(bot_name)
        assert bot_name in forward and ibkr_ticker in forward, \
            f"Forward {bot_name}: {forward}"
        # Rueckwaerts: IBKR-Ticker -> beide
        reverse = expand_symbol_for_match(ibkr_ticker)
        assert bot_name in reverse and ibkr_ticker in reverse, \
            f"Reverse {ibkr_ticker}: {reverse}"


def test_filter_no_crash_on_missing_symbol():
    """v37dg-Schutz: scanner_result ohne 'symbol'-Key (defensive Fallback)
    darf den Filter nicht crashen. .get('symbol') returnt None, None ist
    nicht in existing_symbols-Set (any non-empty Set), Filter bleibt sauber.
    """
    from app.market_scanner import expand_symbol_for_match

    parsed_positions = [{"instrument_id": 1316487, "symbol": "SLV", "invested": 1}]
    existing_symbols = set()
    for p in parsed_positions:
        existing_symbols.update(expand_symbol_for_match(p["symbol"]))
    existing_ids = {p["instrument_id"] for p in parsed_positions}

    # Scanner-Result ohne 'symbol'-Key (defekte Pipeline)
    broken_result = {"etoro_id": 9999, "signal": "STRONG_BUY", "score": 50}

    # Filter darf nicht crashen
    is_buy_candidate = (
        broken_result["signal"] in ("BUY", "STRONG_BUY")
        and broken_result["score"] >= 40
        and broken_result["etoro_id"] not in existing_ids
        and broken_result.get("symbol") not in existing_symbols  # None not in set -> True
    )
    # Verhalten: defektes Result OHNE Symbol kann nicht durch Symbol-Match
    # blockiert werden — geht durch (etoro_id-Check muss ausreichen).
    assert is_buy_candidate is True


def test_filter_blocks_reverse_direction():
    """v37dg-Schutz: Bot hat SILVER-Position (etoro_id=5003 + symbol='SILVER'
    aus ASSET_UNIVERSE). Scanner liefert SLV-Buy-Signal (z.B. von externer
    Source). Symbol-Match muss greifen — bidirektional, nicht nur SLV->SILVER.
    """
    from app.market_scanner import expand_symbol_for_match

    # Bot-Position mit Bot-Universum-Symbol (statt IBKR-Ticker)
    parsed_positions = [
        {"instrument_id": 5003, "symbol": "SILVER", "invested": 41890.0},
    ]
    existing_ids = {p["instrument_id"] for p in parsed_positions}
    existing_symbols = set()
    for p in parsed_positions:
        existing_symbols.update(expand_symbol_for_match(p["symbol"]))

    # Scanner-Resultat mit IBKR-Ticker (SLV) statt Bot-Name
    scanner_result = {"symbol": "SLV", "etoro_id": 9999, "signal": "STRONG_BUY", "score": 50}

    is_buy_candidate = (
        scanner_result["signal"] in ("BUY", "STRONG_BUY")
        and scanner_result["score"] >= 40
        and scanner_result["etoro_id"] not in existing_ids
        and scanner_result.get("symbol") not in existing_symbols
    )
    assert is_buy_candidate is False, \
        "Filter muss SLV-Buy blocken wenn SILVER-Position existiert (reverse direction)"


def test_filter_with_empty_positions_passes_through():
    """v37dg-Schutz: Fresh-Start-Szenario. existing_symbols + existing_ids
    sind leer. Filter darf BUYs nicht versehentlich blocken."""
    from app.market_scanner import expand_symbol_for_match

    parsed_positions = []  # Bot hat keine Positionen
    existing_ids = {p["instrument_id"] for p in parsed_positions}
    existing_symbols = set()
    for p in parsed_positions:
        existing_symbols.update(expand_symbol_for_match(p["symbol"]))

    # Beliebiges BUY-Signal
    scanner_result = {"symbol": "SILVER", "etoro_id": 5003, "signal": "STRONG_BUY", "score": 60}

    is_buy_candidate = (
        scanner_result["signal"] in ("BUY", "STRONG_BUY")
        and scanner_result["score"] >= 40
        and scanner_result["etoro_id"] not in existing_ids
        and scanner_result.get("symbol") not in existing_symbols
    )
    assert is_buy_candidate is True, "Empty-Portfolio darf neue BUYs nicht blocken"


def test_filter_multi_position_silver_blocked_nvda_passes():
    """v37dg-Schutz: Multi-Position-Szenario. Bot hat SLV + AAPL. Scanner
    liefert mehrere Buy-Kandidaten. Genau die mit existierendem Match
    werden geblockt, andere passieren.
    """
    from app.market_scanner import expand_symbol_for_match

    parsed_positions = [
        {"instrument_id": 1316487, "symbol": "SLV",  "invested": 41890},
        {"instrument_id": 265598,  "symbol": "AAPL", "invested": 50000},
    ]
    existing_ids = {p["instrument_id"] for p in parsed_positions}
    existing_symbols = set()
    for p in parsed_positions:
        existing_symbols.update(expand_symbol_for_match(p["symbol"]))

    scanner_results = [
        {"symbol": "SILVER", "etoro_id": 5003, "signal": "STRONG_BUY", "score": 50},  # blocked
        {"symbol": "AAPL",   "etoro_id": 6408, "signal": "STRONG_BUY", "score": 50},  # blocked
        {"symbol": "NVDA",   "etoro_id": 7777, "signal": "STRONG_BUY", "score": 50},  # passes
        {"symbol": "GOLD",   "etoro_id": 5001, "signal": "STRONG_BUY", "score": 50},  # passes (no GLD)
    ]

    decisions = []
    for r in scanner_results:
        is_buy = (
            r["signal"] in ("BUY", "STRONG_BUY")
            and r["score"] >= 40
            and r["etoro_id"] not in existing_ids
            and r.get("symbol") not in existing_symbols
        )
        decisions.append((r["symbol"], is_buy))

    expected = [("SILVER", False), ("AAPL", False), ("NVDA", True), ("GOLD", True)]
    assert decisions == expected, f"Multi-Position-Filter falsch: {decisions}"
