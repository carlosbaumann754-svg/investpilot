"""R-A45 (24.05.2026 Sprint-Tag-13): Persistent-Layer fuer Asset-Discovery.

Architektur-Lücke vor R-A45:
  - Asset-Discovery findet Freitags neue Symbole via discover_new_assets()
  - evaluate_new_assets() bewertet sie technisch
  - add_to_scanner_universe() mutiert ASSET_UNIVERSE (Python-Runtime-Dict)
  - Bei Container-Restart: Mutationen verloren → Discoveries weg
  - Resultat: Bot startet immer mit nur den 70 hardcoded Universe-Symbolen

R-A45 Fix:
  - Neue Modul-Schicht persistiert Discoveries in
    data/discovered_universe_persist.json
  - Beim Bot-Boot werden persisted Discoveries automatisch ins
    ASSET_UNIVERSE re-merged
  - Optional: Time-Window-Cleanup (>90d ohne Trade → remove) gegen
    Universe-Pollution

Disziplin-Schutz:
  - max_age_days=90 = nach 3 Monaten ohne Trade automatisch raus
    (gegen schlecht-performende Discoveries die nie zum Edge werden)
  - mark_traded() updated last_traded_at bei jedem realen Trade
  - User kann manuell purgen via purge_discovery(symbol)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

AUDIT_METADATA = {
    "purpose": "R-A45 Persistent-Layer fuer Asset-Discovery Universe-Adds",
    "config_section": None,
    "state_files": ["discovered_universe_persist.json"],
    "self_tests": [],
    "scheduler_hooks": [],
    "health_check": None,
    "added_in": "R-A45 (24.05.2026)",
}

DATA_DIR = Path(__file__).parent.parent / "data"
PERSIST_FILE = DATA_DIR / "discovered_universe_persist.json"


def _load_raw() -> dict:
    """Internal: lade JSON-Datei, returnt Default-Struktur wenn leer."""
    if not PERSIST_FILE.exists():
        return {"discoveries": [], "updated_at": None}
    try:
        with open(PERSIST_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"discoveries": [], "updated_at": None}
        if "discoveries" not in data:
            data["discoveries"] = []
        return data
    except Exception as e:
        log.warning(f"R-A45 _load_raw failed: {e}")
        return {"discoveries": [], "updated_at": None}


def _save_raw(data: dict) -> None:
    """Internal: speichere JSON-Datei (atomic)."""
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PERSIST_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp.replace(PERSIST_FILE)


def save_persisted_discovery(symbol: str, asset_info: dict, score: float) -> bool:
    """Persisiere eine Discovery-Addition.

    Args:
        symbol: e.g. "SPOT"
        asset_info: dict mit {etoro_id, yf, class, name}
        score: Discovery-Score (≥15 per add_to_scanner_universe)

    Returns:
        True wenn neu hinzugefuegt, False wenn schon drin (idempotent).
    """
    data = _load_raw()
    discoveries = data.get("discoveries", [])
    # Idempotent: kein doppelter Eintrag
    for d in discoveries:
        if d.get("symbol") == symbol:
            return False  # bereits drin
    # R-A46 (25.05.2026): id=None -> -1 normalisieren (sonst int()-Crash).
    # R-B1 Phase 4b (29.05.2026): internal_id ist der semantisch korrekte
    # Name. asset_info kann internal_id ODER (Legacy) etoro_id liefern.
    # Persistiert wird BEIDES (Dual-Key) damit alte Reader (die noch etoro_id
    # lesen) + neue (internal_id) beide funktionieren. Wert bleibt numerisch.
    eid = asset_info.get("internal_id")
    if eid is None:
        eid = asset_info.get("etoro_id")
    _norm_id = int(eid) if eid is not None else -1
    entry = {
        "symbol": symbol,
        "internal_id": _norm_id,
        "etoro_id": _norm_id,  # Dual-Key fuer Backward-Compat (entfernt in 4d)
        "yf_symbol": asset_info.get("yf"),
        "asset_class": asset_info.get("class"),
        "name": asset_info.get("name"),
        "sector": asset_info.get("sector"),
        "added_at": datetime.now(timezone.utc).isoformat(),
        "score": float(score),
        "last_traded_at": None,
    }
    discoveries.append(entry)
    data["discoveries"] = discoveries
    _save_raw(data)
    log.info(f"R-A45 Persistiert: {symbol} (Score={score:.1f})")
    return True


def load_persisted_discoveries() -> list[dict]:
    """Liefere alle aktuell persisted Discoveries.

    Returns:
        Liste der dicts (kann leer sein).
    """
    data = _load_raw()
    return data.get("discoveries", [])


def merge_into_asset_universe(asset_universe: dict) -> int:
    """Beim Bot-Boot: persisted Discoveries ins ASSET_UNIVERSE re-mergen.

    Idempotent: Symbol das schon im ASSET_UNIVERSE ist (z.B. hardcoded
    oder bereits gemerged) wird nicht ueberschrieben.

    Args:
        asset_universe: das Python-Dict ASSET_UNIVERSE (wird in-place mutiert).

    Returns:
        Anzahl der neu hinzugefuegten Symbole.
    """
    discoveries = load_persisted_discoveries()
    added = 0
    for d in discoveries:
        sym = d.get("symbol")
        if not sym or sym in asset_universe:
            continue  # schon drin, skip
        # R-A46: id default -1 (NICHT None) damit trader.py int()-Cast nicht
        # crasht. R-B1 Phase 4b: internal_id bevorzugt (Legacy-Files haben
        # nur etoro_id -> Fallback). Dual-Key ins ASSET_UNIVERSE.
        _pid = d.get("internal_id")
        if _pid is None:
            _pid = d.get("etoro_id")
        _pid = _pid if _pid is not None else -1
        asset_universe[sym] = {
            "internal_id": _pid,
            "etoro_id": _pid,  # Dual-Key (entfernt in 4d)
            "yf": d.get("yf_symbol"),
            "class": d.get("asset_class"),
            "name": d.get("name"),
            "sector": d.get("sector"),
        }
        added += 1
    if added:
        log.info(f"R-A45 Boot-Merge: {added} persisted Discoveries ins Universe geladen")
    return added


def mark_traded(symbol: str) -> bool:
    """Updated last_traded_at fuer ein Symbol — Schutz vor Time-Window-Cleanup.

    Wird bei jedem realen Trade aufgerufen (save_trade-Hook).

    Returns:
        True wenn Symbol in Discoveries gefunden + geupdated, sonst False.
    """
    data = _load_raw()
    discoveries = data.get("discoveries", [])
    found = False
    for d in discoveries:
        if d.get("symbol") == symbol:
            d["last_traded_at"] = datetime.now(timezone.utc).isoformat()
            found = True
            break
    if found:
        _save_raw(data)
    return found


def cleanup_stale_discoveries(max_age_days: int = 90) -> list[str]:
    """Entferne Discoveries die >max_age_days alt + nie getraded wurden.

    Disziplin-Schutz vor Universe-Pollution durch schlecht-performende
    Discoveries die nie zum Bot's Edge wurden.

    Args:
        max_age_days: Schwelle (default 90).

    Returns:
        Liste der entfernten Symbole.
    """
    data = _load_raw()
    discoveries = data.get("discoveries", [])
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    keep = []
    removed = []
    for d in discoveries:
        # Wenn jemals getraded → behalten
        if d.get("last_traded_at"):
            keep.append(d)
            continue
        # Wenn juenger als cutoff → behalten
        try:
            added_dt = datetime.fromisoformat(d.get("added_at", "").replace("Z", "+00:00"))
            if added_dt > cutoff:
                keep.append(d)
                continue
        except Exception:
            keep.append(d)
            continue
        # >max_age_days alt und nie getraded → entfernen
        removed.append(d.get("symbol"))
    if removed:
        data["discoveries"] = keep
        _save_raw(data)
        log.info(f"R-A45 cleanup: {len(removed)} stale Discoveries entfernt: {removed}")
    return removed


def purge_discovery(symbol: str) -> bool:
    """Manueller Purge eines Discovery-Symbols.

    Returns:
        True wenn entfernt, False wenn nicht gefunden.
    """
    data = _load_raw()
    discoveries = data.get("discoveries", [])
    new_list = [d for d in discoveries if d.get("symbol") != symbol]
    if len(new_list) == len(discoveries):
        return False  # nicht gefunden
    data["discoveries"] = new_list
    _save_raw(data)
    log.info(f"R-A45 purged: {symbol}")
    return True


def get_persist_stats() -> dict:
    """Status-Stats fuer Dashboard/Diagnose."""
    discoveries = load_persisted_discoveries()
    total = len(discoveries)
    traded = sum(1 for d in discoveries if d.get("last_traded_at"))
    return {
        "total_persisted": total,
        "ever_traded": traded,
        "never_traded": total - traded,
        "symbols": [d.get("symbol") for d in discoveries],
    }
