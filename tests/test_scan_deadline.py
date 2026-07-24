"""R-B44 (25.07.2026) — Scan-Deadline: ein haengender Abruf darf den Zyklus nicht toeten.

Vorfall 24.07.: Netzwerk-Abruf in der Kandidaten-Analyse blockierte 3h35min
(20:41-00:16) — Herzschlag, SL/TP-Checks, Zusammenfassung und Snapshot standen
still, mitten in der letzten Handelsstunde. Der Guard wandelt so einen Haenger
in einen TimeoutError, den der bestehende FAIL-SAFE-Faenger zu 'kein Kauf in
diesem Zyklus' macht.
"""
import signal
import time

import pytest

from app.market_scanner import SCAN_DEADLINE_SEC, _ScanDeadline

_POSIX = hasattr(signal, "SIGALRM")


def test_deadline_grosszuegig_gegen_normalen_scan():
    """Normal dauert der Scan ~18s — die Schranke darf nur echte Haenger treffen."""
    assert SCAN_DEADLINE_SEC >= 300


def test_noop_pfad_laesst_arbeit_durch():
    """Ohne SIGALRM (Windows) oder ausserhalb des Main-Threads: No-Op, kein Crash."""
    with _ScanDeadline(1):
        time.sleep(0.01)


@pytest.mark.skipif(not _POSIX, reason="SIGALRM nur auf POSIX")
def test_deadline_bricht_haenger_ab():
    """DER Vorfalls-Fall: blockierender Aufruf -> TimeoutError statt Ewigkeit."""
    with pytest.raises(TimeoutError, match="Scan-Deadline"):
        with _ScanDeadline(1):
            time.sleep(5)


@pytest.mark.skipif(not _POSIX, reason="SIGALRM nur auf POSIX")
def test_alarm_wird_nach_erfolg_entschaerft():
    """Nach sauberem Scan darf kein Rest-Alarm spaeter in den Zyklus feuern."""
    with _ScanDeadline(1):
        pass
    time.sleep(1.2)   # wuerde ein vergessener Alarm feuern, crasht der Test
