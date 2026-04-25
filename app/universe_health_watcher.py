"""
Universe Health Watcher (Roadmap POST_LIVE_TECH Item)
======================================================

Beobachtet `data/universe_health.json` (Schema:
{report: {SYMBOL: {status: 'ok'|'<error>', days: int}}}) und schlaegt
Auto-Disable / Re-Enable vor — aber NIE automatisch ausgefuehrt.

Strategien:

**(B) Auto-Disable Vorschlag**: Counter pro Symbol. Wenn 3 aufeinander-
folgende Universe-Health-Checks 'not ok' melden -> Vorschlag in
disable-suggestions Liste. User klickt 1× im Dashboard -> Symbol wandert
in `disabled_symbols`.

**(C) Re-Enable Vorschlag**: Wenn ein bereits disabled Symbol jetzt
3 Wochen in Folge 'ok' liefert (oder anderes Re-Enable-Kriterium) ->
Vorschlag in enable-suggestions. User klickt -> aus disabled_symbols raus.

State-File: `data/universe_health_counters.json`
{
    SYMBOL: {
        "consecutive_not_ok": 0,
        "consecutive_ok": 0,
        "last_status": "ok",
        "last_seen": "2026-04-25T16:00:00",
        "history": [...]  # letzte 10 Checks (status + ts)
    }
}

Suggestions-File: `data/universe_health_suggestions.json`
{
    "to_disable": [{"symbol", "reason", "suggested_at"}],
    "to_enable":  [{"symbol", "reason", "suggested_at"}]
}

Aufruf via Trader/Scheduler nach jedem Universe-Health-Run, oder
manuell via CLI:
    python -m app.universe_health_watcher
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

COUNTERS_FILE = "universe_health_counters.json"
SUGGESTIONS_FILE = "universe_health_suggestions.json"

# Konfiguration (zukuenftig aus config.json: universe_management.{...})
DISABLE_THRESHOLD = 3       # 3 consecutive 'not ok' -> disable suggestion
ENABLE_THRESHOLD = 3        # 3 consecutive 'ok' (im Disabled-Status) -> re-enable suggestion
MAX_HISTORY = 10


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _load(filename: str) -> dict:
    from app.config_manager import load_json
    data = load_json(filename)
    return data if isinstance(data, dict) else {}


def _save(filename: str, data: dict) -> None:
    from app.config_manager import save_json
    save_json(filename, data)


def _is_status_ok(entry: dict) -> bool:
    """Gibt True wenn das Universe-Health Entry 'ok' ist."""
    return (entry or {}).get("status") == "ok"


def update_counters(universe_health: Optional[dict] = None,
                    disabled_symbols: Optional[list] = None) -> dict:
    """Hauptlogik: liest universe_health.report + state, updated counters,
    generiert Vorschlaege.

    Args:
        universe_health: optional Override fuer testing. Sonst aus Datei.
        disabled_symbols: optional Override. Sonst aus config.

    Returns:
        Dict mit 'counters' (state) und 'suggestions' (to_disable/to_enable).
    """
    # 1. Universe-Health Report laden
    if universe_health is None:
        universe_health = _load("universe_health.json")
    report = universe_health.get("report") or {}
    if not report:
        log.warning("Universe-Health Report leer — counters nicht aktualisiert")
        return {"counters": {}, "suggestions": {"to_disable": [], "to_enable": []}}

    # 2. Aktuelle disabled_symbols
    if disabled_symbols is None:
        from app.config_manager import load_config
        cfg = load_config() or {}
        disabled_symbols = list(cfg.get("disabled_symbols") or [])
    disabled_set = set(disabled_symbols)

    # 3. State laden
    counters = _load(COUNTERS_FILE)

    # 4. Pro Symbol updaten
    now = _now()
    for symbol, entry in report.items():
        is_ok = _is_status_ok(entry)
        c = counters.setdefault(symbol, {
            "consecutive_not_ok": 0,
            "consecutive_ok": 0,
            "last_status": None,
            "last_seen": None,
            "history": [],
        })
        # Counter updaten
        if is_ok:
            c["consecutive_ok"] = (c.get("consecutive_ok") or 0) + 1
            c["consecutive_not_ok"] = 0
        else:
            c["consecutive_not_ok"] = (c.get("consecutive_not_ok") or 0) + 1
            c["consecutive_ok"] = 0
        c["last_status"] = entry.get("status", "?")
        c["last_seen"] = now
        # History append (max MAX_HISTORY)
        c.setdefault("history", []).append({"ts": now, "status": entry.get("status")})
        c["history"] = c["history"][-MAX_HISTORY:]

    _save(COUNTERS_FILE, counters)

    # 5. Suggestions generieren
    to_disable = []
    to_enable = []
    for symbol, c in counters.items():
        if symbol in disabled_set:
            # Disabled -> pruefe Re-Enable
            if c["consecutive_ok"] >= ENABLE_THRESHOLD:
                to_enable.append({
                    "symbol": symbol,
                    "reason": f"{c['consecutive_ok']} Checks in Folge 'ok'",
                    "suggested_at": now,
                })
        else:
            # Active -> pruefe Disable
            if c["consecutive_not_ok"] >= DISABLE_THRESHOLD:
                to_disable.append({
                    "symbol": symbol,
                    "reason": f"{c['consecutive_not_ok']} Checks in Folge nicht-ok ({c['last_status']})",
                    "suggested_at": now,
                })

    suggestions = {
        "to_disable": to_disable,
        "to_enable": to_enable,
        "generated_at": now,
        "thresholds": {
            "disable_after_consecutive_not_ok": DISABLE_THRESHOLD,
            "enable_after_consecutive_ok": ENABLE_THRESHOLD,
        },
    }
    _save(SUGGESTIONS_FILE, suggestions)
    log.info("Universe-Watcher: %d disable-Vorschlaege, %d enable-Vorschlaege",
             len(to_disable), len(to_enable))

    return {"counters": counters, "suggestions": suggestions}


def get_suggestions() -> dict:
    """Read-only: aktueller Stand der Suggestions."""
    return _load(SUGGESTIONS_FILE) or {
        "to_disable": [], "to_enable": [],
        "generated_at": None,
        "thresholds": {
            "disable_after_consecutive_not_ok": DISABLE_THRESHOLD,
            "enable_after_consecutive_ok": ENABLE_THRESHOLD,
        },
    }


def confirm_disable(symbol: str) -> dict:
    """User bestaetigt Auto-Disable-Vorschlag. Symbol wandert in
    config.disabled_symbols. Counters werden zurueckgesetzt damit
    spaeteres Re-Enable korrekt zaehlt."""
    from app.config_manager import load_config, save_config
    cfg = load_config() or {}
    disabled = list(cfg.get("disabled_symbols") or [])
    if symbol in disabled:
        return {"status": "noop", "message": f"{symbol} ist bereits disabled"}
    disabled.append(symbol)
    cfg["disabled_symbols"] = disabled
    save_config(cfg)
    # Counter resetten
    counters = _load(COUNTERS_FILE)
    if symbol in counters:
        counters[symbol]["consecutive_not_ok"] = 0
        counters[symbol]["consecutive_ok"] = 0
        _save(COUNTERS_FILE, counters)
    log.info("Universe-Watcher: %s manuell disabled (auf User-Bestaetigung)", symbol)
    return {"status": "ok", "symbol": symbol, "now_disabled_count": len(disabled)}


def confirm_enable(symbol: str) -> dict:
    """User bestaetigt Re-Enable-Vorschlag. Symbol raus aus disabled_symbols."""
    from app.config_manager import load_config, save_config
    cfg = load_config() or {}
    disabled = list(cfg.get("disabled_symbols") or [])
    if symbol not in disabled:
        return {"status": "noop", "message": f"{symbol} ist nicht in disabled_symbols"}
    disabled.remove(symbol)
    cfg["disabled_symbols"] = disabled
    save_config(cfg)
    counters = _load(COUNTERS_FILE)
    if symbol in counters:
        counters[symbol]["consecutive_not_ok"] = 0
        counters[symbol]["consecutive_ok"] = 0
        _save(COUNTERS_FILE, counters)
    log.info("Universe-Watcher: %s re-enabled (auf User-Bestaetigung)", symbol)
    return {"status": "ok", "symbol": symbol, "remaining_disabled_count": len(disabled)}


def main():
    """CLI: aktualisiert counters + zeigt Suggestions."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    result = update_counters()
    print(json.dumps(result["suggestions"], indent=2, default=str))


if __name__ == "__main__":
    main()
