"""Tests fuer app/fundamental_signals.py — die 5 Signal-Berechnungen.

Prueft Korrektheit + Vorzeichen + None-Handling jedes Signals und das
Zusammenspiel in compute_signals.
"""
from app import fundamental_signals as fs


def _pt(s, e, f, v):
    return {"s": s, "e": e, "f": f, "v": v}


# --- value (Book-to-Market) -----------------------------------------------
def test_value_signal():
    rec = {"StockholdersEquity": [_pt(None, "2023-12-31", "2024-02-15", 1000)]}
    assert fs.value_signal(rec, "2024-06-30", 2000) == 0.5  # 1000/2000


def test_value_signal_no_marketcap():
    rec = {"StockholdersEquity": [_pt(None, "2023-12-31", "2024-02-15", 1000)]}
    assert fs.value_signal(rec, "2024-06-30", 0) is None
    assert fs.value_signal(rec, "2024-06-30", None) is None


def test_value_signal_no_equity():
    assert fs.value_signal({}, "2024-06-30", 2000) is None


# --- quality (Gross-Profitability) ----------------------------------------
def test_quality_signal():
    rec = {
        "GrossProfit": [_pt("2023-01-01", "2023-12-31", "2024-02-15", 400)],
        "Assets": [_pt(None, "2023-12-31", "2024-02-15", 1000)],
    }
    assert fs.quality_signal(rec, "2024-06-30") == 0.4


def test_quality_signal_missing():
    assert fs.quality_signal({}, "2024-06-30") is None


# --- reversal (Short-Term, Vorzeichen ist entscheidend) -------------------
def test_reversal_recent_winner_negative():
    # +10% in den letzten ~21T -> juengster Gewinner -> niedriger Score (negativ)
    assert abs(fs.reversal_signal(110, 100) - (-0.10)) < 1e-9


def test_reversal_recent_loser_positive():
    # -10% -> juengster Verlierer -> hoher Score (positiv, = bounce-Kandidat)
    assert abs(fs.reversal_signal(90, 100) - 0.10) < 1e-9


def test_reversal_invalid_ref():
    assert fs.reversal_signal(100, 0) is None
    assert fs.reversal_signal(100, None) is None


# --- leverage (Eigenkapitalquote) -----------------------------------------
def test_leverage_signal():
    rec = {
        "StockholdersEquity": [_pt(None, "2023-12-31", "2024-02-15", 600)],
        "Assets": [_pt(None, "2023-12-31", "2024-02-15", 1000)],
    }
    assert fs.leverage_signal(rec, "2024-06-30") == 0.6


# --- earngrowth (YoY, geclippt, prev>0) -----------------------------------
def test_earngrowth_signal():
    rec = {"NetIncomeLoss": [
        _pt("2021-01-01", "2021-12-31", "2022-02-15", 100),
        _pt("2022-01-01", "2022-12-31", "2023-02-15", 120),
    ]}
    assert abs(fs.earngrowth_signal(rec, "2023-06-30") - 0.2) < 1e-9


def test_earngrowth_clipped():
    rec = {"NetIncomeLoss": [
        _pt("2021-01-01", "2021-12-31", "2022-02-15", 10),
        _pt("2022-01-01", "2022-12-31", "2023-02-15", 1000),  # +9900% -> clip 2.0
    ]}
    assert fs.earngrowth_signal(rec, "2023-06-30") == 2.0


def test_earngrowth_negative_prior_is_none():
    rec = {"NetIncomeLoss": [
        _pt("2021-01-01", "2021-12-31", "2022-02-15", -50),  # Verlust-Vorjahr
        _pt("2022-01-01", "2022-12-31", "2023-02-15", 120),
    ]}
    assert fs.earngrowth_signal(rec, "2023-06-30") is None


# --- compute_signals (Zusammenspiel) --------------------------------------
def test_compute_signals_all_present():
    rec = {
        "StockholdersEquity": [_pt(None, "2023-12-31", "2024-02-15", 600)],
        "Assets": [_pt(None, "2023-12-31", "2024-02-15", 1000)],
        "GrossProfit": [_pt("2023-01-01", "2023-12-31", "2024-02-15", 400)],
        "NetIncomeLoss": [
            _pt("2022-01-01", "2022-12-31", "2023-02-15", 100),
            _pt("2023-01-01", "2023-12-31", "2024-02-15", 120),
        ],
        "CommonStockSharesOutstanding": [_pt(None, "2023-12-31", "2024-02-15", 100)],
    }
    out = fs.compute_signals(rec, "2024-06-30", price_now=20, price_ref=22)
    # market_cap = 100 * 20 = 2000
    assert abs(out["value"] - 0.3) < 1e-9        # 600/2000
    assert abs(out["quality"] - 0.4) < 1e-9      # 400/1000
    assert out["lev"] == 0.6                      # 600/1000
    assert abs(out["earngrowth"] - 0.2) < 1e-9   # (120-100)/100
    assert out["reversal"] > 0                    # 20<22 -> Verlierer -> positiv


def test_compute_signals_missing_fundamentals():
    out = fs.compute_signals({}, "2024-06-30", price_now=20, price_ref=22)
    assert out["value"] is None
    assert out["quality"] is None
    assert out["lev"] is None
    assert out["earngrowth"] is None
    # reversal braucht nur Preise -> trotzdem berechenbar
    assert out["reversal"] is not None
