"""M2A-Gates-Wecker (R-B66) — G1-Monatspruefung + G3/G4-Leiter-Countdown.

Bindendes Regelwerk: docs/M2A_MOTOR_REGELWERK.md. Host-Cron TAEGLICH
(Mo-Fr 06:00 UTC, stdin-Pattern); das Skript entscheidet selbst:
- m2a nicht aktiv -> stiller Exit (Cron darf schon vor dem Schnitt laufen).
- G1 feuert genau EINMAL pro Monat (am 1. Handelstag): Vormonats-Rendite
  (R-B71: FX-neutral, nur M2a-Positionen — m2a_motor.monatsrenditen)
  gegen die Modell-Baender aus data/m2a_erwartungsbaender.json.
  < p01 -> Prio 1 (Regelwerk-relevant), sonst Prio 0 (Protokoll).
- G3/G4: Countdown zur 6-Monats-Leiter im G1-Text; die Leiter selbst wird
  beim Claude-Check gefahren (Pushover erinnert im 6. Monat mit Prio 1).
Marker: data/m2a_gates_state.json (einmal pro Monat).
"""
import json
import os
from datetime import date, datetime, timedelta

AUDIT_METADATA = {
    "purpose": (
        "M2A-Gates-Wecker (bindendes Regelwerk R-B65/66): meldet monatlich "
        "am 1. Handelstag die Vormonats-Rendite (USD, FX-neutral, nur M2a — R-B71) gegen die validierten "
        "Modell-Baender (G1), erinnert an die 6-Monats-Leiter (G3/G4) und "
        "eskaliert Prio 1 bei Monat < p01. Stiller Exit solange m2a nicht "
        "aktiv — Cron kann vor dem Schnitt installiert werden."
    ),
    "config_section": "m2a (lesend) + alerts.pushover",
    "state_files": ["m2a_gates_state.json", "m2a_erwartungsbaender.json"],
    "self_tests": [],
    "scheduler_hooks": [],
    "health_check": None,
    "added_in": "R-B66 (28.08.2026)",
}

DATA = os.environ.get("M2A_DATA_DIR", "/app/data")
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"


