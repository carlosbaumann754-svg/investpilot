"""R-B54 (13.08.2026): Struktur-Test gegen das Einfrieren der Backup-Listen.

Historie: Die FILES-Liste in scripts/backup_state.sh ist bereits DREIMAL
eingefroren, waehrend data/ wuchs (April->R-B30, R-B30->R-B54). Jedes Mal
lagen kritische Dateien wochenlang nur auf der VPS-Platte. Dieser Test macht
die Fehlerklasse strukturell sichtbar: Wer eine kritische State-Datei einfuehrt,
muss sie HIER und in der Backup-Liste eintragen — sonst ist die Suite rot.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Kritische State-Dateien: Verlust ist unersetzlich ODER aendert Bot-Verhalten
# ODER zerstoert einen Audit-/Mess-Trail. Neue kritische Dateien HIER ergaenzen.
KRITISCH_TAR = {
    "config.json",
    "trade_history.json",
    "brain_state.json",
    "risk_state.json",
    "equity_history.json",
    "signal_score_history.json",
    "signal_pit_snapshots.json",
    "stack_wfo_baseline.json",
    "manual_lock_overrides.json",
    "roundtrip_pf_reference.json",
    "cutover_confirmations.json",
    "trailing_sl_state.json",
    "buy_cooldown.json",
    "alert_state.json",
    "zwischencheck25_state.json",
    "m2a_geerbt.json",
    "m2a_erwartungsbaender.json",
}

# Unersetzlich (nicht rekonstruierbar ohne Look-Ahead-Bias) -> MUSS zusaetzlich
# eine Off-VPS-Kopie haben (Wochen-Archiv-Gist), weil das tar auf derselben
# Platte liegt wie die Originale.
UNERSETZLICH_OFF_VPS = {
    "signal_score_history.json",
    "signal_pit_snapshots.json",
}


def _tar_liste() -> set:
    text = (REPO / "scripts" / "backup_state.sh").read_text(encoding="utf-8")
    # Schliessende Klammer steht allein am Zeilenanfang — Klammern in
    # Kommentaren duerfen den Match nicht vorzeitig beenden.
    m = re.search(r"FILES=\((.*?)\n\)", text, re.DOTALL)
    assert m, "FILES=(...)-Block in backup_state.sh nicht gefunden"
    return set(re.findall(r'"([^"]+)"', m.group(1)))


def test_taegliches_tar_enthaelt_alle_kritischen():
    fehlt = KRITISCH_TAR - _tar_liste()
    assert not fehlt, (
        f"Kritische State-Dateien fehlen im taeglichen Backup "
        f"(scripts/backup_state.sh FILES): {sorted(fehlt)} — "
        "eintragen oder hier mit dokumentierter Begruendung ausnehmen.")


def test_supervisor_state_wird_gesichert():
    # Liegt auf HOST-Ebene ausserhalb von data/ — braucht den separaten
    # host_state-Tarball im selben Skript.
    text = (REPO / "scripts" / "backup_state.sh").read_text(encoding="utf-8")
    assert "supervisor_state.json" in text, (
        "supervisor_state.json (Host-Ebene) fehlt in backup_state.sh")


def test_unersetzliche_haben_off_vps_kopie():
    text = (REPO / "scripts" / "wochen_archiv_push.py").read_text(encoding="utf-8")
    m = re.search(r"ARCHIV_DATEIEN\s*=\s*\[(.*?)\]", text, re.DOTALL)
    assert m, "ARCHIV_DATEIEN in wochen_archiv_push.py nicht gefunden"
    archiv = set(re.findall(r'"([^"]+)"', m.group(1)))
    fehlt = UNERSETZLICH_OFF_VPS - archiv
    assert not fehlt, (
        f"Unersetzliche Dateien ohne Off-VPS-Kopie: {sorted(fehlt)}")


def test_zyklus_gist_traegt_keine_schweren_dateien():
    # Der Zyklus-Gist wird ~550x/Tag gepusht — die zwei grossen Archive
    # gehoeren NICHT hinein (R-B54: waere >1 GB Upload/Tag).
    from app.persistence import BACKUP_FILES
    schwer = UNERSETZLICH_OFF_VPS & set(BACKUP_FILES)
    assert not schwer, (
        f"Schwere Archivdateien im hochfrequenten Zyklus-Gist: {sorted(schwer)}")
