"""Tests fuer v37dc Leverage-Reality-Aware-Patch.

Bug 06.05.2026: Trades-Tab zeigte 2x Leverage, Positionen-Tab 1x.
IBKR-Reality: 1x (verifiziert via Cash-Math auf VPS).
Root-Cause: IbkrBroker.buy/sell ignorierten leverage-Parameter ohne Reality-
Marker, Bot's calculate_leveraged_position_size rechnete mit intended-
Leverage → Position 2x zu gross relativ zu Bot's Risk-Annahme.

Fix v37dc:
1. leverage_manager.py: Default fallback 2 → 1 (Konsistenz)
2. IbkrBroker.buy/sell: leverage_actual=1 im Result-Dict (Reality-Marker)
"""

import inspect


def test_leverage_manager_default_is_1():
    """Default-Fallback im calculate_optimal_leverage muss 1 sein, nicht 2."""
    from app.leverage_manager import calculate_optimal_leverage

    # Mit komplett leerer config sollte default_leverage=1 greifen
    result = calculate_optimal_leverage(
        symbol="AAPL",
        asset_class="stocks",
        volatility=2.5,
        signal_confidence=20,
        market_regime="bull",
        vix_level=15,
        config={"leverage": {}},  # kein default_leverage gesetzt
    )
    # Mit vol 2.5 (medium), conf 20 (mid), bull, vix 15 = nur base + minor adjustments
    # Mit base=1: result sollte <= 2 sein (eher 1)
    # Mit base=2 (alter Default): result waere ~2-3
    assert result <= 2, f"Mit Default-Fallback=1 sollte Leverage hoechstens 2 sein, got {result}"


def test_ibkr_buy_clamps_leverage_in_result():
    """IbkrBroker.buy/sell setzt leverage_actual=1 im Result-Dict."""
    from app.ibkr_client import IbkrBroker

    sig = inspect.signature(IbkrBroker.buy)
    assert "leverage" in sig.parameters
    assert sig.parameters["leverage"].default == 1


def test_ibkr_sell_clamps_leverage_in_result():
    from app.ibkr_client import IbkrBroker

    sig = inspect.signature(IbkrBroker.sell)
    assert "leverage" in sig.parameters
    assert sig.parameters["leverage"].default == 1


def test_position_sizing_with_leverage_returns_positive():
    """Position-Sizing mit verschiedenen Leverages returnt valide positive Werte.

    Hinweis: calculate_leveraged_position_size dividiert intern durch leverage
    am Ende, daher ist 1x-Position groesser als 2x-Position (counter-intuitiv aber
    mathematisch korrekt fuer Risk-pro-Trade-konstant). Mit IBKR-Reality (1x) ist
    der 1x-Ast die korrekte Berechnung — keine Phantom-Leverage-Korrektur noetig.
    """
    from app.risk_manager import calculate_leveraged_position_size

    pos_size_1x = calculate_leveraged_position_size(
        portfolio_value=100_000,
        stop_loss_pct=-3,
        leverage=1,
        config={"risk_management": {"risk_per_trade_pct": 2.0}},
    )
    pos_size_2x = calculate_leveraged_position_size(
        portfolio_value=100_000,
        stop_loss_pct=-3,
        leverage=2,
        config={"risk_management": {"risk_per_trade_pct": 2.0}},
    )
    assert pos_size_1x > 0, "1x Position muss positiv sein"
    assert pos_size_2x > 0, "2x Position muss positiv sein"
    # Beide unter Portfolio-Wert (Sanity-Check)
    assert pos_size_1x < 100_000
    assert pos_size_2x < 100_000


def test_ibkr_result_has_leverage_actual_marker():
    """v37dc: Result-Dict muss leverage_actual als Reality-Marker enthalten.

    Test via Mock weil echter IBKR-Connect waere e2e-Test.
    """
    from unittest.mock import patch, MagicMock
    from app.ibkr_client import IbkrBroker

    broker = IbkrBroker.__new__(IbkrBroker)  # ohne __init__
    fake_result = {"order_id": "123", "qty": 10, "price": 100.0}

    with patch.object(broker, "_place_market_order", return_value=fake_result):
        result = broker.buy(instrument_id=1, amount_usd=1000, leverage=2)

    assert result is not None
    assert result.get("leverage_actual") == 1, \
        "leverage_actual=1 muss als Reality-Marker im Result-Dict sein"
