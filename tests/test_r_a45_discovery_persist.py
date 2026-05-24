"""Tests fuer R-A45 — Persistent-Layer fuer Asset-Discovery.

Bug: add_to_scanner_universe mutiert nur Runtime-ASSET_UNIVERSE (Python-Dict).
Container-Restart = Verlust aller Discovery-Adds.

Fix R-A45: separate Modul app/discovery_persist.py persistiert in
data/discovered_universe_persist.json + Boot-Time-Reload in market_scanner.py.
"""

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


def _temp_persist_file():
    """Erstellt temp-File-Pfad und cleant nach Test."""
    fd, path = tempfile.mkstemp(suffix="_discoveries.json", prefix="r_a45_test_")
    os.close(fd)
    Path(path).unlink()  # delete leeres file
    return Path(path)


def test_r_a45_save_persisted_discovery_creates_entry():
    """save_persisted_discovery legt Eintrag an + persistiert in JSON."""
    from app import discovery_persist
    tmp = _temp_persist_file()
    with patch.object(discovery_persist, "PERSIST_FILE", tmp):
        result = discovery_persist.save_persisted_discovery(
            "SPOT",
            {"etoro_id": 123, "yf": "SPOT", "class": "stocks", "name": "Spotify", "sector": "tech"},
            25.2,
        )
    assert result is True
    data = json.loads(tmp.read_text(encoding="utf-8"))
    assert len(data["discoveries"]) == 1
    assert data["discoveries"][0]["symbol"] == "SPOT"
    assert data["discoveries"][0]["score"] == 25.2
    tmp.unlink()


def test_r_a45_save_persisted_discovery_idempotent():
    """Doppel-save eines Symbols ist no-op (idempotent)."""
    from app import discovery_persist
    tmp = _temp_persist_file()
    with patch.object(discovery_persist, "PERSIST_FILE", tmp):
        discovery_persist.save_persisted_discovery(
            "MU", {"etoro_id": 456, "yf": "MU", "class": "stocks", "name": "Micron"}, 15.0
        )
        result2 = discovery_persist.save_persisted_discovery(
            "MU", {"etoro_id": 456, "yf": "MU", "class": "stocks", "name": "Micron"}, 99.0
        )
    assert result2 is False  # zweiter Call kein Add
    data = json.loads(tmp.read_text(encoding="utf-8"))
    assert len(data["discoveries"]) == 1
    assert data["discoveries"][0]["score"] == 15.0  # nicht ueberschrieben
    tmp.unlink()


def test_r_a45_merge_into_asset_universe_adds_missing():
    """merge_into_asset_universe addet Symbols die NICHT im AU sind."""
    from app import discovery_persist
    tmp = _temp_persist_file()
    with patch.object(discovery_persist, "PERSIST_FILE", tmp):
        discovery_persist.save_persisted_discovery(
            "SPOT", {"etoro_id": 1, "yf": "SPOT", "class": "stocks", "name": "Spotify"}, 25.2
        )
        au = {"AAPL": {"name": "Apple"}}
        added = discovery_persist.merge_into_asset_universe(au)
    assert added == 1
    assert "SPOT" in au
    tmp.unlink()


def test_r_a45_merge_respects_existing_in_asset_universe():
    """merge_into_asset_universe ueberschreibt NICHT wenn Symbol schon im AU."""
    from app import discovery_persist
    tmp = _temp_persist_file()
    with patch.object(discovery_persist, "PERSIST_FILE", tmp):
        discovery_persist.save_persisted_discovery(
            "AAPL", {"etoro_id": 999, "yf": "AAPL", "class": "stocks", "name": "FAKE"}, 50.0
        )
        au = {"AAPL": {"name": "Apple Real", "etoro_id": 6408}}
        added = discovery_persist.merge_into_asset_universe(au)
    assert added == 0
    assert au["AAPL"]["name"] == "Apple Real"  # nicht ueberschrieben
    tmp.unlink()


