"""R-B55c (14.08.2026): Per-Position-Isolation im SL/TP-Exit-Loop.

Audit-Strukturhinweis: Vorher riss ein deterministischer Fehler bei Position k
die Exit-Pruefung ALLER Folgepositionen jeden Zyklus mit — dieselbe
Fehlerklasse wie der E6-Vorfall (still, dauerhaft, kein Test merkt es).
Der Regressionstest hier stellt genau das nach: Position 1 crasht hart,
Position 2 MUSS trotzdem geprueft werden.
"""
from unittest import mock

from app import trader


def _fake_portfolio_mit_zwei_positionen():
    return {
        "positions": [
            {"symbol": "CRASH", "instrument_id": 1, "position_id": "p1"},
            {"symbol": "OKAY", "instrument_id": 2, "position_id": "p2"},
        ],
        "credit": 100000,
    }


def test_position_1_crasht_position_2_laeuft_trotzdem():
    client = mock.Mock()
    client.get_portfolio.return_value = _fake_portfolio_mit_zwei_positionen()

    calls = []

    def parse_und_crash_bei_erster(pos):
        calls.append(pos.get("symbol"))
        if pos.get("symbol") == "CRASH":
            raise ValueError("deterministischer Test-Crash in Position 1")
        # Fuer OKAY reicht ein Minimal-Dict; der Body bricht danach frueh ab
        # (kein pnl/kein Preis) — entscheidend ist, dass er ERREICHT wird.
        return {"position_id": "p2", "instrument_id": 2, "symbol": "OKAY",
                "pnl_pct": 0.0, "pnl": 0.0, "invested": 0.0, "amount": 0.0,
                "leverage": 1, "is_buy": True}

    trader._EXIT_LOOP_FAILS["streak"] = 0
    with mock.patch.object(trader.EtoroClient, "parse_position",
                           side_effect=parse_und_crash_bei_erster):
        with mock.patch.object(trader, "send_alert", create=True):
            # Darf NICHT raisen — und muss beide Positionen anfassen.
            trader.check_stop_loss_take_profit(client, {"demo_trading": {}})

    assert calls == ["CRASH", "OKAY"], (
        f"Position 2 wurde nach Crash von Position 1 nicht mehr geprueft: {calls}")
    assert trader._EXIT_LOOP_FAILS["streak"] >= 1


def test_ohne_crash_kein_streak():
    client = mock.Mock()
    client.get_portfolio.return_value = {"positions": [], "credit": 0}
    trader._EXIT_LOOP_FAILS["streak"] = 0
    trader.check_stop_loss_take_profit(client, {"demo_trading": {}})
    assert trader._EXIT_LOOP_FAILS["streak"] == 0
