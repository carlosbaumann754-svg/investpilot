"""v37h+2 Q3-10 (15.05.2026) — Adaptive LIMIT->MARKET Close-Order.

Carlos's SLV-Bug 14.05.2026: close_position platzierte LimitOrder mit
0.5% Slippage-Buffer. In schnell fallenden Maerkten verfehlt der Limit-
Preis den Markt sofort -> Order haengt 'Submitted' ohne Fill. SLV-
Position blieb ~21h ungefuellt waehrend Preis weiter fiel von $77.72
auf $70.16 (-9.7%). Bei Cutover mit echtem Geld waere das ein
verlorener SL = unbeschraenkter Drawdown.

Fix: _place_close_order_adaptive() versucht erst LIMIT, faellt bei
Timeout auf MARKET zurueck. Tests verifizieren:
  1. LIMIT fillt vollstaendig -> kein Fallback
  2. LIMIT 0-Fill nach Timeout -> Cancel + MARKET-Fallback
  3. LIMIT partial-Fill -> Cancel + MARKET fuer Rest, weighted avg
  4. Kein Quote -> None Return
  5. Error-Handling: Cancel-Failure
  6. Result-Dict Schema korrekt
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# Skip wenn ib_insync nicht installed (lokal-dev)
_HAS_IBKR = True
try:
    import ib_insync  # noqa: F401
except ImportError:
    _HAS_IBKR = False


@pytest.fixture
def mock_ib_insync(monkeypatch):
    """Mock ib_insync.LimitOrder + MarketOrder ohne echtes ib_insync zu brauchen."""
    if _HAS_IBKR:
        return None  # echtes Modul nutzen falls da

    # Sonst: fake-Module injection
    import types
    fake_module = types.ModuleType("ib_insync")

    class _FakeOrder:
        def __init__(self, action, qty, *args):
            self.action = action
            self.qty = qty
            self.orderId = 9999
            self.parentId = 0
            self.transmit = True
            self.outsideRth = False

    fake_module.LimitOrder = _FakeOrder
    fake_module.MarketOrder = _FakeOrder
    fake_module.StopOrder = _FakeOrder
    monkeypatch.setitem(sys.modules, "ib_insync", fake_module)
    return fake_module


@pytest.fixture
def mock_broker(mock_ib_insync):
    """Build IbkrBroker mit gemockten IB + Quote-Helpers."""
    from app.ibkr_client import IbkrBroker
    broker = IbkrBroker.__new__(IbkrBroker)
    broker.limit_slippage_pct = 0.5
    broker.fill_timeout_s = 0.5  # schnell fuer Tests
    broker.cancel_on_timeout = True

    # Mock _get_ib
    mock_ib = MagicMock()
    mock_ib.sleep = MagicMock(return_value=None)  # kein echtes sleep
    broker._get_ib = MagicMock(return_value=mock_ib)
    return broker, mock_ib


def _make_trade_mock(filled, status="Filled", avg_price=100.0, order_id=1001):
    """Build ein Mock-Trade-Object analog ib_insync.Trade."""
    trade = MagicMock()
    trade.order.orderId = order_id
    trade.orderStatus.filled = filled
    trade.orderStatus.status = status
    trade.orderStatus.avgFillPrice = avg_price
    trade.isDone = MagicMock(return_value=(status in ("Filled", "Cancelled")))
    return trade


# ============================================================
# Happy Path: LIMIT fillt vollstaendig
# ============================================================

def test_adaptive_close_limit_filled_no_fallback(mock_broker):
    """LIMIT fillt komplett in fill_timeout -> kein MARKET-Fallback noetig."""
    broker, mock_ib = mock_broker
    contract = MagicMock(symbol="AAPL")

    limit_trade = _make_trade_mock(filled=100, status="Filled", avg_price=200.50)
    mock_ib.placeOrder.return_value = limit_trade

    with patch("app.ibkr_contract_resolver.get_quote", return_value=200.0):
        result = broker._place_close_order_adaptive(
            contract=contract, action="SELL", qty=100,
            fill_timeout=0.1, purpose="close",
        )

    assert result is not None
    assert result["_used_market_fallback"] is False
    assert result["orderForOpen"]["statusID"] == "Filled"
    assert result["orderForOpen"]["filledQuantity"] == 100
    assert result["_limit_fill_qty"] == 100
    assert result["_market_fill_qty"] == 0
    # Nur 1 placeOrder-Call (kein MARKET-Fallback)
    assert mock_ib.placeOrder.call_count == 1


# ============================================================
# Q3-10 Kern-Fix: LIMIT haengt -> MARKET-Fallback
# ============================================================

def test_adaptive_close_limit_zero_fill_falls_back_to_market(mock_broker):
    """SLV-Bug-Szenario: LIMIT 0-fill nach timeout -> MARKET-Fallback."""
    broker, mock_ib = mock_broker
    contract = MagicMock(symbol="SLV")

    # LIMIT bleibt 'Submitted' mit 0 Fills (= Carlos's Bug)
    limit_trade = _make_trade_mock(filled=0, status="Submitted", order_id=165)
    limit_trade.isDone = MagicMock(return_value=False)  # nie done waehrend wait
    # MARKET-Fallback fillt komplett
    market_trade = _make_trade_mock(filled=1321, status="Filled",
                                     avg_price=70.16, order_id=999)

    # Side-effect: 1. Call gibt LIMIT zurueck, 2. Call MARKET
    mock_ib.placeOrder.side_effect = [limit_trade, market_trade]

    with patch("app.ibkr_contract_resolver.get_quote", return_value=77.72):
        result = broker._place_close_order_adaptive(
            contract=contract, action="SELL", qty=1321,
            fill_timeout=0.1, purpose="sl_close",
        )

    assert result is not None
    assert result["_used_market_fallback"] is True
    assert result["orderForOpen"]["statusID"] == "Filled"
    assert result["orderForOpen"]["filledQuantity"] == 1321
    assert result["_limit_fill_qty"] == 0
    assert result["_market_fill_qty"] == 1321
    assert result["_market_order_id"] == "999"
    # 2 placeOrder calls (LIMIT + MARKET)
    assert mock_ib.placeOrder.call_count == 2
    # cancelOrder muss gerufen worden sein
    mock_ib.cancelOrder.assert_called_once()


# ============================================================
# Partial-Fill: LIMIT teilweise -> MARKET fuer Rest
# ============================================================

def test_adaptive_close_partial_fill_market_completes(mock_broker):
    """LIMIT fillt 60/100, MARKET fillt restliche 40 — weighted-avg."""
    broker, mock_ib = mock_broker
    contract = MagicMock(symbol="EEM")

    # LIMIT: 60 von 100 zu 65.00
    limit_trade = _make_trade_mock(filled=60, status="Submitted",
                                    avg_price=65.00, order_id=200)
    limit_trade.isDone = MagicMock(return_value=False)
    # MARKET: 40 zu 64.80
    market_trade = _make_trade_mock(filled=40, status="Filled",
                                     avg_price=64.80, order_id=201)

    mock_ib.placeOrder.side_effect = [limit_trade, market_trade]

    with patch("app.ibkr_contract_resolver.get_quote", return_value=65.30):
        result = broker._place_close_order_adaptive(
            contract=contract, action="SELL", qty=100,
            fill_timeout=0.1, purpose="close",
        )

    assert result["_used_market_fallback"] is True
    assert result["orderForOpen"]["filledQuantity"] == 100
    assert result["orderForOpen"]["statusID"] == "Filled"
    # Weighted-avg: (60*65.00 + 40*64.80) / 100 = (3900 + 2592) / 100 = 64.92
    assert abs(result["orderForOpen"]["avgFillPrice"] - 64.92) < 0.001


# ============================================================
# Defensive: kein Quote -> None
# ============================================================

def test_adaptive_close_no_quote_returns_none(mock_broker):
    """Wenn get_quote None liefert (IBKR-Drop) -> abbrechen, kein placeOrder."""
    broker, mock_ib = mock_broker
    contract = MagicMock(symbol="UNKNOWN")

    with patch("app.ibkr_contract_resolver.get_quote", return_value=None):
        result = broker._place_close_order_adaptive(
            contract=contract, action="SELL", qty=100,
            fill_timeout=0.1, purpose="close",
        )

    assert result is None
    mock_ib.placeOrder.assert_not_called()


def test_adaptive_close_zero_quote_returns_none(mock_broker):
    """get_quote=0 ist auch invalid -> abbrechen."""
    broker, mock_ib = mock_broker
    contract = MagicMock(symbol="SLV")
    with patch("app.ibkr_contract_resolver.get_quote", return_value=0.0):
        result = broker._place_close_order_adaptive(
            contract=contract, action="SELL", qty=100,
            fill_timeout=0.1, purpose="close",
        )
    assert result is None


# ============================================================
# Action-Direction: BUY-Side Limit-Price-Berechnung
# ============================================================

def test_adaptive_close_buy_side_limit_price_correct(mock_broker):
    """BUY-Close (Short-Cover): Limit-Preis liegt UEBER quote (+0.5%)."""
    broker, mock_ib = mock_broker
    contract = MagicMock(symbol="TSLA")

    limit_trade = _make_trade_mock(filled=50, status="Filled", avg_price=200.99)
    mock_ib.placeOrder.return_value = limit_trade

    with patch("app.ibkr_contract_resolver.get_quote", return_value=200.0):
        result = broker._place_close_order_adaptive(
            contract=contract, action="BUY", qty=50,
            fill_timeout=0.1, purpose="close",
        )

    # Limit-Preis bei BUY = quote * (1 + 0.5%) = 201.00
    assert result["orderForOpen"]["intendedPrice"] == 201.00


def test_adaptive_close_sell_side_limit_price_correct(mock_broker):
    """SELL-Close: Limit-Preis liegt UNTER quote (-0.5%)."""
    broker, mock_ib = mock_broker
    contract = MagicMock(symbol="AAPL")

    limit_trade = _make_trade_mock(filled=100, status="Filled", avg_price=199.05)
    mock_ib.placeOrder.return_value = limit_trade

    with patch("app.ibkr_contract_resolver.get_quote", return_value=200.0):
        result = broker._place_close_order_adaptive(
            contract=contract, action="SELL", qty=100,
            fill_timeout=0.1, purpose="close",
        )

    # Limit-Preis bei SELL = quote * (1 - 0.5%) = 199.00
    assert result["orderForOpen"]["intendedPrice"] == 199.00


# ============================================================
# Cancel-Resilience
# ============================================================

def test_adaptive_close_cancel_failure_still_does_market(mock_broker):
    """Wenn cancelOrder eine Exception wirft, soll MARKET-Fallback trotzdem laufen."""
    broker, mock_ib = mock_broker
    contract = MagicMock(symbol="SLV")

    limit_trade = _make_trade_mock(filled=0, status="Submitted", order_id=300)
    limit_trade.isDone = MagicMock(return_value=False)
    market_trade = _make_trade_mock(filled=100, status="Filled", avg_price=70.16)

    mock_ib.placeOrder.side_effect = [limit_trade, market_trade]
    mock_ib.cancelOrder.side_effect = Exception("Network glitch")

    with patch("app.ibkr_contract_resolver.get_quote", return_value=77.72):
        result = broker._place_close_order_adaptive(
            contract=contract, action="SELL", qty=100,
            fill_timeout=0.1, purpose="sl_close",
        )

    assert result is not None
    assert result["_used_market_fallback"] is True
    assert result["orderForOpen"]["filledQuantity"] == 100
    # MARKET wurde trotz Cancel-Error platziert
    assert mock_ib.placeOrder.call_count == 2


# ============================================================
# Result-Schema-Validation
# ============================================================

def test_adaptive_close_result_schema(mock_broker):
    """Result-Dict enthaelt alle erwarteten Keys fuer Caller (trader.py)."""
    broker, mock_ib = mock_broker
    contract = MagicMock(symbol="AAPL")

    limit_trade = _make_trade_mock(filled=10, status="Filled", avg_price=200.50)
    mock_ib.placeOrder.return_value = limit_trade

    with patch("app.ibkr_contract_resolver.get_quote", return_value=200.0):
        result = broker._place_close_order_adaptive(
            contract=contract, action="SELL", qty=10,
            purpose="close",
        )

    # Top-Level keys
    assert "orderForOpen" in result
    assert "_broker" in result
    assert "_action" in result
    assert "_used_market_fallback" in result
    assert "_limit_fill_qty" in result
    assert "_market_fill_qty" in result
    assert "_market_order_id" in result
    # orderForOpen sub-keys
    ofo = result["orderForOpen"]
    assert "orderID" in ofo
    assert "statusID" in ofo
    assert "filledQuantity" in ofo
    assert "avgFillPrice" in ofo
    assert "intendedPrice" in ofo
    assert "refQuote" in ofo
    # Werte
    assert result["_broker"] == "ibkr"
    assert result["_action"] == "close"


def test_adaptive_close_purpose_propagates_to_action(mock_broker):
    """purpose-Parameter landet im _action-Feld fuer Audit-Trail."""
    broker, mock_ib = mock_broker
    contract = MagicMock(symbol="SLV")

    limit_trade = _make_trade_mock(filled=10, status="Filled")
    mock_ib.placeOrder.return_value = limit_trade

    with patch("app.ibkr_contract_resolver.get_quote", return_value=100.0):
        result = broker._place_close_order_adaptive(
            contract=contract, action="SELL", qty=10,
            purpose="partial_close",
        )

    assert result["_action"] == "partial_close"


# ============================================================
# Q3-12: outsideRth-Propagation (BLOCKER B2 Fix)
# ============================================================

def test_adaptive_close_outsiderth_via_instrument_id(mock_broker):
    """Q3-12: instrument_id triggert _resolve_order_settings -> outsideRth gesetzt."""
    broker, mock_ib = mock_broker
    contract = MagicMock(symbol="SLV")

    limit_trade = _make_trade_mock(filled=100, status="Filled", avg_price=70.16)
    mock_ib.placeOrder.return_value = limit_trade

    # Mock _resolve_order_settings -> outside_rth=True (z.B. fuer Forex/Crypto)
    broker._resolve_order_settings = MagicMock(return_value={
        "slippage_pct": 0.5,
        "outside_rth": True,
        "asset_class": "forex",
        "trading_hours_mode": "always",
    })

    with patch("app.ibkr_contract_resolver.get_quote", return_value=70.0):
        broker._place_close_order_adaptive(
            contract=contract, action="SELL", qty=100,
            purpose="close", instrument_id=39039301,
        )

    # _resolve_order_settings wurde aufgerufen
    broker._resolve_order_settings.assert_called_once_with(39039301, None)
    # LIMIT-Order wurde mit outsideRth=True erstellt
    placed_order = mock_ib.placeOrder.call_args[0][1]
    assert placed_order.outsideRth is True


def test_adaptive_close_outsiderth_default_false_without_instrument_id(mock_broker):
    """Wenn instrument_id=None: Default outsideRth=False (backward-compat)."""
    broker, mock_ib = mock_broker
    contract = MagicMock(symbol="AAPL")

    limit_trade = _make_trade_mock(filled=10, status="Filled")
    mock_ib.placeOrder.return_value = limit_trade
    broker._resolve_order_settings = MagicMock()

    with patch("app.ibkr_contract_resolver.get_quote", return_value=200.0):
        broker._place_close_order_adaptive(
            contract=contract, action="SELL", qty=10,
            purpose="close",  # kein instrument_id
        )

    # _resolve_order_settings nicht gerufen
    broker._resolve_order_settings.assert_not_called()
    # outsideRth Default False
    placed_order = mock_ib.placeOrder.call_args[0][1]
    assert placed_order.outsideRth is False


def test_adaptive_close_outsiderth_settings_error_falls_back_safe(mock_broker):
    """Wenn _resolve_order_settings crashed: Default outsideRth=False, kein Abort."""
    broker, mock_ib = mock_broker
    contract = MagicMock(symbol="AAPL")

    limit_trade = _make_trade_mock(filled=10, status="Filled")
    mock_ib.placeOrder.return_value = limit_trade
    broker._resolve_order_settings = MagicMock(side_effect=Exception("config broken"))

    with patch("app.ibkr_contract_resolver.get_quote", return_value=200.0):
        result = broker._place_close_order_adaptive(
            contract=contract, action="SELL", qty=10,
            purpose="close", instrument_id=265598,
        )

    # Trotz Exception: Order wurde platziert mit Default outsideRth=False
    assert result is not None
    placed_order = mock_ib.placeOrder.call_args[0][1]
    assert placed_order.outsideRth is False


def test_adaptive_close_market_fallback_inherits_outsiderth(mock_broker):
    """MARKET-Fallback muss gleichen outsideRth-Mode haben wie LIMIT."""
    broker, mock_ib = mock_broker
    contract = MagicMock(symbol="EUR.USD")

    limit_trade = _make_trade_mock(filled=0, status="Submitted")
    limit_trade.isDone = MagicMock(return_value=False)
    market_trade = _make_trade_mock(filled=10000, status="Filled", avg_price=1.085)

    mock_ib.placeOrder.side_effect = [limit_trade, market_trade]
    broker._resolve_order_settings = MagicMock(return_value={
        "slippage_pct": 0.5,
        "outside_rth": True,
        "asset_class": "forex",
        "trading_hours_mode": "always",
    })

    with patch("app.ibkr_contract_resolver.get_quote", return_value=1.085):
        broker._place_close_order_adaptive(
            contract=contract, action="SELL", qty=10000,
            fill_timeout=0.1, purpose="close", instrument_id=12345,
        )

    # 2 Orders platziert, beide mit outsideRth=True
    assert mock_ib.placeOrder.call_count == 2
    limit_order_placed = mock_ib.placeOrder.call_args_list[0][0][1]
    market_order_placed = mock_ib.placeOrder.call_args_list[1][0][1]
    assert limit_order_placed.outsideRth is True
    assert market_order_placed.outsideRth is True


# ============================================================
# Statuscodes bei nicht-vollstaendigem Fill
# ============================================================

def test_adaptive_close_zero_fill_no_market_returns_submitted(mock_broker):
    """Edge-Case: LIMIT haengt, MARKET fillt auch nicht (z.B. Markt zu).

    -> Status sollte den finalen LIMIT-Status zurueckgeben (Submitted),
    nicht Filled. Caller-trader.py soll wissen dass nichts passiert ist.
    """
    broker, mock_ib = mock_broker
    contract = MagicMock(symbol="SLV")

    limit_trade = _make_trade_mock(filled=0, status="Submitted")
    limit_trade.isDone = MagicMock(return_value=False)
    market_trade = _make_trade_mock(filled=0, status="Submitted")
    market_trade.isDone = MagicMock(return_value=False)

    mock_ib.placeOrder.side_effect = [limit_trade, market_trade]

    with patch("app.ibkr_contract_resolver.get_quote", return_value=77.72):
        result = broker._place_close_order_adaptive(
            contract=contract, action="SELL", qty=100,
            fill_timeout=0.1, purpose="sl_close",
        )

    # Beide failed -> Status nicht Filled
    assert result["orderForOpen"]["statusID"] in ("Submitted", "PartiallyFilled")
    assert result["orderForOpen"]["filledQuantity"] == 0
