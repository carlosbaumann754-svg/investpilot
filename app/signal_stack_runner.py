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
    "config_section": "signal_stack",
    "state_files": ["signal_stack_shadow.json"],
    "self_tests": [],
    "scheduler_hooks": ["run_shadow_scan (taeglich)"],
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
    """{symbol: (price_now, price_ref_21d)} via yfinance. {} wenn yfinance fehlt."""
    try:
        import yfinance as yf
    except ImportError:
        log.warning("yfinance nicht verfuegbar — Shadow-Preise leer")
        return {}
    data = yf.download(symbols, period="%dd" % lookback_days, interval="1d",
                       group_by="ticker", auto_adjust=True, threads=True, progress=False)
    out = {}
    multi = len(symbols) > 1
    for s in symbols:
        try:
            df = data[s] if multi else data
            closes = df["Close"].dropna().values.tolist()
        except Exception:
            continue
        pn, pr = _extract_now_ref(closes)
        if pn is not None and pr is not None:
            out[s] = (pn, pr)
    return out


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
    log.info("Shadow-Scan: %d gescort, %d mit Preis (asof %s)", len(scores), len(prices), asof)
    return {"scored": len(scores), "n_priced": len(prices), "asof": asof}


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
