"""Tests fuer app/signal_stack.py — Perzentil-Ranking + Composite-Score."""
from app import signal_stack as ss


def _pt(s, e, f, v):
    return {"s": s, "e": e, "f": f, "v": v}


# --- percentiles -----------------------------------------------------------
def test_percentiles_basic_order():
    # 10<20<30 -> Perzentile 0, 0.5, 1.0
    p = ss.percentiles([10, 20, 30])
    assert p == [0.0, 0.5, 1.0]


def test_percentiles_none_is_neutral():
    p = ss.percentiles([10, None, 30])
    # None -> 0.5; 10 ist niedrigster, 30 hoechster der 2 gueltigen
    assert p[1] == 0.5
    assert p[0] == 0.0 and p[2] == 1.0


def test_percentiles_too_few_valid_all_neutral():
    assert ss.percentiles([None, 5, None]) == [0.5, 0.5, 0.5]


def test_percentiles_ties():
    p = ss.percentiles([5, 5, 9])
    # zwei 5er teilen sich Rang -> gleiches Perzentil, 9 oben
    assert p[0] == p[1]
    assert p[2] == 1.0


# --- compute_composite_scores ---------------------------------------------
def test_composite_best_and_worst():
    raw = {
        "BEST": {"value": 9, "quality": 9, "reversal": 9, "lev": 9, "earngrowth": 9},
        "MID":  {"value": 5, "quality": 5, "reversal": 5, "lev": 5, "earngrowth": 5},
        "WORST": {"value": 1, "quality": 1, "reversal": 1, "lev": 1, "earngrowth": 1},
    }
    out = ss.compute_composite_scores(raw)
    assert out["BEST"]["score"] == 100.0   # ueberall hoechstes Perzentil (1.0)
    assert out["WORST"]["score"] == 0.0    # ueberall niedrigstes (0.0)
    assert 40 < out["MID"]["score"] < 60   # Mitte


def test_composite_missing_signal_neutral():
    raw = {
        "A": {"value": 9, "quality": None, "reversal": 9, "lev": 9, "earngrowth": 9},
        "B": {"value": 1, "quality": 5, "reversal": 1, "lev": 1, "earngrowth": 1},
        "C": {"value": 5, "quality": 9, "reversal": 5, "lev": 5, "earngrowth": 5},
    }
    out = ss.compute_composite_scores(raw)
    # A fehlt quality -> dort neutral 0.5, aber sonst top -> trotzdem hoechster Score
    assert out["A"]["percentiles"]["quality"] == 0.5
    assert out["A"]["score"] > out["C"]["score"] > out["B"]["score"]


def test_composite_empty():
    assert ss.compute_composite_scores({}) == {}


# --- score_universe (Integration mit EDGAR-Records + Preisen) --------------
def test_score_universe_integration():
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
    # AAA: guenstiger (mehr Equity), profitabler, stark, waechst + juengerer Verlierer
    prices = {"AAA": (20, 24), "BBB": (20, 18)}
    out = ss.score_universe(["AAA", "BBB"], facts, prices, "2024-06-30")
    assert out["AAA"]["score"] > out["BBB"]["score"]


# --- ranked_symbols --------------------------------------------------------
def test_ranked_symbols_order_and_top():
    scores = {"X": {"score": 10}, "Y": {"score": 90}, "Z": {"score": 50}}
    assert ss.ranked_symbols(scores) == ["Y", "Z", "X"]
    assert ss.ranked_symbols(scores, top_fraction=0.34) == ["Y"]
