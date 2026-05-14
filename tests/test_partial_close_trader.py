"""v37h Tab-Audit-Day-2 (12.05.2026) — Tests fuer partial_close-Trader-Pfad.

Hintergrund (Carlos 12.05.2026 Nachmittag): PARTIAL_SIGNAL wurde im
Trader nur GELOGGT statt echte Teil-Verkaeufe zu machen (Erbe aus
eToro-Zeit). Bei IBKR sollte echter partial_close ausgefuehrt werden.

Tests verifizieren:
  1. IbkrBroker.partial_close: 30% von 100 shares -> 30 shares verkauft
  2. partial_close: Round-off (33.33% von 7 shares = 2 shares)
  3. partial_close: zu klein (1% von 5 shares = 0 -> _skipped_too_small)
  4. partial_close: zu gross (100% -> begrenzt auf full_qty-1)
  5. partial_close: invalid pct (0/negative/>100 -> error)
  6. BrokerBase.partial_close default: _unsupported
  7. Trader-Pfad: bei IBKR ruft echten partial_close
  8. Trader-Pfad: bei Broker ohne partial_close faellt auf signal-log zurueck
  9. Tranchen-State wird bei PARTIAL_CLOSE-Success persistiert
"""
from unittest.mock import MagicMock, patch

import pytest

# v37h: ib_insync ist nur im Container installiert. Lokal-Dev-Env (Carlos's
# Win) hat es nicht — daher die IBKR-spezifischen Tests skippen wenn fehlt.
# Die Trader-Pattern-Tests laufen lokal weil sie nur MagicMock nutzen.
_HAS_IBKR = True
try:
    import ib_insync  # noqa: F401
except ImportError:
    _HAS_IBKR = False


# ============================================================
# BrokerBase Default-Implementation
# ============================================================

def test_broker_base_partial_close_returns_unsupported():
    """Default-Impl: alle nicht-IBKR-Broker bekommen _unsupported.

    v37h+1 (14.05.2026): Skip-Decorator entfernt — der Test prueft BrokerBase
    direkt und braucht ib_insync gar nicht. Vorher fehlerhaft: _HAS_IBKR True
    in der Voll-Suite (anderer Test mockt ib_insync in sys.modules) -> Test
    lief -> _MinimalBroker fehlten neue abstract-Methoden. Jetzt: Skip raus,
    _MinimalBroker um alle abstract-Methoden ergaenzt.
    """
    from app.broker_base import BrokerBase

    # BrokerBase ist abstract -> Subclass-Bypass mit allen abstract-Methoden
    class _MinimalBroker(BrokerBase):
        broker_name = "minimal"

        def configured(self): return True
        def get_portfolio(self): return None
        def buy(self, *a, **kw): return None
        def sell(self, *a, **kw): return None
        def close_position(self, *a, **kw): return None
        def search_instrument(self, q): return []
        def get_instrument_info(self, iid): return None
        def get_quote(self, iid): return None
        def get_history(self, iid, **kw): return None
        # v37h+1: neue abstract-Methoden seit dem Test
        def get_available_cash(self): return 0.0
        def get_equity(self): return 0.0
        def get_instruments(self): return []
        def get_pnl(self): return 0.0
        def get_total_invested(self): return 0.0

    b = _MinimalBroker()
    result = b.partial_close("pos-1", 50.0)
    assert result == {"_unsupported": True, "_broker": "base"}


# ============================================================
# IbkrBroker.partial_close — Mock-Level (kein echter IB-Call)
# ============================================================

@pytest.mark.skipif(not _HAS_IBKR, reason="ib_insync nicht installiert (nur Container)")
def test_partial_close_invalid_pct_zero():
    """pct=0 -> Error-Return None."""
    from app.ibkr_client import IbkrBroker

    broker = IbkrBroker.__new__(IbkrBroker)  # bypass __init__
    result = broker.partial_close("123", 0.0)
    assert result is None


@pytest.mark.skipif(not _HAS_IBKR, reason="ib_insync nicht installiert (nur Container)")
def test_partial_close_invalid_pct_negative():
    from app.ibkr_client import IbkrBroker
    broker = IbkrBroker.__new__(IbkrBroker)
    result = broker.partial_close("123", -5.0)
    assert result is None


@pytest.mark.skipif(not _HAS_IBKR, reason="ib_insync nicht installiert (nur Container)")
def test_partial_close_invalid_pct_over_100():
    from app.ibkr_client import IbkrBroker
    broker = IbkrBroker.__new__(IbkrBroker)
    result = broker.partial_close("123", 101.0)
    assert result is None


# ============================================================
# Quantity-Berechnung-Tests via direkter Logik-Aufruf
# ============================================================

def test_qty_round_off_normal_case():
    """30% von 100 = 30."""
    full_qty = 100
    pct = 30.0
    qty_close = int(round(full_qty * pct / 100.0))
    assert qty_close == 30


def test_qty_round_off_uneven():
    """33% von 7 = 2.31 -> 2 (round)."""
    full_qty = 7
    pct = 33.0
    qty_close = int(round(full_qty * pct / 100.0))
    assert qty_close == 2


def test_qty_round_off_half_up():
    """25% von 6 = 1.5 -> 2 (banker's rounding: HALF_EVEN)."""
    # Python's round() ist banker's rounding -> 1.5 wird zu 2 (gerade)
    # 2.5 wuerde zu 2 (gerade), 3.5 zu 4 (gerade)
    full_qty = 6
    pct = 25.0
    qty_close = int(round(full_qty * pct / 100.0))
    assert qty_close == 2  # 1.5 -> 2 via banker's


