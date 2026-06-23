"""v37dy: Fail-safe gegen Positions-Timeout in get_portfolio (PYTHON-FASTAPI-13).

ib_insync loggt bei Timeout "positions request timed out" und liefert dann eine
LEERE positions()/portfolio()-Liste OHNE Exception. get_portfolio muss das
erkennen (leere Positionen ABER Konto haelt Bestaende laut GrossPositionValue)
und None liefern, damit die "if not portfolio"-Guards der Aufrufer greifen und
der Trading-Zyklus uebersprungen wird (kein Blind-Doppelkauf).
"""
from types import SimpleNamespace
from unittest.mock import MagicMock


def _make_broker():
    from app.ibkr_client import IbkrBroker
    return IbkrBroker({"ibkr": {"client_id": 1}})


def _wire(broker, portfolio_items, positions_items, account_values):
    fake_ib = MagicMock()
    fake_ib.isConnected.return_value = True
    fake_ib.portfolio.return_value = portfolio_items
    fake_ib.positions.return_value = positions_items
    broker._get_ib = MagicMock(return_value=fake_ib)
    broker._get_account_value = MagicMock(side_effect=lambda k: account_values.get(k))
    broker._get_account_value_base = MagicMock(side_effect=lambda k: account_values.get(k))
    broker._get_base_currency = MagicMock(return_value="USD")
    return broker


def _item(con_id, qty, avg=10.0, mkt=11.0, unreal=5.0, sym="AAA"):
    c = SimpleNamespace(conId=con_id, symbol=sym)
    return SimpleNamespace(contract=c, position=qty, averageCost=avg, avgCost=avg,
                           marketPrice=mkt, unrealizedPNL=unreal)


def test_failsafe_empty_positions_but_account_holds_returns_none():
    # Positions-Timeout: leere Listen, ABER GrossPositionValue=600k -> Bestaende da
    broker = _wire(_make_broker(), [], [], {
        "NetLiquidation": 1_000_000, "TotalCashValue": 400_000,
        "AvailableFunds": 400_000, "UnrealizedPnL": 0, "RealizedPnL": 0,
        "GrossPositionValue": 600_000,
    })
    assert broker.get_portfolio() is None  # fail-safe -> Guards greifen, Zyklus skip


def test_truly_flat_account_returns_dict_not_none():
    # Echt-flach: keine Positionen UND GrossPositionValue~0 -> KEIN False-Positive
    broker = _wire(_make_broker(), [], [], {
        "NetLiquidation": 1_000_000, "TotalCashValue": 1_000_000,
        "AvailableFunds": 1_000_000, "UnrealizedPnL": 0, "RealizedPnL": 0,
        "GrossPositionValue": 0,
    })
    pf = broker.get_portfolio()
    assert isinstance(pf, dict)
    assert pf["positions"] == []


def test_normal_positions_returns_dict():
    # Happy-Path nicht gebrochen: 1 echte Position kommt sauber durch
    item = _item(123, 10)
    broker = _wire(_make_broker(), [item], [item], {
        "NetLiquidation": 1_000_000, "TotalCashValue": 400_000,
        "AvailableFunds": 400_000, "UnrealizedPnL": 50, "RealizedPnL": 0,
        "GrossPositionValue": 600_000,
    })
    pf = broker.get_portfolio()
    assert isinstance(pf, dict)
    assert len(pf["positions"]) == 1
    assert pf["positions"][0]["symbol"] == "AAA"
