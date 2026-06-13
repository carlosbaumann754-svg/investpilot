"""Signal-Stack-Runner — Shadow-Mode-Orchestrierung (Phase 2).

Berechnet die Composite-Scores fuer ein Universum (EDGAR-Facts + Live-Preise via
yfinance) und loggt sie in signal_stack_shadow.json — OHNE die Handelslogik zu
beruehren. Trading-neutral: validiert den neuen Auswahl-Score LIVE, parallel zur
alten Selektion, bevor er sie in Phase 4 uebernimmt. **Setzt die Soak-Uhr NICHT
zurueck** (kein Eingriff ins Trading).

Preis-Anbindung ist hier isoliert (yfinance); die Kern-Logik (score_universe)
bleibt rein/getestet. ``run_shadow_scan`` akzeptiert ``facts``/``prices`` optional
als Injektion -> voll testbar ohne Netzwerk.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from app import edgar_client, signal_stack

log = logging.getLogger(__name__)

AUDIT_METADATA = {
    "purpose": "Shadow-Mode-Runner: berechnet+loggt Signal-Stack-Scores parallel zur alten Selektion (trading-neutral, validiert vor dem Umschalten in Phase 4)",
    "config_section": None,  # v37dr: config-los (Schalter = top-level use_signal_stack)
    "state_files": ["signal_stack_shadow.json"],
    "self_tests": [],
    "scheduler_hooks": ["run_shadow_scan (Host-Crontab taeglich, 30 21 * * 1-5)"],
    "health_check": "shadow_status",
    "added_in": "v38 (Fundamental-Signal-Stack — Phase 2 Shadow)",
}

_REF_OFFSET = 21  # Handelstage fuer Reversal (~1 Monat)
_SHADOW_FILE = "signal_stack_shadow.json"


def _extract_now_ref(closes: list, ref_offset: int = _REF_OFFSET) -> tuple:
    """(letzter Close, Close ~ref_offset Handelstage zuvor). (None,None) wenn zu kurz."""
    if not closes or len(closes) < ref_offset + 1:
        return None, None
    return closes[-1], closes[-1 - ref_offset]


def fetch_recent_prices(symbols: list, lookback_days: int = 45) -> dict:
    """{symbol: (price_now, price_ref_21d)} via Preis-Provider (v37dm).

    Delegiert an app.price_provider.fetch_recent_prices — yfinance primaer +
    IBKR-Fallback fuer verfehlte Symbole. Bleibt als stabiler Import-Pfad fuer
    den Shadow-Scan-Crontab + Tests erhalten (Backward-Compat-Wrapper). Die
    eigentliche Preis-Anbindung lebt jetzt zentral im Provider (eine Quelle der
    Wahrheit), nicht mehr verstreut hier.
    """
    from app.price_provider import fetch_recent_prices as _provider_fetch
    return _provider_fetch(symbols, lookback_days=lookback_days)


def run_shadow_scan(symbols: list, asof: Optional[str] = None,
                    facts: Optional[dict] = None, prices: Optional[dict] = None) -> dict:
    """Berechne + logge die Composite-Scores. TRADING-NEUTRAL.

    ``facts``/``prices`` koennen injiziert werden (Tests); sonst aus
    edgar_client.load_facts() bzw. yfinance. Speichert signal_stack_shadow.json.
    """
    asof = asof or datetime.utcnow().date().isoformat()
    facts = facts if facts is not None else edgar_client.load_facts()
    prices = prices if prices is not None else fetch_recent_prices(symbols)
    scores = signal_stack.score_universe(symbols, facts, prices, asof)
    payload = {
        "generated_at": datetime.utcnow().isoformat(),
        "asof": asof,
        "n_scored": len(scores),
        "n_priced": len(prices),
        "scores": scores,
    }
    try:
        from app.config_manager import save_json
        save_json(_SHADOW_FILE, payload)
    except Exception as e:  # pragma: no cover - IO best effort
        log.warning("signal_stack_shadow.json schreiben fehlgeschlagen: %s", e)
    _write_universe_health(symbols, prices)
    log.info("Shadow-Scan: %d gescort, %d mit Preis (asof %s)", len(scores), len(prices), asof)
    return {"scored": len(scores), "n_priced": len(prices), "asof": asof}


def _write_universe_health(symbols: list, prices: dict) -> None:
    """Schreibt universe_health.json aus den Shadow-Resultaten fuer das AKTIVE
    (sp600-)Universum: ein Symbol ist 'ok', wenn es einen frischen Kurs hat
    (handelbar + Daten vorhanden), sonst 'no_price'.

    Hintergrund: Der alte Producer (backtester._download_histories) kennt nur das
    ASSET_UNIVERSE und fuehrte alle sp600-Symbole als 'unknown / not in
    ASSET_UNIVERSE' -> Dashboard-Card zeigte faelschlich '331 Probleme'. Da der
    Shadow-Scan die sp600 ohnehin taeglich bepreist, ist er die korrekte Quelle
    fuer die Universe-Health des neuen Motors. Trading-neutral (nur Report).
    Best-effort.
    """
    try:
        from app.config_manager import save_json
        report = {}
        for s in symbols:
            if s in prices:
                report[s] = {"status": "ok", "source": "signal_stack_shadow"}
            else:
                report[s] = {"status": "no_price",
                             "reason": "kein frischer Kurs im Shadow-Scan",
                             "source": "signal_stack_shadow"}
        ok = sum(1 for r in report.values() if r["status"] == "ok")
        save_json("universe_health.json", {
            "generated_at": datetime.utcnow().isoformat(),
            "total_requested": len(symbols),
            "ok_count": ok,
            "error_count": len(report) - ok,
            "source": "signal_stack_shadow",
            "report": report,
        })
        log.info("Universe-Health (Shadow): %d ok, %d ohne Preis", ok, len(report) - ok)
    except Exception as e:  # pragma: no cover - IO best effort
        log.warning("universe_health.json (Shadow) schreiben fehlgeschlagen: %s", e)


def shadow_status() -> dict:
    """Health: letzter Shadow-Lauf."""
    try:
        from app.config_manager import load_json
        p = load_json(_SHADOW_FILE)
    except Exception:
        p = None
    if not isinstance(p, dict) or not p.get("scores"):
        return {"ok": False, "reason": "kein Shadow-Lauf"}
    return {"ok": True, "n_scored": p.get("n_scored", 0), "asof": p.get("asof"),
            "generated_at": p.get("generated_at")}