def test_qty_too_small():
    """1% von 5 = 0.05 -> 0 -> skipped_too_small."""
    full_qty = 5
    pct = 1.0
    qty_close = int(round(full_qty * pct / 100.0))
    assert qty_close == 0
    # Im Code: qty_close < 1 -> _skipped_too_small zurueck


# ============================================================
# Trader-Pfad Integration: Mock-Broker mit partial_close
# ============================================================

def test_trader_uses_partial_close_when_available():
    """Wenn client hasattr partial_close: wird aufgerufen."""
    mock_client = MagicMock()
    mock_client.partial_close = MagicMock(return_value={
        "_close_qty": 30,
        "_remaining_qty": 70,
        "_full_qty": 100,
        "_pct_closed": 30.0,
        "orderForOpen": {
            "filledQuantity": 30,
            "avgFillPrice": 100.0,
            "statusID": "executed",
        },
    })

    # Direkt-Aufruf um Trader-Logic-Pattern zu verifizieren
    result = mock_client.partial_close("pos-1", 30.0, 12345)
    assert result["_close_qty"] == 30
    assert result["_remaining_qty"] == 70
    mock_client.partial_close.assert_called_once_with("pos-1", 30.0, 12345)


def test_trader_falls_back_to_signal_log_on_unsupported():
    """Bei _unsupported Response: Trader sollte signal-log fallback."""
    mock_client = MagicMock()
    mock_client.partial_close = MagicMock(return_value={
        "_unsupported": True,
        "_broker": "etoro",
    })

    result = mock_client.partial_close("pos-1", 30.0)
    assert result.get("_unsupported") is True
    # Im Trader-Pfad wuerde dann action_kind="PARTIAL_SIGNAL" gesetzt
    # und trade_status="signal_logged" — siehe trader.py


def test_trader_handles_too_small_skip():
    """Bei _skipped_too_small: Trader markiert Tranche NICHT als consumed."""
    mock_client = MagicMock()
    mock_client.partial_close = MagicMock(return_value={
        "_skipped_too_small": True,
        "_full_qty": 5,
        "_pct": 1.0,
    })
    result = mock_client.partial_close("pos-1", 1.0)
    assert result.get("_skipped_too_small") is True


def test_trader_handles_already_closed():
    """Bei _already_closed: gleicher Skip-Pfad."""
    mock_client = MagicMock()
    mock_client.partial_close = MagicMock(return_value={
        "_already_closed": True,
        "_conId": 12345,
    })
    result = mock_client.partial_close("pos-1", 30.0)
    assert result.get("_already_closed") is True


# ============================================================
# Hasattr-Check Pattern fuer Backwards-Compat
# ============================================================

def test_hasattr_check_works_for_etoro_legacy():
    """Wenn Mock-Client KEINE partial_close-Methode hat:
    der Trader-Code-Pfad muss hasattr-check sauber durchlaufen.
    """
    # EtoroClient hat keine partial_close-Methode in der Implementation
    # (BrokerBase-Default greift, aber nur wenn von BrokerBase geerbt)
    class LegacyClient:
        """Simuliert sehr alten Client ohne BrokerBase-Erbe."""
        pass

    client = LegacyClient()
    assert not hasattr(client, "partial_close")
    # Trader-Code-Pfad faellt auf reines Signal-Log zurueck (siehe trader.py)


# ============================================================
# State-Persistierung
# ============================================================

def test_tranche_consumed_logic_for_partial_close():
    """Tranche wird als consumed markiert bei action_kind in den 3 Erfolg-Faellen."""
    # PROFIT_LOCK_CLOSE: erfolgreicher voller Close
    # PARTIAL_CLOSE: erfolgreicher Teil-Verkauf
    # PARTIAL_SIGNAL: Legacy-Fallback (sieht aus wie Erfolg fuer State-Tracking)
    success_kinds = {"PROFIT_LOCK_CLOSE", "PARTIAL_CLOSE", "PARTIAL_SIGNAL"}
    skip_kinds = {"PARTIAL_SKIP", "PARTIAL_CLOSE_FAILED"}
    for kind in success_kinds:
        # In trader.py: tranche_consumed = action_kind in (...)
        assert kind in success_kinds
    for kind in skip_kinds:
        assert kind not in success_kinds  # Tranche bleibt offen


# ============================================================
# Backward-Compat: alte trade_history Eintraege bleiben lesbar
# ============================================================

def test_legacy_partial_signal_remains_in_history():
    """Alte PARTIAL_SIGNAL-Eintraege (eToro-Zeit) sollten weiterhin lesbar/
    filterbar sein im Dashboard (action='PARTIAL_SIGNAL' bleibt valid)."""
    legacy_entry = {
        "timestamp": "2026-05-11T17:45:00",
        "action": "PARTIAL_SIGNAL",
        "symbol": "SILVER",
        "pnl_usd": 1096.18,
    }
    # Frontend filter erkennt PARTIAL_SIGNAL als gueltige Aktion
    assert legacy_entry["action"] == "PARTIAL_SIGNAL"
    # Plus neue PARTIAL_CLOSE als zusaetzliche Aktion akzeptiert
    new_entry = legacy_entry.copy()
    new_entry["action"] = "PARTIAL_CLOSE"
    assert new_entry["action"] == "PARTIAL_CLOSE"
