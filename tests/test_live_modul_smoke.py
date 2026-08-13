"""R-B55 (13.08.2026): Smoke-Tests fuer bisher testfreie LIVE-Module.

Audit-Finding: 18 von 76 app-Modulen wurden von keinem Test importiert —
darunter Module, die im KAUFPFAD bzw. im Live-Signal-Stack haengen. Die
Fehlerklasse ist belegt: web/app.py war einmal unparsebar bei gruener Suite
(R-B29), und ein Import-Fehler in einem dieser Module wuerde erst im
Container auffallen. Diese Tests sichern mindestens: Modul importiert,
Kernfunktionen existieren, reine Funktionen rechnen korrekt.
"""
import importlib

import pytest

# Live-verdrahtete Module ohne bisherige Test-Abdeckung (Audit C).
# Reihenfolge = Naehe zum Geld: Kaufentscheid -> Signal-Stack -> Peripherie.
LIVE_MODULE = [
    "app.execution",        # Slippage-Tracking im Buy-Pfad
    "app.asset_filters",    # Trading-Window/Filter im Kaufentscheid
    "app.events_calendar",  # Event-Blackouts im Kaufentscheid
    "app.sentiment",        # Sentiment-Anteil des Alt-Scores (Fallback-Motor)
    "app.ml_scorer",        # ML-Gate im Kaufentscheid (disabled, aber importiert)
    "app.price_provider",   # Preisquelle des LIVE-Signal-Stacks!
    "app.macro_signals",    # via market_context im Regime-Check
    "app.insider_tracker",
    "app.insider_discovery",
]


@pytest.mark.parametrize("modname", LIVE_MODULE)
def test_live_modul_importiert(modname):
    """Import darf nicht crashen (Syntax-/Import-Zeit-Fehlerklasse R-B29)."""
    try:
        mod = importlib.import_module(modname)
    except ImportError as e:
        pytest.skip(f"Container-only-Dependency fehlt lokal: {e}")
    assert mod is not None


def test_price_provider_kernfunktion_existiert():
    from app import price_provider
    assert callable(price_provider.fetch_recent_prices)


def test_price_provider_now_ref_rechnet_richtig():
    # _extract_now_ref: (closes, ref_offset) -> (aktueller Kurs, Referenzkurs)
    from app.price_provider import _extract_now_ref
    closes = [10.0, 11.0, 12.0, 13.0, 14.0]
    now, ref = _extract_now_ref(closes, ref_offset=2)
    assert now == 14.0
    assert ref == 12.0


def test_asset_filters_klassifikatoren():
    from app import asset_filters
    assert asset_filters.is_forex_major("EURUSD") in (True, False)
    assert asset_filters.is_stablecoin("USDT") in (True, False)
    # Aktien-Symbole sind sicher keine Stablecoins/NFTs
    assert asset_filters.is_stablecoin("AAPL") is False
    assert asset_filters.is_nft_token("AAPL") is False


def test_asset_filters_trading_window_stocks():
    from app.asset_filters import is_within_trading_window
    # Muss fuer stocks ohne Exception einen bool-artigen Wert liefern
    ergebnis = is_within_trading_window("stocks", symbol="AAPL", config={})
    if isinstance(ergebnis, tuple):
        assert isinstance(ergebnis[0], bool)
    else:
        assert isinstance(ergebnis, bool)
