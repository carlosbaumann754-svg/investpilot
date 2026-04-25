"""
Regression-Tests fuer die 4 Production-Bugs aus W4 Live-Smoke-Test.

Verhindert dass jemand (ich in 6 Monaten) versehentlich einen davon
zurueckbringt. Kontext: jeder Bug hat Live-Trading kaputtgemacht und
musste mit Hotfix gefixt werden — Tests sind die Versicherung.

W4 Bug 1: ib_insync nicht in requirements.txt
W4 Bug 2: IBC ReadOnlyApi defaulted to 'yes' (Infra, nicht testbar in Python)
W4 Bug 3: ticker.marketPrice ist Method nicht Attribut
W4 Bug 4: MarketOrder vom Paper-Account abgelehnt -> LimitOrder default
W6 Hotfix v21: get_portfolio() returned falsche Top-Level-Keys
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

# Same fake ib_insync setup as test_ibkr_write_ops.py
class FakeContract:
    def __init__(self, secType="STK", symbol="?", exchange="SMART", currency="USD",
                 conId=0, primaryExchange=""):
        self.secType = secType; self.symbol = symbol; self.exchange = exchange
        self.currency = currency; self.conId = conId; self.primaryExchange = primaryExchange


def _install_fake_ib_insync():
    fake = MagicMock()
    fake.IB = MagicMock; fake.Contract = FakeContract
    fake.Stock = lambda s, e, c: FakeContract(secType="STK", symbol=s, exchange=e, currency=c)
    fake.Crypto = lambda s, e, c: FakeContract(secType="CRYPTO", symbol=s, exchange=e, currency=c)
    fake.Forex = lambda p: FakeContract(secType="CASH", symbol=p[:3], exchange="IDEALPRO", currency=p[3:])
    fake.MarketOrder = MagicMock; fake.StopOrder = MagicMock; fake.LimitOrder = MagicMock
    sys.modules["ib_insync"] = fake


_install_fake_ib_insync()


# ----------------------------------------------------------------------
# W4 Bug 1: requirements.txt
# ----------------------------------------------------------------------

def test_w4_bug1_ib_insync_in_requirements():
    """ib_insync MUSS in requirements.txt stehen — sonst Container-Rebuild kaputt.

    Live-Symptom: ModuleNotFoundError: No module named 'ib_insync' beim
    Bot-Start nach docker compose up --build.
    """
    req = (Path(__file__).parent.parent / "requirements.txt").read_text()
    assert "ib_insync" in req, (
        "ib_insync FEHLT in requirements.txt — Container-Rebuild wird "
        "ohne IBKR-Support hochfahren! Siehe CLAUDE.md v19 Bug 1."
    )


# ----------------------------------------------------------------------
# W4 Bug 3: ticker.marketPrice als Method statt Attribut
# ----------------------------------------------------------------------

def test_w4_bug3_safe_num_handles_method_callable():
    """_safe_num() MUSS callable Werte (z.B. ticker.marketPrice in
    ib_insync 0.9.86) korrekt behandeln, nicht direkt mit > 0 vergleichen.

    Live-Symptom: TypeError: '>' not supported between 'method' and 'int'
    -> get_quote() crasht -> alle Orders blocked.
    """
    from app.ibkr_contract_resolver import _safe_num

    # Method (callable) das einen Float zurueckgibt — wie ticker.marketPrice
    method_returning_float = lambda: 270.50
    assert _safe_num(method_returning_float) == 270.50

    # Method das None zurueckgibt
    method_returning_none = lambda: None
    assert _safe_num(method_returning_none) is None

    # Method das exception wirft
    def method_raising():
        raise RuntimeError("no quote")
    assert _safe_num(method_raising) is None

    # Direkter float-Wert (wie ticker.last)
    assert _safe_num(150.75) == 150.75
    assert _safe_num(0) is None  # 0 ist kein gueltiger Quote
    assert _safe_num(-5) is None  # negativ ist kein gueltiger Quote
    assert _safe_num(None) is None
    assert _safe_num(float("nan")) is None


# ----------------------------------------------------------------------
# W4 Bug 4: MarketOrder default abgelehnt -> LimitOrder default
# ----------------------------------------------------------------------

def test_w4_bug4_default_order_type_is_limit():
    """IbkrBroker.default_order_type MUSS 'LIMIT' sein (instance-property).

    Live-Symptom: 'No market data on major exchange for market order' →
    Order Cancelled. Paper-Accounts ohne MD-Abo lehnen MarketOrders ab.

    Konfigurierbar via config.json ibkr.default_order_type, aber Default
    muss 'LIMIT' sein um den Live-Bug zu verhindern.
    """
    from app.ibkr_client import IbkrBroker
    broker = IbkrBroker({})  # Empty config -> Default LIMIT
    assert broker.default_order_type == "LIMIT", (
        f"IbkrBroker default_order_type ist '{broker.default_order_type}', "
        f"sollte 'LIMIT' sein! Siehe CLAUDE.md v19 Bug 4."
    )


def test_w4_bug4_limit_slippage_default_value():
    """limit_slippage_pct MUSS als instance-attribute mit sicherem Default."""
    from app.ibkr_client import IbkrBroker
    broker = IbkrBroker({})  # Empty config
    val = broker.limit_slippage_pct
    # Sanity: zwischen 0.1% und 2% (sonst entweder zu eng oder zu loose)
    assert 0.1 <= val <= 2.0, f"limit_slippage_pct={val} ausserhalb Sanity-Bereich"


def test_v22_order_config_overridable_via_config_dict():
    """ibkr.fill_timeout_s, cancel_on_timeout, limit_slippage_pct, default_order_type
    muessen via config.json overridebar sein (nicht hardcoded)."""
    from app.ibkr_client import IbkrBroker
    cfg = {"ibkr": {
        "fill_timeout_s": 60,
        "cancel_on_timeout": False,
        "limit_slippage_pct": 1.0,
        "default_order_type": "MARKET",
    }}
    broker = IbkrBroker(cfg)
    assert broker.fill_timeout_s == 60
    assert broker.cancel_on_timeout is False
    assert broker.limit_slippage_pct == 1.0
    assert broker.default_order_type == "MARKET"


# ----------------------------------------------------------------------
# W6 Hotfix v21: get_portfolio() Key-Compatibility mit eToro
# ----------------------------------------------------------------------

def test_v21_hotfix_get_portfolio_returns_etoro_compatible_keys():
    """IbkrBroker.get_portfolio() MUSS 'credit' und 'unrealizedPnL' als
    Top-Level-Keys liefern (nicht nur 'creditByRealizedEquity'/'availableCash').

    Live-Symptom: trader.py liest portfolio.get('credit') → 0 → triggert
    sofort TAGES-DRAWDOWN-STOP -100% → Bot pausiert. Siehe CLAUDE.md
    Hotfix-Commit nach v20.
    """
    from app.ibkr_client import IbkrBroker

    broker = IbkrBroker({})
    ib_mock = MagicMock()
    broker._ib = ib_mock
    ib_mock.isConnected.return_value = True
    ib_mock.positions.return_value = []

    # Mock account values (IBKR liefert das so)
    av_mocks = []
    for tag, value, currency in [
        ("NetLiquidation", "1062145.27", "USD"),
        ("AvailableFunds", "1062052.36", "USD"),
        ("UnrealizedPnL", "0.00", "USD"),
        ("RealizedPnL", "0.00", "USD"),
        ("GrossPositionValue", "0.00", "USD"),
    ]:
        av = MagicMock(); av.tag = tag; av.value = value; av.currency = currency
        av_mocks.append(av)
    ib_mock.accountValues.return_value = av_mocks

    portfolio = broker.get_portfolio()

    # Diese 3 Keys sind ETORO-STANDARD und werden von trader.py gelesen!
    assert "credit" in portfolio, "FEHLT 'credit' (eToro-Standard, trader.py liest das)"
    assert "unrealizedPnL" in portfolio, "FEHLT 'unrealizedPnL' (eToro-Standard)"
    assert "positions" in portfolio, "FEHLT 'positions' (eToro-Standard)"

    # Werte muessen plausibel sein
    assert portfolio["credit"] == 1062052.36
    assert portfolio["unrealizedPnL"] == 0.0
    assert portfolio["positions"] == []

    # Legacy-Aliases muessen weiter da sein (backwards-compat)
    assert "creditByRealizedEquity" in portfolio
    assert "availableCash" in portfolio


def test_v21_currency_filter_accepts_empty_currency():
    """_get_account_value MUSS Werte mit leerer currency akzeptieren.

    IBKR liefert NetLiquidation manchmal mit currency='' fuer aggregierte Werte.
    Vorher: filter `currency in ('USD', 'BASE')` schloss die aus -> equity=None.
    """
    from app.ibkr_client import IbkrBroker

    broker = IbkrBroker({})
    ib_mock = MagicMock()
    broker._ib = ib_mock
    ib_mock.isConnected.return_value = True

    # Nur EIN match, mit leerer currency
    av = MagicMock(); av.tag = "NetLiquidation"; av.value = "999000.00"; av.currency = ""
    ib_mock.accountValues.return_value = [av]

    eq = broker.get_equity()
    assert eq == 999000.0, "leere currency darf nicht ignoriert werden — Fallback fehlt"


def test_v21_currency_filter_prefers_usd_over_eur():
    """Wenn mehrere Currencies vorhanden, MUSS USD bevorzugt werden."""
    from app.ibkr_client import IbkrBroker

    broker = IbkrBroker({})
    ib_mock = MagicMock()
    broker._ib = ib_mock
    ib_mock.isConnected.return_value = True

    av1 = MagicMock(); av1.tag = "NetLiquidation"; av1.value = "850000.00"; av1.currency = "EUR"
    av2 = MagicMock(); av2.tag = "NetLiquidation"; av2.value = "1000000.00"; av2.currency = "USD"
    ib_mock.accountValues.return_value = [av1, av2]

    eq = broker.get_equity()
    assert eq == 1000000.0, "USD muss bevorzugt werden, nicht EUR"


# ----------------------------------------------------------------------
# Bonus: Port 4004 Konstante (W2 Discovery)
# ----------------------------------------------------------------------

def test_w2_default_ibg_port_is_4004_not_4002():
    """IBG_PORT default MUSS 4004 sein (socat-bridge), nicht 4002 (raw IBG).

    Sonst landet jeder neue Dev wieder in der Connected->Disconnected-Falle.
    """
    from app.ibkr_client import IBG_PORT
    assert IBG_PORT == 4004, (
        f"IBG_PORT={IBG_PORT}, sollte 4004 sein (socat-bridge im "
        f"gnzsnz/ib-gateway Image). Port 4002 wird vom Container nur auf "
        f"127.0.0.1 exposed und akzeptiert keine Container-Network-Connections."
    )
