"""
Tests fuer app.asset_classes — Registry, Trading-Sessions, IBKR-Hints.

Wichtig: Tests pinnen `now_utc` explizit, sonst rote Tests am Wochenende
oder waehrend US-DST-Wechsel.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from app.asset_classes import (
    REGISTRY,
    any_class_tradeable,
    get_ibkr_hints,
    is_asset_class_tradeable,
    list_classes,
    resolve_spec,
)

# Helper: konkrete UTC-Zeitpunkte
MON_NOON_UTC = datetime(2025, 6, 16, 12, 0, tzinfo=ZoneInfo("UTC"))   # Mo 14:00 CEST / 08:00 ET
SAT_NOON_UTC = datetime(2025, 6, 14, 12, 0, tzinfo=ZoneInfo("UTC"))   # Sa
SUN_NOON_UTC = datetime(2025, 6, 15, 12, 0, tzinfo=ZoneInfo("UTC"))   # So
MON_RTH_UTC = datetime(2025, 6, 16, 14, 0, tzinfo=ZoneInfo("UTC"))    # Mo 10:00 ET = RTH
MON_AFTERHOURS_UTC = datetime(2025, 6, 16, 22, 0, tzinfo=ZoneInfo("UTC"))  # Mo 18:00 ET = After
FRI_LATE_UTC = datetime(2025, 6, 20, 22, 0, tzinfo=ZoneInfo("UTC"))   # Fr 18:00 ET (nach Forex-Close)
SUN_EVENING_UTC = datetime(2025, 6, 15, 22, 0, tzinfo=ZoneInfo("UTC"))  # So 18:00 ET (nach Forex-Open)


# ---------------------- Registry-Basics ----------------------

def test_registry_has_all_expected_classes():
    expected = {"crypto", "stocks", "etf", "stocks_extended",
                "eu_stocks", "uk_stocks", "ch_stocks",
                "jp_stocks", "hk_stocks", "au_stocks",
                "forex", "futures", "indices", "commodities", "bonds"}
    assert expected.issubset(set(REGISTRY.keys()))


def test_resolve_spec_aliases_case_insensitive():
    assert resolve_spec("CRYPTO").name == "crypto"
    assert resolve_spec("Cryptocurrency").name == "crypto"
    assert resolve_spec("FX").name == "forex"
    assert resolve_spec("etfs").name == "etf"


def test_resolve_spec_unknown_returns_none():
    assert resolve_spec("foobar") is None
    assert resolve_spec("") is None


def test_list_classes_sorted():
    classes = list_classes()
    assert classes == sorted(classes)
    assert "crypto" in classes


# ---------------------- Crypto: 24/7 ----------------------

def test_crypto_tradeable_weekday():
    assert is_asset_class_tradeable("crypto", MON_RTH_UTC)


def test_crypto_blocked_during_ibkr_maintenance_saturday_night():
    # Sa 21:00 NY = Sa 01:00 UTC (Sonntag) -> Wartung (Sa 19:00 NY -> So 07:00 NY)
    sat_late = datetime(2025, 6, 15, 1, 0, tzinfo=ZoneInfo("UTC"))  # So 01:00 UTC = Sa 21:00 NY
    assert not is_asset_class_tradeable("crypto", sat_late)


def test_crypto_open_sunday_after_maintenance():
    # So 12:00 NY = So 16:00 UTC -> Wartung vorbei (>= 07:00 NY)
    sun_noon = datetime(2025, 6, 15, 16, 0, tzinfo=ZoneInfo("UTC"))
    assert is_asset_class_tradeable("crypto", sun_noon)


# ---------------------- US Stocks ----------------------

def test_us_stocks_tradeable_during_rth():
    # MON_RTH_UTC = Mo 10:00 ET -> tradeable
    assert is_asset_class_tradeable("stocks", MON_RTH_UTC)


def test_us_stocks_not_tradeable_after_close():
    assert not is_asset_class_tradeable("stocks", MON_AFTERHOURS_UTC)


def test_us_stocks_not_tradeable_weekend():
    assert not is_asset_class_tradeable("stocks", SAT_NOON_UTC)


def test_stocks_extended_tradeable_in_after_hours():
    # 18:00 ET liegt im After-Market 16:00-20:00
    assert is_asset_class_tradeable("stocks_extended", MON_AFTERHOURS_UTC)


# ---------------------- Forex (Sun 17 NY - Fri 17 NY) ----------------------

def test_forex_open_sunday_evening_after_17_ny():
    # SUN_EVENING_UTC = So 22:00 UTC = So 18:00 ET -> open
    assert is_asset_class_tradeable("forex", SUN_EVENING_UTC)


def test_forex_closed_friday_late_evening():
    # FRI_LATE_UTC = Fr 22:00 UTC = Fr 18:00 ET -> closed (after 17:00)
    assert not is_asset_class_tradeable("forex", FRI_LATE_UTC)


def test_forex_closed_saturday():
    assert not is_asset_class_tradeable("forex", SAT_NOON_UTC)


# ---------------------- EU Stocks (Frankfurt 09:00-17:30 CET) ----------------------

def test_eu_stocks_tradeable_morning():
    # MON_NOON_UTC = Mo 14:00 CEST -> Frankfurt offen
    assert is_asset_class_tradeable("eu_stocks", MON_NOON_UTC)


def test_eu_stocks_closed_weekend():
    assert not is_asset_class_tradeable("eu_stocks", SAT_NOON_UTC)


# ---------------------- Asia Stocks ----------------------

def test_jp_stocks_morning_session():
    # Tokio 10:00 JST = 01:00 UTC, Wochentag
    t = datetime(2025, 6, 17, 1, 0, tzinfo=ZoneInfo("UTC"))  # Di 01:00 UTC = Di 10:00 JST
    assert is_asset_class_tradeable("jp_stocks", t)


def test_jp_stocks_lunch_break():
    # 12:00 JST = 03:00 UTC, sollte in Pause sein (11:30-12:30)
    t = datetime(2025, 6, 17, 3, 0, tzinfo=ZoneInfo("UTC"))
    assert not is_asset_class_tradeable("jp_stocks", t)


# ---------------------- IBKR-Hints ----------------------

def test_ibkr_hints_for_known_classes():
    assert get_ibkr_hints("stocks")["secType"] == "STK"
    assert get_ibkr_hints("crypto")["secType"] == "CRYPTO"
    assert get_ibkr_hints("forex")["secType"] == "CASH"
    assert get_ibkr_hints("futures")["secType"] == "FUT"
    assert get_ibkr_hints("eu_stocks")["currency"] == "EUR"
    assert get_ibkr_hints("uk_stocks")["currency"] == "GBP"


def test_ibkr_hints_unknown_returns_none():
    assert get_ibkr_hints("foobar") is None


# ---------------------- any_class_tradeable ----------------------

def test_any_class_tradeable_picks_open_class():
    # SAT_NOON_UTC = Sa 12:00 UTC = Sa 08:00 NY -> Crypto offen (vor 19 NY Wartung)
    assert any_class_tradeable(["stocks", "crypto"], SAT_NOON_UTC)
    # Pure-Stocks-Universum am Sa: zu
    assert not any_class_tradeable(["stocks", "etf"], SAT_NOON_UTC)


def test_any_class_tradeable_empty():
    assert not any_class_tradeable([], MON_RTH_UTC)


# ---------------------- Default-Behavior bei Unknown ----------------------

def test_unknown_class_default_permissive():
    """Unknown class -> True (permissiv), damit neue Asset-Typen nicht blocken."""
    assert is_asset_class_tradeable("warrants", MON_RTH_UTC)
    assert is_asset_class_tradeable("warrants", SAT_NOON_UTC)


def test_unknown_class_strict_mode():
    assert not is_asset_class_tradeable("warrants", MON_RTH_UTC, default_if_unknown=False)
