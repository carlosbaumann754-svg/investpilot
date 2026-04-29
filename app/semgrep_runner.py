"""
app/semgrep_runner.py — Wochentlicher Semgrep-Scan-Postprozessor.

Was:
   Liest data/semgrep_latest.json (geschrieben von docker run semgrep im
   Wrapper-Skript), vergleicht mit dem letzten Run aus
   data/semgrep_history.json, persistiert + triggert Telegram-Alert bei
   neuen oder verschwundenen Findings.

Aufruf-Pattern (vom Bash-Wrapper):
   docker run --rm ... returntocorp/semgrep:latest semgrep scan ... \\
       --json --output /src/data/semgrep_latest.json
   docker exec investpilot python -m app.semgrep_runner

Hard-Gate-Trigger fuer Telegram:
   1. NEUE ERROR-Findings vs vorigem Run        -> ERROR
   2. NEUE WARNING-Findings >= 3 vs vorigem Run -> WARN
   3. Findings-Count steigt > 2 ueber den letzten Wert -> WARN
   Stille = OK.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)


SEMGREP_LATEST_FILE = "semgrep_latest.json"      # Output des Docker-Scans
SEMGREP_HISTORY_FILE = "semgrep_history.json"    # Time-Series der Runs
SEMGREP_LAST_FINDINGS_FILE = "semgrep_last_findings.json"  # Nur die letzten Findings (fuer Diff)


def _finding_id(f: dict) -> str:
    """Eindeutige ID fuer ein Finding — fuer Diff zwischen Runs.

    Nutzt rule + path + line. Wenn der gleiche Bug an gleicher Stelle
    weiter besteht, ist die ID stabil.
    """
    return (
        f"{f.get('check_id','?')}|"
        f"{f.get('path','?')}|"
        f"{f.get('start',{}).get('line','?')}"
    )


def _summarize(findings: list[dict]) -> dict:
    """Gibt Severity-Counts + Liste der Finding-IDs zurueck."""
    counts: dict[str, int] = {"ERROR": 0, "WARNING": 0, "INFO": 0, "OTHER": 0}
    ids = []
    for f in findings:
        sev = (f.get("extra", {}) or {}).get("severity", "OTHER")
        counts[sev] = counts.get(sev, 0) + 1
        ids.append(_finding_id(f))
    return {
        "total": len(findings),
        "error": counts["ERROR"],
        "warning": counts["WARNING"],
        "info": counts["INFO"],
        "ids": ids,
    }


def _diff_findings(prev_ids: list[str], cur_ids: list[str]) -> dict:
    """Berechnet welche Finding-IDs neu / verschwunden / gleich geblieben sind."""
    prev_set = set(prev_ids or [])
    cur_set = set(cur_ids or [])
    return {
        "new": sorted(cur_set - prev_set),
        "gone": sorted(prev_set - cur_set),
        "stable": sorted(cur_set & prev_set),
    }


def _append_history(summary: dict, diff: dict, trigger: str) -> None:
    """Time-Series-Append in semgrep_history.json (max 60 Eintraege = ~14 Mo)."""
    try:
        from app.config_manager import load_json, save_json
        hist = load_json(SEMGREP_HISTORY_FILE) or {"runs": []}
        if not isinstance(hist, dict):
            hist = {"runs": []}
        runs = hist.get("runs", [])
        runs.append({
            "timestamp": datetime.utcnow().isoformat(),
            "trigger": trigger,
            "total": summary["total"],
            "error": summary["error"],
            "warning": summary["warning"],
            "info": summary["info"],
            "new_count": len(diff["new"]),
            "gone_count": len(diff["gone"]),
            "stable_count": len(diff["stable"]),
        })
        if len(runs) > 60:
            runs = runs[-60:]
        hist["runs"] = runs
        hist["updated_at"] = datetime.utcnow().isoformat()
        save_json(SEMGREP_HISTORY_FILE, hist)
    except Exception as e:
        log.warning("History-append failed: %s", e)


def process_latest_scan(trigger: str = "manual") -> dict:
    """Hauptfunktion — wird vom CLI + Cron aufgerufen.

    1. Liest semgrep_latest.json (Docker-Scan-Output)
    2. Vergleicht mit semgrep_last_findings.json (vorheriger Stand)
    3. Schreibt History + last_findings, triggert Alert wenn noetig
    4. Returns Summary + Diff fuer Caller
    """
    from app.config_manager import load_json, save_json

    latest = load_json(SEMGREP_LATEST_FILE)
    if not latest or "results" not in latest:
        log.error("Kein gueltiges semgrep_latest.json gefunden")
        return {"error": "no scan output found"}

    findings = latest.get("results") or []
    summary = _summarize(findings)
    log.info(
        "Semgrep-Scan: total=%d error=%d warning=%d info=%d",
        summary["total"], summary["error"], summary["warning"], summary["info"],
    )

    prev = load_json(SEMGREP_LAST_FINDINGS_FILE) or {}
    prev_ids = prev.get("ids", [])
    diff = _diff_findings(prev_ids, summary["ids"])
    log.info(
        "Diff vs prev: new=%d gone=%d stable=%d",
        len(diff["new"]), len(diff["gone"]), len(diff["stable"]),
    )

    # Persist current as new "last"
    save_json(SEMGREP_LAST_FINDINGS_FILE, {
        "timestamp": datetime.utcnow().isoformat(),
        "trigger": trigger,
        "summary": {k: v for k, v in summary.items() if k != "ids"},
        "ids": summary["ids"],
        # Vollstaendige Findings nur die ersten 20 mitspeichern (Telegram-Limits)
        "details_top20": [
            {
                "check_id": f.get("check_id"),
                "severity": (f.get("extra") or {}).get("severity"),
                "path": f.get("path"),
                "line": (f.get("start") or {}).get("line"),
                "message": (f.get("extra") or {}).get("message", "")[:200],
            }
            for f in findings[:20]
        ],
    })

    _append_history(summary, diff, trigger)

    # Telegram-Alert nur wenn was Neues
    try:
        from app.alerts import check_semgrep_alerts
        check_semgrep_alerts()
    except Exception as e:
        log.warning("check_semgrep_alerts failed: %s", e)

    return {"summary": summary, "diff": diff}


if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [SEMGREP-RUNNER] [%(levelname)s] %(message)s",
    )
    trigger = sys.argv[1] if len(sys.argv) > 1 else "manual"
    result = process_latest_scan(trigger=trigger)
    print(json.dumps(result, indent=2, default=str))
