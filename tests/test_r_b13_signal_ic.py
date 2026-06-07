"""R-B13 (07.06.2026) — Signal-Qualitaet (IC) als stehende Backtest-Metrik.

Beantwortet ehrlich: sagt der entry_score die Trade-Rendite (pnl_net_pct)
ueberhaupt voraus? IC ~ 0 = kein Edge. Grundlage fuer die Signal-Forschung,
nachdem der WFO-Deep-Dive (07.06) zeigte: aktuelles Signal = generische TA
ohne nachgewiesenen Edge (Mean-OOS-Sharpe -0.916, beste=schlechteste Trades
hatten identische Scores).
"""
from app.backtester import _pearson, _ranks, _compute_signal_ic


def _trades(pairs):
    return [{"entry_score": s, "pnl_net_pct": p} for s, p in pairs]


def test_pearson_perfect_and_anti():
    assert _pearson([1, 2, 3, 4], [1, 2, 3, 4]) > 0.999
    assert _pearson([1, 2, 3, 4], [4, 3, 2, 1]) < -0.999
    assert abs(_pearson([1, 1, 1], [2, 5, 9])) < 1e-9  # konstant -> 0


def test_ranks_handle_ties():
    # [10,10,20] -> Raenge [1.5, 1.5, 3]
    r = _ranks([10, 10, 20])
    assert r[0] == 1.5 and r[1] == 1.5 and r[2] == 3


def test_signal_ic_predictive():
    """Score == Rendite -> IC nahe +1, Top-Quartil >> Bottom-Quartil."""
    pairs = [(50 + i, float(i)) for i in range(40)]
    res = _compute_signal_ic(_trades(pairs))
    assert res["ic_spearman"] > 0.95
    assert res["top_quartile_avg_pct"] > res["bottom_quartile_avg_pct"]
    assert res["verdict"] == "schwach-aber-real"  # > 0.05


def test_signal_ic_no_edge():
    """Score unkorreliert mit Rendite -> IC ~ 0 -> 'KEIN Edge'."""
    pairs = ([(50, 5.0)] * 10 + [(50, -5.0)] * 10
             + [(60, 5.0)] * 10 + [(60, -5.0)] * 10)
    res = _compute_signal_ic(_trades(pairs))
    assert abs(res["ic_pearson"]) < 0.03
    assert abs(res["ic_spearman"]) < 0.03
    assert res["verdict"] == "KEIN Edge (Score trennt nicht)"


def test_signal_ic_anti_predictive():
    """Hoeherer Score -> schlechtere Rendite -> negativer IC."""
    pairs = [(50 + i, float(-i)) for i in range(40)]
    res = _compute_signal_ic(_trades(pairs))
    assert res["ic_spearman"] < -0.95


def test_signal_ic_too_few_trades():
    res = _compute_signal_ic(_trades([(50, 1.0)] * 5))
    assert res["ic_spearman"] is None
    assert res["verdict"] == "zu wenig Trades"


def test_signal_ic_ignores_missing_fields():
    trades = _trades([(50 + i, float(i)) for i in range(40)])
    trades.append({"entry_score": None, "pnl_net_pct": 5})  # ignoriert
    trades.append({"pnl_net_pct": 5})                        # ignoriert
    res = _compute_signal_ic(trades)
    assert res["n"] == 40