def _load(name, default=None):
    try:
        with open(os.path.join(DATA, name), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def klassifiziere(ret_pct, baender):
    if ret_pct < baender["p01"]:
        return "UNTER p01 — Regelwerk-relevant", 1
    if ret_pct < baender["p05"]:
        return "unter p05 (erwartbar in 5% der Monate)", 0
    if ret_pct < baender["p50"]:
        return "im unteren Normalband", 0
    return "im oberen Normalband", 0


def g1_teil(renditen, vor, baender):
    """G1-Satz fuer den Vormonat -> (text, prio).

    R-B71 (26.10.2026): FX-neutral und nur M2a. Die alte Messung (Kontowert
    CHF / USDCHF) mass bei ~1 Mio GBP Bargeld vor allem GBP/USD (September
    -3.38% statt -0.76%). Fehlt ein Stichtag-Wert, meldet G1 das mit Prio 1
    statt still eine falsche Zahl zu klassifizieren."""
    e = (renditen or {}).get(vor)
    if not baender or e is None:
        return f"G1 {vor}: noch kein Monatswert/Baender — Aufbau.", 0
    ret = e.get("ret_pct")
    if ret is None:
        return (f"G1 {vor}: Messung UNVOLLSTAENDIG (Tagesschnappschuss ohne "
                f"geerbt-Anteil) — beim naechsten Claude-Check pruefen.", 1)
    urteil, prio = klassifiziere(ret, baender)
    return (f"G1 {vor}: {ret:+.2f}% (USD, FX-neutral, nur M2a) — {urteil} "
            f"(Baender p01 {baender['p01']}/p05 {baender['p05']}/"
            f"Median {baender['p50']}).", prio)


def index_teil(vergleich, index, vor):
    """R-B75 (13.11.2026): Vergleich mit dem Small-Cap-Index IWM — GO fuer
    echtes Geld verlangt, dass M2a ihn ueber denselben Zeitraum schlaegt."""
    teile = []
    if (index or {}).get(vor) is not None:
        teile.append(f"IWM {vor}: {index[vor]:+.2f}%.")
    if vergleich:
        teile.append(
            f"Seit Schnitt M2a {vergleich['m2a_pct']:+.2f}% vs IWM "
            f"{vergleich['index_pct']:+.2f}% ({vergleich['vorsprung_pp']:+.2f} pp, "
            f"{'vor' if vergleich['schlaegt_index'] else 'HINTER'} dem Index).")
    return " ".join(teile)


def main() -> int:
    import sys
    sys.path.insert(0, "/app")
    from app import m2a_motor
    from app.config_manager import load_config

    cfg = load_config()
    if not m2a_motor.ist_aktiv(cfg):
        print(f"[{datetime.now():%Y-%m-%d}] m2a nicht aktiv — still")
        return 0
    heute = datetime.now().date()
    if m2a_motor.handelstag_im_monat(heute) != 1 or heute.weekday() >= 5:
        print(f"[{heute}] kein 1. Handelstag — still")
        return 0

    state = _load("m2a_gates_state.json", {}) or {}
    monat = heute.strftime("%Y-%m")
    if state.get("last_g1") == monat:
        print(f"[{heute}] G1 fuer {monat} bereits gemeldet")
        return 0

    # R-B66f (31.08., Schnitt-Tag-Fund): die Baender liegen im Artefakt unter
    # resultate.Z3_H126_DS40 — der alte top-level-Read fand NICHTS und haette
    # jeden G1 als "Aufbau" gemeldet statt zu klassifizieren (gebaut != verdrahtet).
    _bd = _load("m2a_erwartungsbaender.json", {}) or {}
    baender = ((((_bd.get("resultate") or {}).get("Z3_H126_DS40") or {})
                .get("monats_baender"))
               or _bd.get("monats_baender") or {})
    schnitt = str((cfg.get("m2a") or {}).get("schnitt_datum", "2026-08-31"))
    _eh = _load("equity_history.json", []) or []
    rows = _eh if isinstance(_eh, list) else _eh.get("history", [])
    renditen = m2a_motor.monatsrenditen(
        rows, _load("trade_history.json", []) or [], schnitt)
    vor = (heute.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    g1_text, prio = g1_teil(renditen, vor, baender)
    text_teile = [g1_text]
    kum = m2a_motor.kumuliert_pct({m: e for m, e in renditen.items() if m <= vor})
    if kum is not None:
        text_teile.append(f"M2a seit Schnitt kumuliert {kum:+.2f}%.")
    # R-B75: Index-Vergleich (GO-Bedingung fuer echtes Geld)
    index = m2a_motor.index_renditen(rows, schnitt)
    idx_text = index_teil(m2a_motor.vergleich_mit_index(renditen, index, bis=vor),
                          index, vor)
    if idx_text:
        text_teile.append(idx_text)

    # R-B66e: zentrale Faelligkeit aus m2a_motor.leiter_status — die alte
    # Inline-Formel (>= 6) haette am 01.02.2027 mit nur 5 VOLLEN Monaten
    # den bindenden Entscheid angefordert. Korrekt: ab 2027-03 (6 volle
    # Monats-Returns Sep..Feb liegen vor).
    from app.m2a_motor import leiter_status
    ls = leiter_status(schnitt, heute)
    if ls["faellig"]:
        text_teile.append(
            "G3/G4: 6-MONATS-LEITER FAELLIG (6 volle Monate abgeschlossen) — "
            "beim naechsten Claude-Check das bindende Leiter-Urteil fahren!")
        prio = max(prio, 1)
    else:
        text_teile.append(
            f"Leiter-Monat {ls['leiter_monat']}/6 — "
            f"Entscheid ab {ls['entscheid_ab']}.")

    text = " ".join(text_teile)
    print(f"[{heute}] {text}")
    if not DRY_RUN:
        try:
            import urllib.parse
            import urllib.request
            po = ((cfg.get("alerts") or {}).get("pushover") or {})
            if po.get("user_key") and po.get("api_token"):
                daten = {"token": po["api_token"], "user": po["user_key"],
                         "title": "InvestPilot M2A-Gates (monatlich)",
                         "message": text, "priority": prio}
                urllib.request.urlopen(urllib.request.Request(
                    "https://api.pushover.net/1/messages.json",
                    data=urllib.parse.urlencode(daten).encode()), timeout=15)
        except Exception as e:
            print(f"  Pushover fehlgeschlagen (non-fatal): {e}")
        state["last_g1"] = monat
        with open(os.path.join(DATA, "m2a_gates_state.json"), "w",
                  encoding="utf-8") as f:
            json.dump(state, f)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
