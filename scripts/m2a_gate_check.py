"""M2A-Gates-Wecker (R-B66) — G1-Monatspruefung + G3/G4-Leiter-Countdown.

Bindendes Regelwerk: docs/M2A_MOTOR_REGELWERK.md. Host-Cron TAEGLICH
(Mo-Fr 06:00 UTC, stdin-Pattern); das Skript entscheidet selbst:
- m2a nicht aktiv -> stiller Exit (Cron darf schon vor dem Schnitt laufen).
- G1 feuert genau EINMAL pro Monat (am 1. Handelstag): Vormonats-Rendite
  (USD-Basis: portfolio_total_value / usdchf_close aus equity_history)
  gegen die Modell-Baender aus data/m2a_erwartungsbaender.json.
  < p01 -> Prio 1 (Regelwerk-relevant), sonst Prio 0 (Protokoll).
- G3/G4: Countdown zur 6-Monats-Leiter im G1-Text; die Leiter selbst wird
  beim Claude-Check gefahren (Pushover erinnert im 6. Monat mit Prio 1).
Marker: data/m2a_gates_state.json (einmal pro Monat).
"""
import json
import os
from datetime import date, datetime

AUDIT_METADATA = {
    "purpose": (
        "M2A-Gates-Wecker (bindendes Regelwerk R-B65/66): meldet monatlich "
        "am 1. Handelstag die Vormonats-Rendite (USD) gegen die validierten "
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


def usd_equity_je_monat(rows):
    """Letzter Eintrag je Monat -> USD-Equity (CHF-Wert / USDCHF-Kurs)."""
    je_monat = {}
    for r in rows or []:
        d = str(r.get("date", ""))
        tv, fx = r.get("portfolio_total_value"), r.get("usdchf_close")
        if len(d) >= 7 and tv and fx:
            je_monat[d[:7]] = float(tv) / float(fx)
    return je_monat


def klassifiziere(ret_pct, baender):
    if ret_pct < baender["p01"]:
        return "UNTER p01 — Regelwerk-relevant", 1
    if ret_pct < baender["p05"]:
        return "unter p05 (erwartbar in 5% der Monate)", 0
    if ret_pct < baender["p50"]:
        return "im unteren Normalband", 0
    return "im oberen Normalband", 0


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

    baender = (_load("m2a_erwartungsbaender.json", {}) or {}).get(
        "monats_baender") or {}
    eq = usd_equity_je_monat(_load("equity_history.json", []) or [])
    monate = sorted(eq)
    schnitt = str((cfg.get("m2a") or {}).get("schnitt_datum", "2026-08-31"))
    text_teile = []
    prio = 0
    if len(monate) >= 2 and baender:
        vor, davor = monate[-1], monate[-2]
        ret = (eq[vor] / eq[davor] - 1) * 100
        urteil, prio = klassifiziere(ret, baender)
        text_teile.append(
            f"G1 {vor}: {ret:+.2f}% USD — {urteil} "
            f"(Baender p01 {baender['p01']}/p05 {baender['p05']}/"
            f"Median {baender['p50']}).")
    else:
        text_teile.append("G1: noch keine zwei Monats-Puntke/Baender — Aufbau.")

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
