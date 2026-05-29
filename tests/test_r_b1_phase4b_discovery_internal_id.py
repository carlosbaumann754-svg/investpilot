"""Tests fuer R-B1 Phase 4b — discovery_persist internal_id Dual-Key.

Phase 4b: discovery_persist schreibt internal_id (semantisch korrekt) +
etoro_id (Dual-Key Backward-Compat). Liest internal_id mit etoro_id-
Fallback (Legacy-persistierte Files). Werte bleiben numerisch.
"""

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path as P
from unittest.mock import patch

from app import discovery_persist


def _tmp_persist():
    fd, path = tempfile.mkstemp(suffix="_p4b.json", prefix="test_")
    os.close(fd)
    p = P(path)
    p.unlink()
    return p


# ---------------------------------------------------------------------------
# save schreibt Dual-Key
# ---------------------------------------------------------------------------

def test_p4b_save_writes_both_keys():
    """save_persisted_discovery schreibt internal_id UND etoro_id (gleicher Wert)."""
    tmp = _tmp_persist()
    with patch.object(discovery_persist, "PERSIST_FILE", tmp):
        discovery_persist.save_persisted_discovery(
            "FOO", {"internal_id": 6408, "yf": "FOO", "class": "stocks", "name": "Foo"}, 20.0)
    data = json.loads(tmp.read_text(encoding="utf-8"))
    e = data["discoveries"][0]
    assert e["internal_id"] == 6408
    assert e["etoro_id"] == 6408  # Dual-Key
    tmp.unlink()


def test_p4b_save_legacy_etoro_id_input():
    """asset_info mit nur etoro_id (Legacy-Caller) -> beide Keys gesetzt."""
    tmp = _tmp_persist()
    with patch.object(discovery_persist, "PERSIST_FILE", tmp):
        discovery_persist.save_persisted_discovery(
            "BAR", {"etoro_id": 1139, "yf": "BAR", "class": "stocks", "name": "Bar"}, 18.0)
    e = json.loads(tmp.read_text(encoding="utf-8"))["discoveries"][0]
    assert e["internal_id"] == 1139
    assert e["etoro_id"] == 1139
    tmp.unlink()


def test_p4b_save_none_id_normalizes_minus1():
    """R-A46-Kompat: id=None -> -1 in BEIDEN Keys (kein int()-Crash)."""
    tmp = _tmp_persist()
    with patch.object(discovery_persist, "PERSIST_FILE", tmp):
        discovery_persist.save_persisted_discovery(
            "SPOT", {"internal_id": None, "etoro_id": None, "yf": "SPOT",
                     "class": "stocks", "name": "Spotify"}, 20.0)
    e = json.loads(tmp.read_text(encoding="utf-8"))["discoveries"][0]
    assert e["internal_id"] == -1
    assert e["etoro_id"] == -1
    tmp.unlink()


# ---------------------------------------------------------------------------
# merge liest internal_id (mit etoro_id-Fallback fuer Legacy-Files)
# ---------------------------------------------------------------------------

def test_p4b_merge_reads_internal_id():
    """merge_into_asset_universe nutzt internal_id wenn vorhanden."""
    tmp = _tmp_persist()
    tmp.write_text(json.dumps({"discoveries": [{
        "symbol": "NEWSYM", "internal_id": 7777, "etoro_id": 7777,
        "yf_symbol": "NEW", "asset_class": "stocks", "name": "New",
        "added_at": datetime.now(timezone.utc).isoformat(),
    }]}), encoding="utf-8")
    with patch.object(discovery_persist, "PERSIST_FILE", tmp):
        au = {}
        discovery_persist.merge_into_asset_universe(au)
    assert au["NEWSYM"]["internal_id"] == 7777
    assert au["NEWSYM"]["etoro_id"] == 7777  # Dual-Key
    tmp.unlink()


def test_p4b_merge_legacy_file_only_etoro_id():
    """Legacy-File (nur etoro_id, kein internal_id) -> Fallback, beide Keys
    ins Universe."""
    tmp = _tmp_persist()
    tmp.write_text(json.dumps({"discoveries": [{
        "symbol": "LEGACYSYM", "etoro_id": 5555,
        "yf_symbol": "LEG", "asset_class": "stocks", "name": "Legacy",
        "added_at": datetime.now(timezone.utc).isoformat(),
    }]}), encoding="utf-8")
    with patch.object(discovery_persist, "PERSIST_FILE", tmp):
        au = {}
        discovery_persist.merge_into_asset_universe(au)
    assert au["LEGACYSYM"]["internal_id"] == 5555
    assert au["LEGACYSYM"]["etoro_id"] == 5555
    tmp.unlink()


def test_p4b_merge_none_id_to_minus1():
    """Legacy-File mit etoro_id=None -> -1 in beiden Keys."""
    tmp = _tmp_persist()
    tmp.write_text(json.dumps({"discoveries": [{
        "symbol": "NONESYM", "etoro_id": None,
        "yf_symbol": "N", "asset_class": "stocks", "name": "None",
        "added_at": datetime.now(timezone.utc).isoformat(),
    }]}), encoding="utf-8")
    with patch.object(discovery_persist, "PERSIST_FILE", tmp):
        au = {}
        discovery_persist.merge_into_asset_universe(au)
    assert au["NONESYM"]["internal_id"] == -1
    assert au["NONESYM"]["etoro_id"] == -1
    tmp.unlink()
