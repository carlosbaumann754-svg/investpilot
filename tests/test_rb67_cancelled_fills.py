"""R-B67 (11.09.2026): Verkaeufe, die nach dem Warte-Fenster fuellen, duerfen
nicht als "cancelled" in der Historie landen.

LIVE-FUND: Der INSP-Verkauf am 11.09. (+2165 USD) stand als "cancelled" in
trade_history.json, obwohl IBKR ihn voll gefuellt hat (realizedPNL 2116.8).
Insgesamt 12 solche Faelle, Summe +10813 USD, fast nur Gewinner —
systematisch, weil TRAILING_SL_CLOSE (der Gewinn-Exit des Alt-Motors) bei
schnellen Bewegungen besonders oft die LIMIT-Stufe verfehlt.

URSACHE (zwei Teile):
1. _place_close_order_adaptive uebernimmt bei 0 Fills im Warte-Fenster den
   Status der STORNIERTEN LIMIT-Stufe ("Cancelled") — obwohl die MARKET-
   Order noch laeuft und Sekunden spaeter fuellt.
2. Die Selbstheilung des E27-Trackers greift nie: er sucht den Eintrag per
   order_id, aber KEIN Close-Eintrag traegt eine (live: 0 von 254 Closes;
   alle 637 Kauf-Eintraege haben eine).

Gegenprobe: Echte Stornos ohne Fill bleiben "cancelled" — der
Phantom-Round-Trip-Schutz aus R-B54 muss erhalten bleiben.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ====================================================================
# Teil 1: Status-Aggregation im adaptiven Close
# ====================================================================

def _trade_mock(filled, status, avg_price=0.0, order_id=1001):
    t = MagicMock()
    t.order.orderId = order_id
    t.orderStatus.filled = filled
    t.orderStatus.status = status
    t.orderStatus.avgFillPrice = avg_price
    t.isDone = MagicMock(return_value=(status in ("Filled", "Cancelled")))
    return t


@pytest.fixture
def mock_ib_insync(monkeypatch):
    """ib_insync ist lokal nicht installiert (nur im Container) — Fake-Modul
    injizieren, analog tests/test_close_order_adaptive.py."""
    try:
        import ib_insync  # noqa: F401
        return None
    except ImportError:
        pass

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
def broker(mock_ib_insync):
    from app.ibkr_client import IbkrBroker
    b = IbkrBroker.__new__(IbkrBroker)
    b.limit_slippage_pct = 0.5
    b.fill_timeout_s = 0.1
    b.cancel_on_timeout = True
    b._e27_enabled = False
    b._tracker = None
    mock_ib = MagicMock()
    mock_ib.sleep = MagicMock(return_value=None)
    b._get_ib = MagicMock(return_value=mock_ib)
    return b, mock_ib


def test_limit_storniert_market_laeuft_noch_ist_nicht_cancelled(broker):
    """DER LIVE-FALL (INSP): LIMIT laeuft ab und wird storniert, MARKET wird
    platziert und fuellt erst NACH dem Warte-Fenster.

    Vorher: Status = "Cancelled" (von der abgelaufenen LIMIT-Stufe) -> der
    Trade landet als storniert in der Historie und wird aus allen
    Round-Trip-Metriken gefiltert.
    Jetzt: Der Status der MARKET-Order ist massgeblich.
    """
    b, mock_ib = broker
    contract = MagicMock(symbol="INSP")

    limit_trade = _trade_mock(filled=0, status="Cancelled", order_id=1322)
    market_trade = _trade_mock(filled=0, status="Submitted", order_id=1323)
    market_trade.isDone = MagicMock(return_value=False)
    mock_ib.placeOrder.side_effect = [limit_trade, market_trade]

    with patch("app.ibkr_contract_resolver.get_quote", return_value=72.25):
        result = b._place_close_order_adaptive(
            contract=contract, action="SELL", qty=205,
            fill_timeout=0.1, purpose="close", instrument_id=316490710,
        )

    status = result["orderForOpen"]["statusID"]
    assert status != "Cancelled", (
        "Status der abgelaufenen LIMIT-Stufe wurde uebernommen — der Trade "
        "wird dadurch faelschlich als storniert verbucht (12 Live-Faelle, "
        "zusammen +10813 USD)")
    assert status == "Submitted"


def test_close_result_traegt_die_effektive_order_id(broker):
    """Bei MARKET-Fallback muss die zurueckgegebene orderID die der MARKET-
    Order sein — sonst zeigt die Historie auf die stornierte LIMIT-Order und
    der E27-Tracker kann den spaeteren Fill nicht zuordnen."""
    b, mock_ib = broker
    contract = MagicMock(symbol="INSP")

    limit_trade = _trade_mock(filled=0, status="Cancelled", order_id=1322)
    market_trade = _trade_mock(filled=0, status="Submitted", order_id=1323)
    market_trade.isDone = MagicMock(return_value=False)
    mock_ib.placeOrder.side_effect = [limit_trade, market_trade]

    with patch("app.ibkr_contract_resolver.get_quote", return_value=72.25):
        result = b._place_close_order_adaptive(
            contract=contract, action="SELL", qty=205,
            fill_timeout=0.1, purpose="close",
        )

    assert result["orderForOpen"]["orderID"] == "1323"


def test_echter_storno_ohne_fill_bleibt_cancelled(broker):
    """GEGENPROBE (R-B54-Phantom-Schutz): Wurde keine MARKET-Order platziert
    und die LIMIT-Order ist storniert, bleibt der Status "Cancelled"."""
    b, mock_ib = broker
    contract = MagicMock(symbol="SLV")

    limit_trade = _trade_mock(filled=0, status="Cancelled", order_id=1400)
    mock_ib.placeOrder.return_value = limit_trade
    b.cancel_on_timeout = False  # kein MARKET-Fallback

    with patch("app.ibkr_contract_resolver.get_quote", return_value=77.72):
        result = b._place_close_order_adaptive(
            contract=contract, action="SELL", qty=100,
            fill_timeout=0.1, purpose="sl_close",
        )

    assert result["orderForOpen"]["statusID"] == "Cancelled"


# ====================================================================
# Teil 2: order_id landet im Close-Eintrag
# ====================================================================

def test_close_eintrag_bekommt_order_id():
    """Ohne order_id findet der E27-Tracker den Eintrag nie und kann einen
    spaeter eintreffenden Fill nicht nachtragen (live: 0 von 254 Closes
    hatten eine)."""
    from app.trader import _attach_fill_prices
    entry = {"action": "TRAILING_SL_CLOSE", "symbol": "INSP"}
    result = {"orderForOpen": {"orderID": "1323", "statusID": "Submitted",
                               "filledQuantity": 0, "avgFillPrice": 0.0}}
    out = _attach_fill_prices(entry, result)
    assert out.get("order_id") == "1323"


# ====================================================================
# Teil 3: Selbstheilung des Trackers
# ====================================================================

def _tracker():
    from app.order_status_tracker import OrderStatusTracker
    t = OrderStatusTracker.__new__(OrderStatusTracker)
    t._pending = {}
    t._status_mapper = None  # lazy-Import-Feld, sonst AttributeError
    return t


def test_tracker_traegt_spaeten_fill_nach():
    """Kommt der Fill nach dem Warte-Fenster, muss der Tracker den Eintrag
    per order_id finden und den Status auf "executed" heben."""
    t = _tracker()
    history = [{"action": "TRAILING_SL_CLOSE", "symbol": "INSP",
                "order_id": "1323", "status": "submitted"}]

    with patch("app.config_manager.load_json", return_value=history), \
         patch("app.config_manager.save_json", return_value=None), \
         patch("app.config_manager._get_file_lock", return_value=MagicMock()):
        t._update_trade_history(
            {"symbol": "INSP", "order_id": "1323",
             "trade_entry_snapshot": {"order_id": "1323", "symbol": "INSP"}},
            "Filled", 205.0, 71.08)

    assert history[0]["status"] == "executed"
    assert history[0]["filled_qty"] == 205.0


def test_tracker_stuft_gefuellten_trade_nicht_zurueck():
    """Ein bereits als gefuellt verbuchter Trade darf durch ein spaeteres
    Cancel-Event (z.B. der zugehoerigen Schutz-Order) nicht auf "cancelled"
    zurueckfallen."""
    t = _tracker()
    history = [{"action": "TRAILING_SL_CLOSE", "symbol": "INSP",
                "order_id": "1323", "status": "executed", "filled_qty": 205}]

    with patch("app.config_manager.load_json", return_value=history), \
         patch("app.config_manager.save_json", return_value=None), \
         patch("app.config_manager._get_file_lock", return_value=MagicMock()):
        t._update_trade_history(
            {"symbol": "INSP", "order_id": "1323",
             "trade_entry_snapshot": {"order_id": "1323", "symbol": "INSP"}},
            "Cancelled", 0.0, 0.0)

    assert history[0]["status"] == "executed", (
        "Gefuellter Trade wurde durch ein spaeteres Cancel-Event entwertet")


def test_tracker_trifft_bei_recycelter_id_nicht_den_fremden_trade():
    """IBKR recycelt Order-IDs pro Session. Ohne Symbol-Qualifier kann die
    Rueckwaertssuche einen fremden Eintrag mit derselben ID treffen und
    ueberschreiben."""
    t = _tracker()
    history = [
        {"action": "TRAILING_SL_CLOSE", "symbol": "INSP", "order_id": "1323",
         "status": "submitted"},
        {"action": "SCANNER_BUY", "symbol": "KSS", "order_id": "1323",
         "status": "executed"},
    ]
    # Rueckwaerts trifft zuerst KSS — ohne Symbol-Pruefung wuerde der
    # Kauf-Eintrag ueberschrieben statt des INSP-Verkaufs.
    with patch("app.config_manager.load_json", return_value=history), \
         patch("app.config_manager.save_json", return_value=None), \
         patch("app.config_manager._get_file_lock", return_value=MagicMock()):
        t._update_trade_history(
            {"symbol": "INSP", "order_id": "1323",
             "trade_entry_snapshot": {"order_id": "1323", "symbol": "INSP"}},
            "Filled", 205.0, 71.08)

    assert history[0]["status"] == "executed", "INSP-Verkauf nicht nachgetragen"
    assert "_e27_last_update" not in history[1], (
        "Fremder Kauf-Eintrag wurde durch ID-Kollision angefasst")
