"""
Tests fuer app.insider_signals — Score-Berechnung mit Mock-Transactions.
Keine Network-Calls.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from app.insider_signals import (
    MAX_NEGATIVE_SCORE,
    MAX_POSITIVE_SCORE,
    compute_insider_score,
    is_enabled,
)


def _tx(name: str, change: int, price: float, days_ago: int = 5) -> dict:
    """Helper: erzeugt eine Mock-Transaction."""
    d = (datetime.utcnow() - timedelta(days=days_ago)).date().isoformat()
    return {
        "name": name,
        "transactionDate": d,
        "filingDate": d,
        "change": change,
        "share": 100000,
        "transactionPrice": price,
        "transactionCode": "P" if change > 0 else "S",
        "currency": "USD",
    }


# ---------------- Empty / no data ----------------

def test_empty_transactions_returns_zero():
    assert compute_insider_score("X", transactions=[]) == 0


def test_only_old_transactions_outside_lookback_returns_zero():
    old = [_tx("CEO Joe", 10000, 100, days_ago=120)]
    assert compute_insider_score("X", lookback_days=30, transactions=old) == 0


# ---------------- Positive path ----------------

def test_cluster_buy_with_volume_returns_max_positive():
    # 3 unique Insider, jeder kauft fuer ~$1M -> Cluster + Volumen-Bonus
    txs = [
        _tx("CEO Alice", 10000, 100),  # +$1M
        _tx("CFO Bob", 5000, 100),     # +$0.5M
        _tx("Director Carol", 3000, 100),  # +$0.3M
    ]
    assert compute_insider_score("X", transactions=txs) == MAX_POSITIVE_SCORE  # +3


def test_cluster_buy_without_significant_volume_returns_two():
    # 3 Insider aber nur Klein-Volumen ($300k total < $500k Schwelle)
    txs = [
        _tx("CEO Alice", 1000, 100),   # +$100k
        _tx("CFO Bob", 1000, 100),
        _tx("Director Carol", 1000, 100),
    ]
    assert compute_insider_score("X", transactions=txs) == 2


def test_volume_bonus_without_cluster_returns_one():
    # Nur 1 Insider aber riesiges Volumen ($1M)
    txs = [_tx("CEO Alice", 10000, 100)]
    assert compute_insider_score("X", transactions=txs) == 1


def test_two_insiders_buying_no_cluster_no_volume_returns_zero():
    # 2 Insider (< Cluster-Schwelle 3), zusammen $200k (< $500k)
    txs = [_tx("CEO Alice", 1000, 100), _tx("CFO Bob", 1000, 100)]
    assert compute_insider_score("X", transactions=txs) == 0


# ---------------- Negative path (asymmetrisch) ----------------

def test_small_sell_does_not_trigger_negative():
    # 5 Insider verkaufen fuer total $500k — unter $2M-Schwelle
    txs = [_tx(f"Insider{i}", -1000, 100) for i in range(5)]
    assert compute_insider_score("X", transactions=txs) == 0


def test_big_sell_cluster_returns_minus_two():
    # 5 Insider verkaufen fuer $2.5M total -> Cluster + grosses Volumen
    txs = [_tx(f"Insider{i}", -5000, 100) for i in range(5)]  # 5 * -$500k = -$2.5M
    assert compute_insider_score("X", transactions=txs) == -2


def test_extreme_single_seller_returns_minus_one():
    # 1 Insider, $11M verkauft (>5x $2M Schwelle) -> -1 ohne Cluster
    txs = [_tx("Founder", -110000, 100)]  # -$11M
    assert compute_insider_score("X", transactions=txs) == -1


def test_buy_dominates_over_sell_when_both_present():
    # Cluster-Buy + ein Verkaeufer -> sollte +3 bleiben (Buy gewinnt)
    txs = [
        _tx("CEO Alice", 10000, 100),
        _tx("CFO Bob", 5000, 100),
        _tx("Director Carol", 3000, 100),
        _tx("Director Dave", -1000, 100),
    ]
    assert compute_insider_score("X", transactions=txs) == MAX_POSITIVE_SCORE


# ---------------- Edge cases ----------------

def test_invalid_date_skipped_gracefully():
    txs = [
        {"name": "X", "transactionDate": "not-a-date",
         "change": 99999, "transactionPrice": 100},
        _tx("CEO Y", 100, 1),  # nicht genug fuer Score
    ]
    # Sollte nicht crashen, kein Score
    assert compute_insider_score("X", transactions=txs) == 0


def test_score_clamped_to_max_positive():
    # Auch bei extremen Daten nie ueber MAX_POSITIVE_SCORE
    txs = [_tx(f"I{i}", 100000, 1000) for i in range(20)]
    assert compute_insider_score("X", transactions=txs) <= MAX_POSITIVE_SCORE


# ---------------- Config-Flag ----------------

def test_is_enabled_default_false():
    assert is_enabled({}) is False
    assert is_enabled({"scanner": {}}) is False


def test_is_enabled_when_config_true():
    assert is_enabled({"scanner": {"insider_signal_enabled": True}}) is True


def test_is_enabled_invalid_config():
    assert is_enabled(None) is False
    assert is_enabled("not-a-dict") is False
