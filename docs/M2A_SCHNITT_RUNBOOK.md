# M2A-Schnitt-Runbook — Montag 31.08.2026, VOR 15:30 CH (R-B66)

Voraussetzung: Regelwerk BINDEND freigegeben (28.08.), Code deployt und
Suite gruen (der Code traegt beide Modi; bis zum Flip verhaelt sich der
Bot exakt wie bisher — verifizierbar am Wochenende).

ARCHITEKTUR-VEREINFACHUNG ggue. Spec: KEINE Locks-Neufassung noetig.
Die manual_lock_overrides (SL -8 etc.) bleiben unveraendert — sie steuern
weiterhin die GEERBTEN Positionen. M2a-Positionen ignorieren die
M0-Exits per Code-Flag. Der Schnitt ist damit ein reiner Config-Patch.

## Schritte (je ~1 Minute, alle rollback-faehig)

1. GEERBT-LISTE einfrieren (aus den LIVE-IBKR-Positionen):
   docker exec: position_ids aller offenen Positionen ->
   data/m2a_geerbt.json {"eingefroren_am": ..., "position_ids": [...]}
   KONTROLLE: Anzahl == Anzahl offener Positionen (aktuell 15).

2. BAENDER-ARTEFAKT pruefen: data/m2a_erwartungsbaender.json vorhanden
   (Kopie von m2a_z3_baender.json; wird beim Wochenend-Deploy angelegt).

3. CONFIG-PATCH (via API/Container, dokumentiert):
   config["m2a"] = {"aktiv": true, "horizon_handelstage": 126,
                    "kauf_fenster_handelstage": 3,
                    "max_neukaeufe_pro_monat": 5,
                    "schnitt_datum": "2026-08-31"}
   config["risk_management"]["catastrophic_stop"]["pct"] = 40
   (E6 platziert im naechsten Zyklus fuer NEUE M2a-Kaeufe -40%-Stops;
   bestehende geerbte -20%-Stops bleiben unangetastet stehen.)

4. VERIFIKATION (Zyklus abwarten, ~5 Min):
   - Log: "M2A: ausserhalb des Kauf-Fensters" (KORREKTUR 28.08.: der
     31.08. ist Handelstag 21 des AUGUST — das Kauf-Fenster oeffnet erst
     Di 01.09. = Handelstag 1. Erste M2a-Kaeufe also DIENSTAG ab 15:30,
     max 5. Montag erwartet: Flip + Fenster-zu-Logzeile + Geerbte normal).
   - Keine SL/Trailing-Zeilen fuer neue Positionen; geerbte unveraendert.
   - Soak-/Dashboard-Karte zeigt M2a-Modus (M2a-Gates-Karte, R-B66b).
   - R-B66c: Exit-Forecast zeigt alle 15 Positionen mit GEERBT-Badge,
     Meta-Zeile "Config (M2a): ..."; WFO-Drift-Watchdog loggt beim
     naechsten Tageslauf den Skip "M2a aktiv — M0-Baselines pausiert".
   - R-B66d: WFO-Karte zeigt "Pausiert (M2a) — Gates G1-G5 uebernehmen";
     Positionen-Tabelle: alle 15 GEERBT-Badges, Verkaufen-Buttons bleiben
     (alle geerbt!). Das "M2a 🔒" erscheint erst ab Dienstag bei den
     ersten M2a-Kaeufen. Earnings-Watchlist traegt den Geerbten-Zusatz.
   - R-B66e: M2a-Karte Leiter-Feld zeigt "Monat 1/6" + "Entscheid ab
     2027-03" (NICHT "in 6 Mt" — Faelligkeits-Bug gefixt: Entscheid
     braucht 6 VOLLE Monats-Returns Sep..Feb, also ab 01.03.2027);
     Readiness-Gate #2 wird zum Leiter-Countdown; V12-Chips Time-Stop/
     Trail-SL tragen "nur Geerbte".

5. DOKU: CHANGELOG-Eintrag "SCHNITT VOLLZOGEN" + Memory + Recap.
   M0-Akte archivieren: data/roundtrip_pf_reference.json + Zwischencheck-
   Protokoll bleiben als M0-Archiv liegen; Meilenstein-Wecker (25/50/80)
   STILLLEGEN (Cron-Zeile auskommentieren — der RT-Zaehler misst M0).

## ROLLBACK (jederzeit, ohne Deploy)
   config["m2a"]["aktiv"] = false
   config["risk_management"]["catastrophic_stop"]["pct"] = 20
   -> Bot verhaelt sich im naechsten Zyklus exakt wie vor dem Schnitt
   (M0-Exits gelten wieder fuer alle; Kauf-Fenster weg). Geerbt-Liste
   ist dann bedeutungslos, kann liegen bleiben.

## Wochenend-Restarbeiten vor dem Schnitt
   - [x] Kern-Code + 11 Regelwerk-Tests (28.08., Suite 1709)
   - [ ] Inertes Code-Deploy (Fr 22:05 Host-Job, Verifikation danach)
   - [ ] data/m2a_erwartungsbaender.json auf VPS anlegen + Backup-Listen
   - [ ] Gates-Cron installieren (laeuft still bis Flip)
   - [ ] M2a-Dashboard-Karte (Sa/So)
   - [ ] Blueprint/Roadmap-Regeneration
