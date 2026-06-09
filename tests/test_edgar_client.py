"""Tests fuer app/edgar_client.py — Point-in-Time-Accessoren (kein Look-Ahead).

Kern-Garantie: ein Wert wird NUR sichtbar, wenn sein ``filed``-Datum <= asof ist.
Das verhindert Look-Ahead-Bias (der teuerste Fehler bei Fundamental-Backtests).
"""
from app import edgar_client as ec


def _pt(s, e, f, v):
    return {"s": s, "e": e, "f": f, "v": v}


# --- latest_stock (Bilanz-Bestandswerte) ----------------------------------
def test_latest_stock_respects_filed_asof():
    pts = [
        _pt(None, "2022-12-31", "2023-02-15", 100),
        _pt(None, "2023-12-31", "2024-02-15", 120),  # filed > asof -> unsichtbar
    ]
    assert ec.latest_stock(pts, "2023-06-30") == 100
    assert ec.latest_stock(pts, "2024-03-01") == 120


def test_latest_stock_empty():
    assert ec.latest_stock([], "2024-01-01") is None


def test_latest_stock_picks_latest_end():
    pts = [
        _pt(None, "2023-03-31", "2023-05-01", 90),
        _pt(None, "2023-06-30", "2023-08-01", 110),
    ]
    assert ec.latest_stock(pts, "2023-12-31") == 110


# --- annual_latest (Jahres-Flusswerte) ------------------------------------
def test_annual_latest_only_annual_duration():
    pts = [
        _pt("2023-01-01", "2023-03-31", "2023-05-01", 25),   # ~90T Quartal -> raus
        _pt("2022-01-01", "2022-12-31", "2023-02-15", 90),   # ~365T Jahr -> gilt
        _pt("2023-01-01", "2023-12-31", "2024-02-15", 110),  # Jahr, filed spaeter
    ]
    assert ec.annual_latest(pts, "2023-06-30") == 90
    assert ec.annual_latest(pts, "2024-03-01") == 110


def test_annual_latest_no_lookahead():
    pts = [_pt("2023-01-01", "2023-12-31", "2024-02-15", 110)]
    assert ec.annual_latest(pts, "2024-01-01") is None  # asof vor filed


def test_annual_latest_none_when_empty():
    assert ec.annual_latest([], "2024-01-01") is None


# --- annual_two (YoY fuer EarnGrowth) -------------------------------------
def test_annual_two_yoy():
    pts = [
        _pt("2021-01-01", "2021-12-31", "2022-02-15", 80),
        _pt("2022-01-01", "2022-12-31", "2023-02-15", 100),
    ]
    now, prev = ec.annual_two(pts, "2023-06-30")
    assert now == 100 and prev == 80


def test_annual_two_missing_prior():
    pts = [_pt("2022-01-01", "2022-12-31", "2023-02-15", 100)]
    now, prev = ec.annual_two(pts, "2023-06-30")
    assert now == 100 and prev is None


def test_annual_two_empty():
    assert ec.annual_two([], "2024-01-01") == (None, None)


# --- shares_outstanding (Prioritaet + Positivitaet) -----------------------
def test_shares_outstanding_priority():
    rec = {
        "CommonStockSharesOutstanding": [_pt(None, "2023-12-31", "2024-02-15", 5_000_000)],
        "SharesDEI": [_pt(None, "2023-12-31", "2024-02-20", 9_999)],
    }
    assert ec.shares_outstanding(rec, "2024-03-01") == 5_000_000


def test_shares_outstanding_fallback_to_dei():
    rec = {"SharesDEI": [_pt(None, "2023-12-31", "2024-02-20", 7_000_000)]}
    assert ec.shares_outstanding(rec, "2024-03-01") == 7_000_000


def test_shares_outstanding_none_when_missing():
    assert ec.shares_outstanding({}, "2024-03-01") is None


# --- _compact (XBRL-Reduktion) --------------------------------------------
def test_compact_filters_incomplete_points():
    units = [
        {"start": "2023-01-01", "end": "2023-12-31", "filed": "2024-02-15", "val": 100},
        {"start": "2023-01-01", "end": None, "filed": "2024-02-15", "val": 50},   # kein end
        {"start": "2023-01-01", "end": "2023-12-31", "filed": "2024-02-15", "val": None},  # kein val
        {"start": "2023-01-01", "end": "2023-12-31", "filed": None, "val": 70},   # kein filed
    ]
    out = ec._compact(units)
    assert len(out) == 1
    assert out[0] == {"s": "2023-01-01", "e": "2023-12-31", "f": "2024-02-15", "v": 100}
