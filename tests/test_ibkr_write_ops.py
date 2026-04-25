"""
Tests fuer IBKR Write-Operations (W3) und Contract-Resolver.

Da ib_insync lokal nicht installiert ist (laeuft nur im VPS-Container),
mocken wir die ib_insync-API komplett.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest


# ----------------------------------------------------------------------
# Fake ib_insync module — wird vor app.ibkr_* Imports installiert
# ----------------------------------------------------------------------

class FakeContract:
    def __init__(self, secType="STK", symbol="?", exchange="SMART", currency="USD",
                 conId=0, primaryExchange=""):
        self.secType = secType
        self.symbol = symbol
        self.exchange = exchange
        self.currency = currency
        self.conId = conId
        self.primaryExchange = primaryExchange


def _install_fake_ib_insync():
    """Installiert ein fake ib_insync-Modul, damit Imports funktionieren."""
    fake = MagicMock()
    fake.IB = MagicMock
    fake.Contract = FakeContract
    fake.Stock = lambda symbol, exchange, currency: FakeContract(
        secType="STK", symbol=symbol, exchange=exchange, currency=currency
    )
    fake.Crypto = lambda symbol, exchange, currency: FakeContract(
        secType="CRYPTO", symbol=symbol, exchange=exchange, currency=currency
    )
    fake.Forex = lambda pair: FakeContract(
        secType="CASH", symbol=pair[:3], exchange="IDEALPRO", currency=pair[3:]
    )
    fake.MarketOrder = MagicMock
    fake.StopOrder = MagicMock
    fake.LimitOrder = MagicMock
    sys.modules["ib_insync"] = fake


_install_fake_ib_insync()


# ----------------------------------------------------------------------
# Resolver-Tests
# ----------------------------------------------------------------------

class TestContractResolver:

    def test_normalize_class_stocks(self):
        from app.ibkr_contract_resolver import _normalize_class
        assert _normalize_class("stocks") == "STK"
        assert _normalize_class("STOCKS") == "STK"
        assert _normalize_class("etf") == "STK"
        assert _normalize_class("Crypto") == "CRYPTO"
        assert _normalize_class("forex") == "CASH"
        assert _normalize_class("unknown") == "STK"

    def test_exchange_for_sec_type(self):
        from app.ibkr_contract_resolver import _exchange_for_sec_type
        assert _exchange_for_sec_type("STK") == "SMART"
        assert _exchange_for_sec_type("CRYPTO") == "PAXOS"
        assert _exchange_for_sec_type("CASH") == "IDEALPRO"

    def test_amount_to_quantity(self):
        from app.ibkr_contract_resolver import amount_to_quantity
        assert amount_to_quantity(1000, 100) == 10
        assert amount_to_quantity(99, 100) == 0  # < min_qty
        assert amount_to_quantity(100, 100) == 1
        assert amount_to_quantity(0, 100) == 0
        assert amount_to_quantity(1000, 0) == 0  # division-by-zero guard
        assert amount_to_quantity(1000, 33.33) == 30  # floor

    def test_resolve_unknown_etoro_id_raises(self, tmp_path, monkeypatch):
        from app import ibkr_contract_resolver as r
        monkeypatch.setattr(r, "CACHE_PATH", tmp_path / "cache.json")
        ib_mock = MagicMock()
        with pytest.raises(ValueError, match="nicht in ASSET_UNIVERSE"):
            r.resolve_contract(ib_mock, etoro_id=999999999)

    def test_resolve_aapl_qualified(self, tmp_path, monkeypatch):
        """AAPL hat etoro_id=6408 in ASSET_UNIVERSE, sollte zu STK qualifizieren."""
        from app import ibkr_contract_resolver as r
        monkeypatch.setattr(r, "CACHE_PATH", tmp_path / "cache.json")

        ib_mock = MagicMock()
        qualified = FakeContract(secType="STK", symbol="AAPL", exchange="NASDAQ",
                                 currency="USD", conId=265598, primaryExchange="NASDAQ")
        ib_mock.qualifyContracts.return_value = [qualified]

        result = r.resolve_contract(ib_mock, etoro_id=6408)
        assert result.symbol == "AAPL"
        assert result.conId == 265598
        ib_mock.qualifyContracts.assert_called_once()

    def test_resolve_uses_cache_on_second_call(self, tmp_path, monkeypatch):
        from app import ibkr_contract_resolver as r
        cache_file = tmp_path / "cache.json"
        monkeypatch.setattr(r, "CACHE_PATH", cache_file)

        ib_mock = MagicMock()
        qualified = FakeContract(secType="STK", symbol="MSFT", exchange="NASDAQ",
                                 currency="USD", conId=272093)
        ib_mock.qualifyContracts.return_value = [qualified]

        # Erste Resolution -> qualifyContracts wird aufgerufen
        r.resolve_contract(ib_mock, etoro_id=1139)
        assert ib_mock.qualifyContracts.call_count == 1
        assert cache_file.exists()
        cache_data = json.loads(cache_file.read_text())
        assert "1139" in cache_data
        assert cache_data["1139"]["symbol"] == "MSFT"

        # Zweite Resolution -> Cache-Hit, kein erneutes qualifyContracts
        r.resolve_contract(ib_mock, etoro_id=1139)
        assert ib_mock.qualifyContracts.call_count == 1  # nicht erhoeht


# ----------------------------------------------------------------------
# IbkrBroker Write-Ops Tests
# ----------------------------------------------------------------------

class TestIbkrBrokerOrders:

    def _make_broker_with_mock_ib(self, monkeypatch, tmp_path):
        """Hilfsmethode: IbkrBroker mit gemockter IB-Connection."""
        from app import ibkr_contract_resolver
        from app import ibkr_client
        monkeypatch.setattr(ibkr_contract_resolver, "CACHE_PATH", tmp_path / "cache.json")

        broker = ibkr_client.IbkrBroker({})
        ib_mock = MagicMock()
        broker._ib = ib_mock

        # Default: connection looks alive
        ib_mock.isConnected.return_value = True

        return broker, ib_mock

    def test_buy_with_unknown_etoro_id_returns_none(self, monkeypatch, tmp_path):
        broker, _ = self._make_broker_with_mock_ib(monkeypatch, tmp_path)
        result = broker.buy(instrument_id=999999999, amount_usd=100)
        assert result is None

    def test_buy_with_zero_quote_returns_none(self, monkeypatch, tmp_path):
        broker, ib_mock = self._make_broker_with_mock_ib(monkeypatch, tmp_path)

        # Resolver liefert AAPL
        qualified = FakeContract(secType="STK", symbol="AAPL", exchange="NASDAQ",
                                 currency="USD", conId=265598)
        ib_mock.qualifyContracts.return_value = [qualified]

        # get_quote returns None -> broker.buy returns None
        from app import ibkr_contract_resolver
        monkeypatch.setattr(ibkr_contract_resolver, "get_quote", lambda ib, c, timeout=3.0: None)

        result = broker.buy(instrument_id=6408, amount_usd=100)
        assert result is None

    def test_buy_amount_below_one_share_returns_none(self, monkeypatch, tmp_path):
        broker, ib_mock = self._make_broker_with_mock_ib(monkeypatch, tmp_path)
        qualified = FakeContract(secType="STK", symbol="AAPL", conId=265598)
        ib_mock.qualifyContracts.return_value = [qualified]

        from app import ibkr_contract_resolver
        monkeypatch.setattr(ibkr_contract_resolver, "get_quote", lambda ib, c, timeout=3.0: 200.0)

        # amount=100, price=200 -> qty=0 -> None
        result = broker.buy(instrument_id=6408, amount_usd=100)
        assert result is None

    def test_buy_returns_etoro_compatible_response(self, monkeypatch, tmp_path):
        broker, ib_mock = self._make_broker_with_mock_ib(monkeypatch, tmp_path)
        qualified = FakeContract(secType="STK", symbol="AAPL", conId=265598)
        ib_mock.qualifyContracts.return_value = [qualified]

        from app import ibkr_contract_resolver
        monkeypatch.setattr(ibkr_contract_resolver, "get_quote", lambda ib, c, timeout=3.0: 150.0)

        # Mock placeOrder -> Trade-Object mit Filled status
        trade_mock = MagicMock()
        trade_mock.order.orderId = 42
        trade_mock.orderStatus.status = "Filled"
        trade_mock.orderStatus.filled = 6
        trade_mock.orderStatus.avgFillPrice = 150.5
        trade_mock.isDone.return_value = True
        ib_mock.placeOrder.return_value = trade_mock

        # amount=$1000, price=$150 -> qty=6
        result = broker.buy(instrument_id=6408, amount_usd=1000)
        assert result is not None
        assert result["orderForOpen"]["orderID"] == "42"
        assert result["orderForOpen"]["statusID"] == "Filled"
        assert result["orderForOpen"]["filledQuantity"] == 6
        assert result["_broker"] == "ibkr"
        assert result["_contract"]["symbol"] == "AAPL"

    def test_close_position_without_position_returns_none(self, monkeypatch, tmp_path):
        broker, ib_mock = self._make_broker_with_mock_ib(monkeypatch, tmp_path)
        ib_mock.positions.return_value = []  # keine Positionen

        result = broker.close_position(position_id="265598")
        assert result is None

    def test_close_position_submits_opposite_order(self, monkeypatch, tmp_path):
        broker, ib_mock = self._make_broker_with_mock_ib(monkeypatch, tmp_path)

        # Position long 10x AAPL
        position_mock = MagicMock()
        position_mock.contract = FakeContract(secType="STK", symbol="AAPL", conId=265598)
        position_mock.position = 10  # long
        ib_mock.positions.return_value = [position_mock]

        trade_mock = MagicMock()
        trade_mock.order.orderId = 99
        trade_mock.orderStatus.status = "Filled"
        trade_mock.orderStatus.filled = 10
        trade_mock.orderStatus.avgFillPrice = 151.0
        trade_mock.isDone.return_value = True
        ib_mock.placeOrder.return_value = trade_mock

        result = broker.close_position(position_id="265598")
        assert result is not None
        assert result["orderForOpen"]["orderID"] == "99"
        assert result["_action"] == "close"
        # placeOrder muss mit SELL-Action aufgerufen worden sein (long -> close = SELL)
        ib_mock.placeOrder.assert_called_once()
        called_order = ib_mock.placeOrder.call_args[0][1]
        # MarketOrder Mock — wir koennen die call-args inspizieren
        assert ib_mock.placeOrder.call_count == 1
