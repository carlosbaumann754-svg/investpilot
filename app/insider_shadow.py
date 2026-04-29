"""
Insider Shadow-Tracker (v37m — C1 Forward-A/B)
==============================================

Passiver Tracker: waehrend ``insider_signal_enabled=false`` (Default) wird
fuer jeden Scanner-Candidate trotzdem der Insider-Score berechnet und in
``data/insider_shadow_log.jsonl`` persistiert. Ueber 2-4 Wochen Paper-
Trading laesst sich so vergleichen, ob die hypothetisch geblockten
Candidates schlechter performt haetten als die durchgelassenen.

Datenfluss
----------
1. ``trader.py`` ruft ``log_shadow_decision()`` pro Candidate.
2. JSONL-Append (eine Zeile pro Decision) — append-only, kein Parse-Risiko.
3. Wenn der Bot tatsaechlich BUYs ausfuehrt, schreibt ``trader.py``
   bereits via Trade-History den Outcome. Beim spaeteren Vergleich
   joinen wir Shadow-Decisions ueber (symbol, timestamp) mit den
   Trade-Outcomes.

Datei-Format (JSONL, eine Zeile pro Eintrag):
    {"timestamp": "...", "symbol": "AAPL", "scanner_score": 65.4,
     "insider_score": 2, "would_block": false, "insider_min_score": -1,
     "cycle_id": "20260429_145712"}

Auswertung erfolgt via ``summary_stats()`` und ``/api/insider/shadow``.
Die Empfehlungs-Entscheidung (Live-Filter aktivieren ja/nein) erfolgt
erst nach E5b SEC EDGAR-Scraper-Backtest in W6-W7 — der Shadow-Tracker
ist Plan-B falls E5b laenger dauert.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SHADOW_LOG_FILENAME = "insider_shadow_log.jsonl"
_LOG_LOCK = threading.Lock()


# ============================================================
# WRITE: log_shadow_decision
# ============================================================

def log_shadow_decision(
    symbol: str,
    scanner_score: float,
    insider_score: int,
    would_block: bool,
    insider_min_score: int,
    *,
    cycle_id: Optional[str] = None,
    extra: Optional[dict] = None,
) -> None:
    """Logge eine Insider-Shadow-Decision in JSONL.

    Append-only, threadsafe. Schluckt alle Exceptions (passive
    Telemetrie darf den Bot NIE unterbrechen).
    """
    try:
        from app.config_manager import get_data_path
        path = Path(get_data_path(SHADOW_LOG_FILENAME))
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "scanner_score": round(float(scanner_score), 2),
            "insider_score": int(insider_score),
            "would_block": bool(would_block),
            "insider_min_score": int(insider_min_score),
            "cycle_id": cycle_id or datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S"),
        }
        if extra:
            entry.update(extra)

        with _LOG_LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.debug(f"Shadow-Log fuer {symbol} fehlgeschlagen: {e}")


# ============================================================
# READ: tail / summary
# ============================================================

def read_recent(limit: int = 1000) -> list[dict]:
    """Letzte N Eintraege lesen (chronologisch)."""
    try:
        from app.config_manager import get_data_path
        path = Path(get_data_path(SHADOW_LOG_FILENAME))
        if not path.exists():
            return []
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        # Letzte N Zeilen
        tail = lines[-limit:] if len(lines) > limit else lines
        return [json.loads(line) for line in tail if line.strip()]
    except Exception as e:
        logger.warning(f"Shadow-Log Read fehlgeschlagen: {e}")
        return []


def summary_stats(*, days: int = 14) -> dict:
    """Aggregierte Statistik der letzten N Tage.

    Liefert pro Asset-Klasse + insgesamt:
      - total_candidates_tracked
      - would_block_count / pct
      - avg_scanner_score (geblockt vs durchgelassen)
      - by_insider_score histogram
      - oldest / newest entry
    """
    try:
        entries = read_recent(limit=100_000)
    except Exception:
        entries = []

    if not entries:
        return {
            "total_candidates_tracked": 0,
            "would_block_count": 0,
            "would_block_pct": 0.0,
            "by_insider_score": {},
            "avg_scanner_score_blocked": None,
            "avg_scanner_score_passed": None,
            "oldest_entry": None,
            "newest_entry": None,
            "days_window": days,
            "note": "Keine Shadow-Eintraege vorhanden. Bot muss mind. 1 Cycle "
                    "mit shadow_tracking=true gefahren sein.",
        }

    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    recent = []
    for e in entries:
        try:
            ts = datetime.fromisoformat(e["timestamp"]).timestamp()
            if ts >= cutoff:
                recent.append(e)
        except Exception:
            recent.append(e)  # ohne Zeitfilter wenn Parse fehlschlaegt

    if not recent:
        return {
            "total_candidates_tracked": 0,
            "days_window": days,
            "note": f"Keine Eintraege juenger als {days} Tage.",
        }

    blocked = [e for e in recent if e.get("would_block")]
    passed = [e for e in recent if not e.get("would_block")]

    by_score: dict[str, int] = {}
    for e in recent:
        key = str(e.get("insider_score", 0))
        by_score[key] = by_score.get(key, 0) + 1

    def _avg(arr: list[dict], key: str) -> Optional[float]:
        vals = [e.get(key) for e in arr if isinstance(e.get(key), (int, float))]
        return round(sum(vals) / len(vals), 2) if vals else None

    return {
        "total_candidates_tracked": len(recent),
        "would_block_count": len(blocked),
        "would_block_pct": round(len(blocked) / len(recent) * 100, 1) if recent else 0,
        "by_insider_score": dict(sorted(by_score.items(), key=lambda x: int(x[0]))),
        "avg_scanner_score_blocked": _avg(blocked, "scanner_score"),
        "avg_scanner_score_passed": _avg(passed, "scanner_score"),
        "oldest_entry": recent[0].get("timestamp") if recent else None,
        "newest_entry": recent[-1].get("timestamp") if recent else None,
        "days_window": days,
        "unique_symbols_tracked": len({e["symbol"] for e in recent if "symbol" in e}),
    }


def joined_with_trade_outcomes(*, days: int = 14) -> list[dict]:
    """Joint Shadow-Decisions mit subsequent Trade-Outcomes.

    Fuer jede 'passed'-Decision (wuerde durchgelassen werden) suchen wir
    den naechsten BUY-Trade fuer das Symbol. Wenn dieser BUY spaeter
    geschlossen wurde (Stop-Loss/TP/Trailing), nehmen wir den realisierten
    pnl_pct. So sehen wir: 'haben durchgelassene Candidates Geld gemacht?'.
    """
    try:
        from app.config_manager import load_json
        history = load_json("trade_history.json") or []
    except Exception:
        history = []

    shadow = read_recent(limit=100_000)
    if not shadow or not history:
        return []

    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    recent_shadow = []
    for s in shadow:
        try:
            ts = datetime.fromisoformat(s["timestamp"]).timestamp()
            if ts >= cutoff:
                recent_shadow.append(s)
        except Exception:
            pass

    # Index trades by (symbol, action)
    buys_by_sym: dict[str, list[dict]] = {}
    for t in history:
        if t.get("action") == "BUY" and t.get("symbol"):
            buys_by_sym.setdefault(t["symbol"], []).append(t)

    joined = []
    for s in recent_shadow:
        sym = s.get("symbol")
        if not sym:
            continue
        try:
            s_ts = datetime.fromisoformat(s["timestamp"]).timestamp()
        except Exception:
            continue
        # Suche naechsten BUY nach s_ts
        candidate_buys = []
        for b in buys_by_sym.get(sym, []):
            try:
                b_ts = datetime.fromisoformat(b["timestamp"]).timestamp()
                if b_ts >= s_ts and (b_ts - s_ts) < 600:  # 10 Min Korrelation
                    candidate_buys.append((b_ts, b))
            except Exception:
                pass
        if candidate_buys:
            candidate_buys.sort(key=lambda x: x[0])
            buy = candidate_buys[0][1]
            joined.append({
                "shadow": s,
                "buy_executed": True,
                "buy_timestamp": buy.get("timestamp"),
                "buy_amount_usd": buy.get("amount_usd"),
            })
        else:
            joined.append({
                "shadow": s,
                "buy_executed": False,
            })

    return joined
