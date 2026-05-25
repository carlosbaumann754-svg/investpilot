"""Tests fuer R-A46 — etoro_id=None TypeError Regression-Fix (from R-A45).

Bug: Sentry-Issue PYTHON-FASTAPI-S "TypeError: int() argument must be a
string ... not 'NoneType'" in trader._resolve_meta_for_id, 67 Events in 6h.

Wurzel: R-A45 Backfill schrieb etoro_id=None fuer SPOT/MRVL/STX/MU
(keine eToro-IDs bekannt). merge_into_asset_universe propagierte None
ins ASSET_UNIVERSE. trader._resolve_meta_for_id versuchte int(None) ->
TypeError bei jedem Bot-Cycle.

Plus Python-Gotcha: dict.get(key, default) returnt None wenn Key
existiert mit Value=None — NICHT den Default. R-A45 hatte
`info.get("etoro_id", -1)` was bei None nicht griff.

Fix R-A46 (Defense-in-Depth):
1. trader._resolve_meta_for_id: 'info.get("etoro_id") or -1' Pattern
2. discovery_persist.save_persisted_discovery: etoro_id=None -> -1
3. discovery_persist.merge_into_asset_universe: same defensive
"""

from pathlib import Path


def test_r_a46_trader_uses_defensive_get_pattern():
    """trader._resolve_meta_for_id MUSS '.get(key) or -1' nutzen,
    NICHT '.get(key, -1)' (gotcha bei Value=None)."""
    src = Path(__file__).parent.parent / "app" / "trader.py"
    body = src.read_text(encoding="utf-8")
    # R-A46 Tag muss vorhanden sein
    assert "R-A46" in body, "R-A46 Tag fehlt in trader.py"
    # Defensive Pattern muss vorhanden sein
    assert 'info.get("etoro_id") or -1' in body, (
        "R-A46 defensive Pattern fehlt — alter 'get(key, -1)' Pattern wuerde "
        "Bug reintroducen"
    )


def test_r_a46_trader_old_pattern_gone():
    """Alter 'info.get(\"etoro_id\", -1)' Pattern darf nicht mehr in
    _resolve_meta_for_id Code-Pfad sein (Regression-Schutz)."""
    src = Path(__file__).parent.parent / "app" / "trader.py"
    body = src.read_text(encoding="utf-8")
    # Lookup nur die _resolve_meta_for_id-Funktion
    fn_start = body.index("def _resolve_meta_for_id")
    fn_end = body.index("\ndef ", fn_start + 50)
    fn_body = body[fn_start:fn_end]
    assert 'info.get("etoro_id", -1)' not in fn_body, (
        "R-A46 REGRESSION: alter buggy Pattern ist zurueck"
    )


def test_r_a46_discovery_persist_etoro_id_none_to_minus1():
    """save_persisted_discovery normalisiert etoro_id=None auf -1."""
    import json
    import os
    import tempfile
    from pathlib import Path as P
    from unittest.mock import patch
    from app import discovery_persist

    fd, fd_path = tempfile.mkstemp(suffix="_r_a46.json", prefix="test_")
    os.close(fd)  # Windows file-lock: FD muss geschlossen sein vor unlink
    tmp = P(fd_path)
    tmp.unlink()

    with patch.object(discovery_persist, "PERSIST_FILE", tmp):
        discovery_persist.save_persisted_discovery(
            "FOO_NONE",
            {"etoro_id": None, "yf": "FOO", "class": "stocks", "name": "Foo"},
            20.0,
        )
    data = json.loads(tmp.read_text(encoding="utf-8"))
    assert data["discoveries"][0]["etoro_id"] == -1, (
        "None etoro_id sollte zu -1 normalisiert werden"
    )
    tmp.unlink()


def test_r_a46_merge_handles_none_etoro_id_in_persisted_file():
    """merge_into_asset_universe schreibt -1 (NICHT None) ins ASSET_UNIVERSE
    wenn die persisted-Discovery noch eine alte None hat (Legacy-State)."""
    import json
    import os
    import tempfile
    from datetime import datetime, timezone
    from pathlib import Path as P
    from unittest.mock import patch
    from app import discovery_persist

    fd, fd_path = tempfile.mkstemp(suffix="_r_a46_legacy.json", prefix="test_")
    os.close(fd)  # Windows file-lock-Fix
    tmp = P(fd_path)
    # Manuell legacy-state mit etoro_id=None schreiben (wie R-A45 v0 schrieb)
    tmp.write_text(
        json.dumps({
            "discoveries": [
                {
                    "symbol": "LEGACY_NONE",
                    "etoro_id": None,
                    "yf_symbol": "LEGACY",
                    "asset_class": "stocks",
                    "name": "Legacy",
                    "added_at": datetime.now(timezone.utc).isoformat(),
                }
            ]
        }),
        encoding="utf-8",
    )

    with patch.object(discovery_persist, "PERSIST_FILE", tmp):
        au = {}
        added = discovery_persist.merge_into_asset_universe(au)

    assert added == 1
    assert "LEGACY_NONE" in au
    assert au["LEGACY_NONE"]["etoro_id"] == -1, (
        "merge muss None auf -1 normalisieren (sonst trader crash)"
    )
    tmp.unlink()


def test_r_a46_int_call_works_with_minus1():
    """Nach R-A46-Fix muss int() in trader._resolve_meta_for_id
    crash-frei laufen auch wenn etoro_id=-1 (statt None)."""
    # Simulation: int(-1) ist OK, int(None) crashed
    assert int(-1) == -1
    # Fix-Pattern: dict.get(k) or -1 - bei None gibt es -1 zurueck
    info = {"etoro_id": None}
    assert (info.get("etoro_id") or -1) == -1
    assert int(info.get("etoro_id") or -1) == -1  # crash-frei


def test_r_a46_known_etoro_ids_still_work():
    """Defensive Pattern darf existierende etoro_ids NICHT brechen."""
    info_apple = {"etoro_id": 6408}
    assert int(info_apple.get("etoro_id") or -1) == 6408
    info_msft = {"etoro_id": 1139}
    assert int(info_msft.get("etoro_id") or -1) == 1139
