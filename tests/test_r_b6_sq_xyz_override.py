"""R-B6 (02.06.2026) — SQ (Block, ex-Square) IBKR-Override auf XYZ.

Block Inc. benannte seinen Ticker SQ -> XYZ um (2024). Das yf-Feld war bereits
migriert (Daten/Scanner ok -> STRONG_BUY-Signal), aber der IBKR-Resolver nutzte
den Universe-Key 'SQ' -> Stock('SQ','SMART') -> IBKR Error 200 'No security
definition'. Der Bot wollte SQ jede Scan-Runde kaufen, scheiterte aber IMMER an
der Aufloesung (30 Sentry-Fehler/4h, verpasster Strong-Buy).

Fix: ibkr_override {"symbol":"XYZ",...} in der SQ-Universe-Entry (gleicher
Mechanismus wie GOLD->GLD) -> Resolver baut Stock('XYZ','SMART') -> resolved.
"""
import sys
from unittest.mock import MagicMock


class _FakeContract:
    def __init__(self, secType=None, conId=0, symbol=None, exchange=None, currency=None):
        self.secType = secType
        self.conId = conId
        self.symbol = symbol
        self.exchange = exchange
        self.currency = currency


def _install_fake_ib_insync():
    """ib_insync lokal nicht installiert (nur VPS) — fake einsetzen."""
    fake = MagicMock()
    fake.IB = MagicMock
    fake.Contract = _FakeContract
    fake.Stock = lambda symbol, exchange, currency: _FakeContract(
        secType="STK", symbol=symbol, exchange=exchange, currency=currency)
    fake.Crypto = lambda symbol, exchange, currency: _FakeContract(
        secType="CRYPTO", symbol=symbol, exchange=exchange, currency=currency)
    fake.Forex = lambda pair: _FakeContract(secType="CASH", symbol=pair[:3])
    sys.modules["ib_insync"] = fake


_install_fake_ib_insync()

import app.ibkr_contract_resolver as resolver  # noqa: E402


def test_sq_resolves_to_xyz_ibkr_symbol(monkeypatch):
    """SQ (Universe-Key) -> Resolver baut Stock('XYZ'), NICHT Stock('SQ')."""
    monkeypatch.setattr(resolver, "_load_cache", lambda: {})
    monkeypatch.setattr(resolver, "_save_cache", lambda cache: None)

    captured = {}

    def fake_qualify(c):
        captured["symbol"] = c.symbol
        captured["exchange"] = c.exchange
        c.conId = 777001
        return [c]

    ib = MagicMock()
    ib.qualifyContracts = fake_qualify

    c = resolver.resolve_contract(ib, "SQ")
    assert captured["symbol"] == "XYZ", (
        f"IBKR-Symbol war '{captured.get('symbol')}', erwartet 'XYZ' (Block-Rename)")
    assert c.symbol == "XYZ"


def test_sq_universe_entry_has_xyz_override():
    """Datenseitig: SQ-Entry traegt ibkr_override.symbol == XYZ."""
    from app.market_scanner import ASSET_UNIVERSE
    sq = ASSET_UNIVERSE.get("SQ", {})
    assert sq.get("ibkr_override", {}).get("symbol") == "XYZ"
