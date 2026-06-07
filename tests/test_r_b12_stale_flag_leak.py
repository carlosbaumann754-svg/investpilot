"""R-B12 (07.06.2026 HOTFIX) — persistiertes _stale-Flag haelt R1-Halt permanent.

Befund (via Carlos' Regime-HALT-Pushover): der Regime-Filter las VIX 21.5 +
F&G 12 (vorhanden!), blockte aber via R1 mit "Market-Context STALE". Ursache:
update_full_context laedt ctx via _load_context; passiert das WAEHREND einer
Stale-Phase, traegt ctx _stale=True; die Funktion setzt echte VIX/F&G, loescht
das Flag aber nicht -> _save_context persistiert _stale=True -> jeder spaetere
Load liest es -> R1 (Batch-2) blockt PERMANENT trotz frischer Daten.

Fix: (1) _load_context bewertet _stale frisch pro Load (bereinigt geleakte
Flags wenn Daten frisch); (2) update_full_context popt _stale vor dem Speichern.
"""
from datetime import datetime, timedelta
from pathlib import Path

import app.market_context as mc


def test_load_context_clears_leaked_stale_when_fresh(monkeypatch):
    """Frische Daten + geleaktes _stale=True in der Datei -> _stale bereinigt."""
    fresh = (datetime.now() - timedelta(minutes=5)).isoformat()
    leaked = {
        "last_update": fresh, "vix_level": 21.5, "fear_greed_index": 12,
        "market_regime": "sideways", "_stale": True,
    }
    monkeypatch.setattr(mc, "load_json", lambda f: dict(leaked))
    ctx = mc._load_context()
    assert ctx.get("_stale") in (False, None), "geleaktes _stale-Flag nicht bereinigt"
    assert ctx.get("vix_level") == 21.5, "frische Daten duerfen nicht genullt werden"
    assert ctx.get("fear_greed_index") == 12


def test_load_context_still_flags_genuinely_stale(monkeypatch):
    """Wirklich alte Daten (>6h) -> _stale=True + VIX/F&G genullt (Regression)."""
    old = (datetime.now() - timedelta(hours=10)).isoformat()
    monkeypatch.setattr(mc, "load_json", lambda f: {
        "last_update": old, "vix_level": 21.5, "fear_greed_index": 12,
        "market_regime": "sideways",
    })
    ctx = mc._load_context()
    assert ctx.get("_stale") is True
    assert ctx.get("vix_level") is None
    assert ctx.get("fear_greed_index") is None


def test_update_full_context_pops_stale_before_save():
    """Source-Guard: update_full_context entfernt _stale VOR _save_context."""
    src = (Path(__file__).parent.parent / "app" / "market_context.py").read_text(encoding="utf-8")
    start = src.find("def update_full_context")
    body = src[start:start + 3000]
    pop_pos = body.find('ctx.pop("_stale"')
    save_pos = body.find("_save_context(ctx)")
    assert pop_pos != -1, "ctx.pop('_stale') fehlt in update_full_context"
    assert save_pos != -1
    assert pop_pos < save_pos, "_stale muss VOR _save_context gepoppt werden"