def test_r_a45_mark_traded_updates_timestamp():
    """mark_traded setzt last_traded_at fuer matching Symbol."""
    from app import discovery_persist
    tmp = _temp_persist_file()
    with patch.object(discovery_persist, "PERSIST_FILE", tmp):
        discovery_persist.save_persisted_discovery(
            "MRVL", {"etoro_id": 7, "yf": "MRVL", "class": "stocks", "name": "Marvell"}, 22.0
        )
        result = discovery_persist.mark_traded("MRVL")
    assert result is True
    data = json.loads(tmp.read_text(encoding="utf-8"))
    assert data["discoveries"][0]["last_traded_at"] is not None
    tmp.unlink()


def test_r_a45_mark_traded_no_op_for_unknown_symbol():
    """mark_traded gibt False fuer Symbol das NICHT in Discoveries ist."""
    from app import discovery_persist
    tmp = _temp_persist_file()
    with patch.object(discovery_persist, "PERSIST_FILE", tmp):
        result = discovery_persist.mark_traded("UNKNOWN_SYMBOL")
    assert result is False
    tmp.unlink() if tmp.exists() else None


def test_r_a45_cleanup_removes_stale_unused():
    """cleanup_stale_discoveries entfernt Eintraege >max_age + nie getradet."""
    from app import discovery_persist
    tmp = _temp_persist_file()
    # Manuell ein altes Discovery in JSON schreiben (95 Tage alt, nie getradet)
    old_dt = (datetime.now(timezone.utc) - timedelta(days=95)).isoformat()
    fresh_dt = datetime.now(timezone.utc).isoformat()
    tmp.write_text(json.dumps({
        "discoveries": [
            {"symbol": "OLD_NEVER_TRADED", "added_at": old_dt, "last_traded_at": None},
            {"symbol": "OLD_BUT_TRADED", "added_at": old_dt, "last_traded_at": fresh_dt},
            {"symbol": "FRESH", "added_at": fresh_dt, "last_traded_at": None},
        ],
        "updated_at": fresh_dt,
    }), encoding="utf-8")
    with patch.object(discovery_persist, "PERSIST_FILE", tmp):
        removed = discovery_persist.cleanup_stale_discoveries(max_age_days=90)
    assert "OLD_NEVER_TRADED" in removed
    assert "OLD_BUT_TRADED" not in removed  # bleibt weil getradet
    assert "FRESH" not in removed  # bleibt weil jung
    tmp.unlink()


def test_r_a45_purge_discovery_removes_symbol():
    """purge_discovery entfernt ein bestimmtes Symbol manuell."""
    from app import discovery_persist
    tmp = _temp_persist_file()
    with patch.object(discovery_persist, "PERSIST_FILE", tmp):
        discovery_persist.save_persisted_discovery(
            "STX", {"etoro_id": 8, "yf": "STX", "class": "stocks", "name": "Seagate"}, 17.0
        )
        result = discovery_persist.purge_discovery("STX")
    assert result is True
    data = json.loads(tmp.read_text(encoding="utf-8"))
    assert len(data["discoveries"]) == 0
    tmp.unlink()


def test_r_a45_market_scanner_boot_hook_present():
    """market_scanner.py hat Boot-Time-Hook fuer merge_into_asset_universe."""
    src = Path(__file__).parent.parent / "app" / "market_scanner.py"
    body = src.read_text(encoding="utf-8")
    assert "R-A45" in body, "R-A45 Tag fehlt in market_scanner.py"
    assert "merge_into_asset_universe(ASSET_UNIVERSE)" in body, (
        "Boot-Merge-Aufruf fehlt"
    )


def test_r_a45_asset_discovery_persists_after_add():
    """add_to_scanner_universe ruft save_persisted_discovery auf."""
    src = Path(__file__).parent.parent / "app" / "asset_discovery.py"
    body = src.read_text(encoding="utf-8")
    assert "R-A45" in body, "R-A45 Tag fehlt in asset_discovery.py"
    assert "save_persisted_discovery" in body, (
        "Persist-Aufruf in add_to_scanner_universe fehlt"
    )


def test_r_a45_save_trade_marks_discovery_traded():
    """save_trade() ruft mark_traded fuer alle Trades auf (Schutz vor cleanup)."""
    src = Path(__file__).parent.parent / "app" / "trader.py"
    body = src.read_text(encoding="utf-8")
    assert "from app.discovery_persist import mark_traded" in body
    assert "mark_traded(symbol)" in body
