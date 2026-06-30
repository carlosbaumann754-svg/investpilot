"""v37dz: yfinance-Sizing-Entkopplung — IBKR-OHLCV-Fallback in analyze_single_asset.

Kernzusagen:
- yfinance-Success-Path bleibt unveraendert + ruft den Fallback NICHT (soak-neutral).
- Faellt yfinance aus, liefern IBKR-OHLCV-Bars trotzdem eine valide Analyse.
- Fallback ist budget-gedeckelt (Pacing-Schutz).
"""
from types import SimpleNamespace
from unittest.mock import MagicMock
import app.market_scanner as ms


def _synth_bars(n=60, start=100.0):
    bars, p = [], start
    for i in range(n):
        p = p * (1.0 + (0.002 if i % 3 else -0.001))
        bars.append({"open": p * 0.99, "high": p * 1.01, "low": p * 0.98,
                     "close": p, "volume": 1000 + i * 10})
    return bars


def test_ta_from_ohlcv_returns_valid_dict():
    bars = _synth_bars(60)
    out = ms._ta_from_ohlcv(
        "TST", {"name": "TST", "class": "stocks", "internal_id": 900999},
        [b["close"] for b in bars], [b["volume"] for b in bars],
        [b["high"] for b in bars], [b["low"] for b in bars])
    assert out["symbol"] == "TST"
    assert out["price"] == round(bars[-1]["close"], 4)
    for k in ("rsi", "macd", "volatility", "atr_pct", "adx", "momentum_5d", "obv_slope"):
        assert k in out


def test_analyze_uses_ibkr_fallback_when_yfinance_off(monkeypatch):
    monkeypatch.setattr(ms, "yf", None)  # yfinance "aus"
    monkeypatch.setattr(ms, "_sizing_fallback_bars",
                        lambda sym, lookback_days=95: _synth_bars(60))
    out = ms.analyze_single_asset(
        "TST", {"name": "TST", "class": "stocks", "yf": "TST", "internal_id": 900999})
    assert out is not None and out["symbol"] == "TST"


def test_analyze_none_when_both_sources_fail(monkeypatch):
    monkeypatch.setattr(ms, "yf", None)
    monkeypatch.setattr(ms, "_sizing_fallback_bars", lambda sym, lookback_days=95: [])
    assert ms.analyze_single_asset("TST", {"name": "TST", "class": "stocks", "yf": "TST"}) is None


def test_yfinance_success_path_does_not_call_fallback(monkeypatch):
    # Beweist Soak-Neutralitaet: yfinance OK -> Fallback ungenutzt
    import pandas as pd
    bars = _synth_bars(60)
    df = pd.DataFrame({
        "Close": [b["close"] for b in bars], "Volume": [b["volume"] for b in bars],
        "High": [b["high"] for b in bars], "Low": [b["low"] for b in bars],
    })
    fake_ticker = MagicMock()
    fake_ticker.history.return_value = df
    fake_ticker.history_metadata = {"instrumentType": "EQUITY"}
    fake_yf = MagicMock(); fake_yf.Ticker.return_value = fake_ticker
    monkeypatch.setattr(ms, "yf", fake_yf)
    called = {"fb": False}

    def _fb(sym, lookback_days=95):
        called["fb"] = True
        return []
    monkeypatch.setattr(ms, "_sizing_fallback_bars", _fb)
    out = ms.analyze_single_asset(
        "TST", {"name": "TST", "class": "stocks", "yf": "TST", "internal_id": 900999})
    assert out is not None and out["symbol"] == "TST"
    assert called["fb"] is False  # yfinance OK -> kein Fallback (soak-neutral)


def test_sizing_fallback_budget_gate():
    ms._SIZING_FALLBACK["budget"] = 0
    assert ms._sizing_fallback_bars("TST") == []  # Budget erschoepft -> kein Connect-Versuch
    ms.reset_sizing_fallback_budget(7)
    assert ms._SIZING_FALLBACK["budget"] == 7


def test_analyze_skips_non_us_ticker(monkeypatch):
    # v37e: rein-numerische/fremde Codes (China-A-Shares) -> None, ohne yfinance/IBKR
    fake_yf = MagicMock()
    fake_yf.Ticker.side_effect = AssertionError("yf.Ticker darf bei Junk-Symbol nicht laufen")
    monkeypatch.setattr(ms, "yf", fake_yf)

    def _fb_must_not_run(*a, **k):
        raise AssertionError("IBKR-Fallback darf bei Junk-Symbol nicht laufen")
    monkeypatch.setattr(ms, "_sizing_fallback_bars", _fb_must_not_run)
    for junk in ("688008", "3037", "300476", "002371", "12AB"):
        assert ms.analyze_single_asset(junk, {"name": junk, "class": "stocks", "yf": junk}) is None


def test_analyze_accepts_valid_us_ticker_format(monkeypatch):
    # Gueltige US-Ticker (auch mit . und -) passieren den Guard
    monkeypatch.setattr(ms, "yf", None)
    monkeypatch.setattr(ms, "_sizing_fallback_bars", lambda sym, lookback_days=95: _synth_bars(60))
    for ok in ("AAPL", "BRK.B", "BF-B"):
        out = ms.analyze_single_asset(ok, {"name": ok, "class": "stocks", "yf": ok, "internal_id": 900999})
        assert out is not None and out["symbol"] == ok


def test_get_recent_daily_bars_parses_ohlcv(monkeypatch):
    import sys
    # ib_insync ist lokal nicht installiert -> Fake-Modul, damit `from ib_insync
    # import Stock` im Funktionskoerper nicht vor unseren Mocks scheitert.
    monkeypatch.setitem(sys.modules, "ib_insync", MagicMock())
    from app.ibkr_client import IbkrBroker
    b = IbkrBroker({"ibkr": {"client_id": 1}})
    fake_bar = SimpleNamespace(open=10.0, high=11.0, low=9.5, close=10.5, volume=1234)
    fake_ib = MagicMock()
    fake_ib.qualifyContracts.return_value = [MagicMock()]
    fake_ib.reqHistoricalData.return_value = [fake_bar, fake_bar]
    b._get_ib = MagicMock(return_value=fake_ib)
    bars = b.get_recent_daily_bars("TST", 30)
    assert len(bars) == 2
    assert bars[0]["close"] == 10.5 and bars[0]["high"] == 11.0 and bars[0]["volume"] == 1234
