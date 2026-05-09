"""v37h Task 1 (09.05.2026) — Asset-Class-Order-Settings Tests.

Hintergrund: Stress-Test 08.05. + Live-Beobachtung zeigten Pattern:
Commodity-ETFs (CPER, SLV) failen wiederholt in Pre-Market wegen duenner
Liquiditaet. Asset-Class-Settings = saubere Cross-Cutting-Concern fuer:
  - per-Asset-Class Slippage (commodities 1.5%, stocks 0.5%)
  - per-Asset-Class Trading-Hours (commodities rth_only, stocks extended)

Tests verifizieren:
1. get_asset_class_for_instrument_id() helper
2. get_asset_class_for_symbol() helper
3. IbkrBroker laedt asset_class_order_settings aus config
4. _place_market_order nutzt asset-class-Slippage statt globaler default
5. _place_market_order setzt outsideRth basierend auf asset-class
6. per-call limit_slippage_pct override gewinnt gegen asset-class-default
"""
from unittest.mock import MagicMock, patch
import pytest


# ============================================================
# Helper-Function Tests
# ============================================================

def test_get_asset_class_for_instrument_id_stocks():
    """AAPL etoro_id=6408 -> stocks."""
    from app.market_scanner import get_asset_class_for_instrument_id
    assert get_asset_class_for_instrument_id(6408) == "stocks"


def test_get_asset_class_for_instrument_id_commodity():
    """SILVER etoro_id=5003 -> commodities."""
    from app.market_scanner import get_asset_class_for_instrument_id
    assert get_asset_class_for_instrument_id(5003) == "commodities"


def test_get_asset_class_for_instrument_id_etf():
    """SLV etoro_id=1115 -> etf."""
    from app.market_scanner import get_asset_class_for_instrument_id
    assert get_asset_class_for_instrument_id(1115) == "etf"


def test_get_asset_class_for_instrument_id_unknown():
    """Unknown id returnt None."""
    from app.market_scanner import get_asset_class_for_instrument_id
    assert get_asset_class_for_instrument_id(999999999) is None


def test_get_asset_class_for_instrument_id_string_input():
    """str-input wird zu int konvertiert (defensiv)."""
    from app.market_scanner import get_asset_class_for_instrument_id
    assert get_asset_class_for_instrument_id("6408") == "stocks"


def test_get_asset_class_for_instrument_id_invalid():
    """Non-numeric input returnt None ohne Crash."""
    from app.market_scanner import get_asset_class_for_instrument_id
    assert get_asset_class_for_instrument_id("not-a-number") is None
    assert get_asset_class_for_instrument_id(None) is None


def test_get_asset_class_for_instrument_id_float_input():
    """v37h Review-Fix #4 (09.05.2026): float-input wird via int() konvertiert."""
    from app.market_scanner import get_asset_class_for_instrument_id
    assert get_asset_class_for_instrument_id(5003.0) == "commodities"
    assert get_asset_class_for_instrument_id(6408.7) == "stocks"  # truncates


def test_get_asset_class_for_symbol_known():
    from app.market_scanner import get_asset_class_for_symbol
    assert get_asset_class_for_symbol("AAPL") == "stocks"
    assert get_asset_class_for_symbol("COPPER") == "commodities"
    assert get_asset_class_for_symbol("SLV") == "etf"


def test_get_asset_class_for_symbol_unknown():
    from app.market_scanner import get_asset_class_for_symbol
    assert get_asset_class_for_symbol("UNKNOWNXYZ") is None
    assert get_asset_class_for_symbol("") is None
    assert get_asset_class_for_symbol(None) is None


# ============================================================
# IbkrBroker config-Loading Tests
# ============================================================

def test_broker_loads_asset_class_settings_from_config():
    """IbkrBroker laedt asset_class_order_settings aus config."""
    from app.ibkr_client import IbkrBroker
    config = {
        "ibkr": {"client_id": 1},
        "asset_class_order_settings": {
            "stocks":      {"slippage_pct": 0.5, "trading_hours": "extended"},
            "commodities": {"slippage_pct": 1.5, "trading_hours": "rth_only"},
        }
    }
    broker = IbkrBroker(config)
    assert broker._asset_class_order_settings["stocks"]["slippage_pct"] == 0.5
    assert broker._asset_class_order_settings["commodities"]["trading_hours"] == "rth_only"


def test_broker_handles_missing_asset_class_settings():
    """Wenn config kein asset_class_order_settings hat: leerer dict (no-op)."""
    from app.ibkr_client import IbkrBroker
    config = {"ibkr": {"client_id": 1}}  # no asset_class_order_settings
    broker = IbkrBroker(config)
    assert broker._asset_class_order_settings == {}


def test_broker_none_config_has_empty_asset_class_settings():
    """v37h Task 2c-Pendant: IbkrBroker(None) auch hier None-safe."""
    from app.ibkr_client import IbkrBroker
    broker = IbkrBroker(None)
    assert broker._asset_class_order_settings == {}


# ============================================================
# _place_market_order Asset-Class-Logik Tests
# ============================================================

# ============================================================
# _resolve_order_settings Tests (isoliert von ib_insync)
# ============================================================

def _make_broker(settings=None, instance_slippage=0.5):
    """Helper: IbkrBroker mit asset_class_order_settings (kein ib_insync nötig)."""
    from app.ibkr_client import IbkrBroker
    config = {
        "ibkr": {"client_id": 1, "limit_slippage_pct": instance_slippage},
        "asset_class_order_settings": settings or {}
    }
    return IbkrBroker(config)


