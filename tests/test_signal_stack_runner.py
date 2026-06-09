"""Tests fuer app/signal_stack_runner.py — Preis-Extraktion + Shadow-Orchestrierung."""
from app import signal_stack_runner as r


def _pt(s, e, f, v):
    return {"s": s, "e": e, "f": f, "v": v}


# --- _extract_now_ref ------------------------------------------------------
def test_extract_now_ref():
    closes = list(range(100))  # 0..99
    pn, pr = r._extract_now_ref(closes, ref_offset=21)
    assert pn == 99
    assert pr == 78  # 99 - 21


def test_extract_now_ref_too_short():
    assert r._extract_now_ref([1, 2, 3], 21) == (None, None)
    assert r._extract_now_ref([], 21) == (None, None)


def test_extract_now_ref_exact_boundary():
    closes = list(range(22))  # genau 22 Werte -> ref_offset 21 ok
    pn, pr = r._extract_now_ref(closes, ref_offset=21)
    assert pn == 21 and pr == 0


# --- run_shadow_scan (injizierte facts+prices -> kein Netzwerk) -------------
def test_run_shadow_scan_injected():
    facts = {
        "AAA": {
            "StockholdersEquity": [_pt(None, "2023-12-31", "2024-02-15", 800)],
            "Assets": [_pt(None, "2023-12-31", "2024-02-15", 1000)],
            "GrossProfit": [_pt("2023-01-01", "2023-12-31", "2024-02-15", 500)],
            "NetIncomeLoss": [
                _pt("2022-01-01", "2022-12-31", "2023-02-15", 100),
                _pt("2023-01-01", "2023-12-31", "2024-02-15", 150)],
            "CommonStockSharesOutstanding": [_pt(None, "2023-12-31", "2024-02-15", 100)],
        },
        "BBB": {
            "StockholdersEquity": [_pt(None, "2023-12-31", "2024-02-15", 200)],
            "Assets": [_pt(None, "2023-12-31", "2024-02-15", 1000)],
            "GrossProfit": [_pt("2023-01-01", "2023-12-31", "2024-02-15", 100)],
            "NetIncomeLoss": [
                _pt("2022-01-01", "2022-12-31", "2023-02-15", 100),
                _pt("2023-01-01", "2023-12-31", "2024-02-15", 90)],
            "CommonStockSharesOutstanding": [_pt(None, "2023-12-31", "2024-02-15", 100)],
        },
    }
    prices = {"AAA": (20, 24), "BBB": (20, 18)}
    res = r.run_shadow_scan(["AAA", "BBB"], asof="2024-06-30", facts=facts, prices=prices)
    assert res["scored"] == 2
    assert res["n_priced"] == 2
    assert res["asof"] == "2024-06-30"


def test_run_shadow_scan_empty_universe():
    res = r.run_shadow_scan([], asof="2024-06-30", facts={}, prices={})
    assert res["scored"] == 0
