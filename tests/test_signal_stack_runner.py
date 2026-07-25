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
# Warum temp_data_dir (conftest, Fix 25.07.2026): run_shadow_scan schreibt
# REAL drei Dateien (signal_stack_shadow.json, signal_score_history.json,
# universe_health.json). Ohne Isolation landeten die Test-Payloads
# ('AAA'/'BBB', asof 2024-06-30) im echten data/ des Repos bzw.
# ueberschrieben dort die gitignorierten Runtime-Outputs.
def test_run_shadow_scan_injected(temp_data_dir):
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


def test_run_shadow_scan_empty_universe(temp_data_dir):
    res = r.run_shadow_scan([], asof="2024-06-30", facts={}, prices={})
    assert res["scored"] == 0


# --- _write_universe_health (sp600-Universe-Health aus Shadow-Resultaten) ----
def test_write_universe_health_marks_priced_ok(monkeypatch):
    """Symbol mit frischem Preis -> 'ok', ohne -> 'no_price'. Behebt den alten
    Backtester-Report, der sp600 als 'not in ASSET_UNIVERSE' fuehrte."""
    import app.config_manager as cm
    captured = {}
    monkeypatch.setattr(cm, "save_json", lambda fn, data: captured.__setitem__(fn, data))

    r._write_universe_health(["AAA", "BBB", "CCC"], {"AAA": (20, 24), "CCC": (10, 9)})

    uh = captured["universe_health.json"]
    assert uh["source"] == "signal_stack_shadow"
    assert uh["total_requested"] == 3
    assert uh["ok_count"] == 2
    assert uh["error_count"] == 1
    assert uh["report"]["AAA"]["status"] == "ok"
    assert uh["report"]["CCC"]["status"] == "ok"
    assert uh["report"]["BBB"]["status"] == "no_price"


def test_write_universe_health_all_unpriced(monkeypatch):
    import app.config_manager as cm
    captured = {}
    monkeypatch.setattr(cm, "save_json", lambda fn, data: captured.__setitem__(fn, data))
    r._write_universe_health(["X", "Y"], {})
    uh = captured["universe_health.json"]
    assert uh["ok_count"] == 0 and uh["error_count"] == 2
    assert all(v["status"] == "no_price" for v in uh["report"].values())