def test_resolve_order_settings_commodity_uses_15_pct():
    """Commodity (instrument_id=5003=SILVER) -> 1.5% + outsideRth=False."""
    settings = {
        "stocks":      {"slippage_pct": 0.5, "trading_hours": "extended"},
        "commodities": {"slippage_pct": 1.5, "trading_hours": "rth_only"},
    }
    broker = _make_broker(settings)
    result = broker._resolve_order_settings(instrument_id=5003)  # SILVER
    assert result["slippage_pct"] == 1.5
    assert result["outside_rth"] is False  # rth_only -> outsideRth=False
    assert result["asset_class"] == "commodities"


def test_resolve_order_settings_stock_uses_05_pct_extended():
    """Stock (instrument_id=6408=AAPL) -> 0.5% + outsideRth=True."""
    settings = {
        "stocks":      {"slippage_pct": 0.5, "trading_hours": "extended"},
        "commodities": {"slippage_pct": 1.5, "trading_hours": "rth_only"},
    }
    broker = _make_broker(settings)
    result = broker._resolve_order_settings(instrument_id=6408)  # AAPL
    assert result["slippage_pct"] == 0.5
    assert result["outside_rth"] is True  # extended -> outsideRth=True
    assert result["asset_class"] == "stocks"


def test_resolve_order_settings_per_call_override_wins():
    """Explizit limit_slippage_pct=2.0 ueberschreibt Asset-Class-Setting."""
    settings = {"stocks": {"slippage_pct": 0.5, "trading_hours": "extended"}}
    broker = _make_broker(settings)
    result = broker._resolve_order_settings(instrument_id=6408, limit_slippage_pct=2.0)
    assert result["slippage_pct"] == 2.0  # override gewinnt
    # Trading-Hours bleibt aus Asset-Class
    assert result["outside_rth"] is True


def test_resolve_order_settings_unknown_instrument_uses_default():
    """Unknown instrument_id -> _default-Settings."""
    settings = {
        "_default": {"slippage_pct": 0.7, "trading_hours": "extended"},
    }
    broker = _make_broker(settings)
    result = broker._resolve_order_settings(instrument_id=999999999)
    assert result["slippage_pct"] == 0.7
    assert result["outside_rth"] is True
    assert result["asset_class"] is None  # nicht im Universe


def test_resolve_order_settings_no_settings_uses_instance_default():
    """Empty settings -> instance.limit_slippage_pct + extended-default."""
    broker = _make_broker(settings={}, instance_slippage=0.5)
    result = broker._resolve_order_settings(instrument_id=6408)  # AAPL
    assert result["slippage_pct"] == 0.5  # instance-default
    assert result["outside_rth"] is True  # default = extended


def test_resolve_order_settings_etf_separate_from_stocks():
    """ETF (instrument_id=1115=SLV) bekommt etf-Settings, nicht stocks."""
    settings = {
        "stocks": {"slippage_pct": 0.5, "trading_hours": "extended"},
        "etf":    {"slippage_pct": 0.6, "trading_hours": "rth_only"},
    }
    broker = _make_broker(settings)
    result = broker._resolve_order_settings(instrument_id=1115)  # SLV
    assert result["slippage_pct"] == 0.6  # etf-spezifisch
    assert result["outside_rth"] is False  # etf rth_only
    assert result["asset_class"] == "etf"


def test_resolve_order_settings_explicit_none_uses_asset_class():
    """limit_slippage_pct=None (default) nutzt Asset-Class-Setting."""
    settings = {"commodities": {"slippage_pct": 1.5, "trading_hours": "rth_only"}}
    broker = _make_broker(settings)
    result = broker._resolve_order_settings(instrument_id=5003, limit_slippage_pct=None)
    assert result["slippage_pct"] == 1.5  # nicht override


def test_resolve_order_settings_explicit_zero_override_works():
    """limit_slippage_pct=0.0 ist explizit (gilt als override, NICHT als None)."""
    settings = {"commodities": {"slippage_pct": 1.5, "trading_hours": "rth_only"}}
    broker = _make_broker(settings)
    result = broker._resolve_order_settings(instrument_id=5003, limit_slippage_pct=0.0)
    assert result["slippage_pct"] == 0.0  # 0.0 ist intentionaler Override


def test_resolve_order_settings_known_class_but_no_settings_falls_back_to_default():
    """v37h Review-Fix #5 (09.05.2026): instrument_id ist in ASSET_UNIVERSE
    (z.B. SILVER=commodities) aber config hat keinen "commodities"-Eintrag.
    Soll dann _default greifen, NICHT instance-default."""
    settings = {
        "_default": {"slippage_pct": 0.9, "trading_hours": "rth_only"},
        # ABSICHTLICH kein "commodities"-key
    }
    broker = _make_broker(settings, instance_slippage=0.5)
    result = broker._resolve_order_settings(instrument_id=5003)  # SILVER
    assert result["slippage_pct"] == 0.9  # _default, NICHT instance-default 0.5
    assert result["outside_rth"] is False  # _default rth_only
    assert result["asset_class"] == "commodities"  # asset_class trotzdem aufgeloest


def test_resolve_order_settings_empty_dict_class_falls_back_to_default():
    """v37h Review-Fix #3 (09.05.2026): config-Typo wo "commodities": {}
    explizit leerer dict ist. `not ac_settings` faengt das ab."""
    settings = {
        "commodities": {},  # leerer dict (Config-Typo)
        "_default": {"slippage_pct": 0.9, "trading_hours": "rth_only"},
    }
    broker = _make_broker(settings)
    result = broker._resolve_order_settings(instrument_id=5003)
    assert result["slippage_pct"] == 0.9  # _default, weil ac_settings={} -> falsy
